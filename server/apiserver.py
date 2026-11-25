"""The HTTP API: the auth gate, every /api/* route, and the built UI from ui/dist.

    uv run python apiserver.py            # http://127.0.0.1:8766
    # dev: run `npm run dev` in ui/ — Vite serves the app and proxies /api here.

UI flags and the MCP `flag` tool write the same reviews table, so they are one feedback channel.
"""
import calendar, glob, json, logging, os, re, sys, threading, time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, quote, parse_qs

from sqlalchemy import and_, column, delete, distinct, exists, func, insert, literal, literal_column, or_, select, text

from analyzer import store, ingest, redact, search, route, auth, llm, queue, improve, entity_types, entity_gate, tenant, tracer
from analyzer import incremental
from analyzer import migrations
from analyzer import db, models
from analyzer import cliauth, orgs, sso
from analyzer import analytics, archive, bootstrap, fleet, providers
from analyzer import gql, pagination
from analyzer.project import normalize_project        # name-imported: `project` is a param name all over this file
import app as recall_app
import csa                                               # no import cycle: csa's app import is lazy

log = logging.getLogger("csa.api")
HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIST = os.path.join(HERE, "ui", "dist")
INSTALL_SH = os.path.join(HERE, "install.sh")            # served, templated, at GET /install
# The client binaries the image builds from its own commit (Dockerfile's client stage).
CLIENT_DIST = os.environ.get("CSA_CLIENT_DIST", "").strip() or os.path.join(HERE, "client-dist")
_ASSET_RE = re.compile(r"^csa-client-(darwin|linux|windows)-(amd64|arm64)(\.exe)?$|^checksums\.txt$")
# What orgs.create_invite mints. Anything else is not a link and is never echoed into a Set-Cookie.
_JOIN_RE = re.compile(r"^%s[A-Za-z0-9_-]{1,64}$" % orgs.JOIN_PREFIX)


def _client_published():
    """The date this box's client binaries were built (checksums.txt's mtime), or None when it
    publishes none."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(
            os.path.join(CLIENT_DIST, "checksums.txt"))))
    except OSError:
        return None

# The pre-auth routes: a closed allowlist, anything not matched here goes through the gate. They run
# on a machine that has no credential yet. /api/policy is here so `csa-client setup` can show the
# redaction floor before consent; the floor says how much the server strips, not any corpus data.
_PUBLIC_EXACT = {"/install", "/cli-auth", "/api/cli-auth/new", "/api/cli-auth/approve", "/api/policy",
                 "/api/ui-login", "/api/ui-logout",
                 "/api/sso/start", "/api/sso/callback",  # GETs, so no _COOKIE_POST_OK entry
                 "/api/auth-config",                     # read by the logged-out SPA
                 "/api/workspace-request"}
_PUBLIC_PREFIXES = ("/latest/", "/api/cli-auth/",        # binaries + the poll endpoint
                    "/join/")                            # an invite link: the invited person has no credential yet


def _revoke_presented(machine):
    """(status, body) for a machine revoking the per-user token it presented. `machine` is None for the
    shared secret and with auth off: there is nothing of its own to revoke."""
    if not machine:
        return 200, {"ok": True, "revoked": False}
    target = orgs.resolve_token(machine["token_hash"], machine["org_id"])
    if target["service"]:
        return 403, {"ok": False, "code": "forbidden",
                     "error": "a service token is revoked from Service Tokens, not by itself"}
    if orgs.is_mutating_admin(target) and orgs.admin_count(machine["org_id"], mutating_only=True) <= 1:
        return 409, {"ok": False, "code": "last_admin",
                     "error": "this is the workspace's only admin credential, so it stays live. "
                              "Make someone else an admin, then revoke it from Team."}
    orgs.revoke(machine["token_hash"])
    return 200, {"ok": True, "revoked": True}


def _content_length(headers):
    """The declared body length, or 0 when the header is missing or not a number: junk reads as an
    empty body, never as a traceback that drops the connection."""
    try:
        return max(0, int(headers.get("Content-Length", 0) or 0))
    except ValueError:
        return 0


def _public(path):
    """True for the pre-auth onboarding surface: public artifacts, or routes gated on their own terms
    (an invite, an unguessable nonce). The SPA shell is not here: do_GET serves it on gate failure."""
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


# What a browser session may POST: what the dashboard does, nothing else. The cookie is ambient
# authority an XSS inherits, so /api/forget, /api/ingest and /api/ask stay out. A header-bearer client
# is unrestricted: it had to hold a token. Each entry below is either reversible and audited, or
# re-checks the caller's role in its route.
_COOKIE_POST_OK = {"/api/flag", "/api/hctype",
                   "/api/entity-types",            # admins only (route-checked): a description is the extraction prompt
                   "/api/track",
                   "/api/graphql",                 # defines no mutations (tests pin it); a mutation must come out of here
                   # /api/org/invite/rotate stays out: it returns the workspace's permanent shared secret.
                   # An invite link names one address, expires, is revocable, and only mints a member.
                   "/api/org/member/role", "/api/org/member/revoke",
                   "/api/org/invite/new", "/api/org/invite/revoke",
                   "/api/org/service-token/new",   # a member token, listed under Team, idles out
                   # Where session text is sent: audited, and refused on a multi-tenant server.
                   "/api/org/provider/save", "/api/org/provider/remove", "/api/org/models",
                   "/api/config"}                  # tenant.apply refuses the off-server fields to a cookie

# The anonymous workspace request is the one route where a stranger makes the server write, so it has
# its own limiter. Keyed by CF-Connecting-IP when present (behind the tunnel every visitor shares
# client_address); the global window bounds a caller who spoofs it from the origin port. In-memory:
# a restart loses nothing a user waits on.
_WSREQ_LOCK = threading.Lock()
_WSREQ_HITS = {}                                          # key -> deque of epoch seconds, newest last
_WSREQ_GLOBAL = deque()
_WSREQ_MAX_KEYS = 4096
WSREQ_PER_IP = 10
WSREQ_WINDOW_S = 3600
WSREQ_GLOBAL = 120


def _wsreq_limited(key):
    """True -> answer 429. Sliding window, per-key and global, on caller and clock only (never stored
    request state, so it is no existence oracle). A refusal touches no state."""
    per_key, window, global_cap = WSREQ_PER_IP, WSREQ_WINDOW_S, WSREQ_GLOBAL
    now = time.time()
    with _WSREQ_LOCK:
        while _WSREQ_GLOBAL and now - _WSREQ_GLOBAL[0] > window:
            _WSREQ_GLOBAL.popleft()
        dq = _WSREQ_HITS.get(key)
        if dq is not None:
            while dq and now - dq[0] > window:
                dq.popleft()
        key_hits = len(dq) if dq is not None else 0
        if key_hits >= per_key or len(_WSREQ_GLOBAL) >= global_cap:
            return True                                   # refuse: create nothing, evict nothing
        if dq is None:                                    # admit a NEW key: now it is worth tracking
            if len(_WSREQ_HITS) >= _WSREQ_MAX_KEYS:       # bound memory: drop the least-recently-hit key
                del _WSREQ_HITS[min(_WSREQ_HITS, key=lambda k: _WSREQ_HITS[k][-1] if _WSREQ_HITS[k] else 0.0)]
            dq = _WSREQ_HITS[key] = deque()
        dq.append(now)
        _WSREQ_GLOBAL.append(now)
        return False


def _webhook_text(req_id, company, email):
    """The one-line admin notice for a new workspace request — plain text, Slack-compatible."""
    return (f"New CSA workspace request: {company} <{email}>\n"
            f"Approve: uv run python csa.py org approve {req_id}")


def _request_webhook():
    """The request_webhook setting (a Slack incoming webhook, or any URL that accepts {"text": ...}),
    or "". Read per request from control.db: a change takes effect without a restart."""
    return tenant.deployment().request_webhook


def _notify_new_request(url, req_id, company, email):
    """Best-effort admin notice, POSTed from a daemon thread; an error is logged, never raised. The URL
    is operator-set; the stranger's company/email are sanitized and ride only in the body."""
    payload = json.dumps({"text": _webhook_text(req_id, company, email)}).encode()

    def _post():
        try:
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:                            # a dead webhook must never break a request
            log.warning("workspace-request webhook failed: %s", str(e)[:200])

    threading.Thread(target=_post, daemon=True).start()


def _org_name(org_id):
    """The org's human name, or '' when there is no row or the control plane is unreadable."""
    try:
        row = db.run(orgs.control_db(), select(tenant.Org.__table__.c.name)
                     .where(tenant.Org.__table__.c.id == org_id)).fetchone()
        return (row and row[0]) or ""
    except Exception:
        return ""


def _same_origin(headers):
    """The CSRF check behind SameSite=Strict, on the unforgeable `Sec-Fetch-Site`. Refuses `same-site`
    too: a sibling subdomain is same-site to SameSite=Strict and would still carry the cookie. An
    absent header is a non-browser client, which authenticates by header."""
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    return site in ("", "same-origin", "none")


def _browser_session(headers):
    """True when this request's authority comes from the session cookie: it carries one AND would not
    authenticate on its headers alone. A presence check on the bearer header would let a junk bearer
    beside a valid cookie escape _COOKIE_POST_OK and _same_origin."""
    if not auth._cookie(headers, auth.SESSION_COOKIE):
        return False
    ok, _p, _err = auth.identify(headers)                 # header-only: allow_cookie defaults False
    return not ok


LABELS = {"gold": "boost", "wrong": "hide", "unimportant": "down-rank",   # 1:1 with rerank actions (see flag tool)
          "clear": "back to neutral"}                                     # removes a prior flag
MAX_BODY = 32 * 1024 * 1024           # matches the proxy's body cap in k8s/

# Core table handles; statements run through db.run(con, ...). `docs` (FTS5) is not a model.
_S = models.SessionRow.__table__
_E = models.Entity.__table__
_RV = models.Review.__table__
_SIG = models.Signal.__table__
_HT = models.HcType.__table__
_PL = models.PrLink.__table__
_SW = models.SessionWorkspace.__table__
_SM = models.SessionMetrics.__table__
_HC = models.hidden_context
_SV = models.served
_MEN = models.mentions
_REL = models.relations
_ACT = models.actions
_LNK = models.links
_WO = models.worked_on
_DS = models.data_sources
_COSTS = models.costs


def _dicts(con, stmt):
    """Rows keyed by the statement's own column names."""
    keys = list(stmt.selected_columns.keys())
    return [dict(zip(keys, r)) for r in db.run(con, stmt).fetchall()]


def session_detail(con, sid):
    s = _SV.alias("s")
    hc_sub = (select(_HC.c.ref, func.min(_HC.c.type).label("type"), func.min(_HC.c.claim).label("claim"),
                     func.min(_HC.c.evidence).label("evidence"))
              .group_by(_HC.c.ref)).subquery("hc")
    rv = _RV.alias("rv")
    rows = db.run(con, select(s.c.asked_at, s.c.question, s.c.ref, s.c.kind, s.c.project,
                              hc_sub.c.type, hc_sub.c.claim, hc_sub.c.evidence, rv.c.status)
                  .select_from(s.outerjoin(hc_sub, hc_sub.c.ref == s.c.ref)
                                .outerjoin(rv, and_(rv.c.kind == "hc", rv.c.ref == s.c.ref)))
                  .where(s.c.session == sid)
                  .order_by(s.c.asked_at, literal_column("s.rowid"))).fetchall()
    qs = {}
    for asked_at, question, ref, kind, project, ty, claim, evidence, label in rows:
        q = qs.setdefault((asked_at, question), {"asked_at": asked_at, "question": question, "facts": []})
        q["facts"].append({"ref": ref, "kind": kind, "project": project, "type": ty, "claim": claim,
                           "evidence": evidence, "label": label, "flaggable": kind == "hidden_context"})
    return list(qs.values())


def corpus_sessions(con, owner="", principal=None):
    """All INDEXED corpus sessions (the mined Claude Code sessions) + per-session fact/entity counts.
    `owner` scopes to one person's sessions. `cost_usd` is None unless gql.may_see_metrics allows this
    caller that owner's numbers."""
    s, h, m, sm = _S.alias("s"), _HC.alias("h"), _MEN.alias("m"), _SM.alias("sm")
    hc = (select(func.count()).select_from(h).where(h.c.session_id == s.c.session_id)
          .scalar_subquery().label("hc"))
    ents = (select(func.count(distinct(m.c.entity_id))).select_from(m)
            .where(m.c.session_id == s.c.session_id).scalar_subquery().label("ents"))
    stmt = (select(s.c.session_id, s.c.project, s.c.git_branch, s.c.started_at, s.c.n_user, s.c.n_asst,
                   s.c.deployed_to_prod, s.c.summary, s.c.user, hc, ents, improve.known_cost(sm))
            .select_from(s.outerjoin(sm, sm.c.session_id == s.c.session_id))
            .where(s.c.source == "session"))
    if owner:
        stmt = stmt.where(s.c.user == owner)
    rows = db.run(con, stmt.order_by(s.c.started_at.desc())).fetchall()
    mode = auth.current_mode()
    return [{"session_id": a, "project": b, "git_branch": c, "started_at": d, "n_user": e, "n_asst": f,
             "deployed_to_prod": g, "summary": h, "user": u, "hidden_context": i, "entities": j,
             "cost_usd": cost if gql.may_see_metrics(principal or {}, u, mode) else None}
            for a, b, c, d, e, f, g, h, u, i, j, cost in rows]


def activity(con, date="", owner="", project="", branch="", limit=200, offset=0):
    """The standup view: on one day (by `ended_at`), who worked on what, and what landed.

    Carries only WHAT was worked on, never durations or costs: how well someone worked is the Improve
    tab's question and stays private to its owner."""
    s = _S.alias("s")
    d = func.date(s.c.ended_at).label("d")
    days = [r[0] for r in db.run(con, select(d).distinct()
                                 .where(s.c.source == "session", s.c.ended_at.isnot(None))
                                 .order_by(d.desc()).limit(60))]
    if not days:
        return {"date": None, "days": [], "people": [], "quiet": [], "totals": {}}
    day = date if date in days else days[0]
    conds = [s.c.source == "session", func.date(s.c.ended_at) == day]
    if owner:
        conds.append(s.c.user == owner)
    if project:
        conds.append(s.c.project == project)      # exact: 'ledger' must not pull in 'ledger-tools'
    if branch:
        conds.append(s.c.git_branch == branch)
    # The day's totals come from SQL over the whole filtered day, never from the page of rows, so the
    # header cannot disagree with the rows or shrink as you paginate. `facts` lets the UI fold trivial
    # sessions.
    lim = max(1, min(int(limit or 200), 1000))
    off = max(0, int(offset or 0))
    n_sessions, n_people, n_prod = db.run(con, select(
        func.count(), func.count(distinct(s.c.user)), func.ifnull(func.sum(s.c.deployed_to_prod), 0))
        .select_from(s).where(*conds)).fetchone()
    pl = _PL.alias("pl")
    n_prs = db.run(con, select(func.count(distinct(pl.c.repo.concat("#").concat(pl.c.pr_number))))
                   .select_from(pl)
                   .where(pl.c.session_id.in_(select(s.c.session_id).where(*conds)))).fetchone()[0]
    h = _HC.alias("h")
    facts_col = (select(func.count()).select_from(h).where(h.c.session_id == s.c.session_id)
                 .scalar_subquery().label("facts"))
    rows = db.run(con, select(s.c.session_id, s.c.user, s.c.project, s.c.summary, s.c.deployed_to_prod,
                              s.c.ended_at, s.c.git_branch, facts_col)
                  .where(*conds).order_by(s.c.ended_at.desc()).limit(lim).offset(off)).fetchall()
    people, prod_total, prs_total = {}, 0, set()
    for sid, r_user, r_proj, r_summary, r_prod, r_ended, r_branch, r_facts in rows:
        who = r_user or "unknown"
        p = people.setdefault(who, {"user": who, "sessions": 0, "projects": {}, "prod": 0,
                                    "prs": [], "did": []})
        p["sessions"] += 1
        p["projects"][r_proj or "?"] = p["projects"].get(r_proj or "?", 0) + 1
        p["prod"] += int(r_prod or 0)
        prod_total += int(r_prod or 0)
        for repo, num, url in db.run(con, select(_PL.c.repo, _PL.c.pr_number, _PL.c.url)
                                     .where(_PL.c.session_id == sid)):
            tag = f"{repo}#{num}"
            if tag not in [x["tag"] for x in p["prs"]]:
                p["prs"].append({"tag": tag, "url": url})
            prs_total.add(tag)
        p["did"].append({"session_id": sid, "project": r_proj, "summary": r_summary,
                         "branch": r_branch, "prod": int(r_prod or 0), "at": (r_ended or "")[11:16],
                         "facts": int(r_facts or 0)})
    out = sorted(people.values(), key=lambda p: -p["sessions"])
    for p in out:
        p["projects"] = sorted(p["projects"].items(), key=lambda kv: -kv[1])
        p["trivial"] = sum(1 for d in p["did"] if not d["facts"])
    # The team is who shipped in the 14 days up to the day on screen ("3 of 5 worked today"), so paging
    # back never reports a last-seen date later than the page.
    roster = db.run(con, select(_S.c.user, func.max(_S.c.ended_at))
                    .where(_S.c.source == "session", _S.c.user.isnot(None),
                           _S.c.ended_at >= func.date(day, "-14 days"),
                           func.date(_S.c.ended_at) <= day)
                    .group_by(_S.c.user)).fetchall()
    # Who was quiet, and when last seen: only for the unfiltered team view, and from its own query
    # because `out` is one page of the day.
    quiet = []
    if not owner and not project and not branch:
        worked = {r[0] for r in db.run(con, select(_S.c.user).distinct()
                                       .where(_S.c.source == "session", func.date(_S.c.ended_at) == day,
                                              _S.c.user.isnot(None)))}
        quiet = sorted(({"user": u, "last": (la or "")[:10]} for u, la in roster if u not in worked),
                       key=lambda q: q["last"], reverse=True)
    return {"date": day, "days": days, "people": out, "quiet": quiet,
            "filters": {"project": project, "branch": branch, "owner": owner},
            "page": {"limit": lim, "offset": off, "total": n_sessions, "returned": len(rows)},
            "totals": {"people": n_people, "team": len(roster), "sessions": n_sessions,
                       "prod": int(n_prod or 0), "prs": n_prs,
                       # per-person chips: higher than `prs` when two people share a PR
                       "pr_credits": sum(len(p["prs"]) for p in out)}}


