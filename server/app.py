"""`recall(topic)`: a natural-language reference to past work ("the ETL work I did earlier") -> a markdown
context blob for a Claude Code session. Retrieval and ranking live in `analyzer.search`; this layer applies
the relevance floors and shapes the hits. The MCP server, the hook and the CLI all sit on it.
"""
import logging, os, re

from sqlalchemy import and_, func, literal_column, or_, select

from analyzer import db, store, search, ingest, dedup, incremental, route, tracer
from analyzer.models import (ClaimCluster, Entity, Fact, Review, SessionRow, hidden_context,
                             worked_on)

log = logging.getLogger("csa.app")

_SESS = SessionRow.__table__

_HEAD = {"decision": "Decisions", "gotcha": "Gotchas", "constraint": "Constraints",
         "todo": "Open TODOs", "preference": "Preferences"}


def _person_digest(con, person=None, since=None, k=12, until=None):
    """The who-and-when path: `person`'s (or everyone's) most recent facts in [since, until), by last
    activity, as recall hit dicts. "What did X work on last week" has no term their facts contain."""
    h, s = hidden_context.alias("h"), _SESS.alias("s")
    r, cc = Review.__table__.alias("r"), ClaimCluster.__table__.alias("cc")
    f = Fact.__table__.alias("f")
    act = func.ifnull(s.c.ended_at, s.c.started_at)
    stmt = (select(literal_column("h.rowid"), h.c.type, h.c.claim, s.c.project, s.c.session_id,
                   s.c.user, func.substr(act, 1, 10), act, func.ifnull(cc.c.sessions, 1))
            .select_from(h
                .join(s, s.c.session_id == h.c.session_id)
                .outerjoin(r, and_(r.c.kind == "hc", r.c.ref == h.c.ref))
                .outerjoin(cc, cc.c.ref == h.c.ref)
                .outerjoin(f, f.c.ref == h.c.ref))
            .where(s.c.source == "session", search.servable(r, f)))
    if person:
        stmt = stmt.where(func.lower(s.c.user) == func.lower(person))
    if since:
        stmt = stmt.where(act >= since)
    if until:
        stmt = stmt.where(act < until)
    stmt = stmt.order_by(act.desc(), literal_column("h.rowid").desc()).limit(k)
    return [dict(doc_id=f"hc{rowid}", kind="hidden_context", session_id=sid, project=proj,
                 started_at=started, type=htype, corroboration=corrob, text=claim, snippet=claim,
                 owner=user, day=day)
            for rowid, htype, claim, proj, sid, user, day, started, corrob in db.run(con, stmt)]


def _sess_meta(con, sids):
    """{session_id: (day, project, branch, owner, summary)} for a batch — one query, no N+1."""
    return {r[0]: tuple(r[1:]) for r in db.run(con, select(
        _SESS.c.session_id, func.substr(func.ifnull(_SESS.c.ended_at, _SESS.c.started_at), 1, 10),
        _SESS.c.project, _SESS.c.git_branch, _SESS.c.user, _SESS.c.summary)
        .where(_SESS.c.session_id.in_(list(sids))))}


