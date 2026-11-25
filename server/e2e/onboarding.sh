#!/usr/bin/env bash
#
# CSA one-line onboarding E2E harness.
#
# Proves the whole `curl <server>/install | sh` -> browser-authorize -> hooks path end to end, and
# fails loudly on any miss:
#   - installer: unsupported OS/arch exits non-zero leaving NOTHING; supported target installs the
#     binary + symlink, prints every path it wrote, and (piped, no TTY) ends with the setup line
#   - "nothing else changed": the scratch HOME is diffed before/after against the printed paths
#   - setup: word-pair code + cli-auth URL, browser approve, config 0600 with a minted per-user
#     token, org policy applied, `csa-client team` works with it
#   - consent: declining at the terminal writes nothing AND mints nothing (a PTY drives the prompt)
#   - hooks: exactly three additive entries, third-party hooks untouched, re-run does not duplicate
#   - ship: a SessionEnd ships the transcript with a planted secret REDACTED; sweep catches a
#     crashed session exactly once and history stays put
#   - auth: per-user token 200s /api/facts, revoked -> 401, admin token still works, X-CSA-User
#     spoofing cannot change the attributed identity
#   - negatives: unknown / expired / already-used nonces yield no credentials; no credential -> 401
#   - uninstall: removes ALL THREE hooks (an uninstall that keeps shipping is the worst bug here)
#
# Self-contained, like server/e2e/trial.sh: it stands up an isolated corpus in a
# tempdir (never touches server/data), mocks the `claude` CLI so extraction runs deterministically
# offline, and needs no docker and no Anthropic account.
#
#   server/e2e/run.sh onboarding
#
# NOTE on the compose: the task doc says "boot the trial compose". docker-compose.trial.yml pulls a
# PREBUILT image (ghcr.io/…/csa-server:latest) with no build stanza, so it cannot contain code from
# this branch — booting it would test the old server and false-green. We boot `apiserver.py`
# directly, exactly as trial.sh does; it is the same command the compose runs.
set -uo pipefail   # NOT -e: run every check, tally failures, exit non-zero at the end if any failed.

# HERMETIC: the client lets env BEAT config (CSA_SERVER/CSA_AUTH_TOKEN/… — see client/CLAUDE.md), so an
# ambient value in the runner's shell leaks straight into the clients under test. That either MASKS a real
# failure (an ambient admin CSA_AUTH_TOKEN makes the per-user-token checks pass for the WRONG reason — they
# would authenticate as admin) or points a test client at a REAL server (CSA_SERVER → a throwaway session
# shipped to prod). This harness drives the client only via flags, so clear the ambient ones first.
unset CSA_SERVER CSA_AUTH_TOKEN CSA_ROOT CSA_QUIET CSA_INTERVAL CSA_COOLDOWN

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"
CLIENT="$ROOT/client"
API="$SERVER/apiserver.py"

C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0; SKIP=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; SKIP=$((SKIP+1)); }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }
# check DESC EXPECTED ACTUAL
check(){ if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (want '$2', got '$3')"; fi; }
# a here-string, not a pipe: under pipefail `printf | grep -q` fails when grep exits early on a big haystack
contains(){ if grep -qF -- "$3" <<<"$2"; then pass "$1"; else fail "$1 (missing '$3')"; fi; }
absent(){ if grep -qF -- "$3" <<<"$2"; then fail "$1 (unexpected '$3')"; else pass "$1"; fi; }

# `cd && pwd` collapses the double slash a trailing-slash $TMPDIR produces (macOS): this harness
# compares paths the client PRINTS against paths it builds, and Go's filepath.Join normalizes.
WORK="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/csa-onboarding-e2e.XXXXXX")" && pwd)"
PIDS=()
cleanup(){ for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
echo "workspace: $WORK"

# --- resolve a python with the server's deps -----------------------------------------------------
PY=""
if [ -x "$SERVER/.venv/bin/python" ]; then
  PY="$SERVER/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  PY="uv run --project $SERVER python"          # keep cwd free: data/ is cwd-relative
fi
if [ -z "$PY" ] || ! $PY -c "import mcp, cryptography, uvicorn" >/dev/null 2>&1; then
  echo "${C_R}cannot find a Python with the server deps.${C_0} Run 'uv sync' in $SERVER first." >&2
  exit 3
fi
echo "python:    $PY"

# --- a mock `claude` CLI (deterministic, offline) ------------------------------------------------
MOCKBIN="$WORK/bin"; mkdir -p "$MOCKBIN"
cat > "$MOCKBIN/claude" <<'MOCK'
#!/usr/bin/env bash
if [ "${1:-}" = "mcp" ]; then
  echo "claude $*" >> "${CSA_MOCK_MCP_LOG:-/dev/null}"
  exit 0
fi
if [ "${1:-}" = "-p" ]; then
  input="$(cat)"
  claim="$(printf '%s' "$input" | grep -oE 'FACTCLAIM=[A-Za-z0-9 ._-]+' | head -1 | sed 's/^FACTCLAIM=//')"
  [ -z "$claim" ] && claim="a decision was recorded"
  python3 - "$claim" <<'PY'
import json, sys
c = sys.argv[1]
inner = json.dumps({"summary": c, "worked_on": [c],
    "hidden_context": [{"type": "decision", "scope": "organizational", "confidence": "high",
                        "claim": c, "evidence": c}],
    "entities": [], "relations": [], "actions": [], "links": [], "data_sources": [], "deployed_to_prod": False})
print(json.dumps({"result": inner, "total_cost_usd": 0.0, "usage": {}}))
PY
  exit 0
fi
exit 0
MOCK
chmod +x "$MOCKBIN/claude"
export PATH="$MOCKBIN:$PATH"

# --- build the client and publish it the way CI would --------------------------------------------
section "build"
DIST="$WORK/client-dist"; mkdir -p "$DIST"
GOOS_N="$(go env GOOS 2>/dev/null)"; GOARCH_N="$(go env GOARCH 2>/dev/null)"
if [ -z "$GOOS_N" ]; then
  echo "${C_R}go toolchain required to build csa-client.${C_0}" >&2; exit 3
fi
if (cd "$CLIENT" && CGO_ENABLED=0 go build -trimpath -o "$DIST/csa-client-$GOOS_N-$GOARCH_N" . 2>"$WORK/build.log"); then
  pass "csa-client builds for $GOOS_N/$GOARCH_N"
else
  fail "csa-client build failed:"; cat "$WORK/build.log"; exit 1
fi
# checksums.txt exactly as release.yml emits it (sha256sum format; macOS uses shasum -a 256)
if command -v sha256sum >/dev/null 2>&1; then (cd "$DIST" && sha256sum csa-client-* > checksums.txt)
else (cd "$DIST" && shasum -a 256 csa-client-* > checksums.txt); fi

SRV_ENV=(env PYTHONPATH="$SERVER")
launch_api(){ # NAME PORT [EXTRA_ENV=val ...]
  local name=$1 port=$2; shift 2
  local dir="$WORK/$name"; mkdir -p "$dir"
  ( cd "$dir" && "${SRV_ENV[@]}" "$@" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN_TOKEN" CSA_BIND=127.0.0.1 \
      CSA_API_PORT="$port" CSA_CLIENT_DIST="$DIST" $PY "$API" ) >"$dir.log" 2>&1 &
  PIDS+=($!)
}
wait_health(){ for _ in $(seq 1 80); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/api/health")" = "200" ] && return 0
    sleep 0.25; done; return 1; }

ADMIN_TOKEN="admin-shared-secret-e2e"
PORT=8791
BASE="http://127.0.0.1:$PORT"
launch_api api "$PORT"
if wait_health "$PORT"; then pass "server up on $BASE (auth=token)"; else
  fail "server never became healthy"; cat "$WORK/api.log"; exit 1; fi

# =================================================================================================
section "installer (Deliverable 1+2)"

# The scratch machine: a clean $HOME with nothing but curl+sh.
SCRATCH="$WORK/home"; mkdir -p "$SCRATCH"
snapshot(){ (cd "$1" && find . -mindepth 1 2>/dev/null | LC_ALL=C sort); }

# --- unsupported OS/arch: non-zero, and NOTHING left behind ---
UNAME_SHIM="$WORK/badarch"; mkdir -p "$UNAME_SHIM"
cat > "$UNAME_SHIM/uname" <<'U'
#!/bin/sh
case "${1:-}" in
  -s) echo "Linux" ;;
  -m) echo "mips64" ;;
  *)  echo "Linux" ;;