def improvements(con, owner="", limit=100, caller="", org_admin=False):
    """Finished sessions worst-first, with what would have made each cheaper or shorter.

    Private to their owner, enforced here: a caller who is not a workspace admin gets only their own
    rows. A session is judged only once quiet for the workspace's settle_min minutes; live ones come
    back flagged `settled: false`."""
    settle_min = tenant.config(con).settle_min
    if not org_admin:
        # owner="" means unfiltered, so a caller the gate could not name falls through to it only where
        # there is provably one user (CSA_AUTH=off).
        if caller:
            owner = caller
        elif auth.current_mode() == "off":
            owner = ""
        else:
            return {"rows": [], "lessons": [], "totals": {}, "scoped_to": "", "can_widen": False,
                    "unnamed": True}
    m, s = _SM.alias("m"), _S.alias("s")
    conds = [m.c.session_id == s.c.session_id]
    if owner:
        conds.append(s.c.user == owner)
    # datetime() on both sides: ended_at's 'T' would compare as TEXT against datetime('now')'s space.
    settled = and_(s.c.ended_at.isnot(None),
                   func.datetime(s.c.ended_at) <= func.datetime("now", f"-{settle_min} minutes")
                   ).label("settled")
    cost = improve.known_cost(m)
    out = _dicts(con, select(
        m.c.session_id, s.c.project, s.c.user, s.c.summary, s.c.ended_at,
        m.c.model, m.c.tool_calls, m.c.tool_errors, m.c.dup_calls, m.c.reread_calls, m.c.top_read,
        m.c.top_read_n, m.c.top_tool, m.c.in_tokens, m.c.out_tokens, m.c.cache_read_tokens,
        m.c.cache_write_tokens, cost, m.c.wall_s, m.c.idle_s, m.c.max_gap_s,
        s.c.n_user, s.c.n_asst, settled)
        .select_from(m, s).where(*conds)          # implicit FROM m, s — the join condition stays in WHERE
        .order_by(cost.desc(), m.c.tool_calls.desc())
        .limit(max(1, min(limit, 500))))
    for d in out:
        d["settled"] = bool(d["settled"])
        d["findings"] = improve.findings(d) if d["settled"] else []
    totals = {"sessions": len(out), "cost_usd": round(sum(d["cost_usd"] or 0 for d in out), 2),
              "tool_calls": sum(d["tool_calls"] or 0 for d in out),
              "wasted_calls": sum((d["dup_calls"] or 0) + (d["reread_calls"] or 0) + (d["tool_errors"] or 0)
                                  for d in out)}
    return {"settle_minutes": settle_min, "totals": totals, "scoped_to": owner, "can_widen": bool(org_admin),
            "projects": improve.project_lessons(out), "rows": out}


# Onboarding ranks on `served` (written on every recall, ambient hook included), not on `signals`
# (scraped from transcripts, which drop the ambient path); reuse is analytics.reuses.
ONBOARD_TYPES = ("gotcha", "constraint")
COLD_START_MIN_AGE_DAYS = 21


def onboarding(con, project="", limit=3, owner="", agents=frozenset()):
    """The highest-signal facts for a first run. `warm`: most-reused first. `cold` (nothing reused
    yet): oldest first, only facts at least COLD_START_MIN_AGE_DAYS old, with no reuse claim.
    `agents` is orgs.agent_emails: they ship but are not people."""
    people, n_agents = analytics.shippers(con, agents)
    h, s = _HC.alias("h"), _S.alias("s")
    conds = [s.c.source == "session", h.c.type.in_(ONBOARD_TYPES),
             # never `= organizational`: rows written before the scope field carry NULL
             or_(h.c.scope.is_(None), h.c.scope != "personal"),
             h.c.claim.isnot(None), func.length(h.c.claim) > 40]
    if project:
        conds.append(s.c.project == project)      # exact, not LIKE: 'ledger' must not match 'ledger-tools'
    # Prefer what someone else learned in the projects this person works in.
    personal = []
    if owner and not project:
        personal = [r[0] for r in db.run(con, select(_S.c.project)
                                         .where(_S.c.source == "session", _S.c.user == owner,
                                                _S.c.project.isnot(None))
                                         .group_by(_S.c.project)
                                         .order_by(func.max(_S.c.ended_at).desc()).limit(5))]
    if personal:
        conds.append(s.c.project.in_(personal))
        conds.append(s.c.user != owner)
    s2, rv = _S.alias("s2"), _RV.alias("rv")
    reuse = analytics.reuses().subquery("reuse")
    per_fact = (select(reuse.c.ref, func.count(distinct(reuse.c.session)).label("sessions"),
                       func.count(distinct(reuse.c.receiver)).label("people"))
                .group_by(reuse.c.ref).subquery("per_fact"))
    reused_col = func.ifnull(per_fact.c.sessions, 0).label("reused")
    gold_col = exists(select(literal(1)).select_from(rv)
                      .where(rv.c.kind == "hc", rv.c.ref == h.c.ref, rv.c.status == "gold")).label("gold")
    inner = (select(h.c.ref, h.c.type, h.c.claim, h.c.evidence, s.c.project, s.c.user, s.c.started_at,
                    reused_col, func.ifnull(per_fact.c.people, 0).label("people"), gold_col, h.c.session_id)
             .select_from(h.join(s, s.c.session_id == h.c.session_id)
                          .outerjoin(per_fact, per_fact.c.ref == h.c.ref))
             .where(*conds))
    n = max(1, min(limit, 10))
    wrapped = inner.subquery()                    # warm reads the computed `reused` -> needs the outer wrap
    warm = db.run(con, select(wrapped).where(wrapped.c.reused > 0)
                  .order_by(wrapped.c.reused.desc(), wrapped.c.gold.desc(), wrapped.c.started_at.asc())
                  .limit(n)).fetchall()
    frame = "warm"
    rows = warm
    scoped = bool(personal) and bool(rows)
    if not rows and personal:                     # nothing from teammates here: the whole corpus instead
        base = onboarding(con, project=project, limit=limit, owner="", agents=agents)
        base["scoped_to_you"] = False
        return base
    if not rows:
        frame = "cold"
        rows = db.run(con, inner.where(s.c.started_at <= func.date("now", f"-{COLD_START_MIN_AGE_DAYS} days"))
                      .order_by(gold_col.desc(), s.c.started_at.asc()).limit(n)).fetchall()
    cards = []
    landed = exists(select(literal(1)).select_from(s2).where(s2.c.session_id == reuse.c.session))
    for ref, ty, claim, evidence, proj, user, started, reused, reached, gold, origin in rows:
        # the receiving sessions the corpus holds, so the reuse claim is checkable
        reused_in = [r[0] for r in db.run(con, select(reuse.c.session).where(reuse.c.ref == ref, landed)
                                          .order_by(reuse.c.last.desc()).limit(5))]
        cards.append({"ref": ref, "type": ty, "claim": claim, "evidence": evidence,
                      "project": proj, "user": user, "learned_at": (started or "")[:10],
                      "reused": int(reused or 0), "people": int(reached or 0), "reused_in": reused_in,
                      "gold": bool(gold), "session_id": origin})
    first = db.run(con, select(func.min(_S.c.started_at)).where(_S.c.source == "session")).fetchone()[0]
    weeks = 0
    if first:
        try:
            weeks = max(1, int((time.time() - calendar.timegm(
                time.strptime(first[:10], "%Y-%m-%d"))) // 604800))
        except (ValueError, TypeError):
            weeks = 0
    q1 = lambda stmt: db.run(con, stmt).fetchone()[0]
    metrics = {
        "facts": q1(select(func.count()).select_from(_HC)),
        "gotchas": q1(select(func.count()).select_from(_HC).where(_HC.c.type.in_(ONBOARD_TYPES))),
        "sessions": q1(select(func.count()).select_from(_S).where(_S.c.source == "session")),
        "people": people, "agents": n_agents, "weeks": weeks,
        "reused": analytics.reused_facts(con),
    }
    # sessions the reader shipped: zero means every number on the screen is someone else's work
    mine = db.run(con, select(func.count()).select_from(_S)
                  .where(_S.c.source == "session", _S.c.user == owner)).fetchone()[0] if owner else 0
    return {"frame": frame, "solo": people <= 1, "team": people, "metrics": metrics, "cards": cards,
            "mine": mine,
            "scoped_to_you": scoped, "projects": personal[:3] if scoped else []}


def corpus_session(con, sid, principal=None):
    """One corpus session's extracted content. `metrics` is None when gql.may_see_metrics denies the
    caller this owner's numbers, and carries None fields when the session was never measured."""
    found = _dicts(con, select(_S.c.session_id, _S.c.project, _S.c.git_branch, _S.c.started_at,
                               _S.c.ended_at, _S.c.n_user, _S.c.n_asst, _S.c.summary,
                               _S.c.deployed_to_prod, _S.c.source, _S.c.user)
                   .where(_S.c.session_id == sid))
    if not found:
        return {"error": f"no session {sid!r}"}
    session = found[0]
    h, rv, g = _HC.alias("h"), _RV.alias("rv"), _SIG.alias("g")
    used_col = (select(func.count(distinct(g.c.session_id))).select_from(g)
                .where(g.c.ref == h.c.ref, g.c.kind.in_(("recalled_in_session", "acted_on")))
                .scalar_subquery())
    dom_col = exists(select(literal(1)).select_from(g)
                     .where(g.c.ref == h.c.ref, g.c.kind == "dominant_entity"))
    hc = {}
    for ty, scope, claim, evidence, ref, label, used, dom in db.run(con, select(
            h.c.type, h.c.scope, h.c.claim, h.c.evidence, h.c.ref, rv.c.status, used_col, dom_col)
            .select_from(h.outerjoin(rv, and_(rv.c.kind == "hc", rv.c.ref == h.c.ref)))
            .where(h.c.session_id == sid)):
        hc.setdefault(ty, []).append({"claim": claim, "evidence": evidence, "scope": scope, "ref": ref,
                                      "label": label, "used": used, "dominant": int(bool(dom))})
    gate_cfg = tenant.config(con)
    if gate_cfg.entity_floor:                        # noise gate: mark, never hide, this session's one-offs
        e, m, erv = _E.alias("e"), _MEN.alias("m"), _RV.alias("rv")
        frm, served_expr = entity_gate.gate_parts(gate_cfg, e)   # the ONE served predicate, as a projection
        entities = [{"id": i, "type": t, "name": n, "attributes": a, "label": l, "served": int(bool(sv))}
                    for i, t, n, a, l, sv in db.run(con, select(
                        e.c.id, e.c.type, e.c.name, e.c.attributes, erv.c.status,
                        served_expr.label("served"))
                        .select_from(m.join(frm, e.c.id == m.c.entity_id)
                                     .outerjoin(erv, and_(erv.c.kind == "entity", erv.c.ref == e.c.id)))
                        .where(m.c.session_id == sid).group_by(e.c.id).order_by(e.c.id))]
    else:                                            # off: no `served` key at all
        # CARVE-OUT (tests/test_no_raw_sql.py): the floor-off parity statement, kept verbatim.
        entities = [{"id": i, "type": t, "name": n, "attributes": a, "label": l} for i, t, n, a, l in con.execute(
            "SELECT e.id,e.type,e.name,e.attributes, rv.status FROM mentions m JOIN entities e ON e.id=m.entity_id "
            "LEFT JOIN reviews rv ON rv.kind='entity' AND rv.ref=e.id WHERE m.session_id=? GROUP BY e.id ORDER BY e.id", (sid,))]
    relations = [{"source_id": a, "rel": r, "target_id": b, "context": c} for a, r, b, c in db.run(
        con, select(_REL.c.source_id, _REL.c.rel, _REL.c.target_id, _REL.c.context)
        .where(_REL.c.session_id == sid))]
    actions = [{"verb": v, "target_id": t, "context": c} for v, t, c in db.run(
        con, select(_ACT.c.verb, _ACT.c.target_id, _ACT.c.context).where(_ACT.c.session_id == sid))]
    links = [{"url": u, "title": t} for u, t in db.run(
        con, select(_LNK.c.url, _LNK.c.title).where(_LNK.c.session_id == sid))]
    worked_on = [w[0] for w in db.run(con, select(_WO.c.item).where(_WO.c.session_id == sid))]
    data_sources = [d[0] for d in db.run(con, select(_DS.c.name).where(_DS.c.session_id == sid))]
    extracted = os.path.exists(csa._out(sid))
    one = ingest.load_one(sid) if extracted else None                # parses only this one .jsonl (cheap)
    pipeline = {"extracted": extracted, "stale": bool(one and csa._stale(one)), "indexed": True, "entities": len(entities)}
    metrics = None
    if gql.may_see_metrics(principal or {}, session["user"], auth.current_mode()):
        metrics = {"cost_usd": None, "tool_calls": None, "wasted": None, "wall_s": None, "idle_s": None}
        mrow = db.run(con, select(improve.known_cost(_SM), _SM.c.tool_calls, _SM.c.dup_calls, _SM.c.reread_calls,
                                  _SM.c.tool_errors, _SM.c.wall_s, _SM.c.idle_s)
                      .where(_SM.c.session_id == sid)).fetchone()
        if mrow:
            cost, calls, dup, reread, errs, wall, idle = mrow
            waste = [x for x in (dup, reread, errs) if x is not None]
            metrics = {"cost_usd": cost, "tool_calls": calls, "wasted": sum(waste) if waste else None,
                       "wall_s": wall, "idle_s": idle}
    return {"session": session, "pipeline": pipeline, "worked_on": worked_on,
            "hidden_context": hc, "entities": entities, "relations": relations, "actions": actions,
            "links": links, "data_sources": data_sources, "metrics": metrics}


def _disk_entries():
    """[(session_id, path, mtime)] for every local session file — stat only, never parses."""
    out = []
    for f in glob.glob(os.path.join(ingest.ROOT, "**", "*.jsonl"), recursive=True):
        try:
            out.append((os.path.splitext(os.path.basename(f))[0], f, os.path.getmtime(f)))
        except OSError:                                              # rotated mid-scan -> skip
            continue
    return out


def _schema_heads():
    return {env: migrations.head(env) for env in ("control", "corpus", "telemetry")}


def corpus_stats(con, org_id=store.DEFAULT_ORG):
    """Pipeline snapshot: stage counts and totals, stat-only. The disk stages stat the server's own
    ~/.claude/projects, which is no tenant's data, so a hosted org gets none."""
    hosted = store.valid_org_id(org_id) != store.DEFAULT_ORG
    entries = [] if hosted else _disk_entries()
    extracted, stale_ids = 0, []
    for sid, _f, m in entries:
        o = csa._out(sid)
        try:
            if m > os.path.getmtime(o):
                stale_ids.append(sid)
            extracted += 1
        except OSError:                                              # no extraction file -> missing
            continue
    q1 = lambda stmt: db.run(con, stmt).fetchone()[0]
    stages = {"on_disk": len(entries), "ingested": len(entries), "extracted": extracted,
              "covered": extracted - len(stale_ids), "stale": len(stale_ids),
              "missing": len(entries) - extracted,
              "indexed": q1(select(func.count()).select_from(_S).where(_S.c.source == "session")),
              "memories": q1(select(func.count()).select_from(_S).where(_S.c.source == "memory"))}
    served = db.run(con, select(func.count(distinct(_SV.c.session)), func.count())
                    .select_from(_SV)).fetchone()
    feedback = {"served_sessions": served[0], "served_facts": served[1],
                "flags": q1(select(func.count()).select_from(_RV)),
                "judge_labels": q1(select(func.count()).select_from(_RV).where(_RV.c.flagger == "judge")),
                "judge_gold": q1(select(func.count(distinct(_SIG.c.ref)))
                                 .select_from(_SIG).where(_SIG.c.kind == "judge_gold"))}
    totals = {"hidden_context": q1(select(func.count()).select_from(_HC)),
              "entities": q1(select(func.count()).select_from(_E)),
              "relations": q1(select(func.count()).select_from(_REL)),
              "docs": con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]}
    crow = db.run(con, select(func.count(), func.ifnull(func.sum(_COSTS.c.cost_usd), 0),
                              func.ifnull(func.sum(_COSTS.c.input_tokens), 0),
                              func.ifnull(func.sum(_COSTS.c.output_tokens), 0))
                  .select_from(_COSTS)).fetchone()
    costs = {"calls": crow[0], "usd": round(crow[1], 4), "in_tokens": crow[2], "out_tokens": crow[3],
             "usd_24h": round(db.run(con, select(func.ifnull(func.sum(_COSTS.c.cost_usd), 0))
                              .where(_COSTS.c.at >= func.datetime("now", "-1 day"))).fetchone()[0], 4),
             "by_step": {k: round(v, 4) for k, v in
                         db.run(con, select(_COSTS.c.step, func.ifnull(func.sum(_COSTS.c.cost_usd), 0))
                                .group_by(_COSTS.c.step))}}
    return {"stages": stages, "totals": totals, "feedback": feedback, "costs": costs,
            "stale_ids": stale_ids}


HC_TYPES = ("decision", "gotcha", "constraint", "todo", "preference")


def _signal_cols(h):
    """`used` (distinct sessions with a usage signal), `dominant` and `judge_gold` over the caller's
    `hidden_context` alias. FACT_ORDERS references the labels by name."""
    g = _SIG.alias("g")
    used = (select(func.count(distinct(g.c.session_id))).select_from(g)
            .where(g.c.ref == h.c.ref, g.c.kind.in_(("recalled_in_session", "acted_on")))
            .scalar_subquery().label("used"))
    dominant = exists(select(literal(1)).select_from(g)
                      .where(g.c.ref == h.c.ref, g.c.kind == "dominant_entity")).label("dominant")
    judge_gold = exists(select(literal(1)).select_from(g)
                        .where(g.c.ref == h.c.ref, g.c.kind == "judge_gold")).label("judge_gold")
    return used, dominant, judge_gold


FACT_ORDERS = {"use": "used DESC, corroboration DESC, s.started_at DESC",                     # no-query browse
               "leverage": "(used > 0) DESC, dominant DESC, corroboration DESC, s.started_at DESC",  # review 'today'
               # with a query: bm25 ascending; NULL = matched by ref prefix, sorts last
               "relevance": "(rk IS NULL), rk ASC, used DESC, corroboration DESC",
               "recent": "s.started_at DESC, used DESC"}


RULE_SCAN = 500                  # newest measured sessions a rule is computed over: 8.8ms median at 4k sessions


def rule_facts(con, owner="", project="", principal=None):
    """improve.project_lessons over the settled sessions' metrics, in the `facts` row shape with type
    "rule". Unscoped it names nobody, so everyone sees it; an owner scope reads one person's numbers and
    is refused where gql.may_see_metrics would refuse them."""
    if owner and not gql.may_see_metrics(principal or {}, owner, auth.current_mode()):
        return []
    settle_min = tenant.config(con).settle_min
    m, s = _SM.alias("m"), _S.alias("s")
    conds = [s.c.source == "session", s.c.ended_at.isnot(None),
             func.datetime(s.c.ended_at) <= func.datetime("now", f"-{settle_min} minutes")]
    if owner:
        conds.append(s.c.user == owner)
    if project:
        conds.append(s.c.project.like(f"%{project}%"))
    reviewed = _dicts(con, select(m.c.session_id, s.c.project, m.c.model, m.c.tool_calls, m.c.tool_errors,
                                  m.c.dup_calls, m.c.reread_calls, m.c.top_read, m.c.top_read_n, m.c.in_tokens,
                                  m.c.out_tokens, m.c.cache_read_tokens, m.c.cache_write_tokens, improve.known_cost(m),
                                  m.c.wall_s, m.c.idle_s, m.c.max_gap_s, s.c.n_user)
                      .select_from(m.join(s, s.c.session_id == m.c.session_id)).where(*conds)
                      .order_by(s.c.ended_at.desc()).limit(RULE_SCAN))
    for d in reviewed:
        d["findings"] = improve.findings(d)
    out = []
    for proj in improve.project_lessons(reviewed):
        for l in proj["lessons"]:
            out.append({"ref": f"rule:{proj['project']}:{l['rule']}", "type": "rule",
                        "claim": f"{l['rule'].replace('-', ' ').capitalize()}. {l['do']}",
                        "evidence": f"{l['sessions']} of {l['reviewed']} sessions", "project": proj["project"],
                        "session_id": "", "user": None, "started_at": None, "label": None, "flagger": None,
                        "corroboration": l["sessions"], "used": 0, "dominant": 0, "judge_gold": 0})
    return out


def _coverage(con, q, searched=None):
    """Which of the query's words the corpus has never seen, which are rare enough to be doing the work,
    and which never reached retrieval: the honest-miss signal, since `_fts_query` ORs the terms and drops
    any that match nothing. `searched` is the part of `q` retrieval ran on, when not all of it."""
    q = q or ""
    terms = search.query_terms(q if searched is None else searched)
    dfs = search.term_df(con, terms)
    total = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0] or 1
    cap = max(3, int(0.06 * total))                      # same distinctiveness cut as search.distinctive_terms
    return {"missing": sorted(t for t, df in dfs.items() if not df),
            "distinctive": sorted(t for t, df in dfs.items() if 0 < df <= cap),
            "terms": len(terms),
            "dismissed": sorted({w.lower() for w in re.findall(r"[A-Za-z0-9_.\-]+", q)} - terms)}


