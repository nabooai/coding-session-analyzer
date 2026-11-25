"""app.py (recall/session_facts) tests — the module's former __main__ self-check, ported.

The four hermetic groups are ported faithfully: locate + in_session (seeded temp corpus),
the _person_digest contract (:memory: raw con), code-home rendering, and the probe-3
self-scope regression. The former live-corpus groups (they ran against the machine's real
data/analyzer.db) are RE-SEEDED hermetically here rather than dropped:
  - `recall("ETL")` grouped markdown, person='mara' provenance, and the unknown-person
    roster message run against the seeded locate corpus (same owner/topic shape);
  - the strict/ambient gate group (distinctive topic fires, generic tightens, zamboni
    control + long off-topic paste abstain) runs against a corpus seeded with the WAL
    fact the prod corpus provided;
  - person-lacks-topic (divider + team results) re-seeds the two-owner corpus instead of
    `shutil.copy`ing the live db.
DROPPED: only the closing demo `print(recall("ETL", k=8))` (804-805) — display only, no
assertions. NB the hermetic re-seeds pass fill=False where the original relied on the
live corpus already having hits: cold-fill in a test would grep this machine's
~/.claude/projects and shell out to `claude -p`.

The seeded fixtures chdir into tmp_path: recall()/session_facts() open their own connection
through store.org_db(), which resolves `data/` against the cwd on every call."""
import re
import sqlite3
import time

import pytest

from app import _person_digest, recall, session_facts
from analyzer import store

_S = [("d00d1111-2ffa-483f-ac09-ca5bcd6f11ba", "acme-ai", "eng-etl-v2-direct-graphdb", "2026-07-19",
       "Continuation: user asked whether worker_daemon could be absorbed into etl.main as one scheduler."),
      ("d00d2222-60e0-4560-8268-ff3d4f928f8e", "acme-ai", "eng-etl-v2-direct-graphdb", "2026-06-28",
       "Benchmark report concluding acme's ETL should stay on native SQLite, not Kafka+Postgres."),
      ("c0ffee00-0000-0000-0000-000000000000", "csa", "master", "2026-07-20", "Unrelated UI work."),
      ("c0ffee11-1111-1111-1111-111111111111", "csa", "master", "2026-07-20", "Also unrelated.")]


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    """The __main__'s seeded temp corpus: 4 mara-owned sessions, 4 facts, 1 worked_on, FTS built.
    The test chdirs into tmp_path so recall()/session_facts() (which route through store.org_db())
    see the temp box's corpus instead of this machine's."""
    monkeypatch.chdir(tmp_path)
    c = store.sqlite_db(store.org_db())
    for sid, p, b, d, summ in _S:
        c.execute("INSERT INTO sessions(session_id,project,git_branch,started_at,ended_at,source,user,summary) "
                  "VALUES (?,?,?,?,?,'session','mara@example.com',?)",
                  (sid, p, b, f"{d}T08:00:00Z", f"{d}T12:00:00Z", summ))
    for sid, ty, ref, claim in [
            (_S[0][0], "decision", "r1", "Ran the full ETL suite E2E in a fresh detached worktree at fa31829d9"),
            (_S[0][0], "constraint", "r2", "test_v2_integration.py is the canonical whole-pipeline E2E check"),
            (_S[1][0], "decision", "r3", "acme ETL stays on native SQLite; acme scale is 11GB/2.46M nodes"),
            (_S[2][0], "gotcha", "r4", "the sidebar logo renders dark on a dark theme")]:
        c.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES (?,?,?,?,'')",
                  (sid, ty, ref, claim))
    c.execute("INSERT INTO worked_on VALUES (?,?)", (_S[0][0], "ETL E2E verification in fresh worktree"))
    c.commit()
    store.build_fts(c)
    return c


def test_locate_session_list(seeded):
    md = recall("find me the session that ran the ETL E2E", fill=False)
    assert md.startswith("# Sessions matching"), ("locate must return a session list", md[:200])
    assert _S[0][0] in md and "matched:" in md, md
    assert md.count("\n- ") <= 5, ("locate stays context-cheap: <=5 session rows", md)


