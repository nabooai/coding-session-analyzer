"""entity_stats: the incremental ingest path must equal the full rebuild, and must not hold the writer.

Why this file exists (prod incident 2026-08-11): `write_entity_stats` used to take the SQLite writer
lock at its DELETE and then compute the aggregate while holding it — 81.8s per call on the prod corpus,
once per session indexed. SQLite allows exactly ONE writer per file, so every other write waited out
`busy_timeout=15000` and raised: the MCP `ask` tool died at `store.log_served` after 15.8s ("database is
locked") with the answer already computed, and the ambient-recall hook 500'd in the same windows. A live
probe found the writer unavailable 53% of the time.

Two properties are pinned here:
  1. PARITY — after an incremental refresh scoped to a session, `entity_stats` equals what a full
     rebuild would produce. This is what makes the scope safe; get the name-FAMILY or the ALIAS case
     wrong and it drifts silently (a wrong gate verdict, not an error).
  2. LOCK — the aggregate computes with the writer FREE, including when the caller arrives with an open
     ingest transaction (index_session does: load_sqlite + signals.harvest are pending writes).

Run from server/: uv run pytest tests/test_entity_stats_incremental.py
"""
import sqlite3
import time

import pytest

from analyzer import store

NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
OLD = "2026-01-01T00:00:00Z"


def _seed(con):
    """A corpus with one of every shape the stats aggregate cares about: a plain singleton, a recurring
    entity, an alias whose canonical id differs, an unmerged case-variant NAME family, an acted_on
    rescue and a headline rescue."""
    ents = [("noise", "file", "noise"), ("recurring", "service", "recurring"),
            ("canon-x", "repo", "canon-x"), ("DUST_TT", "service", "dust.tt"),
            ("Dust.tt", "service", "dust.tt"), ("acted-svc", "service", "acted-svc"),
            ("deliv-doc", "file", "deliv-doc")]
    con.executemany("INSERT INTO entities VALUES (?,?,?,NULL)", ents)
    con.executemany("INSERT INTO sessions(session_id, started_at, ended_at, user) VALUES (?,?,?,?)",
                    [("s1", OLD, OLD, "mara"), ("s2", NOW, NOW, "jules")])
    con.executemany("INSERT INTO mentions(session_id, entity_id, dominant) VALUES (?,?,?)",
                    [("s1", "noise", 0), ("s1", "recurring", 0), ("s2", "recurring", 0),
                     ("s1", "canon-x", 0), ("s1", "DUST_TT", 0), ("s2", "Dust.tt", 0),
                     ("s1", "acted-svc", 0), ("s1", "deliv-doc", 0)])
    con.execute("INSERT INTO aliases VALUES ('canon-x-alias','canon-x')")
    con.execute("INSERT INTO hidden_context(session_id, type, claim, ref) VALUES "
                "('s1','gotcha','acted-svc times out under load','ref-acted')")
    con.execute("INSERT INTO signals VALUES ('ref-acted','acted_on','s1',?)", (NOW,))
    con.execute("INSERT INTO worked_on VALUES ('s1','rewrote deliv-doc export flow')")
    con.commit()
    return con


def _arrive(con, sid="s3"):
    """A new session lands, the way index_session sees it: a brand-new entity, a repeat of an existing
    one, an ALIAS id (what a post-merge live drain writes), a member of the dust.tt name family, and
    text that triggers both rescues."""
    con.execute("INSERT INTO entities VALUES ('newthing','service','newthing',NULL)")
    con.execute("INSERT INTO sessions(session_id, started_at, ended_at, user) VALUES (?,?,?,?)",
                (sid, NOW, NOW, "hermes"))
    con.executemany("INSERT INTO mentions(session_id, entity_id, dominant) VALUES (?,?,?)",
                    [(sid, "newthing", 1), (sid, "recurring", 0), (sid, "canon-x-alias", 0),
                     (sid, "DUST_TT", 0), (sid, "acted-svc", 0)])
    con.execute("INSERT INTO hidden_context(session_id, type, claim, ref) VALUES "
                "(?,'gotcha','acted-svc still flaky','ref-acted2')", (sid,))
    con.execute("INSERT INTO signals VALUES ('ref-acted2','acted_on',?,?)", (sid, NOW))
    con.execute("INSERT INTO worked_on VALUES (?,'shipped newthing behind a flag')", (sid,))
    con.commit()


def _snapshot(con):
    return sorted(con.execute("SELECT * FROM entity_stats").fetchall())


def test_incremental_refresh_equals_full_rebuild():
    """THE contract. Scope the refresh to one session's entities, then rebuild the whole table: the two
    must agree row-for-row, or the gate is reading stats that no full `csa index` would ever produce."""
    con = _seed(store.sqlite_db(":memory:"))
    store.write_entity_stats(con)                            # steady state before the new session
    _arrive(con)

    scope = store.entity_scope_for_session(con, "s3")
    store.write_entity_stats(con, scope)
    incremental = _snapshot(con)

    store.write_entity_stats(con)                            # full rebuild over the same data
    assert incremental == _snapshot(con), "scoped refresh drifted from the full rebuild"


def test_scope_includes_the_name_family_and_canonicalizes_aliases():
    """The two easy-to-miss members of the scope. `name_sessions` is a rollup over the lowercased NAME,
    so a sibling's row moves when a family member gains a session — and a mention may carry an ALIAS id
    while the stats row lives under the canonical one."""
    con = _seed(store.sqlite_db(":memory:"))
    _arrive(con)
    scope = store.entity_scope_for_session(con, "s3")

    assert "canon-x" in scope and "canon-x-alias" not in scope, "alias ids must roll up to canonical"
    assert {"DUST_TT", "Dust.tt"} <= scope, "both members of the dust.tt name family are affected"
    assert "newthing" in scope and "recurring" in scope
    assert "noise" not in scope, "an entity this session never touched must NOT be rebuilt"


