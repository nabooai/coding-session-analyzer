#!/usr/bin/env bash
#
# CSA SaaS org-layer E2E — ONE deployment, TWO orgs.
#
# The core assertion is NEGATIVE: an org-B credential gets ZERO org-A data from EVERY route class
# (GET, POST, MCP), including replays of org-A session ids and fact ids — which recall publishes as
# provenance, so they are not secrets.
#
# It also pins the leak classes two design reviewers found in the pre-org code, each of which the
# per-org-file design kills BY CONSTRUCTION. They are tested anyway: "impossible by construction" is a
# claim about today's code, and these are the exact regressions a future refactor would reintroduce.
#   * `ref` = sha1(claim) — an IDENTICAL claim in two orgs collides in every ref-keyed table, so org A
#     flagging it `wrong` used to hide org B's copy, and hctype's `UPDATE ... WHERE ref=?` rewrote it.
#   * corroboration (⟳N) counted sessions corpus-wide — leaking that another tenant found the same thing.
#   * /api/ingest took session_id from the BODY unchecked -> org B could overwrite org A's session.
#   * /api/pipeline, /api/me, the `person=` roster reply, and forget's error string each leaked identity.
#
# Self-contained, like trial.sh: isolated corpora in a tempdir (never touches server/data), a mocked
# `claude` CLI so extraction is deterministic and offline (no Anthropic account, no docker).
#
#   server/e2e/run.sh org
#
# The legacy single-tenant path is NOT re-tested here — server/e2e/trial.sh is that gate and
# must stay green untouched. NOTE: trial.sh's "cross-tenant" section tests DEPLOYMENT separation (two
# servers, two tokens) — the model this task replaces — and it asserts "single tenant == one shared corpus".
# That is correct for self-host and is why in-DB org isolation needed a new harness rather than an edit.
set -uo pipefail   # NOT -e: run every check, tally failures, exit non-zero at the end if any failed.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"
API="$SERVER/apiserver.py"
MCP="$SERVER/mcp_server.py"
MCPCALL="$HERE/_mcp_call.py"

C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0; SKIP=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; SKIP=$((SKIP+1)); }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/csa-org-e2e.XXXXXX")"
PIDS=()
cleanup(){ for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null
  if [ -n "${CSA_E2E_KEEP:-}" ]; then echo "kept workspace: $WORK"; else rm -rf "$WORK"; fi; }
trap cleanup EXIT
echo "workspace: $WORK"

PY=""
if [ -x "$SERVER/.venv/bin/python" ]; then PY="$SERVER/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then PY="uv run --project $SERVER python"; fi
if [ -z "$PY" ] || ! $PY -c "import mcp, cryptography, uvicorn" >/dev/null 2>&1; then
  echo "${C_R}cannot find a Python with the server deps.${C_0} Run 'uv sync' in $SERVER first." >&2
  exit 3
fi
echo "python:    $PY"

# --- mock `claude`: echoes FACTCLAIM=<token> from the transcript so each org's fact is distinctive -----
MOCKBIN="$WORK/bin"; mkdir -p "$MOCKBIN"
cat > "$MOCKBIN/claude" <<'MOCK'
#!/usr/bin/env bash
if [ "${1:-}" = "-p" ]; then
  input="$(cat)"
  # PLANNER prompt (nl_to_query) — distinct from extraction. If the question carries the ATTACK sentinel,
  # emit the cross-tenant ATTACH plan a malicious planner would; else a harmless fts plan. This is what
  # exercises the ATTACH-denial guard end to end through the real ask._run_plan.
  if printf '%s' "$input" | grep -q "Translate the question into ONE read-only query"; then
    if printf '%s' "$input" | grep -qi "attackplanx"; then
      printf '{"result":"{\\"engine\\":\\"sql\\",\\"query\\":\\"ATTACH DATABASE '"'"'data/orgs/org-a/analyzer.db'"'"' AS a; SELECT claim FROM a.hidden_context\\"}","total_cost_usd":0.0,"usage":{}}'
    else
      printf '{"result":"{\\"engine\\":\\"fts\\",\\"query\\":\\"nothing\\"}","total_cost_usd":0.0,"usage":{}}'
    fi
    exit 0
  fi
  claim="$(printf '%s' "$input" | grep -oE 'FACTCLAIM=[A-Za-z0-9 ._-]+' | head -1 | sed 's/^FACTCLAIM=//')"
  [ -z "$claim" ] && claim="a decision was recorded"
  python3 - "$claim" <<'PY'
import json, sys
c = sys.argv[1]
inner = json.dumps({"summary": c, "worked_on": [c],
    "hidden_context": [{"type": "decision", "scope": "organizational", "confidence": "high",
                        "claim": c, "evidence": c}],
    "entities": [{"id": "payments-service", "type": "service", "name": "payments-service"}],
    "relations": [], "actions": [], "links": [], "data_sources": [], "deployed_to_prod": False})
print(json.dumps({"result": inner, "total_cost_usd": 0.0, "usage": {}}))
PY
  exit 0
fi
exit 0
MOCK
chmod +x "$MOCKBIN/claude"
export PATH="$MOCKBIN:$PATH"

SRV="$WORK/srv"; mkdir -p "$SRV"        # cwd of both servers => data/ (corpora + control.db) lives here
ENVBASE=(env PYTHONPATH="$SERVER" PATH="$PATH")
ADMIN="admin-secret-$$"
APORT=8840; MPORT=8841
BASE="http://127.0.0.1:$APORT"; MBASE="http://127.0.0.1:$MPORT"

