"""The Admin > Analytics numbers for one workspace: adoption, memory growth, recall in practice and
pipeline health, read-only. The workspace is the corpus file `con` opens, so nothing here can reach
another workspace's rows; the control-plane inputs (tokens, queue, stall) are passed in already
scoped to the same org.

Every windowed count keys on a UTC day: the window is `weeks` whole weeks ending with today, the
previous window the same length before it, and a weekly series has one bucket per week, oldest first.
"""
import calendar
import time
from dataclasses import dataclass

from sqlalchemy import and_, case, exists, func, literal, literal_column, select

from analyzer import db, improve, models, orgs, providers, queue, tenant

FACT_ORDER = ("decision", "gotcha", "constraint", "todo", "preference", "rule")   # the node palette; a type keeps its slot
SILENT_AFTER_DAYS = 7
TOP = 8
LOG_LIMIT = 200

_S = models.SessionRow.__table__
_F = models.Fact.__table__
_HC = models.hidden_context
_SV = models.served
_Q = models.queries
_UE = models.UiEvent.__table__
_COSTS = models.costs
_DAY = 86400


def _iso(secs):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


def _day(col):
    return func.substr(col, 1, 10)


def _in(col, lo, hi):
    """`col`'s UTC day in [lo, hi), compared as YYYY-MM-DD so the ledger's "2026-09-05 12:00:00" and the
    sessions' "2026-09-05T12:00:00Z" bucket the same way."""
    return and_(_day(col) >= lo, _day(col) < hi)


@dataclass(frozen=True)
class Window:
    weeks: int
    start: int          # epoch seconds of the first day
    since: str          # YYYY-MM-DD, first day of the window
    until: str          # YYYY-MM-DD, the day after today
    before: str         # YYYY-MM-DD, first day of the previous window

    @classmethod
    def ending_today(cls, weeks, now):
        end = calendar.timegm(time.gmtime(now)[:3] + (0, 0, 0)) + _DAY
        start = end - weeks * 7 * _DAY
        return cls(weeks, start, _iso(start)[:10], _iso(end)[:10], _iso(start - weeks * 7 * _DAY)[:10])

    def week(self, day):
        """Bucket index of a YYYY-MM-DD in this window, or None when it falls in the previous one."""
        if not self.since <= day < self.until:
            return None
        return (calendar.timegm(time.strptime(day, "%Y-%m-%d")) - self.start) // (7 * _DAY)

    def days(self):
        return [_iso(self.start + i * _DAY)[:10] for i in range(self.weeks * 7)]


def workspace(con, org_id, tokens, weeks, stall, now=None):
    """GET /api/workspace/analytics for the workspace whose corpus is `con`. `tokens` is
    orgs.list_tokens(org_id); `stall` is apiserver.extraction_status(org_id)."""
    now = time.time() if now is None else now
    w = Window.ending_today(weeks, now)
    agents = orgs.agent_emails(tokens)
    adoption = _adoption(con, tokens, agents, w, now)
    reach, transfer, top = _transfer(con, w, agents)
    recall = _asks(con, w)
    recall["transfer"] = transfer
    recall["top_reused"] = top
    return {"ok": True, "as_of": _iso(now),
            "window": {"weeks": weeks, "since": w.since, "until": w.until, "previous_since": w.before},
            "week_starts": [_iso(w.start + i * 7 * _DAY)[:10] for i in range(weeks)],
            "adoption": adoption,
            "memory": _memory(con, w, reach, agents),
            "recall": recall,
            "pipeline": _pipeline(con, org_id, stall, w, adoption["contributors"]),
            "web": _web(con, w)}