esac
U
chmod +x "$UNAME_SHIM/uname"
BEFORE_BAD="$(snapshot "$SCRATCH")"
BAD_OUT="$(curl -sSL "$BASE/install" | env HOME="$SCRATCH" PATH="$UNAME_SHIM:$PATH" sh 2>&1)"; BAD_RC=$?
if [ "$BAD_RC" -ne 0 ]; then pass "unsupported arch exits non-zero (rc=$BAD_RC)"; else
  fail "unsupported arch exited 0"; fi
contains "unsupported arch names the arch" "$BAD_OUT" "mips64"
contains "unsupported arch says nothing was installed" "$BAD_OUT" "Nothing was installed"
check "unsupported arch leaves no files" "$BEFORE_BAD" "$(snapshot "$SCRATCH")"

# --- the real thing: piped `curl | sh`, no TTY ---
BEFORE="$(snapshot "$SCRATCH")"
OUT="$(curl -sSL "$BASE/install" | env HOME="$SCRATCH" PATH="/usr/bin:/bin:/usr/sbin:/sbin" sh 2>&1)"; RC=$?
check "curl | sh exits 0 (no TTY)" "0" "$RC"
contains "prints the download URL" "$OUT" "==> Downloading $BASE/latest/csa-client-$GOOS_N-$GOARCH_N"
contains "verifies the sha256" "$OUT" "==> Verified sha256"
contains "prints the installed paths" "$OUT" "==> Installed $SCRATCH/.local/bin/csa-client -> $SCRATCH/.local/lib/csa/csa-client"
contains "no-TTY: ends with the setup instruction" "$OUT" "csa-client setup --server $BASE"
contains "no-TTY: warns \$PATH lacks ~/.local/bin" "$OUT" 'export PATH="$HOME/.local/bin:$PATH"'
absent "installer does not claim to have shipped anything" "$OUT" "Authorized"

[ -x "$SCRATCH/.local/lib/csa/csa-client" ] && pass "binary installed, executable" || fail "binary missing"
[ -L "$SCRATCH/.local/bin/csa-client" ] && pass "symlink installed" || fail "symlink missing"
check "installed binary runs" "$(cd "$CLIENT" && go run . version 2>/dev/null || echo dev)" \
      "$("$SCRATCH/.local/bin/csa-client" version 2>/dev/null)"

# "Nothing else on disk changed": the ONLY new paths are the ones it printed.
NEW_PATHS="$(comm -13 <(printf '%s\n' "$BEFORE") <(snapshot "$SCRATCH"))"
EXPECTED_PATHS="$(printf '%s\n' "./.local" "./.local/bin" "./.local/bin/csa-client" "./.local/lib" "./.local/lib/csa" "./.local/lib/csa/csa-client" | LC_ALL=C sort)"
check "installer touched ONLY the paths it printed" "$EXPECTED_PATHS" "$NEW_PATHS"

# Idempotency: re-running the one-liner must not corrupt or duplicate anything.
OUT2="$(curl -sSL "$BASE/install" | env HOME="$SCRATCH" PATH="/usr/bin:/bin:/usr/sbin:/sbin" sh 2>&1)"; RC2=$?
check "re-running the installer exits 0" "0" "$RC2"
check "re-run leaves the same paths" "$EXPECTED_PATHS" "$(comm -13 <(printf '%s\n' "$BEFORE") <(snapshot "$SCRATCH"))"

# The truncation guard: the script must be Content-Length delimited and end with `main "$@"`.
HDRS="$(curl -sS -D- -o /dev/null "$BASE/install")"
contains "/install sends Content-Length (truncation is loud, not silent)" "$HDRS" "Content-Length:"
contains "/install is served as a shell script, not the SPA fallback" "$HDRS" "text/x-shellscript"
check "installer ends with main \"\$@\" (a truncated script does nothing)" 'main "$@"' \
      "$(curl -sSL "$BASE/install" | tail -1)"
HDRS_BIN="$(curl -sS -D- -o /dev/null "$BASE/latest/csa-client-$GOOS_N-$GOARCH_N")"
contains "/latest binary sends Content-Length" "$HDRS_BIN" "Content-Length:"
check "unknown artifact 404s" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/latest/csa-client-linux-mips")"
check "traversal via /latest is refused" "404" \
      "$(curl -s --path-as-is -o /dev/null -w '%{http_code}' "$BASE/latest/../../etc/passwd")"

CLI="$SCRATCH/.local/bin/csa-client"
run_cli(){ env HOME="$SCRATCH" PATH="$MOCKBIN:/usr/bin:/bin" CSA_MOCK_MCP_LOG="$WORK/mcp.log" "$CLI" "$@"; }

# =================================================================================================
section "consent gate (declining mints NOTHING)"

# A PTY, because the consent prompt refuses a non-terminal stdin by design.
cat > "$WORK/pty_setup.py" <<'PYEOF'
import os, pty, select, subprocess, sys
answer, args = sys.argv[1], sys.argv[2:]
m, s = pty.openpty()
p = subprocess.Popen(args, stdin=s, stdout=s, stderr=s, close_fds=True)
os.close(s)
out = b""
sent = False
while True:
    r, _, _ = select.select([m], [], [], 30)
    if not r:
        break
    try:
        chunk = os.read(m, 4096)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
    if not sent and b"Type \"yes\" to agree" in out:
        os.write(m, (answer + "\n").encode()); sent = True
    if p.poll() is not None and not select.select([m], [], [], 0.2)[0]:
        break
p.wait(timeout=30)
sys.stdout.write(out.decode("utf-8", "replace"))
sys.exit(p.returncode)
PYEOF

