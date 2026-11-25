---
description: Revoke this machine's CSA credential, remove the MCP entry and delete ~/.csa.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py disconnect:*)
---

!`python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py disconnect`

Show the output above verbatim. Do nothing else.