def _adoption(con, tokens, agents, w, now):
    day, user = _day(_S.c.started_at), func.lower(func.ifnull(_S.c.user, ""))
    rows = db.run(con, select(day, user, func.count())
                  .where(_S.c.source == "session", _in(_S.c.started_at, w.before, w.until))
                  .group_by(day, user)).fetchall()
    sessions = [0] * w.weeks
    people = [set() for _ in range(w.weeks)]
    cur, prev, cur_n, prev_n, agent_n = set(), set(), 0, 0, 0
    for day, user, n in rows:
        i = w.week(day)
        if i is None:
            prev_n += n
            prev.add(user)
            continue
        cur_n += n
        agent_n += n if user in agents else 0
        cur.add(user)
        sessions[i] += n
        people[i].add(user)
    shipping_agents = cur & agents
    cur -= agents | {""}
    prev -= agents | {""}
    members, connected = set(), set()
    for t in tokens:
        if not t["active"] or t["service"]:
            continue
        email = (t["email"] or "").lower()
        members.add(email)
        if (t["hostname"] or "") not in orgs.BROWSER_HOSTNAMES:
            connected.add(email)
    owner = func.lower(func.ifnull(_S.c.user, ""))
    shippers = {u for (u,) in db.run(con, select(owner).distinct().where(_S.c.source == "session", owner != ""))}
    return {"members": len(members),
            "shipped_without_credential": len(shippers - members - agents),
            "members_shipping": len(members & cur),
            "members_quiet": len((connected & members) - cur),
            "members_unconnected": len(members - connected - cur),
            "contributors": len(cur), "contributors_prev": len(prev),
            "agents": len(shipping_agents), "agent_sessions": agent_n,
            "sessions": cur_n, "sessions_prev": prev_n,
            "per_week": [{"sessions": sessions[i], "people": len(people[i] - agents - {""})} for i in range(w.weeks)],
            "silent": silent_machines(tokens, now)}


def silent_machines(tokens, now):
    """Machines whose newest contact across every live credential they hold is over a week old, oldest
    first. Per machine, not per credential: each `csa-client setup` mints a new token and leaves the old
    one, so a laptop shipping daily also holds credentials it stopped using; `expired` only when none of
    them still works."""
    machines = {}
    for t in tokens:
        if t["revoked_at"] is not None or (t["hostname"] or "") in orgs.BROWSER_HOSTNAMES:
            continue
        key = ((t["email"] or "").lower(), t["hostname"] or "")
        seen = t["last_used_at"] or t["created"] or ""
        row = machines.get(key)
        if row is None:
            machines[key] = {"user": t["email"], "hostname": t["hostname"], "last_used_at": seen,
                             "expired": not t["active"]}
            continue
        row["last_used_at"] = max(row["last_used_at"], seen)
        row["expired"] = row["expired"] and not t["active"]
    cutoff = _iso(now - SILENT_AFTER_DAYS * _DAY)
    return sorted((r for r in machines.values() if r["last_used_at"] < cutoff), key=lambda r: r["last_used_at"])


def _memory(con, w, reach, agents):
    """`reach` is author -> the teammates their facts reached in the window, from _transfer. `agents`
    marks the service-token authors, counted apart from people as on every screen."""
    cols = (_day(_F.c.created_at), func.ifnull(_F.c.type, "untyped"), func.lower(func.ifnull(_F.c.author, "")))
    rows = db.run(con, select(*cols, func.count())
                  .where(_in(_F.c.created_at, w.before, w.until)).group_by(*cols)).fetchall()
    by_week = [{} for _ in range(w.weeks)]
    authors = {}
    new = prev = 0
    for day, ftype, author, n in rows:
        i = w.week(day)
        if i is None:
            prev += n
            continue
        new += n
        by_week[i][ftype] = by_week[i].get(ftype, 0) + n
        if author:
            authors[author] = authors.get(author, 0) + n
    seen = {t for wk in by_week for t in wk}
    types = [t for t in FACT_ORDER if t in seen] + sorted(seen - set(FACT_ORDER))
    people = sorted(set(authors) | set(reach), key=lambda a: (-authors.get(a, 0), -len(reach.get(a, ())), a))
    return {"total": db.run(con, select(func.count()).select_from(_F)).fetchone()[0],
            "new": new, "new_prev": prev, "types": types,
            "per_week": [[wk.get(t, 0) for t in types] for wk in by_week],
            "contributors": [{"user": a, "facts": authors.get(a, 0), "reached": len(reach.get(a, ())),
                              "agent": a in agents} for a in people[:TOP]]}


