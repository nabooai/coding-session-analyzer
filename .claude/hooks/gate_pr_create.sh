#!/bin/bash
# PreToolUse hook on Bash: block `gh pr create` while lint or tests on the branch's changes are red.
# Exit 2 + stderr blocks the call; exit 0 allows. An absent tool skips its check.
set -u

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
CMD=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
case "$CMD" in
  *"gh pr create"*) ;;
  *) exit 0 ;;
esac

# Gate the checkout the agent is in (a worktree), not a fixed path.
HOOK_CWD=$(printf '%s' "$input" | jq -r '.cwd // empty')
ROOT=""
[ -d "$HOOK_CWD" ] && ROOT=$(cd "$HOOK_CWD" && git rev-parse --show-toplevel 2>/dev/null || true)
[ -n "$ROOT" ] || ROOT="${CLAUDE_PROJECT_DIR:-}"
[ -n "$ROOT" ] || exit 0
cd "$ROOT" || exit 0

fail() {
  echo "gate_pr_create: $1" >&2
  echo "Fix it, then retry the PR." >&2
  exit 2
}

# run <label> <dir> <cmd...>: run from <dir>; on non-zero exit, show the output and block.
run() {
  label=$1; dir=$2; shift 2
  command -v "$1" >/dev/null 2>&1 || return 0
  echo "gate_pr_create: $label" >&2
  if ! out=$(cd "$dir" && "$@" 2>&1); then
    printf '%s\n' "$out" >&2
    fail "$label failed"
  fi
}

BASE=$(git merge-base HEAD origin/master 2>/dev/null || true)
[ -n "$BASE" ] || exit 0
# Working tree vs merge-base: covers committed and not-yet-committed edits. Paths with spaces unsupported.
CHANGED=$(git diff --name-only --diff-filter=ACMR "$BASE" 2>/dev/null || true)
[ -n "$CHANGED" ] || exit 0

PY=$(printf '%s\n' "$CHANGED" | grep -E '^server/.*\.py$' | sed 's|^server/||' || true)
TS=$(printf '%s\n' "$CHANGED" | grep -E '^server/ui/.*\.ts$' || true)
GO=$(printf '%s\n' "$CHANGED" | grep -E '^client/.*\.go$' | sed 's|^client/||' || true)

if [ -n "$PY" ]; then
  # shellcheck disable=SC2086  # word splitting passes the file list
  run "ruff" server uv run --frozen ruff check $PY
  run "pytest" server uv run --frozen python -m pytest tests/ -q -x
fi

if [ -n "$TS" ]; then
  run "npm run check" server/ui npm run check
fi

if [ -n "$GO" ]; then
  if command -v gofmt >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    unformatted=$(cd client && gofmt -l $GO)
    [ -z "$unformatted" ] || fail "gofmt wants to reformat: $(printf '%s' "$unformatted" | tr '\n' ' ')"
  fi
  run "go vet" client go vet ./...
  run "go test" client go test ./...
fi

exit 0