wait_health(){ for _ in $(seq 1 80); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health")" = "200" ] && return 0; sleep 0.25; done; return 1; }
wait_tcp(){ for _ in $(seq 1 80); do
    python3 -c "import socket;socket.create_connection(('127.0.0.1',$1),1).close()" 2>/dev/null && return 0; sleep 0.25; done; return 1; }

# in-org helper: run a python snippet with the server's env + cwd (so it sees the same data/)
srvpy(){ ( cd "$SRV" && "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" $PY - "$@" ); }

# code DESC EXPECTED URL [curl args...]
assert_code(){ local d=$1 e=$2 u=$3; shift 3
  local c; c=$(curl -s -o /dev/null -w '%{http_code}' "$@" "$u")
  [ "$c" = "$e" ] && pass "$d -> $c" || fail "$d -> got $c, want $e"; }
# absent DESC NEEDLE BODY  : the body must NOT contain the needle (the core negative)
assert_absent(){ local d=$1 n=$2 b=$3
  if printf '%s' "$b" | grep -qi -- "$n"; then fail "$d — LEAKED '$n' :: $(printf '%s' "$b" | head -c 200)"
  else pass "$d"; fi; }
assert_present(){ local d=$1 n=$2 b=$3
  if printf '%s' "$b" | grep -qi -- "$n"; then pass "$d"
  else fail "$d — missing '$n' :: $(printf '%s' "$b" | head -c 200)"; fi; }
# a miss ECHOES the query, so a recall is proven by found=true, never by the query term alone
assert_found(){ local d=$1 n=$2 b=$3
  if printf '%s' "$b" | grep -q '"found": true' && printf '%s' "$b" | grep -qi -- "$n"; then pass "$d"
  else fail "$d — not found :: $(printf '%s' "$b" | head -c 200)"; fi; }
# POST /api/ask the way a deliberate ask does: strict=false is the most permissive path, the right one
# for both a positive (the fact is reachable) and a negative (nothing of the other org is)
ask(){ curl -s -X POST "${3:-$BASE}/api/ask" -H "Authorization: Bearer $1" -H "Content-Type: application/json" \
         -d "{\"query\":\"$2\",\"strict\":false}"; }

# =====================================================================================================
section "0. boot: org mode + two orgs"
for _p in $APORT $MPORT; do
  if python3 -c "import socket,sys; s=socket.socket()
sys.exit(0 if s.connect_ex(('127.0.0.1',$_p))==0 else 1)" 2>/dev/null; then
    echo "${C_R}port $_p is already in use${C_0} — a stale server there would serve ANOTHER run's corpus," >&2
    echo "which can pass a cross-org negative for the wrong reason. Kill it and re-run:" >&2
    echo "  lsof -ti tcp:$_p | xargs kill" >&2
    exit 3
  fi
done
# `exec` so $! is the SERVER's pid, not the subshell's — otherwise cleanup orphans it on the port.
( cd "$SRV" && exec "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" CSA_BIND=127.0.0.1 CSA_API_PORT=$APORT \
    $PY "$API" ) >"$WORK/api.log" 2>&1 & PIDS+=($!)
( cd "$SRV" && exec "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" CSA_MCP_BIND=127.0.0.1 CSA_MCP_PORT=$MPORT \
    $PY "$MCP" --http ) >"$WORK/mcp.log" 2>&1 & PIDS+=($!)
wait_health && pass "apiserver up in CSA_AUTH=org on :$APORT" || { fail "api never healthy :: $(tail -5 "$WORK/api.log")"; }
wait_tcp $MPORT && pass "mcp --http up in CSA_AUTH=org on :$MPORT" || fail "mcp never up :: $(tail -5 "$WORK/mcp.log")"

# create orgs + mint per-user tokens through the REAL interface the sibling task calls
CREDS=$(srvpy <<'PY'
from analyzer import orgs
a, ai = orgs.create_org("Org A")
b, bi = orgs.create_org("Org B")
print(a); print(b); print(ai); print(bi)
print(orgs.mint_token("alice@orga.com", a, "laptop-alice"))
print(orgs.mint_token("ana@orga.com",  a, "laptop-ana"))
print(orgs.mint_token("bob@orgb.com",   b, "laptop-bob"))
print(orgs.mint_token("bea@orgb.com",   b, "laptop-bea"))
PY
)
OA=$(sed -n 1p <<<"$CREDS"); OB=$(sed -n 2p <<<"$CREDS")
IA=$(sed -n 3p <<<"$CREDS"); IB=$(sed -n 4p <<<"$CREDS")
TA=$(sed -n 5p <<<"$CREDS"); TA2=$(sed -n 6p <<<"$CREDS"); TB=$(sed -n 7p <<<"$CREDS"); TB2=$(sed -n 8p <<<"$CREDS")
[ -n "$TA" ] && [ -n "$TB" ] && [ "$OA" = "org-a" ] && [ "$OB" = "org-b" ] \
  && pass "two orgs created + 4 per-user tokens minted (mint_token interface)" \
  || { fail "org/token setup failed :: $CREDS"; }

