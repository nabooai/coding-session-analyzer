"""GraphQL for the corpus: session search and paged transcripts.

WHY THIS EXISTS AND WHAT IT IS NOT. CSA's HTTP surface is hand-written JSON routes, one per view,
each shaped for the one screen that calls it. That is fine for a dashboard and wrong for the two
things here: a transcript is unbounded (real sessions run to thousands of blocks, so it MUST page),
and "whose sessions are these" is a filter combination the REST routes would grow a parameter for
every time someone asked. So these two get a typed, paged query surface.

Matching the house style deliberately: plain `*Page` types with
`items/nextCursor/hasMore/totalCount`, NOT Relay connections. The sibling GraphQL services this was modelled on have no edges/node
anywhere, and CSA inventing them would make it the odd one out for nothing.

NO WEB FRAMEWORK. `schema.execute_sync` runs inside the existing `apiserver.do_POST` handler, after
the SAME `auth.identify` gate every other route goes through. There is no second auth path to keep
in sync, no ASGI server, and no way to reach a resolver without a principal: `context` is built by
the caller from the already-authenticated request, and every resolver reads its org from there and
never from an argument.

AUTHORIZATION IS PER FIELD, NOT PER QUERY. `sessions` is org-wide (the corpus is the team's shared
memory — this is what the dashboard already shows). `transcript` is owner-or-admin, the narrower
rule, because the raw text of one person's session is not team-readable. A query asking for both
gets one and not the other; the transcript field returns an empty page rather than an error, for the
same reason the REST route 404s uniformly — it must not become an oracle for whose session an id is.
"""
from __future__ import annotations

import typing

import strawberry
from sqlalchemy import func, or_, select
from strawberry.types import Info

from analyzer import archive, auth, db, improve, models, pagination, store

# The Fact fields copied straight from apiserver.facts()'s row dicts. Named explicitly so a new
# column there does not silently become a GraphQL field nobody reviewed.
_FACT_FIELDS = ("ref", "type", "claim", "evidence", "project", "session_id", "user", "started_at",
                "label", "flagger", "corroboration", "used", "dominant", "judge_gold",
                "author", "domain", "created_at", "valid_at", "invalid_at", "state",
                "cluster_id", "superseded_by", "superseded_at")

_S = models.SessionRow.__table__
_SM = models.SessionMetrics.__table__
_HC = models.hidden_context
_MEN = models.mentions


# --- types ----------------------------------------------------------------------------------------
# snake_case here becomes camelCase in the schema (strawberry's default), which is what the house
# schema already exposes: `nextCursor`, `hasMore`, `totalCount`.

@strawberry.type
class TranscriptBlock:
    """One renderable piece of a session: a message's text, a thought, a tool call, or its result.

    FLAT, not nested inside a message, because this is the unit that PAGES. A long assistant turn is
    many blocks; if the page were a message, one message could blow past any page size. `message_index`
    is what lets a client regroup them for display without the server having to choose."""
    index: int
    message_index: int
    role: str                                  # "user" | "assistant"
    type: str                                  # text | thinking | tool_use | tool_result | image
    text: str = ""
    tool_name: str = ""                        # tool_use only
    tool_id: str = ""                          # pairs a tool_use with its tool_result
    is_error: bool = False                     # tool_result only
    truncated: bool = False                    # the stored copy was capped at ingest
    at: str = ""
    sidechain: bool = False                    # a subagent's message, interleaved with the main run
    meta: bool = False


@strawberry.type
class TranscriptPage:
    items: typing.List[TranscriptBlock]
    total_count: int
    has_more: bool
    next_cursor: typing.Optional[str] = None
    # Retention is part of the answer, not metadata about it: a reader deciding whether to rely on
    # this needs to know it disappears, and when. 0 means this server retains nothing;
    # tenant.KEEP_FOREVER (-1) means it is kept until its owner or a workspace admin forgets it.
    retention_days: int = 0
    redaction_mode: str = ""
    stored_at: str = ""


@strawberry.type
class SessionSummary:
    session_id: str
    user: str = ""
    project: str = ""
    git_branch: str = ""
    started_at: str = ""
    ended_at: str = ""
    summary: str = ""
    n_user: int = 0
    n_asst: int = 0
    fact_count: int = 0
    entity_count: int = 0
    n_user: int = 0
    n_asst: int = 0
    deployed_to_prod: int = 0
    has_transcript: bool = False               # is the raw text still retained AND readable by you
    cost_usd: typing.Optional[float] = None    # None = not measured, or not yours to see (may_see_metrics)


