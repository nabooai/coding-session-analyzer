# Lesson: schema migrations

The models are the schema. A change is a model change plus an Alembic revision, reviewed by hand and
committed together. There is no DDL string to edit any more.

## Three environments, because there are three files

| Environment | File | Models | Version row lands in |
|---|---|---|---|
| `corpus` | `data/orgs/<org>/analyzer.db`, attached as `main` | `tenant.Base` + `analyzer/models.py` | `main.alembic_version` |
| `telemetry` | its `telemetry.db` sibling, attached as `tel` | `models.telemetry` | `tel.alembic_version` |
| `control` | `data/control.db` | `tenant.ControlBase` | `main.alembic_version` |

One SQLite file is one writer, so the file split is the concurrency design; giving each file its own
`alembic_version` keeps a file copyable, backupable and restorable on its own. Three MetaData objects,
not one with filters: a migration then cannot conjure a control table into an org db, or the reverse.

Telemetry migrations declare `schema="tel"`, which is exactly what the models do, and run on the
corpus connection that already has the sibling attached. From the CLI the same revision runs against
an empty `main` with the file attached as `tel`, so the text reads the same either way.

## Adding or changing a table

```bash
cd server
uv run alembic -c alembic.ini -n corpus revision --autogenerate -m "what changed"   # or -n telemetry / -n control
uv run alembic -c alembic.ini -n corpus check                                        # no diff left
uv run python -m pytest tests/test_migrations.py -q
```

Autogenerate diffs the models against the database `alembic.ini` points at, which is a throwaway file
built by `alembic upgrade head`. Point it at a real `data/analyzer.db` and you diff against last
month's schema and get a revision that recreates the world.

`revision --autogenerate` and `check` open the database and therefore run `env.py`; `heads`,
`history` and `show` do not, and they still import every revision module, which imports
`analyzer.migrations`. `alembic.ini`'s `prepend_sys_path = %(here)s` is what puts `server/` on the
path for both cases, so the commands work from any directory. Without it the three that skip
`env.py` die with `ModuleNotFoundError: No module named 'analyzer'`, and `heads` is how the next
branch finds the `down_revision` to parent onto.
`tests/test_migrations.py::test_the_documented_cli_migrates_a_real_file` runs the whole thing as a
subprocess from a directory that is not `server/`, on real files, empty and pre-Alembic, for all
three environments.

Read the generated revision before committing it, and delete Alembic's
`### commands auto generated ###` markers. Three things to check:

- **A server default is SQL, not a Python string.** `server_default="'session'"` renders
  `DEFAULT '''session'''` and the stored value keeps the quotes. Write `sa.text("'session'")`.
- **An empty `upgrade()`** means autogenerate found no diff: the model edit is missing, or the
  file it diffed against was already ahead.
- **A drop or a retype will not render.** `render_as_batch` is deliberately OFF, so autogenerate
  emits a plain `op.add_column` / `op.create_table` and nothing else. SQLite cannot drop or retype a
  column, and Alembic's batch mode does it by COPYING the table into a new one: a full data rewrite
  under the writer lock, and SQLAlchemy does not reflect CHECK constraints, so the copy silently
  drops them (measured: a batch rewrite of `tenant_config` lost `CHECK (id = 1)`, the invariant that
  keeps it a single-row table, and `id=2` then inserted). Write that migration by hand, decide what
  it does to the constraints and the writer lock, and run it where a corpus-wide pass belongs.

Types mirror what SQLite holds: `Text`, `Integer`, `REAL`. And a hand-written `x TEXT PRIMARY KEY` is
NULLABLE in SQLite (the legacy rowid rule), so the models say `nullable=True` on those columns. Both
choices exist so a workspace created today is schema-identical to one created before Alembic:
`tests/test_migrations.py::test_a_fresh_file_is_schema_identical_to_a_pre_alembic_one`.

## Two branches, one head

