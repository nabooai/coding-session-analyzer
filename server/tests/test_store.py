"""store.py's __main__ self-check, ported verbatim to pytest (P3 — the block is deleted from
store.py; `python -m analyzer.store` is retired in favour of `pytest tests/test_store.py`).
Each function is one print-block of the original script; the `con`-chained blocks (basic load →
incremental index → code-home rows → harvest docs) stay one function because they share state."""
import os
import sqlite3
from pathlib import Path

from analyzer.project import normalize_project as _np
from analyzer.store import (build_fts, canon, canonicalize, carry_refs, code_home, data_root,
                            hc_ref, index_session, load_sqlite, merge_entity, org_db, org_dir,
                            ro_connect, set_hc_type, set_review, set_type, sqlite_db,
                            suggest_merges, telemetry_path, valid_org_id, valid_session_id,
                            write_clusters)


def _sess(sid, **kw):
    return dict(dict(session_id=sid, project="p", git_branch="b", started_at=None, ended_at=None,
                     n_user=1, n_asst=1), **kw)


def test_load_fts_review_retype_incremental_codehome_harvest():
    con = sqlite_db(":memory:")
    sess = _sess("s1")
    data = {"summary": "x", "entities": [{"id": "KLG-200", "type": "ticket"}, {"id": "PR-9", "type": "pr"}],
            "relations": [{"source_id": "PR-9", "rel": "implements", "target_id": "KLG-200"}],
            "hidden_context": [{"type": "gotcha", "claim": "redis backpressure", "evidence": "queue overflow"}]}
    load_sqlite(con, sess, canonicalize(con, data))
    build_fts(con)
    merge_entity(con, "klg200", "KLG-200")        # alias resolves on next canonicalize
    assert canon(con, "klg200") == "KLG-200"
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'backpressure'").fetchone()[0] == 1
    set_review(con, "hc", hc_ref("redis backpressure"), "rejected")   # HITL reject -> drops from FTS
    build_fts(con)
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'backpressure'").fetchone()[0] == 0
    set_type(con, "KLG-200", "release")
    assert con.execute("SELECT type FROM entity_types WHERE id='KLG-200'").fetchone()[0] == "release"
    # redaction_mode round-trips: explicit mode persists; a session dict without one defaults to 'secrets'
    load_sqlite(con, _sess("rmode", redaction_mode="strict"), {})
    assert con.execute("SELECT redaction_mode FROM sessions WHERE session_id='rmode'").fetchone()[0] == "strict"
    assert con.execute("SELECT redaction_mode FROM sessions WHERE session_id='s1'").fetchone()[0] == "secrets"

    # index_session: incremental one-session index (no full FTS rebuild)
    s1_docs = con.execute("SELECT COUNT(*) FROM docs WHERE session_id='s1'").fetchone()[0]
    data2 = {"summary": "postgres pool tuning", "worked_on": ["db/pool.py"],
             "hidden_context": [{"type": "gotcha", "claim": "pgbouncer transaction mode breaks prepared statements",
                                 "evidence": "errors after switch"}]}
    index_session(con, _sess("s2", project="p2"), data2)    # load_sqlite + session-scoped FTS
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'pgbouncer' AND session_id='s2'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM docs WHERE session_id='s1'").fetchone()[0] == s1_docs  # corpus untouched

    # code-home: a session running in acme-ai recording a fact about code that lives in ledger — the exact
    # incident shape. project stays acme-ai; the fact carries where the code actually lives.
    index_session(con, _sess("s4", project="acme-ai"), {"summary": "etl3 deploy", "hidden_context": [
        {"type": "gotcha", "claim": "prefect_deploy.py does not exist in this repo",
         "evidence": "searched", "repo": "ledger", "path": "ledgerv2_core/etl3/prefect_deploy.py"},
        {"type": "preference", "claim": "user likes short PR titles", "evidence": "said so"},   # not about code
        {"type": "gotcha", "claim": "absolute path leak check", "evidence": "x",
         "repo": "/Users/someone/Documents/Git/ledger", "path": "/Users/someone/Documents/Git/ledger/a/b.py"},
    ]})
    assert con.execute("SELECT repo FROM hidden_context WHERE claim LIKE 'prefect_deploy%'").fetchone()[0] == "ledger"
    assert con.execute("SELECT repo, path FROM hidden_context WHERE claim LIKE 'user likes%'").fetchone() == (None, None), \
        "a non-code fact must carry NO code-home (NULL renders exactly as before)"
    # trust boundary: an absolute path must never paint a username into a team-served provenance line
    leak = con.execute("SELECT repo, path FROM hidden_context WHERE claim='absolute path leak check'").fetchone()
    assert leak == ("ledger", "a/b.py"), leak
    assert not any("someone" in str(v) for v in leak), leak

    # harvest flow: a session dict carrying sess['harvest'] gets pr_link + workspace docs, in BOTH fts paths
    sess3 = _sess("s3", project="ledger", git_branch="main", started_at="2026-07-06T00:00:00Z",
                  harvest={"prs": [{"repo": "acme/ledger", "n": 322, "url": "https://github.com/acme/ledger/pull/322",
                                    "first": "2026-07-06T23:22:09Z", "last": "2026-07-07T00:00:00Z", "count": 95,
                                    "head": "ledgerv2/etl3-prefect", "created": True}],
                           "merges": [], "dirs": ["/Users/u/Documents/Git/ledger-etl3-prefect"],
                           "branches": ["ledgerv2/etl3-prefect"],
                           "decisions": [{"claim": "Which ETL scheduler? → Prefect over Airflow",
                                          "evidence": 'User answered Claude: "Which ETL scheduler?" = "Prefect over Airflow".'}]})
    index_session(con, sess3, {"summary": "prefect etl", "hidden_context": []})   # EMPTY LLM output: decision must still land
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH '322' AND kind='pr_link'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'prefect' AND kind='workspace'").fetchone()[0] == 1
    assert con.execute("SELECT path FROM session_dirs WHERE session_id='s3'").fetchone()[0].endswith("prefect")
    assert con.execute("SELECT branch FROM session_branches WHERE session_id='s3'").fetchone()[0] == "ledgerv2/etl3-prefect"
    # harvest decision -> hidden_context row (type=decision, org scope, high conf) AND FTS-searchable
    row = con.execute("SELECT type,scope,confidence FROM hidden_context WHERE session_id='s3' AND claim LIKE 'Which ETL%'").fetchone()
    assert row == ("decision", "organizational", "high"), row
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'Airflow' AND kind='hidden_context'").fetchone()[0] == 1
    index_session(con, sess3, {"summary": "prefect etl", "hidden_context": []})   # idempotent re-index
    assert con.execute("SELECT COUNT(*) FROM hidden_context WHERE session_id='s3' AND claim LIKE 'Which ETL%'").fetchone()[0] == 1
    build_fts(con)                                        # full rebuild must regenerate the same harvest docs
    assert con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH '322' AND kind='pr_link'").fetchone()[0] == 1


