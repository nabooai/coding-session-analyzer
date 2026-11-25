"""GET /api/workspace/analytics (Admin > Analytics) and POST /api/track. Run from server/:
uv run pytest tests/test_analytics.py"""
import calendar
import json
import time

import pytest

import apiserver
from analyzer import analytics, auth, orgs, providers, queue, store, tenant

NOW = calendar.timegm((2026, 9, 5, 12, 0, 0))            # a Saturday noon UTC: today is Sep 5, the window ends Sep 6
CALM = {"stalled": False, "reason": ""}


def _iso(secs):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


def _days_ago(n, hour=9):
    return _iso(NOW - n * 86400 - (12 - hour) * 3600)


def _session(con, sid, ago, user, project="p"):
    con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) VALUES(?,?,?,?,?,?)",
                (sid, project, _days_ago(ago), _days_ago(ago, hour=10), "session", user))


def _fact(con, sid, ref, ftype="decision"):
    con.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref,confidence) VALUES(?,?,?,?,?,?,?)",
                (sid, ftype, "shared", f"claim {ref}", "why", ref, "high"))


def _served(con, ref, session, ago, asked_by):
    con.execute("INSERT INTO tel.served(session,asked_at,question,ref,kind,project,person,asked_by,trace_id) "
                "VALUES(?,?,'q',?,'hidden_context','p',NULL,?,'t')", (session, _days_ago(ago), ref, asked_by))


def _query(con, ago, engine, rows, acted=0, session="s", who="a@x", text="q"):
    con.execute("INSERT INTO tel.queries(at,asked_by,session,engine,query_text,planned_query,row_count,latency_ms,acted_on) "
                "VALUES(?,?,?,?,?,'q',?,5,?)", (_days_ago(ago), who, session, engine, text, rows, acted))