def facts(con, q="", ftype="", label="", owner="", project="", offset=0, limit=50, order="use", principal=None,
          served_only=False, scope_named=False):
    """Team knowledge search: dedup'd (by ref) facts ranked by real usage. q -> FTS over the docs index.
    `scope_named`: without an `owner`, a q naming one teammate (route.named_scope) searches theirs on its
    other words, newest first when none is left, and everyone's when theirs match nothing. `served_only`
    keeps to what recall may serve (search.servable). `rules` is non-empty only on the first unsearched
    page and is counted apart from `total`."""
    if scope_named and q and not owner:
        named, topic = route.named_scope(con, q)
        if named:
            scoped = facts(con, topic, ftype, label, named, project, offset, limit,
                           "recent" if not topic and order == "use" else order, principal, served_only)
            if scoped["total"] or offset:
                return scoped
    h, s, rv = _HC.alias("h"), _S.alias("s"), _RV.alias("rv")
    conds = [s.c.source == "session"]
    if ftype:
        conds.append(h.c.type == ftype)
    if owner:
        conds.append(s.c.user == owner)                  # exact: `jules` must not match julesrenner@
    if project:
        conds.append(s.c.project.like(f"%{project}%"))
    if label == "unlabeled":
        conds.append(rv.c.status.is_(None))
    elif label:
        conds.append(rv.c.status == label)
    base = h.join(s, s.c.session_id == h.c.session_id).outerjoin(rv, and_(rv.c.kind == "hc",
                                                                          rv.c.ref == h.c.ref))
    rk = literal_column("NULL")
    if q:                                                # FTS docs -> doc_id 'hc'||rowid -> ref set (see build_fts)
        qs = q.strip()
        fq = search._fts_query(qs)
        # Joined, not just filtered, so bm25 reaches ORDER BY. Raw text(): `docs` is FTS5, and bm25()
        # must live in the same query as its MATCH.
        f = (text("SELECT doc_id, bm25(docs) rk FROM docs WHERE docs MATCH :fq AND kind='hidden_context'")
             .bindparams(fq=fq).columns(column("doc_id"), column("rk")).subquery("f"))
        base = base.outerjoin(f, f.c.doc_id == literal("hc").concat(literal_column("h.rowid")))
        rk = func.min(literal_column("f.rk"))
        clause = [h.c.ref.in_(text(
            "SELECT h2.ref FROM hidden_context h2 WHERE 'hc'||h2.rowid IN "
            "(SELECT doc_id FROM docs WHERE docs MATCH :fq2 AND kind='hidden_context')").bindparams(fq2=fq))]
        if re.fullmatch(r"[0-9a-f]{4,}", qs):            # a hex-looking query ALSO matches a fact id by prefix
            clause.append(h.c.ref.like(qs + "%"))
        conds.append(or_(*clause))
        if order == "use":                               # a caller that did not ask for an order gets relevance
            order = "relevance"
    f2 = models.Fact.__table__.alias("f2")               # schema-v2: 1:1 by ref (LEFT: mid-backfill serves unfiltered)
    base = base.outerjoin(f2, f2.c.ref == h.c.ref)
    if served_only:
        conds.append(search.servable(rv, f2))
    coverage = _coverage(con, q)
    total = db.run(con, select(func.count(distinct(h.c.ref)))
                   .select_from(base).where(*conds)).fetchone()[0]
    used, dominant, judge_gold = _signal_cols(h)
    cc = models.ClaimCluster.__table__.alias("cc")
    fe = models.FactEdge.__table__.alias("fe")
    superseder = (select(fe.c.src_ref).where(fe.c.dst_ref == h.c.ref, fe.c.rel == "supersedes")
                  .order_by(fe.c.at.desc(), fe.c.src_ref).limit(1))
    superseded_at = (select(fe.c.at).where(fe.c.dst_ref == h.c.ref, fe.c.rel == "supersedes")
                     .order_by(fe.c.at.desc(), fe.c.src_ref).limit(1))
    rows = db.run(con, select(                           # MIN(h.rowid): sqlite takes bare columns from the min row
        func.min(literal_column("h.rowid")), h.c.ref, h.c.type, h.c.claim, h.c.evidence,
        s.c.project, h.c.session_id, s.c.user, s.c.started_at, rv.c.status, rv.c.flagger,
        func.count(distinct(h.c.session_id)).label("corroboration"), used, dominant, judge_gold,
        rk.label("rk"),
        # `author` is the first asserter; `user` owns the min-rowid mention's session
        f2.c.author, f2.c.domain, f2.c.created_at, f2.c.valid_at, f2.c.invalid_at, f2.c.state,
        select(cc.c.cluster_id).where(cc.c.ref == h.c.ref).scalar_subquery().label("cluster_id"),
        superseder.scalar_subquery().label("superseded_by"),
        superseded_at.scalar_subquery().label("superseded_at"))
        .select_from(base).where(*conds).group_by(h.c.ref)
        .order_by(text(FACT_ORDERS.get(order, FACT_ORDERS["use"])))
        .limit(limit).offset(offset)).fetchall()
    rules = rule_facts(con, owner, project, principal) \
        if offset == 0 and not q and not label and ftype in ("", "rule") else []
    return {"total": total, "offset": offset, "limit": limit, "coverage": coverage, "rules": rules,
            "rows": [{"ref": ref, "type": ty, "claim": c, "evidence": ev, "project": pj, "session_id": sid,
                      "user": u, "started_at": st, "label": lb, "flagger": fg, "corroboration": co, "used": us,
                      "dominant": int(bool(dm)), "judge_gold": int(bool(jg)),
                      "author": au, "domain": dmn, "created_at": cr, "valid_at": va, "invalid_at": iv,
                      # 'unverifiable' (no registry covers it) is served as the plain no-verdict state
                      "state": "extracted" if stt == "unverifiable" else stt,
                      "cluster_id": cl, "superseded_by": sb, "superseded_at": sa}
                     for _rid, ref, ty, c, ev, pj, sid, u, st, lb, fg, co, us, dm, jg, _rk,
                     au, dmn, cr, va, iv, stt, cl, sb, sa in rows]}


def graph(con, owner="", days=30, limit=60, principal=None, facts_budget=600):
    """Sessions active in the last `days` days, the facts they produced and the recalls between them.
    A fact's home is the EARLIEST window session that mentions it, so a claim repeated for months hangs
    off where it started. `recalled` counts later sessions outside this window and scope too: a scoped
    view must never claim a fact was never reused. `window.total` is the match count before the caps."""
    days = pagination.clamp_limit(days, default=30, maximum=3650)
    limit = pagination.clamp_limit(limit, default=60, maximum=120)
    s, h, sv = _S.alias("s"), _HC.alias("h"), _SV.alias("sv")
    active = func.coalesce(s.c.ended_at, s.c.started_at)
    # a session with no timestamp stays in EVERY window rather than vanishing from one called All time
    conds = [s.c.source == "session", or_(active.is_(None),
                                          func.datetime(active) >= func.datetime("now", f"-{days} days"))]
    if owner:
        conds.append(s.c.user == owner)
    total = db.run(con, select(func.count()).select_from(s).where(*conds)).fetchone()[0]
    n_facts = (select(func.count(distinct(h.c.ref))).select_from(h)
               .where(h.c.session_id == s.c.session_id).scalar_subquery())
    srows = db.run(con, select(s.c.session_id, s.c.summary, s.c.project, s.c.user, s.c.started_at, n_facts)
                   .where(*conds).order_by(active.desc(), s.c.session_id.desc()).limit(limit)).fetchall()
    sessions, spent = [], 0
    for sid, summ, proj, user, started, nf in srows:
        if sessions and spent + (nf or 0) > facts_budget:
            break
        spent += nf or 0
        sessions.append({"session_id": sid, "title": summ or sid, "project": proj or "", "user": user or "",
                         "started_at": started or "", "n_facts": nf or 0})
    ids = [x["session_id"] for x in sessions]
    facts, home, recalls = [], {}, []
    if ids:
        for sid, ref, ty, claim, ev in db.run(con, select(h.c.session_id, h.c.ref, h.c.type, h.c.claim, h.c.evidence)
                                              .select_from(h.join(s, s.c.session_id == h.c.session_id))
                                              .where(h.c.session_id.in_(ids))
                                              .order_by(active.asc(), s.c.session_id.asc(), literal_column("h.rowid"))):
            if ref in home:
                continue
            home[ref] = sid
            facts.append({"ref": ref, "type": ty, "claim": claim, "evidence": ev, "session_id": sid})
        window_refs = select(h.c.ref).where(h.c.session_id.in_(ids))
        recalled = {}
        for ref, sess in db.run(con, select(sv.c.ref, sv.c.session).where(sv.c.ref.in_(window_refs))
                                .group_by(sv.c.ref, sv.c.session)):
            if home.get(ref) not in (None, sess):
                recalled[ref] = recalled.get(ref, 0) + 1
        for f in facts:
            f["recalled"] = recalled.get(f["ref"], 0)
        recalls = [{"ref": ref, "session_id": sess, "at": at}
                   for ref, sess, at in db.run(con, select(sv.c.ref, sv.c.session, func.min(sv.c.asked_at))
                                               .where(sv.c.session.in_(ids), sv.c.ref.in_(window_refs))
                                               .group_by(sv.c.ref, sv.c.session))
                   if home.get(ref) not in (None, sess)]
        anchor = {}
        for x in sessions:                                    # newest first: the first seen per project wins
            anchor.setdefault(x["project"], x["session_id"])
        for r in rule_facts(con, owner, "", principal):
            if r["project"] in anchor:
                facts.append({"ref": r["ref"], "type": "rule", "claim": r["claim"], "evidence": r["evidence"],
                              "session_id": anchor[r["project"]], "recalled": 0})
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    return {"window": {"days": days, "limit": limit, "facts_budget": facts_budget, "since": since, "total": total},
            "sessions": sessions, "facts": facts, "recalls": recalls}


