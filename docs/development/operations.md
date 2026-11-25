# Lesson: operating traps

Traps that only show up on a running deployment. Each one cost a real outage or a lost day. Read
before changing a boot check, the compose files, the client's hooks or its retry loop.

## A boot-time invariant is a landmine

Some checks run only when the server starts: the server refuses to boot with an SSO row and no org
domain, and an older auth mode refused to boot once any org row existed. Changing the state such a
check reads does nothing visible on a running box; it fails on the next restart, hours later, and
looks unrelated to whoever caused it. Rehearse with `server/rehearse_tenancy.py` against a scratch
box before deploying, and never create a throwaway org on a box you cannot recreate.

## `ui/dist` is replaced by contents, never by rename

When `ui/dist` is bind-mounted into the container, Docker binds the directory's inode. Moving or
recreating the directory keeps serving the old bundle with no error. Replace the files inside it.
A UI that needs a new route ships after the image that serves the route, never before.

## A plain build is not prod's image

A plain `docker build -f server/Dockerfile` has no Claude Code. A deployment on a subscription token, Bedrock or
Vertex needs `--target with-claude-code`, which is what the `csa-server` package holds. On the plain image those
paths park the queue with a sentence saying so; nothing is lost, but nothing is read either.

## A machine can recall and never ship

`csa-client setup` and `csa-client init` wire all three Claude Code hooks (SessionStart,
SessionEnd, UserPromptSubmit). Only `init --install --ambient` wires the recall hook alone, because
that machine ships through its login watcher. A machine with the recall hook and no watcher looks
healthy from the inside, since recall keeps working, and ships nothing. Before late 2026-10 `init`
left exactly that shape (or nothing at all) unless `--install` was passed. `csa-client doctor` now
ends with `NOT SHIPPING YET` and the command to run when neither a SessionEnd hook nor a watcher is
present.

Re-running `setup` stamps `sweep_from=now` and drops older unshipped transcripts. Check the token's
`last_used_at` and the hook log before asking someone to re-run it.

## A dead server must not burn the client's CPU

The client once gated retries on the last success, so an unreachable server meant a retry on every
tick: 25 hours of CPU in 44 hours across three watchers. The circuit breaker is whole-pipeline
(`send()` returns `(ok, down)`, `client/main.go`). Keep any new network call behind it.

## Turning on transcript archiving changes consent

`tenant_config.archive_days` (a number of days, or `forever`) is read on every request by api, mcp
and etl. Turning it on pauses every laptop whose recorded consent predates it until the person
re-consents. Read [`org-admin.md`](../public-release/org-admin.md) before changing it.

## Per-session metrics cannot be backfilled

`session_metrics` is computed at ingest from the raw transcript, the only place raw usage exists. A
session ingested before a metric existed has no transcript on the server to recompute from; only a
re-ship from the laptop fills it, at the cost of a fresh extraction when there is no cache.

## SSO does not revoke what came before it

Configuring an issuer closes the invite and typed-email sign-ins for new sessions, but tokens minted
before stay valid until they expire. Revoke them: `csa.py org revoke --minted-by web`. The issuer's
client secret is the one plaintext credential in `control.db`, so every backup carries it.
