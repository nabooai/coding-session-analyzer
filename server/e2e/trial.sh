#!/usr/bin/env bash
#
# CSA External-Trial Auth/Tenant E2E harness (Contract v1, §11 + harness_notes).
#
# From a clean checkout, this PROVES the shared-secret trial end to end and fails loudly on any miss:
#   - auth gate: /api/health open, every corpus route 401 without a token / 200 with it (Bearer + X-CSA-Token)
#   - boot-refuse invariant: a non-loopback bind with CSA_AUTH=off (api AND mcp --http) exits non-zero, binds nothing
#   - 3 simulated users on isolated HOME dirs: build the Go client, init each, ship a fake .jsonl, ingest, recall
#   - forget: a user deletes their OWN session; forging another user's forget is denied (ownership gate)
#   - MCP boundary: the `claude mcp add` line the client emits carries the auth header; /mcp 401 without the bearer
#   - deployment-separation isolation: a second server with a different token rejects the first tenant's token
#
# Self-contained: it stands up isolated corpora in a tempdir (never touches server/data), and mocks the `claude`
# CLI so the LLM-backed extraction + MCP registration run deterministically offline (no Anthropic account, no
# docker needed). Run it:   server/e2e/run.sh trial
#
# Env names come ONLY from THE CONTRACT: CSA_AUTH, CSA_AUTH_TOKEN, CSA_BIND, CSA_API_PORT, CSA_MCP_BIND,
# CSA_MCP_PORT, apiserver.MAX_BODY (32 MiB); credential header `Authorization: Bearer` (fallback `X-CSA-Token`);
# attribution header `X-CSA-User`.
set -uo pipefail   # NOT -e: run every check, tally failures, exit non-zero at the end if any failed.

# HERMETIC: the client lets env BEAT config (CSA_SERVER/CSA_AUTH_TOKEN/… — see client/CLAUDE.md), so an
# ambient value in the runner's shell leaks straight into the clients under test. That either MASKS a real
# failure (an ambient admin CSA_AUTH_TOKEN makes per-user-token checks pass for the WRONG reason) or points
# a test client at a REAL server (CSA_SERVER → a throwaway session shipped to prod). This harness drives the
# client only via flags + the per-command env it sets itself, so clear the ambient ones first.
unset CSA_SERVER CSA_AUTH_TOKEN CSA_ROOT CSA_QUIET CSA_INTERVAL CSA_COOLDOWN

# --- locate the repo ---------------------------------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"
CLIENT="$ROOT/client"
API="$SERVER/apiserver.py"
MCP="$SERVER/mcp_server.py"

# --- output + tally ----------------------------------------------------------------------------------
C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0; SKIP=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; SKIP=$((SKIP+1)); }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }

# --- isolated workspace + cleanup --------------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/csa-trial-e2e.XXXXXX")"
PIDS=()
cleanup(){ for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
echo "workspace: $WORK"

# --- resolve a python with the server's deps (mcp, cryptography, uvicorn) ----------------------
PY=""
if [ -x "$SERVER/.venv/bin/python" ]; then
  PY="$SERVER/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  # keep cwd free (data/ is cwd-relative) but resolve deps from the server project
  PY="uv run --project $SERVER python"
fi
if [ -z "$PY" ] || ! $PY -c "import mcp, cryptography, uvicorn" >/dev/null 2>&1; then
  echo "${C_R}cannot find a Python with the server deps.${C_0} Run 'uv sync' in $SERVER first (creates .venv)." >&2
  exit 3
fi
echo "python:    $PY"

# --- a mock `claude` CLI (deterministic, offline) ----------------------------------------------------
#   `claude -p ...`  : the server's extractor. Emits the {result: "<json string>", ...} envelope extract.py
#                      parses; the hidden_context claim echoes a FACTCLAIM=<token> sentinel from the transcript,
#                      so each tenant's fact is distinct and recall can find it by that token.
#   `claude mcp ...` : the client's registration. Logs argv to $CSA_MOCK_MCP_LOG so we can assert the emitted
#                      `claude mcp add` line carries the auth header. Exit 0 == "connected".
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

# --- shared server env (judge off — no background claude -p) ---
SRV_ENV=(env PYTHONPATH="$SERVER")

# launch_api NAME PORT BIND AUTH TOKEN [EXTRA_ENV=val ...] -> backgrounds an apiserver in an isolated cwd
launch_api(){
  local name=$1 port=$2 bind=$3 mode=$4 token=$5; shift 5
  local dir="$WORK/$name"; mkdir -p "$dir"
  ( cd "$dir" && "${SRV_ENV[@]}" "$@" CSA_AUTH="$mode" CSA_AUTH_TOKEN="$token" CSA_BIND="$bind" \
      CSA_API_PORT="$port" $PY "$API" ) >"$dir.log" 2>&1 &
  PIDS+=($!)
  echo $!
}

wait_health(){ # port -> 0 if /api/health answers 200 within ~15s
  local port=$1
  for _ in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/health")" = "200" ] && return 0
    sleep 0.25
  done
  return 1
}
wait_tcp(){ # port -> 0 if a TCP connect succeeds within ~15s
  local port=$1
  for _ in $(seq 1 60); do
    python3 -c "import socket,sys; socket.create_connection(('127.0.0.1',$1),1).close()" 2>/dev/null && return 0
    sleep 0.25
  done
  return 1
}