# =====================================================================================================
section "1. ingest into both orgs (org comes from the credential, never the payload)"
ingest(){ # TOKEN SESSION_ID FACT [extra curl args...]
  local tok=$1 sid=$2 fact=$3; shift 3
  local body; body=$(python3 - "$sid" "$fact" <<'PY'
import json, sys
sid, fact = sys.argv[1], sys.argv[2]
lines = [
 {"timestamp":"2026-07-14T10:00:00Z","cwd":"/Users/x/Git/repo","message":{"role":"user","content":"planning"}},
 {"timestamp":"2026-07-14T10:00:01Z","cwd":"/Users/x/Git/repo","message":{"role":"assistant","content":[
    {"type":"text","text":f"Decision FACTCLAIM={fact} for the payments-service"}]}},
]
print(json.dumps({"session_id": sid, "content": "\n".join(json.dumps(l) for l in lines)}))
PY
)
  curl -s -X POST "$BASE/api/ingest" -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
       "$@" -d "$body"
}
R=$(ingest "$TA" "a-sess-1" "zorptastic"); printf '%s' "$R" | grep -q '"ok": true' \
  && pass "org A: alice ingests a-sess-1" || fail "org A ingest failed :: $R"
R=$(ingest "$TB" "b-sess-1" "quibblenaut"); printf '%s' "$R" | grep -q '"ok": true' \
  && pass "org B: bob ingests b-sess-1" || fail "org B ingest failed :: $R"
# the SAME claim text in both orgs -> the same content-addressed `ref` (the shared-namespace trap)
RA=$(ingest "$TA" "a-shared" "identicalclaimtext"); RB=$(ingest "$TB" "b-shared" "identicalclaimtext")
[ "$(printf '%s\n%s\n' "$RA" "$RB" | grep -c '"ok": true')" = "2" ] \
  && pass "the SAME claim text ingested into BOTH orgs" || fail "shared-claim ingest failed :: $RA $RB"
# org A rediscovers its fact in 3 sessions -> A should see corroboration, B must not inherit it
ingest "$TA" "a-corrob-2" "zorptastic" >/dev/null
ingest "$TA2" "a-corrob-3" "zorptastic" >/dev/null
# /api/ingest only queues; drain every org the way the etl would, or every negative below passes on an empty corpus
drain(){ ( cd "$SRV" && "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" $PY "$SERVER/csa.py" drain --all ) 2>&1; }
drain >>"$WORK/drain.log"

A_SID="a-sess-1"
A_FACT=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OA"))
print(c.execute("SELECT ref FROM hidden_context WHERE claim LIKE '%zorptastic%' LIMIT 1").fetchone()[0])
PY
)
[ -n "$A_FACT" ] && pass "captured an org-A fact id ($A_FACT) + session id ($A_SID) to replay as org B" \
                || fail "could not capture an org-A fact id"

# =====================================================================================================
section "2. ACCEPTANCE: an org-A user recalls a colleague's org-A session (HTTP + MCP)"
R=$(ask "$TA2" zorptastic)
assert_found "ana (org A) recalls alice's fact via /api/ask" "zorptastic" "$R"
R=$($PY "$MCPCALL" "$MBASE" "$TA2" ask '{"query":"zorptastic"}' 2>/dev/null)
assert_present "ana (org A) recalls alice's fact via MCP ask" "Recalled context for \"zorptastic\"" "$R"
assert_present "  ...with alice as its owner" "alice@orga.com" "$R"
R=$($PY "$MCPCALL" "$MBASE" "$TA" fact "{\"ref\":\"$A_FACT\"}" 2>/dev/null)
assert_present "alice fetches her org's fact by id via MCP fact" "zorptastic" "$R"

# =====================================================================================================
section "3. THE CORE NEGATIVE: an org-B credential gets ZERO org-A data (every route class)"
# --- GET class ---
for r in "/api/facts" "/api/corpus/sessions" "/api/sessions" "/api/questions" "/api/flags" "/api/team" \
         "/api/corpus/stats" "/api/pipeline"; do
  B=$(curl -s -H "Authorization: Bearer $TB" "$BASE$r")
  assert_absent "GET $r (org B) has no org-A fact" "zorptastic" "$B"
  assert_absent "GET $r (org B) has no org-A owner email" "orga.com" "$B"
done
B=$(curl -s -H "Authorization: Bearer $TB" "$BASE/api/team")
assert_present "GET /api/team (org B) DOES list org B's own members" "orgb.com" "$B"
# replay org A's session id on a GET
B=$(curl -s -H "Authorization: Bearer $TB" "$BASE/api/corpus/sessions/$A_SID")
assert_absent "GET /api/corpus/sessions/<org-A session id> as org B leaks no content" "zorptastic" "$B"
assert_present "  ...and answers with a plain not-found" "error" "$B"
# replay org A's fact id on a GET (facts?q=<hex ref> matches by id prefix — apiserver.facts())
B=$(curl -s -H "Authorization: Bearer $TB" "$BASE/api/facts?q=$A_FACT")
assert_absent "GET /api/facts?q=<org-A fact id> as org B returns nothing of A" "zorptastic" "$B"

# --- POST class ---
B=$(ask "$TB" zorptastic)
# NB: the miss reply ECHOES the query back, so assert on found=false plus ECHO-PROOF markers (an org-A
# owner email / session id can only appear if real org-A data crossed) — never on the query term itself.
assert_present "POST /api/ask (org B) for org-A's exact fact text -> found=false" '"found": false' "$B"
assert_absent  "  ...and carries no org-A owner email" "orga.com" "$B"
assert_absent  "  ...and carries no org-A session id" "$A_SID" "$B"
B=$(curl -s -X POST "$BASE/api/flag" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d "{\"ref\":\"$A_FACT\",\"label\":\"wrong\"}")
assert_absent "POST /api/flag on an org-A fact id as org B does not reveal the claim" "zorptastic" "$B"
printf '%s' "$B" | grep -q '"ok": false' && pass "  ...and is refused (no fact with that id, in B's corpus)" \
                                         || fail "  ...but was NOT refused :: $B"