def questions(con):
    """HITL review queue — bucket facts by the decision they need:
    uncertain_extraction = extractor was unsure (confidence != 'high') and no human type yet -> confirm/set TYPE;
    uncertain_labels     = extraction settled but no quality flag yet -> set gold/wrong/unimportant;
    certain              = both decided. A fact flows UE -> UL -> certain. Machines label first (the judge:
    flagger='judge' reviews + judge_gold signals shown as suggestions); humans arbitrate and override."""
    h, s, rv, ht = _HC.alias("h"), _S.alias("s"), _RV.alias("rv"), _HT.alias("ht")
    _u, _d, jg = _signal_cols(h)
    rows = db.run(con, select(h.c.ref, h.c.type, h.c.scope, h.c.claim, h.c.evidence, h.c.confidence,
                              s.c.project, h.c.session_id, rv.c.status, rv.c.flagger, ht.c.type, jg)
                  .select_from(h.join(s, s.c.session_id == h.c.session_id)
                               .outerjoin(rv, and_(rv.c.kind == "hc", rv.c.ref == h.c.ref))
                               .outerjoin(ht, ht.c.ref == h.c.ref))
                  .where(s.c.source == "session").group_by(h.c.ref)
                  .order_by(h.c.confidence.is_(None), h.c.confidence, h.c.type)).fetchall()
    ue, ul, cert = [], [], []
    for ref, ty, scope, claim, evidence, conf, project, sid, label, flagger, confirmed, jg in rows:
        f = {"ref": ref, "type": ty, "scope": scope, "claim": claim, "evidence": evidence, "confidence": conf,
             "project": project, "session_id": sid, "label": label, "flagger": flagger,
             "judge_gold": int(bool(jg)), "type_confirmed": confirmed is not None}
        if not (conf == "high" or confirmed is not None):        # null/low confidence + not human-confirmed
            ue.append(f)
        elif not label:
            ul.append(f)
        else:
            cert.append(f)
    sv, r = _SV.alias("sv"), _RV.alias("r")
    q1 = lambda stmt: db.run(con, stmt).fetchone()[0]
    receipts = {                                         # why-review-matters counters for the UI
        "your_labels": q1(                               # HUMAN labels only — judge rows have their own counter
            select(func.count()).select_from(_RV)
            .where(_RV.c.status.in_(("gold", "wrong", "unimportant")),
                   or_(_RV.c.flagger.is_(None), _RV.c.flagger != "judge"))),
        "labeled_served": q1(
            select(func.count()).select_from(sv)
            .where(exists(select(literal(1)).select_from(r)
                          .where(r.c.kind == "hc", r.c.ref == sv.c.ref,
                                 r.c.status.in_(("gold", "wrong", "unimportant")),
                                 or_(r.c.flagger.is_(None), r.c.flagger != "judge"))))),
        "used_this_corpus": q1(
            select(func.count(distinct(_SIG.c.ref))).select_from(_SIG)
            .where(_SIG.c.kind.in_(("recalled_in_session", "acted_on")))),
        "judge_labels": q1(                              # machine labels awaiting/holding until a human overrides
            select(func.count()).select_from(_RV).where(_RV.c.flagger == "judge")),
        "judge_gold": q1(
            select(func.count(distinct(_SIG.c.ref))).select_from(_SIG)
            .where(_SIG.c.kind == "judge_gold"))}
    return {"types": list(HC_TYPES), "labels": list(LABELS),
            "uncertain_extraction": ue, "uncertain_labels": ul, "certain": cert,
            "counts": {"uncertain_extraction": len(ue), "uncertain_labels": len(ul), "certain": len(cert)},
            "today": facts(con, label="unlabeled", limit=5, order="leverage")["rows"],  # top-5 leverage-first
            "receipts": receipts}


def team(con, agents=frozenset()):
    """Per-owner rollup: whose sessions the corpus holds and how much came from each. `agent` marks an
    owner in `agents` (orgs.agent_emails)."""
    s, h, s2, m, s3 = _S.alias("s"), _HC.alias("h"), _S.alias("s2"), _MEN.alias("m"), _S.alias("s3")
    n = func.count().label("n")
    owner = func.ifnull(s.c.user, "")               # an unattributed session is "", never null
    fc = (select(func.count()).select_from(h.join(s2, s2.c.session_id == h.c.session_id))
          .where(func.ifnull(s2.c.user, "") == owner).scalar_subquery().label("fc"))
    ec = (select(func.count(distinct(m.c.entity_id)))
          .select_from(m.join(s3, s3.c.session_id == m.c.session_id))
          .where(func.ifnull(s3.c.user, "") == owner).scalar_subquery().label("ec"))
    rows = db.run(con, select(owner, n, func.min(s.c.started_at), func.max(s.c.started_at), fc, ec)
                  .where(s.c.source == "session").group_by(owner).order_by(n.desc())).fetchall()
    return [{"user": u, "sessions": n, "first_seen": a, "last_active": b, "facts": fc, "entities": ec,
             "agent": u.lower() in agents} for u, n, a, b, fc, ec in rows]


def _canonical_map():
    """Every alias id with the END of its merge chain, as a CTE (alias_id, canonical_id): merges
    chain (a->b->c) and the live drain writes un-walked ids, so a one-hop join would split one
    entity into two rows. Bounded depth keeps a cycle finite; a cycle maps to nothing and reads as
    its own id."""
    al = models.Alias.__table__
    chain = (select(al.c.alias_id, al.c.canonical_id, literal(1).label("depth"))
             .cte("chain", recursive=True))
    nxt = al.alias("nxt")
    chain = chain.union_all(select(chain.c.alias_id, nxt.c.canonical_id, chain.c.depth + 1)
                            .where(nxt.c.alias_id == chain.c.canonical_id, chain.c.depth < 16))
    end = al.alias("chain_end")
    return (select(chain.c.alias_id, chain.c.canonical_id)
            .where(~exists(select(literal(1)).select_from(end).where(end.c.alias_id == chain.c.canonical_id)))
            .cte("canon"))


def _fold(s, q):
    """The scalar `s` lowered, for a case-insensitive substring match on `q` (ASCII-only LIKE folding
    is SQLite's default; lower() on both sides covers the rest)."""
    return func.lower(s).contains(q.lower(), autoescape=True)


ENTITY_SORTS = ("sessions", "facts", "last_seen", "name")


def entities_page(con, q="", etype="", owner="", served_only=False, sort="sessions", direction="desc",
                  offset=0, limit=50):
    """The entity browser: one row per CANONICAL entity with live counts. `facts` counts the facts
    extracted from the sessions that mention it (not facts about it). `q` matches names, ids and
    merged-away alias ids so a nickname finds its canonical. `served` is the noise gate's verdict
    (1 when the tenant's floor is off)."""
    canon = _canonical_map()
    m, s, h, e = _MEN.alias("m"), _S.alias("s"), _HC.alias("h"), _E.alias("e")
    scope = [s.c.source == "session"]
    if owner:
        scope.append(s.c.user == owner)
    sm = (select(func.ifnull(canon.c.canonical_id, m.c.entity_id).label("eid"),
                 m.c.session_id.label("session_id"),
                 func.ifnull(s.c.ended_at, s.c.started_at).label("at"))
          .select_from(m.outerjoin(canon, canon.c.alias_id == m.c.entity_id)
                       .join(s, s.c.session_id == m.c.session_id))
          .where(*scope).subquery("sm"))
    per = (select(sm.c.eid, func.count().label("mentions"),
                  func.count(distinct(sm.c.session_id)).label("sessions"),
                  func.max(sm.c.at).label("last_seen"))
           .group_by(sm.c.eid).subquery("per"))
    sm2 = sm.alias("sm2")
    fx = (select(sm2.c.eid, func.count(distinct(h.c.ref)).label("facts"))
          .select_from(sm2.join(h, h.c.session_id == sm2.c.session_id))
          .group_by(sm2.c.eid).subquery("fx"))
    cfg = tenant.config(con)
    if cfg.entity_floor:
        frm, served = entity_gate.gate_parts(cfg, e)
    else:
        frm, served = e, literal(1)
    base = per.outerjoin(frm, e.c.id == per.c.eid).outerjoin(fx, fx.c.eid == per.c.eid)
    name = func.ifnull(e.c.name, per.c.eid)
    conds = []
    if etype:
        conds.append(e.c.type == etype)
    if served_only:
        conds.append(served)
    if q:
        e2, al2, c2 = _E.alias("e2"), models.Alias.__table__.alias("al2"), canon.alias("c2")
        hit = (select(e2.c.id.label("id")).where(or_(_fold(e2.c.name, q), _fold(e2.c.id, q)))
               .union(select(al2.c.alias_id.label("id")).where(_fold(al2.c.alias_id, q))).subquery("hit"))
        rolled = (select(func.ifnull(c2.c.canonical_id, hit.c.id))
                  .select_from(hit.outerjoin(c2, c2.c.alias_id == hit.c.id)))
        conds.append(or_(_fold(name, q), per.c.eid.in_(rolled)))
    total = db.run(con, select(func.count()).select_from(base).where(*conds)).fetchone()[0]
    facts_n = func.ifnull(fx.c.facts, 0)
    key = {"sessions": per.c.sessions, "facts": facts_n, "last_seen": per.c.last_seen, "name": func.lower(name)}[
        sort if sort in ENTITY_SORTS else "sessions"]
    order = key.asc() if direction == "asc" else key.desc()
    rows = db.run(con, select(per.c.eid, name, e.c.type, per.c.mentions, per.c.sessions, facts_n,
                              per.c.last_seen, served)
                  .select_from(base).where(*conds)
                  .order_by(order, func.lower(name).asc()).limit(limit).offset(offset)).fetchall()
    return {"total": total, "offset": offset, "limit": limit,
            "rows": [{"id": i, "name": n, "type": t or "", "mentions": mn, "sessions": sn, "facts": fc,
                      "last_seen": ls, "served": int(bool(sv))}
                     for i, n, t, mn, sn, fc, ls, sv in rows]}


def entity_detail(con, eid):
    """One canonical entity: its row, the aliases merged into it, the sessions that mention it and the
    facts those sessions produced. None when nothing in the corpus knows the id."""
    canonical = store.canon(con, eid)
    canon = _canonical_map()
    m, s, h, e, e2 = _MEN.alias("m"), _S.alias("s"), _HC.alias("h"), _E.alias("e"), _E.alias("e2")
    row = db.run(con, select(e.c.id, e.c.type, e.c.name, e.c.attributes).where(e.c.id == canonical)).fetchone()
    aliases = [{"id": a, "name": n} for a, n in db.run(
        con, select(canon.c.alias_id, e2.c.name).select_from(canon.outerjoin(e2, e2.c.id == canon.c.alias_id))
        .where(canon.c.canonical_id == canonical).order_by(canon.c.alias_id))]
    hc_n = (select(func.count(distinct(h.c.ref))).where(h.c.session_id == s.c.session_id)
            .scalar_subquery())
    sessions = [{"session_id": sid, "project": pj, "user": u, "started_at": st, "summary": sm, "facts": n}
                for sid, pj, u, st, sm, n in db.run(
                    con, select(s.c.session_id, s.c.project, s.c.user, s.c.started_at, s.c.summary, hc_n)
                    .select_from(m.outerjoin(canon, canon.c.alias_id == m.c.entity_id)
                                 .join(s, s.c.session_id == m.c.session_id))
                    .where(func.ifnull(canon.c.canonical_id, m.c.entity_id) == canonical,
                           s.c.source == "session")
                    .group_by(s.c.session_id).order_by(s.c.started_at.desc()))]
    if row is None and not sessions:
        return None
    sids = [x["session_id"] for x in sessions]
    rv, h2 = _RV.alias("rv"), _HC.alias("h2")
    used, dominant, judge_gold = _signal_cols(h)
    corroboration = select(func.count(distinct(h2.c.session_id))).where(h2.c.ref == h.c.ref).scalar_subquery()
    facts = [{"ref": ref, "type": ty, "claim": c, "evidence": ev, "project": pj, "session_id": sid,
              "user": u, "started_at": st, "label": lb, "flagger": fg, "corroboration": co, "used": us,
              "dominant": int(bool(dm)), "judge_gold": int(bool(jg))}
             for _rid, ref, ty, c, ev, pj, sid, u, st, lb, fg, co, us, dm, jg in db.run(
                 con, select(func.min(literal_column("h.rowid")), h.c.ref, h.c.type, h.c.claim, h.c.evidence,
                             s.c.project, h.c.session_id, s.c.user, s.c.started_at, rv.c.status, rv.c.flagger,
                             corroboration, used, dominant, judge_gold)
                 .select_from(h.join(s, s.c.session_id == h.c.session_id)
                              .outerjoin(rv, and_(rv.c.kind == "hc", rv.c.ref == h.c.ref)))
                 .where(h.c.session_id.in_(sids)).group_by(h.c.ref)
                 .order_by(text("used DESC"), s.c.started_at.desc()).limit(200))] if sids else []
    entity = {"id": canonical, "type": row[1] or "", "name": row[2] or canonical, "attributes": row[3]} \
        if row else {"id": canonical, "type": "", "name": canonical, "attributes": None}
    entity["aliases"] = aliases
    return {"entity": entity, "sessions": sessions, "facts": facts}


def flags(con):
    """The feedback ledger: every human flag, newest first, joined to what it labeled."""
    r, s = _RV.alias("r"), _S.alias("s")
    h = (select(_HC.c.ref, func.min(_HC.c.claim).label("claim"), func.min(_HC.c.type).label("type"),
                func.min(_HC.c.session_id).label("session_id"))
         .group_by(_HC.c.ref)).subquery("h")
    rows = db.run(con, select(r.c.kind, r.c.ref, r.c.status, r.c.note, r.c.flagger,
                              h.c.claim, h.c.type, h.c.session_id, s.c.project, s.c.user)
                  .select_from(r.outerjoin(h, and_(r.c.kind.in_(("hc", "registry")),   # registry verdicts join their claim too
                                                   h.c.ref == r.c.ref))
                               .outerjoin(s, s.c.session_id == h.c.session_id))
                  .order_by(literal_column("r.rowid").desc())).fetchall()
    out = [{"kind": k, "ref": ref, "label": st, "note": n, "flagger": fl, "claim": c, "type": ty,
            "session_id": sid, "project": p, "user": u}
           for k, ref, st, n, fl, c, ty, sid, p, u in rows]
    counts = {}
    for r in out:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    return {"total": len(out), "counts": counts, "rows": out}


def _fact_preview(md, cap=4):
    """'[type] short claim… (fact:xxxx)' lines for the hook's user-visible systemMessage, parsed from
    the grouped recall markdown, at most `cap`."""
    out, cur = [], ""
    for ln in md.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip().lower().rstrip("s")      # "Decisions" -> "decision"
        elif ln.startswith("- ") and len(out) < cap:
            body = ln[2:]
            m = re.search(r"id:([0-9a-f]{6,})", body)
            claim = re.sub(r"_\([^_]*\)_\s*$", "", body)              # strip trailing provenance _(…)_
            claim = re.sub(r"\s*…?\s*_\[truncated.*$", "…", claim).strip()   # strip the truncation note
            claim = (claim[:88].rstrip() + "…") if len(claim) > 90 else claim
            out.append(f"  • {f'[{cur}] ' if cur else ''}{claim}" + (f" (fact:{m.group(1)})" if m else ""))
    return out


def ask(con, query, session="", asked_by="", person="", k=6, project="", cwd="", org_id=store.DEFAULT_ORG,
        strict=True):
    """Recall over HTTP: the fast path, no LLM, since the prompt hook runs it on every prompt.

    `strict=True` is the hook's bar: the strict floor over gold facts only. `strict=False` is a
    deliberate ask (an agent, a skill, a human): when the strict floor abstains it falls back to the
    lenient floor, then a raw lexical match, then a miss naming the words the corpus has never seen; a
    locate ask skips the strict floor. `tier` says which answered.

    The same-project boost derives the project from `cwd` server-side; a client that sends only
    `project` (already basenamed) gets no boost rather than a wrong one. Every call writes one
    `queries` row, found or not."""
    with tracer.span("recall.hook", "CHAIN", db=store.org_db(org_id), root=True,
                     **{"session.id": session or None, "user.id": asked_by or None,
                        "csa.org_id": org_id, "csa.entry": "hook", "csa.strict": strict}) as sp:
        out = _hook_ask(con, query, session, asked_by, person, k, project, cwd, org_id, strict)
        sp.set(**{"csa.outcome": ("served" if out.get("found") else "miss") if out.get("ok") else "error",
                  "csa.context_chars": len(out.get("context") or "") or None,
                  "csa.n_facts": out.get("n"),
                  "csa.coverage_missing": len((out.get("coverage") or {}).get("missing") or []) or None,
                  "output.value": str(out.get("system_message") or out.get("message")
                                      or out.get("error") or "")[:2000]})
        return out


