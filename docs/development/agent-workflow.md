# Agent workflow

Most changes to CSA are made with Claude Code. The `.claude/` directory is the harness that makes
those sessions repeatable: skills (packaged procedures), agents (reviewers and simulated users) and
hooks (gates that run on their own). None of it is needed to build or run CSA; all of it is how a
change is expected to be made and checked here.

## The hooks (`.claude/hooks/`, wired in `.claude/settings.json`)

| Hook | When | What it does |
|---|---|---|
| `inject_checklist.sh` | session start | adds the marked region of [checklist.md](checklist.md) to the session |
| `auto_format.sh` | after every edit | autofixes the touched file (`ruff check --fix`, `eslint --fix`, `gofmt -w`) |
| `gate_pr_create.sh` | before `gh pr create` | for each component the branch changed: ruff and pytest, `npm run check`, gofmt, `go vet` and `go test`; blocks the PR when one is red |

## The loop, and the skill for each step

1. **Plan before code** (`plan-task`). Read the `CLAUDE.md` of every directory the change touches and
   the lesson each one points at. List the unknowns and ask about the ones that would change the
   approach. Name the class of bug this kind of change usually ships here (a writer held during
   compute, a POST missing from `_COOKIE_POST_OK`, a boot-time invariant, a landing claim without a
   source) and plan the check for it. The `senior-swe-reviewer` and `pm-reviewer` agents, launched
   together, are the plan's challenge round.
2. **Write the tests with the code** (`code-tests`): pytest for the server, vitest for the UI,
   `go test` for the client.
3. **Boot a scratch server** (`scratch-server`) for anything that needs a real server: an org-mode
   box in a temp directory with two seeded workspaces, an invite and an admin. Isolation is only
   testable with two workspaces, so seed the second with a marker phrase and assert it never shows
   in the first. Both workspaces run with `autojudge=off`, so nothing is judged or spent and facts are seeded directly.
4. **Walk the UI** (`ui-visual-walk`) for any visible change: both themes, 390px, reduced motion,
   console and network clean. Some bugs only exist in a browser: a fractional canvas size, a stuck
   hover card on touch, a hidden element that renders anyway.
5. **Simulate users** (`user-sims`, the `user-sim` agent) when a feature is built: a security
   reviewer who tries to falsify every claim, a developer arriving from a teammate's link, a
   workspace admin, a phone visitor, a hostile prober. Each persona reports friction and fixes
   nothing; the implementing session fixes, then re-runs the same personas.
6. **Review with fresh eyes** (`pr-code-review`, one agent, new context) and write the PR body from
   the diff (`pr-description`).
7. **Reflect** (`self-reflection`): what did I miss, what surprised me, what would I do differently,
   written from evidence (the review findings, the simulation reports, the corrections) into the PR
   body. **Promote** (`promote-lessons`) what recurs into a page in this folder, with a one-line
   pointer from the relevant `CLAUDE.md`, and run the `claim-verifier` agent on it.

## Larger programs

`handoff` writes a self-contained task doc for another session (decisions, research with
`file:line` pointers, acceptance, the working protocol). A program of dependent PRs is run from one
session that owns the sequencing and launches one subagent per unblocked PR, each in its own
worktree, rather than from several sessions that collide.

## The rest of `.claude/skills/`

- `csa-install`: install the server and a client end to end from a clean checkout.
- `research-referee`: run the fact-core golden cases in `research/`.
- Deploying: there is no deploy skill; use [install-server.md](../public-release/install-server.md).
