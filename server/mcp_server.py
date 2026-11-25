"""MCP server: the `ask`, `flag` and `fact` tools over past Claude Code sessions. The calling model picks a
tool from its description.

  stdio (default): single-user, reads this machine's data/; cold topics self-extract via `claude -p`.
      claude mcp add csa -- uv run --directory /abs/path/to/repo/server python mcp_server.py
  --http: team mode beside apiserver.py, serving the shared corpus over streamable HTTP, no cold fill.
      claude mcp add --transport http csa http://SERVER:58768/mcp -s user --header "X-CSA-User: <email>"
"""
import json, logging, os, sys, uuid, sqlite3, time
from mcp.server.fastmcp import FastMCP, Context

from sqlalchemy import and_, func, literal, select, text

from analyzer import db, store, ask as qengine, redact, resolve, route, auth, llm, tenant, tracer
from analyzer.models import Fact, FactEdge, Review, SessionRow, hidden_context
import apiserver
import app

_HC, _RV = hidden_context, Review.__table__
_FE = FactEdge.__table__

_HTTP = "--http" in sys.argv

_FLAGS = ("gold", "wrong", "unimportant", "clear")   # rerank actions: boost / hide / down-rank / back-to-neutral
_PROC = uuid.uuid4().hex[:12]                             # one server process = one stdio client / CC session


def _org(ctx):
    """The caller's org, from the X-CSA-Org header the ASGI gate stamps from the verified credential (it
    scrubs any inbound copy). In --http mode a missing header raises: defaulting would read another
    tenant's corpus. stdio is the single-user local install, so the default org."""
    if not _HTTP:
        return store.DEFAULT_ORG
    req = getattr(ctx.request_context, "request", None) if ctx is not None else None
    hdr = (req.headers.get("x-csa-org") if req is not None else None) or ""
    if not hdr:
        raise RuntimeError("no verified org on this request: the ASGI gate did not stamp X-CSA-Org. "
                           "Refusing rather than defaulting — that default would be another tenant's corpus.")
    return store.valid_org_id(hdr)


def _ro(org_id=None):
    """Read-only connection to one org's corpus for the LLM planner (store.ro_connect)."""
    return store.ro_connect(store.org_db(org_id))


def _fmt_table(engine, planned, cols, rows):
    """sql result -> scannable markdown (echoes the query, like the CLI). Capped for the MCP payload."""
    head = f"[{engine}] {planned}\n\n"
    if not rows:
        return head + "(no rows)"
    hdr = " | ".join(cols)
    body = "\n".join(" | ".join("" if x is None else str(x)[:80] for x in r) for r in rows[:50])
    more = f"\n… (+{len(rows) - 50} more rows)" if len(rows) > 50 else ""
    return f"{head}{hdr}\n{'-' * min(90, len(hdr))}\n{body}{more}\n\n({len(rows)} rows)"


def _card_line_count(card):
    return max(1, sum(1 for line in card.splitlines() if line.startswith(("- ", "## "))))


def _standalone_card(query, card):
    """Direct identifier questions should return the deterministic evidence card, not a broad FTS tail."""
    if not card:
        return False
    return any(marker in card for marker in resolve._STANDALONE)   # single source of truth (resolve.py)


def _session(ctx):
    """Best-effort MCP session id for provenance: the streamable-http session header if present, else this
    process id (stdio spawns one server per Claude Code session, so per-process == per-session there)."""
    try:
        req = getattr(ctx.request_context, "request", None)
        sid = req.headers.get("mcp-session-id") if req is not None else None
        if sid:
            return sid[:16]
    except Exception:
        pass
    return _PROC


def _user(ctx):
    """The caller for attribution: X-CSA-User, which the gate replaces with a verified email when it has one."""
    try:
        req = getattr(ctx.request_context, "request", None)
        return (req.headers.get("x-csa-user") or "")[:80] if req is not None else ""
    except Exception:
        return ""


# stdout is the MCP JSON-RPC channel: all logging goes to stderr
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("csa.mcp")

mcp = FastMCP("csa", host=os.environ.get("CSA_MCP_BIND", "0.0.0.0"),
              port=int(os.environ.get("CSA_MCP_PORT", "58768")))
_boot = store.sqlite_db()   # the schema must exist before any read-only connection opens
if not db.run(_boot, text("SELECT 1 FROM sqlite_master WHERE name='docs'")).fetchone():
    store.build_fts(_boot)  # recall queries `docs` unguarded
_boot.close()