@strawberry.type
class Coverage:
    """Which of the query's terms the corpus has never seen, and which are rare enough to carry real
    topical signal. It is what lets the UI admit a miss instead of ranking on the common words."""
    missing: typing.List[str]
    distinctive: typing.List[str]
    terms: int


@strawberry.type
class Fact:
    """One deduplicated claim, as the Memory page renders it.

    Every field the existing view reads is here on purpose. A search that returned a SUBSET would
    look like it worked and quietly blank the labels, the supersession banner and the corroboration
    counts — the parts that make a fact trustworthy rather than just true-looking."""
    ref: str
    type: str = ""
    claim: str = ""
    evidence: typing.Optional[str] = None
    project: typing.Optional[str] = None
    session_id: str = ""
    user: typing.Optional[str] = None
    started_at: typing.Optional[str] = None
    label: typing.Optional[str] = None
    flagger: typing.Optional[str] = None       # "judge" = the auto-labeler, else a human
    corroboration: int = 0                     # extracted independently from N sessions
    used: int = 0                              # recalled into N later sessions
    dominant: int = 0
    judge_gold: int = 0
    # schema v2 (facts table)
    author: typing.Optional[str] = None        # FIRST asserter, distinct from `user`
    domain: typing.Optional[str] = None
    created_at: typing.Optional[str] = None    # system time
    valid_at: typing.Optional[str] = None      # world time
    invalid_at: typing.Optional[str] = None
    state: typing.Optional[str] = None
    cluster_id: typing.Optional[str] = None
    superseded_by: typing.Optional[str] = None
    superseded_at: typing.Optional[str] = None


@strawberry.type
class FactPage:
    items: typing.List[Fact]
    total_count: int
    has_more: bool
    next_cursor: typing.Optional[str] = None
    coverage: typing.Optional[Coverage] = None
    rules: typing.List[Fact] = strawberry.field(default_factory=list)   # project rules, first page only


@strawberry.type
class SessionPage:
    items: typing.List[SessionSummary]
    total_count: int
    has_more: bool
    next_cursor: typing.Optional[str] = None


# --- helpers --------------------------------------------------------------------------------------

def _ctx(info: Info):
    """(con, principal, org_id) from the request context the HTTP layer built. Resolvers never take
    an org from an ARGUMENT — that is the tenant boundary, and it comes only from the credential."""
    c = info.context
    return c["con"], c.get("principal") or {}, c["org_id"]


def may_read_transcript(principal, owner, mode):
    """The transcript rule, shared with apiserver._transcript: the owner, a named workspace admin
    (auth.workspace_admin), or anyone on loopback dev. The self-host shared credential carries the
    admin role with no email and is held by every client, so it never qualifies."""
    if mode == "off" or auth.workspace_admin(principal):
        return True
    me = principal.get("email") or ""
    return bool(me) and me.strip().lower() == (owner or "").strip().lower()


def may_see_metrics(principal, owner, mode):
    """Per-session cost and waste numbers: the session's owner, a workspace admin, or anyone on loopback
    dev. The same boundary apiserver.improvements draws, applied wherever a session row carries them."""
    if mode == "off" or principal.get("role") == "admin":
        return True
    me = principal.get("email") or ""
    return bool(me) and me.strip().lower() == (owner or "").strip().lower()


def _like(term):
    """A SQL LIKE pattern for a user's search box, with their wildcards escaped.

    `%` and `_` typed by a person mean those characters, not "match anything" — an unescaped `%`
    turns a search for a literal into a match-everything, which reads as a broken filter."""
    t = (term or "").strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


# --- query ------------------------------------------------------------------------------------------

