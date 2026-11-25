"""Entity short-circuit for `ask` (rung 1, stdlib, NO model, NO deps): check query tokens against the
entities table BEFORE FTS / SQL-planning, so identifier-bearing questions ("which session created PR #322?")
hit the structured graph directly instead of burning planner calls. Ported from an upstream project's fuzzy_index /
fuzzy_resolve design, scaled down: at ~1.2k entities a dict + linear scan resolves in ~1ms.

Tiered matching with ABSTAIN semantics — nothing confident -> return [] and `ask` proceeds exactly as today:
  0 exact  — a token the user TYPED (a raw query token or its path tail) == a WHOLE entity id/name/tail
             carried by <=FAMILY entities; OR any token == a surface carried by exactly ONE entity.
             Unique == discriminative identifier: '322' (only PR-322) matches; 'deploy' (a fragment of
             5 different file/skill ids) and 'github' (a fragment of 74) abstain.
  1 prefix — typed token >=4 chars, non-numeric, strictly prefixes a surface, and the union is ONE entity.
  2 typo   — typed token >=5 chars, non-numeric: Levenshtein <=2 AND ratio >=0.80 (store.py's corpus-tuned
             gates, reused), union ONE entity.
Pieces of a compound raw token (the 'github'/'com' inside a pasted URL) are WEAK evidence — exact-unique
only, never fuzzed and never granted the multi-entity whole tier.
GUARDS (all load-bearing, from upstream's design): numeric tokens match EXACT only (322 must never fuzzy-reach
321 — adjacent-id hazard); numbered siblings (pr-322/pr-323) never typo-match; a surface carried by >FAMILY
entities never short-circuits alone; stopwords / question scaffolding / <3-char terms never match.
"""
import re, difflib, sqlite3

from sqlalchemy import case, func, or_, select

from analyzer import db, store
from analyzer.dedup import _STOP
from analyzer.models import (Entity, PrLink, SessionRow, SessionWorkspace, actions,
                             hidden_context, mentions, relations, worked_on)

_ENT, _SW = Entity.__table__, SessionWorkspace.__table__
_S, _PL = SessionRow.__table__, PrLink.__table__

FAMILY, CAP = 5, 3                                       # >FAMILY carriers = ambiguous; cap cards per ask
_NOISE = _STOP | set("which what who whom whose how when where why does about http https www "
                     "session sessions".split())        # session(s): scaffolding in ~every question to THIS tool
_TOK = re.compile(r"[a-z0-9][a-z0-9._/#+-]*")            # keeps urls/paths/ids whole; '#322' tokenizes as '322'
_WORD = re.compile(r"[a-z0-9]+")


def _surfaces(value):
    """(whole, fragments) surfaces of one string, lowercased: whole = the full value + its path tail (edge
    punctuation stripped, so '#322' -> '322'); fragments = each delimiter token ('PR-322' -> {'pr','322'},
    'ledger-etl3-prefect' -> {'ledger','etl3','prefect'}). Both filtered: len>=3 and not a stop/noise word."""
    v = (value or "").lower().strip()
    whole = {p.strip("#.,:;!?'\"()[]{}<>_/+-") for p in (v, v.rsplit("/", 1)[-1])}
    keep = lambda ss: {s for s in ss if len(s) >= 3 and s not in _NOISE}
    whole = keep(whole)
    return whole, keep(set(_WORD.findall(v))) - whole


def _index(con):
    """entities(id,name) -> (every, whole): surface -> set(entity_id). `whole` only holds full-value/tail
    surfaces (strong aliases); `every` additionally holds delimiter fragments (weak, need uniqueness)."""
    every, whole = {}, {}
    for eid, name in db.run(con, select(_ENT.c.id, _ENT.c.name)):
        w1, f1 = _surfaces(eid)
        w2, f2 = _surfaces(name)
        for s in w1 | w2:
            whole.setdefault(s, set()).add(eid)
            every.setdefault(s, set()).add(eid)
        for s in (f1 | f2) - (w1 | w2):
            every.setdefault(s, set()).add(eid)
    return every, whole