def _hook_ask(con, query, session, asked_by, person, k, project, cwd, org_id, strict=True):
    t0 = time.perf_counter()
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": 'missing "query"'}
    try:
        k = max(1, min(int(k), 12))
    except Exception:
        k = 6
    # redacted at the server floor, so the log never stores a query weaker than stored transcripts
    q_mode, q_deny, q_allow = tenant.redaction_policy(con)
    safe_query, _ = redact.redact(query[:1000], q_mode, q_deny, q_allow)
    tracer.set_attr(**{"input.value": safe_query})
    rt = route.classify(con, safe_query, asked_by=asked_by or None, person=person or None)
    safe_cwd = str(cwd or "").strip()[:4096]           # 4096 = PATH_MAX on linux; longer isn't a path
    scope = (normalize_project(safe_cwd)[:200] if safe_cwd
             else str(project or "").strip()[:200] or None)
    metrics = {}
    # the strict floor only answers with a fact page, and a locate ask wants a session list
    locate = not strict and route.is_locate(safe_query)
    # strict=True IS the unbidden hook, the only caller that carries the ambient bar
    md = "" if locate else recall_app.recall(
        safe_query, k=k, fill=False, session=session or None, person=rt.person or None,
        asked_by=asked_by or None, strict=True, project=scope, org_id=org_id, metrics=metrics,
        ambient=strict)
    found = _found(md)
    tier = "strict" if found else "miss"
    if not strict and not found:
        tier, md, metrics, coverage = _fallback(con, safe_query, k, session or None, rt.person or None,
                                                asked_by or None, scope, org_id)
        found = tier != "miss"
    else:
        coverage = _coverage(con, safe_query, route.undated(safe_query))
    n = md.count("\n- ") if found else 0
    tracer.set_attr(**{"csa.tier": tier})
    try:                                                  # shadow log: never fail the request on a log error
        # result_ids carries the top hit's rerank score (a diagnostic, not comparable across queries)
        score = metrics.get("score")
        store.log_query(con, asked_by, session or "", "hook" if strict else "fallback", safe_query,
                        f"route={rt.scope}/{rt.shape} person={rt.person or '-'} proj={scope or '-'} "
                        f"cwd_present={int(bool(safe_cwd))} found={int(found)} tier={tier}", n,
                        int((time.perf_counter() - t0) * 1000),
                        result_ids=None if score is None else f"{score:.3f}")
    except Exception as e:
        logging.warning("hook query-log skipped: %s", str(e)[:80])
    if not found:
        return {"ok": True, "found": False, "tier": tier, "message": md, "coverage": coverage}
    max_chars = 7000
    if len(md) > max_chars:
        md = md[:max_chars].rstrip() + "\n\n_[truncated by CSA ambient memory]_"
    who = f" · {person}" if person else ""
    # The banner shows the top-ranked fact, not the first rendered line: render() groups by type size.
    top = metrics.get("top")
    if top:
        claim = re.sub(r"\s+", " ", top["claim"]).strip()
        if claim.lower().startswith(top["type"] + ":"):   # mirror render(): no "[decision] decision: …"
            claim = claim[len(top["type"]) + 1:].strip()
        claim = (claim[:88].rstrip() + "…") if len(claim) > 90 else claim
        day = f" · {top['day']}" if top.get("day") else ""
        prev = [f"  • [{top['type']}] {claim}{day}" + (f" (fact:{top['ref']})" if top.get("ref") else "")]
    else:
        prev = _fact_preview(md, cap=1)                   # non-strict callers: no metrics, fall back to parsing
    n = metrics.get("n") or n                             # md.count also counts list markers inside a fact
    more = max(0, n - len(prev))
    head = f"\U0001f9e0 CSA recalled {n} past-session fact{'' if n == 1 else 's'}{who}:"
    tail = [f"  … and {more} more (full text sent to Claude; ask it to `fact:<id>` for any one)"] if more else []
    sysmsg = "\n".join([head, *prev, *tail])
    return {"ok": True, "found": True, "tier": tier, "query": safe_query, "context": md,
            "n": n, "system_message": sysmsg, "coverage": coverage}


def _found(md):
    return md.startswith("# Recalled context for") and "\n## " in md


def _fallback(con, query, k, session, person, asked_by, scope, org_id):
    """After the strict floor abstained, or was skipped for a locate ask: the lenient floor, which
    answers a locate ask with a session list, then `lenient_tier`. Returns (tier, markdown, metrics,
    coverage)."""
    metrics = {}
    md = recall_app.recall(query, k=k, fill=False, session=session, person=person, asked_by=asked_by,
                           project=scope, org_id=org_id, metrics=metrics)
    tier, md, coverage = lenient_tier(con, query, md, metrics, k, person, scope)
    return tier, md, metrics, coverage


def lenient_tier(con, query, recalled, metrics, k, person, scope):
    """What answers a non-strict ask once the lenient floor returned `recalled` and filled `metrics`: its
    session list, its facts unless the never-seen words outnumber the matched ones, else `below_floor`.
    Returns (tier, markdown, coverage)."""
    lex = route.undated(route.topical(query) if route.is_locate(query) else query)
    if recalled.startswith("# Sessions matching"):
        return "locate", recalled, _coverage(con, query, lex)
    if _found(recalled):
        if metrics.get("digest") or not route.outweighed(*route.topic_words(con, lex)):
            return "lenient", recalled, _coverage(con, query, lex)
    return below_floor(con, query, recalled, k, person, scope)


def below_floor(con, query, recalled, k, person, scope):
    """What answers once the lenient floor missed with `recalled`: raw lexical facts when every topical
    word is known and no date filters the question (a word match is dateless), else the honest miss
    naming the unseen topic words. Returns (tier, markdown, coverage)."""
    lex = route.undated(route.topical(query) if route.is_locate(query) else query)   # no locator or date word reaches a lexical match
    coverage = _coverage(con, query, lex)
    owner = route.resolve_person(con, person) if person else ""
    if person and not owner:                              # unknown person: recall's roster message stands
        return "miss", recalled, coverage
    unseen = route.topic_words(con, lex)[0]
    rows = [] if unseen or any(route.date_window(query)) else \
        facts(con, q=lex, owner=owner, project=scope or "", limit=k, served_only=True)["rows"]
    if rows:
        return "facts", _lexical_page(query, rows), coverage
    return "miss", _honest_miss(query, owner, coverage, unseen), coverage


def _lexical_page(query, rows):
    groups = {}
    for r in rows:
        src = " \u00b7 ".join(x for x in (r["user"], (r["started_at"] or "")[:10], r["project"]) if x)
        groups.setdefault(r["type"], []).append(f"- {r['claim']}  _({src} \u00b7 id:{r['ref']})_")
    out = [f'# Recalled context for "{query}"',
           "_lexical matches only: no relevance floor was cleared. Read before relaying._"]
    for ty, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out += ["", f"## {recall_app._HEAD.get(ty, ty.title())}"] + items
    return "\n".join(out)


def _honest_miss(query, owner, coverage, unseen):
    who = f" in {owner}'s or the team's" if owner else " in past"
    out = [f'Nothing on "{query}"{who} sessions.']
    if unseen:
        out.append(f"Words the corpus has never seen: {', '.join(unseen)}.")
    if coverage["dismissed"]:
        out.append(f"Not searched: {', '.join(coverage['dismissed'])}.")
    return " ".join(out + ["Every retrieval tier ran; this is a real miss, not an outage."])


def hctype(con, ref, hc_type):
    """Human confirms/sets a fact's type from the Questions queue (moves it out of uncertain_extraction)."""
    if hc_type not in HC_TYPES:
        return {"ok": False, "error": f"unknown type {hc_type!r}; use {list(HC_TYPES)}"}
    if not db.run(con, select(literal(1)).select_from(_HC)
                  .where(_HC.c.ref == ref).limit(1)).fetchone():
        return {"ok": False, "error": f"no fact with id {ref!r}"}
    store.set_hc_type(con, ref, hc_type)
    store.build_fts(con)                                          # type change -> refresh docs.etype for rerank/recall
    return {"ok": True, "ref": ref, "type": hc_type}


def flag(con, ref, label, note="", flagger=""):
    if label not in LABELS:
        return {"ok": False, "error": f"unknown label {label!r}; use {list(LABELS)}"}
    if not db.run(con, select(literal(1)).select_from(_HC)
                  .where(_HC.c.ref == ref).limit(1)).fetchone():
        return {"ok": False, "error": f"no fact with id {ref!r}"}
    if label == "clear":                                  # parity with the MCP tool: unflag, back to neutral
        if not db.run(con, select(literal(1)).select_from(_RV)
                      .where(_RV.c.kind == "hc", _RV.c.ref == ref)).fetchone():
            return {"ok": False, "error": f"{ref} has no flag to clear"}
        store.clear_review(con, "hc", ref)                # judge rows leave a human_cleared marker (sticks)
        store.build_fts(con)
        return {"ok": True, "ref": ref, "label": None}
    store.set_review(con, "hc", ref, label, note=note or "ui-flag", flagger=flagger or "web-ui")
    store.build_fts(con)                                  # apply to what recall serves, now
    return {"ok": True, "ref": ref, "label": label}


# Each table forget() wipes, by name: a name without a model KeyErrors instead of surviving a forget.
# `served` is in the telemetry file, so a crash mid-forget can leave telemetry rows; the corpus delete
# is one transaction.
_FORGET_TABLES = {
    "sessions": _S, "worked_on": _WO, "hidden_context": _HC, "mentions": _MEN, "actions": _ACT,
    "relations": _REL, "links": _LNK, "data_sources": _DS,
    "harvest_actions": models.HarvestAction.__table__,
    "harvest_mentions": models.HarvestMention.__table__,
    "harvest_links": models.HarvestLink.__table__,
    "served": _SV, "signals": _SIG, "pr_links": _PL,
    "session_workspace": _SW, "session_metrics": _SM,
}


def _transcript(con, session_id, principal, org_id=None, limit=100, cursor=None):
    """(status, body) for GET /api/sessions/<id>/transcript, the retained redacted transcript.

    Unlike the rest of the corpus, readable only by its owner and a workspace admin. A miss and a
    refusal answer identically, so the route is no existence oracle for session ids."""
    nope = (404, {"ok": False, "error": "no transcript for that session — it may never have been "
                                        "retained, or its retention window has passed"})
    days = archive.retention_days(org_id)
    if days == 0:
        return (404, {"ok": False, "code": "archive_off", "error":
                      "this workspace does not retain transcripts (archive_days is 0)"})
    row = db.run(con, select(models.SessionRow.__table__.c.user)
                 .where(models.SessionRow.__table__.c.session_id == session_id)).fetchone()
    rec = archive.get(session_id, org_id=org_id, days=days)
    if not rec:
        return nope
    # the archived record's `user` covers a session archived at ingest but not yet drained
    owner = (row[0] if row else None) or rec.get("user") or ""
    if not gql.may_read_transcript(principal or {}, owner, auth.current_mode()):
        return nope
    raw = archive.blocks(rec)
    pg = pagination.page(raw, limit=limit, cursor=cursor)
    start = pagination.decode_cursor(cursor)
    return (200, {"ok": True, "session_id": session_id, "user": rec.get("user"),
                  "project": rec.get("project"), "started_at": rec.get("started_at"),
                  "ended_at": rec.get("ended_at"), "redaction_mode": rec.get("redaction_mode"),
                  "stored_at": rec.get("stored_at"), "retention_days": days,
                  "n_user": rec.get("n_user"), "n_asst": rec.get("n_asst"),
                  "items": [{**b, "index": start + i} for i, b in enumerate(pg["items"])],
                  "total_count": pg["total"], "has_more": pg["has_more"],
                  "next_cursor": pg["next_cursor"],
                  "turns": [b.get("text") or b.get("input") or "" for b in pg["items"]]})


def _forget_in_flight(session_id, requester, require_owner, admin, org_id, norm, nope):
    """forget() for a session shipped but not yet extracted: drops the queued copy and the retained
    transcript, under the same owner gate and uniform miss as the corpus path."""
    rec = archive.get(session_id, org_id=org_id) or {}
    if not rec:
        try:
            rec = queue.peek(session_id, org_id=org_id) or {}
        except Exception:                                 # unreadable queue file: treat as absent
            rec = {}
    if not rec:
        return dict(nope)
    owner = rec.get("user")
    if require_owner and not admin and (not norm(requester) or norm(requester) != norm(owner)):
        return dict(nope)
    if admin and norm(requester) != norm(owner):
        log.info("forget: workspace admin %s deleted %s's in-flight session %s", requester, owner, session_id)
    if not require_owner and requester and owner and norm(requester) != norm(owner):
        return {"ok": False, "error": f"session {session_id!r} is owned by {owner!r}, not {requester!r} — "
                                      f"you can only forget your own sessions"}
    archive.drop(session_id, org_id=org_id)
    queue.discard(session_id, org_id=org_id)              # also clears a quarantined (poison/) copy
    incremental.drop(session_id, org_id=org_id)
    log.info("forget: dropped in-flight session %s before extraction", session_id)
    return {"ok": True, "session_id": session_id, "deleted": True, "stage": "in-flight"}


def forget(con, session_id, requester=None, require_owner=False, org_id=None, admin=False):
    """Right-to-delete: remove one session and everything derived from it.

    require_owner=True (the HTTP route): `requester` must match the session's owner or shipper; a
    missing identity is denied. require_owner=False is the direct server-side call. admin=True is a
    workspace admin forgetting any session in their own org. The compare is case-insensitive."""
    row = db.run(con, select(_S.c.user, _S.c.shipped_by)
                 .where(_S.c.session_id == session_id)).fetchone()
    # "not yours" is byte-identical to "does not exist": no owner email, no existence oracle
    _NOPE = {"ok": False, "error": f"{session_id!r} isn't a session you can forget — you can only forget your "
                                   f"OWN sessions; ask the owner otherwise"}
    norm = lambda s: (s or "").strip().lower()
    if not row:                                           # not extracted yet: it may still be in flight
        return _forget_in_flight(session_id, requester, require_owner, admin, org_id, norm, _NOPE)
    owner = row[0]
    # a relay's session: the credited `user` and the authenticated shipper may both delete it
    allowed = {norm(owner)} | ({norm(row[1])} if row[1] else set())
    if require_owner and not admin and norm(requester) not in allowed:   # untrusted caller: missing/empty/mismatch -> deny
        return dict(_NOPE)
    if admin and norm(requester) not in allowed:
        log.info("forget: workspace admin %s deleted %s's session %s", requester, owner, session_id)
    if not require_owner and requester and owner and norm(requester) not in allowed:
        return {"ok": False, "error": f"session {session_id!r} is owned by {owner!r}, not {requester!r} — "
                                      f"you can only forget your own sessions"}
    # Snapshot the session's fact refs BEFORE the mention rows go — the facts recompute below needs to know
    # which facts this session was asserting (afterwards there is nothing left to ask).
    _hf = _HC.alias("hf")
    affected_refs = {r for (r,) in db.run(con, select(_hf.c.ref)
                     .where(_hf.c.session_id == session_id, _hf.c.ref.isnot(None)))}
    for t in ("sessions",) + store._PERSESSION + store._HARVEST_MARKERS + (
            "served", "signals", "pr_links", "session_workspace", "session_metrics"):
        tbl = _FORGET_TABLES[t]
        col = "session" if t == "served" else "session_id"
        db.run(con, delete(tbl).where(tbl.c[col] == session_id))
    # "everything derived" must include the global tables: entities nothing references anymore
    # (else a forgotten secret project's names stay searchable) and flags on refs no fact has.
    m, r, a = _MEN.alias("m"), _REL.alias("r"), _ACT.alias("a")
    db.run(con, delete(_E).where(
        ~exists(select(literal(1)).select_from(m).where(m.c.entity_id == _E.c.id)),
        ~exists(select(literal(1)).select_from(r).where(or_(r.c.source_id == _E.c.id,
                                                            r.c.target_id == _E.c.id))),
        ~exists(select(literal(1)).select_from(a).where(a.c.target_id == _E.c.id))))
    h = _HC.alias("h")
    db.run(con, delete(_RV).where(
        _RV.c.kind == "hc",
        ~exists(select(literal(1)).select_from(h).where(h.c.ref == _RV.c.ref))))
    # same transaction: facts re-derive from the remaining mentions (their dates may move forward), and
    # a fact with none left goes with its edges
    store.refresh_facts(con, refs=affected_refs, recompute=True)

    con.commit()
    store.write_clusters(con)      # a forgotten session must stop counting toward ⟳N corroboration
    store.build_fts(con)
    archive.drop(session_id, org_id=org_id)              # the retained transcript is not derived from the corpus
    incremental.drop(session_id, org_id=org_id)          # the cached claims carry their evidence quotes
    queue.discard(session_id, org_id=org_id)              # a queued re-ship would index the session again
    return {"ok": True, "session_id": session_id, "deleted": True}


_JUDGE_LOCK = threading.Lock()   # one judge at a time


def _autojudge(org_id):
    """The judge sweep over one org's corpus: a catch-up over never-labeled facts, then
    judge.etl_pass. Skipped when another judge is running."""
    if not _JUDGE_LOCK.acquire(blocking=False):
        return
    try:
        from analyzer import judge
        con = store.sqlite_db(store.org_db(org_id))
        with tracer.span("judge", "CHAIN", db=store.org_db(org_id), root=True,
                         **{"session.id": None, "csa.org_id": org_id}) as sp:
            n = judge.run(con)                           # catch-up: pending-only, never re-labels
            n.update(judge.etl_pass(con))
            sp.set(**{"csa.judge.n_reviewed": sum(n.values())})
        if any(n.values()):
            log.info("autojudge %s/etl-sweep: %s", org_id, {k: v for k, v in n.items() if v})
    except Exception as e:                               # judge failure never hurts the pipeline; facts stay queued
        log.warning("autojudge %s/etl-sweep failed: %s", org_id, str(e)[:200])
    finally:
        _JUDGE_LOCK.release()


