"""Ported verbatim from search.py's __main__ self-check (deleted with the P3 migration):
synonym expansion; fts candidates; rerank; corroboration boost; the implicit-usage tiers
(recalled-only < gold, acted_on>=2 > gold, acted_on==1 < gold, judge_gold x1.1 capped);
project boost x1.2 same-project only, worktree cwd normalization, legacy no-cwd degradation.
One flow on one in-memory db — the asserts accumulate state exactly as the selfcheck did."""
import sqlite3

from analyzer.search import (JUDGE_GOLD_FACTOR, PROJECT_BOOST, REPEAT_ACTED_CAP, STATUS_WEIGHT,
                             TYPE_WEIGHT, _fts_query, fts_candidates, rerank)


def test_search_end_to_end(monkeypatch):
    monkeypatch.setenv("CSA_TRACE", "0")     # search() spans must not write into the real data/analyzer.db
    assert "deployment" in _fts_query("deploy") and "authentication" in _fts_query("auth"), "synonyms off"
    assert TYPE_WEIGHT["pr_link"] > TYPE_WEIGHT["gotcha"] and TYPE_WEIGHT["workspace"] > 1.0
    from analyzer.store import DOCS_COLS               # the REAL docs shape, not a copy (schema-proof)
    c = sqlite3.connect(":memory:")
    c.execute(f"CREATE VIRTUAL TABLE docs USING fts5({DOCS_COLS})")
    c.executescript("CREATE TABLE entities(id TEXT); CREATE TABLE mentions(session_id TEXT, entity_id TEXT);")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('1','hidden_context','s1','p','2026-06-20','gotcha','session',NULL,1,'redis backpressure caused queue overflow')")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('2','hidden_context','s2','p','2026-06-20','preference','memory','gold',1,'user likes mermaid diagrams')")
    rows = fts_candidates(c, "backpressure queue")
    assert rows and rows[0][0] == "1", rows
    rk = rerank(c, "backpressure queue", rows, k=2)
    assert rk[0]["doc_id"] == "1" and rk[0]["score"] >= rk[-1]["score"], rk
    # corroboration boost: two identical-relevance docs, the one rediscovered in more sessions ranks first
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('lone','hidden_context','sA','p','2026-06-20','gotcha','session',NULL,1,'alpha beta gamma corroborated signal')")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('many','hidden_context','sB','p','2026-06-20','gotcha','session',NULL,4,'alpha beta gamma corroborated signal')")
    rk2 = rerank(c, "alpha beta gamma corroborated signal", fts_candidates(c, "alpha beta gamma corroborated signal"), k=2)
    assert rk2[0]["doc_id"] == "many" and rk2[0]["corrob"] > 1.0, rk2
    # implicit-usage signals: identical docs, the recalled/acted+dominant one ranks first — capped at 1.15 (< gold 1.25)
    c.executescript("CREATE TABLE hidden_context(session_id TEXT, ref TEXT, claim TEXT);"
                    "CREATE TABLE signals(ref TEXT, kind TEXT, session_id TEXT, at TEXT);"
                    "CREATE TABLE claim_clusters(ref TEXT PRIMARY KEY, cluster_id TEXT, sessions INT);")
    c.execute("INSERT INTO hidden_context VALUES('sC','refcold','delta epsilon zeta')")   # rowid 1 -> doc hc1
    c.execute("INSERT INTO hidden_context VALUES('sD','refhot','delta epsilon zeta')")    # rowid 2 -> doc hc2
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('hc1','hidden_context','sC','p','2026-06-20','gotcha','session',NULL,1,'delta epsilon zeta')")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('hc2','hidden_context','sD','p','2026-06-20','gotcha','session',NULL,1,'delta epsilon zeta')")
    for i in range(3):
        c.execute(f"INSERT INTO signals VALUES('refhot','recalled_in_session','u{i}','')")
    c.execute("INSERT INTO signals VALUES('refhot','dominant_entity','u0','')")
    rk3 = rerank(c, "delta epsilon zeta", fts_candidates(c, "delta epsilon zeta"), k=2)
    assert rk3[0]["doc_id"] == "hc2" and rk3[1]["doc_id"] == "hc1", rk3
    assert abs(rk3[0]["score"] / rk3[1]["score"] - 1.15) < 0.01, rk3   # (1+.05*log2(4))*1.1=1.21 -> capped 1.15
    # served-AND-used tier: repeated acted_on lifts a fact above the recalled-only cap AND above human gold
    for i in range(3):
        c.execute(f"INSERT INTO signals VALUES('refhot','acted_on','a{i}','')")
    rk4 = rerank(c, "delta epsilon zeta", fts_candidates(c, "delta epsilon zeta"), k=2)
    ratio = rk4[0]["score"] / rk4[1]["score"]
    assert STATUS_WEIGHT["gold"] < ratio <= REPEAT_ACTED_CAP, rk4   # 1.15*(1+.15*log2(4))=1.495
    # single acted_on session — even stacked with dominant_entity — must stay BELOW gold (one hit can be noise)
    c.execute("INSERT INTO hidden_context VALUES('sE','refone','eta theta iota')")         # rowid 3 -> doc hc3
    c.execute("INSERT INTO hidden_context VALUES('sF','refnone','eta theta iota')")        # rowid 4 -> doc hc4
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('hc3','hidden_context','sE','p','2026-06-20','gotcha','session',NULL,1,'eta theta iota')")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('hc4','hidden_context','sF','p','2026-06-20','gotcha','session',NULL,1,'eta theta iota')")
    for k_ in ("recalled_in_session", "acted_on", "dominant_entity"):
        c.execute(f"INSERT INTO signals VALUES('refone','{k_}','u0','')")
    rk5 = rerank(c, "eta theta iota", fts_candidates(c, "eta theta iota"), k=2)
    assert rk5[0]["doc_id"] == "hc3" and rk5[0]["score"] / rk5[1]["score"] < STATUS_WEIGHT["gold"], rk5
    # machine gold (judge_gold): boosts a cold doc x1.1, stays in the served-only tier (< human gold 1.25)
    c.execute("INSERT INTO hidden_context VALUES('sG','refjudge','kappa lambda mu')")      # rowid 5 -> doc hc5
    c.execute("INSERT INTO hidden_context VALUES('sH','refplain','kappa lambda mu')")      # rowid 6 -> doc hc6
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('hc5','hidden_context','sG','p','2026-06-20','gotcha','session',NULL,1,'kappa lambda mu')")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('hc6','hidden_context','sH','p','2026-06-20','gotcha','session',NULL,1,'kappa lambda mu')")
    c.execute("INSERT INTO signals VALUES('refjudge','judge_gold','judge','')")
    rk6 = rerank(c, "kappa lambda mu", fts_candidates(c, "kappa lambda mu"), k=2)
    assert rk6[0]["doc_id"] == "hc5" and abs(rk6[0]["score"] / rk6[1]["score"] - JUDGE_GOLD_FACTOR) < 0.01, rk6
    # project boost: the SAME doc scores exactly x1.2 higher when its project matches the caller's cwd-folder,
    # and is unchanged for a non-matching (or absent) project — a multiplicative reorder that never drops rows.
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) VALUES('pa','summary','sP','acme-ai','2026-06-20','summary','session',NULL,1,'nu xi omicron pi')")
    rows_p = fts_candidates(c, "nu xi omicron pi")
    base = rerank(c, "nu xi omicron pi", rows_p, k=1)[0]["score"]
    boosted = rerank(c, "nu xi omicron pi", rows_p, k=1, project="acme-ai")[0]["score"]
    assert abs(boosted / base - PROJECT_BOOST) < 0.01, (base, boosted)
    assert rerank(c, "nu xi omicron pi", rows_p, k=1, project="other-repo")[0]["score"] == base   # non-match: no change
    # BOTH-SIDES CONTRACT (the bug this guards): the doc's project comes from ingest normalizing the WRITER's
    # cwd; the caller's comes from apiserver normalizing the ASKER's cwd. Both go through normalize_project, so
    # a session asking from ANY worktree of acme-ai must still boost acme-ai docs. If either side stops
    # normalizing, this fails — no other test covers the client->server project path.
    from analyzer.project import normalize_project      # selfcheck-only import: not on the recall hot path
    asker_cwd = "/Users/x/Documents/Git/acme-ai/.claude/worktrees/agent-ab12cd34ef56ab78c/server"
    assert normalize_project(asker_cwd) == "acme-ai"   # ...and ingest stored 'acme-ai' for the same repo
    wt = rerank(c, "nu xi omicron pi", rows_p, k=1, project=normalize_project(asker_cwd))[0]["score"]
    assert abs(wt / base - PROJECT_BOOST) < 0.01, (base, wt)
    # LEGACY client (pre-cwd): posts project=basename(cwd) -> 'server'. Accepted degradation — NO boost (exactly
    # x1.0), never a wrong one. Asserted, so a one-sided rollout is a documented state and not a silent surprise.
    assert rerank(c, "nu xi omicron pi", rows_p, k=1, project="server")[0]["score"] == base
    assert rerank(c, "nu xi omicron pi", rows_p, k=1, project=None)[0]["score"] == base   # no project at all

    # schema v2 — validity filter + window overlap (ported from #70's __main__ additions). A fact
    # with a CLOSED validity window is absent from default candidates and reachable only via the
    # include_invalid escape hatch; a [first_at, started_at] span satisfies both since and until.
    _V2 = ("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,"
           "text,first_at,valid_at,invalid_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    c.execute(_V2, ("hcv", "hidden_context", "sV", "p", "2026-07-29", "decision", "session", None, 1,
                    "sigma tau upsilon reversed thing", "2026-07-01", "2026-07-01", "2026-07-20", "superseded"))
    c.execute(_V2, ("hcw", "hidden_context", "sW", "p", "2026-07-29", "decision", "session", None, 1,
                    "sigma tau upsilon current thing", "2026-07-01", "2026-07-01", None, None))
    ids = lambda **kw: {r[0] for r in fts_candidates(c, "sigma tau upsilon", **kw)}
    assert ids() == {"hcw"}, "a closed-window fact must be absent from DEFAULT candidates"
    assert ids(include_invalid=True) == {"hcv", "hcw"}, "…and retrievable via the escape hatch"
    hit = [h for h in rerank(c, "sigma tau upsilon", fts_candidates(c, "sigma tau upsilon", include_invalid=True), k=5)
           if h["doc_id"] == "hcv"][0]
    assert hit["invalid_at"] == "2026-07-20" and hit["state"] == "superseded", \
        "the validity window must ride the hit payload (siblings render the banner from it)"
    assert ids(since="2026-07-25") == {"hcw"}, "re-asserted this week -> a 'this week' result"
    assert ids(until="2026-07-10") == {"hcw"}, "first asserted in early July -> a 'back then' result"
    assert not ids(since="2026-08-01") and not ids(until="2026-06-30"), "fully outside the span -> no hit"


