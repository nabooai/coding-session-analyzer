"""The SQLAlchemy-migration acceptance tripwire (replaces the task doc's fuzzy "grep reaches ~0").

Every raw DBAPI call site left in the tree is a DOCUMENTED CARVE-OUT with a written reason at the
site. This test pins the exact per-file count: a NEW raw `con.execute` in a migrated module — or a
brand-new raw-SQL module — bumps a count or adds a key and fails here, in CI, not in prod. To add a
legitimate carve-out you edit ALLOWED and say why in the commit; there is no silent path back to
hand-assembled SQL.

Counts are grep-line counts (a line with >=1 of the three call forms) — the same metric the task
doc's acceptance uses. The carve-out families, and why each stays raw:

  * db.py (6)        — the bridge itself: run/run_many compile Core and call con.execute on the raw
                       connection; the _pragmas connect-listener runs PRAGMAs. This IS the seam.
  * store.py (43)    — sqlite_db's PRAGMAs, _wal's retried mode-switch and the ATTACH,
                       build_fts's BEGIN IMMEDIATE body + all `docs` FTS5 statements, index_session's
                       docs refresh (its fact-row part is refresh_fact_docs, shared with judge.run since
                       2026-09-01 — same sites, moved), write_entity_stats' correlated-EXISTS aggregate, ro_connect's
                       ATTACH-denial authorizer setup, and refresh_facts' bi-temporal CASE-ladder
                       upsert (#70; see its docstring). FTS5/DDL/txn-critical by design.
                       32 -> 37 (writer-lock fix, 2026-08-11): the stats aggregate split into
                       _entity_stats_rows (2 — full + scoped, same carve-out, now a SELECT so it can run
                       OUTSIDE the writer lock) + write_entity_stats' explicit `BEGIN IMMEDIATE` (1, the
                       same txn-critical reason build_fts has one), and _refresh_entity_docs (3 — the
                       session-scoped face of the `docs` FTS5 entity statements, one DELETE + the
                       gate-on/gate-off INSERT pair build_fts already carries verbatim).
                       49 -> 43 (Alembic adoption): the SCHEMA/TELEMETRY_SCHEMA executescripts, the
                       ALTER migration loop and the `docs` CREATE at cold open are now the baseline
                       revisions under migrations/; what is left is PRAGMA, ATTACH, FTS5 and the
                       one-time legacy telemetry copy.
                       48 -> 49 (review follow-up, same PR): _migrate_telemetry's "already moved?" test
                       became a COUNT pair instead of a `LIMIT 1` emptiness probe — tracer.py creates
                       and writes tel.spans on its OWN connection, so "non-empty" would strand every
                       legacy span. Same introspection family as the rest of that function.
                       37 -> 48 (telemetry split, same PR): _attach_telemetry's ATTACH + tel PRAGMAs +
                       TELEMETRY_SCHEMA executescript, ro_connect's read-only ATTACH + stand-in schema,
                       and _migrate_telemetry's sqlite_master/table_info probes + the one-time copy —
                       all DDL/PRAGMA/introspection, the same family as sqlite_db's cold open.
  * tracer.py (15)   — a self-contained span writer that owns its own sqlite3 connection + DDL +
                       batched writer; never takes a caller connection. Layering-wise not a bridge site.
  * search.py (6)    — `docs` MATCH / bm25 / snippet: the FTS5 virtual table, raw by design.
                       Two of them are _code_home_rows: a hidden_context repo/path probe whose
                       LIKE/IN shape is built from a variable term count, then SELECT ... FROM
                       docs by doc_id — same FTS5 carve-out as the rest.
  * orgs.py (2)      — the two foreign mode=ro legacy-token reads (a pre-expiry corpus schema).
                       5 -> 2 (Alembic adoption): the control DDL is migrations/control/.
  * migrations.py (3)— the version-table SELECT every open runs, the deferred BEGIN that makes an
                       upgrade atomic, and the `PRAGMA table_info` the catch-up baseline reads to
                       decide which columns a pre-Alembic file lacks. Core models none of the
                       three; this module IS the schema seam.
  * apiserver.py (3) — two `docs` COUNT sites (FTS) + the CSA_ENTITY_FLOOR=off reference payload.
  * app.py (2)       — two `docs` FTS sites (_locate_md MATCH, the doc_id full-text fetch).
  * csa.py (2)       — two `docs` COUNT sites (_fts_drift, the fix-projects dry-run readout).
  * ask.py (2)       — run_sql, the model-authored planner seam (one real call + its docstring
                       mention): text() would change the self-repair error text the model reads,
                       and the boundary is ro_connect, not SQLAlchemy.
  * backup.py (2)    — a FILE-level tool: it copies whole databases through SQLite's online backup
                       API (Connection.backup), which has no SQLAlchemy equivalent and takes a raw
                       DBAPI connection by definition. It must NOT go through db.engine_for for the
                       same reason orgs.py must not: engine_for runs `PRAGMA journal_mode=WAL` at
                       connect, and control.db is asserted to stay `delete` (tests/test_orgs_models.py)
                       — backing it up cannot be the thing that rewrites its header. The two statements
                       are SQLite operations Core does not model: busy_timeout on the read-only source
                       and `PRAGMA integrity_check` during verification. Fixtures live in test_backup.py.
  * resolve.py (1)   — the ticket-absence `docs` MATCH probe (FTS).
  * verify.py (5)    — the sweep's `docs` surgery (_unrefute, reindex_refs) + its sqlite_master
                       probes: the same FTS5 family as store.py's index_session block.
"""
import pathlib
import re