def resolve(con, query):
    """query string -> up to CAP confident entity ids, best tier first ([] = ABSTAIN).
    # ponytail: index rebuilt per call (~1ms at 1.2k entities) and typo tier is a linear scan over all
    # surfaces — cache the index / block by length bucket if the corpus grows 100x."""
    every, whole = _index(con)
    seen = {}                                            # surface -> typed? (query order; typed wins over weak)
    for raw in _TOK.findall(query.lower()):
        w, f = _surfaces(raw)
        for t in sorted(w | f, key=lambda s: (-len(s), s)):
            seen[t] = seen.get(t, False) or t in w       # t in w == the user typed it (raw token / its tail)
    hits = {}                                            # entity_id -> best tier (insertion order = query order)
    for t, typed in seen.items():
        ids = every.get(t)
        if ids is not None:                              # exact
            w = whole.get(t, set())
            if typed and w and len(w) <= FAMILY:         # whole id/name/tail — the strong alias tier
                for e in w:
                    hits.setdefault(e, 0)
            elif len(ids) == 1:                          # unique surface == identifier ('322' -> PR-322)
                hits.setdefault(next(iter(ids)), 0)
            continue                                     # ambiguous exact -> this token abstains outright
        if not typed or t.isdigit() or len(t) < 4:       # fuzz only typed tokens; numeric = EXACT only
            continue
        pre = {e for s, es in every.items() if s.startswith(t) for e in es}
        if len(pre) == 1:                                # prefix confident only if it narrows to ONE entity
            hits.setdefault(pre.pop(), 1)
            continue
        if len(t) >= 5:                                  # typo tier: store.py's tuned gates, same guards
            typo = {e for s, es in every.items()
                    if not s.isdigit() and not store._numbered_siblings(t, s)
                    and store._lev(t, s, store.MAX_EDITS) <= store.MAX_EDITS
                    and difflib.SequenceMatcher(None, t, s).ratio() >= store.MIN_RATIO
                    for e in es}
            if len(typo) == 1:
                hits.setdefault(typo.pop(), 2)
    return sorted(hits, key=hits.__getitem__)[:CAP]      # stable: tier, then first-seen in the query


_PR_ID = re.compile(r"^pr-(\d+)$", re.I)                 # the extractor/harvest entity id convention
_PR_REF = re.compile(r"(?:pull/|\bpr[\s#-]*|#)(\d{1,7})\b", re.I)   # explicit PR refs typed in a query
_URL_REF = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", re.I)   # full URL = repo IS specified
_SID_REF = re.compile(r"\b(agent-[0-9a-f]{6,}|[0-9a-f]{8}[0-9a-f-]*)\b")   # session-id-shaped tokens
_TICKET_REF = re.compile(r"\b(bug|ticket|task|issue)[\s#:_-]*(\d{2,7})\b", re.I)   # "Bug 1234", "ticket #77"
_KEY_REF = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")  # JIRA-style key — deliberately case-SENSITIVE
_TICKET_WORDS = {"bug", "ticket", "task", "issue"}


def _pr_evidence(con, n, want_merge=False, query_repo=None):
    """pr_links provenance for PR #n — machine-labeled ground truth (creator session + creation moment).
    PR numbers collide across repos: one answer per repo, an explicit NOTE when several match, and a mismatch
    warning when the query's URL named a repo we have NO evidence for (never silently substitute repos).
    `want_merge` adds merge status (harvested `gh pr merge` actions) — including an honest 'no merge recorded'."""
    p, s = _PL.alias("p"), _S.alias("s")
    rows = db.run(con, select(p.c.repo, p.c.session_id, p.c.first_seen, p.c.last_seen,
                              p.c.n_events, s.c.user, s.c.project)
                  .select_from(p.outerjoin(s, s.c.session_id == p.c.session_id))
                  .where(p.c.pr_number == n)
                  .order_by(p.c.repo, p.c.first_seen)).fetchall()   # repo, then first_seen: first repo wins
    if not rows:
        return []
    lines, seen_repo = [], set()
    repos = {r[0] for r in rows}
    if query_repo and query_repo.lower() not in {r.lower() for r in repos}:
        lines.append(f"⚠ You asked about {query_repo}#{n} — NO pr-link evidence for that repo here. "
                     f"A DIFFERENT repo has a PR #{n}:")
    elif len(repos) > 1:
        lines.append(f"NOTE: PR #{n} exists in {len(repos)} repos here — one answer per repo:")
    for repo, sid, first, last, nev, user, proj in rows:
        if repo in seen_repo:                            # resumed/continued sessions re-emit pr-link streams
            lines.append(f"- {repo}#{n}: session {sid} also streamed pr-link events ({nev} events, {first} → {last})")
        else:
            seen_repo.add(repo)
            lines.append(f"- **{repo}#{n} created by session {sid}** ({user or '?'} · {proj or '-'}) at "
                         f"**{first}** — first of {nev} pr-link events; only the session that opened a PR "
                         f"emits these (viewers do not).")
    if want_merge:
        a, s2 = actions.alias("a"), _S.alias("s")
        m = db.run(con, select(a.c.session_id, s2.c.user)
                   .select_from(a.outerjoin(s2, s2.c.session_id == a.c.session_id))
                   .where(a.c.verb == "merge", a.c.target_id == f"PR-{n}").limit(3)).fetchall()
        lines.append("- merged via `gh pr merge` in session " + ", ".join(f"{sid} ({u or '?'})" for sid, u in m)
                     if m else "- no merge recorded in these sessions — merges done on GitHub aren't visible here; "
                               "GitHub is the source of truth for merged-at.")
    return lines


