#!/usr/bin/env bash
#
# CSA on-prem E2E: the whole compose behind its TLS proxy, on a network with no route out, with a model
# gateway and an identity provider that are stubs, a laptop that can reach only the proxy, and proof that
# nothing else was ever asked for.
#
# Runs docker-compose.trial.yml with `--profile proxy` on a Docker network created `--internal`, plus exactly
# three attached sidecars and one laptop:
#   * sinkhole  a DNS server the compose services and the laptop resolve through (`dns:`); it logs every name
#               and answers only `csa` (the proxy), `stub` and `idp`. Docker's embedded resolver answers the
#               compose service names itself and forwards everything else here.
#   * stub      an Anthropic-format Messages endpoint (B's stub from onprem-egress.sh) whose canned reply
#               depends on the prompt: an extraction JSON carrying FACTCLAIM=<token> from the transcript, an
#               fts plan for the planner, so the mocked extraction yields recallable facts like org.sh.
#   * idp       tests/oidc_stub.py from inside the server image, served over TLS as https://idp (csa.py sso
#               set refuses a plain-http issuer off loopback) behind a CA this harness mints; the api trusts
#               it through the compose's documented SSL_CERT_FILE block, applied here by an override file.
#   * laptop    buildpack-deps:bookworm-curl (curl, bash, sha256sum, no python), trusting the proxy's internal
#               root and the idp CA via update-ca-certificates and NODE_EXTRA_CA_CERTS.
# A sniffer on the network's bridge (host network namespace, promiscuous) logs every TCP SYN and UDP datagram
# by destination, so an IP-literal connection that never touched DNS is caught too.
#
# Sequence: a stale pre-Alembic data/ fixture (migrate must bring it to head) and a corrupt one (migrate
# must fail and keep api/mcp/etl down); boot; adopt-default + sso set at the stub issuer; sign-in through the
# proxy; the negatives (invite approve without an identity, ui-login, workspace-request, the landing);
# `curl https://csa/install | sh` on the laptop; `csa-client setup --yes --no-browser` approved from the
# signed-in browser session; three sessions shipped by the client; drained by the etl through the stub;
# recall via /api/ask and via /mcp through the proxy; `csa-client update` from /latest; then the egress ledger.
#
#   server/e2e/run.sh onprem                     the default build, no Claude Code: extraction goes over HTTPS
#   server/e2e/run.sh onprem with-claude-code    the target production runs: extraction goes through the CLI
#   CSA_SERVER_IMAGE=<local tag>             reuse a built image of that target instead of building (and keep it)
#   CSA_E2E_KEEP=1                            keep the workspace (logs, hosts.txt, files.txt)
#
# Needs docker with buildx and compose v2 (the plugin or the standalone docker-compose), go (the update step
# publishes a second client build for the daemon's architecture), openssl. Runs OUTSIDE pytest. On colima the
# workspace lives under $HOME/.cache: only $HOME and /tmp/colima are shared with the VM, so a bind mount
# anywhere else is silently empty. The box's data/ is a named volume rather than a bind mount: colima shares
# the host through sshfs, which has no working file locks, and SQLite's WAL sidecars raced between
# containers there (the runbook's "SQLite on NFS corrupts", observed as `unable to open database file` on
# a second `up`). The volume is native ext4 inside the VM, and it is where the runbook's chown to 10001 is
# exercised for real.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERVER="$ROOT/server"

C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
PASS=0; FAIL=0; SKIP=0
pass(){ printf "  ${C_G}PASS${C_0} %s\n" "$1"; PASS=$((PASS+1)); }
fail(){ printf "  ${C_R}FAIL${C_0} %s\n" "$1"; FAIL=$((FAIL+1)); }
skip(){ printf "  ${C_Y}SKIP${C_0} %s\n" "$1"; SKIP=$((SKIP+1)); }
section(){ printf "\n${C_B}== %s ==${C_0}\n" "$1"; }
die(){ printf "${C_R}%s${C_0}\n" "$1" >&2; exit 3; }

TARGET="${1:-slim}"
case "$TARGET" in
  slim) BUILD_TARGET=() ;;
  with-claude-code) BUILD_TARGET=(--target with-claude-code) ;;
  *) die "the first argument is slim or with-claude-code, not '$TARGET'" ;;
esac

P="csa-onprem-$$"
NET="$P-net"
IMG="${CSA_SERVER_IMAGE:-csa-server:$P}"
LAPTOP_IMG="buildpack-deps:bookworm-curl@sha256:c63e1f47b008e42150a551f51443fd5007d737e9951661bd18b0a808db56abbe"
FORBIDDEN='downloads\.claude\.ai|sentry\.io|datadoghq\.com|storage\.googleapis\.com|registry\.npmjs\.org|api\.anthropic\.com|claude\.ai|platform\.claude\.com|api\.openai\.com|pypi\.org|files\.pythonhosted\.org|deb\.debian\.org|ghcr\.io|github\.com|accounts\.google\.com|googleapis\.com|letsencrypt\.org|^ocsp\.|statsig'

docker info >/dev/null 2>&1 || die "docker is not running"
docker buildx version >/dev/null 2>&1 || die "docker buildx is required (the Dockerfile reads ARG TARGETARCH)"
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
else die "compose v2 is required (docker compose, or docker-compose 2.x)"; fi
command -v go >/dev/null 2>&1 || die "go is required to build the second csa-client the update step publishes"
command -v openssl >/dev/null 2>&1 || die "openssl is required for the idp certificate"
HOSTPY="$SERVER/.venv/bin/python"; [ -x "$HOSTPY" ] || HOSTPY="uv run --project $SERVER python"
DAEMON_ARCH="$(docker version --format '{{.Server.Arch}}')"
BASE="$(grep -o 'python:3.12-slim@sha256:[0-9a-f]*' "$SERVER/Dockerfile" | head -1 || true)"
[ -n "$BASE" ] || die "cannot read the pinned base image from server/Dockerfile"

