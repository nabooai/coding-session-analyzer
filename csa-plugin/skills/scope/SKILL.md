---
description: Show or change which project folders CSA may ship: only (allowlist) and ignore (blocklist).
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py scope:*)
---

The user ran `/csa:scope` with: $ARGUMENTS

With no arguments, run `python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py scope`. To change a list, quote the glob as one argument:

```
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py scope only '<glob>'
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py scope ignore '<glob>'
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py scope clear only
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py scope clear ignore
```

Globs match the folder names `scope` prints, and `*` does not cross `/`. Show the output verbatim.
