---
description: Connect this machine to a CSA server, automatically or with --manual for only the sessions you choose.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py connect:*)
---

The user ran `/csa:connect` with: $ARGUMENTS

Run this one command, with their server URL as one quoted argument, and `--manual` only if they wrote it. If they gave no URL, it reuses the one already configured.

```
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py connect '<url>' [--manual]
```

Show the output verbatim, including the verification code. Then stop. Do not open any URL, read anything under `~/.csa`, or retry. The person's click on Authorize in their browser is their consent, and it is theirs to give.
