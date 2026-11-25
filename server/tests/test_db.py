"""The migration-era bridge (analyzer/db.py): transaction neutrality, DBAPI error passthrough,
and the engine registry's WAL/busy_timeout contract. These pin WHY the bridge is
compile-and-execute rather than session_from(con) — see db.py's docstring."""
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy import column as sa_column, table as sa_table

from analyzer import db


def test_bridge_never_touches_the_callers_transaction():
    """store.py calls migrated readers MID-TRANSACTION (resolve_owner during ingest,
    tenant.config inside build_fts's BEGIN IMMEDIATE). A bridge read must leave the caller's
    pending writes pending — neither committed nor rolled back."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(x INT)")
    con.commit()
    con.execute("INSERT INTO t VALUES (1)")              # pending, uncommitted
    assert con.in_transaction
    rows = db.run(con, select(sa_column("x")).select_from(sa_table("t"))).fetchall()
    assert rows == [(1,)]                                # the read sees the caller's own pending state
    assert con.in_transaction, "bridge committed or rolled back the caller's transaction"
    con.rollback()
    assert db.run(con, select(sa_column("x")).select_from(sa_table("t"))).fetchall() == []


def test_bridge_preserves_dbapi_exception_types():
    """fuzzy_axes/budget/tenant fail open on sqlite3.OperationalError specifically — a bridge
    that re-raised sqlalchemy.exc.* would silently break every fail-open contract."""
    con = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        db.run(con, select(sa_column("x")).select_from(sa_table("missing"))).fetchall()


def test_bridge_renders_postcompile_in_clauses():
    """.in_(list) renders IN (__[POSTCOMPILE_x]) without render_postcompile — a syntax error on
    a raw connection. Nothing in P1 uses IN; the bridge outlives P1, so pin it now."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(x INT)")
    con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    got = db.run(con, select(sa_column("x")).select_from(sa_table("t"))
                 .where(sa_column("x").in_([1, 3]))).fetchall()
    assert sorted(got) == [(1,), (3,)]


def test_bridge_refuses_python_side_defaults():
    """The bridge bypasses SQLAlchemy's execution context, so a model-level default= would
    silently bind NULL. The bridge must refuse the statement instead."""
    t = Table("d", MetaData(), Column("id", Integer, primary_key=True),
              Column("v", String, default="filled-by-orm"))
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE d(id INT PRIMARY KEY, v TEXT)")
    with pytest.raises(TypeError, match="explicitly"):   # a real raise: -O strips asserts
        db.run(con, insert(t).values(id=1))              # omits v -> default would prefetch


def test_bridge_handles_escaped_bind_names():
    """A bind derived from a dotted identifier gets an ESCAPED params key while positiontup
    holds the raw name — construct_params(escape_names=False) keeps them aligned (PR #71
    review finding; fuzzy_axes feeds introspected identifiers into bind comparisons)."""
    con = sqlite3.connect(":memory:")
    con.execute('CREATE TABLE t("a.b" TEXT)')
    con.execute('INSERT INTO t VALUES (?)', ("v",))
    got = db.run(con, select(sa_column("a.b")).select_from(sa_table("t"))
                 .where(sa_column("a.b") == "v")).fetchall()
    assert got == [("v",)]


def test_engine_for_refuses_memory():
    with pytest.raises(ValueError, match=":memory:"):
        db.engine_for(":memory:")


def test_engine_for_applies_wal_and_busy_timeout(tmp_path):
    """Every per-org engine connect gets the PR #49 pragmas, same values as store.sqlite_db."""
    eng = db.engine_for(str(tmp_path / "org.db"))
    with eng.connect() as c:
        raw = c.connection.driver_connection
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA busy_timeout").fetchone()[0] == 15000


def test_engine_cache_is_canonical_per_path(tmp_path, monkeypatch):
    p = tmp_path / "org.db"
    monkeypatch.chdir(tmp_path)
    assert db.engine_for(str(p)) is db.engine_for("org.db")   # abspath-keyed


def test_db_and_models_never_import_store():
    """store.py imports entity_types which imports db/models — if db or models ever imports
    store the server dies at import time with a cycle."""
    subprocess.run(
        [sys.executable, "-c",
         "import analyzer.db, analyzer.models, sys; assert 'analyzer.store' not in sys.modules"],
        check=True)