@mcp.tool()
def ask(query: str, person: str = "", in_session: str = "", ctx: Context = None) -> str:
    """Answer a question from the team's past Claude Code sessions (the user's own and their teammates') — the
    undocumented decisions, gotchas, and constraints. This is the ONE retrieval tool; call it whenever the user
    refers to past or team work instead of guessing. It routes automatically:

      • PLAIN LOOKUP (default, fast, no cost) — "what did we decide about X", "the ETL work I did earlier",
        "how do we release to production", "the auth setup". Returns golden context as markdown grouped by type
        with who/when provenance and an `id:` per fact (pass that id to `fact` or `flag`). When no relevance
        floor is cleared: a plain word match over facts that says so, else a miss naming the unseen words.
      • RELATIONAL / AGGREGATE / MULTI-HOP — "what depends on graph.db", "how many gotchas about ETL",
        "which sessions touched Cabana", "who owns the deploy path", "most-mentioned services". These plan and
        run a SQL/graph query over the corpus and return a small result table. (Costs a planning step, ~seconds.)
      • LOCATE A SESSION — "which session ran the ETL E2E", "find me the session that talked about X",
        "what sessions from last week touched the graph writer lock". Returns a short RANKED LIST OF SESSIONS
        (id · date · project@branch · owner · which of your words actually matched · summary) instead of a
        fact page. `matched:` is the honest part: a word of yours missing there means the corpus never used
        it, so relax that word rather than trusting the ranking. Then pass an id back as `in_session`.
      • SCOPE-AWARE — first-person questions ("what did I decide about X", "the ETL work I did") automatically
        put the ASKER's own sessions first, clearly labeled; naming a teammate ("how did jules deploy") scopes
        to them. Scoping reorders and labels, never hides: teammates' relevant work still appears under a
        divider. A ticket/bug id the corpus has never seen gets an honest "nothing indexed mentions this"
        instead of adjacent noise.

    `query` is a natural-language question or a few keywords. Optionally pass `person` (a name/email fragment —
    "alice", "bob@acme.dev") to scope a plain lookup to a teammate ("Ask Alice: how did you deploy?"); that
    person's sessions come first, falling through to the team's if their coverage is thin. Unknown names never
    error — the reply lists the known people so you can retry.
    Optionally pass `in_session` (a session id, or its first 8 characters as shown in any provenance line) to
    read ONE session — its summary, what it worked on, and its facts. That's the follow-up to a locate answer;
    `query` is ignored when it is set.
    """
    session, who = _session(ctx), _user(ctx) or None
    org_id = _org(ctx)
    # redacted at the server floor, as /api/ask does, before the span, the log or the planner sees it
    q_mode, q_deny, q_allow = tenant.redaction_policy(db_path=store.org_db(org_id))
    query, _ = redact.redact((query or "")[:1000], q_mode, q_deny, q_allow)
    # root=True: FastMCP runs every sync tool on one thread, so a leaked span stack would poison later asks
    with tracer.span("ask", "CHAIN", db=store.org_db(org_id), root=True,
                     **{"input.value": query, "session.id": session, "user.id": who,
                        "csa.org_id": org_id, "csa.entry": "mcp_ask"}) as sp:
        out = _ask(query, person, in_session, ctx, session, who, org_id)
        sp.set(**{"output.value": (out or "")[:2000]})
        return out