B=$(curl -s -X POST "$BASE/api/hctype" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d "{\"ref\":\"$A_FACT\",\"type\":\"gotcha\"}")
printf '%s' "$B" | grep -q '"ok": false' && pass "POST /api/hctype on an org-A fact id as org B is refused" \
                                         || fail "hctype cross-org was NOT refused :: $B"
B=$(curl -s -X POST "$BASE/api/forget" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d "{\"session_id\":\"$A_SID\"}")
printf '%s' "$B" | grep -q '"ok": false' && pass "POST /api/forget on an org-A session id as org B is refused" \
                                         || fail "cross-org forget was NOT refused :: $B"
assert_absent "  ...and the refusal names no org-A email (uniform not-found, no enumeration oracle)" "orga.com" "$B"
# ...and org A's data is UNCHANGED after every one of those attempts
A=$(ask "$TA" zorptastic)
assert_found "org A's data is unchanged after org B's flag/hctype/forget attempts" "zorptastic" "$A"

# --- MCP class ---
B=$($PY "$MCPCALL" "$MBASE" "$TB" ask '{"query":"zorptastic"}' 2>/dev/null)
assert_present "MCP ask (org B) for org-A's fact text -> an honest miss" "real miss, not an outage" "$B"
assert_absent  "  ...and carries no org-A owner email" "orga.com" "$B"
assert_absent  "  ...and carries no org-A session id" "$A_SID" "$B"
B=$($PY "$MCPCALL" "$MBASE" "$TB" fact "{\"ref\":\"$A_FACT\"}" 2>/dev/null)
assert_absent "MCP fact(<org-A fact id>) as org B returns no content" "zorptastic" "$B"
B=$($PY "$MCPCALL" "$MBASE" "$TB" flag "{\"ref\":\"$A_FACT\",\"label\":\"wrong\"}" 2>/dev/null)
assert_absent "MCP flag(<org-A fact id>) as org B does not reveal the claim" "zorptastic" "$B"
# header spoofing on the MCP path: X-CSA-Org must be scrubbed and re-stamped from the credential
B=$($PY "$MCPCALL" "$MBASE" "$TB" ask '{"query":"zorptastic"}' "X-CSA-Org:$OA" "X-CSA-User:alice@orga.com" 2>/dev/null)
assert_present "MCP ask with a SPOOFED X-CSA-Org header -> still an honest miss" "real miss, not an outage" "$B"
assert_absent  "  ...no org-A data (the gate scrubs X-CSA-Org and re-stamps it from the credential)" "orga.com" "$B"

# --- the roster / "Known people" reply must not enumerate the other company's staff ---
B=$(curl -s -X POST "$BASE/api/ask" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d '{"query":"anything","person":"zzz-nobody"}')
assert_absent "an unknown person= from org B does not list org-A emails in its friendly reply" "orga.com" "$B"
B=$($PY "$MCPCALL" "$MBASE" "$TB" ask '{"query":"anything","person":"zzz-nobody"}' 2>/dev/null)
assert_absent "  ...same via MCP ask" "orga.com" "$B"

# --- the LLM SQL PLANNER must not ATTACH another org's file (the file-per-org isolation's one SQL bypass) ---
# The mock planner emits `ATTACH DATABASE 'data/orgs/org-a/analyzer.db'; SELECT claim FROM a.hidden_context`
# for the sentinel question. mode=ro blocks writes, NOT ATTACH — store.ro_connect's authorizer must.
B=$($PY "$MCPCALL" "$MBASE" "$TB" ask '{"query":"how many attackplanx decisions depend on the service"}' 2>/dev/null)
assert_absent "MCP planner ask (org B) cannot ATTACH org-A's db to read its facts" "zorptastic" "$B"
assert_absent "  ...and surfaces no org-A owner email that way" "orga.com" "$B"
# org A's own planner path still works for a benign relational question (guard isn't over-broad)
A=$($PY "$MCPCALL" "$MBASE" "$TA" ask '{"query":"how many decisions depend on the service"}' 2>/dev/null)
printf '%s' "$A" | grep -qiv "could not\|error" && pass "org A's own relational planner query still runs (ATTACH guard not over-broad)" \
  || fail "the ATTACH guard broke a legitimate planner query :: $A"

# --- the disk browser is GONE: it served the OPERATOR's own ~/.claude/projects to every tenant, so
# --- there was nothing correct for it to return. Retired, not merely gated: the route must 404, and a
# --- 200 here would mean an unknown /api path is falling through to the SPA shell again.
assert_code "GET /api/local/sessions (retired) -> 404" 404 \
  "$BASE/api/local/sessions" -H "Authorization: Bearer $TB"
assert_code "GET /api/local/sessions/<id> (retired) -> 404" 404 \
  "$BASE/api/local/sessions/anything" -H "Authorization: Bearer $TB"