def _workspace_card(con, tok):
    """Deterministic branch/worktree lookup: a path-or-branch-shaped query token ('ledgerv2/etl3-prefect',
    'ledger-etl3-prefect') matched against session_workspace values. Branches aren't entities, so resolve()
    alone can't reach them; popular exact paths return a capped list plus total instead of disappearing."""
    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.run(con, select(_SW.c.kind, _SW.c.value, _SW.c.session_id)
                  .where(_SW.c.value.collate("NOCASE").like(f"%{esc}%", escape="\\"))
                  .limit(40)).fetchall()
    if not rows:
        return None
    grouped = {}
    for k, v, sid in rows:
        grouped.setdefault((k, v), set()).add(sid)
    lines = [f"## Workspace match: {tok}"]
    for (k, v), sids in sorted(grouped.items()):
        total = len(sids)
        suffix = f" (showing 8 of {total})" if total > 8 else ""
        lines.append(f"- {k} {v} — sessions{suffix}: {', '.join(sorted(sids)[:8])}")
    return "\n".join(lines)


def _session_card(con, sid_prefix):
    """Deterministic session card for a session-id-shaped token, rendered wholly from stored data (NO planner):
    the session summary + who/when, what it worked on (worked_on items + top hidden_context facts), its actions
    (create/fix/investigate/merge with targets), plus pr_links creation evidence and session_workspace. Fires on
    an unambiguous prefix match; returns None ONLY when the id doesn't exist (a real session always gets a card)."""
    ids = [r[0] for r in db.run(con, select(_S.c.session_id)
                                .where(_S.c.session_id.like(sid_prefix + "%")).limit(3))]
    if len(ids) != 1:                                    # 0 = not a session; >1 = ambiguous prefix -> abstain
        return None
    sid = ids[0]
    user, proj, day, summary = db.run(con, select(
        _S.c.user, _S.c.project, func.substr(_S.c.started_at, 1, 10), _S.c.summary)
        .where(_S.c.session_id == sid)).fetchone()
    lines = [f"## Session: {sid} ({user or '?'} · {proj or '-'} · {day or '?'})"]
    if summary:
        lines.append(f"summary: {summary}")
    prs = db.run(con, select(_PL.c.repo, _PL.c.pr_number, _PL.c.first_seen, _PL.c.n_events)
                 .where(_PL.c.session_id == sid).order_by(_PL.c.first_seen)).fetchall()
    if prs:
        lines.append("PRs this session created (pr-link evidence):")
        lines += [f"- {r}#{n} at {f} ({e} events)" for r, n, f, e in prs]
    acts = db.run(con, select(actions.c.verb, actions.c.target_id, actions.c.context)
                  .where(actions.c.session_id == sid).limit(12)).fetchall()
    if acts:
        lines.append("actions:")
        lines += [f"- {v} {t or '?'}" + (f" — {c[:80]}" if c else "") for v, t, c in acts]
    worked = [w for (w,) in db.run(con, select(worked_on.c.item)
                                   .where(worked_on.c.session_id == sid).limit(10))]
    if worked:
        lines.append("worked on:")
        lines += [f"- {w}" for w in worked]
    facts = db.run(con, select(                          # top facts: high-confidence first, capped small
        hidden_context.c.type, hidden_context.c.claim, hidden_context.c.confidence)
        .where(hidden_context.c.session_id == sid)
        .order_by(case({"high": 0, "medium": 1}, value=hidden_context.c.confidence, else_=2))
        .limit(8)).fetchall()
    if facts:
        lines.append("facts learned:")
        lines += [f"- [{t or '?'}] {c}" + (f" ({conf})" if conf else "") for t, c, conf in facts]
    ws = db.run(con, select(_SW.c.kind, _SW.c.value).where(_SW.c.session_id == sid)
                .order_by(_SW.c.kind).limit(12)).fetchall()
    if ws:
        lines.append("worked in:")
        lines += [f"- {k}: {v}" for k, v in ws]
    return "\n".join(lines)                              # session exists -> always a card (id-only checked above)


