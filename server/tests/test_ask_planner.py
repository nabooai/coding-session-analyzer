"""The planner may still name an engine this server does not run (a model that saw Cypher in training
will emit it). plan_and_run returns it un-run as `fts-graphless`; the callers answer from lexical
recall and append DEGRADE_NOTE, never an error."""
from analyzer import ask, search, store


def _plan(monkeypatch, engine, query):
    monkeypatch.setattr(ask, "nl_to_query",
                        lambda question, con=None, model="sonnet": {"engine": engine, "query": query})


def test_cypher_plan_degrades_to_fts_graphless(monkeypatch):
    con = store.sqlite_db(":memory:")
    _plan(monkeypatch, "cypher", "MATCH (a:Entity)-[:REL]->(b:Entity) RETURN a.id")
    assert ask.plan_and_run(con, "what depends on graph.db") == (
        "fts-graphless", "MATCH (a:Entity)-[:REL]->(b:Entity) RETURN a.id", None, None)


def test_cli_ask_answers_a_cypher_plan_from_recall_with_the_note(monkeypatch, capsys):
    con = store.sqlite_db(":memory:")
    _plan(monkeypatch, "cypher", "MATCH (n) RETURN n")
    monkeypatch.setattr(search, "answer", lambda con, q, model="sonnet": ("prose from recall", []))
    kind, text, hits = ask.ask(con, "what depends on graph.db")
    assert kind == "answer"
    assert text.startswith("prose from recall") and text.endswith(ask.DEGRADE_NOTE)
    assert hits == []
    assert "[fts-graphless] MATCH (n) RETURN n" in capsys.readouterr().out


def test_sql_plan_still_runs(monkeypatch):
    con = store.sqlite_db(":memory:")
    _plan(monkeypatch, "sql", "SELECT COUNT(*) FROM sessions")
    assert ask.plan_and_run(con, "how many sessions") == (
        "sql", "SELECT COUNT(*) FROM sessions", ["COUNT(*)"], [(0,)])
