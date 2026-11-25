"""Ported verbatim from judge.py's __main__ self-check (deleted with the P2 migration):
array parsing, pending (no confidence gate, session scope), apply semantics, etl_pass,
stale candidates, human-vs-judge precedence."""
from analyzer import store
from analyzer.judge import _extract_array, _stale_candidates, apply, etl_pass, pending


def _seed():
    con = store.sqlite_db(":memory:")
    for i, conf in enumerate(("low", "high", "high", "high", "high")):
        con.execute("INSERT INTO hidden_context(session_id,type,claim,evidence,ref,confidence) "
                    "VALUES ('s1','gotcha',?,'e',?,?)", (f"c{i}", f"r{i}", conf))
    return con


def test_extract_array_ignores_fences_and_prose():
    a = _extract_array('noise ```json\n[{"ref":"r1","type":"gotcha","label":"gold"}]\n``` tail')
    assert a[0]["label"] == "gold", a


def test_pending_and_apply_semantics():
    con = _seed()
    assert len(pending(con)) == 5                  # high-confidence facts get quality-labeled too
    con.execute("INSERT INTO facts(ref, type) VALUES ('r0','gotcha')")   # durable row must track the retype
    n = apply(con, {"r0", "r1", "r2", "r3", "r4"}, [
        {"ref": "r0", "type": "decision", "label": "gold"},
        {"ref": "r1", "type": "gotcha", "label": "wrong"},
        {"ref": "r2", "type": "todo", "label": "unimportant"},
        {"ref": "r3", "type": "gotcha", "label": "unsure"},
        {"ref": "rX", "type": "gotcha", "label": "gold"},      # hallucinated ref -> ignored
        {"ref": "r4", "type": "bad", "label": "gold"},         # bad type -> unsure, stays queued
    ], "2026-07-13T00:00:00Z")
    assert (n["gold"], n["wrong"], n["unimportant"], n["unsure"]) == (1, 1, 1, 2), n
    assert not con.in_transaction      # regression: an open txn here held the db lock across 30s LLM calls
    assert con.execute("SELECT type FROM hc_types WHERE ref='r0'").fetchone()[0] == "decision"
    assert con.execute("SELECT type FROM facts WHERE ref='r0'").fetchone()[0] == "decision", \
        "judge retype must mirror into facts.type like set_hc_type does (cold-review catch)"
    assert con.execute("SELECT 1 FROM signals WHERE ref='r0' AND kind='judge_gold'").fetchone()
    assert con.execute("SELECT status,flagger FROM reviews WHERE ref='r1'").fetchone() == ("wrong", "judge")
    assert con.execute("SELECT status FROM reviews WHERE ref='r2'").fetchone()[0] == "unimportant"
    assert {f["ref"] for f in pending(con)} == {"r3", "r4"}    # unsure + bad-type stay in the human queue
    con.execute("INSERT INTO hidden_context(session_id,type,claim,evidence,ref) VALUES ('s2','gotcha','c9','e','r9')")
    assert {f["ref"] for f in pending(con, session="s2")} == {"r9"}   # auto-judge scope


def test_etl_pass_and_stale_candidates():
    con = _seed()
    apply(con, {"r0", "r1", "r2", "r3", "r4"}, [
        {"ref": "r0", "type": "decision", "label": "gold"},
        {"ref": "r1", "type": "gotcha", "label": "wrong"},
        {"ref": "r2", "type": "todo", "label": "unimportant"},
    ], "t")
    con.execute("INSERT INTO hidden_context(session_id,type,claim,evidence,ref) VALUES ('s2','gotcha','c9','e','r9')")
    con.execute("INSERT INTO claim_clusters VALUES ('r0','cl1',3)")   # rediscovered in 3 sessions
    con.execute("INSERT INTO claim_clusters VALUES ('r9','cl2',1)")   # single-session -> not reaffirmed
    n = etl_pass(con)
    assert n["reaffirmed"] == 1 and n["stale"] == 0 and n["clusters"] == 0, n
    assert con.execute("SELECT 1 FROM signals WHERE ref='r0' AND kind='reaffirmed'").fetchone()
    assert not con.execute("SELECT 1 FROM signals WHERE ref='r9' AND kind='reaffirmed'").fetchone()
    assert etl_pass(con)["reaffirmed"] == 0                           # idempotent re-run
    # _stale_candidates: multi-ref cluster surfaces oldest-first; fully-checked clusters drop out
    con.executescript("INSERT INTO sessions(session_id,started_at,source) VALUES ('sa','2026-01-01','session');"
                      "INSERT INTO sessions(session_id,started_at,source) VALUES ('sb','2026-06-01','session');"
                      "INSERT INTO hidden_context(session_id,type,claim,ref) VALUES ('sa','todo','deploy pending','rA');"
                      "INSERT INTO hidden_context(session_id,type,claim,ref) VALUES ('sb','todo','deploy done','rB');"
                      "INSERT INTO claim_clusters VALUES ('rA','cl3',1);"
                      "INSERT INTO claim_clusters VALUES ('rB','cl3',1);")
    c = _stale_candidates(con)
    assert [r for r, _c, _d in c["cl3"]] == ["rA", "rB"], c           # oldest first
    con.executemany("INSERT INTO signals VALUES (?,?,?,?)",
                    [("rA", "etl_checked", "etl", "t"), ("rB", "etl_checked", "etl", "t")])
    assert "cl3" not in _stale_candidates(con)                        # checked cluster never re-billed


