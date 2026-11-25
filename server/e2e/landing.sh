#!/usr/bin/env bash
#
# Landing page + workspace-request E2E (THE FULL CHAIN).
#
# From a clean data/: a request POSTed at the pre-auth endpoint becomes a listed request, becomes
# an org and a one-time invite via `csa.py org approve`, and that invite signs a (curl) browser in
# through /api/ui-login into the new org. Every negative from the task doc's acceptance runs too:
# oracle-free duplicates, 413/400/429 abuse, double-approve, slug collision, domain uniqueness,
# XSS payload carried as data, and the plaintext invite existing NOWHERE but the approve output.
#
# Self-contained like onboarding.sh: isolated corpus in a tempdir (never touches server/data),
# no docker, no Anthropic account. The server runs CSA_AUTH=org — the mode the chain
# needs (token mode refuses to boot the moment the approved org exists; auth.boot_error).
#
#   server/e2e/run.sh landing
#
set -uo pipefail   # NOT -e: run every check, tally failures, exit non-zero at the end if any failed.

unset CSA_SERVER CSA_AUTH_TOKEN CSA_ROOT CSA_QUIET CSA_INTERVAL CSA_COOLDOWN

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"
API="$SERVER/apiserver.py"

C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0; SKIP=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; SKIP=$((SKIP+1)); }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }
check(){ if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (want '$2', got '$3')"; fi; }
# a here-string, not a pipe: under pipefail `printf | grep -q` fails when grep exits early on a big haystack
contains(){ if grep -qF -- "$3" <<<"$2"; then pass "$1"; else fail "$1 (missing '$3')"; fi; }
absent(){ if grep -qF -- "$3" <<<"$2"; then fail "$1 (unexpected '$3')"; else pass "$1"; fi; }

WORK="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/csa-landing-e2e.XXXXXX")" && pwd)"
PIDS=()
cleanup(){ for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
echo "workspace: $WORK"

PY=""
if [ -x "$SERVER/.venv/bin/python" ]; then
  PY="$SERVER/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  PY="uv run --project $SERVER python"          # keep cwd free: data/ is cwd-relative
fi
if [ -z "$PY" ] || ! $PY -c "import sqlalchemy" >/dev/null 2>&1; then
  echo "${C_R}cannot find a Python with the server deps.${C_0} Run 'uv sync' in $SERVER first." >&2
  exit 3
fi
echo "python:    $PY"

ADMIN="admin-landing-e2e-secret"
PORT=8793
BASE="http://127.0.0.1:$PORT"
APIDIR="$WORK/api"; mkdir -p "$APIDIR"
# The landing and its request form exist only where requests have somewhere to go: set the webhook
# setting first (nothing listens on port 9; a dead webhook is logged, never raised).
( cd "$APIDIR" && PYTHONPATH="$SERVER" $PY -c "
from analyzer import orgs, tenant
tenant.set_deployment(orgs.control_db(), request_webhook='http://127.0.0.1:9/hook')" )
# apiserver.WSREQ_PER_IP is 10: the chain makes five limiter-visible submits, the abuse section tops it up to ten.
( cd "$APIDIR" && env PYTHONPATH="$SERVER" \
    CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" CSA_BIND=127.0.0.1 CSA_API_PORT="$PORT" \
    $PY "$API" ) >"$WORK/api.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 80); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health")" = "200" ] && break
  sleep 0.25
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health")" = "200" ] \
  || { fail "server never became healthy"; cat "$WORK/api.log"; exit 1; }
pass "server up on $BASE (auth=org, clean data/)"