def test_relay_attribution(monkeypatch):
    # The pi Slack bot ships every session under ONE account; the transcript names the real asker.
    rc = sqlite_db(":memory:")
    _paths = ("/a/b/ledger", "/a/b/ledger/.claude/worktrees/x/server", "/var/relaybot/app", "slack/#releases", None)
    _pure_before = [_np(p) for p in _paths]                        # empty corpus (see the assert below)
    BOT = "hermes@example.com"

    def _rel(sid, transcript, harness_="pi", user=BOT):
        load_sqlite(rc, _sess(sid, git_branch=None, harness=harness_, user=user, transcript=transcript), {})
        return rc.execute("SELECT user, shipped_by FROM sessions WHERE session_id=?", (sid,)).fetchone()

    _rel("seed", None, "claude-code", "jules@example.com")             # put a roster up to resolve against
    _rel("seed2", None, "claude-code", "mara@example.com")
    _rel("seed3", None, "claude-code", "julesrenner@example.com")      # a REAL second owner one token from "Renner"
    BLOCK = "USER: # Session metadata\nInitiated by: Jules (<@U01A>) — CTO"
    # CRITICAL: with no relay account declared, a transcript can NOT move a session between accounts,
    # however it is shaped — `harness` is client-authored and NOT a boundary.
    assert _rel("s-spoof", BLOCK, "pi", "mallory@evil.test") == ("mallory@evil.test", None)
    rc.execute("INSERT INTO tenant_config(id, relay_accounts) VALUES (1, ?)", (f" {BOT.upper()} , ",))  # case/whitespace tolerant
    assert _rel("s-spoof2", BLOCK, "pi", "mallory@evil.test") == ("mallory@evil.test", None)   # still not a relay
    # ...and WITHIN a declared relay account the block narrows attribution to the human who asked.
    assert _rel("s-dm", BLOCK) == ("jules@example.com", BOT)           # credited to jules, still deletable by the bot
    rc.execute("UPDATE tenant_config SET person_aliases = ?", ("MoKafor42=mara",))   # case tolerant
    assert _rel("s-alias", "USER: # Session metadata\nInitiated by: mokafor42 (<@U03C>)")[0] == "mara@example.com"
    # older bot transcripts have no pi header (harness.detect says claude-code) — account gate still covers them
    assert _rel("s-legacy", BLOCK, "claude-code") == ("jules@example.com", BOT)
    # NEGATIVE: a name nobody on the roster answers to is never fuzzed onto a member, never minted as new
    assert _rel("s-sean", "USER: # Session metadata\nInitiated by: Sean Renner") == (BOT, None)
    # NEGATIVE: an unnamed relay session stays on the SHIPPING account (coarse beats fragmented)
    assert _rel("s-nometa", "USER: hello") == (BOT, None)
    # NEGATIVE: an ordinary member's session is untouched — shipped_by stays NULL
    assert _rel("s-cc", "USER: fix the flaky test", "claude-code", "mara@example.com") == ("mara@example.com", None)
    # The Person graph reads sess["user"] AFTER load_sqlite, so the two stores must not disagree.
    _s = _sess("s-graph", git_branch=None, harness="pi", user=BOT, transcript=BLOCK)
    load_sqlite(rc, _s, {})
    assert _s["user"] == "jules@example.com" and _s["shipped_by"] == BOT, _s
    # NEGATIVE: the PERSON axis is roster-dependent by nature; the PROJECT axis must not be —
    # same cwds, before and after several sessions land, byte-identical answers.
    assert _pure_before == [_np(p) for p in _paths] == ["ledger", "ledger", "app", "#releases", "?"]


