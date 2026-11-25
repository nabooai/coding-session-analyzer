# Privacy and sharing

CSA is designed around explicit consent and project scope, not silent collection.

## What leaves the machine

In **automatic mode**:

1. A locally redacted prompt goes to your CSA server for recall.
2. A locally redacted completed session goes to the server for extraction.
3. The server sends configured content to the model provider selected by the workspace.

In **manual mode**, prompts and sessions remain local until you explicitly send a session. The plugin does
not backfill old sessions by default and sends no product telemetry.

## Controls

- Inspect the exact payload with `/csa:preview`.
- Restrict projects with `/csa:scope only ...` or `/csa:scope ignore ...`.
- Add local deny/allow rules for organization-specific values.
- Review the local send ledger with `/csa:sent`.
- Delete a session and derived knowledge with `/csa:forget` while connected.
- Revoke the per-user credential with `/csa:disconnect`.

Redaction detects common secret formats; it cannot infer every name, business fact, or secret written as prose.
Treat project scope and preview as primary controls. A connected user's identity is derived from the server token,
not from a client-supplied email.

Workspace operators control transcript retention, provider access, backups, members, and tokens. Publish those
policies to users before asking them to authorize sharing.