Two PRs each cutting a revision off the same head give an environment two heads, and
`alembic upgrade head` then fails at container boot, on prod, for every workspace. Rebase and
re-parent the later revision (`down_revision = <the merged one>`); never merge heads by hand.
`test_one_head_per_environment` fails first, in CI.

## The baseline is a catch-up, and the last one

Every file that predates Alembic was built by some past version of the old DDL string plus a
try/except `ALTER TABLE` list, so no baseline can assume a shape. `migrations.catch_up` creates what
is missing and adds only the columns a file lacks, then the file is stamped. It drops nothing and
rewrites nothing, so an interrupted upgrade is safe to repeat, and a rollback to the previous image
finds every column and row where it left them.

It is the baselines' only tool. Later revisions are ordinary `op.add_column` against a shape the
version row guarantees. "Add if missing" in a normal revision means the version row is not trusted,
and then nothing is.

**Baseline or revision: the line is whether any file has been stamped.** Until the Alembic image is
deployed, no file anywhere has an `alembic_version` row, the baselines are not yet a contract with
anything on disk, and "as of the Alembic adoption" means whatever master holds today. A table that
lands before that deploy belongs IN the baseline, even if it was written as a DDL string, because
some box has already run that DDL and the baseline is what has to land on any of those shapes.
`tel.ui_events` (#106) and `cli_pairings` (#107) merged the day the baselines were written and are
in them for exactly this reason. A revision would have raised: the catch-up finds the table, skips
it, and then `op.create_table` says it already exists, which fails that file's open, which for
telemetry is every corpus connection and for control.db is every request.

Once the image is deployed the baselines are frozen and every change is a revision, with no
exceptions, because from then on the version row does describe the file.
`tests/test_migrations.py::test_the_tables_a_master_image_already_created_are_left_alone` pins the
skip path against `legacy_ddl.SEP05_2026_*`, the shape a box that pulled a master image has.

## What migrations do not own

The FTS5 `docs` virtual table and the two `session_workspace` views are raw `op.execute` in the corpus
baseline (SQLAlchemy has no FTS5 dialect worth adopting). `docs` is a derived index that
`store.build_fts` drops and recreates from `store.DOCS_COLS`, so the baseline reads that constant
rather than freezing a copy. Anything on a file that is not in the environment's metadata is left
alone and never diffed: the FTS5 shadow tables, the views, and the legacy in-corpus
`served`/`queries`/`spans` the telemetry split left behind for rollback.

Code that names tables outside the models is where this leaks. `fuzzy_axes.SKIP_TABLES` is the one
such list: every entry with a model is `SomeModel.__table__.name`, so a rename moves the set, and
the literals are only the things above, which have no model to ask.
`tests/test_migrations.py::test_the_tables_fuzzy_axes_skips_are_real` fails if one of those literals
stops existing on a fresh file, or if it gains a model and should have moved.

## At runtime

`migrations.ensure(con, env, path)` reads `alembic_version` and runs the upgrade only when the file is
behind, so an up-to-date open costs one SELECT (pinned by
`test_an_up_to_date_open_costs_one_extra_statement`). It runs on the caller's connection, never
through `db.engine_for`, whose connect-time `PRAGMA journal_mode=WAL` is persistent and would flip
control.db's on-disk format. The upgrade is one deferred `BEGIN`, never `IMMEDIATE`, which would take
the writer lock on every attached file.

Three containers open every workspace within milliseconds of a deploy, so the upgrade is guarded by a
per-path thread lock and an `flock` on `<file>.migrate.lock`, and every waiter re-reads the version
row before deciding to work.

**A failed migration fails the open.** There is no fail-open: a half-migrated file serving reads is
worse than a loud error, and the failure is scoped to one file, so the other workspaces keep serving.
Two honest caveats. A route that opens a corpus does so outside any handler, so the request dies with
a traceback in the api log and a dropped connection, not a JSON 500 (true of any SQLite error there,
not just a migration). And `control.db` is the exception to the scoping: every request authenticates
through it, so a failure there fails all of them, which `/api/health` reports as 503.