def _asks(con, w):
    """Every recall request in `tel.queries`: the ambient prompt hook (engine `hook`) and the deliberate
    asks (MCP `ask`, the csa-recall skill). Answered means at least one result came back. Acted on is
    the signal harvester's token-overlap flag, which only a session shipped to CSA can carry, so
    `measured` (answered requests from such sessions) is its denominator."""
    landed = exists(select(literal(1)).select_from(_S).where(_S.c.session_id == _Q.c.session))
    cols = (_day(_Q.c.at), func.ifnull(_Q.c.engine, "") == "hook", func.ifnull(_Q.c.row_count, 0) > 0,
            func.ifnull(_Q.c.acted_on, 0) == 1, landed)
    rows = db.run(con, select(*cols, func.count())
                  .where(_in(_Q.c.at, w.before, w.until)).group_by(*cols)).fetchall()
    out = {"asks": 0, "asks_prev": 0, "answered": 0, "answered_prev": 0, "acted": 0, "measured": 0,
           "ambient": {"asks": 0, "answered": 0}, "asked": {"asks": 0, "answered": 0},
           "per_week": [{"asks": 0, "answered": 0, "acted": 0} for _ in range(w.weeks)]}
    for day, ambient, answered, acted, shipped, n in rows:
        i = w.week(day)
        if i is None:
            out["asks_prev"] += n
            out["answered_prev"] += n if answered else 0
            continue
        kind, wk = out["ambient" if ambient else "asked"], out["per_week"][i]
        kind["asks"] += n
        wk["asks"] += n
        out["asks"] += n
        if answered:
            kind["answered"] += n
            wk["answered"] += n
            out["answered"] += n
            out["measured"] += n if shipped else 0
        if acted:
            wk["acted"] += n
            out["acted"] += n
    return out


def recall_log(con, viewer="", limit=LOG_LIMIT, result="", now=None):
    """GET /api/sessions: the newest recall requests, the same `tel.queries` rows `asks` counts, and
    `total`, every request logged. `result` keeps only `answered` or `missed` rows; `total` and
    `answered` stay all-time. A request that found nothing keeps its text and its session (whose
    owner is the asker) private to its asker, and no row names who asked. `source` is the channel:
    `hook` (a strict /api/ask that sent a session, as the prompt hook always does), `api` (any other
    /api/ask) or `mcp`."""
    q = _Q.c
    total, answered = db.run(con, select(func.count(), func.ifnull(func.sum(case((q.row_count > 0, 1), else_=0)), 0))
                             ).fetchone()
    stmt = select(q.at, q.session, q.asked_by, q.engine, q.query_text, q.row_count)
    if result == "answered":
        stmt = stmt.where(q.row_count > 0)
    elif result == "missed":
        stmt = stmt.where(func.ifnull(q.row_count, 0) <= 0)
    rows = db.run(con, stmt.order_by(q.at.desc(), literal_column("rowid").desc()).limit(limit)).fetchall()
    me = (viewer or "").lower()
    out = []
    for at, session, who, engine, text, n in rows:
        mine = bool(me) and (who or "").lower() == me
        shown = (n or 0) > 0 or mine
        source = ("hook" if engine == "hook" and session else "api" if engine in ("hook", "fallback") else "mcp")
        out.append({"at": at, "session": (session or "") if shown else "", "mine": mine, "source": source,
                    "automatic": engine == "hook", "question": text if shown else None, "results": n or 0})
    return {"total": total, "answered": answered, "as_of": _iso(time.time() if now is None else now), "rows": out}


def shippers(con, agents):
    """(people, agents) who shipped a session, all time: distinct owners split by orgs.agent_emails."""
    owner = func.lower(func.ifnull(_S.c.user, ""))
    who = {u for (u,) in db.run(con, select(owner).distinct().where(_S.c.source == "session", owner != ""))}
    return len(who - agents), len(who & agents)


