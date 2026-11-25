"""Ported verbatim from route.py's __main__ self-check (deleted with the P1 migration):
self/person/team + modal guard, whole-token + fuzzy owners, activity/date windows,
resolve_person/resolve_owner, escalation, lookup/relational/locate/miss shapes."""
import sqlite3
from datetime import datetime, timezone

import pytest

from analyzer import store
from analyzer.route import (activity_window, classify, date_window, resolve_owner,
                            resolve_person, since_window, undated)

ME = "mara@example.com"
NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


@pytest.fixture()
def con(monkeypatch):
    monkeypatch.setenv("CSA_TRACE", "0")     # classify() spans must not write into data/analyzer.db
    con = store.sqlite_db(":memory:")
    for u in ("mara@example.com", "jules@example.com", "hermes@example.com", "julesit@example.com"):
        con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
                    "VALUES (?,?,?,'session',?)", (f"s-{u}", "p", "2026-07-01T00:00:00Z", u))
    return con


def test_scope_classification(con):
    r = classify(con, "what did I do about the etl migration", asked_by=ME)
    assert (r.scope, r.person, r.shape) == ("self", ME, "lookup"), r
    assert classify(con, "how did jules deploy the lab server", asked_by=ME).scope == "person"
    assert classify(con, "how did jules deploy the lab server", asked_by=ME).person == "jules@example.com"
    assert classify(con, "what did we decide about auth", asked_by=ME).scope == "team"
    assert classify(con, "how do I deploy to prod", asked_by=ME).scope == "team"      # modal guard
    assert classify(con, "what is julesit working on", asked_by=ME).person == "julesit@example.com"  # whole-token: no jules collision
    r = classify(con, "how many gotchas did I log", asked_by=ME)
    assert (r.scope, r.shape) == ("self", "relational"), r
    assert classify(con, "what did mara decide about FTS", asked_by=ME).scope == "self"   # naming yourself == self
    assert classify(con, "anything", asked_by=ME, person="hermes").person == "hermes"     # explicit param wins


def test_locate_shape(con):
    # LOCATE: the locator word owns the noun. The negatives are the whole point — a fact question that merely
    # CONTAINS "session" must keep its facts, or every ordinary ask degrades to a list of session ids.
    for q in ("find me the session that talked about running the new ETL with acme data",
              "which session ran the ETL E2E", "what sessions mention both etl3 and acme",
              "show me the sessions from last week", "list all sessions on July 17",
              "which sessions depend on the graph writer lock"):   # locate WINS over relational
        assert classify(con, q, asked_by=ME).shape == "locate", ("should locate", q)
    for q in ("what did we decide in the session about auth", "what happened in my session yesterday",
              "how many sessions logged a gotcha", "the session was slow, why",
              "what did I do about the etl migration"):
        assert classify(con, q, asked_by=ME).shape != "locate", ("must NOT locate", q)


def test_escalation_and_llm_degradation(con):
    # E1 (self + named teammate): hook path -> conservative team; MCP path follows the injected verdict;
    # a dead LLM degrades to team, never crashes
    e1 = "did I review jules's ETL fix?"
    assert classify(con, e1, asked_by=ME).scope == "team"
    r = classify(con, e1, asked_by=ME, allow_llm=True,
                 llm=lambda q, a, o: {"scope": "person", "person": "jules@example.com"})
    assert (r.scope, r.person, r.escalated) == ("person", "jules@example.com", True), r
    r = classify(con, e1, asked_by=ME, allow_llm=True, llm=lambda *a: 1 / 0)
    assert (r.scope, r.escalated) == ("team", False), r


def test_miss_shape_on_unresolved_tickets(con):
    # unresolved "Bug 1234" -> miss (id recorded); a resolved ticket entity or the hook path
    # (eids=None) -> lookup; resolved entities on OTHER words don't mask a missed ticket id
    r = classify(con, "what did I do about Bug 1234?", asked_by=ME, eids=[])
    assert (r.scope, r.shape, r.missed) == ("self", "miss", ["Bug 1234"]), r
    assert classify(con, "Bug 1234 in the prefect etl", asked_by=ME, eids=["prefect"]).shape == "miss"
    assert classify(con, "what about Bug 1234", asked_by=ME, eids=["JIRA-1234"]).shape == "lookup"
    assert classify(con, "what did I do about Bug 1234?", asked_by=ME).shape == "lookup"


def test_fuzzy_owner_fallback(con):
    assert classify(con, "what is julesr working on", asked_by=ME).person == "jules@example.com"   # prefix->owner
    assert classify(con, "how did herms deploy the lab", asked_by=ME).person == "hermes@example.com"  # typo->owner
    assert classify(con, "what did we work on last week", asked_by=ME).scope == "team"   # no owner token -> team


def test_naming_yourself_is_self_scope(con):
    r = classify(con, "Who is Mara? role, background, what they work on", asked_by=ME)
    assert (r.scope, r.shape) == ("self", "lookup"), r


def test_resolve_owner_write_side_never_fuzzy(con):
    # resolve_owner (WRITE side): a relay's Slack display name -> the roster identity, alias-aware, never fuzzy.
    assert resolve_owner(con, "Jules") == "jules@example.com"                 # display name -> email
    con.execute("INSERT INTO tenant_config(id, person_aliases) VALUES (1, 'mokafor42=mara')")
    assert resolve_owner(con, "mokafor42") == "mara@example.com"            # the bot's handle for mara
    assert resolve_owner(con, "mara@example.com") == "mara@example.com"        # already an email -> itself
    assert resolve_owner(con, "") is None and resolve_owner(con, None) is None
    # NEGATIVE: no fuzzing. "Julesit" is a DIFFERENT person from "Jules" and read-side fuzzy resolution would
    # happily collapse them; write-side that would permanently credit her work to him.
    assert resolve_owner(con, "Julesit") == "julesit@example.com"
    assert resolve_owner(con, "Sean Renner") is None                     # off the roster -> nobody, never guessed
    assert resolve_owner(con, "jul") is None                             # a prefix is not a person
    # ...and None is why: a user sim showed that storing "Sean Renner" verbatim flips resolve_person("renner")
    # off `julesrenner@example.com`, because the READ side also matches tokens and unique substrings.
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
                "VALUES ('s-renner','p','2026-07-01T00:00:00Z','session','julesrenner@example.com')")
    assert resolve_person(con, "renner") == "julesrenner@example.com"        # unpolluted by any relay display name


