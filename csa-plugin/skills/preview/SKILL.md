---
description: Write the exact redacted text CSA would send for a session to a local file, with its sha256 and label counts. Nothing is sent.
disable-model-invocation: true
allowed-tools: Bash(python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py preview:*)
---

The user ran `/csa:preview` with: $ARGUMENTS

Run this one command. If they named a session id use it; otherwise use `${CLAUDE_SESSION_ID}`.

```
python3 -I -S -B ${CLAUDE_PLUGIN_ROOT}/scripts/csa.py preview --session <id>
```

Show the output verbatim. It holds counts and a file path. Do not open or read the file: it is for the person to read.
