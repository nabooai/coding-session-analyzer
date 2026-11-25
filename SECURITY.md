# Security

## Reporting a vulnerability

Report it privately through GitHub: the **Security** tab of this repository, then **Report a
vulnerability**. Do not open a public issue or pull request for it.

Include what an attacker needs (network position, credentials, role), the steps to reproduce, and the
version or commit you tested. We acknowledge a report within three working days and tell you what we
will do about it. Fixes ship in a new release; only the latest release is supported.

In scope: the server (`server/`), the web UI, the client (`client/`), the Helm chart, the compose
files and the release artifacts. Most useful to us: a read across workspaces, a way past the auth gate
or a role check, a token or secret that leaks, a transcript stored with weaker redaction than the
workspace floor, and anything that makes a laptop send something the consent notice did not name.

## What leaves a laptop

The client, `csa-client`, talks to one host: the CSA server it was set up with. It sends no telemetry
and calls no model. After pairing, every call carries a per-user token.

| When | What is sent |
|---|---|
| A Claude Code session ends | That session's transcript, redacted on the laptop at the mode the person chose (never below `secrets`, never below the workspace floor), plus the owner's identity. The working directory appears inside the transcript lines. |
| Each prompt | The prompt, redacted in `secrets` mode, for recall. Not sent when it has fewer than two content words or a secret survives redaction. |
| Session start | A request for server directives, and a list of sessions a crash left unshipped. |
| `setup`, `doctor`, `forget`, `update` | Health and pairing calls, the session ids to delete, the release checksums. |

Nothing ships before the person accepts a consent notice that names the server, the mode and what is
kept. Every call, with the code that makes it, is in
[docs/public-release/egress.md](docs/public-release/egress.md#laptops).

[`csa-plugin/`](csa-plugin/README.md) is the same client as readable Python for Claude Code, with no compiled artifact.
Its consent is the Authorize click on the server's `/cli-auth` page, which states what ships and for how long. It installs
disabled, has one network call site, contains no self-update, and has a manual mode in which nothing leaves until
the person runs `/csa:send-session`. Its redactor is a byte copy of the server's, enforced in CI. A skill's commands
cannot be started by the model, though nothing stops a model in bypass-permissions mode from running the script.

`csa-client uninstall` and `/csa:disconnect` revoke the machine's credential on the server, through `POST
/api/token/revoke`. The binary `curl <server>/install | sh` installs is not signed by Apple and carries no build
attestation. Its checksum comes from the same server, so it catches a corrupted download and not a tampered one.

## Redaction

There are three modes, each stricter than the last: `secrets` (keys, tokens and the value of
`password=...` style assignments, the default), `privacy` (adds emails, connection strings, more
credential assignments, internal hostnames) and `strict`
(adds a deny list of names, codenames, customers and hosts). The client redacts before sending; the
server redacts again at the workspace's minimum mode before anything is stored. The two
implementations are tested against one shared fixture.

Redaction is pattern matching. It does not catch a coworker's name, a project codename or a secret
written as prose unless they are on the deny list, so redacted text is not safe to share by itself.
Details and known gaps: [docs/redaction.md](docs/redaction.md).

## What the server sends, and keeps

The server talks to the model endpoint you configure (the redacted transcript at extraction, facts at
judging, a question and its matching facts when the `ask` tool plans a search), your identity
provider at sign-in, and nothing else unless you turn on trace export or a workspace-request webhook.
Recall on a prompt makes no model call. See
[docs/public-release/egress.md](docs/public-release/egress.md).

A question asked through `/api/ask` or the MCP `ask` tool is redacted at the workspace floor before it
is searched, logged, traced or sent to a model.

Recall never serves a fact a reviewer flagged `wrong` (or `noise`, `rejected`, `already-documented`), nor
a superseded or refuted one: not the prompt hook, not any tier of `/api/ask`, not a lookup on the MCP
`ask` tool. A flag is not deletion. The fact stays readable by its id (the MCP `fact` tool, the web
UI), with its label shown, to every member of the workspace, and a relational question on the MCP
`ask` tool runs a model-written read-only query over the whole corpus whose result table is not
filtered.

That is what the server does, not something it enforces. Under Docker Compose nothing limits egress.
The Helm chart limits it only with `networkPolicy.enabled=true`, and its default policy still allows
any address on port 443. To allow only your model gateway and identity provider, see
[Hardening](docs/public-release/install-kubernetes.md#hardening).

Model provider keys added on **AI Models**, and an SSO client secret, are stored in plaintext in
`control.db` on the data volume, and every backup carries them: protect the volume and the backups like
the keys themselves.

Facts are kept until the person who shipped the session or a workspace admin deletes them
(`csa-client forget`, or `POST /api/forget` with a bearer token; a browser session cannot delete).
Transcripts are kept only when a workspace turns on retention (`archive_days`). Deleting a session
removes its facts, their quotes, the extraction cache, any queued copy and the retained transcript. It
does not reach backups, logs, model-call costs, or the record of questions asked.

The record of questions asked is visible inside the workspace: the text of every answered question,
and the session it came from, is shown to every member on the recall log (`GET /api/sessions`),
without the asker's name. A question that found nothing is shown only to its asker.
Every file the server and the client write, who can read it and what deletes it is in
[docs/public-release/data-at-rest.md](docs/public-release/data-at-rest.md).

## Isolation

Each workspace has its own database files, and every credential belongs to one workspace, so a request
can only open its own workspace's files. Whoever runs the server has the files on disk; isolation is a
property of the API, not of the host. Limits of the audit log and other known ceilings are listed in
[docs/public-release/org-admin.md](docs/public-release/org-admin.md#known-ceilings-in-v1-say-these-out-loud-to-customers).
