"""The facts-table CONTRACT, as pytest (schema v2, PR #70 — aligned to the P1 test layout).

These are the invariants the sibling fact-core tasks (resolution-engine, registries-verify,
taint-confidence-search-page) build on, ported from the authoritative `analyzer.store` __main__
self-check so they run in the same CI lane as the P1 suite. store.py itself is unmigrated until
P3, so its __main__ block remains the module gate; this file is the cross-PR tripwire — if a
later phase's store migration breaks a rule the siblings depend on, it goes red here by name.
"""

import pytest

from analyzer import store


S = dict(project="p", git_branch="b", n_user=1, n_asst=1)


def _load(con, sid, user, day, hcs):
    store.load_sqlite(con, dict(S, session_id=sid, user=user, started_at=f"{day}T09:00:00Z",
                                ended_at=f"{day}T10:00:00Z"), {"hidden_context": hcs})


@pytest.fixture()
def con():
    return store.sqlite_db(":memory:")


def test_one_fact_row_per_distinct_ref_and_first_session_author(con):
    claim = "the graph store moved from sqlite to duckdb"
    _load(con, "a", "alice@x", "2026-06-01", [{"type": "decision", "claim": claim, "evidence": "e1"}])
    _load(con, "b", "bob@x", "2026-07-01", [{"type": "decision", "claim": claim, "evidence": "e2"}])
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    author, created, valid = con.execute(
        "SELECT author, created_at, valid_at FROM facts").fetchone()
    assert (author, created) == ("alice@x", "2026-06-01T10:00:00Z")
    assert valid == created                              # no as_of -> valid_at defaults to created_at


def test_sibling_stage_columns_survive_reindex(con):
    """resolution-engine/registries-verify OWN invalid_at/expired_at/state/confidence — no
    backfill, reload, or full refresh may ever touch them, and edges are durable."""
    _load(con, "a", "u@x", "2026-06-01", [{"type": "decision", "claim": "keep sqlite", "evidence": "e"}])
    ref = store.hc_ref("keep sqlite")
    con.execute("UPDATE facts SET invalid_at='2026-07-01', expired_at='2026-07-02', "
                "state='superseded', confidence=0.7 WHERE ref=?", (ref,))
    con.execute("INSERT INTO fact_edges VALUES ('winner','supersedes',?,'2026-07-01','resolve','receipt')",
                (ref,))
    con.commit()
    _load(con, "a", "u@x", "2026-06-01", [{"type": "decision", "claim": "keep sqlite", "evidence": "e"}])
    store.refresh_facts(con)
    row = con.execute("SELECT invalid_at, expired_at, state, confidence FROM facts WHERE ref=?",
                      (ref,)).fetchone()
    assert row == ("2026-07-01", "2026-07-02", "superseded", 0.7)
    assert con.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0] == 1


def test_created_at_monotone_and_forget_recompute(con):
    """Normal path: a continuation session's later ended_at must NOT re-date the fact.
    Forget path (recompute=True): dates/author re-derive from the surviving mentions."""
    for sid, user, day in (("a", "ann@x", "2026-06-05"), ("b", "zed@x", "2026-07-05")):
        _load(con, sid, user, day, [{"type": "constraint", "claim": "prod needs a second reviewer",
                                     "evidence": "e"}])
    ref = store.hc_ref("prod needs a second reviewer")
    store.load_sqlite(con, dict(S, session_id="a", user="ann@x", started_at="2026-06-05T09:00:00Z",
                                ended_at="2026-06-20T10:00:00Z"),   # continuation moved forward
                      {"hidden_context": [{"type": "constraint", "claim": "prod needs a second reviewer",
                                           "evidence": "e"}]})
    assert con.execute("SELECT created_at FROM facts WHERE ref=?", (ref,)).fetchone()[0] \
        == "2026-06-05T10:00:00Z"                        # monotone: no silent re-date
    con.execute("DELETE FROM hidden_context WHERE session_id='a'")
    store.refresh_facts(con, refs={ref}, recompute=True)
    assert con.execute("SELECT author, created_at FROM facts WHERE ref=?", (ref,)).fetchone() \
        == ("zed@x", "2026-07-05T10:00:00Z")


def test_zero_mention_fact_removed_with_edges(con):
    _load(con, "a", "u@x", "2026-06-01", [{"type": "gotcha", "claim": "doomed fact", "evidence": "e"}])
    ref = store.hc_ref("doomed fact")
    con.execute("INSERT INTO fact_edges VALUES (?, 'supersedes', 'other', 't', 'x', 'n')", (ref,))
    con.commit()
    _load(con, "a", "u@x", "2026-06-01", [])             # re-extract drops the claim
    assert not con.execute("SELECT 1 FROM facts WHERE ref=?", (ref,)).fetchone()
    assert not con.execute("SELECT 1 FROM fact_edges WHERE src_ref=? OR dst_ref=?", (ref, ref)).fetchone()


def test_as_of_decouples_world_time(con):
    _load(con, "a", "u@x", "2026-07-10", [{"type": "gotcha", "claim": "wal grows unchecked",
                                           "evidence": "e", "as_of": "2026-05-02"}])
    created, valid = con.execute("SELECT created_at, valid_at FROM facts").fetchone()
    assert (created, valid) == ("2026-07-10T10:00:00Z", "2026-05-02")


@pytest.mark.parametrize("junk", ["last week", "2026", "2026-13-99", "2026-06-15 but maybe",
                                  "2026-03-15T99:99:99Z", "2026-03-15T....Z", "", None])
def test_as_of_trust_boundary_rejects(junk):
    assert store.valid_as_of(junk) is None


def test_docs_one_row_per_fact_with_validity_columns(con):
    claim = "the api strips request ids on retry"
    for sid, day in (("a", "2026-06-01"), ("b", "2026-07-01")):
        _load(con, sid, "u@x", day, [{"type": "gotcha", "claim": claim, "evidence": f"seen in {sid}"}])
    store.build_fts(con)
    rows = con.execute("SELECT session_id, first_at, started_at, invalid_at FROM docs "
                       "WHERE kind='hidden_context'").fetchall()
    assert rows == [("b", "2026-06-01T10:00:00Z", "2026-07-01T10:00:00Z", None)]
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH '\"seen in a\"'").fetchone()[0] == 1


def test_backfill_idempotent(con):
    for sid, day in (("a", "2026-06-01"), ("b", "2026-07-01")):
        _load(con, sid, "u@x", day, [{"type": "gotcha", "claim": f"fact from {sid}", "evidence": "e"}])
    store.refresh_facts(con)
    store.build_fts(con)
    snap = lambda: (con.execute("SELECT * FROM facts ORDER BY ref").fetchall(),
                    con.execute("SELECT doc_id, text, first_at, valid_at, invalid_at, state FROM docs "
                                "ORDER BY doc_id").fetchall())
    one = snap()
    store.refresh_facts(con)
    store.build_fts(con)
    assert snap() == one
