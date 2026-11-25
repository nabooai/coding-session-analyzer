"""Engine registry + the connection bridge.

THE BRIDGE — a PERMANENT seam, not migration scaffolding. Callers hold a raw sqlite3 connection;
they build typed SQLAlchemy Core statements and `run()` executes them ON THAT CONNECTION.
Deliberately NOT `session_from(con)`: `Session.close()` issues a DBAPI rollback, and bridge calls
run inside caller transactions — `store.py` calls `route.resolve_owner(con, …)` mid-ingest with
pending uncommitted writes (session loop, dominance flags), and `tenant.config(con)` INSIDE
`build_fts`'s BEGIN IMMEDIATE block. A rollback there wipes pending ingest writes or re-opens the
"no such table: docs" incident scarred in build_fts's docstring. So the bridge never commits,
never rolls back, and never wraps exceptions: the caller keeps its transaction state and its DBAPI
error types (`sqlite3.OperationalError` fail-open contracts in budget/tenant/fuzzy_axes stay intact).

Why it does NOT retire: the migration converted every module's STATEMENTS to typed Core, but the
FTS5 `docs` virtual table, `sqlite_db`'s DDL/PRAGMAs, and `build_fts`'s explicit BEGIN IMMEDIATE
block all stay raw by design (SQLAlchemy has no FTS5 dialect worth adopting, and autobegin must not
touch that transaction). As long as `build_fts` hands a raw connection around with `tenant.config`
reading inside its transaction, the engine-owned world and the raw-DDL world have to meet somewhere
— here. Deleting the bridge would mean converting build_fts to an explicit `Connection` transaction
with its own concurrent-reader proof: real work, not cleanup. Until someone signs up for that, this
is the standing boundary between typed statements and the FTS/DDL carve-outs.

`run_many` is the executemany face of the same bridge (compile once, executemany on the raw con).
"""
import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.dialects import sqlite
from sqlalchemy.pool import NullPool

_DIALECT = sqlite.dialect()


def run(con, stmt):
    """Execute a Core (or text()) statement on a raw sqlite3 connection. Returns the plain
    sqlite3.Cursor — rowcount/fetch semantics identical to `con.execute`.

    # ponytail: compiles per call (~0.2ms — chaos-measured 7× on a hot upsert loop, invisible
    # at prod call rates: upserts are a few-a-day admin action, roster ~0.1% of an ask). If a
    # real workload ever shows it, the upgrade is a compiled-statement cache — separate perf PR,
    # not smuggled into a zero-behavior-change migration."""
    c = stmt.compile(dialect=_DIALECT, compile_kwargs={"render_postcompile": True})
    # The bridge bypasses SQLAlchemy's execution context, so Python-side column defaults
    # would silently bind NULL — every value must be explicit. A real raise (not assert):
    # -O would strip an assert, and this is the only guard.
    if getattr(c, "insert_prefetch", None) or getattr(c, "update_prefetch", None):
        raise TypeError("bridge statements must bind every column explicitly (no model defaults)")
    # escape_names=False: positiontup holds RAW bind names; escaped keys (a bind derived from
    # a dotted identifier) would KeyError on the lookup below (review finding, PR #71).
    params = c.construct_params(escape_names=False)
    if c.positiontup is not None:
        return con.execute(c.string, tuple(params[k] for k in c.positiontup))
    return con.execute(c.string, params)


def run_many(con, stmt, rows):
    """executemany face of the bridge: compile `stmt` once (its bindparam() placeholders, no
    values), execute over `rows` — each a dict keyed by bindparam name. Same guarantees as
    `run`: caller's transaction and DBAPI error types untouched."""
    c = stmt.compile(dialect=_DIALECT)
    if getattr(c, "insert_prefetch", None) or getattr(c, "update_prefetch", None):
        raise TypeError("bridge statements must bind every column explicitly (no model defaults)")
    if c.positiontup is not None:
        return con.executemany(c.string, [tuple(r[k] for k in c.positiontup) for r in rows])
    return con.executemany(c.string, list(rows))


# --- engine registry (the non-bridge path: repositories/tests that own their connection) ----

_engines: dict = {}
_lock = threading.Lock()


def _pragmas(dbapi_con, _record):
    # Same order and values as store.sqlite_db — the PR #49 502-under-long-writer scar.
    dbapi_con.execute("PRAGMA busy_timeout=15000")
    dbapi_con.execute("PRAGMA journal_mode=WAL")


def engine_for(db_path: str | Path):
    """Cached engine per org-db FILE (per-org files ARE the tenant boundary — a global engine
    would be wrong). NullPool: many small dbs, one-shot admin reads/writes; WAL + busy_timeout
    applied on every connect. `:memory:` is refused — NullPool would drop the database between
    connects; in-memory tests use raw sqlite3 connections and the bridge."""
    if db_path == ":memory:":
        raise ValueError("engine_for needs a file path; :memory: dbs use a raw connection + db.run")
    key = os.path.abspath(db_path)
    with _lock:
        eng = _engines.get(key)
        if eng is None:
            eng = create_engine(f"sqlite:///{key}", poolclass=NullPool)
            event.listens_for(eng, "connect")(_pragmas)
            _engines[key] = eng
    return eng
