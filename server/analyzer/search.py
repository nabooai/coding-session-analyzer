"""Full-text search (FTS5), statistical re-ranking, and a RAG answer. Side tables are read through
db.run; every statement on `docs` stays raw, since FTS5's MATCH, bm25() and snippet() have no Core face."""
import re, sqlite3, math, os

from sqlalchemy import and_, case, distinct, func, literal, literal_column, or_, select, union

from analyzer import db, llm, store, tracer
from analyzer import entity_gate
from analyzer.models import ClaimCluster, Entity, Signal, hidden_context, mentions, relations
from datetime import datetime, timezone

REL_SCALE = 6.0                                          # spread bm25 so strong matches stay distinguishable
# knowledge types outrank summaries and worked-on lines
TYPE_WEIGHT = {"decision": 1.30, "gotcha": 1.25, "constraint": 1.20, "todo": 1.0,
               "preference": 0.9, "summary": 0.8, "worked_on": 0.7, "entity": 0.85,
               "pr_link": 1.35, "workspace": 1.10}
TRUST = {"memory": 1.2}                                   # modest: must not bury exact answers
STATUS_WEIGHT = {"gold": 1.25, "confirmed": 1.2, "shared": 1.1, "unimportant": 0.6}
# A human's gold; the judge's comes as a judge_gold signal. Unasked (the ambient bar) a fact must be gold.
GOLD_STATUS = ("gold", "confirmed")
# Same-project boost: reorders, never excludes, and stays below an exact cross-project lexical match.
PROJECT_BOOST = 1.2

# Function words never reach the FTS query: rare in a corpus of terse claims, they would read as topical.
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "not", "near", "is", "are",
         "was", "were", "how", "do", "does", "did", "we", "i", "me", "my", "our", "what", "why", "which",
         "where", "when", "who", "whose", "with", "that", "this", "it", "be", "as", "about", "all", "any",
         "from", "into", "he", "she", "his", "her", "him", "they", "them", "their", "its", "you", "your", "us"}

# synonyms the porter stemmer cannot bridge
_SYNONYMS = {"auth": ["authentication", "authorization"], "deploy": ["deployment", "deployed"],
             "k8s": ["kubernetes"], "pg": ["postgres", "postgresql"], "db": ["database"],
             "repo": ["repository"], "perf": ["performance"], "config": ["configuration"]}
_SYN = {}
for _k, _vs in _SYNONYMS.items():
    for _t in [_k, *_vs]:
        _SYN.setdefault(_t, set()).update([_k, *_vs])


def query_words(q):
    """{content word: the word and its synonyms} for a query, every word lowercased."""
    words = {t.lower() for t in re.findall(r"[A-Za-z0-9_.\-]+", q) if len(t) > 1 and t.lower() not in _STOP}
    return {w: {w, *_SYN.get(w, ())} for w in words}


def query_terms(q):
    """Lowercased content terms from a query, synonym-expanded. Shared by FTS and the lazy-fill grep selector."""
    return set().union(*query_words(q).values())


def term_df(con, terms):
    """{term: document frequency} over the FTS docs, one COUNT per term; an unreadable term counts 0."""
    out = {}
    for t in terms:
        try:
            out[t] = con.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH ?", (f'"{t}"',)).fetchone()[0]
        except Exception:
            out[t] = 0
    return out


def rare_terms(con, terms, k=5):
    """The k rarest of `terms` in the corpus, rarest first: the query's own topic words. Relative, since
    in terse claims ordinary words are rare too. df == 0 terms rank in: a pasted stack trace's k rarest
    are unmatchable, so it abstains instead of matching on its generic words.

    Pass word-like terms only: FTS tokenizes punctuation away, so "..." reports df 0 while a substring
    matcher finds it everywhere."""
    terms = list(terms)                                  # iterated twice: a generator would return []
    df = term_df(con, terms)
    return sorted(terms, key=lambda t: (df[t], t))[:k]


