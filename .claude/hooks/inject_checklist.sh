#!/bin/bash
# SessionStart hook: add the region between the session-start markers of docs/development/checklist.md
# to the session context. Prints nothing when the markers are absent. Must stay fast.
set -u

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
HOOK_CWD=$(printf '%s' "$input" | jq -r '.cwd // empty')
ROOT=""
[ -d "$HOOK_CWD" ] && ROOT=$(cd "$HOOK_CWD" && git rev-parse --show-toplevel 2>/dev/null || true)
[ -n "$ROOT" ] || ROOT="${CLAUDE_PROJECT_DIR:-}"
DOC="$ROOT/docs/development/checklist.md"
[ -f "$DOC" ] || exit 0

awk '
  /<!-- session-start:begin -->/ { grab=1; next }
  /<!-- session-start:end -->/   { grab=0 }
  grab { print }
' "$DOC"

exit 0