def test_locate_fact_page_unchanged_ambient_never_locates(seeded):
    # the fact page is untouched for an ordinary question, and the ambient path NEVER renders a session list
    assert recall("acme SQLite benchmark", fill=False).startswith("# Recalled context")
    assert not recall("which session ran the ETL E2E", fill=False, strict=True).startswith("# Sessions matching")


def test_locate_absent_terms_named(seeded):
    # a rare word of yours that the corpus has NEVER indexed is reported (it can't be ranking anything) —
    # but only while it's the exception; a cold corpus where nothing matches must not cry wolf every time
    md = recall("which session ran the ETL E2E in a zzzqqq worktree", fill=False)
    assert "zzzqqq" in md and "no indexed fact anywhere" in md, ("absent term must be named", md)
    assert "no indexed fact anywhere" not in recall("which session covered aaa bbb ccc ddd", fill=False)


def test_locate_named_date_hard_filters(seeded):
    # the date the asker names filters the LIST (route.date_window), not just its order
    md = recall("which sessions on July 19 ran the ETL", fill=False)
    assert _S[0][0] in md and _S[1][0] not in md, ("date filter must scope the session list", md)


def test_session_facts_prefix_lookup(seeded):
    # in_session: the follow-up. 8-char prefix (what provenance lines show) resolves; ambiguity is honest.
    fx = session_facts("d00d1111")
    assert "worker_daemon" in fx and "fa31829d9" in fx and "Worked on:" in fx, fx
    assert "no indexed session" not in fx.lower()
    assert "Use the full id" in session_facts("c0ffee"), "an ambiguous prefix must not silently pick one"
    assert "No indexed session" in session_facts("deadbeef")


def test_recall_grouped_markdown(seeded):
    # (was live-corpus) recall returns grouped markdown; re-seeded corpus has ETL facts
    md = recall("ETL", k=8, fill=False)
    assert md.startswith("# Recalled context for") and "## " in md, md[:200]


def test_recall_person_mara_provenance(seeded):
    # (a) Ask X (was live-corpus): with person='mara', every provenance email before any divider is
    # mara-owned. (Was "ETL deploy" — the live corpus had deploy content; the seeded one covers this.)
    md = recall("ETL E2E worktree", k=8, fill=False, person="mara")
    head = md.split("\n---\n")[0]
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", head))
    assert "## " in head and emails and all("mara" in e for e in emails), (sorted(emails), md[:300])


def test_recall_unknown_person_fails_soft(seeded):
    # (b) unknown person fails soft: not-found message listing the known owners
    md = recall("ETL", fill=False, person="zzz-nobody")
    assert "zzz-nobody" in md and "mara@example.com" in md and not md.startswith("# Recalled"), md[:300]


@pytest.fixture
def strict_corpus(monkeypatch, tmp_path):
    """(was live-corpus) the strict/ambient-gate corpus: one distinctive WAL fact to fire on."""
    monkeypatch.chdir(tmp_path)
    c = store.sqlite_db(store.org_db())
    c.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
              "VALUES ('sw1','csa','2026-07-01T00:00:00Z','session','mara@example.com')")
    c.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
              "('sw1','decision','rw1','graph.db opens with WAL and a single writer queue to avoid "
              "SQLITE_BUSY under concurrent readers','')")
    c.commit()
    store.build_fts(c)
    return c


def test_recall_strict_fires_and_tightens(strict_corpus):
    # strict (ambient) gate: a distinctive-term topic still fires; strict never injects MORE than lenient,
    # and a query of only common words (no distinctive term) abstains with the "No strong" message.
    md = recall("graph.db WAL single writer queue", k=8, fill=False, strict=True)
    assert md.startswith("# Recalled context for"), ("strict should fire on a distinctive topic", md[:200])
    generic = "please update the code and run it again right now"
    nl = recall(generic, k=8, fill=False).count("\n- ")
    sm = recall(generic, k=8, fill=False, strict=True)
    ns = sm.count("\n- ")
    assert ns <= nl and (sm.startswith("No strong") or ns < nl or nl == 0), ("strict must tighten", nl, ns, sm[:120])