DECLINE_OUT="$(env HOME="$SCRATCH" PATH="$MOCKBIN:/usr/bin:/bin" CSA_SETUP_NO_BROWSER=1 \
  python3 "$WORK/pty_setup.py" "no" "$CLI" setup --server "$BASE" 2>&1)"; DRC=$?
check "declining exits 0" "0" "$DRC"
contains "consent notice states the destination" "$DECLINE_OUT" "$BASE"
contains "consent notice states what ships" "$DECLINE_OUT" "Ships FULL transcripts"
contains "consent notice: every project folder ships unless narrowed" "$DECLINE_OUT" "Every project folder ships unless"
contains "consent notice: uninstall is how to stop" "$DECLINE_OUT" "Stop any time with csa-client uninstall"
contains "consent notice: history does NOT ship by default" "$DECLINE_OUT" "Your EXISTING sessions stay on this machine"
contains "declining says nothing was written" "$DECLINE_OUT" "Declined."
[ -f "$SCRATCH/.csa/config.json" ] && fail "declining wrote a config" || pass "declining wrote no config"
MINTED="$(cd "$WORK/api" && "${SRV_ENV[@]}" $PY -c "
from analyzer import orgs
con = orgs.control_db(); print(con.execute('SELECT COUNT(*) FROM user_tokens').fetchone()[0])" 2>/dev/null)"
check "declining minted NO token server-side" "0" "$MINTED"

# Automation case: stdin=/dev/null (Docker RUN / ssh '… </dev/null') with no --yes. /dev/null is a
# char device, so the TTY check false-passes; setup must still refuse safely and point at --yes.
NULLHOME="$WORK/nullhome"; mkdir -p "$NULLHOME"
NULL_OUT="$(env HOME="$NULLHOME" PATH="$MOCKBIN:/usr/bin:/bin" "$CLI" setup --server "$BASE" </dev/null 2>&1)"
contains "no-TTY setup points at --yes (not a mislabelled 'aborting init')" "$NULL_OUT" "pass --yes"
absent "no-TTY setup does not misname itself as init" "$NULL_OUT" "aborting init"
[ -f "$NULLHOME/.csa/config.json" ] && fail "no-TTY setup wrote a config" || pass "no-TTY setup wrote nothing"

# =================================================================================================
section "setup: browser authorize (Deliverable 3+4+5)"

# Pre-existing third-party hook + user setting: coexistence must survive setup.
mkdir -p "$SCRATCH/.claude"
cat > "$SCRATCH/.claude/settings.json" <<'J'
{
  "hooks": {
    "UserPromptSubmit": [{"matcher":"","hooks":[{"type":"command","command":"/opt/poco/bin/poco","args":["hook"]}]}],
    "PostToolUse": [{"matcher":"Write","hooks":[{"type":"command","command":"/bin/true"}]}]
  },
  "model": "opus"
}
J

# Run setup headlessly and approve it from "the browser" (a direct POST, as the doc prescribes).
SETUP_LOG="$WORK/setup.out"
( env HOME="$SCRATCH" PATH="$MOCKBIN:/usr/bin:/bin" CSA_MOCK_MCP_LOG="$WORK/mcp.log" \
    CSA_SETUP_NO_BROWSER=1 "$CLI" setup --server "$BASE" --yes >"$SETUP_LOG" 2>&1 ) &
SETUP_PID=$!
NONCE=""; CODE=""
for _ in $(seq 1 60); do
  NONCE="$(grep -oE 'nonce=[A-Za-z0-9_-]+' "$SETUP_LOG" 2>/dev/null | head -1 | cut -d= -f2)"
  CODE="$(grep -oE 'Verification code: [A-Z-]+' "$SETUP_LOG" 2>/dev/null | head -1 | sed 's/.*: //')"
  [ -n "$NONCE" ] && [ -n "$CODE" ] && break
  sleep 0.25
done
if [ -n "$NONCE" ]; then pass "setup printed a cli-auth URL with a nonce"; else
  fail "setup never printed a nonce"; cat "$SETUP_LOG"; fi
if [ -n "$CODE" ] && [ "$(printf '%s' "$CODE" | tr -cd '-' | wc -c | tr -d ' ')" = "1" ]; then
  pass "setup printed a word-pair verification code ($CODE)"
else fail "no word-pair code printed (got '$CODE')"; fi

# The page renders, shows the hostname, and asks for the code (it must NOT display it).
PAGE="$(curl -sS "$BASE/cli-auth?nonce=$NONCE&hostname=E2E-Box")"
contains "cli-auth page renders" "$PAGE" "Authorize CSA on"
contains "cli-auth page shows the hostname being authorized" "$PAGE" "E2E-Box"
contains "cli-auth page carries the disclosure" "$PAGE" "full transcripts"
contains "cli-auth page asks the user to TYPE the code" "$PAGE" "Verification code shown in your terminal"
absent  "cli-auth page does NOT display the code (anti-phishing)" "$PAGE" "$CODE"

# NEGATIVES first — a wrong code/invite must not authorize anyone.
BAD_CODE="$(curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$NONCE\",\"code\":\"WRONG-PAIR\",\"email\":\"e2e@x.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
  "$BASE/api/cli-auth/approve")"
contains "wrong word-pair is refused" "$BAD_CODE" '"ok": false'
BAD_INV="$(curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$NONCE\",\"code\":\"$CODE\",\"email\":\"e2e@x.com\",\"invite\":\"guessed\"}" \
  "$BASE/api/cli-auth/approve")"
contains "wrong invite credential is refused" "$BAD_INV" '"ok": false'

# The click.
APPROVE="$(curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$NONCE\",\"code\":\"$CODE\",\"email\":\"e2e@x.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
  "$BASE/api/cli-auth/approve")"
contains "Authorize mints the token" "$APPROVE" '"ok": true'
wait $SETUP_PID; SETUP_RC=$?
check "setup completes after the browser click" "0" "$SETUP_RC"
SETUP_OUT="$(cat "$SETUP_LOG")"
contains "setup reports the authorized identity" "$SETUP_OUT" "Authorized as e2e@x.com"
contains "setup fetched the org policy" "$SETUP_OUT" "Fetched org policy"
contains "setup printed the config path it wrote" "$SETUP_OUT" "$SCRATCH/.csa/config.json"
contains "setup printed the settings path it wrote" "$SETUP_OUT" "$SCRATCH/.claude/settings.json"
contains "setup points at folder scoping" "$SETUP_OUT" "csa-client only pick"
contains "setup points at the undo" "$SETUP_OUT" "csa-client uninstall"

check "config.json is 0600" "600" "$(stat -c '%a' "$SCRATCH/.csa/config.json" 2>/dev/null || stat -f '%OLp' "$SCRATCH/.csa/config.json")"
TOKEN="$(python3 -c "import json;print(json.load(open('$SCRATCH/.csa/config.json'))['auth_token'])")"
case "$TOKEN" in csa_*) pass "config carries a minted per-user token" ;; *) fail "config token is not a per-user token: $TOKEN" ;; esac
check "config identity is the VERIFIED email" "e2e@x.com" \
      "$(python3 -c "import json;print(json.load(open('$SCRATCH/.csa/config.json'))['identity'])")"
