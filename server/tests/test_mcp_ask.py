"""The MCP `ask` tool answers a plain lookup down the same ladder as a non-strict /api/ask: the lenient
floor, then a labelled word match over facts, then a miss naming the words the corpus never saw.

Importing mcp_server opens `data/analyzer.db` in the cwd, so the import happens inside tmp_path."""
import json

import pytest

from analyzer import llm, store, tracer


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _Ctx:
    def __init__(self, headers):
        self.request_context = type("RC", (), {"request": _Req(headers)})()


@pytest.fixture()
def mcp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import mcp_server
    monkeypatch.setattr(mcp_server, "_HTTP", True)        # team mode: no cold fill from this machine's disk
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no LLM in tests")))
    con = store.sqlite_db(store.org_db())
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES "
                "('s1','ledger','2026-08-07T00:00:00Z','session','jules@example.com')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s1','gotcha','ra','the duckdb generation pointer rolled twice mid-session','')")
    con.commit()
    store.build_fts(con)
    con.close()
    ctx = _Ctx({"x-csa-org": store.DEFAULT_ORG, "x-csa-user": "eve@example.com"})
    return lambda q: mcp_server.ask(q, ctx=ctx)


def test_mcp_ask_falls_to_the_word_match_when_no_floor_answers(mcp):
    out = mcp("duckdb ux qa hr")
    assert out.startswith("# Recalled context for") and "id:ra" in out, out
    assert "no relevance floor was cleared" in out, out
    out = mcp("duckdb qqq1 qqq2 qqq3")
    assert "Words the corpus has never seen: qqq1, qqq2, qqq3." in out and "id:ra" not in out, out


def test_mcp_ask_misses_when_unseen_words_match_the_known_ones(mcp):
    out = mcp("what is our duckdb vacation")
    assert "Words the corpus has never seen: vacation." in out and "id:ra" not in out, out


def test_mcp_ask_misses_honestly(mcp):
    out = mcp("zzzqqq wwwxxx")
    assert "Words the corpus has never seen: wwwxxx, zzzqqq." in out, out
    assert "real miss, not an outage" in out, out


def test_mcp_ask_redacts_the_query_before_it_is_logged_or_traced(mcp, monkeypatch, caplog):
    traces = []
    monkeypatch.setattr(tracer, "_finish", traces.append)
    secret = "ghp_0123456789abcdef"
    mcp(f"duckdb pointer token {secret}")
    con = store.sqlite_db(store.org_db())
    logged = [r[0] for r in con.execute("SELECT query_text || ' ' || planned_query FROM queries")]
    con.close()
    assert len(logged) == 1 and secret not in logged[0] and "[REDACTED:github_token]" in logged[0], logged
    attrs = [json.dumps(s["attrs"]) for t in traces for s in t["buf"]]
    assert attrs and not any(secret in a for a in attrs), attrs
    assert secret not in caplog.text
