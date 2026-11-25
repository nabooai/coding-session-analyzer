# Lesson: settings

Everything tunable lives in `analyzer/tenant.py` `_FIELDS` and is read per use. The environment holds
bootstrap values and secrets only; `tests/test_config_screen.py::test_no_setting_is_read_from_the_environment`
pins the list.

- **Who may write a field follows what the field makes the server do.** A value that makes the server
  run a subprocess as the operator (`registry_sources`: `git -C <path>`, `gh api`) is `server_only`,
  refused over HTTP. A value that sends data off the box (`otlp_endpoint`, `request_webhook`) is
  `egress`, refused to a browser cookie session, which an XSS can ride. A deployment field is writable
  over `/api/config` only on a one-workspace server (`orgs.sole_org()`). Found as a CRITICAL review
  finding on PR #133 after the registry fields joined the HTTP API with everything else.
- **A URL can carry a credential.** Mark it `secret`: shown and audited as `scheme://host/…` only,
  and the UI never pre-fills it.
- **Read per use, never into a module constant.** A change applies on its next use, with no
  restart. A per-request read of control.db opens it read-only (`tenant.deployment_row`) so it
  never creates or migrates the file; per-span reads are too hot, so the tracer reads once per
  written batch.
- **The bridge read swallows every error into the defaults.** `tenant.config(con)` has to survive an
  old db and a bare connection, so a bug in it reads as "every workspace is on the defaults" with no
  error. Test it against a raw `sqlite3` connection whenever it changes (`db.run` on one returns plain
  tuples: key rows by `stmt.selected_columns.keys()`).
- **A save spans two files.** The workspace row is the org's db, the deployment row and the audit
  lines are control.db. `tenant.apply` writes the control.db half uncommitted, stores the workspace
  row, then commits; a failed workspace write rolls control.db back.
- **Moving a pool size into a setting changes what the pool is for.** The ETL's drain pool is fixed
  at `MAX_DRAIN_WORKERS` and capped per tick by `drain_workers`; upkeep (registry, compaction, purge)
  has its own `UPKEEP_WORKERS` pool so a raised cap never lifts theirs.
- **An upgrade carries no env fallback.** A box that set `CSA_*` values must write them into the rows
  (`csa.py org config <org> --set field=value`) before the new image starts; the old names are ignored.