check "config records consent at the current policy version" "true" \
      "$(python3 -c "import json;c=json.load(open('$SCRATCH/.csa/config.json'));print(str(c['consent']['policy_version']>=2).lower())")"
check "backfill defaults OFF (history is not swept)" "false" \
      "$(python3 -c "import json;print(str(json.load(open('$SCRATCH/.csa/config.json')).get('backfill',False)).lower())")"
check "sweep_from stamped (bounds the catch-up to new sessions)" "true" \
      "$(python3 -c "import json;print(str(bool(json.load(open('$SCRATCH/.csa/config.json')).get('sweep_from'))).lower())")"

# `csa-client team` with the minted token (the doc's acceptance).
TEAM_OUT="$(run_cli team 2>&1)"; TEAM_RC=$?
check "csa-client team works with the per-user token" "0" "$TEAM_RC"

# =================================================================================================
section "hooks (Deliverable 3)"
HOOKS_JSON="$SCRATCH/.claude/settings.json"
count_csa(){ python3 - "$HOOKS_JSON" "$1" <<'PY'
import json, sys
root = json.load(open(sys.argv[1]))
n = 0
for e in root.get("hooks", {}).get(sys.argv[2], []):
    for h in e.get("hooks", []):
        cmd = h.get("command", "")
        first = cmd.split()[0].strip('"') if cmd else ""
        if first.endswith("csa-client"):
            n += 1
print(n)
PY
}
check "UserPromptSubmit: exactly one CSA hook" "1" "$(count_csa UserPromptSubmit)"
check "SessionEnd: exactly one CSA hook" "1" "$(count_csa SessionEnd)"
check "SessionStart: exactly one CSA hook" "1" "$(count_csa SessionStart)"
SETTINGS_BLOB="$(cat "$HOOKS_JSON")"
contains "third-party (Poco) hook preserved" "$SETTINGS_BLOB" "/opt/poco/bin/poco"
contains "unrelated PostToolUse hook preserved" "$SETTINGS_BLOB" "/bin/true"
contains "unrelated settings preserved" "$SETTINGS_BLOB" '"model": "opus"'
check "ship hook timeout is 240s, not the 3s ambient one" "240" \
      "$(python3 -c "
import json;r=json.load(open('$HOOKS_JSON'))
print([h['timeout'] for e in r['hooks']['SessionEnd'] for h in e['hooks'] if 'csa-client' in h.get('command','')][0])")"
contains "MCP registered with Claude Code" "$(cat "$WORK/mcp.log" 2>/dev/null)" "mcp add"
contains "MCP registration carries the per-user token" "$(cat "$WORK/mcp.log" 2>/dev/null)" "$TOKEN"

# Re-running setup must not duplicate the entries (double-ship guard).
( env HOME="$SCRATCH" PATH="$MOCKBIN:/usr/bin:/bin" CSA_MOCK_MCP_LOG="$WORK/mcp.log" \
    CSA_SETUP_NO_BROWSER=1 "$CLI" setup --server "$BASE" --yes >"$WORK/setup2.out" 2>&1 ) &
SETUP2_PID=$!
for _ in $(seq 1 60); do
  N2="$(grep -oE 'nonce=[A-Za-z0-9_-]+' "$WORK/setup2.out" 2>/dev/null | head -1 | cut -d= -f2)"
  C2="$(grep -oE 'Verification code: [A-Z-]+' "$WORK/setup2.out" 2>/dev/null | head -1 | sed 's/.*: //')"
  [ -n "$N2" ] && [ -n "$C2" ] && break
  sleep 0.25
done
curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$N2\",\"code\":\"$C2\",\"email\":\"e2e@x.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
  "$BASE/api/cli-auth/approve" >/dev/null
wait $SETUP2_PID
check "re-run: still exactly one UserPromptSubmit hook" "1" "$(count_csa UserPromptSubmit)"
check "re-run: still exactly one SessionEnd hook" "1" "$(count_csa SessionEnd)"
check "re-run: still exactly one SessionStart hook" "1" "$(count_csa SessionStart)"

# =================================================================================================
section "ship + sweep (Deliverable 6)"
PROJ="$SCRATCH/.claude/projects/-Users-e2e-Git-demo"
mkdir -p "$PROJ"
PLANTED="sk-fake1234567890abcdefghijklmnop"
cat > "$PROJ/sess-e2e-1.jsonl" <<J
{"message":{"role":"user","content":"my key is $PLANTED and FACTCLAIM=we chose sqlite for the corpus"}}
J
# the ship hook detaches (the shipping runs after it returns), and /api/ingest only queues: wait for the
# marker, then drain the queue the way the etl would
wait_marker(){ for _ in $(seq 1 40); do [ -f "$SCRATCH/.csa/shipped/$1" ] && return 0; sleep 0.25; done; return 1; }
drain(){ (cd "$WORK/api" && "${SRV_ENV[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN_TOKEN" $PY "$SERVER/csa.py" drain --all) >>"$WORK/drain.log" 2>&1; }
run_cli ship <<J >/dev/null 2>&1
{"hook_event_name":"SessionEnd","session_id":"sess-e2e-1","transcript_path":"$PROJ/sess-e2e-1.jsonl","cwd":"/tmp","reason":"other"}
J
wait_marker sess-e2e-1 && pass "SessionEnd ship leaves a marker" || fail "no marker after ship"
drain
FACTS="$(curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/facts?q=sqlite")"
contains "shipped session reached the corpus" "$FACTS" "sqlite"
absent "planted secret did NOT reach the server" "$FACTS" "$PLANTED"
STORED="$(curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/api/corpus/sessions" 2>/dev/null)"
absent "planted secret absent from the corpus session list" "$STORED" "$PLANTED"
check "session attributed to the token's verified email" "e2e@x.com" \
      "$(curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/api/facts?q=sqlite" | python3 -c "
import json,sys
d = json.load(sys.stdin)
rows = d if isinstance(d, list) else d.get('facts', d.get('rows', []))
print(rows[0].get('owner') or rows[0].get('user') or '?') if rows else print('?')" 2>/dev/null)"

# --- the crashed session: no SessionEnd ever fired, so no marker exists ---
cat > "$PROJ/sess-crashed.jsonl" <<J
{"message":{"role":"user","content":"FACTCLAIM=the crashed session still shipped"}}
J
# ...and an OLD transcript that predates setup: history the user did not consent to.
cat > "$PROJ/sess-ancient.jsonl" <<J
{"message":{"role":"user","content":"FACTCLAIM=ancient history nobody consented to"}}
J
touch -t 202001010101 "$PROJ/sess-ancient.jsonl"
# </dev/null: the sweep reads the SessionStart hook JSON from stdin and waits for it on an open one
run_cli ship --sweep </dev/null >/dev/null 2>&1
wait_marker sess-crashed && pass "sweep ships a crashed session (no SessionEnd)" \
  || fail "sweep did not ship the crashed session"
drain
[ -f "$SCRATCH/.csa/shipped/sess-ancient" ] && fail "sweep shipped unconsented history!" \
  || pass "sweep did NOT ship history (backfill was off)"
