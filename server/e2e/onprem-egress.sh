#!/usr/bin/env bash
#
# CSA on-prem egress E2E: the hardened server image reaches ONLY the operator's model endpoint.
#
# Builds one target of server/Dockerfile (the first argument: slim, the default, or with-claude-code), smoke-tests
# the image (server/image-smoke.sh), then starts it on a Docker network
# created with --internal (no route out) beside three sidecars, all on the same pinned python base image:
#   * sinkhole  a DNS server that logs every name the image asks for and answers NXDOMAIN for all but `stub`
#               (the image runs with --dns pointed at it: Docker's embedded resolver forwards every name it
#               does not own there, and no container on the network is called `stub`)
#   * stub      an Anthropic-format Messages endpoint that logs every request (path, Host, model id, stream,
#               tool and MCP counts) and answers a canned reply; with CSA_E2E_UPSTREAM set it forwards the
#               request unchanged to that upstream instead (a logging reverse proxy) and still logs the same
#   * sniff     shares the image's network namespace and logs every outbound TCP SYN and UDP datagram by
#               destination, so an IP-literal connection that never touched DNS is caught too
# Inside the image, as uid 10001, with ANTHROPIC_BASE_URL=http://stub:8080:
#   1. with-claude-code only: claude -p 'reply OK' --model <alias> --output-format json     (the bare CLI)
#   2. one extraction-shaped analyzer.llm.complete() call                (the isolation argv, or HTTPS on slim)
#   3. research/run.py --selfcheck, and the full golden-case suite when research/corpus/ holds a snapshot,
#      compared check by check with the same suite run on this host
#   4. slim only: a subscription token and a Bedrock switch, each refused with the fix and sending nothing
# and asserts: the sinkhole saw no name but `stub`, every SYN went to the stub, the stub saw the requests with
# no tools and no MCP servers on the isolated one, the FULL model ids sent are printed (gateway admins need them)
# and are the ones llm._API_MODELS names for slim, id -u is 10001, and whether `--model sonnet` needed
# downloads.claude.ai/model-catalog.
#
#   server/e2e/run.sh onprem-egress
#   server/e2e/run.sh onprem-egress with-claude-code
#
# Proxy mode (spends real money on your credential; the stub forwards to the upstream, the image still sees
# only the stub, the sinkhole and sniff still prove it):
#   CSA_E2E_UPSTREAM=https://api.anthropic.com ANTHROPIC_API_KEY=sk-ant-... server/e2e/run.sh onprem-egress
#   (or CLAUDE_CODE_OAUTH_TOKEN=... instead of the key, with-claude-code only)
# CSA_E2E_KEEP=1 keeps the workspace (logs, JSON results). Needs docker with buildx (ARG TARGETARCH); runs
# outside pytest.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"
RESEARCH="$ROOT/research"

C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0; SKIP=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; SKIP=$((SKIP+1)); }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }

TARGET="${1:-slim}"
case "$TARGET" in
  slim) BUILD_TARGET=() ;;
  with-claude-code) BUILD_TARGET=(--target with-claude-code) ;;
  *) echo "the first argument is slim or with-claude-code, not '$TARGET'" >&2; exit 2 ;;
esac
TOKEN=sk-ant-oat01-e2e-must-not-leave

P="csa-egress-$$"
NET="$P-net"
IMG="csa-server:$P"
UPSTREAM="${CSA_E2E_UPSTREAM:-}"
FORBIDDEN='downloads\.claude\.ai|sentry\.io|datadoghq\.com|storage\.googleapis\.com|registry\.npmjs\.org|api\.anthropic\.com|claude\.ai|platform\.claude\.com|api\.openai\.com|pypi\.org|files\.pythonhosted\.org|deb\.debian\.org|ghcr\.io|github\.com|accounts\.google\.com|googleapis\.com|letsencrypt\.org|^ocsp\.|statsig'