def test_recall_strict_regressions_abstain(strict_corpus):
    # The two named ambient regressions (both FIRED on master against the prod corpus). Re-seeded:
    # the corpus has real facts, so the floors — not an empty index — do the abstaining.
    for label, probe in (
            ("zamboni control", "what did we decide about the zamboni refurbishment budget"),
            ("long off-topic paste",                     # >160 chars, no corpus-relevant content
             'Traceback (most recent call last):\n  File "/opt/homebrew/lib/python3.12/site-packages/urllib3/'
             'connectionpool.py", line 715, in urlopen\n    httplib_response = self._make_request(conn, method,'
             ' url)\nssl.SSLCertVerificationError: certificate verify failed: unable to get local issuer'
             ' certificate (_ssl.c:1000)')):
        m = {}
        out_md = recall(probe, k=8, fill=False, strict=True, metrics=m)
        assert not out_md.startswith("# Recalled context for"), (label, out_md[:200])
        assert "score" not in m or m["score"] is not None, ("abstain must still log a score", label)


def test_person_digest_contract():
    # who+when digest contract (review fixes): recency order, `since` window, and the REJECT FILTER — a
    # human-flagged 'wrong' fact must never resurface in "what did X work on" (mirrors build_fts, which the
    # digest bypasses). Self-contained temp corpus (no FTS/rerank needed — _person_digest is plain SQL).
    _d = sqlite3.connect(":memory:")
    _d.executescript("CREATE TABLE sessions(session_id TEXT, project TEXT, started_at TEXT, ended_at TEXT, source TEXT, user TEXT);"
                     "CREATE TABLE hidden_context(session_id TEXT, type TEXT, ref TEXT, claim TEXT, evidence TEXT);"
                     "CREATE TABLE reviews(kind TEXT, ref TEXT, status TEXT, note TEXT);"
                     "CREATE TABLE claim_clusters(ref TEXT, cluster_id TEXT, sessions INT);"
                     "CREATE TABLE facts(ref TEXT PRIMARY KEY, invalid_at TEXT);")   # digest joins validity (v2)
    # s1 is a MULTI-DAY session: started 07-11 but last active 07-13 — it must date/filter by ended_at.
    _d.execute("INSERT INTO sessions VALUES('s1','ledger','2026-07-11T00:00:00Z','2026-07-13T00:00:00Z','session','jules@example.com')")
    _d.execute("INSERT INTO sessions VALUES('s0','ledger','2026-06-01T00:00:00Z','2026-06-01T00:00:00Z','session','jules@example.com')")
    for sid, ty, ref, claim in [("s1", "gotcha", "r1", "recent fact"), ("s1", "decision", "r2", "wrong fact"),
                                ("s0", "gotcha", "r3", "old fact")]:
        _d.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES(?,?,?,?,'')", (sid, ty, ref, claim))
    claims = [h["text"] for h in _person_digest(_d, "jules@example.com", None, 10)]
    assert claims[-1] == "old fact" and "recent fact" in claims, claims          # recency: June fact sorts last
    since_claims = [h["text"] for h in _person_digest(_d, "jules@example.com", "2026-07-07", 10)]
    assert "old fact" not in since_claims and "recent fact" in since_claims, since_claims   # `since` drops June
    # LAST-ACTIVITY: s1's since window keys on ended_at (07-13), so `since=2026-07-12` KEEPS it (active 07-13)
    # even though it STARTED 07-11 — the multi-day bug this fixes. And `until` bounds the top of the window.
    keep = [h["text"] for h in _person_digest(_d, "jules@example.com", "2026-07-12", 10)]
    assert "recent fact" in keep, "a session active AFTER `since` must show even if it started before it"
    windowed = [h["text"] for h in _person_digest(_d, None, "2026-07-01", 10, until="2026-07-14")]
    assert "recent fact" in windowed and "old fact" not in windowed, windowed   # date-only digest (no person)
    assert _person_digest(_d, "mara@example.com", None, 10) == [], "owner-scope must not leak jules's facts to mara"
    _d.execute("INSERT INTO reviews VALUES('hc','r2','wrong','human')")
    assert "wrong fact" not in [h["text"] for h in _person_digest(_d, "jules@example.com", None, 10)]  # reject filter
    # schema v2: the digest bypasses docs, so it must apply the validity default itself — a superseded fact
    # must never ride "what did X work on" (the ambient/person path is where an unannotated stale fact is
    # most harmful: injected unbidden, rendered with no banner).
    _d.execute("INSERT INTO facts VALUES('r3','2026-07-20')")
    assert "old fact" not in [h["text"] for h in _person_digest(_d, "jules@example.com", None, 10)], \
        "a closed-validity fact must be excluded from the person digest"