def _ask(query, person, in_session, ctx, session, who, org_id):
    t0 = time.perf_counter()
    if in_session:                                       # explicit id -> read that session, no routing/ranking
        out = app.session_facts(in_session, org_id=org_id)
        tracer.set_attr(**{"csa.outcome": "served"})
        store.log_query(store.sqlite_db(store.org_db(org_id)), who, session, "session", f"in_session={in_session}",
                        "route=team/locate person=-", out.count("\n- "), int((time.perf_counter() - t0) * 1000))
        return out
    reached = None                                       # first-column values a sql query reached
    con = _ro(org_id)
    # Identifier tokens ("PR #322", a pasted URL) resolved against the entities table: their evidence card
    # is prepended to the answer, and an explicit id the corpus has never seen gets an honest miss line.
    try:
        with tracer.span("card", "TOOL") as csp:
            eids = resolve.resolve(con, query)
            card = resolve.cards(con, eids, query)
            csp.set(**{"csa.resolved_ids": ",".join(eids)[:400] or None,
                       "db.table": "pr_links" if card and "created by session" in card else None,
                       "output.value": (card or "")[:1000] or None})
        card = card + "\n\n" if card else ""
    except sqlite3.OperationalError as e:                # a schema surprise degrades to a plain ask
        log.warning("short-circuit skipped (%s)", str(e)[:80])
        eids, card = [], ""
    rt = route.classify(con, query, asked_by=who, person=person or None, eids=eids, allow_llm=True)
    person_eff = rt.person
    outcome = None                                       # csa.outcome on the root span (docs/traces-attrs.md)
    if _standalone_card(query, card):
        out, engine, planned, n = card, "fts", query, _card_line_count(card)
        outcome = "card"
    elif person or rt.shape != "relational":             # fast path, no planner; cold fill only on stdio
        metrics = {}
        out = app.recall(query, fill=not _HTTP, session=session, person=person_eff, asked_by=who, org_id=org_id,
                         metrics=metrics)
        _, out, _ = apiserver.lenient_tier(con, query, out, metrics, 6, person_eff, None)
        engine, planned, n = "fts", query, out.count("\n- ")
    else:
        # Relational: the planner writes and runs SQL on this org's own file. Resolved ids ride along, since
        # its vocabulary misses rare entities.
        hinted = query + (f"\nKnown entity ids for this question: {', '.join(eids)}" if eids else "")
        if rt.scope == "self" and who:
            hinted += f"\nThe asker is {who}: 'I'/'my'/'me' in the question means sessions.user = '{who}'."
        elif rt.scope == "person" and person_eff:
            hinted += f"\nThe question is about {person_eff}: filter sessions.user = '{person_eff}'."
        engine, planned, cols, rows = qengine.plan_and_run(con, hinted)
        if engine in ("fts", "fts-graphless"):           # a content question: recall's provenance
            out = app.recall(query, fill=not _HTTP, session=session, person=person_eff, asked_by=who, org_id=org_id)
            n = out.count("\n- ")
            if engine == "fts-graphless":
                out += "\n\n" + qengine.DEGRADE_NOTE
        elif rows is None:
            out, n = f"[{engine}] {planned}\n\ncould not run this query (unrepairable).", 0
            outcome = "error"
        else:
            out, n = _fmt_table(engine, planned, cols, rows), len(rows)
            outcome = "table"
            seen = dict.fromkeys(str(r[0]) for r in rows if r and r[0] is not None)
            reached = ",".join(list(seen)[:40])[:800] or None
    if card and out != card:
        if _standalone_card(query, card):
            out, n = card, _card_line_count(card)
        else:
            if out.lstrip().startswith(("Nothing on", "Nothing solid", "No past-session context")):
                out = "_(no additional free-text context beyond the evidence above)_"   # never contradict a solid card
            out, n = card + out, max(n, _card_line_count(card))
    rtag = f"route={rt.scope}/{rt.shape}{'+llm' if rt.escalated else ''} person={person_eff or '-'}"
    tracer.set_attr(**{"csa.outcome": outcome or ("served" if n else "miss"),
                       "csa.resolved_ids": ",".join(eids)[:400] or None})
    store.log_query(store.sqlite_db(store.org_db(org_id)), who, session, engine, query,
                    f"{rtag} | {planned}", n, int((time.perf_counter() - t0) * 1000), result_ids=reached)
    log.info("ask(%r) org=%s engine=%s rows=%s user=%s %s ents=%s", query[:60], org_id, engine, n, who or "-",
             rtag, ",".join(eids) or "-")
    return out


@mcp.tool()
def flag(ref: str, label: str, note: str = "", ctx: Context = None) -> str:
    """Correct the corpus so future recalls improve — the self-improvement loop. Pass the `id:` shown next to a
    recalled fact and a label:
      gold        = true & valuable       -> boosted in future recalls
      wrong       = false OR stale        -> hidden (a fact that isn't true or no longer holds; note 'stale' vs 'false' in `note`)
      unimportant = true but low value    -> down-ranked (kept, ranked lower)
      clear       = remove a prior flag   -> back to neutral ranking
    Call this when a recalled fact is incorrect (e.g. a constraint that doesn't hold) or noise — and also when
    the USER contradicts or corrects a recalled fact in conversation. Takes effect now.
    """
    label = label.strip().lower()
    if label not in _FLAGS:
        return f"unknown label {label!r}; use one of: {', '.join(_FLAGS)}"
    con = store.sqlite_db(store.org_db(_org(ctx)))
    if not db.run(con, select(literal(1)).select_from(_HC).where(_HC.c.ref == ref).limit(1)).fetchone():
        return f"no fact with id {ref!r} (use the id:… shown in recall output)"
    who = _user(ctx)
    if label == "clear":
        if not db.run(con, select(literal(1)).select_from(_RV)
                      .where(_RV.c.kind == "hc", _RV.c.ref == ref)).fetchone():
            return f"{ref} has no flag to clear — it was already ranked neutrally."
        store.clear_review(con, "hc", ref)
    else:
        store.set_review(con, "hc", ref, label, note=note or "mcp-flag", flagger=who or "mcp")
    store.build_fts(con)
    log.info("flag(ref=%s, label=%s, by=%s)", ref, label, who or "-")
    verb = {"gold": "boost it (×1.25)", "unimportant": "down-rank it (×0.6)", "wrong": "hide it",
            "clear": "rank it neutrally again"}[label]
    return f"Flagged {ref} as '{label}' — future recalls will {verb}. Re-flag (or 'clear') to change your mind."