WORK="$(mktemp -d "${TMPDIR:-/tmp}/csa-onprem-e2e.XXXXXX")"
cleanup(){
  docker rm -f "$P-sniff" "$P-api" "$P-stub" "$P-sinkhole" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMG" >/dev/null 2>&1 || true
  if [ -n "${CSA_E2E_KEEP:-}" ]; then echo "kept workspace: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT
echo "workspace: $WORK"

docker info >/dev/null 2>&1 || { echo "${C_R}docker is not running${C_0}" >&2; exit 3; }
docker buildx version >/dev/null 2>&1 || { echo "${C_R}docker buildx is required (the Dockerfile reads ARG TARGETARCH)${C_0}" >&2; exit 3; }
BASE="$(grep -o 'python:3.12-slim@sha256:[0-9a-f]*' "$SERVER/Dockerfile" | head -1 || true)"
[ -n "$BASE" ] || { echo "cannot read the pinned base image from server/Dockerfile" >&2; exit 3; }

# --- sidecars -------------------------------------------------------------------------------------------
cat > "$WORK/sinkhole.py" <<'PY'
"""DNS sinkhole: log every question, answer A/AAAA for `stub`, NXDOMAIN for everything else."""
import socket, struct, sys
STUB_IP = sys.argv[1]
QTYPES = {1: "A", 28: "AAAA", 5: "CNAME", 12: "PTR", 33: "SRV", 16: "TXT", 65: "HTTPS"}

def qname(msg):
    i, labels = 12, []
    while msg[i]:
        n = msg[i]; labels.append(msg[i + 1:i + 1 + n].decode("ascii", "replace")); i += 1 + n
    qtype, qclass = struct.unpack("!HH", msg[i + 1:i + 5])
    return ".".join(labels).lower(), qtype, msg[12:i + 5]

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 53))
while True:
    msg, peer = s.recvfrom(4096)
    if len(msg) < 17:
        continue
    name, qtype, question = qname(msg)
    print(f"Q {QTYPES.get(qtype, qtype)} {name}", flush=True)
    if name == "stub":
        if qtype == 1:
            rr = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(STUB_IP)
            s.sendto(msg[:2] + b"\x81\x80" + struct.pack("!HHHH", 1, 1, 0, 0) + question + rr, peer)
        else:
            s.sendto(msg[:2] + b"\x81\x80" + struct.pack("!HHHH", 1, 0, 0, 0) + question, peer)
    else:
        s.sendto(msg[:2] + b"\x81\x83" + struct.pack("!HHHH", 1, 0, 0, 0) + question, peer)
PY

cat > "$WORK/stub.py" <<'PY'
"""Anthropic-format stub: logs every request, answers a canned Messages reply, or forwards to CSA_E2E_UPSTREAM."""
import http.client, json, os, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("CSA_E2E_UPSTREAM", "")
HOP = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "te", "trailer", "upgrade"}