def reuses(lo=None, hi=None):
    """The one definition of reuse, as a select: a fact served to someone other than its author, one row
    per (ref, session) with `receiver`, `author`, `first` and `last`. The receiver is the asking
    credential, else the receiving session's owner, and both it and the author must be known. A serve
    into a session that itself produced the fact is not a reuse. `lo`/`hi` bound the serve's UTC day."""
    sv, rs, h = _SV.alias("sv"), _S.alias("rs"), _HC.alias("h")
    own = exists(select(literal(1)).select_from(h).where(h.c.ref == sv.c.ref, h.c.session_id == sv.c.session))
    receiver = func.lower(func.coalesce(func.nullif(sv.c.asked_by, ""), rs.c.user, ""))
    author = func.lower(func.ifnull(_F.c.author, ""))
    conds = [~own, func.ifnull(sv.c.session, "") != "", receiver != "", author != "", receiver != author]
    if lo:
        conds.append(_in(sv.c.asked_at, lo, hi))
    return (select(sv.c.ref, sv.c.session, func.max(receiver).label("receiver"), func.max(author).label("author"),
                   func.min(sv.c.asked_at).label("first"), func.max(sv.c.asked_at).label("last"))
            .select_from(sv.join(_F, _F.c.ref == sv.c.ref).outerjoin(rs, rs.c.session_id == sv.c.session))
            .where(*conds).group_by(sv.c.ref, sv.c.session))


def reused_facts(con):
    """Facts reused at least once, all time: the Welcome tile and Analytics' all-time figure."""
    r = reuses().subquery()
    return db.run(con, select(func.count(func.distinct(r.c.ref)))).fetchone()[0]


def _transfer(con, w, agents):
    """(reach by author, transfer counts, most reused facts), from `reuses` over this window and the
    previous one, each delivery bucketed by its first serve. An `agents` receiver is counted apart
    from people, the way adoption counts contributors."""
    per = [0] * w.weeks
    receivers, givers, reach, facts, prev = set(), set(), {}, {}, set()
    deliveries = 0
    for ref, session, who, author, first, last in db.run(con, reuses(w.before, w.until)).fetchall():
        i = w.week(first[:10])
        if i is None:
            prev.add(ref)
            continue
        deliveries += 1
        per[i] += 1
        receivers.add(who)
        givers.add(author)
        f = facts.setdefault(ref, {"people": set(), "sessions": set(), "last": ""})
        if who not in agents:
            reach.setdefault(author, set()).add(who)
            f["people"].add(who)
        f["sessions"].add(session)
        f["last"] = max(f["last"], last)
    top = sorted(facts.items(), key=lambda kv: (-len(kv[1]["people"]), -len(kv[1]["sessions"]), kv[0]))[:TOP]
    transfer = {"facts": len(facts), "facts_prev": len(prev), "facts_all_time": reused_facts(con),
                "deliveries": deliveries, "receivers": len(receivers - agents),
                "receiving_agents": len(receivers & agents), "contributors": len(givers),
                "per_week": per}
    return reach, transfer, [_reused_row(con, ref, f) for ref, f in top]


def _reused_row(con, ref, f):
    claim, ftype, author = db.run(con, select(_F.c.claim, _F.c.type, _F.c.author).where(_F.c.ref == ref)).fetchone()
    origin = db.run(con, select(_HC.c.session_id).select_from(_HC.join(_S, _S.c.session_id == _HC.c.session_id))
                    .where(_HC.c.ref == ref)
                    .order_by(func.ifnull(_S.c.ended_at, _S.c.started_at), _HC.c.session_id).limit(1)).fetchone()
    return {"ref": ref, "claim": claim, "type": ftype, "author": author, "people": len(f["people"]),
            "sessions": len(f["sessions"]), "last_served": f["last"], "session_id": origin[0] if origin else None}