ANCIENT="$(curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/api/facts?q=ancient")"
absent "unconsented history never reached the server" "$ANCIENT" "ancient history nobody consented to"
MARK_BEFORE="$(cat "$SCRATCH/.csa/shipped/sess-crashed")"
run_cli ship --sweep </dev/null >/dev/null 2>&1
check "second sweep ships nothing (marker dedup)" "$MARK_BEFORE" "$(cat "$SCRATCH/.csa/shipped/sess-crashed")"
SWEEP_STDOUT="$(run_cli ship --sweep </dev/null 2>/dev/null)"
check "sweep prints NOTHING on stdout (SessionStart stdout is injected as context)" "" "$SWEEP_STDOUT"

# =================================================================================================
section "per-user token authn (Deliverable 4)"
check "per-user token 200s /api/facts" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE/api/facts")"
check "no credential -> 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/facts")"
check "garbage credential -> 401" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer csa_not_a_real_token" "$BASE/api/facts")"
check "admin CSA_AUTH_TOKEN still works" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/api/facts")"

# X-CSA-User spoofing must not change the attributed identity on an ingest. `content` must be real
# transcript JSONL — ingest.load_content parses it and a bare string yields "session not found".
SPOOF_BODY="$(python3 -c "
import json
transcript = json.dumps({'message': {'role': 'user', 'content': 'FACTCLAIM=spoof attempt session'}}) + '\n'
print(json.dumps({'session_id': 'sess-spoof', 'content': transcript, 'owner': 'victim@elsewhere.com'}))")"
SPOOF_RESP="$(curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "X-CSA-User: victim@elsewhere.com" \
  -H 'Content-Type: application/json' -d "$SPOOF_BODY" "$BASE/api/ingest")"
contains "the spoofed ingest was accepted (so the check below is meaningful)" "$SPOOF_RESP" '"ok": true'
drain
SPOOF_OWNER="$(cd "$WORK/api" && "${SRV_ENV[@]}" $PY -c "
from analyzer import store
con = store.sqlite_db()
r = con.execute(\"SELECT user FROM sessions WHERE session_id='sess-spoof'\").fetchone()
print(r[0] if r else 'missing')" 2>/dev/null)"
check "X-CSA-User + payload owner spoof IGNORED (token identity wins)" "e2e@x.com" "$SPOOF_OWNER"

# Revoke -> 401 everywhere; the admin credential is unaffected.
(cd "$WORK/api" && "${SRV_ENV[@]}" $PY -c "
from analyzer import orgs
assert orgs.revoke('$TOKEN') is True" >/dev/null 2>&1) && pass "token revoked via the org interface" \
  || fail "revoke failed"
check "revoked token -> 401" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE/api/facts")"
check "admin token unaffected by the revoke" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/api/facts")"

# =================================================================================================
section "web dashboard sign-in"
# Everything here runs against apiserver.py serving the BUILT ui/dist — never `npm run dev`, whose
# Vite server hands out the shell with no gate at all and would false-green every public-path check.
COOKIE_NAME="__Host-csa_session"

# --- the shell is reachable with no cookie, so a browser can render a login instead of raw 401 JSON
for path in / /index.html; do
  check "shell: $path loads with NO cookie" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")"
done
SHELL_HTML="$(curl -sS "$BASE/")"
absent "shell is HTML, not the 401 JSON body this task exists to remove" "$SHELL_HTML" '"code": "unauthorized"'
contains "shell is the SPA" "$SHELL_HTML" "<div id=\"app\""
ASSET="$(printf '%s' "$SHELL_HTML" | grep -oE '/assets/[A-Za-z0-9._-]+\.js' | head -1)"
if [ -n "$ASSET" ]; then
  check "shell: $ASSET loads with NO cookie" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$ASSET")"
  # The half-deploy guard: server ships, ui/dist doesn't, and the dashboard shows a raw 401 + a dev
  # hint telling a customer to run apiserver.py. The BUILT bundle must contain the login view.
  BUNDLE="$(curl -sS "$BASE$ASSET")"
  contains "the SHIPPED bundle contains the login view (stale ui/dist = the #1 half-deploy failure)" \
           "$BUNDLE" "The invite code is what grants access"
  absent "the shipped bundle carries no invite/token literal" "$BUNDLE" "$ADMIN_TOKEN"
else
  skip "asset checks (no /assets/*.js in the shell — run \`npm run build\` in server/ui)"
fi
check "a bogus deep path still yields the shell (SPA fallback), never a corpus route" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/no/such/page")"
check "/api/* is STILL gated with no cookie (only the shell went public)" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/facts")"

# --- sign in: the cookie, and every attribute the security argument rests on ---
# ONE login, captured two ways: -D- for the Set-Cookie attributes, -c for the jar the browser would
# keep. They MUST come from the same response — a second login mints a second token, and then the
# logout-revokes check below would revoke one token while replaying another and false-green.
CJAR="$WORK/cookies.txt"
LOGIN_HDRS="$(curl -sS -D- -c "$CJAR" -o "$WORK/login.json" -X POST -H 'Content-Type: application/json' \
  -d "{\"invite\":\"$ADMIN_TOKEN\",\"email\":\"webuser@x.com\"}" "$BASE/api/ui-login")"
SET_COOKIE="$(printf '%s' "$LOGIN_HDRS" | grep -i '^set-cookie:' | head -1)"
contains "ui-login sets the session cookie" "$SET_COOKIE" "$COOKIE_NAME="
for attr in HttpOnly Secure SameSite=Strict "Path=/" "Max-Age=2592000"; do
  contains "cookie carries $attr" "$SET_COOKIE" "$attr"
done
absent "cookie has no Domain (__Host- forbids it: no sibling-subdomain plant)" "$SET_COOKIE" "Domain="
contains "ui-login answers ok:true" "$(cat "$WORK/login.json")" '"ok": true'
absent "the token is NEVER echoed in the response body (cookie only)" "$(cat "$WORK/login.json")" "csa_"
WEB_TOKEN="$(printf '%s' "$SET_COOKIE" | sed -n "s/.*$COOKIE_NAME=\([^;]*\).*/\1/p")"
case "$WEB_TOKEN" in csa_*) pass "the cookie carries a per-user token" ;; *) fail "cookie value: $WEB_TOKEN" ;; esac

# --- with the cookie: the corpus is readable, and identity is the SIGNED-IN user ---
check "curl STORED the Secure cookie over loopback (if this fails, every check below is meaningless)" "1" \
      "$(grep -c "$COOKIE_NAME" "$CJAR" 2>/dev/null || echo 0)"
check "cookie JAR round-trip: /api/facts 200 (what the browser actually does)" "200" \
      "$(curl -s -b "$CJAR" -o /dev/null -w '%{http_code}' "$BASE/api/facts")"
contains "the corpus is actually served, not an empty shell" "$(curl -sS -b "$CJAR" "$BASE/api/facts")" '"total"'
# The /api/me trap: it must answer WHO IS ASKING, not the server box's own Claude account.
ME="$(curl -sS -b "$CJAR" "$BASE/api/me")"
check "/api/me returns the SIGNED-IN email, not the server's Claude account" "webuser@x.com" \
      "$(printf '%s' "$ME" | python3 -c 'import sys,json;print(json.load(sys.stdin)["email"])')"