def test_human_precedence_and_poisoned_batch_guard():
    con = _seed()
    apply(con, {"r1"}, [{"ref": "r1", "type": "gotcha", "label": "wrong"}], "t")
    con.execute("INSERT INTO hidden_context(session_id,type,claim,evidence,ref) VALUES ('s2','gotcha','c9','e','r9')")
    store.set_review(con, "hc", "r9", "gold", note="ui-flag", flagger="web-ui")     # human labels r9...
    apply(con, {"r9"}, [{"ref": "r9", "type": "gotcha", "label": "wrong"}], "t")    # ...judge arrives late
    assert con.execute("SELECT status,flagger FROM reviews WHERE ref='r9'").fetchone() == ("gold", "web-ui")
    refs = {f"z{i}" for i in range(6)}                                # a 6-fact batch coming back all-noise
    n = apply(con, refs, [{"ref": r, "type": "gotcha", "label": "noise"} for r in refs], "t")
    assert not any(n.values()), n                                     # guard: skipped whole
    assert not con.execute("SELECT 1 FROM reviews WHERE ref LIKE 'z%'").fetchone()
    store.clear_review(con, "hc", "r1")                               # human clears the judge's 'wrong'
    assert con.execute("SELECT 1 FROM signals WHERE ref='r1' AND kind='human_cleared'").fetchone()
    assert "r1" not in {f["ref"] for f in pending(con)}               # judge never re-labels it
    store.set_review(con, "hc", "rB", "gold", flagger="web-ui")
    store.clear_review(con, "hc", "rB")                               # clearing a HUMAN row leaves no marker
    assert not con.execute("SELECT 1 FROM signals WHERE ref='rB' AND kind='human_cleared'").fetchone()


def test_run_refreshes_only_the_judged_docs_rows(monkeypatch):
    """The ETL's per-session judge must not pay a full build_fts (write_entity_stats + DROP/CREATE docs, ~3 min
    on the prod corpus) for a handful of labels: it re-emits just the touched refs' fact docs rows, corpus-wide."""
    from analyzer import judge
    con = _seed()
    con.execute("INSERT INTO sessions(session_id, started_at, ended_at, user) "
                "VALUES ('s1','2026-07-01T00:00:00Z','2026-07-01T01:00:00Z','u')")
    store.build_fts(con)
    hc = {ref: rid for rid, ref in con.execute("SELECT rowid, ref FROM hidden_context")}
    assert con.execute("SELECT count(*) FROM docs WHERE kind='hidden_context'").fetchone()[0] == 5
    monkeypatch.setattr(judge, "judge_batch", lambda facts, model="haiku", **k: ([
        {"ref": "r0", "type": "decision", "label": "gold"},      # retype: etype + text prefix move
        {"ref": "r1", "type": "gotcha", "label": "wrong"},       # hidden: the row disappears
        {"ref": "r2", "type": "gotcha", "label": "gold"},        # accepted as-is: row re-emitted unchanged
    ], {"usage": {}, "cost_usd": 0.0}))
    monkeypatch.setattr(store, "build_fts",
                        lambda con: (_ for _ in ()).throw(AssertionError("full rebuild on the judge path")))
    n = judge.run(con, session="s1")
    assert (n["gold"], n["wrong"]) == (2, 1), n
    rows = {r[0]: r for r in con.execute("SELECT doc_id, etype, status, text FROM docs WHERE kind='hidden_context'")}
    assert f"hc{hc['r1']}" not in rows, "a 'wrong' fact loses its docs row"
    r0 = rows[f"hc{hc['r0']}"]
    assert r0[1] == "decision" and r0[3].startswith("decision: "), "a retype reaches the docs row"
    assert rows[f"hc{hc['r2']}"][1:3] == ("gotcha", None)
    assert len(rows) == 4 and {f"hc{hc['r3']}", f"hc{hc['r4']}"} <= set(rows), "untouched rows stay; no duplicates"
    assert not con.in_transaction


def test_run_without_labels_touches_no_docs(monkeypatch):
    from analyzer import judge
    con = _seed()
    con.execute("INSERT INTO sessions(session_id, started_at, ended_at, user) "
                "VALUES ('s1','2026-07-01T00:00:00Z','2026-07-01T01:00:00Z','u')")
    store.build_fts(con)
    before = con.execute("SELECT doc_id, etype, status, text FROM docs ORDER BY doc_id").fetchall()
    monkeypatch.setattr(judge, "judge_batch", lambda facts, model="haiku", **k: (
        [{"ref": "r0", "type": "gotcha", "label": "unsure"}], {"usage": {}, "cost_usd": 0.0}))
    monkeypatch.setattr(store, "refresh_fact_docs",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing moved, nothing to refresh")))
    assert judge.run(con, session="s1")["unsure"] == 1
    assert con.execute("SELECT doc_id, etype, status, text FROM docs ORDER BY doc_id").fetchall() == before
