"""sqlite_db's cold-open schema DDL is single-flight per db path (P3, map T1).

Two threads opening the same file after a deploy (api request + etl tick) must not interleave
the connect, the WAL mode-switch and the schema upgrade — the PR #49-class post-deploy shape.
Deterministic
sync: `store._cold_open` is the seam that runs UNDER the per-path lock; parking a patched
_cold_open holds the lock at a known point while the test proves a second opener of the same
path cannot reach the DDL. No sleeps in the pass direction — a blocked thread cannot complete,
so the bounded waits only ever spend their timeout on a regression."""
import os
import sqlite3
import threading

from analyzer import store


def test_schema_lock_is_per_path(tmp_path):
    key_a = os.path.abspath(str(tmp_path / "a.db"))
    lock_a = store._schema_lock(key_a)
    assert store._schema_lock(key_a) is lock_a, "same path must map to the same lock object"
    assert store._schema_lock(os.path.abspath(str(tmp_path / "b.db"))) is not lock_a, \
        "different paths must not share a lock (an org's open must not serialize behind another's)"


def test_cold_open_ddl_is_single_flight(tmp_path, monkeypatch):
    path = str(tmp_path / "cold.db")
    real = store._cold_open
    inside, release, done2 = threading.Event(), threading.Event(), threading.Event()
    entered = []

    def parked(con, path):
        entered.append(threading.get_ident())
        inside.set()
        assert release.wait(10), "test never released the parked opener"
        return real(con, path)

    monkeypatch.setattr(store, "_cold_open", parked)
    t1 = threading.Thread(target=lambda: store.sqlite_db(path).close())
    t1.start()
    try:
        assert inside.wait(10), "sqlite_db never reached _cold_open — seam moved, fix the test"

        def opener2():
            store.sqlite_db(path).close()
            done2.set()

        t2 = threading.Thread(target=opener2)
        t2.start()
        # the whole point: while the first opener holds the per-path lock, a second opener of the
        # SAME path must not enter the DDL block (without the lock this completes in milliseconds)
        assert not done2.wait(0.3), "second cold-open ran the DDL while the first still held the lock"
        assert entered == [t1.ident], "second opener entered _cold_open concurrently"
    finally:
        release.set()
        t1.join(10)
    assert done2.wait(10), "second cold-open never finished after the lock was released"
    t2.join(10)
    assert len(entered) == 2, "both openers must reach the schema check, serialized"

    con = sqlite3.connect(path)                          # and the schema actually landed
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone()
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone()
    con.close()


def test_concurrent_cold_opens_all_usable(tmp_path):
    """Outcome guarantee, no hooks: N racing cold opens of one fresh file all return a usable
    connection with the full (ALTER-migrated) schema."""
    path = str(tmp_path / "race.db")
    errs = []

    def opener():
        try:
            con = store.sqlite_db(path)
            con.execute("SELECT session_id, harness, shipped_by FROM sessions").fetchall()
            con.execute("SELECT trace_id FROM costs").fetchall()     # ALTER-added column present
            con.close()
        except Exception as e:                           # noqa: BLE001 — any opener crash fails the test
            errs.append(e)

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errs, errs
