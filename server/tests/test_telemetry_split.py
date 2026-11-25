"""Telemetry lives in its OWN SQLite file, so the serving path can never be blocked by ingest.

SQLite allows exactly one writer per FILE. `served`/`queries`/`spans` are written on every ask and
every ambient-recall hook, and nothing reads them to answer a question — keeping them in the corpus
meant an `ask` queued behind whatever the ETL was writing. On 2026-08-11 that killed a real ask at
store.log_served ("database is locked" after the 15s busy_timeout, answer already computed).

The property pinned here is structural, not statistical: with the CORPUS writer held, the serving
path's writes still succeed. Run from server/: uv run pytest tests/test_telemetry_split.py
"""
import os
import sqlite3
import time

import pytest

from analyzer import store


@pytest.fixture()
def corpus(tmp_path):
    path = str(tmp_path / "analyzer.db")
    con = store.sqlite_db(path)
    yield con, path
    con.close()


def test_telemetry_is_a_separate_file_attached_as_tel(corpus):
    con, path = corpus
    assert os.path.exists(store.telemetry_path(path)), "telemetry.db must be created next to the corpus"
    assert store.telemetry_path(path) != path
    assert [r[1] for r in con.execute("PRAGMA database_list") if r[1] != "temp"] == ["main", "tel"]
    assert not con.execute("SELECT 1 FROM main.sqlite_master WHERE name='served'").fetchone(), \
        "a FRESH corpus must not carry the telemetry tables at all"


def test_serving_writes_survive_a_held_corpus_writer(corpus):
    """THE test. A drain is writing the corpus; an ask logs its provenance anyway.

    The blocker is a `store.sqlite_db` connection — i.e. one with `tel` ATTACHED — because that is what
    every real writer is. A bare sqlite3.connect() cannot catch the bug this test exists for: `BEGIN
    IMMEDIATE` takes the writer on EVERY attached database, so an IMMEDIATE txn on a real connection
    locks telemetry.db too and the split buys nothing. Corpus writers use deferred+write-first for
    exactly this reason (store.write_entity_stats / store.build_fts)."""
    con, path = corpus
    blocker = store.sqlite_db(path)
    blocker.execute("PRAGMA busy_timeout=0")
    blocker.execute("BEGIN")                                 # what index_session/build_fts hold
    blocker.execute("INSERT INTO entities VALUES ('e1','file','e1',NULL)")

    store.log_served(con, "sess-1", "how do we deploy", [("ref-1", "hidden_context", "csa")])
    store.log_query(con, "mara@example.com", "sess-1", "fts", "how do we deploy", "terms", 1, 12)

    assert con.execute("SELECT COUNT(*) FROM tel.served").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM tel.queries").fetchone()[0] == 1

    con.execute("PRAGMA busy_timeout=200")                   # don't pay the real 15s just to prove it
    with pytest.raises(sqlite3.OperationalError):            # control: the blocker IS holding the
        con.execute("INSERT INTO entities VALUES ('e2','file','e2',NULL)")   # corpus writer
        con.commit()
    con.rollback()
    con.execute("PRAGMA busy_timeout=15000")
    blocker.execute("ROLLBACK")
    blocker.close()