@pytest.fixture()
def corpus(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                           # the queue counts read data/pending under here
    con = store.sqlite_db(":memory:")
    yield con
    con.close()


def _view(con, weeks=1, tokens=(), stall=CALM):
    return analytics.workspace(con, store.DEFAULT_ORG, list(tokens), weeks, stall, now=NOW)


def test_the_window_is_whole_weeks_ending_today_with_one_bucket_per_week(corpus):
    v = _view(corpus, weeks=4)
    assert v["window"] == {"weeks": 4, "since": "2026-08-09", "until": "2026-09-06", "previous_since": "2026-07-12"}
    assert v["week_starts"] == ["2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30"]
    assert len(v["adoption"]["per_week"]) == len(v["recall"]["per_week"]) == len(v["memory"]["per_week"]) == 4
    assert len(v["pipeline"]["spend_per_day"]) == 28
    assert v["pipeline"]["spend_per_day"][-1]["day"] == "2026-09-05"


def test_adoption_counts_sessions_and_the_people_who_shipped_them(corpus):
    for sid, ago, user in (("a1", 0, "A@x"), ("a2", 2, "a@x"), ("b1", 6, "b@x"), ("c1", 9, "c@x"), ("a3", 12, "a@x"),
                           ("old", 30, "d@x")):
        _session(corpus, sid, ago, user)
    corpus.execute("INSERT INTO sessions(session_id,started_at,source,user) VALUES('memo',?,'memory','m@x')", (_days_ago(1),))
    a = _view(corpus)["adoption"]
    assert (a["sessions"], a["sessions_prev"]) == (3, 2), "a memory row is not a shipped session"
    assert (a["contributors"], a["contributors_prev"]) == (2, 2), "people are counted case-blind"
    assert a["per_week"] == [{"sessions": 3, "people": 2}]
    a4 = _view(corpus, weeks=4)["adoption"]
    assert a4["per_week"] == [{"sessions": 0, "people": 0}, {"sessions": 0, "people": 0},
                              {"sessions": 2, "people": 2}, {"sessions": 3, "people": 2}]


def test_members_split_into_shipping_quiet_and_never_connected(corpus):
    _session(corpus, "s1", 1, "ana@x")
    _session(corpus, "s2", 1, "relay-bot@x")
    tokens = [
        {"email": "ana@x", "hostname": "ana-mac", "revoked_at": None, "service": False},
        {"email": "Ben@x", "hostname": "ben-box", "revoked_at": None, "service": False},
        {"email": "ben@x", "hostname": "web-sso", "revoked_at": None, "service": False},
        {"email": "cy@x", "hostname": "web-sso", "revoked_at": None, "service": False},
        {"email": "gone@x", "hostname": "gone-box", "revoked_at": "2026-09-01T00:00:00Z", "service": False},
        {"email": "relay-bot@x", "hostname": "ci", "revoked_at": None, "service": True},
    ]
    for t in tokens:
        t.update(created=_days_ago(1), last_used_at=_days_ago(1), active=t["revoked_at"] is None)
    tokens.append({"email": "old@x", "hostname": "old-box", "revoked_at": None, "service": False,
                   "created": _days_ago(90), "last_used_at": _days_ago(60), "active": False})
    a = _view(corpus, tokens=tokens)["adoption"]
    assert a["members"] == 3, "revoked, expired and service credentials are not members; one person is one member"
    assert (a["members_shipping"], a["members_quiet"], a["members_unconnected"]) == (1, 1, 1)
    assert a["members_shipping"] + a["members_quiet"] + a["members_unconnected"] == a["members"]
    assert (a["contributors"], a["agents"], a["agent_sessions"]) == (1, 1, 1), \
        "an address that only holds a service token is an agent, never a contributor"
    _session(corpus, "s3", 40, "gone@x")
    a = _view(corpus, tokens=tokens)["adoption"]
    assert (a["members"], a["shipped_without_credential"]) == (3, 1), \
        "Team lists every person who ever shipped plus every member; the agent is counted apart"


def _tokens():
    cc = orgs.control_db(":memory:")
    for email, host, used, revoked in (("jules@x", "jules-laptop", 14, None), ("tomasz@x", "tomasz-lab", 41, None),
                                       ("mara@x", "mara-mac", 0.1, None), ("mara@x", "mara-old", 60, 30),
                                       ("jules@x", "web", 20, None), ("zed@x", "web-sso", 20, None), ("gone@x", "gone-box", 20, 1)):
        orgs.mint_token(email, store.DEFAULT_ORG, host, con=cc)
        cc.execute("UPDATE user_tokens SET last_used_at=?, revoked_at=? WHERE hostname=? AND email=?",
                   (_iso(NOW - used * 86400), _iso(NOW - revoked * 86400) if revoked else None, host, email))
    cc.execute("UPDATE user_tokens SET expires_at=? WHERE hostname='tomasz-lab'", (_iso(NOW - 11 * 86400),))
    orgs.mint_token("fresh@x", store.DEFAULT_ORG, "never-used", con=cc)
    cc.execute("UPDATE user_tokens SET created=? WHERE hostname='never-used'", (_iso(NOW - 8 * 86400),))
    cc.commit()
    return orgs.list_tokens(store.DEFAULT_ORG, con=cc)


def test_silent_is_non_browser_unrevoked_and_older_than_seven_days():
    silent = analytics.silent_machines(_tokens(), NOW)
    assert [(r["user"], r["hostname"], r["expired"]) for r in silent] == \
        [("tomasz@x", "tomasz-lab", True), ("jules@x", "jules-laptop", False), ("fresh@x", "never-used", False)], \
        "oldest first; browser tokens, revoked tokens and a machine seen today are out; an expired one stays, marked"
    assert silent[0]["last_used_at"] == _iso(NOW - 41 * 86400)
    assert silent[2]["last_used_at"] == _iso(NOW - 8 * 86400), "never used: minted counts as the last contact"


def test_a_machine_is_silent_only_when_every_credential_it_holds_is():
    tokens = _tokens() + [
        {"email": "jules@x", "hostname": "jules-laptop", "created": _iso(NOW - 90 * 86400),
         "last_used_at": _iso(NOW - 2 * 86400), "revoked_at": None, "active": True},
        {"email": "fresh@x", "hostname": "never-used", "created": _iso(NOW - 8 * 86400),
         "last_used_at": None, "revoked_at": None, "active": True},
    ]
    rows = [(r["user"], r["hostname"]) for r in analytics.silent_machines(tokens, NOW)]
    assert rows == [("tomasz@x", "tomasz-lab"), ("fresh@x", "never-used")], \
        "a second credential used 2 days ago means the machine is live; one row per machine"


def test_a_machine_is_expired_only_when_none_of_its_credentials_works():
    tokens = [
        {"email": "ana@x", "hostname": "ana-box", "created": _iso(NOW - 60 * 86400),
         "last_used_at": _iso(NOW - 40 * 86400), "revoked_at": None, "active": False},
        {"email": "ana@x", "hostname": "ana-box", "created": _iso(NOW - 30 * 86400),
         "last_used_at": _iso(NOW - 20 * 86400), "revoked_at": None, "active": True},
    ]
    assert [(r["hostname"], r["expired"], r["last_used_at"]) for r in analytics.silent_machines(tokens, NOW)] == \
        [("ana-box", False, _iso(NOW - 20 * 86400))]


def test_memory_growth_is_new_facts_by_type_per_week_and_who_contributed(corpus):
    _session(corpus, "s1", 0, "ana@x")
    _session(corpus, "s2", 3, "ben@x")
    _session(corpus, "s3", 10, "ana@x")
    _fact(corpus, "s1", "r1")
    _fact(corpus, "s1", "r2", "gotcha")
    _fact(corpus, "s2", "r3", "gotcha")
    _fact(corpus, "s2", "r4", "playbook")
    _fact(corpus, "s3", "r5")
    corpus.commit()
    store.refresh_facts(corpus)
    m = _view(corpus)["memory"]
    assert (m["total"], m["new"], m["new_prev"]) == (5, 4, 1)
    assert m["types"] == ["decision", "gotcha", "playbook"], "palette types first, in palette order, then the rest"
    assert m["per_week"] == [[1, 2, 1]]
    assert m["contributors"] == [{"user": "ana@x", "facts": 2, "reached": 0, "agent": False},
                                 {"user": "ben@x", "facts": 2, "reached": 0, "agent": False}]


def test_recall_splits_ambient_prompts_from_deliberate_asks(corpus):
    for ago, engine, rows, acted in ((0, "hook", 3, 1), (1, "hook", 0, 0), (2, "hook", 0, 0), (3, "fts", 4, 1),
                                     (4, "fallback", 0, 0), (5, "session", 2, 0), (9, "hook", 1, 0), (10, "fts", 0, 0)):
        _query(corpus, ago, engine, rows, acted)
    r = _view(corpus)["recall"]
    assert (r["asks"], r["answered"], r["acted"]) == (6, 3, 2)
    assert r["ambient"] == {"asks": 3, "answered": 1} and r["asked"] == {"asks": 3, "answered": 2}
    assert (r["asks_prev"], r["answered_prev"]) == (2, 1)
    assert r["per_week"] == [{"asks": 6, "answered": 3, "acted": 2}]


def test_acted_on_is_measured_only_over_answers_from_shipped_sessions(corpus):
    _query(corpus, 0, "hook", 3, 0, session="live-only")
    assert _view(corpus)["recall"]["measured"] == 0, "no answered request came from a session CSA holds"
    _session(corpus, "shipped", 0, "a@x")
    _query(corpus, 0, "hook", 2, 1, session="shipped")
    _query(corpus, 0, "hook", 0, 0, session="shipped")
    r = _view(corpus)["recall"]
    assert (r["answered"], r["measured"], r["acted"]) == (2, 1, 1)


def test_the_recall_log_lists_the_rows_the_count_counts(corpus):
    for ago, engine, rows, who, text in ((0, "hook", 3, "a@x", "deploy order"), (1, "hook", 0, "b@x", "b private miss"),
                                         (2, "fts", 0, "a@x", "my own miss"), (3, "fts", 2, "b@x", "billing retries")):
        _query(corpus, ago, engine, rows, who=who, text=text)
    log = analytics.recall_log(corpus, viewer="A@x")
    assert (log["total"], log["answered"]) == (4, 2)
    assert log["total"] == _view(corpus)["recall"]["asks"], "the log's total is the Analytics count"
    assert [(r["question"], r["results"], r["automatic"], r["mine"]) for r in log["rows"]] == [
        ("deploy order", 3, True, True), (None, 0, True, False), ("my own miss", 0, False, True),
        ("billing retries", 2, False, False)], "newest first; a teammate's miss keeps its text private"
    assert "asked_by" not in log["rows"][0], "no row names who asked"
    assert [bool(r["session"]) for r in log["rows"]] == [True, False, True, True], \
        "a teammate's miss hides its session too: the session's owner is the asker"
    short = analytics.recall_log(corpus, limit=2)
    assert (len(short["rows"]), short["total"]) == (2, 4)


def test_the_recall_log_names_the_channel_a_request_came_through(corpus):
    for ago, engine, rows, session in ((0, "hook", 1, "s1"), (1, "hook", 0, ""), (2, "fallback", 0, ""),
                                       (3, "fts", 0, ""), (4, "hook", 0, "s2")):
        _query(corpus, ago, engine, rows, session=session, who="b@x", text="private")
    rows = analytics.recall_log(corpus, viewer="a@x")["rows"]
    assert [r["source"] for r in rows] == ["hook", "api", "api", "mcp", "hook"]
    assert all(r["question"] is None and r["session"] == "" for r in rows[1:]), "a teammate's miss stays private"


def test_reuse_is_a_fact_reaching_someone_other_than_its_author(corpus):
    _session(corpus, "origin", 20, "ana@x")
    _session(corpus, "echo", 19, "ben@x")                # ben re-asserts the fact: his session produced it too
    for sid, ago, user in (("ben1", 2, "ben@x"), ("ana2", 1, "ana@x"), ("cy1", 1, "cy@x"), ("old", 9, "dee@x")):
        _session(corpus, sid, ago, user)
    _fact(corpus, "origin", "r1")
    _fact(corpus, "echo", "r1")
    _fact(corpus, "origin", "r2", "gotcha")
    _fact(corpus, "origin", "r3")
    corpus.commit()
    store.refresh_facts(corpus)
    _served(corpus, "r1", "origin", 0, "ana@x")         # into a session that produced it: not reuse
    _served(corpus, "r1", "echo", 0, "ben@x")           # ditto, though ben is not the author
    _served(corpus, "r1", "ben1", 2, "Ben@x")
    _served(corpus, "r1", "ben1", 1, "ben@x")           # the same fact into the same session again: one delivery
    _served(corpus, "r1", "ana2", 1, "ana@x")           # the author's own later session: not reuse
    _served(corpus, "r1", "cy1", 1, "")                 # no asking credential: the receiving session's owner
    _served(corpus, "r2", "ben1", 0, "ben@x")
    _served(corpus, "r3", "old", 9, "dee@x")            # the previous window
    _served(corpus, "r1", "ghost", 0, "")               # nobody known received it: not reuse
    _served(corpus, "r1", "live", 0, "eve@x")           # a session the corpus never received: still reuse
    _served(corpus, "r1", "", 0, "fay@x")               # no session at all: no delivery to count
    r = _view(corpus)["recall"]
    assert r["transfer"] == {"facts": 2, "facts_prev": 1, "facts_all_time": 3, "deliveries": 4,
                             "receivers": 3, "receiving_agents": 0, "contributors": 1, "per_week": [4]}
    assert r["transfer"]["facts_all_time"] == analytics.reused_facts(corpus) == apiserver.onboarding(corpus)["metrics"]["reused"], \
        "Welcome's tile and Analytics' all-time figure are one query"
    assert [(t["ref"], t["people"], t["sessions"], t["author"], t["session_id"]) for t in r["top_reused"]] == \
        [("r1", 3, 3, "ana@x", "origin"), ("r2", 1, 1, "ana@x", "origin")]
    assert r["top_reused"][0]["last_served"] == _days_ago(0)
    assert _view(corpus)["memory"]["contributors"] == [{"user": "ana@x", "facts": 0, "reached": 3, "agent": False}], \
        "an author whose facts reached people this window is a contributor even with nothing new"


def test_reuse_needs_a_known_author(corpus):
    corpus.execute("INSERT INTO sessions(session_id,started_at,source,user) VALUES('anon',?,'session',NULL)", (_days_ago(20),))
    _session(corpus, "ben1", 1, "ben@x")
    _fact(corpus, "anon", "r1")
    corpus.commit()
    store.refresh_facts(corpus)
    _served(corpus, "r1", "ben1", 0, "ben@x")
    v = _view(corpus)
    assert (v["recall"]["transfer"]["facts"], v["recall"]["transfer"]["deliveries"]) == (0, 0)
    assert v["recall"]["top_reused"] == []
    assert v["memory"]["contributors"] == []


def test_pipeline_reads_this_workspace_queue_and_ledger_against_the_cap(corpus):
    for at, sid, usd in (("2026-09-05 08:00:00", "s1", 1.5), ("2026-09-05 09:00:00", "s1", 0.5),
                         ("2026-09-03 23:59:59", "s2", 0.25), ("2026-08-25 10:00:00", "s0", 4.0),
                         ("2026-09-04 10:00:00", None, 0.75)):
        corpus.execute("INSERT INTO costs(at,step,model,session_id,cost_usd) VALUES(?,'extract','m',?,?)", (at, sid, usd))
    corpus.execute("INSERT INTO tenant_config(id, daily_usd) VALUES(1, 1.0)")
    corpus.commit()
    queue_dir = queue._dir(store.DEFAULT_ORG)
    (queue_dir / "q1.json").write_text("{}")
    (queue_dir / "poison").mkdir()
    (queue_dir / "poison" / "q2.json").write_text("{}")
    _session(corpus, "s1", 0, "ana@x")
    p = _view(corpus, stall={"stalled": True, "reason": "Invalid API key"})["pipeline"]
    assert (p["backlog"], p["poisoned"], p["stalled"], p["reason"]) == (1, 1, True, "Invalid API key")
    assert (p["spend_usd"], p["spend_prev"], p["extracted"]) == (3.0, 4.0, 2)
    assert p["per_extracted"] == 1.5 and p["per_contributor"] == 3.0
    assert p["daily_cap_usd"] == 1.0 and p["days_over_cap"] == 1, "Sep 5 spent 2.00 against a 1.00 cap"
    assert [d["usd"] for d in p["spend_per_day"]] == [0, 0, 0, 0, 0.25, 0.75, 2.0]
    assert (p["unpriced"], p["tokens"]) == (False, 0), "a priced call with no token counts is not unpriced"


def test_an_unpriced_call_shows_its_tokens_and_no_per_unit_cost(corpus):
    for at, sid, tin, tout, usd in (("2026-09-05 08:00:00", "s1", 800_000, 400_000, None),
                                    ("2026-09-04 08:00:00", "s2", 1000, 500, 0.25),
                                    ("2026-09-04 09:00:00", "s2", None, None, None)):
        corpus.execute("INSERT INTO costs(at,step,model,session_id,input_tokens,output_tokens,cost_usd) "
                       "VALUES(?,'extract','m',?,?,?,?)", (at, sid, tin, tout, usd))
    _session(corpus, "s1", 0, "ana@x")
    p = _view(corpus)["pipeline"]
    assert (p["unpriced"], p["tokens"], p["spend_usd"]) == (True, 1_201_500, 0.25), \
        "the provider reported tokens but no price: the spend is a floor, not the cost"
    assert p["per_extracted"] is None and p["per_contributor"] is None, "an unpriced window claims no cost per unit"
    assert [d["tokens"] for d in p["spend_per_day"]][-2:] == [1500, 1_200_000]


def test_a_call_billed_before_its_model_was_priced_is_priced_on_read(corpus):
    """The rows an on-prem Gemini box wrote before Gemini had a list price: the role, tokens, no cost."""
    cc = orgs.control_db()
    providers.save(providers.Provider(id="gemini", name="Gemini", kind="openai", endpoint="", region="",
                                      project="", api_version="", credential="k", models=("gemini-2.5-flash",)), "a@x", cc)
    tenant.set_deployment(cc, "a@x", main_model="gemini/gemini-2.5-flash", small_model="gemini/gemini-2.5-flash")
    cc.close()
    for at, model, tin, cached, tout, usd in (("2026-09-05 09:26:01", "main", 3944, 1018, 2853, None),
                                              ("2026-09-05 09:26:12", "small", 1135, None, 376, None),
                                              ("2026-09-05 12:47:32", "gemini-2.5-flash", 1297, None, 289, 0.001112),
                                              ("2026-08-25 10:00:00", "main", 1_000_000, None, 0, None)):
        corpus.execute("INSERT INTO costs(at,step,model,session_id,input_tokens,cache_read_tokens,output_tokens,cost_usd) "
                       "VALUES(?,'extract-full',?,'s1',?,?,?,?)", (at, model, tin, cached, tout, usd))
    p = _view(corpus)["pipeline"]
    owed = (2926 * 0.30 + 1018 * 0.03 + 2853 * 2.50) / 1e6 + (1135 * 0.30 + 376 * 2.50) / 1e6 + 0.001112
    assert p["unpriced"] is False and p["spend_per_day"][-1]["usd"] == round(owed, 4)
    assert p["spend_prev"] == 0.3, "the previous window prices the same way"


def test_a_model_csa_cannot_price_still_reads_as_unpriced(corpus):
    corpus.execute("INSERT INTO costs(at,step,model,session_id,input_tokens,output_tokens,cost_usd) "
                   "VALUES('2026-09-05 09:00:00','judge','gemini-9-ultra',NULL,500,100,NULL)")
    p = _view(corpus)["pipeline"]
    assert (p["unpriced"], p["spend_usd"], p["tokens"]) == (True, 0, 600)


def test_web_usage_counts_only_the_window(corpus):
    for ago, user, event, page in ((0, "a@x", "page_view", "memory"), (1, "b@x", "page_view", "memory"),
                                   (2, "a@x", "page_view", "admin"), (3, "a@x", "search", "memory"),
                                   (4, "b@x", "flag", "memory"), (5, "b@x", "session_open", "sessions"),
                                   (9, "c@x", "page_view", "memory")):
        corpus.execute("INSERT INTO tel.ui_events(at,user,event,page,meta) VALUES(?,?,?,?,NULL)", (_days_ago(ago), user, event, page))
    assert _view(corpus)["web"] == {"people": 2, "searches": 1, "session_opens": 1, "flags": 1,
                                    "pages": [{"page": "memory", "n": 2}, {"page": "admin", "n": 1}]}
    assert _view(corpus, weeks=4)["web"]["people"] == 3


def test_an_empty_workspace_is_all_zero_not_a_500(corpus):
    v = _view(corpus, weeks=12)
    assert v["adoption"]["sessions"] == v["memory"]["total"] == v["recall"]["asks"] == 0
    assert v["recall"]["transfer"]["facts"] == v["recall"]["measured"] == 0 and v["recall"]["top_reused"] == []
    assert v["memory"]["types"] == [] and v["memory"]["per_week"] == [[]] * 12
    assert v["pipeline"]["per_extracted"] is None and v["pipeline"]["per_contributor"] is None
    assert v["pipeline"]["days_over_cap"] == 0, "no cap configured means no day is over it"
    assert (v["pipeline"]["unpriced"], v["pipeline"]["tokens"]) == (False, 0)


# --- the route: admins only, and only their own workspace -----------------------------------------

class _Http:
    def __init__(self, path, token):
        self.path = path
        self.headers = auth._CIHeaders({"Authorization": f"Bearer {token}"})
        self.sent = None

    def _send(self, code, body, ctype="application/json", headers=()):
        self.sent = (code, body)

    def _401(self, err=None):
        self.sent = (401, {"ok": False, "error": err})

    _403 = apiserver.Handler._403
    _org_admin_guard = apiserver.Handler._org_admin_guard
    do_GET = apiserver.Handler.do_GET


@pytest.fixture()
def two_orgs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    for k in ("CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
        monkeypatch.delenv(k, raising=False)
    cc = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: cc)
    orgs._create_org("Acme", "acme", con=cc)
    orgs._create_org("Globex", "globex", con=cc)
    for org, n in (("acme", 2), ("globex", 5)):
        con = store.sqlite_db(store.org_db(org))
        for i in range(n):
            con.execute("INSERT INTO sessions(session_id,started_at,source,user) VALUES(?,?,'session',?)",
                        (f"{org}-{i}", _iso(time.time() - 86400), f"dev{i}@{org}"))   # the route's clock is real
        con.commit()
        con.close()
    yield cc
    cc.close()


def _get(path, token):
    h = _Http(path, token)
    h.do_GET()
    return h.sent


def test_an_admin_sees_only_their_own_workspace(two_orgs):
    boss = orgs.mint_token("boss@acme", "acme", "laptop", con=two_orgs, role="admin")
    orgs.mint_token("dev0@globex", "globex", "laptop", con=two_orgs, role="admin")
    code, body = _get("/api/workspace/analytics?weeks=4", boss)
    assert code == 200
    assert body["adoption"]["sessions"] == 2 and body["adoption"]["contributors"] == 2
    assert body["adoption"]["members"] == 1, "globex's credentials are not acme's members"


def test_a_member_is_refused_and_the_window_is_a_closed_set(two_orgs):
    dev = orgs.mint_token("dev@acme", "acme", "laptop", con=two_orgs)
    assert _get("/api/workspace/analytics", dev)[0] == 403
    boss = orgs.mint_token("boss@acme", "acme", "laptop", con=two_orgs, role="admin")
    assert _get("/api/workspace/analytics", boss)[1]["window"]["weeks"] == 12
    for bad in ("7", "0", "x", "52"):
        assert _get(f"/api/workspace/analytics?weeks={bad}", boss)[0] == 400, bad


# --- POST /api/track: the rows the web usage section reads ----------------------------------------

def test_track_event_writes_one_row_credited_to_the_gate(corpus):
    assert apiserver.track_event(corpus, "me@x", {"event": "page_view", "page": "memory", "user": "someone@else"}) == (200, {"ok": True})
    assert apiserver.track_event(corpus, "me@x", {"event": "flag", "page": "m" * 40, "meta": {"label": "gold"}})[0] == 200
    assert apiserver.track_event(corpus, None, {"event": "search"})[0] == 200
    corpus.execute("INSERT INTO tel.ui_events(at,user,event,page,meta) VALUES('2025-01-01T00:00:00Z','old@x','page_view','memory',NULL)")
    assert apiserver.track_event(corpus, "me@x", {"event": "page_view ", "page": "memory", "meta": {str(i): i for i in range(8)}})[0] == 200
    rows = corpus.execute("SELECT user, event, page, meta FROM tel.ui_events ORDER BY rowid DESC LIMIT 4").fetchall()
    assert rows[0][:3] == ("me@x", "page_view", "memory"), "a padded event name is stripped, never stored padded"
    assert corpus.execute("SELECT COUNT(*) FROM tel.ui_events WHERE user='old@x'").fetchone()[0] == 0, "rows past 200 days go with a write"
    assert rows[1:] == [("", "search", "", None), ("me@x", "flag", "m" * 40, '{"label":"gold"}'), ("me@x", "page_view", "memory", None)]


@pytest.mark.parametrize("body", [{"event": "login"}, {"event": ""}, {}, {"event": 7}, {"event": "flag", "meta": "x"},
                                  {"event": "flag", "meta": [1]}, {"event": "flag", "meta": {"k": "v" * 600}},
                                  {"event": "page_view", "page": "<script>alert(1)</script>"},
                                  {"event": "page_view", "page": "m" * 41}, {"event": "page_view", "page": "Memory"},
                                  {"event": "page_view", "page": 123}, {"event": "page_view", "page": None},
                                  {"event": "page_view"}, {"event": "page_view", "page": ""},
                                  {"event": "flag", "meta": {"a": {"b": 1}}}, {"event": "flag", "meta": {"a": [1]}},
                                  {"event": "flag", "meta": {str(i): i for i in range(9)}}])
def test_track_event_rejects_unknown_events_and_bad_meta(corpus, body):
    status, out = apiserver.track_event(corpus, "me@x", body)
    assert status == 400 and out["ok"] is False
    assert corpus.execute("SELECT COUNT(*) FROM tel.ui_events WHERE user='me@x'").fetchone()[0] == 0


def test_track_is_a_browser_post_and_the_analytics_route_is_not_public():
    assert "/api/track" in apiserver._COOKIE_POST_OK
    assert not apiserver._public("/api/track") and not apiserver._public("/api/workspace/analytics")
    assert json.loads(json.dumps(apiserver.TRACK_EVENTS)) == ["page_view", "search", "scope", "flag", "session_open", "entity_open"]


def test_people_and_agents_are_counted_the_same_on_every_screen(corpus):
    cc = orgs.control_db(":memory:")
    for email, host, service in (("ana@x", "ana-mac", False), ("ben@x", "web-sso", False),
                                 ("bot@x", "atlas", True), ("ana@x", "ci", True)):
        if service:
            orgs.mint_service_token(store.DEFAULT_ORG, email, host, con=cc)
        else:
            orgs.mint_token(email, store.DEFAULT_ORG, host, con=cc)
    tokens = orgs.list_tokens(store.DEFAULT_ORG, con=cc)
    agents = orgs.agent_emails(tokens)
    assert agents == {"bot@x"}, "a service token shipping as a person leaves them a person"
    for sid, user in (("s1", "ana@x"), ("s2", "Ben@x"), ("s3", "bot@x"), ("s4", "bot@x")):
        _session(corpus, sid, 1, user)
    a = _view(corpus, tokens=tokens)["adoption"]
    welcome = apiserver.onboarding(corpus, agents=agents)["metrics"]
    team = apiserver.team(corpus, agents)
    team_people = sum(not r["agent"] for r in team)
    assert (a["contributors"], welcome["people"], team_people, a["members"]) == (2, 2, 2, 2)
    assert (a["agents"], welcome["agents"], sum(r["agent"] for r in team)) == (1, 1, 1)
    assert a["per_week"][-1]["people"] == 2 and a["shipped_without_credential"] == 0
    _session(corpus, "origin", 20, "ana@x")
    _fact(corpus, "origin", "r1")
    corpus.commit()
    store.refresh_facts(corpus)
    for sid, who in (("s2", "ben@x"), ("s3", "bot@x"), ("s4", "bot@x")):
        _served(corpus, "r1", sid, 0, who)
    t = _view(corpus, tokens=tokens)["recall"]["transfer"]
    assert (t["deliveries"], t["receivers"], t["receiving_agents"]) == (3, 1, 1), \
        "handoffs split their receivers into people and agents the way contributors are split"
    v = _view(corpus, tokens=tokens)
    assert [(f["people"], f["sessions"]) for f in v["recall"]["top_reused"]] == [(1, 3)], "an agent is not a person reached"
    assert v["memory"]["contributors"] == [{"user": "ana@x", "facts": 0, "reached": 1, "agent": False}]


def test_top_contributors_mark_an_agent(corpus):
    cc = orgs.control_db(":memory:")
    orgs.mint_token("ana@x", store.DEFAULT_ORG, "ana-mac", con=cc)
    orgs.mint_service_token(store.DEFAULT_ORG, "bot@x", "ci", con=cc)
    tokens = orgs.list_tokens(store.DEFAULT_ORG, con=cc)
    _session(corpus, "s1", 0, "ana@x")
    _session(corpus, "s2", 0, "bot@x")
    _fact(corpus, "s1", "r1")
    _fact(corpus, "s2", "r2", "gotcha")
    corpus.commit()
    store.refresh_facts(corpus)
    rows = _view(corpus, tokens=tokens)["memory"]["contributors"]
    assert {r["user"]: r["agent"] for r in rows} == {"ana@x": False, "bot@x": True}, \
        "a service-token author is marked an agent in Top contributors; a person is not"


def test_the_recall_log_filters_to_answered_or_missed(corpus):
    for ago, rows in ((0, 3), (1, 0), (2, 1), (3, 0)):
        _query(corpus, ago, "fts", rows, who="a@x", text=f"q{ago}")
    missed = analytics.recall_log(corpus, viewer="a@x", result="missed")
    answered = analytics.recall_log(corpus, viewer="a@x", result="answered")
    assert [r["question"] for r in missed["rows"]] == ["q1", "q3"]
    assert [r["question"] for r in answered["rows"]] == ["q0", "q2"]
    assert (missed["total"], missed["answered"]) == (4, 2), "the totals stay all-time under a filter"


def test_analytics_and_the_recall_log_are_stamped_with_server_time(corpus):
    assert _view(corpus)["as_of"] == "2026-09-05T12:00:00Z"
    assert analytics.recall_log(corpus, now=NOW)["as_of"] == "2026-09-05T12:00:00Z"
