"""Entity-gate acceptance scenarios, formerly the module
__main__ self-check. Run from server/: uv run pytest tests/"""
import sqlite3
import time

import pytest

from analyzer import store
from analyzer.entity_gate import is_served_entity, served_ids


@pytest.fixture()
def corpus(monkeypatch):
    """Seeded in-memory corpus covering every floor/rescue clause."""
    monkeypatch.delenv("CSA_ENTITY_FLOOR", raising=False)
    monkeypatch.delenv("CSA_ENTITY_GC_DAYS", raising=False)
    con = store.sqlite_db(":memory:")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    old = "2026-01-01T00:00:00Z"                         # far past the 45d GC on any later run date
    mid = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 86400))  # past grace, inside GC
    ents = [("noise-old", "file"), ("noise-fresh", "file"), ("headline", "service"),
            ("recurring", "service"), ("acted-svc", "service"), ("sean-w", "person"),
            ("stale-dom", "service"), ("canon-x", "repo"), ("pinned-p", "file"),
            ("DUST_TT", "service"), ("Dust.tt", "service"), ("deliv-doc", "file"), ("bad-ts", "file")]
    con.executemany("INSERT INTO entities VALUES (?,?,?,NULL)", [(i, t, i) for i, t in ents])
    con.execute("UPDATE entities SET name='dust.tt' WHERE id IN ('DUST_TT','Dust.tt')")  # unmerged case-variants
    con.executemany(
        "INSERT INTO sessions(session_id, started_at, ended_at, user) VALUES (?,?,?,?)",
        [("s1", old, mid, "mara"), ("s2", now, now, "mara"), ("s3", old, old, "jules"),
         ("s4", "garbage-ts", "", None)])
    con.executemany(
        "INSERT INTO mentions(session_id, entity_id, dominant) VALUES (?,?,?)",
        [("s1", "noise-old", 0), ("s2", "noise-fresh", 0), ("s1", "headline", 1),
         ("s1", "recurring", 0), ("s2", "recurring", 0), ("s1", "acted-svc", 0),
         ("s1", "sean-w", 0), ("s3", "stale-dom", 1), ("s1", "canon-x", 0), ("s1", "pinned-p", 0),
         ("s1", "DUST_TT", 0), ("s3", "Dust.tt", 0), ("s1", "deliv-doc", 0), ("s4", "bad-ts", 0)])
    # alias rollup: a post-merge live drain wrote the ALIAS id into mentions for a second session
    con.execute("INSERT INTO aliases VALUES ('canon-x-alias','canon-x')")
    con.execute("INSERT INTO mentions(session_id, entity_id, dominant) VALUES ('s2','canon-x-alias',0)")
    # acted-on rescue: an acted_on fact in s1 references acted-svc by name
    con.execute("INSERT INTO hidden_context(session_id, type, claim, ref) VALUES "
                "('s1','gotcha','acted-svc times out under load','ref-acted')")
    con.execute("INSERT INTO signals VALUES ('ref-acted','acted_on','s1',?)", (now,))
    con.execute("INSERT INTO entity_types VALUES ('pinned-p','person')")   # HITL pin wins over stored type
    con.execute("INSERT INTO worked_on VALUES ('s1','rewrote deliv-doc export flow')")  # headline rescue
    store.write_entity_stats(con)
    return con


def test_floor_gates_stale_never_dominant_singleton(corpus):
    assert not is_served_entity(corpus, "noise-old")


def test_grace_window_serves_fresh_singleton(corpus):
    assert is_served_entity(corpus, "noise-fresh")


def test_dominant_singleton_stays_served(corpus):
    assert is_served_entity(corpus, "headline")


def test_two_session_entity_served_regardless_of_dominance(corpus):
    assert is_served_entity(corpus, "recurring")


def test_acted_on_fact_rescues(corpus):
    assert is_served_entity(corpus, "acted-svc")


def test_person_exemption(corpus):
    assert is_served_entity(corpus, "sean-w")