_PAT = re.compile(r"\.execute(?:many|script)?\(")

ALLOWED = {
    "analyzer/db.py": 6,
    # 43 pre-existing, plus 4 for the legacy-table drop in _migrate_telemetry: cross-schema DDL and
    # COUNTs over `main.` vs `tel.` on one connection, which Core has no face for (the copy those
    # four guard is raw for the same reason). Plus 4 for stems(): an in-memory fts5 table and its
    # fts5vocab twin, the only way to read the index tokenizer's stems.
    "analyzer/store.py": 51,
    "analyzer/tracer.py": 15,
    "analyzer/search.py": 6,
    "analyzer/migrations.py": 3,
    "analyzer/orgs.py": 2,
    "analyzer/backup.py": 2,
    "analyzer/ask.py": 2,
    "analyzer/resolve.py": 1,
    "analyzer/verify.py": 5,
    "apiserver.py": 3,
    "app.py": 2,
    "csa.py": 2,
}

_SERVER = pathlib.Path(__file__).resolve().parent.parent


def _count(relpath):
    return sum(1 for line in (_SERVER / relpath).read_text().splitlines() if _PAT.search(line))


def test_no_undocumented_raw_sql():
    """Every migrated .py under server/ + analyzer/ has exactly its allowlisted raw-call count —
    0 for anything not in ALLOWED. A new raw site anywhere fails until it's added here with a reason.

    SCOPE is structural, by what this walk visits: all of `analyzer/` (the task doc's acceptance
    surface) + the four converted server-root modules named below. Dev/eval scripts at server
    root (eval_recall.py, rehearse_tenancy.py) DO carry raw
    SQL but are never on a serving path and never in a phase — they are excluded by NOT being walked,
    not by a filter list. If the root loop is ever broadened to glob('*.py'), those scripts must get
    explicit ALLOWED entries or the walk must skip them by name — do not assume they're handled."""
    checked = []
    for py in sorted((_SERVER / "analyzer").glob("*.py")):
        rel = f"analyzer/{py.name}"
        n = _count(rel)
        checked.append(rel)
        assert n == ALLOWED.get(rel, 0), (
            f"{rel}: {n} raw DBAPI call sites, allowlist expects {ALLOWED.get(rel, 0)}. "
            f"A new one? migrate it or add a documented carve-out to ALLOWED.")
    for name in ("apiserver.py", "app.py", "csa.py", "mcp_server.py"):
        n = _count(name)
        assert n == ALLOWED.get(name, 0), (
            f"{name}: {n} raw DBAPI call sites, allowlist expects {ALLOWED.get(name, 0)}.")
    # every ALLOWED analyzer key was actually visited (a typo'd/renamed key can't hide)
    for key in ALLOWED:
        if key.startswith("analyzer/"):
            assert key in checked, f"ALLOWED names {key} but it doesn't exist — stale allowlist"