# =====================================================================================================
section "4. cross-org WRITES: content-addressed ref must not let one org mutate another"
# Read the ref from EACH corpus rather than recomputing hc_ref() from the query token: the mock's
# FACTCLAIM regex allows spaces, so the stored claim is the whole trailing phrase. Reading both also
# PROVES the collision is real before we test that it is contained.
refof(){ srvpy <<REFPY
from analyzer import store
c = store.sqlite_db(store.org_db("$1"))
r = c.execute("SELECT ref FROM hidden_context WHERE claim LIKE '%identicalclaimtext%' LIMIT 1").fetchone()
print(r[0] if r else "NONE")
REFPY
}
SHARED_REF=$(refof "$OA"); SHARED_REF_B=$(refof "$OB")
{ [ "$SHARED_REF" = "$SHARED_REF_B" ] && [ "$SHARED_REF" != "NONE" ]; } \
  && pass "identical claim text in two orgs -> the SAME fact id ($SHARED_REF) — the collision is real" \
  || fail "precondition broken: refs differ or missing (A=$SHARED_REF B=$SHARED_REF_B)"
curl -s -X POST "$BASE/api/flag" -H "Authorization: Bearer $TA" -H "Content-Type: application/json" \
     -d "{\"ref\":\"$SHARED_REF\",\"label\":\"wrong\"}" >/dev/null
B=$(ask "$TB" identicalclaimtext)
assert_found "org A flagging a byte-identical claim 'wrong' does NOT hide it from org B's recall" \
               "identicalclaimtext" "$B"
curl -s -X POST "$BASE/api/hctype" -H "Authorization: Bearer $TA" -H "Content-Type: application/json" \
     -d "{\"ref\":\"$SHARED_REF\",\"type\":\"constraint\"}" >/dev/null
BT=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OB"))
r = c.execute("SELECT type FROM hidden_context WHERE ref='$SHARED_REF_B'").fetchone()
print(r[0] if r else "MISSING")
PY
)
[ "$BT" = "decision" ] && pass "org A's hctype pin does NOT rewrite org B's identical row (type still 'decision')" \
                       || fail "cross-org hctype UPDATE leaked: org B's type is now '$BT'"
# corroboration: A rediscovered its fact in 3 sessions; B's single sighting must not inherit ⟳
CA=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OA")); store.write_clusters(c)
print(c.execute("SELECT sessions FROM claim_clusters WHERE ref='$A_FACT'").fetchone()[0])
PY
)
CB=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OB")); store.write_clusters(c)
r = c.execute("SELECT sessions FROM claim_clusters WHERE ref='$SHARED_REF_B'").fetchone()
print(r[0] if r else 0)
PY
)
[ "${CA:-0}" -ge 2 ] && pass "org A sees its OWN cross-session corroboration (⟳$CA)" \
                     || fail "org A lost its corroboration signal (got '$CA')"
[ "${CB:-0}" = "1" ] && pass "org B's identical claim shows ⟳1 — no signal from the other tenant's sessions" \
                     || fail "corroboration leaked across orgs: org B sees ⟳$CB"
# entity clobber: both orgs mention 'payments-service'; each keeps its own row
EA=$(srvpy <<PY
from analyzer import store
for o in ("$OA", "$OB"):
    c = store.sqlite_db(store.org_db(o))
    print(o, c.execute("SELECT COUNT(*) FROM entities WHERE id='payments-service'").fetchone()[0])
PY
)
printf '%s' "$EA" | grep -q "$OA 1" && printf '%s' "$EA" | grep -q "$OB 1" \
  && pass "both orgs hold their OWN 'payments-service' entity row (no global-PK clobber)" \
  || fail "entity rows are not per-org :: $EA"

# =====================================================================================================
section "5. ingest: identity + org come from the credential, not the payload"
R=$(ingest "$TB" "b-spoof-1" "spoofcheck" -H "X-CSA-User: alice@orga.com")
printf '%s' "$R" | grep -q '"ok": true' && pass "org B ingest with a spoofed X-CSA-User is accepted..." \
                                        || fail "spoof ingest failed outright :: $R"
drain >>"$WORK/drain.log"
OWN=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OB"))
print(c.execute("SELECT user FROM sessions WHERE session_id='b-spoof-1'").fetchone()[0])
PY
)
[ "$OWN" = "bob@orgb.com" ] && pass "  ...and is attributed to the TOKEN's verified email (bob@orgb.com), not the spoof" \
                            || fail "spoofed X-CSA-User changed attribution: owner='$OWN'"
# a payload-asserted owner is likewise ignored
R=$(curl -s -X POST "$BASE/api/ingest" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d '{"session_id":"b-spoof-2","owner":"alice@orga.com","org_id":"'"$OA"'","content":"{\"timestamp\":\"2026-07-14T10:00:00Z\",\"cwd\":\"/x\",\"message\":{\"role\":\"user\",\"content\":\"FACTCLAIM=payloadspoof\"}}"}')
drain >>"$WORK/drain.log"
OWN2=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OB"))
r = c.execute("SELECT user FROM sessions WHERE session_id='b-spoof-2'").fetchone()
print(r[0] if r else "ABSENT")
PY
)
[ "$OWN2" = "bob@orgb.com" ] && pass "a payload 'owner'+'org_id' is ignored — owner=bob@orgb.com, org from the token" \
                             || fail "payload owner/org_id leaked through: owner='$OWN2'"
IN_A=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OA"))
print(c.execute("SELECT COUNT(*) FROM sessions WHERE session_id IN ('b-spoof-1','b-spoof-2')").fetchone()[0])
PY
)
[ "$IN_A" = "0" ] && pass "  ...and nothing of org B's landed in org A's corpus" || fail "org B wrote $IN_A row(s) into org A"