def distinctive_terms(con, q, max_df_frac=0.06):
    """The query terms in at most `max_df_frac` of the docs (or 3): an absolute document-frequency cut,
    used by the person partition."""
    terms = query_terms(q)
    if not terms:
        return set()
    try:                                                 # unreadable: every term passes
        total = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0] or 1
    except Exception:
        return terms
    cap = max(3, int(max_df_frac * total))
    out = {t for t, df in term_df(con, terms).items() if 0 < df <= cap}
    return out


def _fts_query(q):
    return " OR ".join(f'"{t}"' for t in query_terms(q)) or '"_"'


def servable(review, fact):
    """The serving rule for a query that bypasses `docs`: no hiding review, and a validity window still
    open (a superseded or refuted fact has a closed one)."""
    return and_(fact.c.invalid_at.is_(None),
                or_(review.c.status.is_(None), review.c.status.notin_(store.HIDE_STATUS)))


def src_label(sid):
    """Human-readable source tag: memory filename, else short session id."""
    return os.path.basename(sid) if sid and sid.startswith("mem:") else (sid or "")[:8]


def _age_days(started_at):
    if not started_at:
        return 9999.0
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return 9999.0


def fts_candidates(con, q, limit=60, since=None, until=None, include_invalid=False):
    """bm25 candidates for `q`, plus code-home ones. since/until is a hard [since, until) filter, an
    overlap test on a fact's [first_at, started_at] span. A fact with a closed validity window is left
    out unless `include_invalid`. Empty while `docs` is mid-rebuild."""
    clause, filt = "", []
    if not include_invalid:
        clause += " AND invalid_at IS NULL"
    if since:
        clause += " AND started_at >= ?"
        filt.append(since)
    if until:
        clause += " AND IFNULL(first_at, started_at) < ?"
        filt.append(until)
    try:                                  # OperationalError is exactly the rebuild window: keep it narrow
        rows = con.execute(
            "SELECT doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,"
            "snippet(docs,9,'[',']','…',14) snip, bm25(docs) bm, "   # col 9 = text
            "first_at, valid_at, invalid_at, state "
            "FROM docs WHERE docs MATCH ?" + clause + " ORDER BY bm25(docs) LIMIT ?",
            [_fts_query(q), *filt, limit]).fetchall()
        return rows + _code_home_rows(con, q, clause, filt, [r[0] for r in rows])
    except sqlite3.OperationalError:
        tracer.set_attr(**{"csa.index.rebuilding": True})
        return []


# Synthetic bm25 for code-home candidates, which carry no MATCH: a named file is about as specific as a
# query gets, a named repo barely better than a topic word. Near the strong end of real bm25 values.
CODE_HOME_BM_PATH = -14.0    # rel ≈ 0.91: an exact path match outranks prose grazing, not an exact quote
CODE_HOME_BM_REPO = -4.0     # rel ≈ 0.66: barely over rerank's 0.5 floor
FILEISH = re.compile(r"^[a-z0-9_-]+\.[a-z0-9_.-]+$")     # 'main.go', 'auth.py'; app.recall reads it too