@strawberry.type
class Query:
    @strawberry.field(description="Sessions in your org, newest first. `user` is a SEARCH: a "
                                  "case-insensitive substring of the owner, so 'mara' finds "
                                  "mara@example.com. Combine with `project` and `q` to narrow.")
    def sessions(self, info: Info,
                 user: typing.Optional[str] = None,
                 project: typing.Optional[str] = None,
                 q: typing.Optional[str] = None,
                 limit: int = 25,
                 cursor: typing.Optional[str] = None) -> SessionPage:
        con, principal, org_id = _ctx(info)
        days = archive.retention_days(org_id)          # once per page, not once per row
        s = _S.alias("s")
        conds = [s.c.source == "session"]
        if user:
            # A SEARCH, not an equality filter. The REST route matches the owner exactly, which is
            # right for a "my sessions" toggle and useless for a person typing a name: nobody
            # remembers whether a teammate ships as `jules` or `jules@example.com`, and both exist in a
            # real corpus (an OS-username fallback identity has no domain).
            conds.append(func.lower(func.coalesce(s.c.user, "")).like(_like(user), escape="\\"))
        if project:
            conds.append(func.lower(func.coalesce(s.c.project, "")).like(_like(project), escape="\\"))
        if q:
            # `user` is in this OR because the filter box offers "person" first and a person's name
            # is the most common thing typed into it. Without it, searching a colleague returned
            # nothing, which reads as "they have no sessions" rather than "this box ignores names".
            # `user:` above stays the EXACT scope (the Mine/Everyone switch); this is the free search.
            conds.append(or_(func.lower(func.coalesce(s.c.user, "")).like(_like(q), escape="\\"),
                             func.lower(func.coalesce(s.c.project, "")).like(_like(q), escape="\\"),
                             func.lower(func.coalesce(s.c.summary, "")).like(_like(q), escape="\\"),
                             func.lower(func.coalesce(s.c.git_branch, "")).like(_like(q), escape="\\"),
                             func.lower(s.c.session_id).like(_like(q), escape="\\")))
        total = db.run(con, select(func.count()).select_from(s).where(*conds)).fetchone()[0]
        lim, off = pagination.sql_window(limit, cursor)
        hc = (select(func.count()).select_from(_HC)
              .where(_HC.c.session_id == s.c.session_id).scalar_subquery())
        ents = (select(func.count(func.distinct(_MEN.c.entity_id))).select_from(_MEN)
                .where(_MEN.c.session_id == s.c.session_id).scalar_subquery())
        m = _SM.alias("m")
        rows = db.run(con, select(s.c.session_id, s.c.user, s.c.project, s.c.git_branch,
                                  s.c.started_at, s.c.ended_at, s.c.summary, s.c.n_user,
                                  s.c.n_asst, hc, ents, s.c.deployed_to_prod, improve.known_cost(m))
                      .select_from(s.outerjoin(m, m.c.session_id == s.c.session_id))
                      .where(*conds)
                      # started_at can be NULL on a session parsed before it was recorded; session_id
                      # is the tiebreaker so the ORDER IS TOTAL. Without it two rows with equal
                      # timestamps can swap between pages and the walk repeats or skips one.
                      .order_by(s.c.started_at.desc(), s.c.session_id.desc())
                      .limit(lim).offset(off)).fetchall()
        mode = info.context.get("auth_mode", "")
        items = []
        for sid, u, pj, br, st, en, summ, nu, na, nhc, nent, dep, cost in rows:
            items.append(SessionSummary(
                session_id=sid, user=u or "", project=pj or "", git_branch=br or "",
                started_at=st or "", ended_at=en or "", summary=summ or "",
                n_user=nu or 0, n_asst=na or 0, fact_count=nhc or 0, entity_count=nent or 0,
                deployed_to_prod=int(bool(dep)),
                cost_usd=cost if may_see_metrics(principal, u, mode) else None,
                # Truthful per CALLER: a teammate's retained transcript exists but is not theirs to
                # read, so it must not advertise as available to them.
                has_transcript=bool(may_read_transcript(principal, u, mode)
                                    and archive.get(sid, org_id=org_id, days=days))))
        p = pagination.page(items, limit=lim, cursor=cursor, total=total)
        return SessionPage(items=p["items"], total_count=p["total"], has_more=p["has_more"],
                           next_cursor=p["next_cursor"])

    @strawberry.field(description="Team knowledge search: deduplicated facts ranked by real usage. "
                                  "`q` runs the FTS index, and a `q` naming one teammate searches "
                                  "their facts on its other words; `owner` matches a person exactly "
                                  "(the Mine/Everyone scope). This is what the global search box calls.")
    def facts(self, info: Info,
              q: typing.Optional[str] = None,
              type: typing.Optional[str] = None,
              label: typing.Optional[str] = None,
              owner: typing.Optional[str] = None,
              project: typing.Optional[str] = None,
              limit: int = 50,
              cursor: typing.Optional[str] = None) -> FactPage:
        # Late import: apiserver imports this module, so a top-level import would cycle. The query
        # itself is NOT reimplemented here — it is one FTS-ranked statement with a usage join and a
        # supersession subquery, already the most-exercised read in the app. A second copy would be
        # a second thing to keep correct, and the first divergence would be silent.
        import apiserver
        con, principal, _org = _ctx(info)
        lim, off = pagination.sql_window(limit, cursor)
        res = apiserver.facts(con, q or "", type or "", label or "", owner or "",
                              project or "", offset=off, limit=lim, principal=principal, scope_named=True)
        rows = res.get("rows") or []
        p = pagination.page(rows, limit=lim, cursor=cursor, total=res.get("total") or 0)
        cov = res.get("coverage") or None
        return FactPage(
            items=[Fact(**{k: r.get(k) for k in _FACT_FIELDS if k in r}) for r in p["items"]],
            rules=[Fact(**{k: r.get(k) for k in _FACT_FIELDS if k in r}) for r in res.get("rules") or []],
            total_count=p["total"], has_more=p["has_more"], next_cursor=p["next_cursor"],
            coverage=Coverage(missing=cov.get("missing") or [],
                              distinctive=cov.get("distinctive") or [],
                              terms=int(cov.get("terms") or 0)) if cov else None)

    @strawberry.field(description="The retained transcript of one session, in pages. Readable by "
                                  "the session's owner and by a workspace admin. An empty page "
                                  "means: not retained, past its window, or not yours.")
    def transcript(self, info: Info, session_id: str, limit: int = 100,
                   cursor: typing.Optional[str] = None) -> TranscriptPage:
        con, principal, org_id = _ctx(info)
        days = archive.retention_days(org_id)
        empty = TranscriptPage(items=[], total_count=0, has_more=False, retention_days=days)
        rec = archive.get(session_id, org_id=org_id, days=days)
        if not rec:
            return empty
        row = db.run(con, select(_S.c.user).where(_S.c.session_id == session_id)).fetchone()
        owner = (row[0] if row else None) or rec.get("user") or ""
        if not may_read_transcript(principal, owner, info.context.get("auth_mode", "")):
            # The same answer as "no such session", for the same reason the REST route gives it:
            # session ids are handed out as recall provenance, so a distinguishable refusal would
            # confirm that an id exists and belongs to a named colleague.
            return empty
        raw = archive.blocks(rec)
        p = pagination.page(raw, limit=limit, cursor=cursor)
        items = []
        for i, b in enumerate(p["items"], start=pagination.decode_cursor(cursor)):
            items.append(TranscriptBlock(
                index=i, message_index=int(b.get("message_index") or 0),
                role=b.get("role") or "user", type=b.get("type") or "text",
                text=b.get("text") or b.get("input") or "",
                tool_name=b.get("name") or "", tool_id=b.get("tool_id") or "",
                is_error=bool(b.get("is_error")), truncated=bool(b.get("truncated")),
                at=b.get("at") or "", sidechain=bool(b.get("sidechain")), meta=bool(b.get("meta"))))
        return TranscriptPage(items=items, total_count=p["total"], has_more=p["has_more"],
                              next_cursor=p["next_cursor"], retention_days=days,
                              redaction_mode=rec.get("redaction_mode") or "",
                              stored_at=rec.get("stored_at") or "")


