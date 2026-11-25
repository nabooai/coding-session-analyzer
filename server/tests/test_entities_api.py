"""GET /api/entities and /api/entities/{id}: one row per canonical entity, live counts, and a search
that finds an entity by a name that only exists as an alias (merged away, re-created by a drain, or
two hops down a merge chain)."""
import pytest

import apiserver
from analyzer import store

ROW_KEYS = {"id", "name", "type", "mentions", "sessions", "facts", "last_seen", "served"}
FACT_KEYS = {"ref", "type", "claim", "evidence", "project", "session_id", "user", "started_at", "label",
             "flagger", "corroboration", "used", "dominant", "judge_gold"}


@pytest.fixture()
def con():
    c = store.sqlite_db(":memory:")
    for sid, user, day, source in (("s1", "mara@x", "01", "session"), ("s2", "jules@x", "02", "session"),
                                   ("s3", "mara@x", "03", "session"), ("s4", "mara@x", "04", "memory")):
        c.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) VALUES(?,?,?,?,?,?)",
                  (sid, "p", f"2026-09-{day}T09:00:00Z", f"2026-09-{day}T10:00:00Z", source, user))
    for sid, ref in (("s1", "r1"), ("s1", "r2"), ("s2", "r3"), ("s3", "r1"), ("s3", "r4"), ("s4", "r9")):
        c.execute("INSERT INTO hidden_context(session_id,type,claim,evidence,ref) VALUES(?,?,?,?,?)",
                  (sid, "gotcha", f"claim {ref}", "why", ref))
    for eid, ty in (("api-gateway", "service"), ("PAY-1", "ticket"), ("pay-1", "ticket"), ("Rin", "person")):
        c.execute("INSERT INTO entities(id,type,name) VALUES(?,?,?)", (eid, ty, eid))
    # gw was merged into api-gateway (its entities row is gone); apigw2 -> gw -> api-gateway is a chain;
    # pay-1 was merged into PAY-1 and a later drain re-created its entities row
    for a, canonical in (("gw", "api-gateway"), ("apigw2", "gw"), ("pay-1", "PAY-1")):
        c.execute("INSERT INTO aliases(alias_id,canonical_id) VALUES(?,?)", (a, canonical))
    for sid, eid in (("s1", "api-gateway"), ("s1", "PAY-1"), ("s1", "Rin"), ("s2", "gw"), ("s2", "pay-1"),
                     ("s3", "apigw2"), ("s3", "Rin"), ("s4", "api-gateway")):
        c.execute("INSERT INTO mentions(session_id,entity_id) VALUES(?,?)", (sid, eid))
    c.commit()
    return c


def by_id(page):
    return {r["id"]: r for r in page["rows"]}


def test_rows_are_canonical_with_live_counts(con):
    page = apiserver.entities_page(con)
    assert set(page) == {"total", "offset", "limit", "rows"}
    assert page["total"] == 3 and {r["id"] for r in page["rows"]} == {"api-gateway", "PAY-1", "Rin"}
    assert all(set(r) == ROW_KEYS for r in page["rows"])
    gw = by_id(page)["api-gateway"]
    assert gw["sessions"] == 3 and gw["mentions"] == 3, "alias and chain mentions roll up; the memory row does not count"
    assert gw["facts"] == 4, "distinct refs across s1, s2, s3 (r1 is shared by two of them)"
    assert gw["last_seen"] == "2026-09-03T10:00:00Z" and gw["type"] == "service"
    assert by_id(page)["PAY-1"]["sessions"] == 2 and by_id(page)["Rin"]["facts"] == 3
    assert page["rows"][0]["id"] == "api-gateway", "default sort is sessions desc"


def test_search_finds_the_canonical_through_every_alias_state(con):
    for q, want in (("gw", "api-gateway"), ("APIGW2", "api-gateway"), ("pay-1", "PAY-1"), ("rin", "Rin"),
                    ("gate", "api-gateway")):
        page = apiserver.entities_page(con, q=q)
        assert [r["id"] for r in page["rows"]] == [want], q
    assert apiserver.entities_page(con, q="nothing-here")["total"] == 0
    assert apiserver.entities_page(con, q="%")["total"] == 0, "LIKE wildcards are literal"