def test_family_sibling_row_actually_changes():
    """Guards the guard: if the family rollup could never move a sibling, the scope test above would be
    vacuous. Dust.tt gains a session only because its case-variant DUST_TT was mentioned."""
    con = _seed(store.sqlite_db(":memory:"))
    store.write_entity_stats(con)
    before = dict(con.execute("SELECT entity_id, name_sessions FROM entity_stats").fetchall())
    _arrive(con)
    store.write_entity_stats(con, store.entity_scope_for_session(con, "s3"))
    after = dict(con.execute("SELECT entity_id, name_sessions FROM entity_stats").fetchall())
    assert after["Dust.tt"] > before["Dust.tt"], "the sibling's family count must move with the family"


def test_a_dropped_mention_shrinks_the_row(tmp_path):
    """Nothing else in this file makes a count go DOWN. `load_sqlite` is DELETE+INSERT, so a
    re-extraction that no longer mentions an entity must still refresh it — computing the scope only
    AFTER the reload leaves a phantom row whose `sessions` never comes back down, and the entity gate
    keeps serving an entity the corpus no longer supports. index_session passes the pre-reload ids as
    `extra` for exactly this."""
    con = _seed(store.sqlite_db(str(tmp_path / "analyzer.db")))
    _arrive(con)
    store.write_entity_stats(con)                            # steady state INCLUDING s3's mentions
    assert dict(con.execute("SELECT entity_id, sessions FROM entity_stats"))["newthing"] == 1

    before_reload = store.mentioned_ids(con, "s3")           # what index_session captures first
    con.execute("DELETE FROM mentions WHERE session_id='s3'")   # the reload: newthing is gone from it
    con.executemany("INSERT INTO mentions(session_id, entity_id, dominant) VALUES (?,?,?)",
                    [("s3", "recurring", 0)])
    con.commit()

    store.write_entity_stats(con, store.entity_scope_for_session(con, "s3", extra=before_reload))
    incremental = _snapshot(con)
    store.write_entity_stats(con)
    assert incremental == _snapshot(con), "a dropped mention left a phantom entity_stats row"
    assert "newthing" not in dict(con.execute("SELECT entity_id, sessions FROM entity_stats")), \
        "an entity with no mentions left must lose its row entirely"


def test_empty_scope_is_a_no_op():
    con = _seed(store.sqlite_db(":memory:"))
    store.write_entity_stats(con)
    before = _snapshot(con)
    store.write_entity_stats(con, set())                     # a session with no mentions at all
    assert _snapshot(con) == before


def test_writer_lock_is_free_while_the_aggregate_computes(tmp_path, monkeypatch):
    """The incident itself, as a check. A second connection with busy_timeout=0 must be able to take the
    writer at the moment the aggregate runs — including when the caller arrives mid-transaction, which
    is exactly index_session's state (load_sqlite + signals.harvest are pending writes when it calls).

    Deterministic: the probe fires from inside the compute step, no sleeps, no threads. On the pre-fix
    shape this fails by construction — the DELETE ran first and held the writer for the whole aggregate
    (81.8s on prod)."""
    path = str(tmp_path / "corpus.db")
    con = _seed(store.sqlite_db(path))
    store.write_entity_stats(con)
    _arrive(con)

    probe = sqlite3.connect(path)
    probe.execute("PRAGMA busy_timeout=0")                   # no waiting: either free right now, or not
    seen = {}
    real = store._entity_stats_rows

    def spy(c, scope):
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            seen["free"] = True
        except sqlite3.OperationalError as e:
            seen["free"] = False
            seen["err"] = str(e)
        return real(c, scope)

    monkeypatch.setattr(store, "_entity_stats_rows", spy)
    con.execute("INSERT INTO mentions(session_id, entity_id, dominant) VALUES ('s3','noise',0)")
    assert con.in_transaction, "fixture must reproduce index_session's open-transaction arrival"

    store.write_entity_stats(con, store.entity_scope_for_session(con, "s3"))

    assert seen.get("free"), f"writer lock was HELD during the compute: {seen.get('err')}"
    probe.close()


@pytest.mark.parametrize("floor", ["1", "0"])
def test_scoped_entity_docs_match_a_full_rebuild(tmp_path, monkeypatch, floor):
    """index_session refreshes only the entity docs rows it touched, because the corpus-wide pass now
    runs at compaction. Those rows must be the ones a full build_fts would have written — with the gate
    on AND off (floor=off is a byte-identity contract)."""
    monkeypatch.setenv("CSA_ENTITY_FLOOR", floor)
    con = _seed(store.sqlite_db(str(tmp_path / "corpus.db")))
    store.build_fts(con)
    _arrive(con)
    scope = store.entity_scope_for_session(con, "s3")
    store.write_entity_stats(con, scope)

    store._refresh_entity_docs(con, scope)
    con.commit()
    incremental = sorted(con.execute("SELECT doc_id, text, etype FROM docs WHERE kind='entity'").fetchall())

    store.build_fts(con)                                     # the full pass over the same data
    assert incremental == sorted(
        con.execute("SELECT doc_id, text, etype FROM docs WHERE kind='entity'").fetchall())
    assert ("ent" + "newthing") in [d for d, _t, _e in incremental], "the new entity must be searchable"