def _code_home_rows(con, q, clause, filt, seen, limit=20):
    """Candidates reached by where the code lives (hidden_context.repo/path), which their prose often
    never names. Appended, not an indexed FTS column: fts5's bm25 normalizes by total row length, so a
    longer row would rescore the whole corpus. Owner and project are not matched: recall already scopes
    and boosts by them."""
    terms = query_terms(q)
    if not terms:                                    # nothing to match on, and `IN ()` is not valid SQL
        return []
    files = sorted(t for t in terms if FILEISH.match(t))
    # a repo matches exactly; a path by substring, so a basename reaches the full repo-relative path
    pathcond = " OR ".join(["LOWER(IFNULL(path,'')) LIKE ?"] * len(files)) if files else "0"
    pathargs = [f"%{f}%" for f in files]                       # binds for pathcond, used TWICE below
    cond = f"({pathcond}) OR LOWER(IFNULL(repo,'')) IN (%s)" % ",".join("?" * len(terms))
    args = pathargs + sorted(terms)                            # binds for cond
    try:   # its own guard: a partial schema costs the extra candidates, not the whole result
        # path matches first, newest first, so the LIMIT truncates deterministically
        by_id = dict(con.execute(
            f"SELECT 'hc'||rowid, ({pathcond}) FROM hidden_context WHERE {cond} "
            "ORDER BY 2 DESC, rowid DESC LIMIT ?",
            [*pathargs, *args, limit]).fetchall())
        by_id = {d: p for d, p in by_id.items() if d not in seen}
        if not by_id:
            return []
        rows = con.execute(
            "SELECT doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,"
            "substr(text,1,200) snip,0 bm,"      # no MATCH, so no snippet()/bm25(): bm filled below
            "first_at,valid_at,invalid_at,state "
            "FROM docs WHERE doc_id IN (%s)" % ",".join("?" * len(by_id)) + clause,
            [*by_id, *filt]).fetchall()
        return [(*r[:10], CODE_HOME_BM_PATH if by_id.get(r[0]) else CODE_HOME_BM_REPO, *r[11:])
                for r in rows]
    except sqlite3.OperationalError:
        return []


# --- implicit-usage boost (feeds rerank via _signal_factors) ---------------------------------------
SERVED_ONLY_CAP = 1.15                               # served but never confirmed used: stays below gold (1.25)
DOMINANT_FACTOR = 1.1                                # fact mentions the session's dominant entity
JUDGE_GOLD_FACTOR = 1.1                              # LLM judge rated it gold: machine opinion, capped tier only
SINGLE_ACTED_CAP = STATUS_WEIGHT["gold"] - 0.01      # one acted_on hit can be detector noise: never beats gold
REPEAT_ACTED_CAP = 1.5                               # used in >=2 distinct sessions: may outrank one-time gold

_SIG = Signal.__table__
_ENT = Entity.__table__
_CC = ClaimCluster.__table__


def _ref_by_doc(con, doc_ids):
    """doc_id -> fact ref: `docs` keys by rowid, `signals` by ref."""
    hc_rowids = [int(d[2:]) for d in doc_ids if d[:2] == "hc" and d[2:].isdigit()]
    if not hc_rowids:
        return {}
    try:
        by_rowid = dict(db.run(con, select(literal_column("rowid"), hidden_context.c.ref)
                               .where(literal_column("rowid").in_(hc_rowids))).fetchall())
    except sqlite3.OperationalError:                 # partial schema (old db): no refs, no gate, no crash
        return {}
    return {f"hc{rowid}": ref for rowid, ref in by_rowid.items() if ref}


def gold_docs(con, doc_ids):
    """Of `doc_ids`, the ones the LLM judge labelled gold (the ambient bar; rerank only weighs it)."""
    ref_by_doc = _ref_by_doc(con, doc_ids)
    if not ref_by_doc:
        return frozenset()
    try:
        gold = {r for (r,) in db.run(con, select(_SIG.c.ref).where(
            _SIG.c.ref.in_(sorted(set(ref_by_doc.values()))), _SIG.c.kind == literal("judge_gold")))}
    except sqlite3.OperationalError:
        return frozenset()
    return frozenset(doc for doc, ref in ref_by_doc.items() if ref in gold)


