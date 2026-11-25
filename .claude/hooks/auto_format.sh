#!/bin/bash
# PostToolUse hook on Edit|MultiEdit|Write: lint-autofix the one file the agent just touched.
# ruff check --fix (never ruff format), eslint --fix, gofmt -w. No-op when the tool is absent.
set -u

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
FILE=$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.filePath // empty')
HOOK_CWD=$(printf '%s' "$input" | jq -r '.cwd // empty')

# Sessions run in git worktrees: resolve the root from the hook's cwd, not from a fixed path.
ROOT=""
[ -d "$HOOK_CWD" ] && ROOT=$(cd "$HOOK_CWD" && git rev-parse --show-toplevel 2>/dev/null || true)
[ -n "$ROOT" ] || ROOT="${CLAUDE_PROJECT_DIR:-}"
[ -n "$ROOT" ] || exit 0

case "$FILE" in
  "") exit 0 ;;
  /*) ;;
  *) FILE="${HOOK_CWD:-$ROOT}/$FILE" ;;
esac
[ -f "$FILE" ] || exit 0

case "$FILE" in
  "$ROOT"/server/ui/src/*.ts)
    command -v npx >/dev/null 2>&1 || exit 0
    (cd "$ROOT/server/ui" && npx --no-install eslint --fix --quiet "$FILE") >/dev/null 2>&1
    ;;
  "$ROOT"/server/*.py)
    command -v uv >/dev/null 2>&1 || exit 0
    uv run --frozen --project "$ROOT/server" ruff check --fix --quiet "$FILE" >/dev/null 2>&1
    ;;
  *.go)
    command -v gofmt >/dev/null 2>&1 || exit 0
    gofmt -w "$FILE"
    ;;
esac

exit 0