def test_activity_and_since_windows():
    assert activity_window("What did Jules work on last week?", NOW) == (True, "2026-07-07")
    assert activity_window("what is mara up to", NOW)[0]
    assert activity_window("what is mara up to", NOW)[1] is None
    assert activity_window("how did jules deploy the lab", NOW) == (False, None)   # topical, not activity
    assert since_window("anything yesterday", NOW) == "2026-07-13"


def test_date_window_parses():
    assert date_window("nothing here", NOW) == (None, None), "no date -> no window"
    assert date_window("results from 2026-07-16", NOW) == ("2026-07-16", "2026-07-17"), "ISO single day"
    assert date_window("what did we decide on July 17", NOW) == ("2026-07-17", "2026-07-18"), "Month Day"
    assert date_window("the 12th of jul benchmark", NOW) == ("2026-07-12", "2026-07-13"), "Day Month"
    assert date_window("July 17 asked in a Jul-14 present resolves to THIS year", NOW)[0] == "2026-07-17"
    assert date_window("anything from July 10 to July 12", NOW) == ("2026-07-10", "2026-07-13"), "range inclusive"
    assert date_window("since 2026-07-01", NOW) == ("2026-07-01", None), "since -> open forward"
    assert date_window("before 2026-07-10", NOW) == (None, "2026-07-10"), "before -> open backward"
    assert date_window("last 3 days", NOW) == ("2026-07-11", "2026-07-15"), "last N days incl today"
    assert date_window("5 days ago", NOW) == ("2026-07-09", "2026-07-15")
    assert date_window("today's results", NOW) == ("2026-07-14", "2026-07-15"), "today"
    assert date_window("what happened yesterday", NOW) == ("2026-07-13", "2026-07-14"), "yesterday"
    assert date_window("this week's work", NOW) == ("2026-07-07", "2026-07-15"), "relative week -> window"
    assert date_window("Feb 30 is not a date", NOW) == (None, None), "invalid calendar day -> no window"


def test_undated_drops_only_what_date_window_reads():
    words = lambda q: undated(q).split()
    assert words("what is the weather today") == ["what", "is", "the", "weather"]
    assert words("what changed since July 1 in the pool") == ["what", "changed", "in", "the", "pool"]
    assert words("lunch in the last 3 days") == ["lunch", "in", "the"]
    assert words("what did we do this week") == ["what", "did", "we", "do"]
    assert words("what happens after deploy") == ["what", "happens", "after", "deploy"], "no date: unchanged"


def test_date_window_no_month_prefix_false_positives():
    # A word that merely STARTS with a month prefix + a number is NOT a date. This was a silent-wrong-answer
    # regression — "decide 3" hard-filtered recall to Dec 3 and returned nothing.
    for q in ("how do we decide 3 things", "separate 2 auth services", "maybe 3 retries",
              "marketing 5 channels", "declare 2 vars", "augment 5 tests", "top 5 results",
              "the 401 error", "port 8080", "gpt-5-mini v2", "novel 3 approach", "junction 2"):
        assert date_window(q, NOW) == (None, None), f"false-positive date filter on {q!r}"
    # but real month-day forms still parse
    assert date_window("december 3 deploy", NOW) == ("2025-12-03", "2025-12-04"), "full month name (past -> last yr)"
    assert date_window("sept 5 results", NOW) == ("2026-09-05", "2026-09-06"), "sept abbrev"
    assert date_window("what shipped on aug 5", NOW) == ("2026-08-05", "2026-08-06"), "aug abbrev"


def test_resolve_person_layering():
    # nickname alias > exact > fuzzy > unique substring (the explicit person= path); a BARE
    # sessions table (not store schema) — resolve_person must work on any con with the table.
    rc = sqlite3.connect(":memory:")
    rc.execute("CREATE TABLE sessions(session_id TEXT, user TEXT, source TEXT)")
    for u in ("mara@example.com", "jules@example.com", "julesrenner@example.com", "hermes@example.com"):
        rc.execute("INSERT INTO sessions VALUES(?,?,'session')", (f"s-{u}", u))
    assert resolve_person(rc, "mokafor") is None, "no alias is configured: a nickname names no one"
    assert resolve_person(rc, "mara") == "mara@example.com", "exact local-part"
    assert resolve_person(rc, "jules") == "jules@example.com", "exact local-part wins over julesrenner substring"
    assert resolve_person(rc, "herms") == "hermes@example.com", "fuzzy typo"
    assert resolve_person(rc, "nobody") is None and resolve_person(rc, "") is None, "unknown -> None"
    rc = store.sqlite_db(":memory:")
    for u in ("mara@example.com", "jules@example.com"):
        rc.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES (?,?,?,'session',?)",
                   (f"s-{u}", "p", "2026-07-01T00:00:00Z", u))
    rc.execute("INSERT INTO tenant_config(id, person_aliases) VALUES (1, 'mokafor=mara')")
    assert resolve_person(rc, "mokafor") == "mara@example.com", "alias: a true nickname fuzzy can't reach"