def test_code_home_pure():
    assert code_home({"repo": "  ", "path": None}) == (None, None)
    assert code_home({"repo": "ledger", "path": "x" * 500}) == ("ledger", None)          # rambling path dropped, repo kept
    assert code_home({"repo": "ledger", "path": "ledgerv2_core/etl3/x.py"}) == ("ledger", "ledgerv2_core/etl3/x.py")
    assert code_home({"repo": None, "path": "/Users/n/Documents/Git/ledger/a.py"}) == (None, "Documents/Git/ledger/a.py")
    assert "n" not in code_home({"repo": None, "path": "/Users/n/x.py"})[1].split("/")  # username gone, repo unknown
    # a windows client's transcript carries backslashed paths — the username must not survive them either
    assert code_home({"repo": "ledger", "path": "C:\\Users\\nb\\Git\\ledger\\a.py"}) == ("ledger", "a.py")
    assert code_home({"repo": "C:\\Users\\nb\\Git\\ledger", "path": None}) == ("ledger", None)
    # a HOME DIR is not a repo: `code: nb` would paint a username into every rendered line
    for home in ("/Users/nb", "/home/hermes", "/root", "C:/Users/nb"):
        assert code_home({"repo": home}) == (None, None), home
    assert code_home({"repo": "/Users/nb/Git/ledger"}) == ("ledger", None)          # a repo UNDER a home: fine


