# server/

The CSA server: one Python process per role, all from this directory.

> **Wrap-up gate:** [`../docs/development/checklist.md`](../docs/development/checklist.md). All commands run from `server/`.

## Layout

| Path | What it is |
|---|---|
| `apiserver.py` | The HTTP API (stdlib `ThreadingHTTPServer`): the auth gate, every `/api/*` route, the static UI, the anonymous workspace request. Start reading at `do_GET` and `do_POST`. |
| `mcp_server.py` | The MCP server a coding session talks to (`ask`, `fact`, `flag`). Stdout is the JSON-RPC channel: never `print()` on its path. |
| `csa.py` | The operator CLI: ingest, drain, org administration (`csa.py org ...` is the only cross-workspace plane). |
| `analyzer/` | The package. [`analyzer/CLAUDE.md`](analyzer/CLAUDE.md). |
| `ui/` | The web UI, built to `ui/dist`, served by `apiserver.py`. [`ui/CLAUDE.md`](ui/CLAUDE.md). |
| `migrations/` | The three Alembic environments (`corpus`, `telemetry`, `control`) and their revisions, driven by `alembic.ini`. The models are the schema: `../docs/development/schema-migrations.md`. |
| `tests/` | pytest. `test_migrations.py` pins the schema, `test_no_raw_sql.py` the ORM carve-outs, `test_apiserver.py` the routes and gates. |
| `e2e/` | The end-to-end harnesses, one entrypoint `e2e/run.sh [name]`. [`e2e/README.md`](e2e/README.md) says what each proves and needs. |
| `rehearse_tenancy.py` | A migration rehearsal that boots the real server over HTTP against a scratch box. The pattern for any scratch-server script. |
| `docker-compose.*.yml`, `Dockerfile` | The images. A plain `docker build` has no Claude Code and is published as `csa-server-slim`; `--target with-claude-code` adds it and is what prod pulls, `ghcr.io/nabooai/csa-server:lab-<sha>`. Any build for prod needs that target. `ui/dist` is bind-mounted, never in the image. |

## Run it

```bash
uv sync
uv run python apiserver.py                 # http://127.0.0.1:8766, auth off on loopback
CSA_AUTH=org CSA_AUTH_TOKEN=x CSA_API_PORT=8794 uv run python apiserver.py   # multi-tenant scratch box (cwd holds data/)
uv run python -m pytest tests/ -q          # the suite
e2e/run.sh                                 # the hermetic E2E harnesses (needs a built ui/dist)
uv run ruff check .                        # lint
uv run alembic -c alembic.ini -n corpus revision --autogenerate -m "what changed"   # a schema change
uv run python -m analyzer.<module>         # a legacy module's self-check
```

The `scratch-server` skill boots a seeded org-mode box for browser work.

## Conventions

- Auth is a gate at the top of `do_GET` / `do_POST`; a route is public only if it is in `_PUBLIC_EXACT`
  or `_PUBLIC_PREFIXES`; a cookie session may POST only what `_COOKIE_POST_OK` lists and only same-origin.
- Every `/api/...` miss is a JSON 404, never the SPA shell; a missing static FILE is a 404 too (a stale
  bundle hash served as HTML would be cached for a year).
- The body of a POST is read through `_content_length()` and must be a JSON object; anything else is a 400.
- Settings live in `analyzer/tenant.py` `_FIELDS`: a workspace field in the org's `tenant_config` row,
  a deployment field in control.db's `settings` row, both read per use, both set with
  `csa.py org config`. The environment carries only bootstrap values and secrets
  (`tests/test_config_screen.py` pins the list). A new `CSA_*` variable needs a written reason in the PR.
- One SQLite writer per file: corpus (`analyzer.db`), telemetry (`telemetry.db` attached as `tel`),
  control plane (`control.db`). Compute first, then write. See `../docs/development/sqlite-concurrency.md`.
- The schema lives in the models, one MetaData per file, and reaches disk as an Alembic revision.
  See `../docs/development/schema-migrations.md`.

## Lessons that apply here

- Deploying, the org-mode boot invariant, `ui/dist` and compose traps: `../docs/development/operations.md`.
- Writer locks, attached databases, the telemetry split: `../docs/development/sqlite-concurrency.md`.
- Adding a table or column, and reading a generated revision: `../docs/development/schema-migrations.md`.
- What the landing may claim and how the audit holds it: `../docs/development/landing-copy.md`.
- Adding a setting, who may write it, reading it per use: `../docs/development/settings.md`.
