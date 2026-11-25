#!/usr/bin/env bash
# Smoke test a built server image: runs as uid 10001, carries the CLI its label names (with-claude-code) or no trace
# of it (slim), the venv imports the package, /app/VERSION and the version label match, the UI is baked, the api
# serves all five client binaries at /latest/ matching its checksums.txt and stamped with the image's version, and
# there is no Node or curl to drift on.
#   bash server/image-smoke.sh <image> <expected CSA_VERSION> <slim|with-claude-code>
set -euo pipefail
IMAGE="$1"; VERSION="$2"; TARGET="$3"

uid="$(docker run --rm "$IMAGE" id -u)"
[ "$uid" = "10001" ] || { echo "id -u is $uid, want 10001" >&2; exit 1; }

want_cli="$(docker inspect --format '{{index .Config.Labels "com.csa.claude_code"}}' "$IMAGE")"
opt_outs="$(docker run --rm "$IMAGE" sh -c 'env | grep -c "^CLAUDE_CODE_DISABLE_\|^DISABLE_" || true')"
case "$TARGET" in
  with-claude-code)
    cli="$(docker run --rm "$IMAGE" claude --version)"
    case "$cli" in "$want_cli "*|"$want_cli") ;; *) echo "claude --version is '$cli', label says $want_cli" >&2; exit 1 ;; esac
    [ "$opt_outs" = 11 ] || { echo "$opt_outs of the 11 opt-out variables are set" >&2; exit 1; }
    ;;
  slim)
    cli=none
    case "$want_cli" in ""|"<no value>") ;; *) echo "a slim image labelled com.csa.claude_code=$want_cli" >&2; exit 1 ;; esac
    if docker run --rm "$IMAGE" sh -c 'command -v claude'; then echo "a slim image with claude on the PATH" >&2; exit 1; fi
    found="$(docker run --rm --user 0 "$IMAGE" sh -c 'find / -xdev \( -path /proc -o -path /sys \) -prune -o -type f \( -name "claude" -o -name "claude-code-linux-*" \) -print')"
    [ -z "$found" ] || { echo "a slim image carrying: $found" >&2; exit 1; }
    [ "$opt_outs" = 0 ] || { echo "a slim image with $opt_outs Claude Code opt-out variables set" >&2; exit 1; }
    ;;
  *) echo "the third argument is slim or with-claude-code, not '$TARGET'" >&2; exit 2 ;;
esac

docker run --rm "$IMAGE" /app/.venv/bin/python -c "import analyzer"

ver="$(docker run --rm "$IMAGE" /app/.venv/bin/python -c "print(open('/app/VERSION').read().strip())")"
[ "$ver" = "$VERSION" ] || { echo "/app/VERSION is '$ver', want $VERSION" >&2; exit 1; }
label="$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")"
[ "$label" = "$VERSION" ] || { echo "version label is '$label', want $VERSION" >&2; exit 1; }

docker run --rm "$IMAGE" grep -q 'id="app"' /app/ui/dist/index.html

docker run --rm -e VERSION="$VERSION" "$IMAGE" sh -c '/app/.venv/bin/python apiserver.py >/tmp/api.log 2>&1 &
exec /app/.venv/bin/python - <<"PY"
import hashlib, os, platform, subprocess, sys, time, urllib.request
base = "http://127.0.0.1:8766"
for _ in range(60):
    try:
        urllib.request.urlopen(base + "/api/health", timeout=2); break
    except Exception:
        time.sleep(0.5)
else:
    sys.exit("the api never answered /api/health:\n" + open("/tmp/api.log").read())
get = lambda name: urllib.request.urlopen(base + "/latest/" + name, timeout=10).read()
sums = {name: digest for digest, name in (line.split() for line in get("checksums.txt").decode().splitlines())}
want = {f"csa-client-{t}" for t in ("darwin-arm64", "darwin-amd64", "linux-amd64", "linux-arm64", "windows-amd64.exe")}
if set(sums) != want:
    sys.exit(f"checksums.txt lists {sorted(sums)}, want {sorted(want)}")
for name, digest in sums.items():
    if hashlib.sha256(get(name)).hexdigest() != digest:
        sys.exit(f"/latest/{name} does not match checksums.txt")
arch = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
got = subprocess.run([f"/app/client-dist/csa-client-linux-{arch}", "version"], capture_output=True, text=True).stdout.strip()
if got != os.environ["VERSION"]:
    sys.exit(f"csa-client-linux-{arch} version is {got!r}, want {os.environ['VERSION']!r}")
PY'

if docker run --rm "$IMAGE" sh -c 'command -v node || command -v npm || command -v curl' >/dev/null; then
  echo "image carries node, npm or curl; the runtime stage must not" >&2; exit 1
fi

echo "smoke OK: $IMAGE uid=$uid claude=$cli version=$ver client=5 binaries at /latest/"