def test_hitl_person_pin_exempts(corpus):
    assert is_served_entity(corpus, "pinned-p")


def test_gc_demotes_stale_dominant_singleton(corpus):
    assert not is_served_entity(corpus, "stale-dom")


def test_alias_rollup_combines_sessions(corpus):
    assert is_served_entity(corpus, "canon-x")
    assert is_served_entity(corpus, "canon-x-alias")     # alias id resolves through the chain
    stats = dict(corpus.execute("SELECT entity_id, sessions FROM entity_stats").fetchall())
    assert stats["canon-x"] == 2 and "canon-x-alias" not in stats


def test_name_family_rescues_unmerged_case_variants(corpus):
    assert is_served_entity(corpus, "DUST_TT") and is_served_entity(corpus, "Dust.tt")


def test_headline_rescue_from_worked_on(corpus):
    assert is_served_entity(corpus, "deliv-doc")


def test_unknown_id_fails_open(corpus):
    assert is_served_entity(corpus, "never-heard-of-it")


def test_unparseable_timestamp_fails_open(corpus):
    assert is_served_entity(corpus, "bad-ts")


def test_served_ids_batch(corpus):
    got = served_ids(corpus, {"noise-old", "recurring", "ghost-relation-id"})
    assert got == {"recurring", "ghost-relation-id"}     # gates noise, keeps served + unknown


def test_stats_rebuild_is_idempotent(corpus):
    before = corpus.execute("SELECT * FROM entity_stats ORDER BY entity_id").fetchall()
    store.write_entity_stats(corpus)
    assert corpus.execute("SELECT * FROM entity_stats ORDER BY entity_id").fetchall() == before


def test_floor_off_serves_everything(corpus):
    corpus.execute("INSERT INTO tenant_config(id, entity_floor) VALUES (1, 'off')")
    assert is_served_entity(corpus, "noise-old")
    assert served_ids(corpus, {"noise-old"}) == {"noise-old"}


def test_tenant_config_row_decides_per_org(corpus):
    """Floor off for THIS org only, and back on, from the row alone."""
    corpus.execute("INSERT INTO tenant_config(id, entity_floor) VALUES (1, 'off')")
    assert is_served_entity(corpus, "noise-old")
    corpus.execute("UPDATE tenant_config SET entity_floor='default', entity_gc_days=10000")
    assert not is_served_entity(corpus, "noise-old")     # floor back on
    assert is_served_entity(corpus, "stale-dom")         # per-tenant GC widened: dominant singleton survives


def test_partial_schema_fails_open():
    bare = sqlite3.connect(":memory:")                   # no schema at all: the pinned S4 API must not raise
    assert is_served_entity(bare, "anything")
    assert served_ids(bare, {"a"}) == {"a"}


def test_gated_entity_absent_from_fts_but_durable(corpus):
    store.build_fts(corpus)
    fts_ids = {r[0] for r in corpus.execute("SELECT doc_id FROM docs WHERE kind='entity'")}
    assert "ent" + "noise-old" not in fts_ids
    assert "ent" + "headline" in fts_ids
    assert corpus.execute("SELECT COUNT(*) FROM entities WHERE id='noise-old'").fetchone()[0] == 1


def test_string_and_core_faces_agree(corpus):
    """Tripwire (PR #74 review): build_fts consumes the STRING face (gate_joins/served_cond)
    while every other consumer uses the Core face (gate_parts/is_served_entity). The two must
    never diverge: the entity-doc set after a rebuild == the set the Core face serves."""
    store.build_fts(corpus)
    fts_served = {r[0][3:] for r in corpus.execute("SELECT doc_id FROM docs WHERE kind='entity'")}
    all_ids = [r[0] for r in corpus.execute("SELECT id FROM entities")]
    core_served = {i for i in all_ids if is_served_entity(corpus, i)}
    assert fts_served == core_served, (
        f"faces diverged: fts-only={fts_served - core_served} core-only={core_served - fts_served}")
