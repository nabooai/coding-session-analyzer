"""Query layer (analyzer/ask.py) tests — the module's former __main__ self-check, ported."""
import re
import sqlite3

import pytest

from analyzer.ask import NL2Q_PROMPT, run_sql


def test_run_sql_cols_and_rows():
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1),(2);")
    cols, rows = run_sql(c, "SELECT COUNT(*) AS n FROM t")
    assert cols == ["n"] and rows[0][0] == 2, (cols, rows)


def test_mode_ro_blocks_writes_at_the_engine(tmp_path):
    """The run_sql carve-out's safety boundary: a `mode=ro` connection refuses writes with
    sqlite3.OperationalError — the guarantee that holds against arbitrary planner SQL."""
    p = str(tmp_path / "ro.db")
    sqlite3.connect(p).execute("CREATE TABLE t(x)")
    ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")


def test_nl2q_prompt_schema_has_user_column():
    # tripwire: the planner schema must keep advertising sessions.user (who-did-X questions)
    assert re.search(r"sessions\([^)]*\buser\)", NL2Q_PROMPT), "planner schema lost the user column"