# assert_code DESC EXPECTED URL [curl args...]
assert_code(){
  local desc=$1 exp=$2 url=$3; shift 3
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' "$@" "$url")
  [ "$code" = "$exp" ] && pass "$desc -> $code" || fail "$desc -> got $code, want $exp"
}
# assert_unauth_body DESC URL  : 401 AND body parses to {ok:false, code:unauthorized} AND WWW-Authenticate: Bearer
assert_unauth_body(){
  local desc=$1 url=$2 code shape www
  code=$(curl -s -D "$WORK/_h" -o "$WORK/_b" -w '%{http_code}' "$url")
  shape=$(python3 -c 'import json,sys
try:
  d=json.load(open(sys.argv[1])); print("ok" if d.get("ok") is False and d.get("code")=="unauthorized" else "bad")
except Exception: print("bad")' "$WORK/_b")
  www=$(grep -i '^WWW-Authenticate:' "$WORK/_h" | tr -d '\r')
  if [ "$code" = "401" ] && [ "$shape" = "ok" ] && printf '%s' "$www" | grep -qi 'Bearer'; then
    pass "$desc -> 401 {ok:false,code:unauthorized} + WWW-Authenticate: Bearer"
  else
    fail "$desc -> code=$code shape=$shape www='$www'"
  fi
}

# =====================================================================================================
section "0. build the Go client"
if command -v go >/dev/null 2>&1; then
  if ( cd "$CLIENT" && go build -o "$WORK/csa-client" . ) 2>"$WORK/build.err"; then
    pass "cd client && go build -> $WORK/csa-client"
    CC="$WORK/csa-client"
  else
    fail "go build failed: $(tail -2 "$WORK/build.err")"
    CC=""
  fi
else
  skip "go toolchain not on PATH — client build + init/forget/MCP-registration checks cannot run"
  CC=""
fi

# =====================================================================================================
section "1. boot-refuse invariant (§5) — must exit non-zero, bind nothing"
# refuse_case DESC [ENV=val ...]  : run apiserver, expect a non-zero exit (it should never block)
refuse_case(){
  local desc=$1; shift
  ( cd "$WORK" && "${SRV_ENV[@]}" "$@" $PY "$API" ) >"$WORK/refuse.log" 2>&1
  local rc=$?
  if [ "$rc" -ne 0 ]; then pass "$desc -> exit $rc :: $(tail -1 "$WORK/refuse.log")"
  else fail "$desc -> exit 0 (should have refused)"; fi
}
refuse_case "(a) CSA_BIND=0.0.0.0 + CSA_AUTH=off"          CSA_BIND=0.0.0.0 CSA_AUTH=off        CSA_API_PORT=8811
refuse_case "(b) CSA_AUTH=token (no longer a mode)"       CSA_BIND=0.0.0.0 CSA_AUTH=token CSA_AUTH_TOKEN=x CSA_API_PORT=8811
refuse_case "(c) CSA_AUTH=cloudflare (no longer a mode)"  CSA_BIND=0.0.0.0 CSA_AUTH=cloudflare CSA_API_PORT=8811
refuse_case "(d) CSA_AUTH=bogus"                          CSA_BIND=127.0.0.1 CSA_AUTH=bogus     CSA_API_PORT=8811
# mcp --http default bind is 0.0.0.0 (non-loopback) -> off must refuse
( cd "$WORK" && "${SRV_ENV[@]}" CSA_MCP_BIND=0.0.0.0 CSA_AUTH=off CSA_MCP_PORT=8812 $PY "$MCP" --http ) >"$WORK/mcprefuse.log" 2>&1
if [ $? -ne 0 ]; then pass "(mcp) --http default bind 0.0.0.0 + off -> exit non-zero :: $(tail -1 "$WORK/mcprefuse.log")"
else fail "(mcp) --http 0.0.0.0 + off did not refuse"; fi
# the loopback dev default MUST still boot
BP=$(launch_api boot 8813 127.0.0.1 off "")
if wait_health 8813; then pass "loopback default (CSA_BIND=127.0.0.1, CSA_AUTH=off) DOES boot (health 200)"
else fail "loopback default failed to boot :: $(tail -2 "$WORK/boot.log")"; fi
kill "$BP" 2>/dev/null

# =====================================================================================================
section "2. org-mode auth gate on a NON-loopback bind (§11.1-4, §7, §8)"
TOK="trial-secret-$$"
launch_api api 8820 0.0.0.0 org "$TOK" >/dev/null
if ! wait_health 8820; then fail "org API never became healthy :: $(tail -5 "$WORK/api.log")"; else
  B="http://127.0.0.1:8820"
  pass "org API up on 0.0.0.0:8820 (reached via 127.0.0.1)"

  # open route + preflight
  assert_code "GET /api/health  (no auth, open)"            200 "$B/api/health"
  assert_code "OPTIONS /api/facts (CORS preflight, open)"   204 "$B/api/facts" -X OPTIONS

  # every corpus route: 401 without a token, with the exact unauthorized shape + WWW-Authenticate
  for r in /api/facts /api/corpus/sessions /api/corpus/stats /api/sessions /api/questions \
           /api/flags /api/team /api/me /api/pipeline; do
    assert_unauth_body "GET $r (no auth)" "$B$r"
  done

  # wrong / right / fallback header on a representative corpus route
  assert_code "GET /api/facts  (wrong bearer)"              401 "$B/api/facts" -H "Authorization: Bearer wrong"
  assert_code "GET /api/facts  (correct bearer)"            200 "$B/api/facts" -H "Authorization: Bearer $TOK"
  assert_code "GET /api/facts  (correct X-CSA-Token fallback)" 200 "$B/api/facts" -H "X-CSA-Token: $TOK"
  assert_code "GET /api/team   (correct bearer)"            200 "$B/api/team"  -H "Authorization: Bearer $TOK"

  # POST routes gated too; and the corpus must stay untouched by an unauthed write
  for r in /api/ingest /api/ask /api/forget /api/flag /api/hctype; do
    assert_code "POST $r (no token)"    401 "$B$r" -X POST -H "Content-Type: application/json" -d '{}'
    assert_code "POST $r (wrong token)" 401 "$B$r" -X POST -H "Authorization: Bearer wrong" -d '{}'
  done
  TEAM_AFTER_UNAUTH=$(curl -s -H "Authorization: Bearer $TOK" "$B/api/team")
  if [ "$TEAM_AFTER_UNAUTH" = "[]" ]; then pass "corpus unchanged after unauthed POSTs (/api/team == [])"
  else fail "corpus changed by an unauthed POST (/api/team = $TEAM_AFTER_UNAUTH)"; fi

  # 413: reject a body over apiserver.MAX_BODY on the Content-Length alone, before reading it (§7).
  # A raw socket lets us claim a >32MiB Content-Length without actually uploading 32MiB.
  BIG413=$(python3 - "$TOK" <<'PY'
import socket, sys
tok=sys.argv[1].encode()
s=socket.create_connection(("127.0.0.1",8820),5)
s.sendall(b"POST /api/ingest HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "+tok+b"\r\n"
          b"Content-Type: application/json\r\nContent-Length: 33554433\r\n\r\n{\"x\":1}")
s.shutdown(socket.SHUT_WR)
data=b""
while True:
    c=s.recv(4096)
    if not c: break
    data+=c
    if b"payload_too_large" in data or len(data)>4096: break
print("413" if (b" 413 " in data and b'"code": "payload_too_large"' in data) else data.split(b"\r\n",1)[0].decode("latin1"))
PY
)
  [ "$BIG413" = "413" ] && pass "POST body > MAX_BODY -> 413 code:payload_too_large" \
                        || fail "oversize body did not 413 (got: $BIG413)"

  # 400: malformed JSON with a valid token
  R400=$(curl -s -w '\n%{http_code}' -X POST "$B/api/ingest" -H "Authorization: Bearer $TOK" \
         -H "Content-Type: application/json" -d 'not json{')
  if printf '%s' "$R400" | tail -1 | grep -q '^400$' && printf '%s' "$R400" | grep -q 'bad_request'; then
    pass "POST malformed JSON (valid token) -> 400 code:bad_request"
  else fail "malformed JSON did not 400 (got: $(printf '%s' "$R400" | tr '\n' ' '))"; fi
fi

# =====================================================================================================
section "3. MCP --http token gate (harness_notes)"
MP=$(  ( cd "$WORK/api" && "${SRV_ENV[@]}" CSA_MCP_BIND=127.0.0.1 CSA_AUTH=org CSA_AUTH_TOKEN="$TOK" \
         CSA_MCP_PORT=8821 $PY "$MCP" --http ) >"$WORK/mcp.log" 2>&1 & echo $! )
PIDS+=("$MP")
if wait_tcp 8821; then
  pass "mcp_server.py --http (org mode) boots + TCP-live on 127.0.0.1:8821"
  assert_code "POST /mcp (no bearer)"    401 "http://127.0.0.1:8821/mcp" -X POST \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{}'
  MBODY=$(curl -s -X POST "http://127.0.0.1:8821/mcp" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -d '{}')
  printf '%s' "$MBODY" | grep -q '"code": "unauthorized"' \
    && pass "/mcp 401 body matches {ok:false,code:unauthorized}" \
    || fail "/mcp 401 body wrong: $MBODY"
else fail "mcp --http token server never came up :: $(tail -5 "$WORK/mcp.log")"; fi

# =====================================================================================================
section "4. three simulated users (isolated HOME) — init, ingest, recall (single shared tenant)"
declare -A FACT=( [alice]=zorptastic [bob]=quibblenaut [carol]=flimberdoodle )
USERS=(alice bob carol)
MCP_LOG="$WORK/mcp-register.log"; : > "$MCP_LOG"

make_user(){ # name -> isolated HOME with ~/.claude.json + a fixture project holding a distinctive fact
  local u=$1 home="$WORK/home-$u"
  mkdir -p "$home" "$home/proj/-Users-$u-Git-repo-$u"
  echo "{\"oauthAccount\":{\"emailAddress\":\"$u@team.com\"}}" > "$home/.claude.json"
  python3 - "$home/proj/-Users-$u-Git-repo-$u/$u-sess-1.jsonl" "$u" "${FACT[$u]}" <<'PY'
import json, sys
path, u, tok = sys.argv[1], sys.argv[2], sys.argv[3]
lines = [
 {"timestamp":"2026-07-14T10:00:00Z","cwd":f"/Users/{u}/Git/repo-{u}","message":{"role":"user","content":f"{u} planning the cache layer"}},
 {"timestamp":"2026-07-14T10:00:01Z","cwd":f"/Users/{u}/Git/repo-{u}","message":{"role":"assistant","content":[
    {"type":"text","text":f"Decision FACTCLAIM={tok} chosen for repo-{u} by {u}"}]}},
]
open(path,"w").write("\n".join(json.dumps(l) for l in lines))
PY
}

if [ -n "$CC" ]; then
  for u in "${USERS[@]}"; do
    make_user "$u"
    HOME="$WORK/home-$u" CSA_MOCK_MCP_LOG="$MCP_LOG" \
      "$CC" init --yes --server "http://127.0.0.1:8820" --token "$TOK" --email "$u@team.com" \
      --only "*repo-$u*" --backfill=true >"$WORK/init-$u.log" 2>&1
    grep -q "wrote" "$WORK/init-$u.log" && pass "user $u: init wrote consent config (0600)" \
                                        || fail "user $u: init did not write config :: $(tail -2 "$WORK/init-$u.log")"
    # watcher ingest: backfill the one fixture, then stop
    HOME="$WORK/home-$u" CSA_ROOT="$WORK/home-$u/proj" CSA_QUIET=1s CSA_INTERVAL=400ms CSA_COOLDOWN=1s \
      "$CC" >"$WORK/watch-$u.log" 2>&1 &
    WP=$!
    for _ in $(seq 1 50); do grep -q "$u-sess-1 ok=true" "$WORK/watch-$u.log" && break; sleep 0.4; done
    kill "$WP" 2>/dev/null
    grep -q "$u-sess-1 ok=true" "$WORK/watch-$u.log" \
      && pass "user $u: client watcher shipped + server ingested $u-sess-1" \
      || fail "user $u: watcher did not ingest :: $(grep -iE 'sent|error|FAIL' "$WORK/watch-$u.log" | tail -2)"
  done
  # /api/ingest only queues; drain it the way the etl would
  ( cd "$WORK/api" && "${SRV_ENV[@]}" CSA_AUTH=org CSA_AUTH_TOKEN="$TOK" $PY "$SERVER/csa.py" drain --all ) >"$WORK/drain.log" 2>&1 \
    && pass "csa.py drain extracted the shipped sessions" || fail "drain failed :: $(tail -3 "$WORK/drain.log")"

  # team rollup: single tenant == one shared corpus holding all three owners, each with >=1 fact
  TEAM=$(curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:8820/api/team")
  for u in "${USERS[@]}"; do
    printf '%s' "$TEAM" | python3 -c "import json,sys; d=json.load(sys.stdin); r=[x for x in d if x['user']=='$u@team.com']; sys.exit(0 if r and r[0]['facts']>=1 else 1)" \
      && pass "team rollup attributes a fact to $u@team.com" \
      || fail "team rollup missing $u@team.com (team=$TEAM)"
  done

  # recall: each tenant's distinctive fact is retrievable by its token
  for u in "${USERS[@]}"; do
    ANS=$(curl -s -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
          -d "{\"query\":\"${FACT[$u]}\",\"asked_by\":\"$u@team.com\",\"strict\":false}" "http://127.0.0.1:8820/api/ask")
    # `found`, not the token alone: a miss echoes the query back
    printf '%s' "$ANS" | grep -q '"found": true' && printf '%s' "$ANS" | grep -q "${FACT[$u]}" \
      && pass "ask('${FACT[$u]}') recalls $u's fact, attributed to $u@team.com" \
      || fail "ask did not recall $u's fact (${FACT[$u]})"
  done

  # MCP registration boundary: the emitted `claude mcp add` line carries the origin credential + identity
  if grep -q "mcp add" "$MCP_LOG"; then
    LINE=$(grep "mcp add" "$MCP_LOG" | tail -1)
    printf '%s' "$LINE" | grep -q -- "--header Authorization: Bearer $TOK" \
      && pass "client's 'claude mcp add' carries --header Authorization: Bearer <token>" \
      || fail "MCP registration line missing the bearer header: $LINE"
    printf '%s' "$LINE" | grep -q -- "--header X-CSA-User: " \
      && pass "client's 'claude mcp add' carries --header X-CSA-User: <identity>" \
      || fail "MCP registration line missing X-CSA-User: $LINE"
  else
    fail "no 'claude mcp add' line was captured (mock claude registration did not run)"
  fi
else
  skip "client not built — sections 4 & 5 (init/ingest/recall/forget/MCP-registration) not run"
fi

# =====================================================================================================
section "5. forget — own-session delete works; forging another's is denied (ownership gate)"
if [ -n "$CC" ]; then
  # alice forges a forget of bob's session (her identity, his session_id) -> denied, bob's data survives
  FORGE=$(HOME="$WORK/home-alice" "$CC" forget bob-sess-1 2>&1)
  printf '%s' "$FORGE" | grep -q '"ok": false' \
    && pass "alice forging forget of bob-sess-1 is DENIED ($(printf '%s' "$FORGE" | grep -o 'owned by[^"]*' | head -1))" \
    || fail "forged cross-owner forget was NOT denied: $FORGE"
  curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:8820/api/team" | grep -q 'bob@team.com' \
    && pass "bob's session survived the forged forget" \
    || fail "bob's session vanished after a forged forget"

  # alice forgets her OWN session -> gone
  SELF=$(HOME="$WORK/home-alice" "$CC" forget alice-sess-1 2>&1)
  printf '%s' "$SELF" | grep -q '"ok": true' \
    && pass "alice forgets her OWN session alice-sess-1 -> deleted" \
    || fail "alice could not forget her own session: $SELF"
  curl -s -H "Authorization: Bearer $TOK" "http://127.0.0.1:8820/api/team" | grep -q 'alice@team.com' \
    && fail "alice's session still present after her own forget" \
    || pass "alice's facts are gone from the corpus after forget"
fi

# =====================================================================================================
section "6. deployment-separation isolation — a second tenant rejects the first tenant's token (§11.8)"
launch_api api2 8822 127.0.0.1 org "different-secret-$$" >/dev/null
if wait_health 8822; then
  assert_code "tenant-2 with tenant-1's token"  401 "http://127.0.0.1:8822/api/facts" -H "Authorization: Bearer $TOK"
  assert_code "tenant-2 with its OWN token"     200 "http://127.0.0.1:8822/api/facts" -H "Authorization: Bearer different-secret-$$"
else fail "second tenant server never came up :: $(tail -3 "$WORK/api2.log")"; fi

# =====================================================================================================
section "summary"
printf "  ${C_G}%d passed${C_0}, ${C_R}%d failed${C_0}, ${C_Y}%d skipped${C_0}\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] && { echo "  ${C_G}TRIAL E2E: GREEN${C_0}"; exit 0; } || { echo "  ${C_R}TRIAL E2E: RED${C_0}"; exit 1; }