def test_code_home_reach(monkeypatch):
    """A fact whose repo/path appears NOWHERE in its prose is still reachable by that path — and finding
    it must not perturb the bm25 ranking of anything else (the candidate source is APPENDED)."""
    monkeypatch.setenv("CSA_TRACE", "0")
    from analyzer import search
    from analyzer.store import build_fts, load_sqlite, sqlite_db

    con = sqlite_db(":memory:")
    sess = dict(session_id="s1", project="csa", git_branch="b", started_at="2026-07-01T09:00:00Z",
                ended_at="2026-07-01T10:00:00Z", n_user=1, n_asst=1, user="jules@example.com")
    load_sqlite(con, sess, {"hidden_context": [
        # the shipped incident shape: the claim never names the file it is about
        {"type": "gotcha", "claim": "PR #45 is verified clean for header-bearer clients",
         "evidence": "checked the gate", "repo": "csa", "path": "server/analyzer/auth.py"},
        {"type": "decision", "claim": "the retry loop swallows the 429", "evidence": "seen in prod",
         "repo": "ledger", "path": "etl/backoff.py"},
    ]})
    build_fts(con)

    def ids(q):
        return [r[0] for r in search.fts_candidates(con, q)]

    # reachable by the FULL path, by the BASENAME alone, and by the REPO — none of which its text says
    for q in ("server/analyzer/auth.py", "auth.py", "what changed in auth.py?"):
        assert ids(q), f"no candidate for {q!r}"
        assert any("auth" in (con.execute("SELECT path FROM hidden_context WHERE rowid=?",
                                          (int(d[2:]),)).fetchone() or [""])[0]
                   for d in ids(q) if d[:2] == "hc"), f"the auth.py fact is not reachable by {q!r}"
    assert len(ids("backoff.py")) == 1, "basename match must not drag in the whole corpus"
    # repo matches whole-word only: 'ledger' finds it, a substring of it does not
    assert ids("ledger"), "repo name must reach its facts"
    assert not ids("led"), "repo match must be exact, not a substring (that is what floods)"
    # scored by WHAT matched: an exact path is far more specific than a repo name, and must rank above it
    from analyzer.search import CODE_HOME_BM_PATH, CODE_HOME_BM_REPO
    bm = lambda q, d: next(r[10] for r in search.fts_candidates(con, q) if r[0] == d)
    doc = ids("auth.py")[0]
    assert bm("auth.py", doc) == CODE_HOME_BM_PATH, "a path match must score as a path match"
    assert bm("csa", doc) == CODE_HOME_BM_REPO, "a repo-only match must not score like a path match"
    assert CODE_HOME_BM_PATH < CODE_HOME_BM_REPO, "lower bm25 = stronger; path must beat repo"
    # APPEND-ONLY: a query with no path/repo token gets byte-identical candidates to the bm25 query alone
    q = "retry loop swallows"
    plain = con.execute(
        "SELECT doc_id FROM docs WHERE docs MATCH ? AND invalid_at IS NULL ORDER BY bm25(docs) LIMIT 60",
        (search._fts_query(q),)).fetchall()
    assert [r[0] for r in plain] == ids(q), "code-home candidates must never reorder the bm25 set"
    # and the fact's rendered text stays free of the path (code-home is a lookup key, not prose)
    assert "auth.py" not in con.execute(
        "SELECT text FROM docs WHERE doc_id=?", (ids("auth.py")[0],)).fetchone()[0]