CLI(){ ( cd "$APIDIR" && env PYTHONPATH="$SERVER" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" \
        $PY "$SERVER/csa.py" "$@" ) 2>>"$WORK/cli.log"; }
CTL="$APIDIR/data/control.db"
ROWS(){ ( cd "$APIDIR" && env PYTHONPATH="$SERVER" $PY -c \
        "from analyzer import orgs; print(len(orgs.list_requests(status=None)))" ) 2>/dev/null; }

# --- the pre-auth surface ------------------------------------------------------------------------
section "pre-auth surface"
check "GET / with no cookie -> 200" 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")"
BUNDLE="$(ls "$SERVER"/ui/dist/assets/*.js 2>/dev/null | head -1)"
if [ -n "$BUNDLE" ]; then
  contains "landing copy is in the served bundle" "$(cat "$BUNDLE")" "Request a workspace"
else
  skip "ui/dist not built — bundle copy check (run npm run build in server/ui)"
fi
check "GET /api/workspace-request -> 404 (POST-only public path)" 404 \
  "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/workspace-request")"

# --- submit + oracle -----------------------------------------------------------------------------
section "submit + no-oracle duplicates"
B1="$(curl -s -X POST "$BASE/api/workspace-request" -H 'content-type: application/json' \
      -d '{"company":"Acme Corp","email":"dev@acme.com"}')"                              # limiter 1
check "first submit -> {\"ok\": true}" '{"ok": true}' "$B1"
check "row present in control.db" 1 "$(ROWS)"
B2="$(curl -s -X POST "$BASE/api/workspace-request" -H 'content-type: application/json' \
      -d '{"company":"Acme Corp","email":"dev@acme.com"}')"                              # limiter 2
check "exact resubmit -> byte-identical body" "$B1" "$B2"
B3="$(curl -s -X POST "$BASE/api/workspace-request" -H 'content-type: application/json' \
      -d '{"company":"  acme   CORP ","email":"DEV@ACME.com"}')"                         # limiter 3
check "case/whitespace variant -> byte-identical body" "$B1" "$B3"
check "duplicates created no extra rows" 1 "$(ROWS)"

# --- abuse ---------------------------------------------------------------------------------------
section "abuse"
head -c 102400 /dev/zero > "$WORK/big.bin"
check "100KB body -> 413 (refused before parsing)" 413 \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/workspace-request" \
     -H 'content-type: application/json' --data-binary @"$WORK/big.bin")"
check "non-JSON body -> 400" 400 \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/workspace-request" \
     -H 'content-type: application/json' -d 'not json')"
XSS='<img src=x onerror=alert(1)>'
curl -s -X POST "$BASE/api/workspace-request" -H 'content-type: application/json' \
  -d "{\"company\":\"$XSS\",\"email\":\"x@xss.com\"}" >/dev/null                         # limiter 4
check "script-tag company stored as a row (rendering stays literal in the UI)" 2 "$(ROWS)"
curl -s -X POST "$BASE/api/workspace-request" -H 'content-type: application/json' \
  -d '{"company":"Acme. Corp","email":"c@collide.com"}' >/dev/null                       # limiter 5
for i in 6 7 8 9 10; do                                                                   # limiter 6..10
  curl -s -X POST "$BASE/api/workspace-request" -H 'content-type: application/json' \
    -d '{"company":"  acme   CORP ","email":"DEV@ACME.com"}' >/dev/null
done
RL="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/workspace-request" \
     -H 'content-type: application/json' -d '{"company":"Sixth Co","email":"s@six.com"}')"
check "submit loop -> 429 once the per-IP window is spent" 429 "$RL"
check "the refused submit wrote no row" 3 "$(ROWS)"

