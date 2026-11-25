"""Ports of three deleted __main__ assertions the #74 cold review found un-replaced:
(a) old-shape docs auto-upgrade on open — recall must not be dark after the upgrade;
(b) index_session's incremental dedup — ONE docs row per fact, union evidence;
(c) forget recompute drops a dead as_of (valid_at re-derives to created_at)."""

from analyzer import store
import apiserver


def _sess(sid, day, user="u@x", **kw):
    return dict(session_id=sid, project="p", git_branch=None, started_at=f"{day}T09:00:00Z",
                ended_at=f"{day}T10:00:00Z", n_user=1, n_asst=1, user=user, source="session", **kw)


def test_old_shape_docs_upgrades_on_open(tmp_path):
    path = str(tmp_path / "analyzer.db")
    con = store.sqlite_db(path)
    store.index_session(con, _sess("s1", "2026-07-01"),
                        {"summary": "upgrade fixture", "hidden_context": [
                            {"type": "gotcha", "claim": "upgrade probe claim", "evidence": "e"}]})
    # regress docs to the PRE-v2 shape (no first_at/valid_at/invalid_at/state) with stale content
    con.execute("DROP TABLE docs")
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(doc_id UNINDEXED, kind UNINDEXED, "
                "session_id UNINDEXED, project UNINDEXED, started_at UNINDEXED, etype UNINDEXED, "
                "source UNINDEXED, status UNINDEXED, corroboration UNINDEXED, "
                "text, tokenize='porter unicode61')")
    con.commit()
    con.close()
    con2 = store.sqlite_db(path)                         # the upgrade open: probe -> rebuild
    rows = con2.execute("SELECT text FROM docs WHERE docs MATCH 'upgrade'").fetchall()
    assert rows, "recall must NOT be dark after the old-shape docs upgrade open"
    con2.execute("SELECT first_at FROM docs LIMIT 0")    # new shape present (no OperationalError)


def test_index_session_incremental_dedup_one_row_union_evidence():
    con = store.sqlite_db(":memory:")
    claim = "shared incremental fact"
    store.index_session(con, _sess("sA", "2026-07-01"),
                        {"hidden_context": [{"type": "decision", "claim": claim,
                                             "evidence": "evidence-from-A"}]})
    store.index_session(con, _sess("sB", "2026-07-02"),
                        {"hidden_context": [{"type": "decision", "claim": claim,
                                             "evidence": "evidence-from-B"}]})
    ref = store.hc_ref(claim)
    docs = con.execute("SELECT text FROM docs WHERE doc_id IN "
                       "(SELECT 'hc'||h.rowid FROM hidden_context h WHERE h.ref=?)", (ref,)).fetchall()
    assert len(docs) == 1, f"one docs row per FACT on the incremental path, got {len(docs)}"
    assert "evidence-from-A" in docs[0][0] and "evidence-from-B" in docs[0][0], \
        "the fact's docs text must union every mention's evidence"
    # re-index converges (still one row, same text)
    store.index_session(con, _sess("sB", "2026-07-02"),
                        {"hidden_context": [{"type": "decision", "claim": claim,
                                             "evidence": "evidence-from-B"}]})
    again = con.execute("SELECT text FROM docs WHERE doc_id IN "
                        "(SELECT 'hc'||h.rowid FROM hidden_context h WHERE h.ref=?)", (ref,)).fetchall()
    assert again == docs


def test_forget_recompute_drops_dead_as_of():
    con = store.sqlite_db(":memory:")
    claim = "dead as_of fixture"
    store.load_sqlite(con, _sess("early", "2026-06-01", user="early@x"),
                      {"hidden_context": [{"type": "decision", "claim": claim, "evidence": "e",
                                           "as_of": "2026-05-01"}]})
    store.load_sqlite(con, _sess("late", "2026-07-01", user="late@x"),
                      {"hidden_context": [{"type": "decision", "claim": claim, "evidence": "e"}]})
    ref = store.hc_ref(claim)
    assert con.execute("SELECT valid_at FROM facts WHERE ref=?", (ref,)).fetchone()[0] == "2026-05-01"
    assert apiserver.forget(con, "early")["ok"]    # the as_of carrier leaves
    valid_at, created_at = con.execute(
        "SELECT valid_at, created_at FROM facts WHERE ref=?", (ref,)).fetchone()
    assert valid_at == created_at == "2026-07-01T10:00:00Z", \
        f"dead as_of must drop; valid_at re-derives to created_at, got {valid_at!r}/{created_at!r}"