def _signal_factors(con, rows):
    """Implicit-usage rank factor per hidden_context doc, two tiers:
    - served-only (recalled, no confirmed use): 1 + 0.05*log2(1+sessions), *1.1 if dominant_entity,
      *JUDGE_GOLD_FACTOR if the LLM judge labeled it gold — cap SERVED_ONLY_CAP, below human gold:
      being served is the engine's own pick and a machine label is machine opinion, both weak evidence.
    - acted_on (served AND used in a later session): extra 1 + 0.15*log2(1+acted_sessions).
      A SINGLE acted session caps at SINGLE_ACTED_CAP, just under gold (the detector is a token-overlap
      heuristic, one hit can be noise); only 2+ distinct acted sessions reach REPEAT_ACTED_CAP."""
    ref_by_doc = _ref_by_doc(con, [r[0] for r in rows])
    if not ref_by_doc:
        return {}
    try:
        refs = sorted(set(ref_by_doc.values()))
        boost = {}
        for ref, served, acted, dominant, judge_gold in db.run(con, select(
                _SIG.c.ref,
                func.count(distinct(case((_SIG.c.kind.in_(["recalled_in_session", "acted_on"]),
                                          _SIG.c.session_id)))).label("served_sessions"),
                func.count(distinct(case((_SIG.c.kind == "acted_on",
                                          _SIG.c.session_id)))).label("acted_sessions"),
                func.max(_SIG.c.kind == literal("dominant_entity")).label("has_dominant"),
                func.max(_SIG.c.kind == literal("judge_gold")).label("judge_gold"))
                .where(_SIG.c.ref.in_(refs)).group_by(_SIG.c.ref)):
            factor = 1.0 + 0.05 * math.log2(1 + served)
            if dominant:
                factor *= DOMINANT_FACTOR
            if judge_gold:
                factor *= JUDGE_GOLD_FACTOR
            factor = min(SERVED_ONLY_CAP, factor)
            if acted:
                cap = REPEAT_ACTED_CAP if acted >= 2 else SINGLE_ACTED_CAP
                factor = min(cap, factor * (1.0 + 0.15 * math.log2(1 + acted)))
            boost[ref] = factor
        return {doc: boost[ref] for doc, ref in ref_by_doc.items() if ref in boost}
    except sqlite3.OperationalError:                 # partial schema (old db): no boost, no crash
        return {}


def rerank(con, q, rows, k=10, half_life=90.0, floor=0.5, project=None):
    """Score candidates by calibrated bm25, recency and entity overlap, scaled by type, trust, review
    status, corroboration, usage and project; one hit per near-duplicate cluster; the top `k`."""
    if not rows:
        return []
    qtokens = set(re.findall(r"[a-z0-9.\-_]+", q.lower()))
    q_ents = {e[0] for e in db.run(con, select(_ENT.c.id)) if e[0] and e[0].lower() in qtokens}
    # q_ents is not gated: the user typed the entity's full id
    neighbors = set()                                # 1-hop graph neighbors of the query entities
    if q_ents:
        qe = [*q_ents]
        for (nid,) in db.run(con, union(
                select(relations.c.target_id).where(relations.c.source_id.in_(qe)),
                select(relations.c.source_id).where(relations.c.target_id.in_(qe)))):
            if nid:
                neighbors.add(nid)
        neighbors = entity_gate.served_ids(con, neighbors)   # gated: nobody typed them
    factors = _signal_factors(con, rows)
    out = []
    for doc_id, kind, sid, proj, started, etype, source, status, corrob_n, snip, bm, *v2 in rows:
        first_at, valid_at, invalid_at, state = v2 if v2 else (None, None, None, None)
        rel = 1.0 / (1.0 + math.exp(bm / REL_SCALE))  # calibrated, non-saturating
        if rel < floor:
            continue
        # recency from the last mention: staleness is the validity filter's job, freshness is this one's
        rec = 0.5 ** (_age_days(started) / half_life)
        ov = 0.0
        if sid and (q_ents or neighbors):
            ment = {m[0] for m in db.run(con, select(mentions.c.entity_id)
                                         .where(mentions.c.session_id == sid))}
            direct, neigh = len(ment & q_ents), len(ment & neighbors)
            ov = min(1.0, min(direct, 3) / 3.0 + 0.5 * min(neigh, 2) / 2.0)
        # corroboration across sessions, log-saturating and capped below an exact lexical match
        corrob = min(1.3, 1.0 + 0.2 * math.log2(corrob_n)) if corrob_n and corrob_n > 1 else 1.0
        proj_boost = PROJECT_BOOST if project and proj == project else 1.0
        score = (0.60 * rel + 0.25 * rec + 0.15 * ov) * TYPE_WEIGHT.get(etype, 1.0) \
            * TRUST.get(source, 1.0) * STATUS_WEIGHT.get(status, 1.0) * corrob * factors.get(doc_id, 1.0) * proj_boost
        out.append(dict(doc_id=doc_id, kind=kind, session_id=sid, project=proj, started_at=started,
                        type=etype, source=source, status=status, snippet=snip, bm25=round(bm, 2),
                        rel=round(rel, 3), rec=round(rec, 3), ov=round(ov, 2),
                        corrob=round(corrob, 2), corroboration=corrob_n, score=round(score, 3),
                        first_at=first_at, valid_at=valid_at, invalid_at=invalid_at, state=state))
    out.sort(key=lambda h: -h["score"])              # stable: bm25 order breaks ties
    # one hit per near-duplicate cluster, before the k-cut
    seen, uniq = set(), []
    _h, _cc = hidden_context.alias("h"), _CC.alias("cc")
    for h in out:
        key = (h["kind"], h["snippet"])
        if h["kind"] == "hidden_context" and h["doc_id"][:2] == "hc" and h["doc_id"][2:].isdigit():
            try:
                row = db.run(con, select(func.ifnull(_cc.c.cluster_id, _h.c.ref))
                             .select_from(_h.outerjoin(_cc, _cc.c.ref == _h.c.ref))
                             .where(literal_column("h.rowid") == int(h["doc_id"][2:]))).fetchone()
                key = ("hc", row[0] if row and row[0] else h["doc_id"])
            except sqlite3.OperationalError:         # partial schema: no cluster dedup
                pass
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq[:k]


