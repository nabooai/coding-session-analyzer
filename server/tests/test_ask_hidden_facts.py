"""No tier of the ask ladder serves a fact recall hides: a reviewer's `wrong`, a closed validity window,
a superseded fact. Session-less /api/ask and MCP ask both default to the ladder.

Importing mcp_server opens `data/analyzer.db` in the cwd, so every test runs inside tmp_path."""
import re

import pytest

import apiserver
from analyzer import llm, store

OK, WRONG, INVALID, SUPERSEDED = "a0c0ffee01", "badc0ffee1", "dead0ffee1", "feed0ffee1"
HIDDEN = {WRONG: "s-wrong", INVALID: "s-inv", SUPERSEDED: "s-sup"}
QUERIES = ("duckdb generation pointer", "duckdb pointer qqq1", "duckdb ux qa hr",
           "which session talked about the duckdb generation pointer", *HIDDEN)


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _Ctx:
    def __init__(self, headers):
        self.request_context = type("RC", (), {"request": _Req(headers)})()


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no LLM in tests")))
    con = store.sqlite_db(store.org_db())
    for sid, ref, claim in (("s-ok", OK, "the duckdb generation pointer rolled twice mid-session"),
                            ("s-wrong", WRONG, "the duckdb generation pointer is safe to share across writers"),
                            ("s-inv", INVALID, "the duckdb generation pointer lives in the manifest file"),
                            ("s-sup", SUPERSEDED, "the duckdb generation pointer is kept in redis")):
        con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user,summary) VALUES "
                    "(?,'ledger','2026-08-07T00:00:00Z','2026-08-07T01:00:00Z','session','jules@example.com',?)",
                    (sid, f"summary of {sid}"))
        con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES (?,'gotcha',?,?,'')",
                    (sid, ref, claim))
        con.execute("INSERT INTO facts(ref,claim,type) VALUES (?,?,'gotcha')", (ref, claim))
        con.execute("INSERT INTO signals(ref,kind,session_id,at) VALUES (?,'judge_gold',?,'')", (ref, sid))
    con.execute("UPDATE facts SET invalid_at='2026-08-01T00:00:00Z' WHERE ref=?", (INVALID,))
    con.execute("UPDATE facts SET invalid_at='2026-08-02T00:00:00Z', state='superseded' WHERE ref=?", (SUPERSEDED,))
    con.execute("INSERT INTO fact_edges VALUES (?,'supersedes',?,'2026-08-02T00:00:00Z','resolve','r')",
                (OK, SUPERSEDED))
    con.commit()
    store.build_fts(con)
    assert apiserver.flag(con, WRONG, "wrong", flagger="reviewer@example.com")["ok"]
    yield con
    con.close()


def _leaks(text):
    return sorted(ref for ref, sid in HIDDEN.items() if f"id:{ref}" in text or f"`{sid}`" in text)


def test_api_ask_never_serves_a_hidden_fact_on_any_tier(corpus):
    tiers = set()
    for q in QUERIES:
        for strict in (False, True):
            out = apiserver.ask(corpus, q, asked_by="eve@example.com", strict=strict)
            assert out["ok"], out
            tiers.add(out["tier"])
            assert _leaks(out.get("context") or out.get("message") or "") == [], (q, strict, out)
    assert {"strict", "lenient", "locate", "facts"} <= tiers, tiers
    served = apiserver.ask(corpus, "duckdb ux qa hr", strict=False)
    assert served["tier"] == "facts" and f"id:{OK}" in served["context"], "an accepted fact is still served"


def test_mcp_ask_never_serves_a_hidden_fact(corpus, monkeypatch):
    import mcp_server
    monkeypatch.setattr(mcp_server, "_HTTP", True)
    ctx = _Ctx({"x-csa-org": store.DEFAULT_ORG, "x-csa-user": "eve@example.com"})
    for q in QUERIES:
        out = mcp_server.ask(q, ctx=ctx)
        assert _leaks(out) == [], (q, out)
    out = mcp_server.ask("duckdb ux qa hr", ctx=ctx)
    assert re.search(rf"id:{OK}\b", out), out