def test_recall_person_lacks_topic_falls_through(monkeypatch, tmp_path):
    # (c) (was live-corpus: shutil.copy of the real db) person lacks the topic -> divider + team results
    # beneath. Re-seeded: mara owns sessions (so `person="mara"` resolves) but only alice's doc carries
    # the topic.
    tmp = str(tmp_path / "c.db")
    c2 = store.sqlite_db(tmp)
    c2.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
               "VALUES ('sc-mara','csa','2026-07-01T00:00:00Z','session','mara@example.com')")
    c2.execute("INSERT OR REPLACE INTO sessions(session_id,project,started_at,source,user) "
               "VALUES ('sc-alice','proj-x','2026-07-01T00:00:00Z','session','alice@example.com')")
    store.build_fts(c2)                                  # creates the docs FTS table, then seed it directly
    c2.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
               "VALUES ('wo-sc','worked_on','sc-alice','proj-x','2026-07-01T00:00:00Z','worked_on','session',"
               "NULL,1,'zzzgadget flux capacitor calibration')")
    c2.commit()
    c2.close()
    _orig = store.sqlite_db
    monkeypatch.setattr(store, "sqlite_db", lambda *a, **k: _orig(tmp))   # ponytail: monkeypatch >
    md = recall("zzzgadget flux capacitor", k=8, fill=False, person="mara")   # plumbing a con param
    assert "---" in md and "here's what the team knows" in md, md[:400]
    assert "alice@example.com" in md.split("---", 1)[1], md


def test_recall_code_home(monkeypatch, tmp_path):
    # CODE-HOME in the provenance line: the incident shape — a session running in acme-ai records a fact about
    # code that lives in ledger. The line must point at the CODE, not just where the session ran; and a fact with
    # no code-home must render byte-identically to before (NULL columns are the pre-2026-07-16 corpus).
    tmp = str(tmp_path / "h.db")
    _orig = store.sqlite_db
    c3 = _orig(tmp)
    store.load_sqlite(c3, dict(session_id="ch1", project="acme-ai", git_branch=None, started_at="2026-07-06T00:00:00Z",
                               ended_at=None, n_user=1, n_asst=1),
                      {"hidden_context": [
                          {"type": "gotcha", "claim": "qqprefect deploy needs the etl3 image tag pinned",
                           "evidence": "e", "repo": "ledger", "path": "ledgerv2_core/etl3/prefect_deploy.py"},
                          {"type": "gotcha", "claim": "qqprefect runs are billed per worker minute", "evidence": "e"},
                          {"type": "gotcha", "claim": "qqprefect workers need the shared volume mounted",
                           "evidence": "e", "path": "ledgerv2_core/etl3/worker.py"},        # path, NO repo
                          {"type": "gotcha", "claim": "qqprefect ui port clashes with grafana", "evidence": "e",
                           "repo": "ledger"},                                               # repo, no path
                      ]})
    store.build_fts(c3); c3.commit(); c3.close()
    monkeypatch.setattr(store, "sqlite_db", lambda *a, **k: _orig(tmp))   # recall calls sqlite_db(org_db(org_id))
    md = recall("qqprefect", k=8, fill=False)
    line = lambda frag: next(l for l in md.splitlines() if frag in l)
    home_line, bare_line = line("image tag pinned"), line("billed per worker")
    assert "code: ledger/ledgerv2_core/etl3/prefect_deploy.py" in home_line, home_line
    assert "acme-ai/ch1" in home_line, home_line          # provenance still says where it was RECORDED...
    assert "code:" not in bare_line, bare_line             # ...and a non-code fact is untouched
    # path with NO repo: an unrooted path next to project `acme-ai` reads as acme-ai's — the exact
    # mis-routing this feature exists to stop. Render nothing rather than a badge on a guess.
    assert "code:" not in line("shared volume mounted"), line("shared volume mounted")
    assert "code: ledger" in line("clashes with grafana")    # repo without a path still binds the referent
    assert "code: ledger/" not in line("clashes with grafana"), "repo-only must not render a trailing slash"


