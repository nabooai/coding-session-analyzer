---
description: List who ships to this CSA server, with session counts.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py team:*)
---

!`python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py team`

Show the output above verbatim. Do nothing else.