def test_code_home_survives_partial_schema():
    """No hidden_context table (selfcheck dbs, old corpora) costs the EXTRA candidates, not all of them."""
    from analyzer.store import DOCS_COLS
    c = sqlite3.connect(":memory:")
    c.execute(f"CREATE VIRTUAL TABLE docs USING fts5({DOCS_COLS})")
    c.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,"
              "corroboration,text) VALUES('1','hidden_context','s1','p','2026-06-20','gotcha','session',"
              "NULL,1,'redis backpressure in main.go')")
    assert [r[0] for r in fts_candidates(c, "backpressure main.go")] == ["1"]


def test_tiny_corpus_single_term_found(monkeypatch):
    """A trial-sized workspace: a term in most of its docs gets fts5's clamped IDF, so bm25 sits a hair
    under 0 and rel a hair over 0.5. The floor must still let every match through."""
    monkeypatch.setenv("CSA_TRACE", "0")
    from analyzer import search
    from analyzer.store import build_fts, load_sqlite, sqlite_db

    con = sqlite_db(":memory:")
    for sid, user, claims in (
            ("alice", "alice@tiny.test", ["Daily billing resets run at midnight UTC."]),
            ("bob", "bob@tiny.test", ["Install the client with the one-line installer.",
                                      "Re-run setup after an install to wire all three hooks.",
                                      "An install on a new laptop needs a token first."])):
        sess = dict(session_id=sid, project="trial", git_branch="main", started_at="2026-10-01T09:00:00Z",
                    ended_at="2026-10-01T10:00:00Z", n_user=1, n_asst=1, user=user)
        load_sqlite(con, sess, {"hidden_context": [
            {"type": "decision", "claim": c, "evidence": c} for c in claims]})
    build_fts(con)

    rows = search.fts_candidates(con, "install")
    assert len(rows) == 3 and all(-1e-3 < r[10] < 0 for r in rows), "the IDF clamp is what this test pins"
    hits = search.search(con, "install")
    assert len(hits) == 3 and {h["session_id"] for h in hits} == {"bob"}
    assert all(h["rel"] >= 0.5 for h in hits)
