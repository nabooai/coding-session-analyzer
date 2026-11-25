# CSA

CSA is team memory for Claude Code. A light Go client on each laptop redacts and ships finished
session transcripts; a Python server extracts typed facts (decision, gotcha, constraint, todo,
preference) with the quote that proves them, stores them per workspace in SQLite, and recalls them
into later sessions through a prompt hook and an MCP tool. A vanilla TypeScript web UI reads the
workspace's memory. Claude Code is the live source; every other harness is a parser.

> **Wrap-up gate:** every change clears [`docs/development/checklist.md`](docs/development/checklist.md)
> before it is declared done. Open it first, not last.

## Layout

| Path | What it is | Read first |
|---|---|---|
| `server/` | The API server, the analyzer package, the MCP server, the tests. Python 3.12, uv. | [`server/CLAUDE.md`](server/CLAUDE.md) |
| `server/analyzer/` | Extraction, storage, recall, auth, tenancy. One module per concern. | [`server/analyzer/CLAUDE.md`](server/analyzer/CLAUDE.md) |
| `server/ui/` | The web UI. TypeScript + Vite, no runtime deps. | [`server/ui/CLAUDE.md`](server/ui/CLAUDE.md) |
| `server/helm/csa/` | The Helm chart (packaged by `.github/workflows/server-release.yml`). | `docs/public-release/install-kubernetes.md` |
| `client/` | The Go client: watch, redact, ship, recall hook. Stdlib only. | [`client/CLAUDE.md`](client/CLAUDE.md) |
| `csa-plugin/`, `Formula/` | The Claude Code plugin, a client in readable Python with no binary, and the Homebrew formula. `csa-plugin/scripts/redact.py` is a byte copy of `server/analyzer/redact.py`: change both. | |
| `docs/` | Public docs: install, operate, security, architecture. Contributor pages in `docs/development/`. | [`docs/README.md`](docs/README.md) |
| `research/` | The fact-core research environment and golden cases. | `research/README.md` |
| `.claude/` | The harness layer: skills, agents, hooks, settings. | [`docs/development/agent-workflow.md`](docs/development/agent-workflow.md) |

## Where to look first

- **Lessons by area**: [`docs/development/README.md`](docs/development/README.md). Read the matching
  page before starting, not after getting stuck.
- **How it fits together**: [`docs/architecture.md`](docs/architecture.md). **Adding a harness**:
  [`docs/harnesses.md`](docs/harnesses.md).
- **A handed-off task** is a doc written by the `handoff` skill; it carries a binding working protocol
  (worktree, SWE + PM review agents, user simulations, the checklist). Follow it before the code.
- **A production deployment**: read [`docs/development/operations.md`](docs/development/operations.md)
  and [`docs/public-release/install-server.md`](docs/public-release/install-server.md) before touching one.
- **The design system**: [`docs/design/palette.md`](docs/design/palette.md) (tokens, fonts, node colours, the Ring mark).

## Conventions that are true everywhere

- Work in a git worktree off `origin/onprem-main`, the trunk (never the shared checkout; local refs
  lag). Branch names are plain short descriptions. Commit messages end with the co-author trailer.
- Tests: `cd server && uv run python -m pytest tests/ -q`; `cd server/ui && npm run check`;
  `cd client && go vet ./... && go test ./...`. Lint: `ruff` (server), `eslint` (ui), `gofmt` (client).
  The `gh pr create` hook runs these on the changed files and blocks when they are red.
- No new environment variables (config rides `tenant_config` or an existing knob), no legacy shims,
  no narrative comments around functions, asserts live in tests. Source: the founder's PR reviews,
  ledgered in [`.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md`](.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md).
- User-facing copy: no em or en dashes (use a period or a middle dot), no slogans, no feature tiles,
  fictional names only on public pages, and every landing claim names the file or route behind it.
- SQLite: one writer per file, compute first then write, never `BEGIN IMMEDIATE` on an attached set.
  See [`docs/development/sqlite-concurrency.md`](docs/development/sqlite-concurrency.md).
- The web UI never hardcodes a host; every browser-reachable POST is allowlisted in `_COOKIE_POST_OK`.
- `docs/` is public. Internal plans, task hand-offs, research notes and reflections do not go there.

## Skills to load when working here

- **`plan-task`** before any multi-file change.
- **`code-tests`** before writing or running tests.
- **`scratch-server`** to boot an org-mode server with seeded demo data for a browser walk.
- **`ui-visual-walk`** for any visible UI change (both themes, 390px).
- **`user-sims`** after a feature is built: five personas, fix, re-run.
- **`pr-code-review`** (fresh context) and **`pr-description`** before opening a PR.
- **`self-reflection`** as the last step of every change (into the PR body); **`promote-lessons`** periodically.
- **`handoff`** to write a task doc for another session.