def _autojudge_on(org_id):
    return tenant.get_config(store.org_db(org_id)).autojudge


def _spawn_boot_sweep():
    """The judge sweep over every org, serialized by _JUDGE_LOCK."""
    def run():
        for o in orgs.corpus_orgs():                     # just `default` when the control plane is unreadable
            if _autojudge_on(o):
                _autojudge(o)
    threading.Thread(target=run, daemon=True).start()


_DEMO = None     # process-lifetime cache: gold flags change rarely


def demo_query(con):
    """Onboarding demo topic, or {}: the entity name most often behind gold-flagged facts for which
    search returns at least 3 hits."""
    global _DEMO
    if _DEMO is None:
        picked = {}
        rv, h, m, e = _RV.alias("r"), _HC.alias("h"), _MEN.alias("m"), _E.alias("e")
        n = func.count().label("n")
        cands = [row[0] for row in db.run(con, select(e.c.name, n)
                 .select_from(rv.join(h, and_(h.c.ref == rv.c.ref, rv.c.kind == "hc",
                                              rv.c.status == "gold"))
                              .join(m, m.c.session_id == h.c.session_id)
                              .join(e, e.c.id == m.c.entity_id))
                 .where(e.c.type.in_(("service", "repo", "file", "release", "env")),
                        func.length(e.c.name) <= 30,
                        e.c.name.not_like("%/%"))       # a demo question must be speakable
                 .group_by(e.c.name).order_by(n.desc()).limit(5)).fetchall() if row[0]]
        for q in cands:
            hits = search.search(con, q, k=5)
            if len(hits) >= 3:
                picked = {"demo_query": q, "demo_facts": len(hits)}
                break
        _DEMO = picked
    return _DEMO


# The live pipeline view: in-memory on purpose, it is about now, and a restart losing it is fine.
_PIPELINE = {}   # (org_id, session_id) -> {session_id, org_id, stage, t0, updated, owner?, project?, finished?, ...}
_PIPE_KEEP = 20  # finished entries retained for the "recent" feed
_PIPE_LOCK = threading.Lock()


def _track(session_id, stage, org_id=store.DEFAULT_ORG, **kw):
    """Record a stage boundary inside ingest_session. Keyed by org too: two orgs can hold the same id.
    "queued" is not terminal; pipeline_status() finishes an entry once the queue has drained it."""
    key = (org_id, session_id)
    with _PIPE_LOCK:
        if stage == "received":                          # a re-ingest of a finished id starts a fresh run
            e = _PIPELINE[key] = {"session_id": session_id, "org_id": org_id, "t0": time.monotonic()}
        else:
            e = _PIPELINE.setdefault(key, {"session_id": session_id, "org_id": org_id, "t0": time.monotonic()})
        e.update(stage=stage, updated=time.time(), **{k: v for k, v in kw.items() if v is not None})
        if stage in ("done", "failed"):
            _finish(e)


def _finish(e):
    """Freeze a pipeline entry and evict the oldest finished ones past _PIPE_KEEP. Hold _PIPE_LOCK."""
    if "finished" not in e:
        e["finished"] = time.time()
        e["elapsed_s"] = round(time.monotonic() - e["t0"], 1)
    finished = sorted((k for k, v in _PIPELINE.items() if "finished" in v),
                      key=lambda k: _PIPELINE[k]["finished"])
    for k in finished[:-_PIPE_KEEP]:
        _PIPELINE.pop(k, None)


def extraction_status(org_id=None):
    """{"stalled", "reason"}: the etl's last batch failed for a systemic reason and sessions are waiting.
    One workspace with its real reason when `org_id` is given; otherwise any workspace, with a fixed
    sentence, because the anonymous health check must not echo provider error text. A model this image cannot
    run is stalled from boot, sessions waiting or not."""
    blocked = llm.blocked()
    if blocked:
        return {"stalled": True, "reason": blocked if org_id else "the configured model needs Claude Code, "
                "which this image does not include; an admin sees the fix on AI Models"}
    try:
        stalls = orgs.extraction_stalls()
        for oid, stall in stalls.items():
            if (org_id in (None, oid)) and queue.depth(oid)[0]:
                return {"stalled": True, "reason": stall["reason"] if org_id else
                        "the model provider is refusing requests; an admin sees why on the Pipeline page"}
    except Exception:
        log.exception("extraction status unavailable")
    return {"stalled": False, "reason": ""}


def pipeline_status(org_id=store.DEFAULT_ORG):
    """GET /api/pipeline payload for one org: what is in flight and the last few finished. Filtered by
    org here: the process-global dict is outside the per-org file boundary."""
    now = time.monotonic()
    inflight, recent = [], []
    with _PIPE_LOCK:
        mine = sorted((e for e in _PIPELINE.values() if e.get("org_id", store.DEFAULT_ORG) == org_id),
                      key=lambda e: e["t0"], reverse=True)                                     # newest first
        for e in mine:
            stage, finished = e["stage"], "finished" in e
            if not finished:
                # the ETL drains in another process: the queue directory is the shared state
                live = queue.stage(e["session_id"], org_id=e.get("org_id", store.DEFAULT_ORG))
                if live:
                    stage = live                          # queued -> extracting, as the drain claims it
                elif stage == "queued":                   # gone from the queue = drained
                    stage, finished = "done", True
                    _finish(e)
            item = {"session_id": e["session_id"], "stage": stage, "owner": e.get("owner"),
                    "project": e.get("project"), "elapsed_s": e.get("elapsed_s", round(now - e["t0"], 1))}
            if e.get("error"):
                item["error"] = e["error"]
            if e.get("action"):                          # skip / incremental / full — operator observability
                item["action"] = e["action"]
            if e.get("facts") is not None:
                item["facts"], item["entities"] = e["facts"], e.get("entities", 0)
            (recent if finished else inflight).append(item)
    return {"inflight": inflight, "recent": recent}


def ingest_session(session_id, content=None, owner=None, dry_run=False, redaction_mode=None,
                   org_id=store.DEFAULT_ORG, on_behalf_of=None):
    """/api/ingest: redact one session and queue it for the ETL. Touches no database, so a POST costs
    a file write; extraction is the ETL's job.

    `content` is the raw .jsonl a remote client read; without it the server reads `session_id` from
    its own disk. Storage is redacted at max(the client's declared `redaction_mode`, the workspace
    floor), never weaker (docs/redaction.md)."""
    try:                                                                # session_id becomes a filename: a '../'
        store.valid_session_id(session_id)                              # would escape the org dir
    except ValueError:
        return {"ok": False, "error": "invalid session_id", "code": "bad_request"}
    _track(session_id, "received", org_id=org_id, owner=owner)
    min_mode, sdeny, sallow = tenant.redaction_policy(db_path=store.org_db(org_id))  # queue path opens no con on purpose
    declared = (redaction_mode or "secrets").strip().lower()             # normalize: a raw client may send junk/casing
    if declared not in redact.LEVELS:                                    # unknown -> secrets (never trust it UP a level)
        declared = "secrets"
    effective = redact.max_mode(declared, min_mode)                      # prevent weaker-than-required policy
    sess = (ingest.load_content(session_id, content, mode=effective, deny=sdeny, allow=sallow)
            if content else ingest.load_one(session_id))                 # disk path is already gated at min_mode
    if not sess:
        _track(session_id, "failed", org_id=org_id, error="session not found")
        return {"ok": False, "error": "session not found"}
    if dry_run:                                                         # client smoke test: parse and validate only
        _PIPELINE.pop((org_id, session_id), None)
        return {"ok": True, "dry_run": True, "session_id": session_id}
    if ingest.is_self_session(sess):                                    # the miner's own `claude -p` run
        _track(session_id, "done", org_id=org_id, project=sess.get("project"))
        return {"ok": True, "skipped": "self-session", "session_id": session_id}
    if owner:
        sess["user"] = owner                                            # else store falls back to this machine's account
    if on_behalf_of:                                                    # a service token crediting someone else
        sess["on_behalf_of"] = on_behalf_of
    # the mode actually applied: the disk path was redacted by load_one at the server floor
    sess["redaction_mode"] = effective if content else min_mode
    queue.queue_pending(sess, org_id=org_id)
    # Archived here, where the redacted text exists (the queue file goes once extracted). Best-effort:
    # a full disk costs the archive, never the ingest.
    try:
        archive.put(sess, org_id=org_id)
    except Exception as e:
        log.warning("archive: storing %s failed (%s) - session still queued", session_id, str(e)[:120])
    _track(session_id, "queued", org_id=org_id, project=sess.get("project"))
    return {"ok": True, "queued": True, "session_id": session_id}      # ok=true -> client marks sent, stops retrying


_QINT_MAX = 2**31 - 1


def _qint(qs, key, default):
    """A non-negative integer query param, or `default` when absent or not a number. Clamped to what
    SQLite can bind."""
    try:
        return min(max(0, int(qs.get(key, [default])[0])), _QINT_MAX)
    except ValueError:
        return default


# --- usage events: the CSA web app's own use, read by Admin > Analytics -------------------------
_UE = models.UiEvent.__table__
TRACK_EVENTS = ("page_view", "search", "scope", "flag", "session_open", "entity_open")
_PAGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def _utc(secs):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


def track_event(con, user, body):
    """(status, body) for POST /api/track: one product-usage row in this org's telemetry file. The event
    is a closed set, `page` is a slug (required for a page view), `meta` is a flat object of at most
    eight scalars or absent, and the row credits the gate's identity, never a name in the body. Rows
    older than 200 days go with each write: the widest Analytics window is 182."""
    event = _text(body, "event")
    if event not in TRACK_EVENTS:
        return 400, {"ok": False, "error": "unknown event", "code": "bad_request"}
    page = body.get("page", "")
    if not isinstance(page, str) or (page and not _PAGE_RE.match(page)) or (event == "page_view" and not page):
        return 400, {"ok": False, "error": "page must be a slug", "code": "bad_request"}
    meta = body.get("meta")
    if meta is not None:
        if not isinstance(meta, dict) or len(meta) > 8 \
                or not all(isinstance(v, (str, int, float, bool)) for v in meta.values()):
            return 400, {"ok": False, "error": "meta must be a flat object of at most eight values", "code": "bad_request"}
        meta = json.dumps(meta, separators=(",", ":"))
        if len(meta) > 512:
            return 400, {"ok": False, "error": "meta too large", "code": "bad_request"}
    db.run(con, insert(_UE).values(at=_utc(time.time()), user=user or "", event=event, page=page, meta=meta))
    db.run(con, delete(_UE).where(_UE.c.at < _utc(time.time() - 200 * 86400)))
    con.commit()
    return 200, {"ok": True}


def _text(body, key):
    """A string field of a JSON body, stripped; anything else reads as ''."""
    v = body.get(key)
    return v.strip() if isinstance(v, str) else ""


