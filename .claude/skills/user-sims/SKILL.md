---
name: user-sims
description: After a feature is built, launch several user-simulation agents concurrently (security reviewer, developer from a teammate's link, workspace admin, phone visitor, hostile prober), fix what they find, then re-run them to confirm. Use when a task's protocol calls for user simulations, or when the user says "run the sims", "simulate users", "/user-sims".
---

# User simulations

A fresh agent playing a user reports what the engineer has a story for. Five personas, in one
message so they run concurrently, each in its own browser context; then fix; then the same five
again with the list of what changed.

## Before launching

- The `scratch-server` skill is up with the built UI; you have both invites and the admin email.
- Each brief states: the URL, the invites and emails, the persona, the scenario, the rules
  (own `isolatedContext`, own pageId only, never modify files, never stop a server, report ranked
  friction under 700 words, no em dashes), and what round it is.

## The five briefs (adapt the scenario to the feature)

1. **Security reviewer**: reads every sentence on the landing and the sign-in, tries to falsify each
   against the repo (`llm.py`, `judge.py`, `archive.py`, `apiserver.py`, `orgs.py`, `csa.py`,
   `client/`), checks the `source` on each claim in `landing.ts`, lists third-party requests on load.
2. **Developer from a teammate's link**: a deep link signed out, sign in, land on it, find the team's
   memory in under a minute, scope to one teammate and prove no third person's fact shows, flag and
   undo, theme, keyboard only, console; then the OTHER workspace, which must show none of the first.
3. **Workspace admin**: every tab and link, what the page promises versus what exists, entity types
   add and remove, sign out and Back, a bookmark while signed out.
4. **Phone visitor** at `390x844x2,mobile,touch`: hero, pillars, request form submit, sign-in card,
   no horizontal scroll, tap targets, overlap, input font sizes.
5. **Hostile prober** (curl plus the browser): every browser-reachable POST with oversize, non-JSON,
   non-object, junk Content-Length, cross-origin, a 40-request burst; `<script>` in every free-text
   field; malformed hashes; the server log for tracebacks before and after.

## Round two

Same personas, a brief that lists each round-one finding and its fix, and asks for pass or fail per
item plus a short regression pass. A finding that survives round two blocks the PR.

## Record

Findings and fixes go in the worktree's `worklog.md` (round one, round two) and, at the end, in
the PR body's reflection.

Related: `docs/development/agent-workflow.md`, the `user-sim` agent in `.claude/agents/`.