def test_recall_self_scope_probe3(monkeypatch, tmp_path):
    # (d) probe-3 regression: >=3 GENERIC own hits must not suppress the teammate's distinctive answer,
    # and self-scope (person == asked_by) switches the copy to your-voice. Alice owns the only doc carrying
    # the distinctive term; mara owns three that merely graze the common words. HERMETIC corpus (was:
    # shutil.copy of the default corpus — coupled the assertion to the live dev corpus; env-dependent).
    # Seed exactly two owners so the self/team partition is the only thing under test.
    tmp = str(tmp_path / "d.db")
    _orig = store.sqlite_db
    c2 = _orig(tmp)
    c2.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
               "VALUES ('sd-mara','p','2026-07-01T00:00:00Z','session','mara@example.com')")
    c2.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
               "VALUES ('sd-alice','proj-x','2026-07-01T00:00:00Z','session','alice@example.com')")
    store.build_fts(c2)                                  # creates the docs FTS table (empty), then seed it directly:
    c2.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
               "VALUES ('wo-sd','worked_on','sd-alice','proj-x','2026-07-01T00:00:00Z','worked_on','session',"
               "NULL,1,'fixed the qqfizzbuzz retry storm in the deploy path')")
    for i in range(3):                                   # generic own hits: share only the common words
        c2.execute("INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
                   f"VALUES ('wo-sd{i}','worked_on','sd-mara','p','2026-07-01T00:00:00Z','worked_on','session',"
                   "NULL,1,'general deploy path cleanup notes')")
    c2.commit(); c2.close()
    monkeypatch.setattr(store, "sqlite_db", lambda *a, **k: _orig(tmp))
    md = recall("qqfizzbuzz deploy path", k=8, fill=False,
                person="mara", asked_by="mara@example.com")
    assert "scoped to your own sessions first" in md, md[:300]           # self-voice header
    assert "---" in md and "qqfizzbuzz" in md.split("---", 1)[1], md      # teammate answer NOT suppressed
    assert "your own" in md.split("---", 1)[1].splitlines()[1] or "None of your own" in md, md