def canned(model, stream):
    if not stream:
        return "application/json", json.dumps({
            "id": "msg_stub", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}}).encode()
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_stub", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "text/event-stream", "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_body(self, body):
        try:
            b = json.loads(body or b"{}")
        except ValueError:
            return "body=non-json", "?", False
        msgs = b.get("messages") or []
        text = json.dumps(msgs)
        tag = "extract" if "TRANSCRIPT:" in text else "cli"
        sysm = b.get("system") or ""
        sysm = sysm if isinstance(sysm, str) else json.dumps(sysm)
        info = (f"model={b.get('model')} stream={b.get('stream', False)} tools={len(b.get('tools') or [])} "
                f"mcp_servers={len(b.get('mcp_servers') or [])} msgs={len(msgs)} system_chars={len(sysm)} "
                f"max_tokens={b.get('max_tokens')} tag={tag}")
        return info, b.get("model"), bool(b.get("stream"))

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = self.path
        if "e2e-must-not-leave" in str(self.headers) + body.decode("utf-8", "replace") + path:
            print(f"LEAK {self.command} {path}", flush=True)
        route = urllib.parse.urlsplit(path).path
        host = self.headers.get("Host")
        if route == "/health":
            return self._reply(200, "text/plain", b"ok\n")
        if route == "/api/hello":                       # the CLI's preflight on the base URL (HEAD, no body)
            print(f"REQ {self.command} {path} host={host}", flush=True)
            return self._reply(200, "text/plain", b"")
        info, model, stream = self._log_body(body) if body else ("", "?", False)
        print(f"REQ {self.command} {path} host={host} {info}", flush=True)
        if UPSTREAM:
            return self._forward(path, body)
        if route == "/v1/messages" and self.command == "POST":
            ctype, out = canned(model, stream)
            return self._reply(200, ctype, out)
        if route == "/v1/messages/count_tokens" and self.command == "POST":
            return self._reply(200, "application/json", json.dumps({"input_tokens": 10}).encode())
        print(f"UNKNOWN {self.command} {path}", flush=True)
        self._reply(404, "application/json", json.dumps({"type": "error", "error": {
            "type": "not_found_error", "message": f"stub has no route for {self.command} {route}"}}).encode())

    def _forward(self, path, body):
        u = urllib.parse.urlsplit(UPSTREAM)
        conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Host"] = u.netloc
        conn.request(self.command, path, body=body, headers=headers)
        r = conn.getresponse()
        print(f"UPSTREAM {r.status} {u.netloc}{path}", flush=True)
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _handle

print(f"stub up, upstream={UPSTREAM or 'none (canned)'}", flush=True)
ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
PY

cat > "$WORK/sniff.py" <<'PY'
"""Log every outbound TCP SYN and UDP datagram from this network namespace by destination."""
import socket, struct, sys
ME = sys.argv[1]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
print("sniff up", flush=True)
while True:
    frame, _ = s.recvfrom(65535)
    if len(frame) < 34:
        continue
    eth = struct.unpack("!H", frame[12:14])[0]
    if eth == 0x86DD:
        dst = frame[38:54]
        if dst[:2] != b"\xff\x02":                     # link-local multicast (neighbour discovery, MLD) is the
            print(f"IPV6 {dst.hex()}", flush=True)     # interface coming up, not egress
        continue
    if eth != 0x0800:
        continue
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    proto, src, dst = ip[9], socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
    if src != ME:
        continue
    l4 = ip[ihl:]
    if proto == 6 and len(l4) >= 14:
        dport, flags = struct.unpack("!H", l4[2:4])[0], l4[13]
        if flags & 0x02 and not flags & 0x10:
            print(f"SYN {dst}:{dport}", flush=True)
    elif proto == 17 and len(l4) >= 4:
        print(f"UDP {dst}:{struct.unpack('!H', l4[2:4])[0]}", flush=True)
    else:
        print(f"IP proto={proto} {dst}", flush=True)
PY

cat > "$WORK/complete.py" <<'PY'
"""One extraction-shaped call through the provider seam: the prompt is byte-identical to extract.extract()."""
import json
from analyzer import entity_types, extract, llm
transcript = ("user: the queue keeps losing sessions when the ETL restarts\n"
              "assistant: we decided to move the pending queue to SQLite in WAL mode with one writer per file, "
              "and to park sessions over budget instead of dropping them.\n"
              "user: agreed, ship it behind CSA_DRAIN_WORKERS=3\n")
prompt = extract._SCHEMA_RULES + entity_types.prompt_block(None) + "TRANSCRIPT:\n" + transcript
text, cost, usage = llm.complete(prompt, model="sonnet")
print(json.dumps({"text": text, "cost_usd": cost, "usage": usage}))
PY

cat > "$WORK/refused.py" <<'PY'
"""The call must raise RuntimeError (systemic, the queue parks) naming the fix, and report the same at boot."""
from analyzer import llm
try:
    llm.complete("reply OK", "sonnet")
except RuntimeError as e:
    assert "--target with-claude-code" in str(e), e
    assert llm.provider_summary() == f"NONE: {e}", llm.provider_summary()
    assert llm.blocked() == str(e) and not llm.model_ready()
    print(e)