def _locate_md(con, topic, hits, qterms, k=5, window=""):
    """The locate answer: fact hits grouped into sessions, two compact lines each, ranked by how many of
    the query's words a session covers, then by summed score. Returns None when no hit names a session."""
    topical = {t for t in qterms if t not in route.LOCATE_WORDS}   # "find me the session" is not evidence
    by = {}
    for h in hits:
        sid = h.get("session_id")
        if not sid:                                      # entity docs are corpus-wide, they name no session
            continue
        txt = (h.get("match_text") or h.get("text") or h.get("snippet") or "")   # match_text includes the code home
        e = by.setdefault(sid, {"score": 0.0, "n": 0, "terms": set()})
        e["score"] += h.get("score") or 0.0
        e["n"] += 1
        e["terms"] |= {t for t in topical if t in txt.lower()}
    if not by:
        return None
    # A term no indexed doc contains cannot be ranking anything (search.distinctive_terms drops df == 0).
    absent = []
    for t in sorted(topical):
        try:                                             # raw: docs is FTS5
            if not con.execute("SELECT 1 FROM docs WHERE docs MATCH ? LIMIT 1", (f'"{t}"',)).fetchone():
                absent.append(t)
        except Exception:                                # unparseable FTS token -> claim nothing about it
            pass
    top = sorted(by.items(), key=lambda kv: (-len(kv[1]["terms"]), -kv[1]["score"]))[:k]
    meta = _sess_meta(con, [sid for sid, _ in top])
    base = os.environ.get("CSA_PUBLIC_URL", "").rstrip("/")
    out = [f'# Sessions matching "{topic}"',
           f"_top {len(top)} of {len(by)} sessions whose indexed facts match{window}; ranked by how much of the "
           f"topic each one covers. `missed:` = words of yours that hit nothing in that session — if the word "
           f'you most trust is missed everywhere, the corpus calls it something else. Call ask again with '
           f'in_session="<id>" for one session\'s facts._']
    # only when it is the exception: on a small corpus every word is absent
    if absent and len(absent) <= max(1, len(topical) // 3):
        absent = absent[:4]
        out.append(f"_**{', '.join(absent)}** — no indexed fact anywhere in the corpus contains "
                   f"{'these' if len(absent) > 1 else 'this'}, so the ranking below is not using "
                   f"{'them' if len(absent) > 1 else 'it'}. The corpus may call it something else._")
    out.append("")
    for sid, e in top:
        day, proj, branch, owner, summary = meta.get(sid, ("", "", "", "", ""))
        where = " @ ".join(x for x in (proj or "-", branch or "") if x)
        idpart = f"[`{sid}`]({base}/#/corpus/session/{sid})" if base else f"`{sid}`"
        matched = ", ".join(sorted(e["terms"])[:6]) or "—"
        missed = ", ".join(sorted(topical - e["terms"])[:6])
        out.append(f"- {idpart} · {day} · {where} · {owner} · {e['n']} matching fact(s) · matched: {matched}"
                   + (f" · **missed: {missed}**" if missed else ""))
        if summary:
            out.append(f"  {summary[:200]}{'…' if len(summary) > 200 else ''}")
    return "\n".join(out).strip()


def session_facts(sid, k=15, org_id=None):
    """One session's summary and at most `k` facts, by id or 8-char prefix: the follow-up to a locate
    answer."""
    con = store.sqlite_db(store.org_db(org_id))
    sid = re.sub(r"[^0-9a-zA-Z_-]", "", str(sid or ""))[:80]
    if not sid:
        return "_in_session needs a session id (or its first 8 characters)._"
    rows = db.run(con, select(_SESS.c.session_id)
                  .where(or_(_SESS.c.session_id == sid, _SESS.c.session_id.like(sid + "%")))
                  .limit(6)).fetchall()                  # the UI + fact provenance show 8 chars — accept those
    if not rows:
        return f'_No indexed session `{sid}`. It may not be ingested yet, or the id is from another corpus._'
    if len(rows) > 1 and not any(r[0] == sid for r in rows):
        return f'_`{sid}` matches {len(rows)} sessions ({", ".join(r[0][:12] for r in rows[:6])}). Use the full id._'
    sid = sid if any(r[0] == sid for r in rows) else rows[0][0]
    day, proj, branch, owner, summary = _sess_meta(con, [sid])[sid]
    out = [f"# Session `{sid}` · {day} · {proj or '-'}{f' @ {branch}' if branch else ''} · {owner}"]
    if summary:
        out += ["", summary]
    wo = [w[0] for w in db.run(con, select(worked_on.c.item)
                               .where(worked_on.c.session_id == sid).limit(12))]
    if wo:
        out += ["", "**Worked on:** " + " · ".join(wo)]
    h, r, f = hidden_context.alias("h"), Review.__table__.alias("r"), Fact.__table__.alias("f")
    facts = db.run(con, select(h.c.type, h.c.claim, h.c.ref)
                   .select_from(h.outerjoin(r, and_(r.c.kind == "hc", r.c.ref == h.c.ref))
                                .outerjoin(f, f.c.ref == h.c.ref))
                   .where(h.c.session_id == sid, search.servable(r, f))
                   .order_by(literal_column("h.rowid")).limit(k + 1)).fetchall()
    groups = {}
    for ty, claim, ref in facts[:k]:
        text = (claim or "")[:300] + ("…" if len(claim or "") > 300 else "")
        groups.setdefault(ty, []).append(f"- {text}  _(id:{ref})_")
    for ty, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out += ["", f"## {_HEAD.get(ty, ty.title())}"] + items
    if len(facts) > k:
        out += ["", "_…more facts in this session — narrow with a topical ask, or open it in the UI._"]
    if not facts:
        out += ["", "_This session is indexed but has no extracted facts (nothing durable was decided in it)._"]
    return "\n".join(out).strip()


def recall(topic, k=12, fill=True, fill_k=3, session=None, person=None, asked_by=None, strict=False, project=None,
           org_id=None, metrics=None, ambient=False):
    """Natural-language topic -> markdown context blob.

    `fill`: on a cold topic in the default org, extract and index a few matching local sessions first.
    `person` ("Ask X"): a name or email fragment; their hits render first, then the team's when their
    coverage is thin. `strict`: the floor for an unbidden prompt; `ambient` adds the gold-only bar.
    `org_id`: whose corpus to open (None = default). `metrics`: an optional caller-owned dict, filled
    with `score` (the top hit's, abstains included), on a strict hit `top`, and on a hit `n` (facts
    rendered) and `digest` (a person's recent work, matched on no word)."""
    with tracer.span("recall", "CHAIN", db=store.org_db(org_id),
                     **{"csa.k": k, "csa.person": person, "input.value": (str(topic) if topic else "")[:1000]}):
        return _recall(topic, k, fill, fill_k, session, person, asked_by, strict, project, org_id, metrics,
                       ambient)


def _recall(topic, k, fill, fill_k, session, person, asked_by, strict, project, org_id, metrics, ambient=False):
    k = max(1, min(int(k), 50))
    con = store.sqlite_db(store.org_db(org_id))
    person = str(person).strip()[:80] if person and str(person).strip() else None
    if person:
        resolved = route.resolve_person(con, person)
        if not resolved:                                 # zero or ambiguous match: answer with the roster
            owners = route.roster(con)
            tracer.set_attr(**{"csa.miss_reason": "person_unknown"})
            return (f'No one matching "{person}" owns sessions here. Known people: {", ".join(owners)}. '
                    "Retry with one of these (a name or email fragment works), or omit `person` to search everyone.")
        person = resolved
    self_scope = person is not None and person == (asked_by or "")
    # An explicit date ("on July 17", "last 3 days") is a hard filter on every scope: [d_since, d_until).
    # A person plus a temporal cue is a recency digest of their work, since their facts never say "last
    # week"; never on the strict path, where a digest would fire with no relevance test at all.
    d_since, d_until = route.date_window(topic)
    dated = bool(d_since or d_until)
    activity, since = route.activity_window(topic) if person else (False, None)
    temporal = bool(person and since and not strict)
    digest_mode = temporal                               # may flip below for a thin non-temporal activity query
    locate = route.is_locate(topic) and not strict       # a session list is a pointer, useless pushed unasked
    tracer.set_attr(**{"csa.locate": locate})
    fetch_k = min(k * 3, 50) if (person or locate) else k  # the person partition and locate grouping need depth
    q = route.undated(route.topical(topic) if locate else topic)   # FTS sees neither the locator's words nor a date
    # A date is no topic: the words left must pass the honest-miss rule, and with none left the ask is a
    # pure time window ("what did we do today"), answered with everyone's facts in it; unbidden, only
    # when the prompt asks ("thanks, that's all for today" does not).
    unseen, known = route.topic_words(con, q) if dated else ([], 0)
    if route.outweighed(unseen, known):
        tracer.set_attr(**{"csa.miss_reason": "unseen_topic"})
        return f'Nothing on "{topic}" in past sessions. Words the corpus has never seen: {", ".join(unseen)}.'
    window = dated and not person and not (unseen or known) and (not ambient or route.asks(topic))
    if temporal:
        hits = _person_digest(con, person, d_since or since, k, until=d_until)
    elif window:
        hits = _person_digest(con, None, d_since, k, until=d_until)
        digest_mode = bool(hits)
    else:
        hits = search.search(con, q, k=fetch_k, project=project, since=d_since, until=d_until)
    filled = 0
    # cold fill reads this machine's disk, which is no hosted org's data
    if org_id is not None and store.valid_org_id(org_id) != store.DEFAULT_ORG:
        fill = False
    if not hits and fill and not (temporal or window):
        import csa                                       # late import: app <-> csa cycle
        cache = incremental.extract_dir()
        done = {f[:-5] for f in os.listdir(cache)} if os.path.isdir(cache) else set()
        cand = ingest.grep_sessions(search.query_terms(topic), exclude_ids=done, k=fill_k)
        if cand:
            log.info('no indexed context for "%s" — extracting %d session(s) with sonnet…', topic, len(cand))
            csa._extract_sessions(cand, workers=len(cand), model="sonnet")
            csa.do_index()
            con = store.sqlite_db(store.org_db(org_id))
            hits = search.search(con, q, k=fetch_k, project=project, since=d_since, until=d_until)
            filled = len(cand)
    if not hits and person and activity and not temporal and not strict:   # non-temporal activity -> recent work
        hits = _person_digest(con, person, None, k)
        digest_mode = bool(hits)
    # a date filter that emptied recall while the topic has hits without it was a mis-parsed date
    if not hits and dated and not person and not window:
        unfiltered = search.search(con, topic, k=fetch_k, project=project)
        if unfiltered:
            hits, dated = unfiltered, False
    if not hits:
        if temporal:
            win = f" since {since}" if since else ""
            tracer.set_attr(**{"csa.miss_reason": "person_no_activity"})
            return (f'_{person} has no recorded session activity{win} yet — their work becomes recallable '
                    "here once a session of theirs is ingested._")
        if person:
            tracer.set_attr(**{"csa.miss_reason": "no_candidates"})
            return (f'Nothing on "{topic}" yet — neither {person}\'s sessions nor the team\'s have touched it. '
                    "Once anyone works on it, it'll be recallable here.")
        if dated:
            win = (f"between {d_since} and {d_until}" if d_since and d_until
                   else f"on or after {d_since}" if d_since else f"before {d_until}")
            tracer.set_attr(**{"csa.miss_reason": "empty_date_window"})
            return (f'No session activity {win} matches "{topic}". Either nothing was worked on then, or '
                    "those sessions aren't indexed yet.")
        tracer.set_attr(**{"csa.miss_reason": "no_candidates"})
        return f'No past-session context found for "{topic}".'
    for h in hits:                                       # the snippet is truncated: read the full indexed text
        row = con.execute("SELECT text FROM docs WHERE doc_id=?", (h["doc_id"],)).fetchone()   # raw: docs is FTS5
        h["text"] = row[0] if row else h["snippet"]
        h["owner"], h["day"] = db.run(con, select(       # day = last activity
            _SESS.c.user, func.substr(func.ifnull(_SESS.c.ended_at, _SESS.c.started_at), 1, 10))
            .where(_SESS.c.session_id == h["session_id"])).fetchone() or ("", "")
    # `match_text` is what a hit matched on (its prose plus its code home); `text` is what renders.
    _hcr = [int(h["doc_id"][2:]) for h in hits if h["doc_id"][:2] == "hc" and h["doc_id"][2:].isdigit()]
    _hc = hidden_context.alias("h")
    ch = {f"hc{rid}": f"{repo or ''} {path or ''}" for rid, repo, path in db.run(con, select(
        literal_column("h.rowid"), _hc.c.repo, _hc.c.path)
        .where(literal_column("h.rowid").in_(_hcr)))} if _hcr else {}
    for h in hits:
        h["match_text"] = (h["text"] + " " + ch.get(h["doc_id"], "")).strip()

    if hits:                                             # before every floor: an abstain reports its score too
        if metrics is not None:
            metrics["score"] = hits[0].get("score")
        tracer.set_attr(**{"csa.top_score": hits[0].get("score")})

    # Locate answers before the floors: a thin match is what it wants to show, with its coverage. It serves
    # pointers, not facts, so nothing is logged as served.
    if locate:
        block = [h for h in hits if person.lower() in (h["owner"] or "").lower()] if person else hits
        win = (f" between {d_since} and {d_until}" if d_since and d_until
               else f" on or after {d_since}" if d_since else f" before {d_until}" if d_until else "")
        md = _locate_md(con, topic, block or hits, search.query_terms(topic), window=win)
        if md:
            return md

    # A non-temporal activity phrase ("what is X up to") with thin person coverage gets the recency digest;
    # never when strict, where a digest skips both floors.
    if person and activity and not digest_mode and not strict:
        p = person.lower()
        if sum(1 for h in hits if p in (h["owner"] or "").lower()) < 3:
            dig = _person_digest(con, person, None, k)
            if dig:
                hits, digest_mode = dig, True

    # Relevance floors by term coverage, never by score magnitude (a broad legit term scores lower than
    # noise, and rerank mixes in recency). The strict floor is for unbidden prompts and pays for
    # precision; the lenient one refuses only matches that graze generic tokens.
    qterms = search.query_terms(topic)
    qdist = set(search.distinctive_terms(con, topic, max_df_frac=0.02)) if person else set()
    if strict and not digest_mode:
        # Unbidden, every prompt (tuned on logged hook queries): the top-ranked
        # claim must share `need` content words including one of the 5 rarest; a single content word
        # never fires. Words compare as the index stems them, the way retrieval found the claim; filler
        # ("running" is "run") is no topic, and a teammate's name (the scoped person's, else any the
        # query names) matches the facts they own.
        # claims only, filtered before the rank-1 test: a pasted PR URL would otherwise veto a good claim
        cand = [h for h in hits if h["kind"] == "hidden_context"]
        if ambient:
            # Gold only, and a human 'unimportant' vetoes machine gold. Here, not in rerank, so an
            # ungilded set abstains as `ambient_floor` rather than `no_candidates`.
            gold = search.gold_docs(con, [h["doc_id"] for h in cand])
            cand = [h for h in cand if h["status"] != "unimportant"
                    and (h["status"] in search.GOLD_STATUS or h["doc_id"] in gold)]
        words = {w: alts for w, alts in search.query_words(topic).items() if len(w) > 2}
        owners = [person] if person else route.roster(con)
        named = {w: o for w in words if (o := route.named_owner(owners, w, route.aliases(con)))}
        alts = sorted(set().union(*words.values()))
        toks = dict(zip(alts, store.stems(alts)))
        filler = set().union(*store.stems(sorted(route.FILLER)))
        qt = {w: [toks[a] for a in a_set if toks[a]] for w, a_set in words.items()
              if toks[w] and not toks[w] <= filler}
        text_toks = dict(zip([h["doc_id"] for h in cand], store.stems([h["text"] for h in cand])))
        topical = [w for w in qt if w not in named]     # a name is never the rare word: its df is ~0
        rare = search.rare_terms(con, topical, k=5)
        need = min(3, len(qt))
        ok = lambda h: (lambda m: len(m) >= need and any(w in m for w in rare))(
            {w for w, a in qt.items() if any(x <= text_toks[h["doc_id"]] for x in a)
             or (w in named and h["owner"] == named[w])})
        qual = [h for h in cand if ok(h)] if len(topical) >= 2 else []
        if not qual or not cand or not ok(cand[0]):
            tracer.set_attr(**{"csa.miss_reason": "ambient_floor"})
            return f'No strong past-session context for "{topic}".'
        hits = qual
        if metrics is not None:
            # The banner's fact, with its day (a superseded decision is the harm to see). Provisional: the
            # person partition may not render it, so it is reconciled against `served` after render().
            h0 = hits[0]
            _h, _f = hidden_context.alias("h"), Fact.__table__.alias("f")
            row = db.run(con, select(_h.c.ref, _f.c.state, _f.c.valid_at, _f.c.invalid_at)
                         .select_from(_h.outerjoin(_f, _f.c.ref == _h.c.ref))
                         .where(literal_column("h.rowid") == int(h0["doc_id"][2:]))
                         ).fetchone() if h0["doc_id"][:2] == "hc" else None
            metrics["top"] = dict(type=h0.get("type") or "", claim=h0.get("text") or "",
                                  day=h0.get("day") or "", ref=row[0] if row else "",
                                  state=(row[1] if row else None) or "",
                                  valid_at=(row[2] if row else None) or "",
                                  invalid_at=(row[3] if row else None) or "")
    elif len(qterms) >= 3 and not digest_mode:
        # When the top 3 together cover under half the query, keep only the hits that clear that bar
        # alone (on match_text) or carry a file the query names, and abstain if none do. A rescue, not
        # a filter: per-hit coverage on every query would narrow good answers.
        cov = lambda t: sum(w in t for w in qterms) / len(qterms)
        if cov(" ".join(h["match_text"] for h in hits[:3]).lower()) < 0.5:
            files = {t for t in qterms if search.FILEISH.match(t)}
            qual = [h for h in hits if cov((h["match_text"] or "").lower()) >= 0.5
                    or any(f in ch.get(h["doc_id"], "").lower() for f in files)]
            if not qual:
                who = f" in {person}'s or the team's" if person else " in past"
                tracer.set_attr(**{"csa.miss_reason": "coverage_floor"})
                return (f'Nothing solid about "{topic}"{who} sessions — the closest matches only graze a couple '
                        "of your words. Try the specific name (a service, file, ticket, or decision).")
            hits = qual

    n_sess, n_mem = (db.run(con, select(func.count()).select_from(_SESS)
                            .where(_SESS.c.source == "session")).fetchone()[0],
                     db.run(con, select(func.count()).select_from(_SESS)
                            .where(_SESS.c.source == "memory")).fetchone()[0])
    out = [f'# Recalled context for "{topic}"',
           f"_from {n_sess} indexed sessions{f' + {n_mem} curated memories' if n_mem else ''} "
           f"(yours and the team's), most relevant first; ⟳N = corroborated across N sessions._"]
    if filled:
        out.append(f"_(auto-extracted {filled} session(s) for this topic just now — Sonnet, ~cents.)_")
    if person:
        if digest_mode:
            whose = "your" if self_scope else f"{person}'s"
            out.append(f"_{whose} recent work{f' since {since}' if since else ''}, most recent first:_")
        else:
            out.append(f"_scoped to your own sessions first ({person})._" if self_scope else
                       f"_scoped to {person} — their sessions first._")
    out.append("")
    served = []                                          # (ref, kind, project) of everything rendered, both blocks
    seen = set()                                         # one row per fact cluster (⟳N carries the rest)

    def render(block):                                   # one grouped-markdown block; shares served/seen
        groups, rel, lines = {}, {}, []                  # rel: text -> [line, count, src]
        for h in block:
            # soft attribution: whose session and when, never "X decided"
            u, d = h["owner"], h["day"]
            src = " · ".join(x for x in (u if u and u != "owner" else "", d or "",
                                         f"{h['project'] or '-'}/{search.src_label(h['session_id'])}") if x)
            tag = f" ⟳{h['corroboration']}" if h.get("corroboration", 1) > 1 else ""
            text = h["text"]
            if h["kind"] == "hidden_context":
                _h, _cc = hidden_context.alias("h"), ClaimCluster.__table__.alias("cc")
                row = db.run(con, select(_h.c.ref, func.ifnull(_cc.c.cluster_id, _h.c.ref),
                                         _h.c.repo, _h.c.path)
                             .select_from(_h.outerjoin(_cc, _cc.c.ref == _h.c.ref))
                             .where(literal_column("h.rowid") == int(h["doc_id"][2:]))).fetchone()
                ref, ckey, hrepo, hpath = row if row else ("?", "?", None, None)  # ckey collapses paraphrases
                if ckey in seen:
                    continue
                seen.add(ckey)
                served.append((ref, "hidden_context", h["project"]))
                if text.startswith(h["type"] + ":"):      # drop the redundant "gotcha:" prefix under its heading
                    text = text[len(h["type"]) + 1:].strip()
                if len(text) > 400:
                    text = text[:400].rstrip() + f'… _[truncated — call fact("{ref}") for the full text]_'
                base = os.environ.get("CSA_PUBLIC_URL", "").rstrip("/")   # set: ids click through
                idpart = f"[id:{ref}]({base}/#/corpus/session/{h['session_id']})" if base else f"id:{ref}"
                # where the code lives, which may not be where the session ran; a path without a repo
                # would read as rooted in the session's project, so it is not shown
                home = f" · code: {hrepo}" + (f"/{hpath}" if hpath else "") if hrepo else ""
                groups.setdefault(h["type"], []).append(f"- {text}  _({src}{tag}{home} · {idpart})_")
            else:
                if h["kind"] == "entity":                 # entity docs carry "id name attrs-json": the name only
                    row = db.run(con, select(Entity.__table__.c.name)
                                 .where(Entity.__table__.c.id == h["doc_id"][3:])).fetchone()
                    text, src = (row[0] if row and row[0] else h["doc_id"][3:]), "entity"
                if text in rel:                           # the same line from sibling sessions: one row, counted
                    rel[text][1] += 1
                    continue
                served.append((h["doc_id"], h["kind"], h["project"]))
                rel[text] = [f"_{h['type']}_ {text}", 1, src]

        if len(rel) > 1:                                 # merge near-identical related rows into the earlier one
            toks = {t: dedup.tokens(re.sub(r"\S*/", "", t.lower())) for t in rel}   # paths -> basenames
            keys = list(rel)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    if a in rel and b in rel and toks[a] and toks[b]:
                        inter = toks[a] & toks[b]
                        if len(inter) >= 2 and len(inter) / min(len(toks[a]), len(toks[b])) >= 0.66:
                            rel[a][1] += rel[b][1]
                            del rel[b]

        for ty, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            lines += [f"## {_HEAD.get(ty, ty.title())}"] + items + [""]
        if rel:
            lines += ["## Related sessions / entities"] + [
                f"- {line}{f' (×{n} sessions)' if n > 1 else ''}  _({src})_" for line, n, src in rel.values()]
        return lines

    if person:
        p = person.lower()
        mine = [h for h in hits if p in (h["owner"] or "").lower()]
        rest = [h for h in hits if p not in (h["owner"] or "").lower()]
        out += render(mine[:k])
        # Thin coverage counts relevant own hits: at least 2 query terms including a distinctive one (any
        # hit counts when the query has no distinctive term).
        qd = qdist & set(qterms)
        rel = (lambda h: (lambda s: len(s) >= min(2, len(qterms)) and any(w in qd for w in s))(
            [w for w in qterms if w in (h["text"] or "").lower()])) if qd else (lambda h: True)
        mine_rel, team_rel = [h for h in mine if rel(h)], [h for h in rest if rel(h)]
        # relevant team hits first; min-3 floor so a full own-block can't starve a triggered fallthrough
        team = (team_rel + [h for h in rest if not rel(h)])[:max(3, k - len(mine[:k]))]
        if len(mine_rel) < 3 and team_rel:               # thin person coverage: fall through to the team
            # absence is about their sessions, never their knowledge
            whose = "your own" if self_scope else f"{person}'s"
            if not mine:
                note = (f'_None of your own sessions touched "{topic}" — below is teammates\' work:_'
                        if self_scope else
                        f'_{person} hasn\'t hit "{topic}" in their sessions yet — here\'s what the team knows:_')
            elif not mine_rel:
                note = f'_{whose} sessions only graze "{topic}" — the team\'s work below is the substantive answer:_'
            else:
                note = (f"_That's everything from {whose} sessions on this topic ({len(mine)} so far) "
                        f"— here's more from the team:_")
            out += ["---", note, ""]
            out += render(team)
    else:
        out += render(hits)
    if metrics is not None:
        metrics["n"] = len(served)                       # what was rendered
        metrics["digest"] = digest_mode
        # the banner may only headline a fact that was actually rendered
        refs = [ref for ref, kind, _ in served if kind == "hidden_context"]
        if metrics.get("top") and refs and metrics["top"]["ref"] not in refs:
            _h, _s = hidden_context.alias("h"), _SESS.alias("s")
            row = db.run(con, select(_h.c.type, _h.c.claim, func.substr(_s.c.started_at, 1, 10))
                         .select_from(_h.join(_s, _s.c.session_id == _h.c.session_id))
                         .where(_h.c.ref == refs[0]).limit(1)).fetchone()
            if row:
                metrics["top"] = dict(type=row[0] or "", claim=row[1] or "", day=row[2] or "", ref=refs[0])
    tracer.set_attr(**{"csa.n_served": len(served), "csa.digest": bool(digest_mode),
                       "csa.served.refs": ",".join(r for r, kind, _ in served
                                                   if kind == "hidden_context")[:800] or None})
    if session:                                          # provenance for the feedback UI
        store.log_served(con, session, topic, served, person=person, asked_by=asked_by)
    return "\n".join(out).strip()