# --- the ingest-overwrite attack: org B POSTs org A's session id ---
R=$(ingest "$TB" "$A_SID" "hijacked")
drain >>"$WORK/drain.log"
STILL=$(ask "$TA" zorptastic)
assert_found "org B re-POSTing org A's session id does NOT destroy org A's session" "zorptastic" "$STILL"
AOWN=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OA"))
print(c.execute("SELECT user FROM sessions WHERE session_id='$A_SID'").fetchone()[0])
PY
)
[ "$AOWN" = "alice@orga.com" ] && pass "  ...and org A's session is still owned by alice@orga.com" \
                               || fail "org A's session was hijacked: owner='$AOWN'"
assert_absent "  ...and org A never sees org B's content for that id" "hijacked" "$STILL"

# =====================================================================================================
section "6. /api/me + operator disclosure"
R=$(curl -s -H "Authorization: Bearer $TB" "$BASE/api/me")
assert_present "/api/me returns the AUTHENTICATED principal (bob@orgb.com)" "bob@orgb.com" "$R"
assert_present "  ...with its org" "$OB" "$R"
assert_absent  "  ...and not the operator's own Claude account from the server's ~/.claude.json" "$(python3 -c "
import json,os
try: print(json.load(open(os.path.expanduser('~/.claude.json'))).get('oauthAccount',{}).get('emailAddress') or 'no-op-email')
except Exception: print('no-op-email')")" "$R"

# =====================================================================================================
section "7. revocation + invite rotation"
assert_code "org B token works on /api/facts before revoke" 200 "$BASE/api/facts" -H "Authorization: Bearer $TB2"
srvpy <<PY >/dev/null
from analyzer import orgs
orgs.revoke("$TB2")
PY
assert_code "revoked token -> 401 on /api/facts"        401 "$BASE/api/facts" -H "Authorization: Bearer $TB2"
assert_code "revoked token -> 401 on /api/ask (POST)"   401 "$BASE/api/ask"   -X POST -H "Authorization: Bearer $TB2" -d '{}'
assert_code "revoked token -> 401 on /api/ingest"       401 "$BASE/api/ingest" -X POST -H "Authorization: Bearer $TB2" -d '{}'
R=$($PY "$MCPCALL" "$MBASE" "$TB2" ask '{"query":"x"}' 2>/dev/null)
assert_present "revoked token -> 401 on MCP too" "HTTP 401" "$R"
# a revoked token must NOT fall through to the shared admin secret
assert_code "revoked token + the shared secret in X-CSA-Token still 401 (no fall-through)" 401 "$BASE/api/facts" \
  -H "Authorization: Bearer $TB2" -H "X-CSA-Token: $ADMIN"
assert_code "the OTHER org-B token is unaffected by the revoke" 200 "$BASE/api/facts" -H "Authorization: Bearer $TB"

# rotation: the old invite dies, minted tokens live
NEW_IB=$(srvpy <<PY
from analyzer import orgs
print(orgs.rotate_invite("$OB"))
PY
)
OLD_OK=$(srvpy <<PY
from analyzer import orgs
print(orgs.verify_invite("$IB"))
PY
)
NEW_OK=$(srvpy <<PY
from analyzer import orgs
print(orgs.verify_invite("$NEW_IB"))
PY
)
[ "$OLD_OK" = "None" ] && pass "rotating org B's invite kills the OLD invite" || fail "old invite still valid: $OLD_OK"
[ "$NEW_OK" = "$OB" ] && pass "the new invite resolves to org B" || fail "new invite broken: $NEW_OK"
assert_code "previously minted user tokens KEEP WORKING after invite rotation" 200 "$BASE/api/facts" \
  -H "Authorization: Bearer $TB"

# =====================================================================================================
section "8. cross-workspace admin is \`csa.py org\` on the box, never HTTP"
csa_org(){ ( cd "$SRV" && "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" $PY "$SERVER/csa.py" org "$@" ) 2>&1; }
assert_code "the shared secret is no credential once tenants exist"  401 "$BASE/api/admin/orgs" -H "Authorization: Bearer $ADMIN"
assert_code "GET /api/admin/orgs with a customer token -> 404 (no such plane)" 404 "$BASE/api/admin/orgs" -H "Authorization: Bearer $TB"
assert_code "POST /api/admin/org with a customer token -> 404"  404 "$BASE/api/admin/org" -X POST \
  -H "Authorization: Bearer $TB" -H "Content-Type: application/json" -d '{"name":"Evil"}'
R=$(csa_org tokens --org "$OA")
assert_present "csa.py org tokens lists org A's tokens (hashes + last_used, for revocation)" "alice@orga.com" "$R"
assert_absent  "  ...and the listing contains NO plaintext token" "$TA" "$R"
R=$(csa_org list)
assert_absent "csa.py org list carries no corpus content" "zorptastic" "$R"

# =====================================================================================================
section "9. boot guard: CSA_AUTH=token is not a mode any more"
( cd "$SRV" && "${ENVBASE[@]}" CSA_AUTH=token CSA_AUTH_TOKEN="$ADMIN" CSA_BIND=127.0.0.1 CSA_API_PORT=8849 \
    $PY "$API" ) >"$WORK/refuse.log" 2>&1
if [ $? -ne 0 ] && grep -q "not one of" "$WORK/refuse.log"; then
  pass "CSA_AUTH=token -> refuses to boot, naming the valid modes :: $(tail -1 "$WORK/refuse.log" | head -c 90)"
else
  fail "CSA_AUTH=token booted (only off|org are modes) :: $(tail -2 "$WORK/refuse.log")"
