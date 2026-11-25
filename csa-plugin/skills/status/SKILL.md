---
description: Show whether CSA is connected, how, the last sends, and the one thing to fix if anything is wrong.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py status:*)
---

!`python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py status`

Show the output above verbatim. Do nothing else.
