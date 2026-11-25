---
description: Delete sessions you sent from the CSA server, with the facts drawn from them.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py forget:*) Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py sent:*)
---

The user ran `/csa:forget` with: $ARGUMENTS

With no ids, run `python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py sent`, show it, and ask which to delete. With ids, list them back. If there is more than one, get a plain yes first. Then run:

```
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py forget <id> [<id>...]
```

Deletion is permanent. Show the output verbatim.