def test_type_owner_sort_and_paging(con):
    assert [r["id"] for r in apiserver.entities_page(con, etype="ticket")["rows"]] == ["PAY-1"]
    jules = apiserver.entities_page(con, owner="jules@x")
    assert {r["id"]: r["sessions"] for r in jules["rows"]} == {"api-gateway": 1, "PAY-1": 1}, \
        "owner scopes the counts, not just the rows"
    names = [r["id"] for r in apiserver.entities_page(con, sort="name", direction="asc")["rows"]]
    assert names == ["api-gateway", "PAY-1", "Rin"]
    assert [r["id"] for r in apiserver.entities_page(con, sort="junk", direction="junk")["rows"]][0] == "api-gateway"
    first = apiserver.entities_page(con, limit=2, offset=0)
    rest = apiserver.entities_page(con, limit=2, offset=2)
    assert first["total"] == 3 and len(first["rows"]) == 2 and len(rest["rows"]) == 1
    assert apiserver.entities_page(con, offset=99)["rows"] == []


def test_detail_by_alias_id_returns_the_canonical(con):
    d = apiserver.entity_detail(con, "apigw2")
    assert set(d) == {"entity", "sessions", "facts"}
    assert d["entity"]["id"] == "api-gateway" and d["entity"]["type"] == "service"
    assert [a["id"] for a in d["entity"]["aliases"]] == ["apigw2", "gw"]
    assert d["entity"]["aliases"][0]["name"] is None, "a merged-away alias has no entities row"
    assert [s["session_id"] for s in d["sessions"]] == ["s3", "s2", "s1"]
    assert {s["session_id"]: s["facts"] for s in d["sessions"]} == {"s1": 2, "s2": 1, "s3": 2}
    assert {f["ref"] for f in d["facts"]} == {"r1", "r2", "r3", "r4"}
    assert all(set(f) == FACT_KEYS for f in d["facts"])
    assert next(f for f in d["facts"] if f["ref"] == "r1")["corroboration"] == 2


def test_detail_of_an_unknown_id_is_none(con):
    assert apiserver.entity_detail(con, "nope") is None
    assert apiserver.entity_detail(con, "") is None


def test_served_only_filters_on_the_gate(con, monkeypatch):
    assert {r["served"] for r in apiserver.entities_page(con)["rows"]} == {1}, "floor off: everything is served"
    monkeypatch.setenv("CSA_ENTITY_FLOOR", "on")
    store.write_entity_stats(con)
    page = apiserver.entities_page(con, served_only=True)
    assert {r["id"] for r in page["rows"]} == {"api-gateway", "PAY-1", "Rin"}, \
        "multi-session entities and people pass the floor"
    assert apiserver.entities_page(con)["total"] == page["total"]


def test_paging_params_are_clamped_before_sqlite_sees_them(con):
    """A 20-digit offset used to raise OverflowError out of the sqlite3 binding and drop the connection."""
    assert apiserver._qint({"offset": ["99999999999999999999"]}, "offset", 0) == apiserver._QINT_MAX
    assert apiserver._qint({"offset": ["-5"]}, "offset", 0) == 0
    assert apiserver._qint({"offset": ["abc"]}, "offset", 7) == 7
    assert apiserver._qint({}, "limit", 50) == 50
    assert apiserver.entities_page(con, offset=apiserver._QINT_MAX)["rows"] == []
    assert apiserver.facts(con, offset=apiserver._QINT_MAX)["rows"] == []


def test_an_alias_cycle_does_not_hang_the_canonical_walk(con):
    """merge_entity cannot make one, but a hand-edited or half-applied merge could; the CTE is bounded."""
    con.execute("INSERT INTO aliases(alias_id,canonical_id) VALUES('loop-a','loop-b')")
    con.execute("INSERT INTO aliases(alias_id,canonical_id) VALUES('loop-b','loop-a')")
    con.execute("INSERT INTO entities(id,type,name) VALUES('loop-a','service','loop-a')")
    con.execute("INSERT INTO mentions(session_id,entity_id) VALUES('s1','loop-a')")
    con.commit()
    page = apiserver.entities_page(con, q="loop")
    assert page["total"] >= 1, "a cycle answers instead of hanging"
    assert apiserver.entity_detail(con, "loop-a") is not None