def test_write_clusters_single_snapshot():
    # same claim in 2 sessions -> ONE cluster spanning both (the cross-session signal)
    cc = sqlite_db(":memory:")
    for sid in ("c1", "c2"):
        load_sqlite(cc, _sess(sid), {"hidden_context": [{"type": "gotcha",
                                                         "claim": "redis backpressure drops writes",
                                                         "evidence": "e"}]})
    load_sqlite(cc, _sess("c3"), {"hidden_context": [{"type": "gotcha",
                                                      "claim": "totally unrelated pgbouncer thing",
                                                      "evidence": "e"}]})
    assert write_clusters(cc) == 1, "the shared claim must form ONE multi-session cluster"
    shared = hc_ref("redis backpressure drops writes")
    assert cc.execute("SELECT sessions FROM claim_clusters WHERE ref=?", (shared,)).fetchone()[0] == 2
    lone = hc_ref("totally unrelated pgbouncer thing")
    assert cc.execute("SELECT sessions FROM claim_clusters WHERE ref=?", (lone,)).fetchone()[0] == 1


def test_ref_carry_forward():
    # a re-extract that REWORDS a claim must not orphan the human/usage rows keyed on its ref
    rc = sqlite_db(":memory:")
    rsess = _sess("rx")
    v1 = "pgbouncer transaction pooling breaks prepared statements in the payments service"
    v2 = "pgbouncer transaction pooling breaks prepared statements for the payments-svc service (confirmed twice)"
    other = "the nightly backup job writes to the wrong s3 bucket prefix"
    fresh_claim = "grafana alert rules live in terraform, not the UI"
    load_sqlite(rc, rsess, {"hidden_context": [{"type": "gotcha", "claim": v1, "evidence": "e"},
                                               {"type": "gotcha", "claim": other, "evidence": "e"}]})
    ref1 = hc_ref(v1)
    set_review(rc, "hc", ref1, "gold", note="human")
    set_hc_type(rc, ref1, "decision")
    rc.execute("INSERT INTO served(session,asked_at,question,ref,kind,project) VALUES ('m1','t','pgbouncer?',?,'hc','p')",
               (ref1,))
    rc.execute("INSERT INTO signals VALUES (?,?,?,?)", (ref1, "acted_on", "rx", "t"))
    rc.commit()
    load_sqlite(rc, rsess, {"hidden_context": [                       # re-extract: one reworded, one identical, one new
        {"type": "gotcha", "claim": v2, "evidence": "e"},
        {"type": "gotcha", "claim": other, "evidence": "e"},
        {"type": "gotcha", "claim": fresh_claim, "evidence": "e"}]})
    now = dict(rc.execute("SELECT claim, ref FROM hidden_context WHERE session_id='rx'").fetchall())
    assert now[other] == hc_ref(other), "unchanged claim must keep its content-address ref (zero churn)"
    assert now[v2] == ref1, "reworded claim must CARRY the old ref — else review/hc_type/served/signals orphan"
    assert now[fresh_claim] == hc_ref(fresh_claim), "a genuinely new fact gets a genuinely new ref"
    assert len({now[v2], now[other], now[fresh_claim]}) == 3, "two distinct facts must never collapse onto one ref"
    assert rc.execute("SELECT type FROM hidden_context WHERE ref=?", (ref1,)).fetchone()[0] == "decision", \
        "the hc_types override must still apply after a REWORDED re-index (it survives re-index)"
    for tbl, sql in (("reviews", "SELECT COUNT(*) FROM reviews r JOIN hidden_context h ON h.ref=r.ref WHERE r.status='gold'"),
                     ("served", "SELECT COUNT(*) FROM served s JOIN hidden_context h ON h.ref=s.ref"),
                     ("signals", "SELECT COUNT(*) FROM signals g JOIN hidden_context h ON h.ref=g.ref")):
        assert rc.execute(sql).fetchone()[0] == 1, f"{tbl} row orphaned by the re-index"
    # carry_refs directly: one-to-one, and no ref theft by an unrelated fact
    one = "the etl drain holds a per-org lock so two workers never bill the same session twice"
    got = carry_refs([(hc_ref(one), one)], [one + " (restated by the extractor)", one + " — restated once more"])
    assert got.count(hc_ref(one)) == 1, "one-to-one: two new claims must not BOTH claim the same old ref"
    assert carry_refs([], ["anything at all"]) == [hc_ref("anything at all")], "no history -> pure content address"
    unrelated = "grafana alert rules live in terraform, not the UI"
    assert carry_refs([(hc_ref(one), one)], [unrelated]) == [hc_ref(unrelated)], \
        "an unrelated fact must never inherit a vanished fact's ref (that would move a human label onto it)"