mkdir -p "$HOME/.cache"
WORK="$(mktemp -d "$HOME/.cache/csa-onprem-e2e.XXXXXX")"
BOX="$WORK/box"
VOL="$P-data"
mkdir -p "$BOX/certs" "$BOX/config" "$WORK/idp" "$WORK/fixture" "$WORK/client-dist"
cleanup(){
  set +e
  if [ -f "$BOX/override.yml" ]; then
    compose down -v --remove-orphans >/dev/null 2>&1
  fi
  docker rm -f "$P-laptop" "$P-guard" "$P-sniff" "$P-idp" "$P-stub" "$P-sinkhole" >/dev/null 2>&1
  docker volume rm "$VOL" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  [ -n "${CSA_SERVER_IMAGE:-}" ] || docker rmi "$IMG" >/dev/null 2>&1
  if [ -n "${CSA_E2E_KEEP:-}" ]; then echo "kept workspace: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT
echo "workspace: $WORK"
echo "compose:   ${COMPOSE[*]} ($("${COMPOSE[@]}" version --short))   daemon arch: $DAEMON_ARCH"

compose(){ "${COMPOSE[@]}" -f "$SERVER/docker-compose.trial.yml" -f "$BOX/override.yml" \
             --project-name "$P" --project-directory "$BOX" --env-file "$BOX/.env" --profile proxy "$@"; }
lap(){ docker exec "$P-laptop" "$@"; }
api(){ compose exec -T api /app/.venv/bin/python csa.py "$@"; }
# curl from the laptop: -sS, the cookie jar, and the status code on the last line of stdout
lcurl(){ lap curl -sS -b /root/jar -c /root/jar -w '\n%{http_code}' "$@"; }
last(){ tail -n1; }
body(){ sed '$d'; }

# --- sidecar scripts --------------------------------------------------------------------------------------
cat > "$WORK/sinkhole.py" <<'PY'
"""DNS sinkhole: log every question; answer A for the names given as name=ip arguments, NXDOMAIN for the rest."""
import socket, struct, sys
ANSWERS = dict(a.split("=") for a in sys.argv[1:])
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
    print(f"Q {QTYPES.get(qtype, qtype)} {name} from {peer[0]}", flush=True)
    if name in ANSWERS:
        if qtype == 1:
            rr = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(ANSWERS[name])
            s.sendto(msg[:2] + b"\x81\x80" + struct.pack("!HHHH", 1, 1, 0, 0) + question + rr, peer)
        else:
            s.sendto(msg[:2] + b"\x81\x80" + struct.pack("!HHHH", 1, 0, 0, 0) + question, peer)
    else:
        s.sendto(msg[:2] + b"\x81\x83" + struct.pack("!HHHH", 1, 0, 0, 0) + question, peer)
PY

cat > "$WORK/stub.py" <<'PY'
"""Anthropic-format stub: logs every request and answers what the prompt is for, so the real extraction
pipeline yields facts. The transcript's FACTCLAIM=<token> becomes the decision it extracts; the planner
gets an fts plan; anything else gets a bland extraction (the shape org.sh's mocked claude uses)."""
import json, re, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def extraction(claim):
    return json.dumps({"summary": claim, "worked_on": [claim],
        "hidden_context": [{"type": "decision", "scope": "organizational", "confidence": "high",
                            "claim": claim, "evidence": claim}],
        "entities": [{"id": "payments-service", "type": "service", "name": "payments-service"}],
        "relations": [], "actions": [], "links": [], "data_sources": [], "deployed_to_prod": False})

def answer(text):
    if "Translate the question into ONE read-only query" in text:
        return json.dumps({"engine": "fts", "query": "nothing"})
    m = re.search(r"FACTCLAIM=([A-Za-z0-9 ._-]+)", text)
    return extraction(m.group(1) if m else "a decision was recorded")

def canned(model, stream, text):
    usage = {"input_tokens": 10, "output_tokens": 1}
    if not stream:
        return "application/json", json.dumps({
            "id": "msg_stub", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": usage}).encode()
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_stub", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": usage}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
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

    def _reply(self, status, ctype, out):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        route = urllib.parse.urlsplit(self.path).path
        if route == "/health":
            return self._reply(200, "text/plain", b"ok\n")
        if route == "/api/hello":
            print(f"REQ {self.command} {self.path}", flush=True)
            return self._reply(200, "text/plain", b"")
        try:
            b = json.loads(raw or b"{}")
        except ValueError:
            b = {}
        msgs = b.get("messages") or []
        text = json.dumps(msgs)
        tag = "extract" if "TRANSCRIPT:" in text else "plan" if "read-only query" in text else "other"
        print(f"REQ {self.command} {self.path} model={b.get('model')} stream={b.get('stream', False)} "
              f"tools={len(b.get('tools') or [])} mcp_servers={len(b.get('mcp_servers') or [])} tag={tag}", flush=True)
        if route == "/v1/messages" and self.command == "POST":
            ctype, out = canned(b.get("model"), bool(b.get("stream")), answer(text))
            return self._reply(200, ctype, out)
        if route == "/v1/messages/count_tokens" and self.command == "POST":
            return self._reply(200, "application/json", json.dumps({"input_tokens": 10}).encode())
        print(f"UNKNOWN {self.command} {self.path}", flush=True)
        self._reply(404, "application/json", json.dumps({"type": "error", "error": {
            "type": "not_found_error", "message": f"stub has no route for {self.command} {route}"}}).encode())

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _handle

print("stub up", flush=True)
ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
PY

cat > "$WORK/idp.py" <<'PY'
"""tests/oidc_stub.py as https://idp: the same handler behind a TLS listening socket, logging every request."""
import ssl, sys, threading
from http.server import ThreadingHTTPServer
sys.path.insert(0, "/app")
import tests.oidc_stub as oidc

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/idp/idp.crt", "/idp/idp.key")

class TLSServer(ThreadingHTTPServer):
    def server_bind(self):
        super().server_bind()
        self.socket = ctx.wrap_socket(self.socket, server_side=True)

oidc.ThreadingHTTPServer = TLSServer
stub = oidc.OidcStub(443, host="0.0.0.0")
stub.issuer = "https://idp"
stub.email = sys.argv[1]
base = stub.server.RequestHandlerClass
stub.server.RequestHandlerClass = type("Logged", (base,), {
    "log_message": lambda self, fmt, *a: print("REQ " + (fmt % a), flush=True)})
print(f"idp up at {stub.issuer}, signs in {stub.email}", flush=True)
threading.Event().wait()
PY

cat > "$WORK/sniff.py" <<'PY'
"""Log every TCP SYN and UDP datagram crossing the network's bridge, by source and destination."""
import socket, struct, sys
IFACE = sys.argv[1]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
s.bind((IFACE, 0))
s.setsockopt(263, 1, struct.pack("IHH8s", socket.if_nametoindex(IFACE), 1, 0, b"\x00" * 8))   # PACKET_MR_PROMISC
print(f"sniff up on {IFACE}", flush=True)
while True:
    frame, _ = s.recvfrom(65535)
    if len(frame) < 34:
        continue
    eth = struct.unpack("!H", frame[12:14])[0]
    if eth == 0x86DD:
        dst = frame[38:54]
        if dst[:2] != b"\xff\x02":
            print(f"IPV6 {dst.hex()}", flush=True)
        continue
    if eth != 0x0800:
        continue
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    proto, src, dst = ip[9], socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
    l4 = ip[ihl:]
    if proto == 6 and len(l4) >= 14:
        dport, flags = struct.unpack("!H", l4[2:4])[0], l4[13]
        if flags & 0x02 and not flags & 0x10:
            print(f"SYN {src} {dst}:{dport}", flush=True)
    elif proto == 17 and len(l4) >= 4:
        print(f"UDP {src} {dst}:{struct.unpack('!H', l4[2:4])[0]}", flush=True)
PY

# --- image + client -----------------------------------------------------------------------------------------
section "image and client"
if [ -n "${CSA_SERVER_IMAGE:-}" ]; then
  docker image inspect "$IMG" >/dev/null 2>&1 || die "CSA_SERVER_IMAGE=$IMG is not a local image"
  pass "using $IMG"
else
  if docker build -f "$SERVER/Dockerfile" ${BUILD_TARGET[@]+"${BUILD_TARGET[@]}"} -t "$IMG" --build-arg CSA_VERSION=onprem-e2e "$ROOT" > "$WORK/build.log" 2>&1; then
    pass "docker build $IMG, $TARGET (CSA_VERSION=onprem-e2e)"
  else tail -40 "$WORK/build.log"; fail "docker build"; exit 1; fi
fi
found_cli="$(docker run --rm "$IMG" sh -c 'command -v claude' || true)"
case "$TARGET:$found_cli" in
  slim:) pass "the slim image has no claude on its PATH" ;;
  with-claude-code:?*) pass "the with-claude-code image has $found_cli" ;;
  *) fail "$TARGET image: command -v claude says '$found_cli'"; exit 1 ;;