else:
    raise SystemExit("the call went through")
PY

cat > "$WORK/compare.py" <<'PY'
"""Per-check statuses of two run.py --json results; prints the diff, exits 1 when they differ."""
import json, sys
def checks(p):
    text = open(p).read()                          # run.py prints its status lines first, then the JSON
    res = json.loads(text[text.index("\n{") + 1:])
    cases = res["cases"] if isinstance(res, dict) else res
    return sorted(f"{c['id']} :: {ch['name']} = {ch['status']}" for c in cases for ch in c["checks"])
a, b = checks(sys.argv[1]), checks(sys.argv[2])
for line in sorted(set(a) ^ set(b)):
    print(("host  " if line in a else "image ") + line)
print(f"{len(a)} checks on host, {len(b)} in the image")
sys.exit(0 if a == b else 1)
PY

# --- image ----------------------------------------------------------------------------------------------
section "image"
if docker build -f "$SERVER/Dockerfile" ${BUILD_TARGET[@]+"${BUILD_TARGET[@]}"} -t "$IMG" --build-arg CSA_VERSION=onprem-e2e "$ROOT" > "$WORK/build.log" 2>&1; then
  pass "docker build $IMG, $TARGET (CSA_VERSION=onprem-e2e)"
else
  tail -40 "$WORK/build.log"; fail "docker build"; exit 1
fi
if bash "$SERVER/image-smoke.sh" "$IMG" onprem-e2e "$TARGET" > "$WORK/smoke.log" 2>&1; then
  pass "image-smoke.sh: $(tail -1 "$WORK/smoke.log")"
else
  cat "$WORK/smoke.log"; fail "image-smoke.sh"
fi

# --- network --------------------------------------------------------------------------------------------
section "internal network + sidecars"
# scripts travel by `docker cp` into created containers: a bind mount needs the host path shared with the VM
sidecar(){ local name="$1" script="$2" args="$3"; shift 3
  docker create --name "$name" "$@" "$BASE" sh -c "exec python3 /$script $args" >/dev/null
  docker cp "$WORK/$script" "$name:/$script"; docker start "$name" >/dev/null; }
# the first free 10.253.x.0/24, so a second run on the same daemon gets its own
NETP=""
for i in $(seq 0 249); do
  cand="10.253.$(( ($$ + i) % 250 ))"
  docker network create --internal --subnet "$cand.0/24" "$NET" >/dev/null 2>&1 && { NETP="$cand"; break; }
done
[ -n "$NETP" ] || { echo "no free 10.253.x.0/24 for $NET" >&2; exit 3; }
IP_SINK="$NETP.2"; IP_STUB="$NETP.3"; IP_API="$NETP.10"
sidecar "$P-sinkhole" sinkhole.py "$IP_STUB" --network "$NET" --ip "$IP_SINK"
sidecar "$P-stub" stub.py "" --network "$NET" --ip "$IP_STUB" -e "CSA_E2E_UPSTREAM=$UPSTREAM"
if [ -n "$UPSTREAM" ]; then
  docker network connect bridge "$P-stub"          # the proxy needs a route out; the image never gets one
  echo "  proxy mode: stub forwards to $UPSTREAM"
fi
API_ENV=(-e ANTHROPIC_BASE_URL=http://stub:8080)
if [ -n "$UPSTREAM" ]; then
  API_ENV+=(-e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}" -e "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}")
else
  API_ENV+=(-e ANTHROPIC_API_KEY=sk-ant-api03-onprem-e2e-dummy)
fi
docker run -d --name "$P-api" --network "$NET" --ip "$IP_API" --dns "$IP_SINK" \
  "${API_ENV[@]}" "$IMG" sleep infinity >/dev/null