def cards(con, eids, query=""):
    """Compact markdown ENTITY CARD per resolved id: the entities row + its actions / relations / mentions,
    joined to sessions for who/when. `ask` prepends this to whatever it was going to return. Lists capped
    at 10 rows each (mentions per spec; actions/relations for payload safety). PR entities additionally carry
    pr_links evidence; explicit PR refs in `query` that match nothing get an honest miss line, never silence."""
    out, covered = [], set()
    ql = (query or "").lower()
    want_merge = "merge" in ql
    url_repo = {int(n): r for r, n in _URL_REF.findall(query or "")}   # full URL -> the repo was SPECIFIED
    refs = {int(x) for x in _PR_REF.findall(query or "")}
    issue_only = "issue" in ql and not re.search(r"\bpr\b|pull", ql)
    direct_workspace = re.search(r"\b(which|what)\s+sessions?\b|worked (?:in|on)|used branch|worktree|\bpath\b|/documents/git/", ql)
    if issue_only and refs:
        return "\n".join(f"CSA tracks PR creation via pr-link events; GitHub issues aren't indexed here, "
                         f"so I can't identify issue #{n} from this corpus." for n in sorted(refs))
    if refs:
        eids = [e for e in eids if _PR_ID.match(e)]       # explicit PR refs should not drag in scaffold entities
    elif direct_workspace:
        eids = []                                         # direct workspace/session lookup: skip repo entity cards
    eids = sorted(eids, key=lambda e: not _PR_ID.match(e))   # PR entities first: the direct answer above repo dumps
    for eid in eids:
        row = db.run(con, select(_ENT.c.id, _ENT.c.type, _ENT.c.name, _ENT.c.attributes)
                     .where(_ENT.c.id == eid)).fetchone()
        if not row:
            continue
        id_, ty, nm, attrs = row
        lines = [f"## Entity: {id_} ({ty or '?'}) — {nm or id_}"]
        if attrs and attrs != "{}":
            lines.append(f"attributes: {attrs}")
        m = _PR_ID.match(id_)
        if m:                                            # deterministic creator evidence, above extraction rows
            n = int(m.group(1))
            ev = _pr_evidence(con, n, want_merge=want_merge, query_repo=url_repo.get(n))
            if ev:
                lines += ev
                covered.add(n)
        a, s = actions.alias("a"), _S.alias("s")
        acts = db.run(con, select(a.c.verb, a.c.context, s.c.user, func.substr(s.c.started_at, 1, 10),
                                  s.c.project, a.c.session_id)
                      .select_from(a.outerjoin(s, s.c.session_id == a.c.session_id))
                      .where(a.c.target_id == eid).limit(10)).fetchall()
        uniq = lambda xs: list(dict.fromkeys(xs))        # extraction re-records the same edge per session — show once
        if acts:
            lines.append("actions:")
            lines += uniq(f"- {v} — {u or '?'} · {d or '?'} · {p or '-'}/{sid}" + (f" — {c[:80]}" if c else "")
                          for v, c, u, d, p, sid in acts)
        rels = db.run(con, select(relations.c.source_id, relations.c.rel, relations.c.target_id,
                                  relations.c.context)
                      .where(or_(relations.c.source_id == eid, relations.c.target_id == eid))
                      .limit(10)).fetchall()
        if rels:
            lines.append("relations:")
            lines += uniq(f"- {s} {r} {t}" + (f" — {c[:80]}" if c else "") for s, r, t, c in rels)
        m, s = mentions.alias("m"), _S.alias("s")
        ment = db.run(con, select(m.c.session_id, s.c.user, func.substr(s.c.started_at, 1, 10),
                                  s.c.project, func.substr(s.c.summary, 1, 90))
                      .select_from(m.outerjoin(s, s.c.session_id == m.c.session_id))
                      .where(m.c.entity_id == eid)
                      .order_by(s.c.started_at.desc()).limit(10)).fetchall()
        if ment:
            lines.append("mentioned in:")
            lines += uniq(f"- {u or '?'} · {d or '?'} · {p or '-'}/{sid}" + (f" — {sm}" if sm else "")
                          for sid, u, d, p, sm in ment)
        out.append("\n".join(lines))
    pr_intent = re.search(r"\bpr\b|pull|merge|creat|open", ql)   # "#3" alone may be a standup/issue/step number —
    for n in sorted(refs - covered):                         # only claim a PR miss on PR intent
        ev = _pr_evidence(con, n, want_merge=want_merge, query_repo=url_repo.get(n))
        if ev:                                           # entity missing but harvest knows it -> still answer
            out.append("\n".join(ev))
            continue
        a, s = actions.alias("a"), _S.alias("s")
        cre = db.run(con, select(a.c.session_id, s.c.user)   # no pr-link event, but the extractor may
                     .select_from(a.outerjoin(s, s.c.session_id == a.c.session_id))   # have seen a create edge
                     .where(a.c.target_id == f"PR-{n}", a.c.verb.in_(("create", "open")))
                     .limit(3)).fetchall()
        if cre:                                          # reconcile, don't deny: extractor evidence != deterministic pr-link proof
            out.append(f"Extractor evidence: PR #{n} created by session " +
                       ", ".join(f"{s} ({u or '?'})" for s, u in cre) +
                       " — no deterministic pr-link event to confirm (GitHub is the source of truth).")
        elif pr_intent:
            out.append(f"No session here recorded creating PR #{n} — no pr-link events in the corpus "
                       f"(opened outside these sessions or before indexing; GitHub is the source of truth).")
    # Ticket/bug-shaped identifiers ("Bug 1234", "NAB-123") the corpus has never seen get an honest miss —
    # otherwise recall serves a confident page of unrelated facts for a nonexistent id. Verify absence with
    # an FTS phrase probe first: an id living in free text (not as an entity) must stay recallable, silently.
    tickets = [(m.group(0), m.group(2)) for m in _TICKET_REF.finditer(query or "")]
    tickets += [(t, t) for t in _KEY_REF.findall(query or "") if not t.upper().startswith("PR-")]
    misses = []
    for disp, num in dict(tickets).items():
        if num.isdigit() and int(num) in covered:        # a PR card already answered this number
            continue
        if any(num.lower() == e.lower() or num.lower() in _WORD.findall(e.lower()) for e in eids):
            continue                                     # resolve() reached an entity for it -> a card answered
        phrase = " ".join(_WORD.findall(disp.lower()))
        try:   # raw by design (FTS MATCH): docs is the FTS5 virtual table, outside Base.metadata
            if con.execute("SELECT 1 FROM docs WHERE docs MATCH ? LIMIT 1", (f'"{phrase}"',)).fetchone():
                continue                                 # lives in free text -> recall will surface it
        except sqlite3.OperationalError:
            continue                                     # can't verify absence (no docs / odd token) -> no claim
        misses.append(disp)
    if misses:
        content = [t for t in _WORD.findall(ql) if len(t) >= 3 and t not in _NOISE
                   and t not in _TICKET_WORDS and not t.isdigit()
                   and t not in {w for d in misses for w in _WORD.findall(d.lower())}]
        if content:                                      # id + real topic words -> label, then let recall run
            out.insert(0, "Note: nothing indexed mentions " + ", ".join(misses) +
                          " — the context below matched your other words, not that identifier.")
        else:                                            # the id IS the question -> the miss IS the answer
            out.append("\n".join(f"No indexed session mentions {d} — it may predate CSA or have "
                                 f"happened outside these sessions." for d in misses))
    seen_sids = set()
    for tok in _SID_REF.findall(query or ""):            # "what did session abcd1234 do" — sessions aren't
        card = _session_card(con, tok)                    # entities; answer from pr_links/workspace directly
        if card and card not in seen_sids:
            seen_sids.add(card)
            out.append(card)
    ws_toks = [t for t in dict.fromkeys(_TOK.findall(ql)) if len(t) >= 6 and ("/" in t or "-" in t)]
    for tok in ws_toks[:2]:                               # branch/worktree-shaped tokens -> workspace evidence
        card = _workspace_card(con, tok)
        if card:
            out.append(card)
    return "\n\n".join(out)


# Markers that make a card a COMPLETE answer (skip FTS), not a weak entity match (prepend above FTS).
# Single source of truth: both card_for (CLI) and mcp_server._standalone_card read this list.
_STANDALONE = ("created by session", "No session here recorded", "## Workspace match:", "## Session:",
               "no merge recorded", "merged via `gh pr merge`", "issues aren't indexed", "NO pr-link evidence",
               "Extractor evidence:", "No indexed session mentions")


def card_for(con, query):
    """Identifier short-circuit for the CLI entry points (recall/ask), mirroring mcp_server.ask.
    Returns (card, standalone):
      card == ''       -> ABSTAIN: nothing structured; caller runs FTS/planner exactly as today.
      standalone True  -> deterministic evidence; the card IS the answer, skip FTS/planner.
      standalone False -> weak entity card; caller PREPENDS it above the FTS/planner output."""
    try:
        card = cards(con, resolve(con, query), query)
    except sqlite3.OperationalError:                     # schema surprise -> degrade to plain FTS, never crash
        return "", False
    return card, bool(card) and any(m in card for m in _STANDALONE)