esac
if docker run --rm "$IMG" /app/.venv/bin/python -c "import tests.oidc_stub" >/dev/null 2>&1; then
  pass "tests/oidc_stub.py is importable inside the image (the idp sidecar runs it)"
else fail "tests.oidc_stub is not importable inside $IMG"; exit 1; fi

IMG_VERSION="$(docker run --rm "$IMG" cat /app/VERSION)"
docker run --rm "$IMG" sh -c 'cd /app/client-dist && sha256sum -c --quiet checksums.txt' \
  && pass "the image carries its own client build ($IMG_VERSION) + checksums.txt" || { fail "the image's client-dist"; exit 1; }
CLIENT_ASSET="csa-client-linux-$DAEMON_ARCH"

# --- certificates ----------------------------------------------------------------------------------------------
section "the idp's CA (the customer's corporate CA in this story)"
( cd "$WORK/idp" \
  && openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=CSA e2e corporate CA" \
       -addext "basicConstraints=critical,CA:TRUE" -keyout ca.key -out ca.crt \
  && openssl req -newkey rsa:2048 -nodes -subj "/CN=idp" -keyout idp.key -out idp.csr \
  && printf 'subjectAltName=DNS:idp\n' > san.cnf \
  && openssl x509 -req -in idp.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 2 -extfile san.cnf -out idp.crt ) > "$WORK/openssl.log" 2>&1 \
  && cp "$WORK/idp/ca.crt" "$BOX/certs/ca.crt" \
  && pass "minted the idp CA + leaf; the root is certs/ca.crt (what SSL_CERT_FILE points at)" \
  || { cat "$WORK/openssl.log"; fail "openssl"; exit 1; }

# --- network + sidecars ------------------------------------------------------------------------------------------
section "internal network, sinkhole, stub, idp, sniffer"
# the first free 10.254.x.0/24, so a second run on the same daemon (another checkout, a CI shard) gets its own
NETP=""
for i in $(seq 0 249); do
  cand="10.254.$(( ($$ + i) % 250 ))"
  docker network create --internal --subnet "$cand.0/24" "$NET" >/dev/null 2>&1 && { NETP="$cand"; break; }
done
[ -n "$NETP" ] || die "no free 10.254.x.0/24 for $NET"
SUBNET="$NETP.0/24"
IP_SINK="$NETP.2"; IP_STUB="$NETP.3"; IP_IDP="$NETP.4"
IP_PROXY="$NETP.20"; IP_API="$NETP.21"; IP_MCP="$NETP.22"; IP_ETL="$NETP.23"; IP_MIGRATE="$NETP.24"
IP_LAPTOP="$NETP.30"
pass "network $NET on $SUBNET"
BRIDGE="br-$(docker network inspect -f '{{.Id}}' "$NET" | cut -c1-12)"
sidecar(){ local name="$1" script="$2" args="$3" image="$4"; shift 4
  docker create --name "$name" "$@" "$image" sh -c "exec python3 /$script $args" >/dev/null
  docker cp "$WORK/$script" "$name:/$script"; }
sidecar "$P-sinkhole" sinkhole.py "csa=$IP_PROXY stub=$IP_STUB idp=$IP_IDP" "$BASE" --network "$NET" --ip "$IP_SINK"
docker start "$P-sinkhole" >/dev/null
sidecar "$P-stub" stub.py "" "$BASE" --network "$NET" --ip "$IP_STUB"
docker start "$P-stub" >/dev/null
docker create --name "$P-idp" --network "$NET" --ip "$IP_IDP" --user 0 "$IMG" /app/.venv/bin/python /idp.py alice@acme.test >/dev/null
docker cp "$WORK/idp.py" "$P-idp:/idp.py"
docker cp "$WORK/idp" "$P-idp:/idp"
docker start "$P-idp" >/dev/null
sidecar "$P-sniff" sniff.py "$BRIDGE" "$BASE" --network host --cap-add NET_RAW
docker start "$P-sniff" >/dev/null
sleep 2
for c in sinkhole stub idp sniff; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$P-$c")" = true ]; then pass "$c up"; else fail "$c died"; docker logs "$P-$c"; exit 1; fi
done

# --- the box ----------------------------------------------------------------------------------------------------
section "the box: kit, .env, a pre-Alembic data/"
cp "$SERVER/Caddyfile" "$BOX/Caddyfile"
cat > "$BOX/.env" <<EOF
CSA_PUBLIC_URL=https://csa
CSA_SERVER_IMAGE=$IMG
ANTHROPIC_BASE_URL=http://stub:8080
ANTHROPIC_API_KEY=sk-ant-api03-onprem-e2e-dummy
EOF
# what the runbook's §3b block does, applied without editing the tracked compose; the sinkhole as resolver and
# fixed addresses so it can answer `csa`; data/ on the named volume (see the header)
docker volume create "$VOL" >/dev/null
cat > "$BOX/override.yml" <<EOF
networks:
  default:
    name: $NET
    external: true
volumes:
  boxdata:
    name: $VOL
    external: true