ME_ADMIN="$(curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/api/me")"
[ -n "$ME_ADMIN" ] && pass "/api/me with the admin token still answers (server identity, unchanged)" \
  || fail "/api/me broke for the admin token"

# --- NEGATIVE: every way sign-in must fail closed ---
check "no cookie -> 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/facts")"
check "forged cookie -> 401" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: $COOKIE_NAME=csa_forged_not_real" "$BASE/api/facts")"
check "garbage (non-token) cookie -> 401" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: $COOKIE_NAME=totally-garbage" "$BASE/api/facts")"
check "the UNPREFIXED cookie name is not a credential" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: csa_session=$WEB_TOKEN" "$BASE/api/facts")"
check "the shared admin secret in a cookie is NOT a credential" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: $COOKIE_NAME=$ADMIN_TOKEN" "$BASE/api/facts")"
for bad_invite in '"invite":"wrong-code","email":"x@x.com"' '"invite":"","email":"x@x.com"' '"email":"x@x.com"'; do
  BAD_HDRS="$(curl -sS -D- -o "$WORK/bad.json" -X POST -H 'Content-Type: application/json' \
    -d "{$bad_invite}" "$BASE/api/ui-login")"
  check "bad invite ($bad_invite) -> 401" "401" "$(printf '%s' "$BAD_HDRS" | head -1 | grep -oE '[0-9]{3}')"
  if printf '%s' "$BAD_HDRS" | grep -qi '^set-cookie:'; then
    fail "a refused sign-in SET A COOKIE ($bad_invite)"
  else pass "refused sign-in sets NO cookie ($bad_invite)"; fi
done
# wrong vs blank invite must be byte-identical — no oracle for which half you got right
E_WRONG="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{"invite":"wrong","email":"x@x.com"}' "$BASE/api/ui-login")"
E_BLANK="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{"invite":"","email":"x@x.com"}' "$BASE/api/ui-login")"
check "wrong-invite and blank-invite errors are identical (no oracle)" "$E_WRONG" "$E_BLANK"
check "bad email -> 401, invite never consulted" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
         -d "{\"invite\":\"$ADMIN_TOKEN\",\"email\":\"not-an-email\"}" "$BASE/api/ui-login")"
for payload in '{"invite":123,"email":null}' '{"invite":{},"email":[]}' 'not json'; do
  TC="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$payload" "$BASE/api/ui-login")"
  case "$TC" in 400|401) pass "ui-login type-confusion handled cleanly ($TC)" ;; *) fail "ui-login type-confusion -> $TC (want 400/401, not 500)" ;; esac
done

# --- CSRF: SameSite=Strict is the control; this proves the same-SITE gap is closed too ---
check "cookie POST from a CROSS-SITE context -> 401" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'Sec-Fetch-Site: cross-site' \
         -H 'Content-Type: application/json' -d '{"ref":"x","label":"gold"}' "$BASE/api/flag")"
check "cookie POST from a SAME-SITE sibling subdomain -> 401 (Strict alone would allow this)" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'Sec-Fetch-Site: same-site' \
         -H 'Content-Type: application/json' -d '{"ref":"x","label":"gold"}' "$BASE/api/flag")"
check "login-CSRF: cross-site POST to ui-login -> 401 (cannot sign a victim into an attacker email)" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Sec-Fetch-Site: cross-site' \
         -H 'Content-Type: application/json' -d "{\"invite\":\"$ADMIN_TOKEN\",\"email\":\"attacker@evil.com\"}" \
         "$BASE/api/ui-login")"
check "the SPA's own same-origin POST still works" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'Sec-Fetch-Site: same-origin' \
         -H 'Content-Type: application/json' -d '{"ref":"no-such-ref","label":"gold"}' "$BASE/api/flag")"

# --- blast radius: a browser session reads and rates the corpus. Nothing else. ---
for danger in /api/forget /api/ingest /api/ask; do
  check "cookie-only POST $danger -> 403 (an XSS in the SPA cannot reach it)" "403" \
        "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'Content-Type: application/json' \
           -d '{"session_id":"sess-spoof","query":"x"}' "$BASE$danger")"
  # THE BYPASS: a junk bearer alongside the cookie must NOT escape containment. gate() still
  # authenticates off the cookie, so _browser_session must still classify this as a browser session.
  check "cookie + JUNK bearer POST $danger -> 403 (the _browser_session bypass — must stay closed)" "403" \
        "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'Authorization: Bearer totally-bogus' \
           -H 'Content-Type: application/json' -d '{"session_id":"sess-spoof","query":"x"}' "$BASE$danger")"
  check "cookie + JUNK X-CSA-Token POST $danger -> 403 (same bypass, proxy-header variant)" "403" \
        "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'X-CSA-Token: not-a-token' \
           -H 'Content-Type: application/json' -d '{"session_id":"sess-spoof","query":"x"}' "$BASE$danger")"
done
check "the SAME request with a header bearer is unrestricted (machine clients unaffected)" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
         -H 'Content-Type: application/json' -d '{"query":"anything","session":"s"}' "$BASE/api/ask")"
# logout is same-origin too (symmetric with login): a cross-site forced-logout is refused
check "cross-site POST /api/ui-logout -> 401 (no cross-site forced logout)" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$CJAR" -H 'Sec-Fetch-Site: cross-site' "$BASE/api/ui-logout")"

# --- the cookie is an HTTP-dashboard credential and must not authenticate MCP ---
# Cookies ignore the port, and the deploy path-routes /mcp onto this same hostname, so a browser
# WILL send this cookie there. identify's allow_cookie defaults to False precisely for this.
if [ -n "${MCP_PORT:-}" ]; then
  check "the session cookie does NOT authenticate the MCP gate" "401" \
        "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Cookie: $COOKIE_NAME=$WEB_TOKEN" \
           -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:$MCP_PORT/mcp")"
else
  # No MCP server in this harness: assert the gate contract directly instead of skipping silently.
  # CSA_AUTH must be set explicitly — SRV_ENV carries none, and the default (mode=off) allows
  # everything, which would make the "default rejects the cookie" assertion pass for the wrong reason.
  (cd "$WORK/api" && "${SRV_ENV[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN_TOKEN" $PY -c "
from analyzer import auth
h = auth._CIHeaders({'Cookie': auth.SESSION_COOKIE + '=$WEB_TOKEN'})
assert auth.identify(h, allow_cookie=True)[0] is True, 'opted-in identify() must accept the cookie'
assert auth.identify(h)[0] is False, 'default identify() must REJECT the cookie (this is the MCP call site)'" >/dev/null 2>&1) \
    && pass "identify rejects the cookie unless opted in (what keeps it out of MCP)" \
    || fail "identify's allow_cookie default is not fail-closed"
fi

# --- sign out: revokes the token AND clears the cookie ---
LOGOUT_HDRS="$(curl -sS -D- -o /dev/null -X POST -b "$CJAR" -c "$CJAR" "$BASE/api/ui-logout")"
LOGOUT_COOKIE="$(printf '%s' "$LOGOUT_HDRS" | grep -i '^set-cookie:' | head -1)"
contains "logout expires the cookie" "$LOGOUT_COOKIE" "Max-Age=0"
for attr in HttpOnly Secure SameSite=Strict "Path=/"; do
  contains "logout cookie matches session_cookie's $attr (or the browser keeps the original)" "$LOGOUT_COOKIE" "$attr"
done
check "after logout the jar no longer authenticates" "401" \
      "$(curl -s -b "$CJAR" -o /dev/null -w '%{http_code}' "$BASE/api/facts")"
# The one that matters: logout must REVOKE, not just forget. Replay the captured token as a bearer.
check "LOGOUT REVOKED the token server-side (replaying it as a bearer -> 401)" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $WEB_TOKEN" "$BASE/api/facts")"
check "...and as a cookie -> 401" "401" \
      "$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: $COOKIE_NAME=$WEB_TOKEN" "$BASE/api/facts")"