def test_carry_refs_no_swap_and_order_invariant():
    # (a) NO SWAP: ambiguity must carry nothing — a wrong carry RE-ATTRIBUTES human judgement silently
    swp_a = "the etl drain acquires a per-org lock before billing a session to the org budget"
    swp_b = "the etl drain acquires a per-org lock before billing a session to the org ledger and budget cap"
    swp_a2 = swp_b + " today"                       # a rewording of A that reads like B
    swp_b2 = swp_a + " cap"                         # a rewording of B that reads like A
    sw = carry_refs([(hc_ref(swp_a), swp_a), (hc_ref(swp_b), swp_b)], [swp_a2, swp_b2])
    assert sw[0] != hc_ref(swp_b) and sw[1] != hc_ref(swp_a), \
        f"REF SWAP: two facts traded human judgement ({sw})"
    assert sw == [hc_ref(swp_a2), hc_ref(swp_b2)], \
        f"ambiguous match must carry NOTHING, not pick a winner by hash/position order ({sw})"

    # (b) ORDER-INVARIANT: the candidate list is LLM output — permuting it must not move a label
    ord_o = "redis backpressure drops writes when the queue depth exceeds the configured watermark"
    ord_1 = "redis backpressure drops writes when queue depth exceeds the configured watermark limit"
    ord_2 = "redis backpressure drops writes when the queue depth exceeds the watermark configured limit"
    hist = [(hc_ref(ord_o), ord_o)]
    fwd = dict(zip([ord_1, ord_2], carry_refs(hist, [ord_1, ord_2])))
    rev = dict(zip([ord_2, ord_1], carry_refs(hist, [ord_2, ord_1])))
    assert fwd == rev, f"emission order decided which fact inherits the ref: {fwd} vs {rev}"
    assert hc_ref(ord_o) not in fwd.values(), "two equally-good candidates must both abstain, not race"


def test_no_cross_session_ref_sharing():
    # (c) `ref` implies one claim text corpus-wide; ref-keyed joins are session-blind
    xc = sqlite_db(":memory:")
    shared_claim = "the ingest endpoint always queues and the etl service drains every org in parallel"
    for s in ("x1", "x2"):
        load_sqlite(xc, _sess(s), {"hidden_context": [{"type": "gotcha", "claim": shared_claim, "evidence": "e"}]})
    xc.commit()
    assert xc.execute("SELECT COUNT(DISTINCT session_id) FROM hidden_context WHERE ref=?",
                      (hc_ref(shared_claim),)).fetchone()[0] == 2, "fixture: both sessions must share the ref"
    load_sqlite(xc, _sess("x1"),      # x1 re-extracts with its wording changed
                {"hidden_context": [{"type": "gotcha", "claim": shared_claim + " with a per-org budget cap",
                                     "evidence": "e"}]})
    xc.commit()
    assert xc.execute("SELECT COUNT(*) FROM (SELECT ref FROM hidden_context WHERE ref IS NOT NULL "
                      "GROUP BY ref HAVING COUNT(DISTINCT claim) > 1)").fetchone()[0] == 0, \
        "one ref must never map to two claim texts — a human flag on one would silently hit the other"
    assert xc.execute("SELECT COUNT(DISTINCT session_id) FROM hidden_context WHERE ref=?",
                      (hc_ref(shared_claim),)).fetchone()[0] == 1, \
        "the reworded session must NOT still hold the other session's ref"
    set_hc_type(xc, hc_ref(shared_claim), "decision")
    build_fts(xc)
    assert xc.execute("SELECT COUNT(*) FROM hidden_context WHERE type='decision'").fetchone()[0] == 1, \
        "set_hc_type on one session's ref must not retype the other session's fact"
    set_review(xc, "hc", hc_ref(shared_claim), "wrong", note="human")
    build_fts(xc)
    assert xc.execute("SELECT COUNT(*) FROM docs WHERE kind='hidden_context'").fetchone()[0] == 1, \
        "a `wrong` flag on ONE session's fact must not un-index the other session's fact"


def test_sqlite_db_creates_parent_dirs(tmp_path):
    # fresh install: sqlite_db must create the db's parent dir (mcp ask() crashed with no data/)
    fresh = str(tmp_path / "sub" / "dir" / "a.db")
    sqlite_db(fresh).close()
    assert os.path.exists(fresh), "sqlite_db must mkdir its parent path"