services:
  migrate:
    dns: [$IP_SINK]
    networks: { default: { ipv4_address: $IP_MIGRATE } }
    volumes: ["boxdata:/app/data"]
  api:
    dns: [$IP_SINK]
    networks: { default: { ipv4_address: $IP_API } }
    environment: { SSL_CERT_FILE: /app/certs/ca.crt, NODE_EXTRA_CA_CERTS: /app/certs/ca.crt }
    volumes: ["boxdata:/app/data", "./certs:/app/certs:ro"]
  mcp:
    dns: [$IP_SINK]
    networks: { default: { ipv4_address: $IP_MCP } }
    environment: { SSL_CERT_FILE: /app/certs/ca.crt, NODE_EXTRA_CA_CERTS: /app/certs/ca.crt }
    volumes: ["boxdata:/app/data", "./certs:/app/certs:ro"]
  etl:
    dns: [$IP_SINK]
    networks: { default: { ipv4_address: $IP_ETL } }
    environment: { SSL_CERT_FILE: /app/certs/ca.crt, NODE_EXTRA_CA_CERTS: /app/certs/ca.crt }
    volumes: ["boxdata:/app/data", "./certs:/app/certs:ro"]
  proxy:
    dns: [$IP_SINK]
    networks: { default: { ipv4_address: $IP_PROXY } }
EOF
compose config -q && pass "compose config validates with the override" || { fail "compose config"; exit 1; }
if [ "$(compose config --format json | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(v['source'].split('/')[-1] for v in d['services']['api']['volumes']))")" = "['boxdata', 'certs', 'config']" ]; then
  pass "the override moved api's /app/data to the volume and kept certs and config"
else fail "api's mounts are not what the override meant: $(compose config --format json | python3 -c "import json,sys; d=json.load(sys.stdin); print([v['source'] for v in d['services']['api']['volumes']])")"; exit 1; fi

# the volume's contents: `indata CMD...` runs a shell over it as root (the operator's sudo), like the runbook's chown
indata(){ docker run --rm -v "$VOL:/app/data" -v "$WORK/fixture:/fixture:ro" "$BASE" sh -c "$1"; }
stale_fixture(){ # the files the last pre-Alembic image left behind: no alembic_version anywhere, owned by uid 10001
  rm -rf "$WORK/fixture"; mkdir -p "$WORK/fixture"
  ( cd "$SERVER" && $HOSTPY - "$WORK/fixture" <<'PY'
import sqlite3, sys
from tests import legacy_ddl
data = sys.argv[1]
con = sqlite3.connect(f"{data}/control.db"); con.executescript(legacy_ddl.JUL_2026_CONTROL); con.commit(); con.close()
con = sqlite3.connect(f"{data}/analyzer.db"); con.executescript(legacy_ddl.JUL_2026_CORPUS)
con.execute("INSERT INTO sessions(session_id,project,user) VALUES('old-1','csa','someone@acme.test')"); con.commit(); con.close()
con = sqlite3.connect(f"{data}/telemetry.db"); con.executescript(legacy_ddl.SEP05_2026_TELEMETRY.replace("tel.", "")); con.commit(); con.close()
PY
  )
  indata 'find /app/data -mindepth 1 -delete && cp /fixture/*.db /app/data/ && chown -R 10001:10001 /app/data && ls -ln /app/data' > "$WORK/fixture.log"
}
version_of(){ # FILE (relative to data/) -> its alembic revision, or none
  indata "python3 -c \"import sqlite3
try: print(sqlite3.connect('file:/app/data/$1?mode=ro', uri=True).execute('SELECT version_num FROM alembic_version').fetchone()[0])
except Exception: print('none')\""; }
HEADS="$(cd "$SERVER" && $HOSTPY -c "from analyzer import migrations; print(' '.join(migrations.head(e) for e in ('control','corpus','telemetry')))")"
read -r HEAD_CONTROL HEAD_CORPUS HEAD_TEL <<<"$HEADS"
chmod -R a+rwX "$BOX/certs"

# --- migrate, negative --------------------------------------------------------------------------------------------
section "migrate, negative: a file that cannot be migrated keeps api, mcp and etl down"
stale_fixture
indata "printf 'not a database at all' > /app/data/control.db"
set +e
compose up -d --no-color > "$WORK/up-negative.log" 2>&1; rc=$?
set -e
if [ "$rc" != 0 ] && grep -qE "didn't complete successfully|dependency failed to start" "$WORK/up-negative.log"; then
  pass "up exits $rc: $(grep -oE "service \"migrate\" didn't complete successfully.*|dependency failed to start.*" "$WORK/up-negative.log" | head -1)"
else fail "up should have failed on the corrupt control.db (rc=$rc)"; tail -5 "$WORK/up-negative.log"; fi
for s in api mcp etl; do
  st="$(docker inspect -f '{{.State.Status}}' "$P-$s-1" 2>/dev/null || echo absent)"
  if [ "$st" != running ]; then pass "$s is $st"; else fail "$s is running behind a failed migrate"; fi
done
migrate_log="$(compose logs migrate 2>&1 || true)"
case "$migrate_log" in *"migrate failed"*) true ;; *) false ;; esac && pass "migrate said why: $(compose logs --no-log-prefix migrate 2>&1 | grep 'migrate failed' | head -1 | cut -c1-90)" \
  || fail "migrate gave no reason"
compose down --remove-orphans >/dev/null 2>&1

# --- boot ----------------------------------------------------------------------------------------------------------
section "boot: migrate first, then api, mcp, etl, proxy"
stale_fixture
[ "$(version_of control.db)" = none ] && [ "$(version_of analyzer.db)" = none ] \
  && pass "fixture is pre-Alembic (no version table in control.db or analyzer.db)" || fail "fixture already stamped"
compose up -d --no-color > "$WORK/up.log" 2>&1 || { fail "compose up"; tail -20 "$WORK/up.log"; exit 1; }
healthy=0
for _ in $(seq 1 180); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$P-api-1" 2>/dev/null)" = healthy ] \
    && [ "$(docker inspect -f '{{.State.Health.Status}}' "$P-mcp-1" 2>/dev/null)" = healthy ] && { healthy=1; break; }
  sleep 2
done
[ "$healthy" = 1 ] && pass "api and mcp healthy (the python urllib probe, no curl in the image)" \
  || { fail "api/mcp never healthy"; compose ps -a; compose logs api | tail -20; exit 1; }
mig_rc="$(docker inspect -f '{{.State.ExitCode}}' "$P-migrate-1")"
[ "$mig_rc" = 0 ] && pass "migrate exited 0" || fail "migrate exited $mig_rc"
compose logs --no-log-prefix migrate > "$WORK/migrate.log" 2>&1
grep -q "control: $HEAD_CONTROL" "$WORK/migrate.log" && grep -q "corpus: $HEAD_CORPUS" "$WORK/migrate.log" \
  && grep -q "telemetry: $HEAD_TEL" "$WORK/migrate.log" \
  && pass "migrate printed the three heads: $(grep -E '^(control|corpus|telemetry):' "$WORK/migrate.log" | tr '\n' ' ')" \
  || { fail "migrate output"; cat "$WORK/migrate.log"; }