schema = strawberry.Schema(query=Query)


def run_query(query, variables=None, con=None, principal=None, org_id=None, auth_mode=""):
    """Run one operation and return a JSON-serialisable {data, errors} envelope.

    The context is assembled HERE from what the HTTP layer already authenticated — a resolver cannot
    be reached without one, and cannot widen it. Errors are returned as GraphQL errors (a 200 with an
    `errors` array is the protocol's own shape) rather than raised, so a bad query never becomes a
    500 or leaks a traceback to a client."""
    if not isinstance(query, str) or not query.strip():   # strawberry raises on an empty document
        return {"data": None, "errors": [{"message": "query must be a non-empty string"}]}
    result = schema.execute_sync(query, variable_values=variables or {},
                                 context_value={"con": con, "principal": principal or {},
                                                "org_id": org_id, "auth_mode": auth_mode})
    out = {"data": result.data}
    if result.errors:
        # `str(e)` is the message plus location; the original exception is not exposed.
        out["errors"] = [{"message": str(e.message if hasattr(e, "message") else e)}
                         for e in result.errors]
    return out


def sdl():
    """The schema as SDL — what a client generates types from, and what a review reads."""
    return schema.as_str()


if __name__ == "__main__":
    import os, tempfile

    wd = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(wd)
    try:
        from analyzer import tenant
        store.sqlite_db(store.org_db(None)).close()
        tenant.set_config(store.org_db(None), archive_days=30)
        con = store.sqlite_db(":memory:")
        rows = [("s1", "mara@example.com", "csa", "2026-08-30T10:00:00Z", "fixed the lab 502"),
                ("s2", "tomasz@example.com", "ledger", "2026-08-29T10:00:00Z", "purge investigation"),
                ("s3", "MARA@example.com", "csa", "2026-08-28T10:00:00Z", "entity types"),
                ("s4", "jules", "acme-ai", "2026-08-27T10:00:00Z", "sqlite writer lock")]
        from sqlalchemy import insert
        _HC = models.hidden_context
        for sid, u, pj, st, summ in rows:                # via the bridge, like every other statement
            db.run(con, insert(_S).values(session_id=sid, source="session", user=u, project=pj,
                                          started_at=st, summary=summ, n_user=2, n_asst=2))
        con.commit()

        def q(query, principal=None, variables=None, mode="org"):
            return run_query(query, variables, con=con, principal=principal or {"email": "mara@example.com",
                                                                              "org_id": "default",
                                                                              "role": "member"},
                           org_id=None, auth_mode=mode)

        # --- search by username: a SUBSTRING, case-insensitively -----------------------------------
        r = q('{ sessions(user: "mara") { items { sessionId user } totalCount } }')
        assert not r.get("errors"), r
        got = {i["sessionId"] for i in r["data"]["sessions"]["items"]}
        assert got == {"s1", "s3"}, f"'mara' must find both casings of the address, got {got}"
        assert r["data"]["sessions"]["totalCount"] == 2

        r = q('{ sessions(user: "MARA@EXAMPLE.COM") { items { sessionId } } }')
        assert {i["sessionId"] for i in r["data"]["sessions"]["items"]} == {"s1", "s3"}, "the query is folded too"

        r = q('{ sessions(user: "jules") { items { sessionId user } } }')
        assert [i["sessionId"] for i in r["data"]["sessions"]["items"]] == ["s4"], \
            "an OS-username identity (no domain) must be findable by the same search"

        r = q('{ sessions(user: "nobody") { items { sessionId } totalCount } }')
        assert r["data"]["sessions"]["items"] == [] and r["data"]["sessions"]["totalCount"] == 0

        # a typed wildcard is a literal, not a match-everything
        r = q('{ sessions(user: "%") { totalCount } }')
        assert r["data"]["sessions"]["totalCount"] == 0, "an unescaped % would have matched every session"

        # --- other filters, and they compose --------------------------------------------------------
        r = q('{ sessions(project: "csa") { items { sessionId } } }')
        assert {i["sessionId"] for i in r["data"]["sessions"]["items"]} == {"s1", "s3"}
        r = q('{ sessions(user: "mara", project: "ledger") { totalCount } }')
        assert r["data"]["sessions"]["totalCount"] == 0, "filters AND together"
        r = q('{ sessions(q: "502") { items { sessionId } } }')
        assert [i["sessionId"] for i in r["data"]["sessions"]["items"]] == ["s1"], "q searches the summary"
        r = q('{ sessions(q: "jules") { items { sessionId user } } }')
        assert [i["sessionId"] for i in r["data"]["sessions"]["items"]] == ["s4"], \
            "the free-text box must find a PERSON: it offers 'person' first, and a name is what gets typed"
        r = q('{ sessions(q: "ledger") { items { sessionId } } }')
        assert [i["sessionId"] for i in r["data"]["sessions"]["items"]] == ["s2"], "q searches the project too"

        # --- pagination: a total order, and a walk that visits each row once ------------------------
        seen, cur, pages = [], None, 0
        while True:
            r = run_query('query($c: String) { sessions(limit: 2, cursor: $c) '
                        '{ items { sessionId } hasMore nextCursor totalCount } }',
                        {"c": cur}, con=con, principal={"email": "mara@example.com", "role": "member"},
                        org_id=None, auth_mode="org")
            pg = r["data"]["sessions"]
            seen += [i["sessionId"] for i in pg["items"]]
            pages += 1
            assert pg["totalCount"] == 4
            if not pg["hasMore"]:
                assert pg["nextCursor"] is None
                break
            cur = pg["nextCursor"]
            assert pages < 5, "pagination did not terminate"
        assert seen == ["s1", "s2", "s3", "s4"], f"newest first, each exactly once: {seen}"
        assert pages == 2

        # --- transcripts: paged, and gated per caller -----------------------------------------------
        archive.put({"session_id": "s1", "user": "mara@example.com", "redaction_mode": "secrets",
                     "turns": ["USER: x"], "messages": [
                         {"role": "user", "blocks": [{"type": "text", "text": "run the tests"}]},
                         {"role": "assistant", "blocks": [
                             {"type": "thinking", "text": "pytest then"},
                             {"type": "tool_use", "name": "Bash", "tool_id": "t1",
                              "input": '{"command":"pytest -q"}'},
                             {"type": "tool_result", "tool_id": "t1", "text": "277 passed",
                              "is_error": False},
                             {"type": "text", "text": "all green"}]}]})

        r = q('{ transcript(sessionId: "s1") { items { index type role toolName text isError } '
              'totalCount hasMore retentionDays redactionMode } }')
        t = r["data"]["transcript"]
        assert [b["type"] for b in t["items"]] == ["text", "thinking", "tool_use", "tool_result", "text"], t
        assert t["totalCount"] == 5 and t["retentionDays"] == 30 and t["redactionMode"] == "secrets"
        tu = [b for b in t["items"] if b["type"] == "tool_use"][0]
        assert tu["toolName"] == "Bash" and "pytest" in tu["text"], "a tool call carries its name and input"
        assert [b["role"] for b in t["items"]] == ["user"] + ["assistant"] * 4

        # paging a transcript keeps a stable global index
        r = q('{ transcript(sessionId: "s1", limit: 2) { items { index type } hasMore nextCursor } }')
        p1 = r["data"]["transcript"]
        assert [b["index"] for b in p1["items"]] == [0, 1] and p1["hasMore"]
        r = run_query('query($c: String) { transcript(sessionId: "s1", limit: 2, cursor: $c) '
                    '{ items { index type } hasMore } }', {"c": p1["nextCursor"]},
                    con=con, principal={"email": "mara@example.com", "role": "member"},
                    org_id=None, auth_mode="org")
        p2 = r["data"]["transcript"]
        assert [b["index"] for b in p2["items"]] == [2, 3], "index is the position in the WHOLE transcript"

        # a teammate gets an empty page, indistinguishable from a session that has none
        mate = {"email": "tomasz@example.com", "role": "member"}
        r = q('{ transcript(sessionId: "s1") { items { index } totalCount } }', principal=mate)
        assert r["data"]["transcript"]["items"] == [] and r["data"]["transcript"]["totalCount"] == 0
        missing = q('{ transcript(sessionId: "nope") { items { index } totalCount } }', principal=mate)
        assert r["data"] == missing["data"], "'not yours' must look exactly like 'no such session'"

        # ...and hasTranscript on the listing tells each caller the truth about THEMSELVES
        r = q('{ sessions(user: "mara@example.com") { items { sessionId hasTranscript } } }')
        assert {i["sessionId"]: i["hasTranscript"] for i in r["data"]["sessions"]["items"]}["s1"] is True
        r = q('{ sessions(user: "mara@example.com") { items { sessionId hasTranscript } } }', principal=mate)
        assert all(i["hasTranscript"] is False for i in r["data"]["sessions"]["items"]), \
            "a teammate must not be told a transcript is available to them"

        # loopback dev is open; in org mode a self-host shared secret is every client's credential
        # (see may_read_transcript), while a named workspace admin reads
        admin = {"email": None, "role": "admin"}
        r = q('{ transcript(sessionId: "s1") { totalCount } }', principal=admin, mode="off")
        assert r["data"]["transcript"]["totalCount"] == 5
        r = q('{ transcript(sessionId: "s1") { totalCount } }', principal=admin, mode="org")
        assert r["data"]["transcript"]["totalCount"] == 0, "no shared-secret bypass in org mode"
        boss = {"email": "boss@example.com", "role": "admin"}
        r = q('{ transcript(sessionId: "s1") { totalCount } }', principal=boss, mode="org")
        assert r["data"]["transcript"]["totalCount"] == 5, "a named workspace admin reads"

        # --- facts: the global search box, and the page it feeds -------------------------------------
        # Seeded through the real writer so the FTS index and the usage joins are the real ones.
        from analyzer import store as _store
        for i, (sid, claim, ty) in enumerate([
                ("s1", "WAL must be set at connect time or a long writer 502s the server", "gotcha"),
                ("s3", "custom entity types apply only to sessions extracted afterwards", "constraint"),
                ("s4", "more workers makes SQLite writer contention worse", "gotcha")]):
            db.run(con, insert(_HC).values(session_id=sid, type=ty, claim=claim,
                                           evidence="ev", ref=f"ref{i}", confidence="high"))
        con.commit()
        _store.build_fts(con)

        r = q("{ facts(limit: 10) { totalCount items { ref type claim user } coverage { terms } } }")
        assert not r.get("errors"), r
        f = r["data"]["facts"]
        assert f["totalCount"] == 3, f
        assert {i["type"] for i in f["items"]} == {"gotcha", "constraint"}

        r = q('{ facts(type: "gotcha") { totalCount } }')
        assert r["data"]["facts"]["totalCount"] == 2, "type narrows"

        r = q('{ facts(owner: "jules") { items { user claim } totalCount } }')
        assert r["data"]["facts"]["totalCount"] == 1, "owner is the Mine/Everyone scope"

        r = q('{ facts(q: "SQLite") { items { claim } coverage { missing distinctive terms } } }')
        got = r["data"]["facts"]
        assert any("SQLite" in i["claim"] for i in got["items"]), got
        assert got["coverage"] is not None, "coverage rides along so the UI can admit a miss"

        r = q('{ facts(q: "zzzznotinthecorpus") { totalCount coverage { missing } } }')
        assert r["data"]["facts"]["totalCount"] == 0
        assert "zzzznotinthecorpus" in (r["data"]["facts"]["coverage"]["missing"] or []), \
            "a term the corpus has never seen must be reported, not silently ranked around"

        # every field the Memory page renders must be selectable: a subset would blank the labels and
        # the supersession banner while still looking like a working search
        r = q("{ facts(limit: 1) { items { ref type claim evidence project sessionId user startedAt "
              "label flagger corroboration used dominant judgeGold author domain createdAt validAt "
              "invalidAt state clusterId supersededBy supersededAt } } }")
        assert not r.get("errors"), r

        # paging facts walks the whole set once
        seen, cur, pages = [], None, 0
        while True:
            rr = run_query('query($c: String) { facts(limit: 2, cursor: $c) '
                           '{ items { ref } hasMore nextCursor totalCount } }', {"c": cur},
                           con=con, principal={"email": "mara@example.com", "role": "member"},
                           org_id=None, auth_mode="org")
            pg = rr["data"]["facts"]
            seen += [i["ref"] for i in pg["items"]]
            pages += 1
            if not pg["hasMore"]:
                break
            cur = pg["nextCursor"]
            assert pages < 5, "fact pagination did not terminate"
        assert len(seen) == 3 and len(set(seen)) == 3, f"each fact exactly once: {seen}"

        # --- a bad query is a GraphQL error, never a 500 ---------------------------------------------
        r = q("{ nope }")
        assert r.get("errors") and "nope" in r["errors"][0]["message"]
        r = q("this is not graphql")
        assert r.get("errors"), "a syntax error returns errors, it does not raise"

        assert "type SessionPage" in sdl() and "nextCursor" in sdl()
        print("OK gql: username search is a case-insensitive substring (typed wildcards escaped); "
              "filters compose; a paged walk is totally ordered and visits each row once; transcript "
              "blocks carry tools + a whole-transcript index; per-caller authz on both the page and "
              "hasTranscript; no shared-secret bypass in org mode; bad queries return errors")
    finally:
        os.chdir(cwd)
        import shutil
        shutil.rmtree(wd, ignore_errors=True)