class Handler(BaseHTTPRequestHandler):
    def _header(self, k, v):
        """send_header, refusing a CR or LF: http.server does not validate, and one in a value an
        attacker reached would write headers of their choosing. Every value this server sends goes
        through here."""
        v = str(v)
        if "\r" in v or "\n" in v:
            log.error("refusing header %s with a newline in its value", k)
            raise ValueError("header value may not contain CR or LF")
        self.send_header(k, v)

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        self.send_response(code)
        self._header("Content-Type", ctype)
        for k, v in headers:
            self._header(k, v)
        self.end_headers()                                # no Access-Control-Allow-Origin, on purpose
        self.wfile.write(data)

    def _send_file(self, code, data, ctype):
        """_send with Content-Length, for anything a shell pipes or executes: over HTTP/1.0 without it,
        curl exits 0 on a truncated file. No-store and Vary: Host, because /install is templated per
        request."""
        self.send_response(code)
        self._header("Content-Type", ctype)
        self._header("Content-Length", str(len(data)))
        self._header("X-Content-Type-Options", "nosniff")
        self._header("Cache-Control", "no-store")
        self._header("Content-Security-Policy", "frame-ancestors 'none'")   # /cli-auth and /join are never framed
        self._header("Vary", "Host")
        self.end_headers()
        self.wfile.write(data)

    def _base_url(self):
        """The public base URL baked into the installer and the sign-in links: CSA_PUBLIC_URL, else a
        loopback Host, else None. A client-controlled Host would be remote code execution: the
        installer fetches the binary and its checksum from this base."""
        configured = os.environ.get("CSA_PUBLIC_URL", "").strip().rstrip("/")
        if configured:
            return configured
        host = (self.headers.get("Host") or "").strip()
        if not re.match(r"^[A-Za-z0-9.\-]+(:[0-9]{1,5})?$", host):      # no scheme, path, space, quotes
            return None
        hostname = host.rsplit(":", 1)[0].lower()
        if hostname not in ("127.0.0.1", "::1", "localhost"):
            return None                                                # non-loopback Host w/o CSA_PUBLIC_URL: refuse
        return "http://%s" % host                                      # loopback dev: plain http is fine

    def _onboarding_get(self, p, qs):
        """The pre-auth onboarding surface (see _public). Returns True if it handled the request."""
        if p == "/install":
            base = self._base_url()
            if base is None:
                log.error("/install refused: CSA_PUBLIC_URL is unset and Host %r is not loopback "
                          "(would bake a client-controlled download URL). Set CSA_PUBLIC_URL.",
                          self.headers.get("Host"))
                self._send(421, {"ok": False, "error": "server misconfigured: CSA_PUBLIC_URL is not "
                                 "set. The operator must set it to this server's public URL."})
                return True
            try:
                with open(INSTALL_SH, "rb") as fh:
                    script = fh.read()
            except OSError as e:
                log.error("install.sh unreadable at %s: %s", INSTALL_SH, e)
                self._send(500, {"ok": False, "error": "installer unavailable"})
                return True
            script = script.replace(b"@@BASE_URL@@", base.encode("utf-8"))
            self._send_file(200, script, "text/x-shellscript; charset=utf-8")
            return True

        if p.startswith("/latest/"):
            name = unquote(p[len("/latest/"):])
            if not _ASSET_RE.match(name):                 # an allowlist of the published names, not a sanitizer
                self._send(404, {"ok": False, "error": "unknown client artifact"})
                return True
            fp = os.path.join(CLIENT_DIST, name)
            if not os.path.isfile(fp):
                self._send(404, {"ok": False, "error": "%s is not published on this server "
                                                       "(see docs/public-release/install-server.md)" % name})
                return True
            with open(fp, "rb") as fh:
                body = fh.read()
            ctype = "text/plain; charset=utf-8" if name == "checksums.txt" else "application/octet-stream"
            self._send_file(200, body, ctype)
            return True

        if p == "/api/policy":
            # a credential (a re-run of setup) names the workspace; without one, only a sole workspace
            ok, principal, _ = auth.identify(self.headers)
            self._send(200, cliauth.policy(principal["org_id"] if ok else orgs.sole_org()))
            return True

        if p == "/api/auth-config":
            # what the logged-out sign-in screen renders; never who may sign in
            con = orgs.control_db()
            cfg = auth.sso_config(con)
            sole = orgs.sole_org(con)
            cookie = auth._cookie(self.headers, cliauth.JOIN_COOKIE) or ""
            join = orgs.invite_for(cookie, con=con)
            self._send(200, {"ok": True,
                             "sso": {"name": cfg.display_name, "start_url": "/api/sso/start"} if cfg else None,
                             "sso_required": cfg is not None,
                             "workspace_requests": bool(_request_webhook()),   # the landing needs somewhere to send one
                             # only a single-workspace server can say before sign-in
                             "archive_days": archive.retention_days(sole) if sole else None,
                             "base_url": self._base_url(),     # null: /install would refuse with 421
                             "client_published": _client_published(),
                             # from this browser's own invite cookie: tells its holder what the link said
                             "join_email": join and join["email"],
                             "join_org": join and _org_name(join["org_id"]),
                             "join_stale": bool(cookie) and not join})
            return True

        if p.startswith("/join/"):
            # Validates nothing: every unusable link answers the same 302, and the one refusal comes
            # back through the sign-in (cliauth.JOIN_REFUSED), so /join is no oracle.
            token = unquote(p[len("/join/"):]).strip()
            if not _JOIN_RE.match(token):                # not a link at all: carry nothing forward
                token = ""
            # already signed in (an admin checking a link): do not arm this browser's next sign-in
            if token and self._session_identity():
                token = ""
            self._send(302, b"", "text/html; charset=utf-8", headers=(
                ("Location", "/#/connect"),
                ("Set-Cookie", cliauth.join_cookie(token) if token else cliauth.clear_join_cookie()),
                ("Referrer-Policy", "no-referrer"),      # the path IS a credential
                ("Cache-Control", "no-store")))
            return True

        if p == "/cli-auth":
            con = orgs.control_db()
            who, who_org, _ = self._session_identity(con) or ("", None, False)
            nonce = qs.get("nonce", [""])[0]
            self._send_file(200, cliauth.page(nonce, qs.get("hostname", [""])[0], who, auth.sso_config(con),
                                              cliauth.shipping_of(nonce),
                                              cliauth.policy(who_org or orgs.sole_org())["archive_days"]),
                            "text/html; charset=utf-8")
            return True

        if p in ("/api/sso/start", "/api/sso/callback"):
            # no _same_origin: the issuer sends the callback cross-site by construction
            con = orgs.control_db()
            cfg = auth.sso_config(con)
            if cfg is None:
                self._send(404, {"ok": False, "error": "sign-in with an identity provider is not configured"})
                return True
            base = self._base_url()
            if base is None:
                self._send(421, {"ok": False, "error": "server misconfigured: CSA_PUBLIC_URL is not set."})
                return True
            if p == "/api/sso/start":
                status, body, headers = sso.start(con, cfg, base, qs.get("return", ["/#/welcome"])[0],
                                                  auth._cookie(self.headers, cliauth.JOIN_COOKIE))
            else:
                status, body, headers = sso.finish(con, cfg, base, qs.get("state", [""])[0],
                                                   qs.get("code", [""])[0], qs.get("error", [""])[0],
                                                   auth._cookie(self.headers, sso.STATE_COOKIE))
            self._send(status, body, "text/html; charset=utf-8", headers=headers)
            return True

        if p.startswith("/api/cli-auth/") and p not in ("/api/cli-auth/new", "/api/cli-auth/approve"):
            # the CLI's poll: credentials exactly once; pending, unknown, expired and claimed look alike
            creds = cliauth.claim(unquote(p[len("/api/cli-auth/"):]))
            if creds is None:
                return self._send(200, {"status": "pending"}) or True
            return self._send(200, dict(creds, status="approved")) or True
        return False

    def _401(self, err=None):
        """The 401 body; org mode adds WWW-Authenticate: Bearer."""
        data = json.dumps({"ok": False, "error": err or "authentication required",
                           "code": "unauthorized"}).encode()
        self.send_response(401)
        self._header("Content-Type", "application/json")
        if auth.current_mode() == "org":                  # the one mode that presents a Bearer (matches the MCP gate)
            self._header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(data)

    def _403(self, err):
        return self._send(403, {"ok": False, "code": "forbidden", "error": err})

    def do_OPTIONS(self):
        self._send(204, b"")                              # CORS preflight: open, no corpus, carries no credential

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        qs = parse_qs(u.query)
        if _public(p):                                     # handled here; an unhandled public path 404s
            try:
                if self._onboarding_get(p, qs):
                    return
            except Exception:
                log.exception("onboarding GET %s failed", p)
                return self._send(500, {"ok": False, "error": "internal error"})
            return self._send(404, {"ok": False, "error": "not found"})
        principal, org_id = None, store.DEFAULT_ORG
        if p not in auth.OPEN_PATHS:                       # allowlist-first: anything not open is DENIED when not ok
            ok, principal, err = auth.identify(self.headers, allow_cookie=True)   # browser dashboard: + the session cookie
            if not ok:
                # The SPA shell is data-free, so it is served to render the sign-in. This returns:
                # an unauthenticated request never reaches a route below.
                if not p.startswith("/api/"):
                    return self._serve_ui(p)
                return self._401(err)
            org_id = principal["org_id"]                    # THE tenant boundary — from the credential, never a header
        if p == "/api/health":                            # no auth: lets clients tell "unreachable" from "broken"
            cp = orgs.control_plane_ok()                  # every credential needs control.db: unreadable is a 503
            try:
                schema = _schema_heads()
            except Exception:
                log.exception("schema heads unavailable")
                schema = None
            h = {"ok": cp, "control_plane": "ok" if cp else "unreadable",
                 "version": migrations.image_version(), "schema": schema,
                 "extraction": extraction_status(),
                 "mcp_port": int(os.environ.get("CSA_MCP_PORT", "58768")), "mcp_path": "/mcp"}
            if os.environ.get("CSA_MCP_URL"):             # a proxy that routes /mcp by path on one hostname
                h["mcp_url"] = os.environ["CSA_MCP_URL"]
            if auth.current_mode() == "off":              # corpus details only on the loopback dev path
                try:
                    hc = store.sqlite_db()
                    h["sessions"] = db.run(hc, select(func.count()).select_from(_S)
                                           .where(_S.c.source == "session")).fetchone()[0]
                    h.update(demo_query(hc))
                except Exception:
                    pass
            return self._send(200 if cp else 503, h)
        if p == "/api/pipeline":                          # live ingest tracker; the stall reason is for admins
            stall = extraction_status(org_id)
            if principal["role"] != "admin":
                stall = {**stall, "reason": ""}
            return self._send(200, {**pipeline_status(org_id), "extraction": stall})

        def qint(k, d):
            return _qint(qs, k, d)
        if p.startswith("/api/org/"):                     # tenant admin — scoped to the credential's own org
            return self._org_get(p, qs, principal)
        if p == "/api/config":
            return self._config(principal)
        if p == "/api/machines":                          # the caller's own machines, so it needs no role
            mine = [m for m in orgs.list_tokens(org_id)
                    if m["active"] and (m["email"] or "").lower() == (principal.get("email") or "").lower()
                    and m["hostname"] not in orgs.BROWSER_HOSTNAMES]
            return self._send(200, {"ok": True, "machines": [
                {"hostname": m["hostname"], "created": m["created"], "last_used_at": m["last_used_at"]}
                for m in mine]})
        if p == "/api/client-config":
            # polled by csa-client at every SessionStart, so no corpus query
            m = self._machine()
            return self._send(200, {"ok": True,
                                    # the client refuses to ship past the retention its user consented to
                                    "archive_days": archive.retention_days(org_id),
                                    "directives": fleet.for_client(m) if m else []})
        con = store.sqlite_db(store.org_db(org_id))       # <- the isolation boundary: one corpus file per org
        if p == "/api/labels":
            return self._send(200, LABELS)
        if p == "/api/entity-types":                      # this org's taxonomy: fixed defaults + its custom types
            return self._send(200, {"default": list(entity_types.DEFAULTS),
                                    "default_info": entity_types.DEFAULT_INFO,
                                    "custom": entity_types.custom(con)})
        if p == "/api/entities":                          # the entity browser (Entities page)
            return self._send(200, entities_page(
                con, q=qs.get("q", [""])[0][:200], etype=qs.get("type", [""])[0][:64],
                owner=qs.get("owner", [""])[0], served_only=qs.get("served", [""])[0] == "1",
                sort=qs.get("sort", [""])[0], direction=qs.get("dir", [""])[0],
                offset=qint("offset", 0), limit=max(1, min(qint("limit", 50), 200))))
        if p.startswith("/api/entities/"):
            _e = entity_detail(con, unquote(p[len("/api/entities/"):]))
            return self._send(404 if _e is None else 200,
                              _e if _e else {"ok": False, "error": "no such entity", "code": "not_found"})
        if p == "/api/sessions":
            return self._send(200, analytics.recall_log(con, (principal or {}).get("email") or "",
                                                        result=qs.get("result", [""])[0]))
        if p.startswith("/api/sessions/"):
            return self._send(200, session_detail(con, p.split("/api/sessions/", 1)[1]))
        if p == "/api/corpus/stats":
            return self._send(200, corpus_stats(con, org_id))
        if p == "/api/onboarding":
            return self._send(200, onboarding(
                con, qs.get("project", [""])[0], qint("limit", 3),
                owner=(principal or {}).get("email") or "", agents=orgs.agent_emails(orgs.list_tokens(org_id))))
        if p == "/api/activity":
            return self._send(200, activity(con, qs.get("date", [""])[0], qs.get("owner", [""])[0],
                                            project=qs.get("project", [""])[0],
                                            branch=qs.get("branch", [""])[0],
                                            limit=qint("limit", 200), offset=qint("offset", 0)))
        if p == "/api/improve":
            return self._send(200, improvements(
                con, qs.get("owner", [""])[0], qint("limit", 100),
                caller=principal.get("email") or "",
                org_admin=principal.get("role") == "admin"))     # the workspace admin, never the operator
        if p == "/api/corpus/sessions":
            return self._send(200, corpus_sessions(con, qs.get("owner", [""])[0], principal))
        if p.startswith("/api/corpus/sessions/") and p.endswith("/transcript"):   # before the catch-all
            sid = unquote(p[len("/api/corpus/sessions/"):-len("/transcript")])
            return self._send(*_transcript(con, sid, principal, org_id,
                                           limit=qint("limit", 100), cursor=qs.get("cursor", [""])[0]))
        if p.startswith("/api/corpus/sessions/"):
            _d = corpus_session(con, unquote(p.split("/api/corpus/sessions/", 1)[1]), principal)
            return self._send(404 if _d.get("error") else 200, _d)
        if p == "/api/facts":
            return self._send(200, facts(con, qs.get("q", [""])[0], qs.get("type", [""])[0],
                                         qs.get("label", [""])[0], qs.get("owner", [""])[0],
                                         qs.get("project", [""])[0], qint("offset", 0),
                                         min(qint("limit", 50), 200), principal=principal))
        if p == "/api/graph":
            return self._send(200, graph(con, qs.get("owner", [""])[0], qs.get("days", [30])[0],
                                         qs.get("limit", [60])[0], principal))
        if p == "/api/questions":
            return self._send(200, questions(con))
        if p == "/api/flags":
            return self._send(200, flags(con))
        if p == "/api/team":
            return self._send(200, team(con, orgs.agent_emails(orgs.list_tokens(org_id))))
        if p == "/api/workspace/analytics":               # this org's own dashboard: admins only
            oid = self._org_admin_guard(principal)
            if oid is None:
                return
            weeks = qs.get("weeks", ["12"])[0]
            if weeks not in ("4", "12", "26"):
                return self._send(400, {"ok": False, "error": "weeks must be 4, 12 or 26", "code": "bad_request"})
            return self._send(200, analytics.workspace(con, oid, orgs.list_tokens(oid), int(weeks),
                                                       extraction_status(oid)))
        if p == "/api/me":
            # the gate's verified principal; store.owner_identity() is the server's own account
            days = archive.retention_days(org_id)
            if principal.get("email"):
                return self._send(200, {"email": principal["email"], "name": principal["email"],
                                        "org_id": org_id, "org_name": _org_name(org_id),
                                        "role": principal["role"], "verified": True,
                                        "hostname": self._hostname(),
                                        "archive_days": days, "model_ready": llm.model_ready()})
            # a credential the gate could not name, on a self-host box: whoever is looking owns it
            if org_id == store.DEFAULT_ORG:
                return self._send(200, {**store.owner_identity(), "org_id": org_id,
                                        "org_name": _org_name(org_id),
                                        "role": principal["role"], "verified": False,
                                        "archive_days": days, "model_ready": llm.model_ready()})
            return self._send(200, {"email": None, "name": None, "org_id": org_id,
                                    "org_name": _org_name(org_id), "verified": False,
                                    "archive_days": days, "model_ready": llm.model_ready()})
        return self._serve_ui(p)

    def _onboarding_post(self, p):
        """Pre-auth POSTs, each gated on its own terms. Returns True if handled."""
        n = _content_length(self.headers)
        if n > 64 * 1024:                                  # a handshake body is a few hundred bytes
            self._send(413, {"ok": False, "error": "request body too large", "code": "payload_too_large"})
            return True
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "invalid JSON body", "code": "bad_request"})
            return True
        if p == "/api/cli-auth/new":
            base = self._base_url()
            if base is None:
                self._send(421, {"ok": False, "error": "server misconfigured: CSA_PUBLIC_URL is not set."})
                return True
            nonce, code, ttl = cliauth.new(str(body.get("hostname", ""))[:120], str(body.get("shipping", "")))
            self._send(200, {"ok": True, "nonce": nonce, "code": code, "expires_in": ttl,
                             "consent_policy": cliauth.CONSENT_POLICY,
                             "url": "%s/cli-auth?nonce=%s&hostname=%s" % (
                                 base, quote(nonce, safe=""),
                                 quote(str(body.get("hostname", ""))[:120], safe=""))})
            return True
        # The cookie-carrying public POSTs check _same_origin themselves: public paths return before
        # do_POST's check, and SameSite=Strict still admits a sibling subdomain.
        if p == "/api/cli-auth/approve":
            if not _same_origin(self.headers):
                self._send(401, {"ok": False, "error": "cross-site request refused", "code": "unauthorized"})
                return True
            try:
                ok, err = cliauth.approve(str(body.get("nonce", "")), str(body.get("code", "")),
                                          str(body.get("email", "")), str(body.get("invite", "")),
                                          self._session_identity(), body.get("ack_days"))
            except cliauth.RetentionNotShown as e:
                self._send(200, {"ok": False, "needs_ack": True, "archive_days": e.days,
                                 "error": cliauth.retention_sentence(e.days) + " Click Authorize again to agree."})
                return True
            if not ok:                                    # 200: a 401 would make the browser prompt for basic auth
                self._send(200, {"ok": False, "error": err})
                return True
            self._send(200, {"ok": True})
            return True

        if p == "/api/ui-login":                          # login-CSRF would sign a victim in as the attacker
            if not _same_origin(self.headers):
                self._send(401, {"ok": False, "error": "cross-site request refused", "code": "unauthorized"})
                return True
            join = auth._cookie(self.headers, cliauth.JOIN_COOKIE) or ""
            ok, result = cliauth.web_login(str(body.get("email", "")), str(body.get("invite", "")), join)
            if not ok:
                self._send(401, {"ok": False, "error": result, "code": "unauthorized"})
                return True
            # the token goes only into the httpOnly cookie; the join cookie is spent either way
            self._send(200, {"ok": True}, headers=(("Set-Cookie", cliauth.session_cookie(result)),
                                                   ("Set-Cookie", cliauth.clear_join_cookie())))
            return True

        if p == "/api/workspace-request":
            # The one anonymous write: rate-limited, one success body whether new or duplicate, and a
            # 404 without a webhook to send it to. No _same_origin: it carries no credential.
            webhook = _request_webhook()
            if not webhook:
                self._send(404, {"ok": False, "error": "not found"})
                return True
            key = (self.headers.get("CF-Connecting-IP") or "").strip() or self.client_address[0]
            if _wsreq_limited(key):
                self._send(429, {"ok": False, "error": "too many requests"},
                           headers=(("Retry-After", "600"),))
                return True
            try:                                          # raw values: request_workspace type-checks them
                new_req = orgs.request_workspace(body.get("company"), body.get("email"))
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e), "code": "bad_request"})
                return True
            if new_req:                                   # truthy ONLY for a genuinely new row (dupes are silent)
                log.info("workspace request %s: %s <%s>", new_req["id"], new_req["company"], new_req["email"])
                _notify_new_request(webhook, new_req["id"], new_req["company"], new_req["email"])
            self._send(200, {"ok": True})                 # identical response new-or-dup (no oracle)
            return True

        if p == "/api/ui-logout":                         # a cross-site page could still force a logout
            if not _same_origin(self.headers):
                self._send(401, {"ok": False, "error": "cross-site request refused", "code": "unauthorized"})
                return True
            tok = auth._cookie(self.headers, auth.SESSION_COOKIE)   # revoked, not just dropped by the browser
            if tok:
                try:
                    orgs.revoke(tok)
                except Exception as e:                    # DB down: still clear the cookie, say ok
                    log.warning("ui-logout: revoke failed (%s) — clearing the cookie anyway", e)
            self._send(200, {"ok": True}, headers=(("Set-Cookie", cliauth.logout_cookie()),))
            return True
        return False

    def do_POST(self):
        p = urlparse(self.path).path
        if _public(p):
            try:
                if self._onboarding_post(p):
                    return
            except Exception:
                log.exception("onboarding POST %s failed", p)
                return self._send(500, {"ok": False, "error": "internal error"})
            return self._send(404, {"ok": False, "error": "not found"})
        ok, principal, err = auth.identify(self.headers, allow_cookie=True)  # before the route check: no route oracle
        if not ok:
            return self._401(err)
        ident, org_id = principal["email"], principal["org_id"]
        if _browser_session(self.headers):
            if not _same_origin(self.headers):
                return self._401("cross-site request refused")
            if p not in _COOKIE_POST_OK:
                return self._send(403, {"ok": False, "code": "forbidden",
                                        "error": "%s is not available to a browser session; use a "
                                                 "per-user token (csa-client)" % p})
        if p not in ("/api/flag", "/api/hctype", "/api/ingest", "/api/forget", "/api/ask",
                     "/api/entity-types", "/api/client-config/ack", "/api/graphql", "/api/track",
                     "/api/config", "/api/token/revoke") \
                and not p.startswith("/api/org/"):
            return self._send(404, {"error": "not found"})
        n = _content_length(self.headers)
        if n > (1024 if p == "/api/track" else MAX_BODY):   # reject oversize without reading the body
            return self._send(413, {"ok": False, "error": "request body too large", "code": "payload_too_large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return self._send(400, {"ok": False, "error": "invalid JSON body", "code": "bad_request"})
        if p.startswith("/api/org/"):                     # control-plane writes: opens NO corpus file
            return self._org_post(p, body, principal)
        if p == "/api/config":
            return self._config(principal, body)
        if p == "/api/token/revoke":                      # a machine disconnecting itself, the presented token only
            return self._send(*_revoke_presented(self._machine()))
        con = store.sqlite_db(store.org_db(org_id))       # <- the isolation boundary: one corpus file per org
        # `ident` (the verified email) overrides any self-asserted owner/asked_by; org_id is never read
        # from a body.
        if p == "/api/graphql":
            self._send(200, gql.run_query(body.get("query", ""), body.get("variables"),
                                        con=con, principal=principal, org_id=org_id,
                                        auth_mode=auth.current_mode()))
        elif p == "/api/client-config/ack":               # a machine can only ack its own directive
            m = self._machine()
            if not m:
                self._send(403, {"ok": False, "code": "forbidden", "error":
                                 "acks come from a per-user token (csa-client), not a shared credential"})
            else:
                try:
                    done = fleet.ack(body.get("id", ""), m, body.get("status", ""),
                                     shipped=body.get("shipped", 0), detail=body.get("detail", ""))
                    self._send(200 if done else 404,
                               {"ok": done, "error": None if done else "no such directive"})
                except ValueError as e:                   # unknown status -> a 400, not a 500
                    self._send(400, {"ok": False, "error": str(e)})
        elif p == "/api/track":
            self._send(*track_event(con, ident, body))
        elif p == "/api/flag":
            self._send(200, flag(con, body.get("ref", ""), body.get("label", ""), body.get("note", ""),
                                 ident or body.get("flagger", "")))
        elif p == "/api/ask":                             # the prompt hook always sends its session id
            self._send(200, ask(con, body.get("query", ""), body.get("session", ""),
                                ident or body.get("asked_by", ""), body.get("person", ""), body.get("k", 6),
                                body.get("project", ""), body.get("cwd", ""),
                                org_id=org_id, strict=body.get("strict", bool(body.get("session")))))
        elif p == "/api/forget":
            self._send(200, forget(con, body.get("session_id", ""), ident or body.get("owner"),
                                   require_owner=True, org_id=org_id, admin=auth.workspace_admin(principal)))
        elif p == "/api/hctype":
            self._send(200, hctype(con, body.get("ref", ""), body.get("type", "")))
        elif p == "/api/entity-types":                       # a description is the workspace's extraction prompt
            if self._org_admin_guard(principal) is None:
                return
            try:
                if body.get("delete"):
                    _k = (body.get("key") or "").strip().lower()
                    if not _k:
                        self._send(400, {"ok": False, "error": "missing 'key': which type to delete"})
                    elif entity_types.remove(con, _k):
                        self._send(200, {"ok": True, "key": _k})
                    else:
                        self._send(404, {"ok": False, "error": f"no custom type '{_k}' (built-ins can't be deleted)"})
                else:                                        # attribution as /api/me
                    by = ident or (store.owner_identity().get("email", "")
                                   if org_id == store.DEFAULT_ORG else "")
                    self._send(200, {"ok": True, **entity_types.upsert(
                        con, body.get("key", ""), body.get("description", ""), by=by)})
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e)})
        else:
            # Only a service token may credit a body `owner` (one token streaming many developers);
            # for anyone else `ident` wins, since `sessions.user` is an authz field.
            body_owner = str(body.get("owner") or "").strip().lower()
            on_behalf = None
            if body_owner and principal.get("service"):
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", body_owner):
                    return self._send(400, {"ok": False, "code": "bad_request",
                                            "error": "owner must be a valid email address"})
                on_behalf = body_owner
            self._send(200, ingest_session(body.get("session_id", ""), body.get("content"),
                                           ident or body_owner or None, dry_run=bool(body.get("dry_run")),
                                           redaction_mode=body.get("redaction_mode"), org_id=org_id,
                                           on_behalf_of=on_behalf))

    # --- /api/org/*: an admin managing their own workspace, the org taken only from the credential ---
    def _org_admin_guard(self, principal):
        """The caller's own org id, or None having already sent the refusal."""
        oid = principal["org_id"]
        if principal["role"] != "admin":
            self._403(f"managing the {oid!r} workspace is limited to its admins; ask one of them to "
                      f"make the change, or to give you the admin role")
            return None
        return oid

    def _config(self, principal, body=None):
        """GET (body None) or POST the Settings screen. A refused POST is a 400 with a sentence per
        field and stores nothing. Deployment settings are writable only on a one-workspace server."""
        oid = self._org_admin_guard(principal)
        if oid is None:
            return
        editable, browser = orgs.sole_org() is not None, _browser_session(self.headers)
        if body is not None:
            errors = tenant.apply(oid, body, principal.get("email") or "", editable, browser, http=True)
            if errors:
                return self._send(400, {"ok": False, "code": "invalid", "errors": errors,
                                        "error": "Nothing was saved. " + " ".join(errors.values())})
        return self._send(200, {"ok": True, "org_id": oid, **tenant.describe(oid, editable, browser, http=True)})

    def _providers_view(self):
        dep = tenant.deployment()
        return {"ok": True, "kinds": providers.KINDS, "editable": orgs.sole_org() is not None,
                "providers": [providers.public(x) for x in providers.list_all()],
                "main_model": dep.main_model, "small_model": dep.small_model,
                "default": llm.provider_summary(), "installed": llm.install_credential(),
                "blocked": llm.blocked(), "unavailable_kinds": llm.unavailable_kinds()}

    def _providers_post(self, p, body, principal):
        """AI Models writes. Deployment-wide, so a server hosting several workspaces refuses them: the
        operator sets providers there with `csa.py org config`."""
        if orgs.sole_org() is None:
            return self._send(403, {"ok": False, "code": "forbidden", "error": "this server hosts several "
                                    "workspaces; model providers are set by whoever runs it"})
        who = principal.get("email") or ""
        if p == "/api/org/provider/save":
            existing = providers.get(_text(body, "id")) if _text(body, "id") else None
            prov = providers.checked(body, existing)        # ValueError -> 400 in _org_post
            problem = llm.needs_cli(providers.Target(prov, prov.models[0]))
            if not problem and body.get("test", True):
                problem = llm.probe(prov.models[0], provider=prov)
            if problem:
                return self._send(400, {"ok": False, "code": "probe", "error": problem})
            providers.save(prov, who)
        elif p == "/api/org/provider/remove":
            if not providers.remove(_text(body, "id"), who):
                return self._send(404, {"ok": False, "error": "no such provider"})
        else:
            errors = tenant.apply(orgs.sole_org(), {f: body.get(f, "") for f in ("main_model", "small_model")
                                                      if f in body}, who, True, http=True)
            if errors:
                return self._send(400, {"ok": False, "code": "invalid", "errors": errors,
                                        "error": " ".join(errors.values())})
        return self._send(200, self._providers_view())

    def _org_get(self, p, qs, principal):
        oid = self._org_admin_guard(principal)
        if oid is None:
            return
        if p == "/api/org/members":                      # token_hash is the revoke handle for this admin's own org
            return self._send(200, {"ok": True, "org_id": oid, "members": orgs.list_tokens(oid),
                                    "okta_admin_group": bootstrap.admin_group()})
        if p == "/api/org/invite":                       # a read: only the hash is stored, so say so
            last = next((r for r in orgs.audit_log(oid, 500)
                         if r["action"] in ("rotate_invite", "create_org", "adopt_default_org")), None)
            return self._send(200, {"ok": True, "org_id": oid,
                                    "has_invite": orgs.has_invite(oid),
                                    "last_changed": (last or {}).get("at"),
                                    "note": "the invite is stored only as a hash and cannot be shown "
                                            "again. Rotating issues a new one and kills the old for "
                                            "NEW sign-ins; people who already joined keep working."})
        if p == "/api/org/providers":
            return self._send(200, self._providers_view())
        if p == "/api/org/invites":                      # the live per-person links; never a token
            return self._send(200, {"ok": True, "org_id": oid, "invites": orgs.list_invites(oid),
                                    "max_pending": orgs.MAX_PENDING_INVITES})
        if p == "/api/org/audit":
            try:
                limit = min(int(qs.get("limit", ["100"])[0]), 500)
            except ValueError:
                limit = 100
            return self._send(200, {"ok": True, "org_id": oid, "rows": orgs.audit_log(oid, limit),
                                    "limits": {"actor": False, "reads_logged": False}})   # admin_log has no actor column
        return self._send(404, {"ok": False, "error": "not found"})

    def _org_post(self, p, body, principal):
        oid = self._org_admin_guard(principal)
        if oid is None:
            return
        if not isinstance(body, dict):
            return self._send(400, {"ok": False, "error": "body must be a JSON object", "code": "bad_request"})
        try:
            if p == "/api/org/member/revoke":
                ident = _text(body, "token_hash") or _text(body, "token")
                target = orgs.resolve_token(ident, oid)
                if target and self._presented(target["token_hash"], oid):
                    return self._send(409, {"ok": False, "code": "self_revoke", "error":
                                            "that is the token behind this request. Sign out to end "
                                            "this session, or revoke it from another machine."})
                if target and orgs.is_mutating_admin(target) and orgs.admin_count(oid, mutating_only=True) <= 1:
                    return self._send(409, {"ok": False, "code": "last_admin", "error":
                                            f"that is the only admin of {oid!r} who can change "
                                            f"anything. Revoking it would lock the workspace out of "
                                            f"its own settings. Promote someone first: POST "
                                            f"/api/org/member/role {{\"email\": ..., \"role\": "
                                            f"\"admin\"}}."})
                done = orgs.revoke(ident, org_id=oid)    # org-scoped: another tenant's hash answers like a miss
                return self._send(200 if done else 404,
                                  {"ok": done, "error": None if done else "no such member token"})
            if p == "/api/org/member/role":
                email, role = _text(body, "email"), _text(body, "role")
                n = orgs.set_role(email, oid, role)
                service = not n and any(t["service"] and t["active"] and (t["email"] or "").lower() == email.lower()
                                        for t in orgs.list_tokens(oid))
                return self._send(200 if n else 404,
                                  {"ok": bool(n), "org_id": oid, "email": email, "role": role,
                                   "tokens_updated": n,
                                   "error": None if n else "a service token is always a member" if service
                                   else "nobody in this workspace has a live token for that email "
                                        "(they must sign in first)"})
            if p == "/api/org/invite/new":                 # members only; promotion is member/role
                base = self._base_url()
                if base is None:
                    return self._send(421, {"ok": False, "error": "this server does not know its own "
                                            "public URL, so it cannot make a link. The operator must "
                                            "set CSA_PUBLIC_URL."})
                invite_id, token = orgs.create_invite(oid, _text(body, "email"), principal.get("email"),
                                                      body.get("ttl_days", orgs.INVITE_TTL_DAYS))
                row = next((i for i in orgs.list_invites(oid) if i["id"] == invite_id), None)
                pending = [i for i in orgs.list_invites(oid) if not i["used_at"]]
                return self._send(200, {"ok": True, "org_id": oid, "id": invite_id,
                                        "link": "%s/join/%s" % (base, quote(token, safe="")),
                                        "invite": row, "invites": pending})
            if p == "/api/org/service-token/new":
                email, name = _text(body, "email").lower(), _text(body, "name")
                token = orgs.mint_service_token(oid, email, name)
                return self._send(200, {"ok": True, "org_id": oid, "email": email, "name": name,
                                        "token": token})     # in the response ONCE; only its hash is stored
            if p in ("/api/org/provider/save", "/api/org/provider/remove", "/api/org/models"):
                return self._providers_post(p, body, principal)
            if p == "/api/org/invite/revoke":
                done = orgs.revoke_invite(_text(body, "id"), oid, actor=principal.get("email") or "")
                return self._send(200 if done else 404,
                                  {"ok": done, "error": None if done else "no such invite link"})
            if p == "/api/org/invite/rotate":
                if oid == store.DEFAULT_ORG and not orgs.default_is_adopted():
                    return self._send(400, {"ok": False, "error":
                                            "this workspace onboards with the env invite (CSA_ORG_INVITE); "
                                            "adopt it with `csa.py org adopt-default` to give it its own"})
                people = {m["email"] for m in orgs.list_tokens(oid) if m["active"]}
                invite = orgs.rotate_invite(oid)
                return self._send(200, {"ok": True, "org_id": oid, "invite": invite,
                                        "members_admitted": len(people),
                                        "note": "the old invite is dead for NEW sign-ins. The %d "
                                                "member(s) who already joined keep working — if you "
                                                "rotated because the invite leaked, check "
                                                "/api/org/members and revoke anyone you do not "
                                                "recognise." % len(people)})
        except orgs.LastAdminError as e:
            return self._send(409, {"ok": False, "code": "last_admin", "error": str(e)})
        except ValueError as e:                          # ambiguous prefix / no such org -> 400, not 500
            return self._send(400, {"ok": False, "error": str(e)})
        return self._send(404, {"ok": False, "error": "not found"})

    def _presented(self, token_hash, org_id):
        """Is `token_hash` the credential this request authenticated with (bearer or cookie)?"""
        cands = auth._bearer_candidates(self.headers) + [auth._cookie(self.headers, auth.SESSION_COOKIE)]
        for tok in cands:
            row = orgs.resolve_token(tok, org_id) if tok else None
            if row and row["token_hash"] == token_hash:
                return True
        return False

    def _session_identity(self, con=None):
        """(email, org_id, verified) for a dashboard cookie this server may mint a machine for, else
        None. `verified` means an issuer sign-in, the one a minted token may inherit a role from. Once
        an issuer is configured an invite-era `web` session is refused: it outlives the cutover."""
        tok = auth._cookie(self.headers, auth.SESSION_COOKIE)
        if not tok:
            return None
        con = con or orgs.control_db()
        m = orgs.machine(tok, con=con)
        if not m or (m["hostname"] == "web" and auth.sso_required(con)):
            return None
        return (m["email"], m["org_id"], m["hostname"] == "web-sso")

    def _hostname(self):
        """The presented credential's recorded hostname: `web` for the invite form's sign-in, `web-sso`
        for an issuer sign-in, else the machine that ran `csa-client setup`. "" for a credential that is none."""
        m = self._machine()
        if not m:
            tok = auth._cookie(self.headers, auth.SESSION_COOKIE)
            m = orgs.machine(tok) if tok else None
        return m["hostname"] if m else ""

    def _machine(self):
        """Which machine is calling, from the bearer token's row alone (never the request): orgs.machine()
        or None for anything that is not a per-user token."""
        con = orgs.control_db()
        for tok in auth._bearer_candidates(self.headers):
            m = orgs.machine(tok, con=con)
            if m:
                return m
        return None

    def _serve_ui(self, p):
        if p.startswith("/api/"):                         # an unknown /api path is a 404, never the shell
            return self._send(404, {"ok": False, "error": "not found", "code": "not_found"})
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        fp = os.path.normpath(os.path.join(UI_DIST, rel))
        if not (fp == UI_DIST or fp.startswith(UI_DIST + os.sep)) or not os.path.isfile(fp):   # sep-anchored: reject sibling `dist-*`
            # a missing FILE is a 404 (as the shell it would be cached a year); routes get the shell
            if "." in os.path.basename(rel):
                return self._send(404, {"ok": False, "error": "not found", "code": "not_found"})
            rel, fp = "index.html", os.path.join(UI_DIST, "index.html")
        if not os.path.isfile(fp):
            return self._send(404, {"error": "UI not built: run `npm run build` in ui/ (or `npm run dev` for dev)"})
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css",
                 "svg": "image/svg+xml", "json": "application/json", "woff2": "font/woff2",
                 "ico": "image/x-icon", "png": "image/png"}.get(fp.rsplit(".", 1)[-1], "application/octet-stream")
        cache = (("Cache-Control", "public, max-age=31536000, immutable"),) if rel.startswith("assets/") \
            else (("Cache-Control", "public, max-age=86400"),) if rel.startswith("fonts/") else ()
        with open(fp, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype, headers=cache + (("Content-Length", str(len(data))),))

    def log_message(self, *a):
        pass                                                            # quiet per-request logging


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("CSA_API_PORT", "8766"))
    bind = os.environ.get("CSA_BIND", "127.0.0.1")
    boot_err = auth.boot_error(bind)                     # refuse to expose the corpus unauthenticated
    if boot_err:
        print(boot_err, file=sys.stderr)
        sys.exit(1)
    log.info("csa feedback API on http://%s:%d (auth=%s, UI: %s)", bind, port, auth.current_mode(),
             "ui/dist" if os.path.isdir(UI_DIST) else "dev via vite")
    log.info("LLM provider: %s", llm.provider_summary())
    _spawn_boot_sweep()
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