# --- typo recovery: the whole reason Sign out exists ---
TYPO_JAR="$WORK/typo.txt"
curl -sS -c "$TYPO_JAR" -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d "{\"invite\":\"$ADMIN_TOKEN\",\"email\":\"marr@example.com\"}" "$BASE/api/ui-login"
check "signed in with a typo'd email — the UI shows it (that is how you catch it)" "marr@example.com" \
      "$(curl -sS -b "$TYPO_JAR" "$BASE/api/me" | python3 -c 'import sys,json;print(json.load(sys.stdin)["email"])')"
curl -sS -X POST -b "$TYPO_JAR" -c "$TYPO_JAR" -o /dev/null "$BASE/api/ui-logout"
curl -sS -c "$TYPO_JAR" -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d "{\"invite\":\"$ADMIN_TOKEN\",\"email\":\"mara@example.com\"}" "$BASE/api/ui-login"
check "...sign out + sign in again fixes it" "mara@example.com" \
      "$(curl -sS -b "$TYPO_JAR" "$BASE/api/me" | python3 -c 'import sys,json;print(json.load(sys.stdin)["email"])')"

# --- two browsers, same person, at once (no single-session assumption) ---
J1="$WORK/b1.txt"; J2="$WORK/b2.txt"
for j in "$J1" "$J2"; do
  curl -sS -c "$j" -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d "{\"invite\":\"$ADMIN_TOKEN\",\"email\":\"twice@x.com\"}" "$BASE/api/ui-login"
done
check "two concurrent browser sessions for one email: first still 200" "200" \
      "$(curl -s -b "$J1" -o /dev/null -w '%{http_code}' "$BASE/api/facts")"
check "two concurrent browser sessions for one email: second also 200" "200" \
      "$(curl -s -b "$J2" -o /dev/null -w '%{http_code}' "$BASE/api/facts")"

# --- attribution: a UI flag lands under the COOKIE's verified email, not a typed field ---
# A REAL ref: flag() refuses one that isn't in hidden_context, so a made-up id would write no row and
# this check would "pass" against an empty result. Ask the corpus what it actually has.
REAL_REF="$(cd "$WORK/api" && "${SRV_ENV[@]}" $PY -c "
from analyzer import store
r = store.sqlite_db().execute('SELECT ref FROM hidden_context LIMIT 1').fetchone()
print(r[0] if r else '')" 2>/dev/null)"
if [ -n "$REAL_REF" ]; then
  FLAG_RESP="$(curl -sS -X POST -b "$J1" -H 'Content-Type: application/json' \
    -d "{\"ref\":\"$REAL_REF\",\"label\":\"gold\",\"note\":\"via web\",\"flagger\":\"impostor@evil.com\"}" \
    "$BASE/api/flag")"
  contains "the UI flag was accepted (so the attribution check below is meaningful)" "$FLAG_RESP" '"ok": true'
  FLAGGER="$(cd "$WORK/api" && "${SRV_ENV[@]}" $PY -c "
from analyzer import store
r = store.sqlite_db().execute(\"SELECT flagger FROM reviews WHERE kind='hc' AND ref=?\", ('$REAL_REF',)).fetchone()
print(r[0] if r else 'missing')" 2>/dev/null)"
  check "a UI flag attributes to the COOKIE's verified email, not the typed 'flagger'" "twice@x.com" "$FLAGGER"
else
  skip "flag attribution (the scratch corpus extracted no hidden_context rows)"
fi

# =================================================================================================
section "NEGATIVE: nonce replay / expiry"
NEW3="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{"hostname":"box"}' "$BASE/api/cli-auth/new")"
N3="$(printf '%s' "$NEW3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')"
C3="$(printf '%s' "$NEW3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["code"])')"
check "unknown nonce yields no credentials" "pending" \
      "$(curl -sS "$BASE/api/cli-auth/totally-made-up-nonce" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$N3\",\"code\":\"$C3\",\"email\":\"replay@x.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
  "$BASE/api/cli-auth/approve" >/dev/null
FIRST="$(curl -sS "$BASE/api/cli-auth/$N3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
check "approved nonce yields credentials once" "approved" "$FIRST"
SECOND="$(curl -sS "$BASE/api/cli-auth/$N3")"
check "REPLAY: already-used nonce yields no credentials" "pending" \
      "$(printf '%s' "$SECOND" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
absent "replayed nonce leaks no token" "$SECOND" "csa_"
RE_APPROVE="$(curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$N3\",\"code\":\"$C3\",\"email\":\"attacker@x.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
  "$BASE/api/cli-auth/approve")"
contains "REPLAY: re-approving a used nonce is refused" "$RE_APPROVE" '"ok": false'