# --- operator: list + approve --------------------------------------------------------------------
section "operator approve"
REQS="$(CLI org requests)"
contains "org requests lists the pending request" "$REQS" "Acme Corp"
contains "…and the XSS row as literal text" "$REQS" "$XSS"
RID="$(printf '%s\n' "$REQS" | awk '/dev@acme.com/ {print $1}' | head -1)"
[ -n "$RID" ] && pass "request id parsed: $RID" || fail "could not parse a request id"
APPROVE="$(CLI org approve "$RID" --domain acme.com)"
contains "approve prints the org" "$APPROVE" "org:     acme-corp"
INVITE="$(printf '%s\n' "$APPROVE" | awk '/^invite:/ {print $2}')"
case "$INVITE" in inv_*) pass "approve printed a one-time invite";; *) fail "no invite in approve output";; esac
contains "approve bound the Google domain" "$APPROVE" "domain:  acme.com"
# the plaintext invite exists ONLY in that output
if grep -a -q "$INVITE" "$CTL" 2>/dev/null; then fail "plaintext invite found in control.db"; else pass "invite not in control.db"; fi
AUDIT="$(CLI org audit --org acme-corp)"
contains "the approve is in the audit log" "$AUDIT" "acme-corp"
absent "…and the invite is not" "$AUDIT" "$INVITE"
absent "invite not in the server log" "$(cat "$WORK/api.log")" "$INVITE"
DOUBLE="$(CLI org approve "$RID" 2>&1)"; DRC=$?
check "double-approve exits non-zero" 1 "$DRC"
contains "…with a readable reason, not a traceback" "$DOUBLE" "already approved"
absent "…and no traceback" "$DOUBLE" "Traceback"
check "still exactly one acme org" 1 "$(CLI org list | grep -c '^acme-corp ')"

# --- slug collision ------------------------------------------------------------------------------
section "slug collision"
CID="$(CLI org requests | awk '/c@collide.com/ {print $1}')"
COLL="$(CLI org approve "$CID" 2>&1)"; CRC=$?
check "colliding approve exits non-zero" 1 "$CRC"
contains "…advising --id" "$COLL" "--id"
absent "…and no traceback" "$COLL" "Traceback"
COLL2="$(CLI org approve "$CID" --id acme-corp-two)"
contains "explicit --id resolves the collision" "$COLL2" "org:     acme-corp-two"

# --- domain uniqueness ---------------------------------------------------------------------------
section "domain uniqueness"
XID="$(CLI org requests | awk '/x@xss.com/ {print $1}')"
DOM="$(CLI org approve "$XID" --domain acme.com --id xss-org)"
contains "a second org cannot claim the bound domain (reported, invite kept)" "$DOM" "domain NOT bound"
contains "…the org and invite still minted" "$DOM" "invite:  inv_"

# --- sign-in: the invite opens the new org's dashboard -------------------------------------------
section "sign-in with the approved invite"
JAR="$WORK/cookies.txt"
LOGIN_CODE="$(curl -s -o "$WORK/login.json" -w '%{http_code}' -c "$JAR" -X POST "$BASE/api/ui-login" \
  -H 'content-type: application/json' \
  -d "{\"invite\":\"$INVITE\",\"email\":\"dev@acme.com\"}")"
check "ui-login with the printed invite -> 200" 200 "$LOGIN_CODE"
grep -q "csa_session" "$JAR" && pass "session cookie set" || fail "no session cookie in the jar"
ME="$(curl -s -b "$JAR" "$BASE/api/me")"
contains "/api/me is scoped to the NEW org" "$ME" '"org_id": "acme-corp"'
contains "…as the signed-in developer" "$ME" "dev@acme.com"
check "signed-in GET / -> 200 (the SPA; identical bytes by design)" 200 \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/")"
check "signed-out /api/me stays 401 (the landing is for strangers, data is not)" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/me")"
contains "org list shows the org with its minted token" "$(CLI org list)" "acme-corp"
check "…token count 1" 1 "$(CLI org list | awk '/^acme-corp /{print $(NF-1)}')"

# --- operator read surface: `csa.py org` is the only cross-workspace plane ------------------------
section "operator overview"
OV="$(CLI org overview)"
contains "org overview lists the workspaces approved from requests" "$OV" "xss-org"
absent "…and never an invite" "$OV" "inv_"

section "hygiene"
absent "no tracebacks in any CLI stderr" "$(cat "$WORK/cli.log" 2>/dev/null)" "Traceback"

printf "\n${C_B}== result ==${C_0}\n  PASS %d  FAIL %d  SKIP %d\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
