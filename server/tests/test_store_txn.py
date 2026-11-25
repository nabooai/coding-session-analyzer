"""build_fts's BEGIN IMMEDIATE contract, enforced by a test instead of a docstring
(migration acceptance item; the store migration itself is P3 — this pins CURRENT behavior).

A reader on another connection must see old-or-new docs, NEVER "no such table: docs" — the
scarred incident. Deterministic sync: build_fts calls tenant.config(con) exactly once, INSIDE
the BEGIN IMMEDIATE block after DROP+CREATE+INSERTs; a patched config parks the writer there
while the reader reads. No sleeps.

P3 additions: (a) index_session's session-scoped docs refresh gives a concurrent reader
old-or-new docs, never a mid-refresh gap (parked via a connection proxy on the refresh's own
DELETE); (b) write_entity_stats' rows are COMMITTED — visible to a second connection — by the
moment build_fts's BEGIN IMMEDIATE starts (parked at the same tenant.config hook point)."""
import sqlite3
import threading


from analyzer import store, tenant


def test_concurrent_reader_never_sees_missing_docs(tmp_path, monkeypatch):
    path = str(tmp_path / "analyzer.db")
    con = store.sqlite_db(path)
    con.execute("INSERT INTO sessions(session_id,project,started_at,summary,source,user) "
                "VALUES ('s1','p','2026-07-01T00:00:00Z','built the thing','session','mara@example.com')")
    con.execute("INSERT INTO worked_on VALUES ('s1','the thing')")
    con.commit()
    store.build_fts(con)                                 # unhooked populate
    con.close()
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    assert before > 0, "populate failed — the test would pass vacuously"

    inside = threading.Event()                           # writer is inside BEGIN IMMEDIATE, docs dropped+recreated
    read_done = threading.Event()
    real_config = tenant.config

    def parked_config(c):
        inside.set()
        assert read_done.wait(10), "reader never ran"
        return real_config(c)

    monkeypatch.setattr(tenant, "config", parked_config)
    failures = []

    def writer():
        wcon = store.sqlite_db(path)                     # sqlite3 connections are thread-bound
        try:
            store.build_fts(wcon)
        except Exception as e:                           # noqa: BLE001 — any writer crash fails the test
            failures.append(e)
        finally:
            wcon.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert inside.wait(10), "build_fts never reached tenant.config — hook point moved, fix the test"
        reader = sqlite3.connect(path)                   # fresh connection: sees last COMMITTED state
        reader.execute("PRAGMA busy_timeout=15000")
        mid = reader.execute("SELECT COUNT(*) FROM docs").fetchone()[0]   # must not raise
        assert mid == before, f"reader saw a partial rebuild: {mid} != {before}"
        reader.close()
    finally:
        read_done.set()
        t.join(30)
    assert not failures, failures
    after = sqlite3.connect(path).execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    assert after == before, "rebuild changed the corpus on identical inputs"


class _ParkAfter:
    """Connection proxy: execute normally, but the FIRST statement containing `needle` parks the
    writer (sets `inside`, waits for `release`) AFTER executing it — i.e. inside the open write
    transaction, at a known dangerous moment. Everything else (commit, in_transaction, the
    db.run bridge, tenant.config's reads) passes straight through to the real connection."""

    def __init__(self, con, needle, inside, release):
        self._con, self._needle = con, needle
        self._inside, self._release = inside, release

    def execute(self, sql, *args):
        cur = self._con.execute(sql, *args)
        if self._needle in sql and not self._inside.is_set():
            self._inside.set()
            assert self._release.wait(10), "test never released the parked writer"
        return cur

    def __getattr__(self, name):
        return getattr(self._con, name)