fi
R=$(csa_org create "Late Corp")
printf '%s' "$R" | grep -q "^org: *late-corp" && pass "csa.py org create adds a further org while in org mode" \
                                             || fail "org create failed in org mode :: $R"

# =====================================================================================================
section "10. migration/idempotency: default org == the historical layout, untouched"
DEF=$(srvpy <<'PY'
from analyzer import store
print(store.org_db("default"), store.org_db(None), store.org_dir("default"))
PY
)
SRVP="$(cd "$SRV" && pwd -P)"
[ "$DEF" = "$SRVP/data/analyzer.db $SRVP/data/analyzer.db $SRVP/data" ] \
  && pass "the default org IS data/analyzer.db — existing installs need no migration at all" \
  || fail "default org path changed (self-host would break): $DEF"
# per-org files exist and are genuinely distinct
ls "$SRV/data/orgs/$OA/analyzer.db" >/dev/null 2>&1 && ls "$SRV/data/orgs/$OB/analyzer.db" >/dev/null 2>&1 \
  && pass "each org has its own corpus FILE (data/orgs/<org>/analyzer.db) — the isolation boundary" \
  || fail "per-org corpus files missing :: $(find "$SRV/data" -name '*.db' | head)"
# idempotent: re-opening every db twice adds nothing
BEFORE=$(srvpy <<PY
from analyzer import store
c = store.sqlite_db(store.org_db("$OA"))
print(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
      len(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
PY
)
AFTER=$(srvpy <<PY
from analyzer import store
for _ in range(2):
    c = store.sqlite_db(store.org_db("$OA"))
print(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
      len(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
PY
)
[ "$BEFORE" = "$AFTER" ] && pass "re-running the schema/migration is idempotent (same tables, same rows: $AFTER)" \
                         || fail "migration not idempotent: $BEFORE -> $AFTER"
# per-org extraction + queue dirs are namespaced (the on-disk half of the session-id collision)
if [ -d "$SRV/data/orgs/$OA/extracted" ] && [ -d "$SRV/data/orgs/$OB/extracted" ]; then
  A_HAS=$(ls "$SRV/data/orgs/$OA/extracted" | grep -c "$A_SID" || true)
  B_HAS=$(ls "$SRV/data/orgs/$OB/extracted" | grep -c "$A_SID" || true)
  [ "$A_HAS" = "1" ] && [ "$B_HAS" = "1" ] \
    && pass "the same session id lives in BOTH orgs' extraction caches without collision" \
    || fail "extraction cache is not org-namespaced (A=$A_HAS B=$B_HAS for $A_SID)"
else
  fail "per-org extraction dirs missing :: $(find "$SRV/data" -maxdepth 3 -type d | head)"
fi

# =====================================================================================================
section "10b. session_id path traversal — the OTHER half of a per-org path (regression: hostile-prober find)"
# org_id is guarded, but session_id also becomes a FILENAME (queue + extraction cache). A '../' in it used
# to climb OUT of the org dir -> org B could DELETE org A's queued/cached files or WRITE into org A's drain
# queue (corpus poison). Reproduce the exact attack as org B and prove each sink now refuses.
mkdir -p "$SRV/data/orgs/$OA/extracted" "$SRV/data/orgs/$OA/pending"
echo '{"canary":1}' > "$SRV/data/orgs/$OA/extracted/canary.json"
echo '{"canary":1}' > "$SRV/data/orgs/$OA/pending/canary.json"
R=$(curl -s -X POST "$BASE/api/ingest" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d '{"session_id":"../../org-a/extracted/canary","content":"{\"timestamp\":\"2026-07-14T10:00:00Z\",\"cwd\":\"/x\",\"message\":{\"role\":\"user\",\"content\":\"FACTCLAIM=traversaldelete\"}}"}')
printf '%s' "$R" | grep -q '"code": "bad_request"' && pass "traversal session_id on /api/ingest -> 400 bad_request" \
  || fail "traversal ingest was not rejected :: $R"
[ -f "$SRV/data/orgs/$OA/extracted/canary.json" ] \
  && pass "  ...and org A's cached file is INTACT (no cross-tenant delete)" \
  || fail "org B DELETED org A's extraction cache via ../ traversal"
R=$(curl -s -X POST "$BASE/api/ingest" -H "Authorization: Bearer $TB" -H "Content-Type: application/json" \
      -d '{"session_id":"../../org-a/pending/INJECTED","content":"{\"timestamp\":\"2026-07-14T10:00:00Z\",\"cwd\":\"/x\",\"message\":{\"role\":\"user\",\"content\":\"FACTCLAIM=orgbinjectedlie\"}}"}')
printf '%s' "$R" | grep -q '"code": "bad_request"' && pass "traversal session_id (queue path) -> 400 bad_request" \
  || fail "traversal queue-write was not rejected :: $R"
[ ! -f "$SRV/data/orgs/$OA/pending/INJECTED.json" ] \
  && pass "  ...and NO org-B file was written into org A's drain queue (no corpus poison)" \
  || fail "org B WROTE into org A's pending queue via ../ traversal"
[ -f "$SRV/data/orgs/$OA/pending/canary.json" ] && pass "  ...and org A's existing queue file is untouched" \
  || fail "org A's queue canary vanished"
R=$(ingest "$TB" "b-normal-2" "normalid")
printf '%s' "$R" | grep -q '"ok": true' && pass "a normal session id still ingests (guard rejects only path-dangerous ids)" \
  || fail "the session_id guard is too strict :: $R"

# =====================================================================================================
section "11. org delete (the contractual right-to-delete)"
DEL=$(srvpy <<PY
from analyzer import orgs
print(orgs.delete_org("$OB"))
PY
)
printf '%s' "$DEL" | grep -q "'corpus_removed': True" && pass "delete_org removed org B's whole corpus in one step" \
                                                      || fail "delete_org did not remove the corpus :: $DEL"
[ ! -d "$SRV/data/orgs/$OB" ] && pass "  ...the org's data directory is gone from disk" \
                             || fail "org B's directory survived deletion"
assert_code "  ...and its user tokens no longer authenticate (401)" 401 "$BASE/api/facts" -H "Authorization: Bearer $TB"
A=$(ask "$TA" zorptastic)
assert_found "  ...and org A is completely unaffected" "zorptastic" "$A"

# =====================================================================================================
section "12. budget -> queue -> drain (a hosted org over budget must never LOSE a session)"
# a brand-new org C on a server with a $0.01 daily budget: its first extraction bills > that, so the
# NEXT ingest must queue rather than drop, and `csa.py drain --org` must extract it from the org's own dir.
CPORT=8842
( cd "$SRV" && exec "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" \
    CSA_BIND=127.0.0.1 CSA_API_PORT=$CPORT $PY "$API" ) >"$WORK/apic.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 80); do [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$CPORT/api/health")" = "200" ] && break; sleep 0.25; done
CBASE="http://127.0.0.1:$CPORT"
CTOK=$(srvpy <<PYC
from analyzer import orgs
c, _ = orgs.create_org("Org C")
print(c); print(orgs.mint_token("carol@orgc.com", c, "lap"))
PYC
)
OC=$(sed -n 1p <<<"$CTOK"); TC=$(sed -n 2p <<<"$CTOK")
srvpy <<PYC >/dev/null
from analyzer import store, tenant
tenant.set_config(store.org_db("$OC"), daily_usd=0.01)   # the Settings screen's write, no restart
PYC
# force a costs row so the org is "over" its $0.01 budget, then ingest
srvpy <<PYC >/dev/null
from analyzer import store
c = store.sqlite_db(store.org_db("$OC"))
store.record_cost(c, "extract", "sonnet", "seed", {}, 0.50)   # already spent $0.50 today > $0.01 budget
PYC
ingest_c(){ local body; body=$(python3 - "$1" "$2" <<'PYB'
import json, sys
sid, fact = sys.argv[1], sys.argv[2]
lines=[{"timestamp":"2026-07-14T10:00:00Z","cwd":"/x","message":{"role":"user","content":"planning"}},
 {"timestamp":"2026-07-14T10:00:01Z","cwd":"/x","message":{"role":"assistant","content":[{"type":"text","text":f"Decision FACTCLAIM={fact} for x"}]}}]
print(json.dumps({"session_id": sid, "content":"\n".join(json.dumps(l) for l in lines)}))
PYB
); curl -s -X POST "$CBASE/api/ingest" -H "Authorization: Bearer $TC" -H "Content-Type: application/json" -d "$body"; }
R=$(ingest_c "c-over-1" "budgetqueued")
printf '%s' "$R" | grep -q '"queued": true' \
  && pass "an over-budget hosted ingest QUEUES — not dropped" \
  || fail "over-budget ingest did not queue :: $R"
printf '%s' "$R" | grep -q '"ok": true' && pass "  ...and returns ok:true so the client won't retry/lose it" \
  || fail "over-budget ingest returned ok:false (client would keep retrying) :: $R"
[ -f "$SRV/data/orgs/$OC/pending/c-over-1.json" ] \
  && pass "  ...and the session is on disk in the ORG's own queue (data/orgs/$OC/pending)" \
  || fail "queued session is not in the org pending dir :: $(find "$SRV/data/orgs/$OC" -type f 2>/dev/null)"
# it must NOT have leaked into the default queue
[ ! -f "$SRV/data/pending/c-over-1.json" ] && pass "  ...and NOT in the default org's queue" \
  || fail "over-budget hosted session landed in data/pending (default) — cross-org"
# now drain org C explicitly (budget is bypassed on drain — it's the deliberate, operator-run spend)
DRAIN=$( cd "$SRV" && "${ENVBASE[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN" $PY "$SERVER/csa.py" drain --org "$OC" --all 2>&1 )
printf '%s' "$DRAIN" | grep -q "drained 1" && pass "csa.py drain --org $OC extracts the queued session (no data loss)" \
  || fail "drain did not extract the org's queued session :: $(printf '%s' "$DRAIN" | tail -3)"
GONE=$(ls "$SRV/data/orgs/$OC/pending/"*.json 2>/dev/null | wc -l | tr -d ' ')
[ "$GONE" = "0" ] && pass "  ...and the org's queue is now empty" || fail "queue not drained: $GONE left"
# and the drained fact is now recallable in org C (default org totally untouched)
RC=$(ask "$TC" budgetqueued "$CBASE")
printf '%s' "$RC" | grep -q '"found": true' && printf '%s' "$RC" | grep -q "budgetqueued" && pass "  ...and the drained session is now recallable in org C" \
  || fail "drained session not recallable :: $RC"

# =====================================================================================================
section "summary"
printf "  ${C_G}%d passed${C_0}, ${C_R}%d failed${C_0}, ${C_Y}%d skipped${C_0}\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] && { echo "  ${C_G}ORG E2E: GREEN${C_0}"; exit 0; } || { echo "  ${C_R}ORG E2E: RED${C_0}"; exit 1; }