def test_recall_reaches_code_home_and_floor_lets_it_through(monkeypatch, tmp_path):
    """The retrieval-reach case: a fact whose claim never names the file it is ABOUT is recalled by that
    file's path — and the coverage floor, which measures PROSE overlap, must not throw it away."""
    tmp = str(tmp_path / "reach.db")
    _orig = store.sqlite_db
    c = _orig(tmp)
    store.load_sqlite(c, dict(session_id="rc1", project="csa", git_branch=None,
                              started_at="2026-07-06T00:00:00Z", ended_at=None, n_user=1, n_asst=1),
                      {"hidden_context": [
                          # the shipped incident shape: the path appears NOWHERE in claim or evidence
                          {"type": "gotcha", "claim": "PR #45 is verified clean for header-bearer clients",
                           "evidence": "walked the gate", "repo": "csa", "path": "server/analyzer/auth.py"},
                          {"type": "gotcha", "claim": "the zamboni resurfaces between periods",
                           "evidence": "unrelated filler"},
                      ]})
    store.build_fts(c); c.commit(); c.close()
    monkeypatch.setattr(store, "sqlite_db", lambda *a, **k: _orig(tmp))
    # 'auth.py' is 1 of 3 query terms — under a pure >=50% prose floor this abstains on its own best answer
    md = recall("what changed in auth.py recently", k=8, fill=False)
    assert "PR #45" in md, md
    assert "server/analyzer/auth.py" in md, "the code-home line still points at the file"
    # reachable by the FULL path too, and the honest miss is untouched
    assert "PR #45" in recall("what do we know about server/analyzer/auth.py", k=8, fill=False)
    miss = recall("zzz nonexistent frobnicator alignment widget", k=8, fill=False)
    assert "Nothing solid" in miss or "No past-session context" in miss, miss   # honest miss intact
    # the exemption is FILENAME-scoped: the repo name alone must not wave every csa fact past the floor
    assert "zamboni" not in recall("csa qqunmatched qqtokens here", k=8, fill=False)


def test_recall_floor_rescues_instead_of_blanking(monkeypatch, tmp_path):
    """The incident this replaces: the top 3 hits collectively graze, so the OLD floor blanked the whole
    response — discarding a fact at rank 9 that covers every single query term. Now that fact is served.
    The rescue is gated behind the collective check, so a query whose top 3 DO cover is untouched."""
    tmp = str(tmp_path / "floor.db")
    _orig = store.sqlite_db
    c = _orig(tmp)
    hc = [{"type": "gotcha", "claim": f"quokka plumbing note {i} rewritten again and again quokka quokka",
           "evidence": "quokka"} for i in range(8)]                       # match ONE term, and outrank...
    hc.append({"type": "preference", "claim": "the quokka migration ships behind a staging flag",
               "evidence": "agreed"})                                     # ...the only fact that answers it
    # dated now: rerank adds recency to relevance, so an aging fixture lets relevance alone lift the answer
    # above the grazers, the top 3 cover the query, and the floor under test never fires
    store.load_sqlite(c, dict(session_id="fl1", project="p", git_branch=None,
                              started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), ended_at=None,
                              n_user=1, n_asst=1),
                      {"hidden_context": hc})
    store.build_fts(c); c.commit(); c.close()
    monkeypatch.setattr(store, "sqlite_db", lambda *a, **k: _orig(tmp))
    md = recall("quokka migration ships staging flag", k=12, fill=False)
    assert "Nothing solid" not in md, "a fact covering EVERY query term must not be blanked with the grazers"
    assert "ships behind a staging flag" in md, md
    assert "plumbing note" not in md, "once the floor fires, only the qualifying hits are served"
    # ADDITIVE: when the top 3 cover the query, the floor never fires and every hit is still rendered
    md2 = recall("quokka plumbing note rewritten", k=12, fill=False)
    assert md2.count("\n- ") >= 8, f"non-grazing queries must be untouched, got {md2.count(chr(10) + '- ')}"


def test_function_words_never_reach_the_fts_query():
    # "about" sits in <6% of terse facts, so the distinctiveness cut called it a term carrying the search
    # (prod coverage for Jules's query: distinctive = [about, embedding, measurements, models]). Function
    # words are dismissed at the query, the way acme-ai's tfidf STOP_WORDS are: rarity is not topicality.
    from analyzer import search, route
    assert search.query_terms("measurements about embedding models") == {"measurements", "embedding", "models"}
    assert search.query_terms("which session did jules work on, and where?") == {"session", "jules", "work"}
    # the locator's own vocabulary is stripped BEFORE retrieval on a locate ask, not only from `matched:`
    assert route.topical("find me the session where jules measured embedding models on ledger") == \
        "jules measured embedding models on ledger"
    assert route.topical("which sessions talked about it") == "it"
