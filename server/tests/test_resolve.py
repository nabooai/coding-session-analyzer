"""Entity short-circuit (analyzer/resolve.py) tests — the module's former __main__ self-check,
ported faithfully. In-memory raw sqlite3 connections + the bridge (db.run), per db.engine_for's
:memory: contract."""
import sqlite3

import pytest

from analyzer import resolve, store
from analyzer.resolve import _session_card, _workspace_card, card_for, cards, resolve as _resolve

SID = "abcd1234-bcdd-480d-90e8-b373cd74bf36"


@pytest.fixture
def con():
    """The __main__ base corpus: PR-322 + fragment-ambiguous entities + one session with a create
    action, a mention, and an implements relation."""
    c = store.sqlite_db(":memory:")
    c.executemany("INSERT INTO entities(id,type,name) VALUES(?,?,?)", [
        ("PR-322", "pr", "acme/ledger#322 — Prefect orchestration for connector downloads"),
        ("k8s-deploy", "skill", "k8s-deploy"),           # 'deploy' fragment carried by 3 ids -> ambiguous
        ("prefect_deploy.py", "file", "ledgerv2_core/etl3/prefect_deploy.py"),
        ("deploy-bot", "service", "deploy-bot"),
        ("cabana", "data_source", "cabana"),             # prefix + typo tier target
    ] + [(f"w{i}", "service", "widget") for i in range(6)])   # 6 carriers of whole 'widget' -> FAMILY
    c.execute("INSERT INTO sessions(session_id,project,user,started_at,summary) VALUES(?,?,?,?,?)",
              (SID, "acme-ai", "mara@example.com", "2026-06-30T09:00:00Z", "Researched ledgerv2 ETL Collectors"))
    c.execute("INSERT INTO actions VALUES(?,?,?,?)", (SID, "create", "PR-322", "Prefect orchestration for etl3"))
    c.execute("INSERT INTO mentions(session_id,entity_id) VALUES(?,?)", (SID, "PR-322"))
    c.execute("INSERT INTO entities(id,type,name) VALUES(?,?,?)",
              ("ledger-etl3-prefect", "repo", "ledger-etl3-prefect worktree"))
    c.execute("INSERT INTO relations VALUES(?,?,?,?,?)", (SID, "PR-322", "implements", "ledger-etl3-prefect", None))
    return c


def _seed_pr_link(c):
    c.execute("INSERT INTO pr_links VALUES (?,?,?,?,?,?,?)",
              (SID, "acme/ledger", 322, "https://github.com/acme/ledger/pull/322",
               "2026-07-06T23:22:09Z", "2026-07-07T01:00:00Z", 95))


def _seed_session_extras(c):
    c.execute("INSERT INTO session_workspace VALUES (?,?,?)",
              (SID, "dir", "/Users/u/Documents/Git/ledger-etl3-prefect"))
    c.execute("INSERT INTO worked_on VALUES (?,?)", (SID, "ledgerv2 ETL Collectors research"))
    c.execute("INSERT INTO hidden_context(session_id,type,claim,confidence,scope) VALUES (?,?,?,?,?)",
              (SID, "todo", "PR #322 is open and CI-green but not yet merged.", "high", "organizational"))


def test_resolve_tiers(con):
    # the verified failure: url token -> fragment '322' is unique -> PR-322, no planner needed
    assert _resolve(con, "Which Claude session created this PR - https://github.com/acme/ledger/pull/322?") == ["PR-322"]
    assert _resolve(con, "status of #322?") == ["PR-322"]              # '#' edge stripped
    assert _resolve(con, "how do we deploy to production") == []       # ABSTAIN: 'deploy' fragment ambiguous
    assert _resolve(con, "what happened to 321") == []                 # numeric = exact only: 321 never reaches 322
    assert _resolve(con, "tell me about widget") == []                 # FAMILY: whole surface carried by 6 entities
    assert _resolve(con, "the caban ingest") == ["cabana"]             # prefix narrows to ONE entity
    assert _resolve(con, "the cabena ingest") == ["cabana"]            # typo tier: 1 edit, ratio 0.83


def test_card_contradiction_guard_and_card_for(con):
    card = cards(con, ["PR-322"])
    assert SID in card and "create" in card and "implements" in card, card

    # CONTRADICTION GUARD (bullet 3): actions has a create edge but pr_links is EMPTY for 322 here —
    # the card must reconcile with extractor evidence, never deny the creation it just displayed.
    card = cards(con, ["PR-322"], "which session created PR #322?")
    assert "No session here recorded creating PR #322" not in card, card
    assert f"Extractor evidence: PR #322 created by session {SID}" in card, card
    # card_for: an identifier query is a standalone answer (skip FTS); a topic query abstains
    c, standalone = card_for(con, "which session created PR #322?")
    assert standalone and "Extractor evidence:" in c, (standalone, c)
    assert card_for(con, "how do we deploy to production") == ("", False)


