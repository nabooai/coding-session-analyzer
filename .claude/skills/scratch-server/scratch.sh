#!/bin/bash
# Boot a throwaway multi-tenant CSA server with seeded data. Usage: scratch.sh <box-dir> [port]
# Deletes and recreates <box-dir>. Never touches production.
set -eu
BOX=${1:?box dir}; PORT=${2:-8794}
ROOT=$(git rev-parse --show-toplevel)
SRV=$ROOT/server; PY=$SRV/.venv/bin/python
[ -x "$PY" ] || (cd "$SRV" && uv sync --quiet)
[ -f "$SRV/ui/dist/index.html" ] || (cd "$SRV/ui" && npm ci --silent && npm run build --silent)
pkill -f "$SRV/apiserver.py" 2>/dev/null || true
rm -rf "$BOX"; mkdir -p "$BOX/data"
export PYTHONPATH=$SRV CSA_AUTH=org CSA_AUTH_TOKEN=scratch-shared-secret
cd "$BOX"
INV1=$("$PY" "$SRV/csa.py" org create "Demo Co" 2>&1 | grep -o 'inv_[A-Za-z0-9_-]*')
INV2=$("$PY" "$SRV/csa.py" org create "Other Co" 2>&1 | grep -o 'inv_[A-Za-z0-9_-]*')
"$PY" "$ROOT/.claude/skills/scratch-server/seed.py"
for org in demo-co other-co; do "$PY" "$SRV/csa.py" org config $org --set autojudge=off >/dev/null; done   # no model spend
CSA_API_PORT=$PORT nohup "$PY" "$SRV/apiserver.py" > "$BOX/server.log" 2>&1 &
for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 0.25; done
curl -sf "http://127.0.0.1:$PORT/api/auth-config" >/dev/null || { echo "server did not come up; see $BOX/server.log" >&2; exit 1; }
cat <<EOF
scratch server: http://127.0.0.1:$PORT   log: $BOX/server.log
demo-co   invite $INV1   people mara@demo.test (admin), jules@demo.test, tomasz@demo.test
other-co  invite $INV2   people zed@other.test (corpus contains OTHERCO CONFIDENTIAL)
stop: pkill -f "$SRV/apiserver.py"
EOF
