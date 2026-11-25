"""Ported verbatim from signals.py's __main__ self-check (deleted with the P2 migration):
dominant + recalled + acted_on detection; queries.acted_on write-back (marker + hook paths),
negative case stays 0, idempotent re-index, backfill no-op."""
from analyzer import store
from analyzer.signals import backfill


def test_signals_end_to_end():
    con = store.sqlite_db(":memory:")
    claim = "pgbouncer transaction pooling breaks prepared statements"
    ref = store.hc_ref(claim)
    transcript = ("USER: payments-svc deploy is failing\n\n"
                  "ASSISTANT: payments-svc config drift; restarting payments-svc\n\n"
                  f'USER: # Recalled context for "payments"\n- {claim}  _(p/s0 · id:{ref})_\n\n'
                  "ASSISTANT: fixed: pgbouncer transaction pooling now avoids prepared statements")
    sess = dict(session_id="sX", project="p", git_branch=None, started_at=None, ended_at=None,
                n_user=2, n_asst=2, transcript=transcript)
    data = {"summary": "x",
            "entities": [{"id": "payments-svc", "type": "service", "name": "payments-svc"},
                         {"id": "unrelated-widget", "type": "file", "name": "unrelated-widget"}],
            "hidden_context": [{"type": "gotcha", "claim": claim, "evidence": "seen on payments-svc"}]}
    store.log_served(con, "sX", "payments", [(ref, "hidden_context", "p")])
    store.log_query(con, "u", "sX", "hook", "payments", "found=1", 1, 5)
    store.log_query(con, "u", "sX", "hook", "unrelated question", "found=0", 0, 5)
    store.index_session(con, sess, data)                 # index_session calls harvest
    assert con.execute("SELECT dominant FROM mentions WHERE session_id='sX' AND entity_id='payments-svc'").fetchone()[0] == 1
    assert con.execute("SELECT dominant FROM mentions WHERE entity_id='unrelated-widget'").fetchone()[0] == 0
    kinds = {k for (k,) in con.execute("SELECT kind FROM signals WHERE ref=?", (ref,))}
    assert kinds == {"dominant_entity", "recalled_in_session", "acted_on"}, kinds

    # write-back: the query that SERVED the acted-on ref flips; a query in the same session that
    # served nothing must NOT (an instrument that always reads 1 is worse than none).
    acted = dict(con.execute("SELECT query_text, acted_on FROM queries WHERE session='sX'"))
    assert acted == {"payments": 1, "unrelated question": 0}, acted

    # NEGATIVE: injected, never used — <2 distinctive claim tokens after the prompt.
    claim2 = claim
    t2 = ("USER: unrelated redis question\n\n"
          "ASSISTANT: bumped the redis maxmemory policy and moved on\n\n"
          "USER: thanks\n\nASSISTANT: done")
    sess2 = dict(sess, session_id="sN", transcript=t2)
    store.log_served(con, "sN", "unrelated redis question", [(store.hc_ref(claim2), "hidden_context", "p")])
    store.log_query(con, "u", "sN", "hook", "unrelated redis question", "found=1", 1, 5)
    store.index_session(con, sess2, data)
    assert con.execute("SELECT acted_on FROM queries WHERE session='sN'").fetchone()[0] == 0, "false positive"

    # HOOK path: the ambient injection arrives as an attachment ingest drops — anchor on the prompt.
    t3 = ("USER: connection pooler keeps dropping my prepared statements\n\n"
          "ASSISTANT: that's pgbouncer transaction pooling — prepared statements don't survive it\n\n"
          "USER: fixed\n\nASSISTANT: ok")
    sess3 = dict(sess, session_id="sH", transcript=t3)
    store.log_served(con, "sH", "connection pooler keeps dropping my prepared statements",
                     [(ref, "hidden_context", "p")])
    store.log_query(con, "u", "sH", "hook", "connection pooler keeps dropping my prepared statements",
                    "found=1", 1, 5)
    store.index_session(con, sess3, data)
    assert not con.execute("SELECT 1 FROM signals WHERE session_id='sH'").fetchone(), \
        "hook-path evidence must NOT enter `signals` — those rows feed search.py's rank boost"
    assert con.execute("SELECT acted_on FROM queries WHERE session='sH'").fetchone()[0] == 1, "hook path missed"

    # IDEMPOTENT: re-indexing must not double-count or flip a 1 back to 0.
    before = con.execute("SELECT COUNT(*), SUM(acted_on) FROM queries").fetchone()
    for s in (sess, sess2, sess3):
        store.index_session(con, s, data)
    assert con.execute("SELECT COUNT(*), SUM(acted_on) FROM queries").fetchone() == before

    assert backfill(con) == 0, "backfill must be a no-op once harvest already wrote back"