def test_build_fts_does_not_lock_the_telemetry_file(tmp_path, monkeypatch):
    """The one that catches the real shape. `build_fts` is the longest corpus writer AND it runs on the
    SERVING containers (flag, retype, /api/forget, the autojudge sweep), so if its transaction reaches
    telemetry.db, a teammate's flag click can starve an ask's log_served past busy_timeout — the exact
    incident, reintroduced through the attach. Parked at the same tenant.config hook test_store_txn uses."""
    import threading

    from analyzer import tenant

    path = str(tmp_path / "analyzer.db")
    con = store.sqlite_db(path)
    con.execute("INSERT INTO sessions(session_id,project,started_at,summary,source,user) "
                "VALUES ('s1','p','2026-07-01T00:00:00Z','built the thing','session','mara@example.com')")
    con.commit()

    inside, release, failures = threading.Event(), threading.Event(), []
    real_config = tenant.config

    def parked_config(c):
        inside.set()
        assert release.wait(10), "serving path never ran"
        return real_config(c)

    monkeypatch.setattr(tenant, "config", parked_config)

    def writer():
        wcon = store.sqlite_db(path)                         # thread-bound; the real writer's shape
        try:
            store.build_fts(wcon)
        except Exception as e:                               # noqa: BLE001
            failures.append(e)
        finally:
            wcon.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert inside.wait(10), "build_fts never reached tenant.config — hook point moved, fix the test"
        con.execute("PRAGMA busy_timeout=300")               # a real stall would be 15s; 300ms proves it
        store.log_served(con, "sess-1", "how do we deploy", [("ref-1", "hidden_context", "csa")])
        store.log_query(con, "mara@example.com", "sess-1", "fts", "how do we deploy", "terms", 1, 12)
        assert con.execute("SELECT COUNT(*) FROM tel.served").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM tel.queries").fetchone()[0] == 1
    finally:
        con.execute("PRAGMA busy_timeout=15000")
        release.set()
        t.join(30)
    assert not failures, failures
    con.close()


def test_spans_land_in_the_telemetry_file_not_the_corpus(tmp_path):
    from analyzer import tracer
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()
    assert tracer._telemetry_path(path) == store.telemetry_path(path), \
        "tracer duplicates the path rule (no import back into store) — the two must agree"

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # inside the 30-day retention _prune applies
    tracer._write_db(path, [{"trace_id": "t1", "span_id": "s1", "parent_id": None, "name": "ask",
                             "kind": "CHAIN", "at": now, "dur_ms": 5,
                             "status": "OK", "attrs": {}}])
    tel = sqlite3.connect(store.telemetry_path(path))
    assert tel.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 1
    corpus = sqlite3.connect(path)
    assert not corpus.execute("SELECT 1 FROM sqlite_master WHERE name='spans'").fetchone(), \
        "spans must not be created in the corpus file"


def test_legacy_rows_move_once_and_the_copied_tables_are_dropped(tmp_path):
    """Upgrade path. search.rerank scores a fact partly by how often it was served, so these rows are
    a ranking input, not bookkeeping — they have to come across. They are then dropped from the corpus:
    a frozen table with the live table's name and shape answers questions confidently and wrongly."""
    path = str(tmp_path / "analyzer.db")
    legacy = sqlite3.connect(path)                           # a pre-split corpus: telemetry inside it
    legacy.executescript("""
        CREATE TABLE served(session TEXT, asked_at TEXT, question TEXT, ref TEXT, kind TEXT,
                            project TEXT, person TEXT, asked_by TEXT, trace_id TEXT);
        CREATE TABLE queries(at TEXT, asked_by TEXT, session TEXT, engine TEXT, query_text TEXT,
                             planned_query TEXT, row_count INT, latency_ms INT, acted_on INT,
                             result_ids TEXT, trace_id TEXT);""")
    legacy.execute("INSERT INTO served(session, ref, kind) VALUES ('old-1','ref-old','hidden_context')")
    legacy.execute("INSERT INTO queries(at, session, engine) VALUES ('2026-01-01','old-1','fts')")
    legacy.commit()
    legacy.close()

    store._TEL_MIGRATED.clear()                              # the in-process memo, not the guard
    con = store.sqlite_db(path)
    assert con.execute("SELECT ref FROM tel.served").fetchall() == [("ref-old",)]
    assert con.execute("SELECT session FROM tel.queries").fetchall() == [("old-1",)]
    assert not con.execute("SELECT 1 FROM main.sqlite_master WHERE name='served'").fetchone(), \
        "the copied legacy table is gone, so nobody can read a frozen answer out of it"

    con.close()
    store._TEL_MIGRATED.clear()                              # reopen: must NOT copy a second time
    con = store.sqlite_db(path)
    assert con.execute("SELECT COUNT(*) FROM tel.served").fetchone()[0] == 1, "migration must be once-only"
    # and the drop must not have cost a row: the copy is the only reason the drop is allowed
    assert con.execute("SELECT ref FROM tel.served").fetchall() == [("ref-old",)]
    con.close()
