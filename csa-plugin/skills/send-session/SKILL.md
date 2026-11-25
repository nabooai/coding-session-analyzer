---
description: Send one session to the CSA server through the same redaction. The human running this is the decision to send.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py send-session:*)
---

The user ran `/csa:send-session` with: $ARGUMENTS

If they asked which sessions there are, or wrote `list`, run `python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py send-session --list`, show it, and stop.

Otherwise run this one command. If they named a session id use it; otherwise use `${CLAUDE_SESSION_ID}`.

```
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py send-session --session <id>
```

Show the output verbatim, then stop. Never run this unless the user invoked this command in this message.