# Expiry, over real HTTP: the pairing is aged past cliauth.TTL_SECONDS in control.db, as sixteen idle
# minutes would leave it.
EPORT=8792
launch_api api_ttl "$EPORT"
if wait_health "$EPORT"; then
  EBASE="http://127.0.0.1:$EPORT"
  ENEW="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{"hostname":"slow"}' "$EBASE/api/cli-auth/new")"
  EN="$(printf '%s' "$ENEW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')"
  EC="$(printf '%s' "$ENEW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["code"])')"
  ( cd "$WORK/api_ttl" && PYTHONPATH="$SERVER" $PY -c "
from analyzer import orgs
con = orgs.control_db()
con.execute('UPDATE cli_pairings SET created = ?', (orgs._now(-16 * 60),))
con.commit()" )
  EXP="$(curl -sS -X POST -H 'Content-Type: application/json' \
    -d "{\"nonce\":\"$EN\",\"code\":\"$EC\",\"email\":\"late@x.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
    "$EBASE/api/cli-auth/approve")"
  contains "EXPIRED nonce cannot be approved" "$EXP" '"ok": false'
  contains "expired nonce gets an actionable message" "$EXP" "expired"
  check "EXPIRED nonce yields no credentials from the poll" "pending" \
        "$(curl -sS "$EBASE/api/cli-auth/$EN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
else
  skip "expiry checks (second server did not start)"
fi

# =================================================================================================
section "hardening (installer + endpoint)"
# Host-header poisoning: /install bakes in a download URL, so a non-loopback Host with no
# CSA_PUBLIC_URL must be REFUSED, not baked into a script that then downloads from the attacker.
POISON_CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: evil.example.com" "$BASE/install")"
check "Host-poisoning: non-loopback Host w/o CSA_PUBLIC_URL is refused (not baked)" "421" "$POISON_CODE"
POISON_BODY="$(curl -s -H "Host: evil.example.com" "$BASE/install")"
absent "poison attempt never yields a script pointing at the attacker host" "$POISON_BODY" "evil.example.com"
# /install must not be cacheable (a shared cache + Host poisoning = served to victims).
contains "/install sends Cache-Control: no-store" "$(curl -sS -D- -o /dev/null "$BASE/install")" "no-store"
# CSA_PUBLIC_URL, when set, is authoritative and ignores the Host header.
PORT_PUB=8793; launch_api api_pub "$PORT_PUB" CSA_PUBLIC_URL="https://csa.example.com"
if wait_health "$PORT_PUB"; then
  PUB_BODY="$(curl -sS -H "Host: evil.example.com" "http://127.0.0.1:$PORT_PUB/install")"
  contains "CSA_PUBLIC_URL is authoritative over Host" "$PUB_BODY" 'CSA_BASE="https://csa.example.com"'
  absent "CSA_PUBLIC_URL mode ignores a poison Host" "$PUB_BODY" "evil.example.com"
else skip "CSA_PUBLIC_URL check (server did not start)"; fi
# A non-ASCII verification code must be a clean deny, never an unhandled 500 on this public route.
NEWH="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{"hostname":"h"}' "$BASE/api/cli-auth/new")"
NH="$(printf '%s' "$NEWH" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')"
UNICODE_CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  --data-binary "$(printf '{"nonce":"%s","code":"caf\xc3\xa9-se\xc3\xb1or","email":"d@x.com","invite":"%s"}' "$NH" "$ADMIN_TOKEN")" \
  "$BASE/api/cli-auth/approve")"
check "non-ASCII verification code -> clean 200 deny, not a 500 crash" "200" "$UNICODE_CODE"
# Type-confusion on the public approve endpoint must not 500 either.
for payload in '{"nonce":123,"code":true,"email":null,"invite":[]}' '{"nonce":{},"code":{},"email":{},"invite":{}}' 'not json'; do
  TC="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$payload" "$BASE/api/cli-auth/approve")"
  case "$TC" in 200|400) pass "type-confusion payload handled cleanly ($TC)" ;; *) fail "type-confusion -> $TC (want 200/400, not 500)" ;; esac
done
# /api/policy is public so setup can show an honest floor BEFORE consent.
check "/api/policy is reachable pre-auth (honest consent)" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/policy")"

# =================================================================================================
section "existing-user upgrade (watcher migration + no redaction downgrade)"
UP="$WORK/uphome"; mkdir -p "$UP/.claude"
# the old watcher is a launchd agent on macOS and a systemd user unit elsewhere
case "$GOOS_N" in
  darwin) WATCHER="$UP/Library/LaunchAgents/org.example.csa-client.plist" ;;
  *)      WATCHER="$UP/.config/systemd/user/csa-client.service" ;;
esac
mkdir -p "$(dirname "$WATCHER")"
# Pre-existing OLD-flow install: config at redaction=privacy + a fake login watcher.
env HOME="$UP" PATH="$MOCKBIN:/usr/bin:/bin" "$CLI" init --yes --server "$BASE" --token "$ADMIN_TOKEN" \
  --email hermes@example.com --redact-mode privacy --backfill=false >/dev/null 2>&1
printf '<plist><dict><key>Label</key><string>org.example.csa-client</string><key>ProgramArguments</key><array><string>/x/csa-client</string></array></dict></plist>' > "$WATCHER"   # a systemd unit is matched by name only
check "precondition: old config is redaction=privacy" "privacy" \
      "$(python3 -c "import json;print(json.load(open('$UP/.csa/config.json'))['redaction']['mode'])")"
# Now run the new setup on top of it.
( env HOME="$UP" PATH="$MOCKBIN:/usr/bin:/bin" CSA_MOCK_MCP_LOG="$WORK/mcp2.log" CSA_SETUP_NO_BROWSER=1 \
    "$CLI" setup --server "$BASE" --yes >"$WORK/upsetup.out" 2>&1 ) &
UPID=$!
for _ in $(seq 1 60); do
  UN="$(grep -oE 'nonce=[A-Za-z0-9_-]+' "$WORK/upsetup.out" 2>/dev/null | head -1 | cut -d= -f2)"
  UC="$(grep -oE 'Verification code: [A-Z-]+' "$WORK/upsetup.out" 2>/dev/null | head -1 | sed 's/.*: //')"
  [ -n "$UN" ] && [ -n "$UC" ] && break; sleep 0.25
done
curl -sS -X POST -H 'Content-Type: application/json' \
  -d "{\"nonce\":\"$UN\",\"code\":\"$UC\",\"email\":\"hermes@example.com\",\"invite\":\"$ADMIN_TOKEN\"}" \
  "$BASE/api/cli-auth/approve" >/dev/null
wait $UPID
check "upgrade keeps redaction=privacy (never silently downgraded to secrets)" "privacy" \
      "$(python3 -c "import json;print(json.load(open('$UP/.csa/config.json'))['redaction']['mode'])")"
UPTOK="$(python3 -c "import json;print(json.load(open('$UP/.csa/config.json'))['auth_token'])")"
case "$UPTOK" in csa_*) pass "upgrade replaced the shared token with a per-user token" ;; *) fail "upgrade token: $UPTOK" ;; esac
contains "upgrade retires the login watcher (no double-ship)" "$(cat "$WORK/upsetup.out")" "migrating off the login watcher"
if [ "$GOOS_N" = darwin ]; then
  [ -e "$WATCHER" ] && fail "watcher plist left behind after migration" || pass "watcher plist removed on migration"
else
  contains "the systemd unit's removal steps are printed" "$(cat "$WORK/upsetup.out")" "systemctl --user disable --now csa-client"
fi

# =================================================================================================
section "uninstall (nothing keeps shipping)"
run_cli uninstall >/dev/null 2>&1
for ev in UserPromptSubmit SessionEnd SessionStart; do
  check "uninstall removed the $ev hook" "0" "$(count_csa $ev)"
done
contains "uninstall preserved the third-party hook" "$(cat "$HOOKS_JSON")" "/opt/poco/bin/poco"
[ -e "$SCRATCH/.local/bin/csa-client" ] && fail "uninstall left the symlink on PATH" \
  || pass "uninstall removed the ~/.local/bin symlink"
[ -d "$SCRATCH/.local/lib/csa" ] && fail "uninstall left ~/.local/lib/csa" \
  || pass "uninstall removed ~/.local/lib/csa"

# =================================================================================================
printf "\n${C_B}== summary ==${C_0}\n"
printf "  ${C_G}%d passed${C_0}, ${C_R}%d failed${C_0}, ${C_Y}%d skipped${C_0}\n" "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  printf "  ${C_R}ONBOARDING E2E FAILED${C_0}\n"; exit 1
fi
printf "  ${C_G}ONBOARDING E2E GREEN${C_0}\n"