sidecar "$P-sniff" sniff.py "$IP_API" --network "container:$P-api" --cap-add NET_RAW
up=0
for _ in $(seq 1 30); do
  if docker exec "$P-api" /app/.venv/bin/python -c \
      "import urllib.request; urllib.request.urlopen('http://stub:8080/health', timeout=2)" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
if [ "$up" = 1 ]; then pass "image resolves and reaches stub through the sinkhole on the internal network"
else fail "image cannot reach http://stub:8080 (sinkhole/stub logs follow)"; docker logs "$P-sinkhole"; docker logs "$P-stub"; exit 1; fi

# --- inside the image -----------------------------------------------------------------------------------
section "inside the image"
uid="$(docker exec "$P-api" id -u)"
if [ "$uid" = 10001 ]; then pass "(c) id -u = 10001"; else fail "(c) id -u = $uid, want 10001"; fi
ALIASES="$(docker exec "$P-api" /app/.venv/bin/python -c "from analyzer import llm; print(' '.join(llm._API_MODELS))")"
WANT_MODELS="$(docker exec "$P-api" /app/.venv/bin/python -c "from analyzer import llm; print(' '.join(sorted(llm._API_MODELS.values())))")"
N_ALIASES="$(printf '%s\n' $ALIASES | wc -l | tr -d ' ')"
if [ "$TARGET" = slim ]; then
  if docker exec "$P-api" sh -c 'command -v claude' >/dev/null; then fail "claude is on the PATH of the slim image"
  else pass "the slim image has no claude on its PATH"; fi
else
  cli_ver="$(docker exec "$P-api" claude --version)"
  case "$cli_ver" in 2.1.270*) pass "claude --version = $cli_ver" ;; *) fail "claude --version = $cli_ver, want 2.1.270" ;; esac
fi

for alias in $ALIASES; do                         # every alias llm.py callers use, each to the same full model id
  if [ "$TARGET" = slim ]; then
    if docker exec "$P-api" /app/.venv/bin/python -c "from analyzer import llm; assert llm.complete('reply OK', '$alias')[0] == 'OK'" 2> "$WORK/https-$alias.err"
    then pass "llm.complete('reply OK', '$alias') over HTTPS with the API key"
    else fail "llm.complete('$alias') over HTTPS"; sed 's/^/    /' "$WORK/https-$alias.err" | tail -8; fi
    continue
  fi
  set +e
  docker exec "$P-api" timeout 180 claude -p 'reply OK' --model "$alias" --output-format json \
    > "$WORK/cli-$alias.json" 2> "$WORK/cli-$alias.err"; rc=$?
  set -e
  cli_summary="$(python3 - "$WORK/cli-$alias.json" "$rc" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
try:
    o = json.loads(raw)
except ValueError:
    print(f"exit={sys.argv[2]} non-json stdout: {raw[:200]!r}"); sys.exit(1)
ok = sys.argv[2] == "0" and not o.get("is_error")
models = ",".join((o.get("modelUsage") or {}).keys()) or "?"
print(f"exit={sys.argv[2]} is_error={o.get('is_error')} result={str(o.get('result'))[:60]!r} "
      f"modelUsage={models} cost={o.get('total_cost_usd')}")
sys.exit(0 if ok else 1)
PY
)" && cli_ok=1 || cli_ok=0
  if [ "$cli_ok" = 1 ]; then pass "claude -p 'reply OK' --model $alias: $cli_summary"
  else fail "claude -p --model $alias refused or errored: $cli_summary"; printf '  stderr:\n'; sed 's/^/    /' "$WORK/cli-$alias.err" | head -30; fi
done

set +e
docker exec -i "$P-api" /app/.venv/bin/python - < "$WORK/complete.py" > "$WORK/complete.json" 2> "$WORK/complete.err"; rc=$?
set -e
if [ "$rc" = 0 ]; then
  complete_summary="$(python3 -c "