def search(con, q, k=10, project=None, since=None, until=None):
    with tracer.span("retrieve.fts", "RETRIEVER", **{"csa.engine": "fts", "db.statement": _fts_query(q),
                     "csa.date.since": since, "csa.date.until": until, "csa.project": project}) as sp:
        rows = fts_candidates(con, q, since=since, until=until)
        sp.set(**{"csa.row_count": len(rows)})
    with tracer.span("rerank", "RERANKER", **{"reranker.top_k": k, "csa.input_count": len(rows)}) as sp:
        hits = rerank(con, q, rows, k=k, project=project)
        sp.set(**{"csa.output_count": len(hits), "reranker.output_documents": [
            {"id": h["doc_id"], "score": h["score"], "rel": h["rel"], "rec": h["rec"],
             "ov": h["ov"], "corrob": h["corrob"]} for h in hits[:10]]})
    return hits


ANSWER_PROMPT = """Answer the question using ONLY the retrieved session knowledge below.
Cite each claim as [project/session8]. If the answer isn't in the context, say so plainly.
You see only the top-k retrieved items, NOT the full corpus — do NOT state totals/counts as definitive (say "at least N here").

QUESTION: {q}

RETRIEVED CONTEXT:
{ctx}
"""


def answer(con, q, k=8, model="main"):
    hits = search(con, q, k=k)
    for h in hits:                                   # the full indexed text: the snippet is truncated
        t = con.execute("SELECT text FROM docs WHERE doc_id=?", (h["doc_id"],)).fetchone()
        h["text"] = t[0] if t else h["snippet"]
    ctx = "\n".join(f"- [{h['project'] or '-'}/{src_label(h['session_id'])}] ({h['type']}) {h['text']}"
                    for h in hits)
    try:
        text, _, _ = llm.complete(ANSWER_PROMPT.format(q=q, ctx=ctx), model, timeout=120)
        return text.strip(), hits
    except Exception as e:
        return f"[llm failed: {str(e)[:200]}]", hits