def test_pr_links_evidence_collision_miss_and_merge(con):
    # pr_links evidence: creator + timestamp + basis, straight from the deterministic harvest table
    _seed_pr_link(con)
    card = cards(con, ["PR-322"], "Which Claude session created PR https://github.com/acme/ledger/pull/322?")
    assert f"created by session {SID}" in card and "2026-07-06T23:22:09Z" in card and "95 pr-link events" in card, card
    # repo collision: same number in a second repo -> both answers + explicit NOTE, never a silent pick
    con.execute("INSERT INTO pr_links VALUES ('other-sess','acme/web',322,'u','2026-07-01T00:00:00Z','2026-07-01T00:00:00Z',3)")
    card = cards(con, ["PR-322"], "who created PR #322?")
    assert "exists in 2 repos" in card and "acme/web#322" in card and "acme/ledger#322" in card, card
    # honest miss: an explicit PR ref with no pr-link evidence says so — no grazing, no silence
    card = cards(con, [], "which session created PR #999?")
    assert "No session here recorded creating PR #999" in card, card
    assert "PR #3" not in cards(con, [], "what did we decide in standup #3?")   # no PR intent -> no bogus miss line
    # wrong-repo URL: never silently substitute — warn that the ASKED repo has no evidence
    card = cards(con, ["PR-322"], "summarize https://github.com/nabooai/coding-session-analyzer/pull/322")
    assert "NO pr-link evidence for that repo" in card and "nabooai/coding-session-analyzer#322" in card, card
    # merge question: state merge status outright — including the honest negative
    card = cards(con, ["PR-322"], "did anyone merge PR 322?")
    assert "no merge recorded in these sessions" in card, card
    con.execute("INSERT INTO actions VALUES(?,?,?,?)", ("m-sess", "merge", "PR-322", "gh pr merge"))
    card = cards(con, ["PR-322"], "did anyone merge PR 322?")
    assert "merged via `gh pr merge` in session m-sess" in card, card


def test_session_id_card(con):
    # session-id direction: "what did session X do" answers deterministically from stored data (NO
    # planner): summary + worked_on + hidden_context facts + actions + pr_links + workspace, not FTS
    _seed_pr_link(con)
    _seed_session_extras(con)
    card = cards(con, [], "what did session abcd1234 do?")
    assert f"## Session: {SID}" in card and "Researched ledgerv2 ETL Collectors" in card \
        and "ledgerv2 ETL Collectors research" in card and "[todo] PR #322 is open" in card \
        and "not yet merged" in card and "create PR-322" in card \
        and "acme/ledger#322 at 2026-07-06T23:22:09Z" in card and "ledger-etl3-prefect" in card, card
    assert _session_card(con, "deadbeef") is None        # unknown prefix -> no card (a commit sha never fires)


def test_workspace_card(con):
    # workspace card: branch-shaped token -> deterministic session list; common/plain tokens abstain
    _seed_session_extras(con)
    con.execute("INSERT INTO session_workspace VALUES (?,?,?)", (SID, "branch", "ledgerv2/etl3-prefect"))
    card = cards(con, [], "which sessions used branch ledgerv2/etl3-prefect?")
    assert "## Workspace match: ledgerv2/etl3-prefect" in card and SID in card, card
    card = cards(con, ["ledger-etl3-prefect"], "Which sessions worked in /Users/u/Documents/Git/ledger-etl3-prefect?")
    assert "## Workspace match:" in card and "## Entity:" not in card, card
    for i in range(9):                                   # popular exact ids stay useful; cap display, show total
        con.execute("INSERT INTO session_workspace VALUES (?,?,?)", (f"s{i}", "branch", "release/main-line"))
    crowded = _workspace_card(con, "release/main-line")
    assert crowded and "showing 8 of 9" in crowded and "s0" in crowded, crowded


def test_issue_disclaimer(con):
    # issue phrasing: disclose the PR-only ceiling instead of silently answering a PR
    card = cards(con, ["PR-322"], "which session created issue #322?")
    assert "issues aren't indexed" in card and "created by session" not in card, card
    # "issue #N" keeps the single issue disclaimer — no double answer
    c3 = cards(con, [], "which session created issue #322?")
    assert c3.count("issue #322") == 1 and "No indexed session mentions" not in c3, c3


def test_ticket_honest_miss(con):
    # ticket honest-miss: an unknown "Bug NNNN" as the whole question -> standalone miss (the id IS
    # the answer)
    store.build_fts(con)                                 # docs table must exist for the absence probe
    c, standalone = card_for(con, "What did I do about Bug 1234?")
    assert standalone and "No indexed session mentions Bug 1234" in c, (standalone, c)
    # mixed query (id + real topic words) -> Note-form label, NOT standalone: recall still runs beneath
    c, standalone = card_for(con, "Bug 1234 in the cabana ingest")
    assert not standalone and "Note: nothing indexed mentions Bug 1234" in c, (standalone, c)
    # the id living in FREE TEXT (not as an entity) stays recallable — no false miss claim
    con.execute("INSERT INTO hidden_context(session_id,type,claim) VALUES (?,?,?)",
                (SID, "gotcha", "Bug 1234 was a duplicate of the WAL lock issue."))
    store.build_fts(con)
    assert "Bug 1234" not in cards(con, [], "What did I do about Bug 1234?"), "FTS-known id must stay silent"
    # JIRA-style key: unknown -> miss; PR-shaped keys stay the PR path's business
    c2 = cards(con, [], "what about NAB-123?")
    assert "No indexed session mentions NAB-123" in c2, c2
    assert "No indexed session mentions" not in cards(con, [], "status of PR-322?")


def test_card_for_schema_surprise_fails_open():
    """The OperationalError fail-opens survive the Core conversion (the bridge preserves DBAPI
    error types): a connection with NO schema at all must degrade to ('', False) — plain FTS —
    never crash the ask path."""
    con = sqlite3.connect(":memory:")                    # no tables whatsoever
    assert resolve.card_for(con, "which session created PR #322?") == ("", False)


def test_ticket_miss_probe_fails_open_without_docs():
    """The docs MATCH absence probe (raw by design) fail-open: a corpus with the base schema but
    no FTS `docs` table cannot verify absence, so an unknown ticket id must yield NO miss claim
    rather than raise."""
    con = store.sqlite_db(":memory:")
    con.execute("DROP TABLE docs")                       # e.g. a pre-FTS db the migrations missed
    card, standalone = resolve.card_for(con, "What did I do about Bug 1234?")
    assert "Bug 1234" not in card and not standalone