def _ledger_model(ref):
    """The model a ledger row names. Rows written before the ledger kept the served id hold the role
    they ran under (`main`), read as the model that role runs on now."""
    try:
        return providers.target(ref or "").model
    except RuntimeError:
        return ref


def _spend(con, lo, hi):
    """{day: (usd, tokens, unpriced)} over the ledger. A call that reported tokens but no cost is priced
    now at its model's list price, so a row billed before CSA knew the price counts; `unpriced` when the
    model still has none."""
    c = _COSTS.c
    day = _day(c.at)
    owed = lambda col: func.sum(case((c.cost_usd.is_(None), func.ifnull(col, 0)), else_=0))
    tokens = func.ifnull(c.input_tokens, 0) + func.ifnull(c.output_tokens, 0)
    out = {}
    for d, ref, billed, n, tin, cached, tout in db.run(
            con, select(day, c.model, func.ifnull(func.sum(c.cost_usd), 0), func.sum(tokens),
                        owed(c.input_tokens), owed(c.cache_read_tokens), owed(c.output_tokens))
            .where(_in(c.at, lo, hi)).group_by(day, c.model)).fetchall():
        late = improve.list_cost(_ledger_model(ref), max(0, tin - cached), cached, 0, tout) if tin or tout else 0.0
        usd, k, blind = out.get(d, (0.0, 0, False))
        out[d] = (usd + billed + (late or 0.0), k + n, blind or late is None)
    return out


def _pipeline(con, org_id, stall, w, contributors):
    """`unpriced`: a call in the window reported tokens but no model CSA can price, so `spend_usd` is
    a floor and no per-unit cost is claimed."""
    c = _COSTS.c
    per_day = {d: {"day": d, "usd": 0.0, "tokens": 0} for d in w.days()}
    unpriced = False
    for d, (usd, n, blind) in _spend(con, w.since, w.until).items():
        per_day[d]["usd"] = round(usd, 4)
        per_day[d]["tokens"] = n
        unpriced = unpriced or blind
    spend = sum(r["usd"] for r in per_day.values())
    prev = sum(usd for usd, _n, _b in _spend(con, w.before, w.since).values())
    extracted = db.run(con, select(func.count(func.distinct(c.session_id)))
                       .where(_in(c.at, w.since, w.until), c.session_id.isnot(None))).fetchone()[0]
    cap = tenant.config(con).daily_usd
    return {"backlog": queue.depth(org_id)[0], "poisoned": queue.poison_depth(org_id),
            "stalled": stall["stalled"], "reason": stall["reason"],
            "extracted": extracted, "spend_usd": round(spend, 2), "spend_prev": round(prev, 2),
            "tokens": sum(r["tokens"] for r in per_day.values()), "unpriced": unpriced,
            "per_extracted": round(spend / extracted, 4) if extracted and not unpriced else None,
            "per_contributor": round(spend / contributors, 2) if contributors and not unpriced else None,
            "daily_cap_usd": cap, "days_over_cap": sum(1 for r in per_day.values() if 0 < cap <= r["usd"]),
            "spend_per_day": list(per_day.values())}


def _web(con, w):
    """The CSA web app's own use, from the browser's POST /api/track rows."""
    win = _in(_UE.c.at, w.since, w.until)
    n = func.count().label("n")
    events = dict(db.run(con, select(_UE.c.event, func.count()).where(win).group_by(_UE.c.event)).fetchall())
    return {"people": db.run(con, select(func.count(func.distinct(_UE.c.user)))
                             .where(win, _UE.c.user != "")).fetchone()[0],
            "searches": events.get("search", 0), "session_opens": events.get("session_open", 0),
            "flags": events.get("flag", 0),
            "pages": [{"page": p, "n": k} for p, k in db.run(con, select(_UE.c.page, n)
                                                             .where(win, _UE.c.event == "page_view")
                                                             .group_by(_UE.c.page).order_by(n.desc())).fetchall()]}