@mcp.tool()
def fact(ref: str, ctx: Context = None) -> str:
    """Fetch ONE fact's full, untruncated text with its evidence quote and source, by the `id:` shown in recall
    output. Use when a recalled fact was truncated, or before acting on / flagging a fact you want to verify."""
    con = store.sqlite_db(store.org_db(_org(ctx)))       # fact ids are guessable content hashes: org from ctx
    h, s = _HC.alias("h"), SessionRow.__table__.alias("s")
    r, f = _RV.alias("r"), Fact.__table__.alias("f")
    row = db.run(con, select(
        h.c.type, h.c.claim, h.c.evidence, h.c.session_id, s.c.project, s.c.user,
        func.substr(s.c.started_at, 1, 10), r.c.status,
        h.c.repo, h.c.path, f.c.author, f.c.created_at, f.c.valid_at, f.c.invalid_at, f.c.state)
        .select_from(h
            .join(s, s.c.session_id == h.c.session_id)
            .outerjoin(r, and_(r.c.kind == "hc", r.c.ref == h.c.ref))
            .outerjoin(f, f.c.ref == h.c.ref))
        .where(h.c.ref == ref).limit(1)).fetchone()
    log.info("fact(ref=%s) org=%s hit=%s by=%s", ref, _org(ctx), int(bool(row)), _user(ctx) or "-")
    if not row:
        return f"no fact with id {ref!r} (use the id:… shown in recall output)"
    ty, claim, ev, sid, proj, user, day, status, hrepo, hpath, author, created, valid, invalid, state = row
    home = f"\n— code: {hrepo}" + (f"/{hpath}" if hpath else "") if hrepo else ""   # as recall: repo required
    # a closed validity window shows here, with its winners: recall hides it, this is the direct read
    validity = ""
    if invalid:
        winners = db.run(con, select(_FE.c.src_ref, _FE.c.at)
                         .where(_FE.c.dst_ref == ref, _FE.c.rel == "supersedes")
                         .order_by(_FE.c.at.desc(), _FE.c.src_ref)).fetchall()
        by = f" by {', '.join(f'id:{w}' for w, _ in winners)}" if winners else ""
        validity = f"\n\n**⚠ superseded {invalid[:10]}{by}** — no longer current; valid {valid and valid[:10] or '?'}→{invalid[:10]}."
    elif state and state not in ("extracted", "current"):
        validity = f"\n\n_state: {state}_"
    return (f"**{ty}** (id:{ref} · label: {status or 'unreviewed'})\n\n{claim or ''}\n\n"
            + (f"> {ev}\n\n" if ev else "")
            + f"— {user} · {day or '?'} · {proj or '-'}/{sid}{home}"
            + (f"\n— author: {author} · since {valid[:10] if valid else '?'}" if author else "") + validity)


class _AuthGate:
    """ASGI auth gate around the streamable-http app: auth.identify, the same gate as the HTTP api, on every
    HTTP request, else the canonical 401. Stamps X-CSA-Org from the credential, and X-CSA-User when the
    credential carries a verified email."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = auth._CIHeaders({k.decode("latin-1"): v.decode("latin-1")   # latin-1 maps all bytes: never raises before identify()
                                   for k, v in scope.get("headers") or []})
        ok, principal, err = auth.identify(headers)
        if not ok:
            body = json.dumps({"ok": False, "error": err or "authentication required",
                               "code": "unauthorized"}).encode()
            hdrs = [(b"content-type", b"application/json")]
            if auth.current_mode() == "org":
                hdrs.append((b"www-authenticate", b"Bearer"))
            await send({"type": "http.response.start", "status": 401, "headers": hdrs})
            return await send({"type": "http.response.body", "body": body})
        scope = dict(scope)
        # X-CSA-Org selects the corpus file: always scrubbed, always set from the credential
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"x-csa-org"]
        scope["headers"].append((b"x-csa-org", principal["org_id"].encode()))
        if principal["email"]:                           # without one (self-host shared secret) it stays asserted
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"x-csa-user"]
            scope["headers"].append((b"x-csa-user", principal["email"].encode()))
        await self.app(scope, receive, send)


def _run_http():
    bind = os.environ.get("CSA_MCP_BIND", "0.0.0.0")
    boot_err = auth.boot_error(bind)                     # the default bind is 0.0.0.0
    if boot_err:
        print(boot_err, file=sys.stderr)
        sys.exit(1)
    import uvicorn
    log.info("team MCP on http://%s:%s/mcp (auth=%s)", mcp.settings.host, mcp.settings.port, auth.current_mode())
    log.info("LLM provider: %s", llm.provider_summary())
    uvicorn.run(_AuthGate(mcp.streamable_http_app()), host=mcp.settings.host, port=mcp.settings.port,
                log_level="info")


if __name__ == "__main__":
    if _HTTP:
        _run_http()
    else:
        mcp.run()                                        # stdio: single-user, local, no gate
