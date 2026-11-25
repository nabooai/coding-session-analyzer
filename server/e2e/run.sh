#!/usr/bin/env bash
# Run one E2E harness by name, or every harness that needs no docker when no name is given.
# Stdin is /dev/null: the harnesses are non-interactive, and a client hook left reading an open stdin hangs.
#   server/e2e/run.sh                 org, onboarding, landing, trial, plugin
#   server/e2e/run.sh onprem          one harness; extra arguments and env pass through
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMETIC=(org onboarding landing trial plugin)
ALL=(org onboarding landing trial plugin onprem onprem-egress)

if [ $# -gt 0 ]; then
  name="$1"; shift
  [ -f "$HERE/$name.sh" ] || { echo "unknown harness '$name'; one of: ${ALL[*]}" >&2; exit 2; }
  exec bash "$HERE/$name.sh" "$@" </dev/null
fi

failed=()
for name in "${HERMETIC[@]}"; do
  printf '\n\033[1m######## %s ########\033[0m\n' "$name"
  bash "$HERE/$name.sh" </dev/null || failed+=("$name")
done
[ ${#failed[@]} -eq 0 ] && { printf '\nE2E: %s green\n' "${HERMETIC[*]}"; exit 0; }
printf '\nE2E: red: %s\n' "${failed[*]}" >&2
exit 1