[ "$(version_of control.db)" = "$HEAD_CONTROL" ] && [ "$(version_of analyzer.db)" = "$HEAD_CORPUS" ] \
  && [ "$(version_of telemetry.db)" = "$HEAD_TEL" ] \
  && pass "the stale files are stamped at head" || fail "version rows: $(version_of control.db) $(version_of analyzer.db) $(version_of telemetry.db)"
mig_done="$(docker inspect -f '{{.State.FinishedAt}}' "$P-migrate-1")"
order_ok=1
for s in api mcp etl proxy; do
  started="$(docker inspect -f '{{.State.StartedAt}}' "$P-$s-1")"
  python3 -c "import sys,datetime as d
p=lambda t: d.datetime.fromisoformat(t[:26].rstrip('Z')+'+00:00') if '+' not in t[:26] else d.datetime.fromisoformat(t[:26])
sys.exit(0 if p(sys.argv[2]) >= p(sys.argv[1]) else 1)" "$mig_done" "$started" || order_ok=0
done
[ "$order_ok" = 1 ] && pass "api, mcp, etl and proxy all started after migrate finished ($mig_done)" || fail "a service started before migrate finished"
api_started="$(docker inspect -f '{{.State.StartedAt}}' "$P-api-1")"
compose up -d --no-color > "$WORK/up2.log" 2>&1 || true
compose logs --no-log-prefix migrate > "$WORK/migrate2.log" 2>&1
if [ "$(docker inspect -f '{{.State.StartedAt}}' "$P-api-1")" = "$api_started" ] \
   && [ "$(docker inspect -f '{{.State.ExitCode}}' "$P-migrate-1")" = 0 ] \
   && grep -q "control: $HEAD_CONTROL" "$WORK/migrate2.log"; then
  pass "a second up is a no-op: migrate re-ran at head, api was not recreated"