import json, sys; o = json.load(open(sys.argv[1])); u = o['usage'] or {}
print(f\"text={o['text'][:60]!r} model={u.get('model')} cost={o['cost_usd']} in={u.get('input_tokens')} out={u.get('output_tokens')}\")
" "$WORK/complete.json")"
  if [ -z "$UPSTREAM" ] && [ "$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['text'])" "$WORK/complete.json")" != "OK" ]; then
    fail "llm.complete(extraction prompt) returned something other than the canned reply: $complete_summary"
  else
    pass "llm.complete(extraction prompt) through the provider seam: $complete_summary"
  fi
else
  fail "llm.complete(extraction prompt) raised (exit $rc)"; sed 's/^/    /' "$WORK/complete.err" | tail -15
fi

# the referee inside the image: /home/csa/repo/{research,server->/app} so run.py finds analyzer/ at /app
docker exec "$P-api" mkdir -p /home/csa/repo
tar -C "$ROOT" --exclude research/results --exclude research/phoenix/data -cf - research | docker exec -i "$P-api" tar -xf - -C /home/csa/repo --delay-directory-restore
docker exec "$P-api" ln -s /app /home/csa/repo/server
if docker exec "$P-api" /app/.venv/bin/python /home/csa/repo/research/run.py --selfcheck > "$WORK/selfcheck.log" 2>&1; then
  pass "research/run.py --selfcheck inside the image (uid 10001, no network)"
else fail "research/run.py --selfcheck inside the image"; tail -5 "$WORK/selfcheck.log"; fi

if [ "$TARGET" = slim ]; then
  if said="$(docker exec -i -e ANTHROPIC_API_KEY= -e "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN" "$P-api" /app/.venv/bin/python - < "$WORK/refused.py" 2>&1)"
  then pass "a subscription token alone is refused, sends nothing: $said"
  else fail "a subscription token alone: $said"; fi
  if said="$(docker exec -i -e CLAUDE_CODE_USE_BEDROCK=1 "$P-api" /app/.venv/bin/python - < "$WORK/refused.py" 2>&1)"
  then pass "a Bedrock switch beside the API key is refused, not swapped for Anthropic: $said"
  else fail "a Bedrock switch beside the API key: $said"; fi
fi

if [ -n "$(ls -A "$RESEARCH/corpus" 2>/dev/null)" ]; then
  HOSTPY="$SERVER/.venv/bin/python"; [ -x "$HOSTPY" ] || HOSTPY="uv run --project $SERVER python"
  $HOSTPY "$RESEARCH/run.py" --json > "$WORK/suite-host.json" 2> "$WORK/suite-host.log" || true
  docker exec "$P-api" /app/.venv/bin/python /home/csa/repo/research/run.py --json > "$WORK/suite-image.json" 2> "$WORK/suite-image.log" || true
  if python3 "$WORK/compare.py" "$WORK/suite-host.json" "$WORK/suite-image.json" > "$WORK/compare.log" 2>&1; then
    pass "golden cases: image == host, $(tail -1 "$WORK/compare.log") ($(grep -m1 ' pass · ' "$WORK/suite-image.json" || true))"
  else fail "golden cases differ between host and image"; cat "$WORK/compare.log"; fi
else
  skip "golden-case suite: no snapshot under research/corpus/ (sh research/snapshot.sh /path/to/analyzer.db)"
fi

# --- egress -----------------------------------------------------------------------------------------------
section "egress observed from the image"
sleep 1
docker logs "$P-sinkhole" > "$WORK/sinkhole.log" 2>&1
docker logs "$P-stub"     > "$WORK/stub.log" 2>&1
docker logs "$P-sniff"    > "$WORK/sniff.log" 2>&1
names="$(awk '/^Q /{print $3}' "$WORK/sinkhole.log" | sort -u)"
bad_names="$(printf '%s\n' "$names" | grep -v -x 'stub' || true)"
echo "  names asked: $(printf '%s' "$names" | tr '\n' ' ')"
if [ -z "$bad_names" ]; then pass "(a) sinkhole saw no name other than stub"
else
  fail "(a) sinkhole saw: $(printf '%s' "$bad_names" | tr '\n' ' ')"
  printf '%s\n' "$bad_names" | grep -E "$FORBIDDEN" | sed 's/^/    forbidden: /' || true
fi
syns="$(awk '/^SYN /{print $2}' "$WORK/sniff.log" | sort -u)"
udps="$(awk '/^UDP /{print $2}' "$WORK/sniff.log" | sort -u)"
other="$(grep -E '^(IPV6|IP ) ' "$WORK/sniff.log" | sort -u || true)"
echo "  SYN to: $(printf '%s' "$syns" | tr '\n' ' ')   UDP to: $(printf '%s' "$udps" | tr '\n' ' ')"
if [ "$(printf '%s\n' "$syns" | grep -v -x "$IP_STUB:8080" || true)" = "" ] \
   && [ "$(printf '%s\n' "$udps" | grep -v -x "$IP_SINK:53" || true)" = "" ] && [ -z "$other" ]; then
  pass "(a) every packet went to stub:8080 or sinkhole:53"
else fail "(a) packets left for another destination"; printf '%s\n' "$other" | sed 's/^/    /'; fi

reqs="$(grep '^REQ ' "$WORK/stub.log" || true)"
n_msgs="$(printf '%s\n' "$reqs" | grep -c 'POST /v1/messages' || true)"
models="$(printf '%s\n' "$reqs" | grep -o 'model=[^ ]*' | sort -u | sed 's/^model=//' || true)"
if [ "$n_msgs" -ge "$((N_ALIASES + 1))" ]; then pass "(b) stub received $n_msgs POST /v1/messages (one per alias + llm.complete)"
else fail "(b) stub received $n_msgs POST /v1/messages, want >= $((N_ALIASES + 1))"; fi
echo "  FULL MODEL IDS SENT: $(printf '%s' "$models" | tr '\n' ' ')"
if [ "$(printf '%s' "$models" | tr '\n' ' ' | sed 's/ $//')" = "$WANT_MODELS" ]; then
  pass "(b) the model ids sent are exactly llm._API_MODELS: slim and the pinned CLI call the same models"
else fail "(b) model ids sent [$(printf '%s' "$models" | tr '\n' ' ')], llm._API_MODELS names [$WANT_MODELS]"; fi
if [ "$TARGET" = slim ]; then
  if grep -q '^LEAK ' "$WORK/stub.log"; then fail "the subscription token reached the model endpoint: $(grep '^LEAK ' "$WORK/stub.log" | tr '\n' ' ')"
  else pass "the subscription token never reached the model endpoint"; fi
fi
printf '%s\n' "$reqs" | sed 's/^/  /'
if grep -q '^UNKNOWN ' "$WORK/stub.log"; then fail "the CLI asked the stub for a route it does not serve: $(grep '^UNKNOWN ' "$WORK/stub.log" | tr '\n' ' ')"; fi
iso="$(printf '%s\n' "$reqs" | grep 'tag=extract' || true)"
if [ -n "$iso" ] && ! grep -q -v 'tools=0 mcp_servers=0' <<<"$iso"; then
  pass "isolated call carried tools=0 mcp_servers=0"
elif [ -z "$iso" ]; then fail "no extraction-shaped request reached the stub"
else fail "isolated call carried tools or MCP servers: $iso"; fi
if [ -n "$UPSTREAM" ]; then
  printf '%s\n' "$(grep '^UPSTREAM ' "$WORK/stub.log" || true)" | sed 's/^/  /'
fi

if [ "$TARGET" = with-claude-code ]; then
  if grep -q 'downloads.claude.ai' <<<"$names" || grep -q 'model-catalog' "$WORK/stub.log"; then
    fail "(d) FINDING: --model sonnet wanted the model catalog (downloads.claude.ai); see the names above"
  else pass "(d) --model sonnet resolved with no model-catalog fetch"; fi
fi

printf "\n${C_B}%d passed, %d failed, %d skipped${C_0}\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" = 0 ]