def test_index_session_refresh_reader_never_sees_missing_docs(tmp_path):
    """(a) The session-scoped docs refresh (DELETE this session's docs + re-INSERT) runs inside
    ONE uncommitted transaction: a reader on another connection parked right after the DELETE
    must still see the session's OLD docs — never a gap."""
    path = str(tmp_path / "analyzer.db")
    con = store.sqlite_db(path)
    sess = dict(session_id="s1", project="p", git_branch="b", started_at="2026-07-01T00:00:00Z",
                ended_at=None, n_user=1, n_asst=1)
    data = {"summary": "built the thing", "worked_on": ["the thing"],
            "hidden_context": [{"type": "gotcha", "claim": "the thing needs a retry", "evidence": "e"}]}
    store.index_session(con, dict(sess), dict(data))       # unhooked populate
    con.close()
    ro = sqlite3.connect(path)
    before_s1 = ro.execute("SELECT COUNT(*) FROM docs WHERE session_id='s1'").fetchone()[0]
    before_all = ro.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    ro.close()
    assert before_s1 > 0, "populate failed — the test would pass vacuously"

    inside, release = threading.Event(), threading.Event()
    failures = []

    def writer():
        wcon = store.sqlite_db(path)                     # sqlite3 connections are thread-bound
        try:
            proxy = _ParkAfter(wcon, "DELETE FROM docs WHERE session_id=?", inside, release)
            store.index_session(proxy, dict(sess), dict(data))
        except Exception as e:                           # noqa: BLE001 — any writer crash fails the test
            failures.append(e)
        finally:
            wcon.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert inside.wait(10), "index_session never ran the session-scoped docs DELETE — hook moved, fix the test"
        reader = sqlite3.connect(path)                   # fresh connection: sees last COMMITTED state
        reader.execute("PRAGMA busy_timeout=15000")
        mid_s1 = reader.execute("SELECT COUNT(*) FROM docs WHERE session_id='s1'").fetchone()[0]
        assert mid_s1 == before_s1, f"reader saw the session's docs missing mid-refresh: {mid_s1} != {before_s1}"
        assert reader.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == before_all
        reader.close()
    finally:
        release.set()
        t.join(30)
    assert not failures, failures
    after = sqlite3.connect(path).execute("SELECT COUNT(*) FROM docs WHERE session_id='s1'").fetchone()[0]
    assert after == before_s1, "idempotent re-index changed the session's docs on identical inputs"


def test_entity_stats_committed_before_begin_immediate(tmp_path, monkeypatch):
    """(b) build_fts = write_entity_stats (own COMMITTED txn) then BEGIN IMMEDIATE. Parked at
    tenant.config — which build_fts calls INSIDE the BEGIN IMMEDIATE block — a second connection
    must already see the REBUILT entity_stats rows. If write_entity_stats' commit ever moved
    inside the block, the reader would see the stale row instead (and the docs incident scarred
    in build_fts's docstring would re-open)."""
    path = str(tmp_path / "analyzer.db")
    con = store.sqlite_db(path)
    con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,user) "
                "VALUES ('s1','p','2026-07-01T00:00:00Z','2026-07-02T00:00:00Z','mara@example.com')")
    con.execute("INSERT INTO entities(id,type,name) VALUES ('E1','service','E-One')")
    con.execute("INSERT INTO mentions(session_id,entity_id) VALUES ('s1','E1')")
    con.execute("INSERT INTO entity_stats(entity_id,sessions) VALUES ('stale-x',99)")   # pre-rebuild junk
    con.commit()
    con.close()

    inside, read_done = threading.Event(), threading.Event()
    real_config = tenant.config

    def parked_config(c):
        inside.set()
        assert read_done.wait(10), "reader never ran"
        return real_config(c)

    monkeypatch.setattr(tenant, "config", parked_config)
    failures = []

    def writer():
        wcon = store.sqlite_db(path)
        try:
            store.build_fts(wcon)
        except Exception as e:                           # noqa: BLE001
            failures.append(e)
        finally:
            wcon.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert inside.wait(10), "build_fts never reached tenant.config — hook point moved, fix the test"
        reader = sqlite3.connect(path)                   # BEGIN IMMEDIATE is open on the writer right now
        reader.execute("PRAGMA busy_timeout=15000")
        ids = {r[0] for r in reader.execute("SELECT entity_id FROM entity_stats")}
        assert "E1" in ids, "write_entity_stats' rebuild was NOT committed before BEGIN IMMEDIATE started"
        assert "stale-x" not in ids, "stale entity_stats row still visible — the rebuild txn didn't commit"
        reader.close()
    finally:
        read_done.set()
        t.join(30)
    assert not failures, failures