else fail "second up changed something (migrate exit $(docker inspect -f '{{.State.ExitCode}}' "$P-migrate-1"))"; tail -5 "$WORK/up2.log"; grep -v '^20.* INFO ' "$WORK/migrate2.log" | tail -15; fi
# an --internal network publishes no host port at all, so the loopback-only rule is read off the resolved config
pub="$(compose config --format json | python3 -c "import json,sys; d=json.load(sys.stdin); print(' '.join(f\"{s}:{p.get('host_ip','*')}:{p['published']}\" for s in ('api','mcp','proxy') for p in d['services'][s].get('ports',[])))")"
[ "$pub" = "api:127.0.0.1:8766 mcp:127.0.0.1:58768 proxy:*:443" ] && pass "published ports: $pub (api and mcp loopback only; 443 is the way in)" || fail "published ports: $pub"
# the proxy's start command refuses a CSA_PUBLIC_URL that would move Caddy off 443 (a port) or scope it to a path
# `compose config` keeps the `$$` escape that `up` turns into `$` when it creates the container; de-escape the same way
proxy_cmd="$(compose config --format json | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]["proxy"]; print(s["image"]); print(s["command"][2].replace("$$", "$"))')"
proxy_img="$(printf '%s\n' "$proxy_cmd" | head -1)"; proxy_sh="$(printf '%s\n' "$proxy_cmd" | tail -n +2)"
for bad in https://csa:8443 https://csa/csa; do
  docker rm -f "$P-guard" >/dev/null 2>&1 || true
  docker run -d --name "$P-guard" --network none -e "CSA_PUBLIC_URL=$bad" "$proxy_img" sh -c "$proxy_sh" >/dev/null
  rc=""
  for _ in $(seq 1 120); do
    rc="$(docker inspect -f '{{if not .State.Running}}{{.State.ExitCode}}{{end}}' "$P-guard")"
    [ -n "$rc" ] && break
    sleep 0.5
  done
  docker logs "$P-guard" > "$WORK/guard.log" 2>&1; docker rm -f "$P-guard" >/dev/null 2>&1 || true
  if [ "$rc" = 1 ] && grep -q "no port and no path" "$WORK/guard.log"; then pass "the proxy refuses CSA_PUBLIC_URL=$bad before starting"
  elif [ -z "$rc" ]; then fail "the proxy STARTED with CSA_PUBLIC_URL=$bad (still running after 10s)"
  else fail "proxy guard on $bad exited $rc: $(head -2 "$WORK/guard.log" | tr '\n' ' ')"; fi
done

# --- the laptop -------------------------------------------------------------------------------------------------
section "laptop: trusts the proxy's internal root and the corporate CA, reaches only the proxy"
root_ok=0
for _ in $(seq 1 90); do
  compose exec -T proxy cat /data/caddy/pki/authorities/local/root.crt > "$WORK/csa-root.crt" 2>/dev/null && grep -q "BEGIN CERTIFICATE" "$WORK/csa-root.crt" && { root_ok=1; break; }
  sleep 1
done
[ "$root_ok" = 1 ] && pass "exported the proxy's internal root (the runbook's §3a step)" || { fail "no internal root at /data/caddy/pki/authorities/local/root.crt"; compose logs proxy | tail; exit 1; }
docker run -d --name "$P-laptop" --network "$NET" --ip "$IP_LAPTOP" --dns "$IP_SINK" \
  -e NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/csa-root.crt "$LAPTOP_IMG" sleep infinity >/dev/null
docker cp "$WORK/csa-root.crt" "$P-laptop:/usr/local/share/ca-certificates/csa-root.crt"
docker cp "$WORK/idp/ca.crt" "$P-laptop:/usr/local/share/ca-certificates/corporate-ca.crt"
lap update-ca-certificates > "$WORK/laptop-ca.log" 2>&1 && pass "update-ca-certificates: $(tail -1 "$WORK/laptop-ca.log")" || fail "update-ca-certificates"
h="$(lcurl https://csa/api/health)"
if [ "$(printf '%s' "$h" | last)" = 200 ] && printf '%s' "$h" | grep -q '"mcp_url": "https://csa/mcp"'; then
  pass "https://csa/api/health through the proxy, TLS verified, mcp_url=https://csa/mcp: $(printf '%s' "$h" | body | tr -d '\n' | cut -c1-120)"
else fail "health through the proxy: $h"; fi
lap python3 --version >/dev/null 2>&1 && skip "laptop has python (the story wants a bare laptop)" || pass "laptop has curl, bash and sha256sum, no python"
# the compose comment's claim: with SSL_CERT_FILE set, OpenSSL still consults the system directory of public roots
ca_check="$(docker exec "$P-api-1" sh -c 'echo "SSL_CERT_FILE=$SSL_CERT_FILE"; openssl verify /app/certs/ca.crt; openssl verify "$(ls /etc/ssl/certs/*.pem | head -1)"' 2>&1 | tr '\n' ' ')"
case "$ca_check" in
  *"ca.crt: OK"*".pem: OK"*) pass "in the api, $ca_check" ;;
  *) fail "SSL_CERT_FILE in the api: $ca_check" ;;
esac

# --- first boot ---------------------------------------------------------------------------------------------------
section "first boot: adopt-default, sso set at https://idp, no restart"
out="$(api org adopt-default "Acme" --domain acme.test --yes 2>&1)"
printf '%s' "$out" | grep -q "adopted: default is now the org 'Acme'" && pass "adopt-default Acme --domain acme.test --yes" || { fail "adopt-default: $out"; }
out="$(api sso set --issuer https://idp --client-id csa-e2e --name "Acme IdP" 2>&1)"
printf '%s' "$out" | grep -q "redirect URI:  https://csa/api/sso/callback" \
  && pass "sso set at https://idp (discovery fetched through SSL_CERT_FILE); printed redirect URI https://csa/api/sso/callback" \
  || { fail "sso set: $out"; }
INVITE="$(api org invite default 2>&1 | grep -o 'inv_[A-Za-z0-9_-]*' | head -1)"
[ -n "$INVITE" ] && pass "minted an invite to prove it no longer mints anything" || fail "org invite"

# --- sign-in through the proxy ------------------------------------------------------------------------------------
section "sign-in: browser round trip through the proxy to the stub issuer"
lap rm -f /root/jar
r="$(lcurl -L -o /root/signin.html 'https://csa/api/sso/start?return=/')"
if [ "$(printf '%s' "$r" | last)" = 200 ] && lap grep -q "__Host-csa_session" /root/jar && lap grep -q "Signed in" /root/signin.html; then
  pass "start -> idp /authorize -> callback: 200, __Host-csa_session set (Secure, so only over the proxy's TLS)"
else fail "sign-in: $r; jar: $(lap cat /root/jar 2>/dev/null | grep -c csa)"; fi
me="$(lcurl https://csa/api/me)"
if printf '%s' "$me" | grep -q '"email": "alice@acme.test"' && printf '%s' "$me" | grep -q '"hostname": "web-sso"'; then
  pass "/api/me with the cookie: alice@acme.test, hostname web-sso"
else fail "/api/me: $(printf '%s' "$me" | tr '\n' ' ' | cut -c1-200)"; compose logs --no-log-prefix api 2>&1 | grep -A 25 Traceback | tail -30; fi
# capture first: `docker logs | grep -q` under pipefail fails when grep exits early and docker gets SIGPIPE
idp_log=""
for _ in $(seq 1 15); do idp_log="$(docker logs "$P-idp" 2>&1 || true)"; case "$idp_log" in *"POST /token"*) break ;; esac; sleep 1; done
case "$idp_log" in *"POST /token"*) pass "the idp saw the code exchange (POST /token)" ;; *) fail "no token exchange at the idp" ;; esac

section "negatives: the invite paths are dead, the front door is the sign-in"
new="$(lap curl -sS -H 'Content-Type: application/json' -d '{"hostname":"mallory-box"}' https://csa/api/cli-auth/new)"
n="$(printf '%s' "$new" | grep -o '"nonce": "[^"]*"' | cut -d'"' -f4)"; c="$(printf '%s' "$new" | grep -o '"code": "[^"]*"' | cut -d'"' -f4)"
r="$(lap curl -sS -H 'Content-Type: application/json' -d "{\"nonce\":\"$n\",\"code\":\"$c\",\"email\":\"mallory@acme.test\",\"invite\":\"$INVITE\"}" https://csa/api/cli-auth/approve)"
printf '%s' "$r" | grep -q '"ok": false' && printf '%s' "$r" | grep -q "requires signing in with Acme IdP" \
  && pass "approve with a valid invite and no identity is refused: $(printf '%s' "$r" | grep -o '"error": "[^"]*"')" || fail "approve with invite: $r"
printf '%s' "$(lap curl -sS https://csa/api/cli-auth/$n)" | grep -q '"status": "pending"' && pass "...and the pairing stays pending" || fail "pairing minted"
r="$(lap curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "{\"email\":\"mallory@acme.test\",\"invite\":\"$INVITE\"}" https://csa/api/ui-login)"
[ "$r" = 401 ] && pass "ui-login with email + invite -> 401" || fail "ui-login -> $r"
r="$(lap curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"company":"Evil","email":"e@evil.test"}' https://csa/api/workspace-request)"
[ "$r" = 404 ] && pass "POST /api/workspace-request -> 404 (no request webhook on this box)" || fail "workspace-request -> $r"
cfg="$(lap curl -sS https://csa/api/auth-config)"
printf '%s' "$cfg" | grep -q '"workspace_requests": false' && printf '%s' "$cfg" | grep -q '"name": "Acme IdP"' && printf '%s' "$cfg" | grep -q '"sso_required": true' \
  && pass "auth-config: workspace_requests=false, sso Acme IdP required (what routes.frontDoor renders as the sign-in)" || fail "auth-config: $cfg"
r="$(lap curl -sS -o /root/index.html -w '%{http_code}' https://csa/)"
[ "$r" = 200 ] && lap grep -q 'id="app"' /root/index.html && pass "signed-out GET / -> the SPA shell (200); with workspace_requests=false it renders the sign-in, not the landing" || fail "GET / -> $r"

# --- install + setup + ship ---------------------------------------------------------------------------------------
section "laptop: curl https://csa/install | sh, csa-client setup, three sessions"
r="$(lap curl -sS -o /dev/null -w '%{http_code}' https://csa/install)"
[ "$r" = 200 ] && pass "/install answers 200 (CSA_PUBLIC_URL is wired into the api; no 421)" || fail "/install -> $r"
if lap sh -c 'curl -sSL https://csa/install | sh' > "$WORK/install.log" 2>&1 && lap test -x /root/.local/lib/csa/csa-client; then
  pass "installer: $(grep -o 'Verified sha256\|Installed [^ ]*' "$WORK/install.log" | tr '\n' ' ')"
else fail "installer"; cat "$WORK/install.log"; fi
v="$(lap /root/.local/bin/csa-client version 2>/dev/null | head -1)"
[ "$v" = "$IMG_VERSION" ] && pass "the installed csa-client is the image's own build ($v)" || fail "installed version: $v, image: $IMG_VERSION"
lap sh -c '/root/.local/bin/csa-client setup --server https://csa --yes --no-browser > /root/setup.out 2>&1; echo $? > /root/setup.rc' &
SETUP_PID=$!
n=""; c=""
for _ in $(seq 1 120); do
  n="$(lap sh -c "grep -oE 'nonce=[A-Za-z0-9_-]+' /root/setup.out 2>/dev/null | head -1 | cut -d= -f2")"
  c="$(lap sh -c "grep -oE 'Verification code: [A-Z-]+' /root/setup.out 2>/dev/null | head -1 | sed 's/.*: //'")"
  [ -n "$n" ] && [ -n "$c" ] && break
  sleep 0.5
done
[ -n "$n" ] && [ -n "$c" ] && pass "setup printed its authorize link (nonce) and verification code" || { fail "setup never printed a nonce"; lap cat /root/setup.out; }
r="$(lcurl -H 'Content-Type: application/json' -d "{\"nonce\":\"$n\",\"code\":\"$c\"}" https://csa/api/cli-auth/approve)"
printf '%s' "$r" | grep -q '"ok": true' && pass "the signed-in browser session approved the pairing (no invite, no typed email)" || fail "approve: $r"
wait $SETUP_PID || true
[ "$(lap cat /root/setup.rc)" = 0 ] && pass "csa-client setup exited 0" || { fail "setup exited $(lap cat /root/setup.rc)"; lap cat /root/setup.out; }
lap grep -q "Authorized as alice@acme.test" /root/setup.out && pass "setup: Authorized as alice@acme.test (the idp's identity, nothing typed)" || fail "setup identity"
lap grep -q "https://csa/mcp" /root/setup.out && pass "setup registered (printed) the MCP server at https://csa/mcp" || fail "setup MCP url"
TOK="$(lap sh -c "grep -o '\"auth_token\": *\"[^\"]*\"' /root/.csa/config.json | cut -d'\"' -f4")"
[ -n "$TOK" ] && pass "~/.csa/config.json holds the per-user token" || fail "no token in config.json"
hooks="$(lap sh -c "grep -o 'csa-client' /root/.claude/settings.json | wc -l | tr -d ' '")"
[ "$hooks" -ge 3 ] && pass "~/.claude/settings.json carries the three hooks ($hooks csa-client entries)" || fail "hooks in settings.json: $hooks"

ship(){ # SID TOKENWORD
  lap sh -c "mkdir -p /root/.claude/projects/-work-repo && printf '%s\n%s\n' \
    '{\"timestamp\":\"2026-09-14T10:00:00Z\",\"cwd\":\"/work/repo\",\"message\":{\"role\":\"user\",\"content\":\"planning\"}}' \
    '{\"timestamp\":\"2026-09-14T10:00:01Z\",\"cwd\":\"/work/repo\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"Decision FACTCLAIM=$2 for the payments-service\"}]}}' \
    > /root/.claude/projects/-work-repo/$1.jsonl"
  lap sh -c "printf '{\"session_id\":\"$1\",\"transcript_path\":\"/root/.claude/projects/-work-repo/$1.jsonl\"}' | /root/.local/bin/csa-client ship"
}
ship e2e-s1 zorptastic; ship e2e-s2 quibblenaut; ship e2e-s3 wafflecrest
queued=0
for _ in $(seq 1 120); do
  n_pending="$(docker exec "$P-api-1" sh -c 'ls /app/data/pending 2>/dev/null' | grep -c 'e2e-s' || true)"
  n_done="$(docker exec "$P-api-1" sh -c 'ls /app/data/extracted 2>/dev/null' | grep -c 'e2e-s' || true)"
  [ $((n_pending + n_done)) -ge 3 ] && { queued=1; break; }
  sleep 1
done
[ "$queued" = 1 ] && pass "three sessions shipped by csa-client reached data/pending (or were already drained)" \
  || { fail "sessions never queued"; lap cat /root/.csa/ship.log 2>/dev/null | tail -5; }

section "drain through the stub, recall through the proxy (HTTP and MCP)"
# strict=false is a deliberate ask; a miss echoes the query, so a recall is proven by found=true
ask(){ lap curl -sS -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
         -d "{\"query\":\"$1\",\"strict\":false}" https://csa/api/ask; }
recalled(){ case "$2" in *'"found": true'*"$1"*) true ;; *) false ;; esac; }
# the etl drains one session at a time, so each fact gets its own wait
for w in zorptastic quibblenaut wafflecrest; do
  for _ in $(seq 1 180); do
    r="$(ask "$w")"
    recalled "$w" "$r" && break
    sleep 2
  done
  recalled "$w" "$r" && pass "etl drained through the stub; /api/ask through the proxy recalls $w" \
    || { fail "/api/ask never found $w: $(printf '%s' "$r" | cut -c1-160)"; compose logs etl | tail -20; docker logs "$P-stub" | tail -5; }
done
MCPH=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "Authorization: Bearer $TOK")
lap curl -sS -D /root/mcp.hdr -o /root/mcp.init "${MCPH[@]}" --max-time 30 \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"onprem-e2e","version":"1"}}}' https://csa/mcp
SID="$(lap sh -c "grep -i '^mcp-session-id:' /root/mcp.hdr | awk '{print \$2}' | tr -d '\r'")"
if [ -n "$SID" ]; then
  lap curl -sS -o /dev/null "${MCPH[@]}" -H "mcp-session-id: $SID" --max-time 30 -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' https://csa/mcp || true
  r="$(lap curl -sS "${MCPH[@]}" -H "mcp-session-id: $SID" --max-time 60 -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ask","arguments":{"query":"zorptastic"}}}' https://csa/mcp)"
  case "$r" in *"Recalled context for"*zorptastic*) true ;; *) false ;; esac && pass "MCP ask through https://csa/mcp (initialize + tools/call over the proxy's streaming route) recalls zorptastic" \
    || fail "MCP ask: $(printf '%s' "$r" | cut -c1-200)"
else fail "MCP initialize returned no session id: $(lap cat /root/mcp.hdr | tr '\n' ' ' | cut -c1-200)"; fi
r="$(lap curl -sS -o /dev/null -w '%{http_code}' "${MCPH[@]:0:4}" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' https://csa/mcp)"
[ "$r" = 401 ] && pass "MCP without a token -> 401 through the proxy" || fail "MCP without a token -> $r"

section "csa-client update from /latest"
r="$(lap /root/.local/bin/csa-client update --yes 2>&1 || true)"
printf '%s' "$r" | grep -q "already up to date ($IMG_VERSION)" && pass "csa-client update against its own server: already up to date ($IMG_VERSION)" || fail "update on the image's build: $r"
(cd "$ROOT/client" && CGO_ENABLED=0 GOOS=linux GOARCH="$DAEMON_ARCH" \
   go build -trimpath -ldflags="-s -w -X main.version=v0.1.e2e-2" -o "$WORK/client-dist/$CLIENT_ASSET" .) \
  && (cd "$WORK/client-dist" && { command -v sha256sum >/dev/null && sha256sum csa-client-* || shasum -a 256 csa-client-*; } > checksums.txt) \
  && compose cp "$WORK/client-dist/." api:/app/client-dist/ >/dev/null \
  && pass "published a second build (v0.1.e2e-2) into the api's /app/client-dist + checksums.txt (no restart)" || fail "second build"
r="$(lap /root/.local/bin/csa-client update --yes 2>&1 || true)"
printf '%s' "$r" | grep -q "installed to" && pass "csa-client update --yes: $(printf '%s' "$r" | grep -o 'installed to [^ ]*')" || fail "update: $r"
v="$(lap /root/.local/bin/csa-client version 2>/dev/null | head -1)"
[ "$v" = "v0.1.e2e-2" ] && pass "csa-client version = $v after the update" || fail "version after update: $v"

# --- egress -----------------------------------------------------------------------------------------------------------
section "egress observed"
sleep 2
for s in migrate api mcp etl proxy; do compose logs --no-log-prefix "$s" > "$WORK/$s.log" 2>&1; done
docker logs "$P-sinkhole" > "$WORK/sinkhole.log" 2>&1
docker logs "$P-stub"     > "$WORK/stub.log" 2>&1
docker logs "$P-idp"      > "$WORK/idp.log" 2>&1
docker logs "$P-sniff"    > "$WORK/sniff.log" 2>&1
names="$(awk '/^Q /{print $3}' "$WORK/sinkhole.log" | sort -u)"
bad_names="$(printf '%s\n' "$names" | grep -v -x -e csa -e stub -e idp -e api -e mcp -e etl -e proxy -e migrate | grep -v '^$' || true)"
echo "  names asked at the sinkhole: $(printf '%s' "$names" | tr '\n' ' ')"
if [ -z "$bad_names" ]; then pass "the sinkhole saw only csa, stub, idp (and compose service names)"
else
  fail "the sinkhole saw: $(printf '%s' "$bad_names" | tr '\n' ' ')"
  printf '%s\n' "$bad_names" | grep -E "$FORBIDDEN" | sed 's/^/    FORBIDDEN: /' || true
fi
syn_out="$(awk -v p="$NETP." '/^SYN / && index($3, p) != 1 {print $3}' "$WORK/sniff.log" | sort -u)"
udp_out="$(awk -v p="$NETP." '/^UDP / && index($3, p) != 1 {print $3}' "$WORK/sniff.log" | sort -u)"
v6="$(grep '^IPV6 ' "$WORK/sniff.log" | sort -u || true)"
syn_in="$(awk '/^SYN /{print $3}' "$WORK/sniff.log" | sort -u | tr '\n' ' ')"
echo "  SYN destinations on the network: $syn_in"
if [ -z "$syn_out" ] && [ -z "$udp_out" ] && [ -z "$v6" ]; then pass "every SYN and datagram stayed inside $SUBNET"
else fail "packets left for another address: syn=[$syn_out] udp=[$udp_out] v6=[$v6]"; fi
n_extract=0
for _ in $(seq 1 120); do
  docker logs "$P-stub" > "$WORK/stub.log" 2>&1
  n_extract="$(grep -c 'tag=extract' "$WORK/stub.log" || true)"
  [ "$n_extract" -ge 3 ] && break
  echo "  waiting for the etl to finish extracting: $n_extract of 3 calls seen"
  sleep 2
done
stub_models="$(grep '^REQ POST /v1/messages' "$WORK/stub.log" | grep -o 'model=[^ ]*' | sort -u | sed 's/^model=//' | tr '\n' ' ' || true)"
[ "$n_extract" -ge 3 ] && pass "the stub served $n_extract extraction calls; FULL MODEL IDS SENT: $stub_models" || { fail "stub saw $n_extract extraction calls"; compose logs --no-log-prefix etl | tail -20; }
if grep 'tag=extract' "$WORK/stub.log" | grep -q -v 'tools=0 mcp_servers=0'; then fail "an extraction call carried tools or MCP servers"
else pass "extraction calls carried tools=0 mcp_servers=0"; fi
want_ids="$(compose exec -T api /app/.venv/bin/python -c "from analyzer import llm; print('|'.join(llm._API_MODELS.values()))" | tr -d '\r')"
if printf '%s' "$stub_models" | tr ' ' '\n' | grep -v '^$' | grep -q -v -E "^($want_ids)$"; then fail "a model id llm._API_MODELS does not name reached the stub: $stub_models"
else pass "every model id sent is one llm._API_MODELS names (no bare alias on the Messages API)"; fi
grep -q 'LLM provider: Claude via an Anthropic API key from the environment' "$WORK/api.log" \
  && pass "the api logged what it runs on: $(grep -m1 -o 'LLM provider: .*' "$WORK/api.log")" || fail "api log: $(grep -m1 'LLM provider' "$WORK/api.log")"
grep -q '^UNKNOWN ' "$WORK/stub.log" && fail "the CLI asked the stub for: $(grep '^UNKNOWN ' "$WORK/stub.log" | tr '\n' ' ')" || true

# --- the ledgers subtask E reads -----------------------------------------------------------------------------------
{
  echo "# hosts seen by the DNS sinkhole (every name any container or the laptop asked for)"
  printf '%s\n' "$names"
  echo "# SYN destinations on the internal network (bridge sniffer): src dst:port, unique"
  awk '/^SYN /{print $2" "$3}' "$WORK/sniff.log" | sort -u
  echo "# UDP destinations: src dst:port, unique"
  awk '/^UDP /{print $2" "$3}' "$WORK/sniff.log" | sort -u
  echo "# requests at the model stub"
  grep '^REQ ' "$WORK/stub.log" | sed 's/ tag=.*//' | sort | uniq -c
  echo "# requests at the identity provider"
  grep '^REQ ' "$WORK/idp.log" | awk '{print $2, $3}' | sort | uniq -c
} > "$WORK/hosts.txt"
{
  echo "# files under the box's data/ after boot, adopt-default, sso set, one sign-in, one paired laptop, three sessions drained"
  docker exec "$P-api-1" sh -c 'cd /app && find data -type f | sort'
  echo "# ownership under data/ (the runbook's chown to 10001, checked on a native filesystem)"
  docker exec "$P-api-1" sh -c 'ls -ln /app/data | tail -n +2'
  echo "# files under the proxy's /data (Caddy): the internal CA and the certificate it issued"
  docker exec "$P-proxy-1" sh -c 'find /data -type f | sort' 
  echo "# files on the laptop under ~/.csa, ~/.claude and ~/.local after install, setup, ship and update"
  lap sh -c 'find /root/.csa /root/.claude /root/.claude.json /root/.local -type f 2>/dev/null | sort'
} > "$WORK/files.txt"
printf "\n${C_B}ledgers for subtask E:${C_0}\n  %s\n  %s\n" "$WORK/hosts.txt" "$WORK/files.txt"
sed 's/^/    /' "$WORK/hosts.txt"
sed 's/^/    /' "$WORK/files.txt"

printf "\n${C_B}%d passed, %d failed, %d skipped${C_0}\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" = 0 ]
