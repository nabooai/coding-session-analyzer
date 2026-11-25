#!/usr/bin/env bash
#
# The Claude Code plugin against the real server, with no compiled binary on the path it tests: the
# device flow approved over HTTP, ship and sweep with planted secrets, retention, both clients, manual
# mode, the scripted recipe, disconnect. Every client request goes through a recording proxy, so each
# "never left the machine" below reads the wire. `claude` is a mock: real Claude Code is a per-release
# run, see csa-plugin/README.md.
#
#   server/e2e/run.sh plugin
set -uo pipefail

unset CSA_SERVER CSA_AUTH_TOKEN CSA_ROOT CLAUDE_CONFIG_DIR CLAUDE_CODE_SESSION_ID CLAUDE_PLUGIN_ROOT SSL_CERT_FILE

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"
PLUGIN="$ROOT/csa-plugin"
CSA="$PLUGIN/scripts/csa.py"

C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }
check(){ if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (want '$2', got '$3')"; fi; }
contains(){ if grep -qF -- "$3" <<<"$2"; then pass "$1"; else fail "$1 (missing '$3')"; fi; }
absent(){ if grep -qF -- "$3" <<<"$2"; then fail "$1 (unexpected '$3')"; else pass "$1"; fi; }

WORK="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/csa-plugin-e2e.XXXXXX")" && pwd)"
PIDS=()
cleanup(){ for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

PY=""
if [ -x "$SERVER/.venv/bin/python" ]; then PY="$SERVER/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then PY="uv run --project $SERVER python"; fi
if [ -z "$PY" ] || ! $PY -c "import mcp, cryptography, uvicorn" >/dev/null 2>&1; then
  echo "cannot find a Python with the server deps. Run 'uv sync' in $SERVER first." >&2; exit 3
fi

REAL_CLAUDE="$(command -v claude || true)"
SYSTEM_PY="$(command -v python3)"
MOCKBIN="$WORK/bin"; mkdir -p "$MOCKBIN"
cat > "$MOCKBIN/claude" <<'MOCK'
#!/usr/bin/env bash
if [ "${1:-}" = "mcp" ]; then echo "claude $*" >> "$HOME/mcp.log"; exit 0; fi
if [ "${1:-} ${2:-}" = "plugin list" ]; then [ -f "$HOME/plugin-list.json" ] && cat "$HOME/plugin-list.json" || echo '[]'; exit 0; fi
if [ "${1:-}" = "-p" ]; then
  input="$(cat)"
  claim="$(printf '%s' "$input" | grep -oE 'FACTCLAIM=[A-Za-z0-9 ._-]+' | head -1 | sed 's/^FACTCLAIM=//')"
  [ -z "$claim" ] && claim="a decision was recorded"
  python3 - "$claim" <<'PY'
import json, sys
c = sys.argv[1]
inner = json.dumps({"summary": c, "worked_on": [c],
    "hidden_context": [{"type": "decision", "scope": "organizational", "confidence": "high", "claim": c, "evidence": c}],
    "entities": [], "relations": [], "actions": [], "links": [], "data_sources": [], "deployed_to_prod": False})
print(json.dumps({"result": inner, "total_cost_usd": 0.0, "usage": {}}))
PY
fi
exit 0
MOCK
printf '#!/bin/sh\necho "$1" >> "$HOME/browser.log"\n' > "$MOCKBIN/open"
cp "$MOCKBIN/open" "$MOCKBIN/xdg-open"
chmod +x "$MOCKBIN"/*
export PATH="$MOCKBIN:$PATH"

ADMIN_TOKEN="plugin-e2e-shared-secret"
PORT=8860; PROXY_PORT=8861
BASE="http://127.0.0.1:$PORT"; WIRE="http://127.0.0.1:$PROXY_PORT"
BOX="$WORK/api"; mkdir -p "$BOX"
srv(){ (cd "$BOX" && env PYTHONPATH="$SERVER" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN_TOKEN" "$@"); }
( cd "$BOX" && env PYTHONPATH="$SERVER" CSA_AUTH=org CSA_AUTH_TOKEN="$ADMIN_TOKEN" CSA_BIND=127.0.0.1 CSA_API_PORT="$PORT" \
    $PY "$SERVER/apiserver.py" ) >"$WORK/api.log" 2>&1 &
PIDS+=($!)
LEDGER="$WORK/ledger.jsonl"; : > "$LEDGER"
"$SYSTEM_PY" "$HERE/_ledger_proxy.py" "$PROXY_PORT" "$BASE" "$LEDGER" >"$WORK/proxy.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 80); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$WIRE/api/health")" = "200" ] && break; sleep 0.25; done
check "server up behind the recording proxy" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$WIRE/api/health")"
srv $PY "$SERVER/csa.py" org config default --set archive_days=30 >/dev/null 2>&1
: > "$LEDGER"

new_home(){ local h="$WORK/homes/$1"; mkdir -p "$h/.claude/projects/-work-repo"; echo "$h"; }
csa(){ local h="$1"; shift; env HOME="$h" python3 -I -S -B "$CSA" "$@"; }
hook(){ local h="$1" cmd="$2" ev="$3"; printf '%s' "$ev" | env HOME="$h" python3 -I -S -B "$CSA" "$cmd"; }
ev_end(){ printf '{"hook_event_name":"SessionEnd","session_id":"%s","transcript_path":"%s","cwd":"/work/repo","reason":"exit"}' "$1" "$2"; }
ev_start(){ printf '{"hook_event_name":"SessionStart","session_id":"%s","transcript_path":"","cwd":"/work/repo","source":"startup"}' "$1"; }
ev_prompt(){ printf '{"hook_event_name":"UserPromptSubmit","session_id":"%s","prompt":"%s","cwd":"/work/repo","permission_mode":"default"}' "$1" "$2"; }
wire_count(){ "$SYSTEM_PY" - "$LEDGER" "$1" <<'PY'
import json, sys
print(sum(1 for line in open(sys.argv[1]) if json.loads(line)["path"] == sys.argv[2]))
PY
}
wire_total(){ wc -l < "$LEDGER" | tr -d ' '; }
cfg_get(){ "$SYSTEM_PY" -c "import json,sys;c=json.load(open(sys.argv[1]+'/.csa/config.json'))
for k in sys.argv[2].split('.'): c=c.get(k) if isinstance(c,dict) else None
print(c)" "$1" "$2"; }
perm(){ stat -c '%a' "$1" 2>/dev/null || stat -f '%OLp' "$1"; }
wait_marker(){ for _ in $(seq 1 60); do [ -f "$1/.csa/shipped/$2" ] && return 0; sleep 0.25; done; return 1; }
# A hook's child waits out its settle before it reads anything, so "nothing was sent" means nothing only after this.
quiet(){ sleep 0.3; for _ in $(seq 1 120); do pgrep -f "$CSA _(ship|sweep)" >/dev/null || return 0; sleep 0.25; done; fail "a child never finished"; }
drain(){ srv $PY "$SERVER/csa.py" drain --all >>"$WORK/drain.log" 2>&1; }
session(){ local h="$1" sid="$2" text="$3"; printf '{"type":"user","sessionId":"%s","cwd":"/work/repo","message":{"role":"user","content":"%s"}}\n' "$sid" "$text" > "$h/.claude/projects/-work-repo/$sid.jsonl"; echo "$h/.claude/projects/-work-repo/$sid.jsonl"; }

# connect HOME EMAIL [--manual]: the person's side of the device flow, over HTTP as the page's script does it
connect(){
  local h="$1" email="$2"; shift 2
  local out nonce code reply days
  out="$(csa "$h" connect "$WIRE" "$@" </dev/null 2>&1)"
  CONNECT_OUT="$out"
  code="$(grep -oE 'Verification code: [A-Z-]+' <<<"$out" | sed 's/.*: //')"
  nonce="$("$SYSTEM_PY" -c "import json,sys;print(json.load(open(sys.argv[1]+'/.csa/connect.pending'))['nonce'])" "$h")"
  reply="$(curl -sS -X POST -H 'Content-Type: application/json' -d "{\"nonce\":\"$nonce\",\"code\":\"$code\",\"email\":\"$email\",\"invite\":\"$ADMIN_TOKEN\",\"ack_days\":null}" "$BASE/api/cli-auth/approve")"
  days="$("$SYSTEM_PY" -c "import json,sys;d=json.loads(sys.argv[1]);print(d.get('archive_days','none') if d.get('needs_ack') else 'none')" "$reply")"
  ACK_REPLY="$reply"
  [ "$days" != "none" ] && curl -sS -X POST -H 'Content-Type: application/json' -d "{\"nonce\":\"$nonce\",\"code\":\"$code\",\"email\":\"$email\",\"invite\":\"$ADMIN_TOKEN\",\"ack_days\":$days}" "$BASE/api/cli-auth/approve" >/dev/null
  for _ in $(seq 1 60); do [ -f "$h/.csa/config.json" ] && [ ! -f "$h/.csa/connect.pending" ] && return 0; sleep 0.25; done
  return 1
}

# =================================================================================================
section "packaging"
check "the redactor is a byte copy of the server's" "same" "$(cmp -s "$PLUGIN/scripts/redact.py" "$SERVER/analyzer/redact.py" && echo same)"
check "nothing in the plugin is compiled" "" "$(find "$PLUGIN" -type f ! -name '*.py' ! -name '*.json' ! -name '*.md' ! -path '*/__pycache__/*' | head -1)"
if [ -n "$REAL_CLAUDE" ]; then
  if env HOME="$WORK/validate-home" CLAUDE_CONFIG_DIR="$WORK/validate-config" "$REAL_CLAUDE" plugin validate "$ROOT" >"$WORK/validate.log" 2>&1; then pass "claude plugin validate"; else fail "claude plugin validate"; cat "$WORK/validate.log"; fi
else skip "claude plugin validate: no real claude on PATH (CI runs it in its own step)"; fi

# =================================================================================================
section "before connecting: inert"
H="$(new_home inert)"
SHIMBIN="$WORK/pyshim"; mkdir -p "$SHIMBIN"; printf '#!/bin/sh\necho "$*" >> "%s/python-calls.log"\nexit 0\n' "$WORK" > "$SHIMBIN/python3"; chmod +x "$SHIMBIN/python3"
for ev in UserPromptSubmit SessionStart SessionEnd; do
  CMD="$("$SYSTEM_PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['hooks'][sys.argv[2]][0]['hooks'][0]['command'])" "$PLUGIN/hooks/hooks.json" "$ev")"
  env HOME="$H" CLAUDE_PLUGIN_ROOT="$PLUGIN" PATH="$SHIMBIN:/usr/bin:/bin" sh -c "$CMD" </dev/null >"$WORK/hook.out" 2>&1
  check "$ev hook exits 0 with no output and no config" "0:" "$?:$(cat "$WORK/hook.out")"
done
[ -f "$WORK/python-calls.log" ] && fail "python3 started before connect" || pass "python3 never started before connect"
check "nothing touched the network" "0" "$(wire_total)"

P="$(session "$H" preview-1 "before connect sk-fake1234567890abcdefghijklmnop")"
PREVIEW="$(csa "$H" preview --session preview-1 2>&1)"
contains "preview works before connecting" "$PREVIEW" "openai_key: 1"
absent "preview prints no secret" "$PREVIEW" "sk-fake1234567890"
check "preview wrote no config" "no" "$([ -f "$H/.csa/config.json" ] && echo yes || echo no)"
check "preview touched no network" "0" "$(wire_total)"

# =================================================================================================
section "consent: decline, then Authorize"
H="$(new_home decline)"
session "$H" old-one "FACTCLAIM=never consented" >/dev/null
OUT="$(csa "$H" connect "$WIRE" </dev/null 2>&1)"
contains "connect prints the notice" "$OUT" "Nothing is sent until you type the code and click Authorize"
contains "connect prints the code" "$OUT" "Verification code:"
absent "the nonce is not on stdout, which the model reads" "$OUT" "nonce"
contains "the browser opened on /cli-auth" "$(cat "$H/browser.log")" "/cli-auth?nonce="
[ -f "$H/.csa/config.json" ] && fail "an unapproved connect wrote a config" || pass "closing the tab leaves no config"
contains "the request names the mode" "$(tail -1 "$LEDGER")" 'plugin'
hook "$H" ship "$(ev_end old-one "$H/.claude/projects/-work-repo/old-one.jsonl")" >/dev/null 2>&1
check "nothing ships from an unapproved connect" "0" "$(wire_count /api/ingest)"
NONCE="$("$SYSTEM_PY" -c "import json,sys;print(json.load(open(sys.argv[1]+'/.csa/connect.pending'))['nonce'])" "$H")"
PAGE="$(curl -sS "$BASE/cli-auth?nonce=$NONCE&hostname=box")"
contains "the page states the consent" "$PAGE" "Authorizing is your consent"
contains "the page states the retention" "$PAGE" "for 30 days"
contains "the page names the plugin's undo" "$PAGE" "/csa:disconnect"
absent "the page does not show the code" "$PAGE" "$(grep -oE 'Verification code: [A-Z-]+' <<<"$OUT" | sed 's/.*: //')"
pkill -f "$CSA _poll" 2>/dev/null

H="$(new_home auto)"
OLD="$(session "$H" before-consent "FACTCLAIM=history nobody consented to")"
touch -t 202001010101 "$OLD"
connect "$H" sam@acme.test && pass "the poll claims the credential after the click" || fail "no config after Authorize"
check "config is 0600" "600" "$(perm "$H/.csa/config.json")"
check "consent policy" "2" "$(cfg_get "$H" consent.policy_version)"
check "consent method" "browser" "$(cfg_get "$H" consent.method)"
check "the retention agreed to" "30" "$(cfg_get "$H" consent.archive_days_at_consent)"
check "shipping" "plugin" "$(cfg_get "$H" shipping)"
check "identity is the verified email" "sam@acme.test" "$(cfg_get "$H" identity)"
contains "the MCP server is registered" "$(cat "$H/mcp.log")" "mcp add --transport http csa"
FIRST="$(hook "$H" recall "$(ev_prompt s1 ok)")"
contains "the next prompt says connected" "$FIRST" "CSA connected as sam@acme.test"
check "and only once" "" "$(hook "$H" recall "$(ev_prompt s1 ok)")"
TOKEN="$(cfg_get "$H" auth_token)"

# =================================================================================================
section "ship, preview, recall"
PLANTS="$("$SYSTEM_PY" - "$ROOT/docs/redaction_samples.json" "$WORK/planted.jsonl" <<'PY'
import json, sys
cases = json.load(open(sys.argv[1]))["cases"]
secret = [c for c in cases if c.get("mode", "secrets") == "secrets" and not c.get("allow") and not c.get("deny") and c.get("must_redact")]
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "user", "sessionId": "ship-1", "cwd": "/work/repo",
                        "message": {"role": "user", "content": "FACTCLAIM=we chose sqlite for the corpus"}}) + "\n")
    for c in secret:
        f.write(c["input"] + "\n")                # a fixture is already raw transcript text
        if '"' not in c["input"] and "\\" not in c["input"]:   # and wrapped as Claude Code writes it, on a new line
            f.write(json.dumps({"message": {"role": "user", "content": "see\n" + c["input"]}}, ensure_ascii=False) + "\n")
print(json.dumps(sorted({s for c in secret for s in c["must_redact"]})))
PY
)"
T="$H/.claude/projects/-work-repo/ship-1.jsonl"; cp "$WORK/planted.jsonl" "$T"
SHA_PREVIEW="$(csa "$H" preview --session ship-1 --out "$WORK/preview.txt" | awk '/^sha256/{print $2}')"
T0="$(wire_total)"
check "preview sent nothing" "$T0" "$(wire_total)"
check "ship prints nothing" "" "$(hook "$H" ship "$(ev_end ship-1 "$T")" 2>&1)"
wait_marker "$H" ship-1 && pass "the detached child ships" || fail "no marker after ship"
quiet "$H"
contains "ship.log says so" "$(cat "$H/.csa/ship.log")" "sent ship-1 ok=true"
RESULT="$("$SYSTEM_PY" - "$LEDGER" "$H/.csa/sent.jsonl" "$WORK/preview.txt" "$PLANTS" <<'PY'
import hashlib, json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
ingest = [r for r in rows if r["path"] == "/api/ingest"]
body = json.loads(ingest[-1]["body"])
print("ua", ingest[-1]["ua"].split("/")[0])
print("sha", hashlib.sha256(body["content"].encode()).hexdigest())
print("ledger", json.loads(open(sys.argv[2]).read().splitlines()[-1])["sha256"])
print("same_as_preview", open(sys.argv[3], encoding="utf-8").read() == body["content"])
print("keys", ",".join(sorted(body)))
print("leaks", [s for s in json.loads(sys.argv[4]) if s in ingest[-1]["body"]])
PY
)"
contains "requests carry the plugin's user agent" "$RESULT" "ua csa-plugin"
SHA_WIRE="$(awk '/^sha /{print $2}' <<<"$RESULT")"
check "preview sha equals the wire's" "$SHA_WIRE" "$SHA_PREVIEW"
check "sent.jsonl sha equals the wire's" "$SHA_WIRE" "$(awk '/^ledger /{print $2}' <<<"$RESULT")"
contains "the preview file is the body" "$RESULT" "same_as_preview True"
contains "the body has only what the docs say" "$RESULT" "keys content,redaction_mode,session_id"
contains "no planted secret on the wire" "$RESULT" "leaks []"
check "sent.jsonl is 0600" "600" "$(perm "$H/.csa/sent.jsonl")"
drain
FACTS="$(curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/facts?q=sqlite")"
contains "the session reached the corpus" "$FACTS" "sqlite"
OWNER="$(srv $PY -c "
from analyzer import store
print(store.sqlite_db().execute(\"SELECT user FROM sessions WHERE session_id='ship-1'\").fetchone()[0])" 2>/dev/null | tail -1)"
check "a server row with the verified email" "sam@acme.test" "$OWNER"
check "a second SessionEnd does not ship it again" "" "$(hook "$H" ship "$(ev_end ship-1 "$T")" 2>&1)"
quiet "$H"; check "one POST for the session" "1" "$(wire_count /api/ingest)"

: > "$LEDGER"
REF="$(curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/facts?q=sqlite" | "$SYSTEM_PY" -c "
import json,sys
d = json.load(sys.stdin)
rows = d if isinstance(d, list) else d.get('facts', d.get('rows', []))
print(rows[0].get('ref') or rows[0].get('id') or '')")"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "{\"ref\":\"$REF\",\"label\":\"gold\"}" "$BASE/api/flag" >/dev/null
RECALL="$(hook "$H" recall "$(ev_prompt s2 'what did we decide about sqlite for the corpus')")"
check "recall POSTs /api/ask" "1" "$(wire_count /api/ask)"
if [ -n "$RECALL" ]; then
  check "recall output is valid JSON" "ok" "$("$SYSTEM_PY" -c "import json,sys;json.loads(sys.argv[1]);print('ok')" "$RECALL")"
  contains "recall injects the fact" "$RECALL" "sqlite"
else fail "recall returned nothing for a seeded fact"; fi
: > "$LEDGER"
hook "$H" recall "$(ev_prompt s2 'why does the deploy need sk-fake1234567890abcdefghijklmnop and ghp_fakefakefakefakefake on staging')" >/dev/null
absent "a planted key in a prompt never reaches /api/ask" "$(cat "$LEDGER")" "sk-fake1234567890"
absent "nor a github token" "$(cat "$LEDGER")" "ghp_fakefake"
: > "$LEDGER"
hook "$H" recall "$(ev_prompt s2 'the password is hunter2hunter2 for the staging box')" >/dev/null
check "a residual secret stays home" "0" "$(wire_total)"

# =================================================================================================
section "sweep"
: > "$LEDGER"
CRASH="$(session "$H" crashed "FACTCLAIM=the crashed session still shipped")"
CURRENT="$(session "$H" starting-now "FACTCLAIM=starting right now")"
mkdir -p "$H/.claude/projects/-work-repo/crashed/subagents"
printf '{"type":"user","message":{"role":"user","content":"sub"}}\n' > "$H/.claude/projects/-work-repo/crashed/subagents/agent-a.jsonl"
check "sweep prints nothing" "" "$(hook "$H" sweep "$(ev_start starting-now)" 2>&1)"
wait_marker "$H" crashed && pass "the sweep ships a crashed session" || fail "crashed session did not ship"
quiet "$H"
[ -f "$H/.csa/shipped/before-consent" ] && fail "older history shipped" || pass "older transcripts never ship"
[ -f "$H/.csa/shipped/starting-now" ] && fail "the current session was swept" || pass "the current session waits for its own SessionEnd"
[ -f "$H/.csa/shipped/agent-a" ] && fail "a subagent file was swept" || pass "subagent files stay home"
absent "history never crossed the wire" "$(cat "$LEDGER")" "history nobody consented to"
check "the sweep polled the retention" "1" "$(wire_count /api/client-config)"

# =================================================================================================
section "retention raised, then lowered"
srv $PY "$SERVER/csa.py" org config default --set archive_days=forever >/dev/null 2>&1
: > "$LEDGER"
PAUSED="$(session "$H" while-paused "FACTCLAIM=shipped after the pause")"
hook "$H" sweep "$(ev_start other)" >/dev/null 2>&1
quiet "$H"
check "the sweep learned the new window" "-1" "$(cfg_get "$H" server_archive_days)"
check "nothing ships past what was agreed" "0" "$(wire_count /api/ingest)"
contains "the next prompt says why" "$(hook "$H" recall "$(ev_prompt s3 ok)")" "CSA paused"
hook "$H" ship "$(ev_end while-paused "$PAUSED")" >/dev/null 2>&1; quiet "$H"
check "SessionEnd honours the cached window" "0" "$(wire_count /api/ingest)"
srv $PY "$SERVER/csa.py" org config default --set archive_days=30 >/dev/null 2>&1
hook "$H" sweep "$(ev_start other)" >/dev/null 2>&1
wait_marker "$H" while-paused && pass "lowering it un-pauses on its own" || fail "still paused after lowering"
quiet "$H"

# =================================================================================================
section "policy voided"
"$SYSTEM_PY" -c "import json,sys;p=sys.argv[1]+'/.csa/config.json';c=json.load(open(p));c['consent']['policy_version']=1;json.dump(c,open(p,'w'))" "$H"
: > "$LEDGER"
session "$H" after-void "FACTCLAIM=never" >/dev/null
contains "the next prompt says why" "$(hook "$H" recall "$(ev_prompt s4 ok)")" "older policy"
hook "$H" sweep "$(ev_start other)" >/dev/null 2>&1
hook "$H" ship "$(ev_end after-void "$H/.claude/projects/-work-repo/after-void.jsonl")" >/dev/null 2>&1
quiet "$H"; check "nothing at all crossed the wire" "0" "$(wire_total)"

# =================================================================================================
section "disconnect"
H="$(new_home leave)"
connect "$H" leaver@acme.test || fail "connect"
LEAVE_TOKEN="$(cfg_get "$H" auth_token)"
check "the token works before" "200" "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $LEAVE_TOKEN" "$BASE/api/facts")"
T="$(session "$H" leave-1 "FACTCLAIM=sent before leaving")"
hook "$H" ship "$(ev_end leave-1 "$T")" >/dev/null 2>&1; wait_marker "$H" leave-1; quiet "$H"
OUT="$(csa "$H" disconnect 2>&1)"
contains "disconnect says it revoked" "$OUT" "Credential revoked"
contains "disconnect names what stays on the server" "$OUT" "leave-1"
contains "disconnect prints the rest" "$OUT" "claude plugin uninstall csa@csa"
check "the token is dead" "401" "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $LEAVE_TOKEN" "$BASE/api/facts")"
contains "the MCP server is removed" "$(cat "$H/mcp.log")" "mcp remove csa -s user"
[ -e "$H/.csa" ] && fail "~/.csa survived disconnect" || pass "no ~/.csa"
: > "$LEDGER"
T="$(session "$H" after-leave "FACTCLAIM=after leaving")"
hook "$H" ship "$(ev_end after-leave "$T")" >/dev/null 2>&1; quiet "$H"
check "a new session ships nothing" "0" "$(wire_total)"

# =================================================================================================
section "manual mode"
H="$(new_home manual)"
connect "$H" manual@acme.test --manual || fail "connect --manual"
contains "the notice says only what you send" "$CONNECT_OUT" "one session at a time"
check "shipping" "plugin-manual" "$(cfg_get "$H" shipping)"
[ -f "$H/mcp.log" ] && fail "manual registered an MCP server" || pass "no MCP registration"
hook "$H" recall "$(ev_prompt m1 ok)" >/dev/null
: > "$LEDGER"
for s in m1 m2 m3; do
  T="$(session "$H" "$s" "FACTCLAIM=manual session $s")"
  hook "$H" sweep "$(ev_start "$s")" >/dev/null 2>&1
  check "recall stays home in $s" "" "$(hook "$H" recall "$(ev_prompt "$s" 'what did we decide about the release rollback plan')")"
  hook "$H" ship "$(ev_end "$s" "$T")" >/dev/null 2>&1
done
quiet "$H"
check "three sessions: zero requests" "0" "$(wire_total)"
check "no child shipped or swept" "0" "$(grep -cE 'sent |ship:' "$H/.csa/ship.log" 2>/dev/null || true)"
OUT="$(env HOME="$H" CLAUDE_CODE_SESSION_ID=m2 python3 -I -S -B "$CSA" send-session 2>&1)"
contains "send-session prints the server" "$OUT" "$WIRE"
contains "send-session says how to take it back" "$OUT" "/csa:forget m2"
check "exactly one POST" "1" "$(wire_count /api/ingest)"
check "and one retention check" "2" "$(wire_total)"
absent "the other sessions stayed home" "$(cat "$LEDGER")" "manual session m1"
contains "the one you sent left" "$(cat "$LEDGER")" "manual session m2"
drain
ROWS="$(srv $PY -c "
from analyzer import store
print(store.sqlite_db().execute(\"SELECT count(*) FROM sessions WHERE user='manual@acme.test'\").fetchone()[0])" 2>/dev/null | tail -1)"
check "exactly one server row" "1" "$ROWS"
OUT="$(csa "$H" forget m2 2>&1)"
contains "forget takes it back" "$OUT" "m2 deleted"
ROWS="$(srv $PY -c "
from analyzer import store
print(store.sqlite_db().execute(\"SELECT count(*) FROM sessions WHERE user='manual@acme.test'\").fetchone()[0])" 2>/dev/null | tail -1)"
check "the row is gone" "0" "$ROWS"

# =================================================================================================
section "both clients"
CLIENT_BIN="$WORK/csa-client"
if (cd "$ROOT/client" && CGO_ENABLED=0 go build -trimpath -o "$CLIENT_BIN" . 2>"$WORK/build.log"); then pass "csa-client builds"
else fail "csa-client build"; cat "$WORK/build.log"; fi
go_setup(){ # HOME EMAIL: setup --yes through the device flow
  local h="$1" email="$2" log="$WORK/setup-$$.log" nonce="" code=""
  ( env HOME="$h" CSA_SETUP_NO_BROWSER=1 "$CLIENT_BIN" setup --server "$WIRE" --yes >"$log" 2>&1 ) &
  local pid=$!
  for _ in $(seq 1 60); do
    nonce="$(grep -oE 'nonce=[A-Za-z0-9_-]+' "$log" 2>/dev/null | head -1 | cut -d= -f2)"
    code="$(grep -oE 'Verification code: [^ ]*[A-Z]+-[A-Z]+' "$log" 2>/dev/null | head -1 | grep -oE '[A-Z]+-[A-Z]+')"
    [ -n "$nonce" ] && [ -n "$code" ] && break; sleep 0.25
  done
  curl -sS -X POST -H 'Content-Type: application/json' -d "{\"nonce\":\"$nonce\",\"code\":\"$code\",\"email\":\"$email\",\"invite\":\"$ADMIN_TOKEN\"}" "$BASE/api/cli-auth/approve" >/dev/null
  wait $pid
}

# Go first, then the plugin
H="$(new_home go-first)"
go_setup "$H" gofirst@acme.test; check "csa-client setup completes" "0" "$?"
check "csa-client's config has no shipping field" "None" "$(cfg_get "$H" shipping)"
OUT="$(csa "$H" connect "$WIRE" </dev/null 2>&1)"; check "connect refuses" "1" "$?"
contains "the refusal names the fix" "$OUT" "csa-client uninstall"
: > "$LEDGER"
T="$(session "$H" both-1 "FACTCLAIM=both clients on one machine")"
WARN="$(hook "$H" recall "$(ev_prompt both-1 'what did we decide about the release rollback plan')")"
contains "the plugin's hook warns" "$WARN" "this plugin is paused"
contains "the warning names the switch" "$WARN" "csa-client uninstall"
hook "$H" ship "$(ev_end both-1 "$T")" >/dev/null 2>&1
hook "$H" sweep "$(ev_start both-1)" >/dev/null 2>&1
quiet "$H"; check "the plugin sent nothing" "0" "$(wire_total)"
printf '%s' "$(ev_prompt both-1 'what did we decide about the release rollback plan')" | env HOME="$H" "$CLIENT_BIN" hook >/dev/null 2>&1
printf '%s' "$(ev_end both-1 "$T")" | env HOME="$H" "$CLIENT_BIN" ship >/dev/null 2>&1
wait_marker "$H" both-1; quiet "$H"
check "exactly one POST" "1" "$(wire_count /api/ingest)"
check "exactly one /api/ask" "1" "$(wire_count /api/ask)"

# Stale consent: setup, uninstall with no purge, enable the plugin
GO_TOKEN="$(cfg_get "$H" auth_token)"
env HOME="$H" "$CLIENT_BIN" uninstall >"$WORK/uninstall.log" 2>&1
contains "csa-client uninstall says it revoked" "$(cat "$WORK/uninstall.log")" "credential revoked"
check "its token is dead" "401" "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $GO_TOKEN" "$BASE/api/facts")"
check "its config holds no dead token" "" "$("$SYSTEM_PY" -c "import json,sys;print(json.load(open(sys.argv[1]+'/.csa/config.json')).get('auth_token',''))" "$H")"
echo '*keepthisout*' > "$H/.csa/ignore"
: > "$LEDGER"
T="$(session "$H" stale-1 "FACTCLAIM=stale consent must not ship")"
contains "the next prompt says connect" "$(hook "$H" recall "$(ev_prompt stale-1 'what did we decide about the release rollback plan')")" "/csa:connect"
hook "$H" ship "$(ev_end stale-1 "$T")" >/dev/null 2>&1
hook "$H" sweep "$(ev_start other)" >/dev/null 2>&1
quiet "$H"; check "old consent ships nothing" "0" "$(wire_total)"
connect "$H" gofirst@acme.test && pass "migrating: connect works after uninstall" || fail "connect after uninstall"
check "migrating keeps the folder rules" "*keepthisout*" "$(cat "$H/.csa/ignore")"
check "the marker from csa-client still counts" "yes" "$([ -f "$H/.csa/shipped/both-1" ] && echo yes)"
hook "$H" recall "$(ev_prompt m ok)" >/dev/null
: > "$LEDGER"
hook "$H" sweep "$(ev_start other)" >/dev/null 2>&1; quiet "$H"
check "what csa-client shipped is not sent again" "0" "$(wire_count /api/ingest)"

# Plugin first, then Go
H="$(new_home plugin-first)"
connect "$H" pfirst@acme.test || fail "connect"
echo '[{"id":"csa@csa","enabled":true,"projectEnabled":false}]' > "$H/plugin-list.json"
OUT="$(env HOME="$H" "$CLIENT_BIN" setup --server "$WIRE" --yes </dev/null 2>&1)"; check "setup refuses" "78" "$?"
contains "the refusal names the fix" "$OUT" "claude plugin disable csa@csa"
OUT="$(env HOME="$H" "$CLIENT_BIN" init --yes --server "$WIRE" --token x </dev/null 2>&1)"; check "init refuses" "78" "$?"
OUT="$(env HOME="$H" "$CLIENT_BIN" install </dev/null 2>&1)"; check "install refuses" "78" "$?"
check "the plugin's config is untouched" "plugin" "$(cfg_get "$H" shipping)"
echo '[{"id":"csa@csa","enabled":false,"projectEnabled":true}]' > "$H/plugin-list.json"
OUT="$(env HOME="$H" "$CLIENT_BIN" setup --server "$WIRE" --yes </dev/null 2>&1)"; check "project-scope enable refuses too" "78" "$?"
: > "$LEDGER"
T="$(session "$H" pf-1 "FACTCLAIM=plugin first")"
printf '%s' "$(ev_end pf-1 "$T")" | env HOME="$H" "$CLIENT_BIN" ship >/dev/null 2>&1
printf '%s' "$(ev_prompt pf-1 'what did we decide about the release rollback plan')" | env HOME="$H" "$CLIENT_BIN" hook >/dev/null 2>&1
quiet "$H"; check "a stray csa-client hook ships nothing on the plugin's config" "0" "$(wire_total)"
hook "$H" ship "$(ev_end pf-1 "$T")" >/dev/null 2>&1; wait_marker "$H" pf-1; quiet "$H"
check "exactly one POST" "1" "$(wire_count /api/ingest)"
DOC="$(env HOME="$H" "$CLIENT_BIN" doctor 2>&1)"
contains "doctor hands a plugin-owned machine to /csa:status" "$DOC" "/csa:status"
absent "doctor does not call it broken" "$DOC" "NOT SHIPPING"
OUT="$(env HOME="$H" "$CLIENT_BIN" uninstall 2>&1)"
absent "csa-client uninstall leaves the plugin's credential alone" "$OUT" "credential revoked"
check "the plugin's token still works" "200" "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cfg_get "$H" auth_token)" "$BASE/api/facts")"

# `only` globs: the same folders as Go, under a home path holding the word
H="$(new_home project-owner)"
connect "$H" globs@acme.test || fail "connect"
hook "$H" recall "$(ev_prompt g ok)" >/dev/null
echo 'proj*' > "$H/.csa/only"
mkdir -p "$H/.claude/projects/proj-a" "$H/.claude/projects/other"
printf '{"type":"user","message":{"role":"user","content":"a"}}\n' > "$H/.claude/projects/proj-a/in-scope.jsonl"
printf '{"type":"user","message":{"role":"user","content":"b"}}\n' > "$H/.claude/projects/other/out-of-scope.jsonl"
: > "$LEDGER"
hook "$H" sweep "$(ev_start other)" >/dev/null 2>&1; wait_marker "$H" in-scope; quiet "$H"
check "only 'proj*' ships the matching folder" "yes" "$([ -f "$H/.csa/shipped/in-scope" ] && echo yes)"
[ -f "$H/.csa/shipped/out-of-scope" ] && fail "only leaked through the home path" || pass "the home path does not widen only"

# =================================================================================================
section "the scripted recipe"
SVC="$(srv $PY -c "
from analyzer import orgs
print(orgs.mint_token('script@acme.test', 'default', 'ci', service=True))" 2>/dev/null | tail -1)"
F="$WORK/recipe.jsonl"
printf '{"type":"user","sessionId":"recipe-1","cwd":"/work/repo","message":{"role":"user","content":"FACTCLAIM=the recipe works sk-fake1234567890abcdefghijklmnop"}}\n' > "$F"
if command -v jq >/dev/null 2>&1; then
  H="$(new_home recipe)"
  env HOME="$H" python3 "$CSA" redact < "$F" > "$F.red" 2>/dev/null
  : > "$LEDGER"
  R="$(jq -n --rawfile c "$F.red" --arg id recipe-1 '{session_id:$id,content:$c,redaction_mode:"secrets"}' \
    | curl -sS --fail-with-body -X POST "$WIRE/api/ingest" -H "Authorization: Bearer $SVC" -H 'Content-Type: application/json' --data-binary @-)"
  contains "the redacting form is accepted" "$R" '"ok": true'
  absent "the redacting form sends no secret" "$(cat "$LEDGER")" "sk-fake1234567890"
  : > "$LEDGER"
  R="$(jq -n --rawfile c "$F" --arg id recipe-2 '{session_id:$id,content:$c,redaction_mode:"secrets"}' \
    | curl -sS --fail-with-body -X POST "$WIRE/api/ingest" -H "Authorization: Bearer $SVC" -H 'Content-Type: application/json' --data-binary @-)"
  contains "the raw form is accepted" "$R" '"ok": true'
  contains "the raw form really does send the secret" "$(cat "$LEDGER")" "sk-fake1234567890"
  drain
  STORED="$(srv $PY - <<'PY' 2>/dev/null
from analyzer import store
con = store.sqlite_db()
print(" ".join(str(r) for r in con.execute("SELECT session_id, summary FROM sessions WHERE session_id LIKE 'recipe-%'")))
PY
)"
  absent "the server's floor still strips it from what is stored" "$STORED" "sk-fake1234567890"
  head -c 33554433 /dev/zero | tr '\0' a > "$WORK/huge.txt"
  check "a body over 32 MiB is 413" "413" "$(jq -n --rawfile c "$WORK/huge.txt" --arg id huge '{session_id:$id,content:$c}' \
    | curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/ingest" -H "Authorization: Bearer $SVC" -H 'Content-Type: application/json' --data-binary @-)"
else skip "the scripted recipe: no jq"; fi

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
