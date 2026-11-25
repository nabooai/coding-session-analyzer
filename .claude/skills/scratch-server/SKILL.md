---
name: scratch-server
description: Boot a throwaway multi-tenant CSA server with two seeded workspaces, an invite and an admin, for browser walks, user simulations and probes. Use when a change needs a real server to look at ("run it locally", "I need a server to screenshot", "boot a scratch box", "/scratch-server"). Never points at production.
---

# Scratch server

One command brings up the real `apiserver.py` in org mode against a scratch directory, creates two
workspaces, seeds sessions, facts, recall receipts and an admin, and prints the invites.

## Run

```bash
bash .claude/skills/scratch-server/scratch.sh /tmp/csa-box 8794      # box dir, port
```

Prints: the two invites, the admin email, the URL, and the log path. Sign in through the form
(Email + Invite code) or with curl:

```bash
curl -s -c jar -H 'content-type: application/json' \
  -d '{"invite":"<invite>","email":"mara@demo.test"}' http://127.0.0.1:8794/api/ui-login
curl -s -b jar -H 'Sec-Fetch-Site: same-origin' http://127.0.0.1:8794/api/facts?limit=3
```

Stop it with `pkill -f "<repo>/server/apiserver.py"`; rerun the script to rebuild the box from
scratch (it deletes the directory first).

## What is in the box

- `demo-co`: 12 sessions across mara@, jules@ and tomasz@demo.test, 57 facts of five types with
  quotes, recall receipts on a few facts, mara is a workspace admin (`csa.py org role`).
- `other-co`: one session whose text contains OTHERCO CONFIDENTIAL, for isolation checks.
- `autojudge=off` in both workspaces' settings: nothing is judged or spent; facts are seeded directly.
- `archive_days` is 0 per workspace unless you set it: `csa.py org config demo-co --set archive_days=forever` from the box directory.

## Traps

- The box directory is the cwd for the server and every `csa.py` call; `data/` is relative.
- The invite path mints `member` for a browser session even for an admin; the role shows through
  `/api/me` only for verified sign-ins. Test admin-only routes with a bearer token if needed.
- The anonymous request route is rate limited per network (10 per window); a second persona on
  loopback sees 429 by design.
- The UI served is `server/ui/dist`: run `npm run build` first, and again after every UI change.

Related: `docs/development/agent-workflow.md`, `docs/development/sqlite-concurrency.md` (seeding).