def test_org_paths_and_session_ids():
    # org isolation: the tenant boundary is the FILE, so these path rules ARE the security check.
    assert org_db(None) == org_db("default") == data_root() / "analyzer.db", "default org keeps data/analyzer.db"
    assert org_dir(None) == org_dir("default") == data_root()
    assert org_db("acme") == data_root() / "orgs" / "acme" / "analyzer.db"
    assert org_db("acme") != org_db("acme2") != org_db("default"), "distinct orgs -> distinct files"
    for bad in ("../etc", "a/b", "/abs", "", "A-CAPS", "-lead", "x" * 64, "..", ".", "a b", "acmé", "a/../b"):
        try:
            org_db(bad)
            raise AssertionError(f"org_db({bad!r}) must reject — it becomes a path")
        except ValueError:
            pass
    # traversal must not escape data/orgs/ even if validation were bypassed downstream
    assert data_root() / "orgs" in org_db("acme").parents
    assert valid_org_id("acme") == "acme"
    for bad_nl in ("acme\n", "acme\r"):                  # $-vs-\Z: a trailing newline must NOT pass
        try:
            valid_org_id(bad_nl)
            raise AssertionError(f"valid_org_id({bad_nl!r}) must reject a trailing newline")
        except ValueError:
            pass
    # session_id is the OTHER attacker-influenced half of a per-org path (queue + cache filename)
    assert valid_session_id("e26d87a3-3958-40") == "e26d87a3-3958-40" and valid_session_id("a-sess-1") == "a-sess-1"
    assert valid_session_id("summary_harness_000") == "summary_harness_000"
    for bad in ("../../org-a/pending/x", "a/b", "..", ".", "", ".hidden", "a\x00b", "a\nb", "x" * 129,
                "..\\..\\x", "/abs/path"):
        try:
            valid_session_id(bad)
            raise AssertionError(f"valid_session_id({bad!r}) must reject a path-dangerous id")
        except ValueError:
            pass


def test_org_isolation_ref_collision():
    # two orgs are genuinely separate corpora: same session_id + same claim text -> zero crosstalk
    a, b = sqlite_db(":memory:"), sqlite_db(":memory:")
    same = {"hidden_context": [{"type": "gotcha", "claim": "identical claim text", "evidence": "e"}]}
    for c in (a, b):
        load_sqlite(c, _sess("COLLIDE", user="x@a.com"), same)
        build_fts(c)
    set_review(a, "hc", hc_ref("identical claim text"), "wrong")      # org A hides its own copy...
    build_fts(a)
    build_fts(b)
    assert a.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'identical'").fetchone()[0] == 0
    assert b.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH 'identical'").fetchone()[0] == 1, \
        "org A's flag must NOT hide org B's identical claim (ref is a content hash — shared-namespace leak)"
    set_hc_type(a, hc_ref("identical claim text"), "decision")        # ...and its UPDATE ... WHERE ref=? stays home
    assert b.execute("SELECT type FROM hidden_context WHERE session_id='COLLIDE'").fetchone()[0] == "gotcha", \
        "org A's hc_type pin must NOT rewrite org B's row"
    assert write_clusters(a) == 0 and a.execute(
        "SELECT sessions FROM claim_clusters WHERE ref=?", (hc_ref("identical claim text"),)).fetchone()[0] == 1, \
        "corroboration counts within-org only (never 'N sessions somewhere else in the tenant')"


def test_ro_connect_denies_attach(tmp_path):
    # ATTACH DATABASE defeats file-per-org isolation on the LLM-planner path (mode=ro blocks writes,
    # not ATTACH). ro_connect must refuse it while a normal cross-table SELECT still works.
    _victim = str(tmp_path / "victim.db")
    vc = sqlite3.connect(_victim)
    vc.execute("CREATE TABLE hidden_context(claim TEXT)")
    vc.execute("INSERT INTO hidden_context VALUES ('ORGA_SECRET')")
    vc.commit()
    vc.close()
    _own = str(tmp_path / "own.db")
    oc = sqlite3.connect(_own)
    oc.execute("CREATE TABLE t(x)")
    oc.execute("INSERT INTO t VALUES (1)")
    oc.commit()
    oc.close()
    hard = ro_connect(_own)
    assert hard.execute("SELECT x FROM t").fetchone()[0] == 1, "a normal SELECT must still work on ro_connect"
    for atk in (f"ATTACH DATABASE '{_victim}' AS a",
                f"ATTACH DATABASE 'file:{_victim}?mode=ro' AS a"):
        try:
            hard.execute(atk)
            raise AssertionError("ATTACH DATABASE must be DENIED on a planner connection (cross-tenant read)")
        except sqlite3.DatabaseError:                    # authorizer -> 'not authorized' (a DatabaseError)
            pass
    # Exactly two dbs: `main` and OUR OWN `tel` (this corpus's telemetry sibling, attached by
    # ro_connect before the authorizer went on, derived from the path — never from model input).
    # The victim must be nowhere in the list: that is the isolation claim, and the sharper form of
    # the old "nothing is attached" assertion now that the serving path has a second file.
    attached = hard.execute("PRAGMA database_list").fetchall()
    assert [r[1] for r in attached] == ["main", "tel"], f"unexpected attachments: {attached}"
    assert not any("victim" in (r[2] or "") for r in attached), "the victim's FILE got attached"
    assert hard.execute("SELECT COUNT(*) FROM tel.served").fetchone()[0] == 0, "tel must be this org's"
    hard.close()


def test_fuzzy_suggest_merges():
    econ = sqlite_db(":memory:")
    ents = [("Cabana", "data_source"), ("cabna", "data_source"), ("cnaba", "data_source"),  # typo triple
            ("cabana-chrome-ext-v2", "service"),                    # substring of Cabana but different type/thing
            ("ledger", "service"), ("ledger", "repo"),                  # same string, DIFFERENT type -> stay split
            ("redis", "service"), ("postgres", "service"),          # genuinely different -> stay split
            ("MINDSPACE_FLOOR1", "env"), ("MINDSPACE_FLOOR5", "env")]  # numbered siblings -> stay split
    for eid, ty in ents:
        econ.execute("INSERT OR IGNORE INTO entities(id,type,name) VALUES(?,?,?)", (eid, ty, eid))
    econ.commit()
    grp = {frozenset(i for i, _, _ in g) for g in suggest_merges(econ)}
    assert frozenset({"Cabana", "cabna", "cnaba"}) in grp, grp                 # typos cluster (transitively)
    assert not any("ledger" in g and len(g) > 1 for g in grp)                    # cross-type homograph NOT merged
    assert not any({"redis", "postgres"} <= g for g in grp)                    # distinct services NOT merged
    assert not any({"MINDSPACE_FLOOR1", "MINDSPACE_FLOOR5"} <= g for g in grp)  # numbered siblings NOT merged
    assert not any("cabana-chrome-ext-v2" in g for g in grp)                   # different type -> not with Cabana


def test_org_paths_are_absolute_and_track_the_cwd(tmp_path, monkeypatch):
    """Every path CSA opens is ABSOLUTE, and it is resolved at CALL time: the server, csa.py,
    rehearse_tenancy.py and half these tests chdir into a box and expect `data/` to follow them."""
    box_a, box_b = tmp_path / "a", tmp_path / "b"
    box_a.mkdir()
    box_b.mkdir()
    monkeypatch.chdir(box_a)
    a_default, a_acme = org_db(), org_db("acme")
    assert a_default.is_absolute() and a_acme.is_absolute() and org_dir("acme").is_absolute()
    assert os.path.isabs(telemetry_path(a_acme)) and telemetry_path(a_acme) == str(a_acme.parent / "telemetry.db")
    assert telemetry_path(":memory:") == ":memory:", "the sentinel is not a path"

    monkeypatch.chdir(box_b)
    assert org_db() != a_default and org_db("acme") != a_acme, "a second cwd is a second box"
    assert data_root() == Path.cwd() / "data" == org_db().parent
    sqlite_db().close()                                  # no argument: THIS box's corpus, not the import's
    assert (box_b / "data" / "analyzer.db").exists() and not (box_a / "data").exists()
