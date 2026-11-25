"""Usage signals — machine-detected evidence a fact matters, harvested at index time from the raw transcript:
the session's DOMINANT entity, facts RECALLED into a session, and facts ACTED ON after recall. Written to
signals(ref,kind,session_id): the 'usage confirms' leg of machines-label / usage-confirms / humans-arbitrate."""
import re, time

from sqlalchemy import and_, func, insert, literal_column, select, update

from analyzer import db
from analyzer.models import Signal, hidden_context, mentions, queries, served

_SIG = Signal.__table__

_REF = re.compile(r"id:\s?([0-9a-f]{12})")           # matches app.recall's "· id:<ref>" lines
_MARKER = "# Recalled context for"                   # app.recall's output header


def dominant_entities(sess, data):
    """[(entity_id, name)] for the top-2 entities whose id/name dominates the transcript: share >= 0.4 of all
    entity occurrences AND raw count >= 3 (~3 turns). Case-insensitive substring; terms <4 chars skipped (noise)."""
    tl = (sess.get("transcript") or "").lower()
    counts = {}
    for e in data.get("entities") or []:
        eid = (e.get("id") or "").strip()
        if not eid:
            continue
        name = (e.get("name") or eid).strip()
        terms = {t.lower() for t in (eid, name) if len(t) >= 4}
        c = max((tl.count(t) for t in terms), default=0)   # max, not sum: name is often == id
        if c:
            counts[eid] = (c, name)
    total = sum(c for c, _ in counts.values())
    if not total:
        return []
    top = sorted(counts.items(), key=lambda kv: -kv[1][0])[:2]
    return [(eid, name) for eid, (c, name) in top if c / total >= 0.4 and c >= 3]


def recalled_refs(transcript):
    """[(ref, end_pos)] for fact ids in recall output.
    # ponytail: scans everything after the FIRST marker — id:<12hex> only appears on recall lines."""
    pos = (transcript or "").find(_MARKER)
    if pos == -1:
        return []
    return [(m.group(1), m.end()) for m in _REF.finditer(transcript, pos)]


def acted_on(transcript, pos, claim):
    """Dumb acted-on check: >=2 distinctive claim tokens (len>6, stopwords self-filter by length)
    appear AFTER the recall position."""
    toks = {t for t in re.findall(r"[a-z0-9_.\-]+", (claim or "").lower()) if len(t) > 6}
    tail = transcript[pos:].lower()
    return sum(1 for t in toks if t in tail) >= 2


def _hook_acted(con, sid, transcript):
    """Refs this session's AMBIENT-HOOK injections served and then visibly used. recalled_refs() cannot see
    them: ingest keeps the recall marker only when it arrives as an MCP tool_result (ingest.py `_text_from_content`),
    but Claude Code writes a UserPromptSubmit injection as an `attachment` event with no `message`, which
    `_session_from_lines` skips outright (across the local corpus: 343 marker occurrences were attachments vs
    308 tool_results). The USER PROMPT that triggered the recall does survive, and `served.question` IS that
    prompt, so anchor acted_on() there instead.
    # ponytail: prefix-match the first 80 chars — question and turn are redacted separately, so only the
    #           head is reliably identical; prompts under 12 chars anchor anywhere and are skipped.
    # ponytail: NOT written to `signals` — those rows feed search.py's usage rank boost, and this is
    #           measurement only. Upgrade path: if the hook path ever needs to influence rank, add a
    #           distinct kind and give it its own (smaller) boost rather than reusing 'acted_on'."""
    tl = (transcript or "").lower()
    out = set()
    for question, ref, claim in db.run(con, select(
            served.c.question, served.c.ref, hidden_context.c.claim).distinct()
            .select_from(served.join(hidden_context, hidden_context.c.ref == served.c.ref))
            .where(served.c.session == sid)):
        q = (question or "").strip().lower()[:80]
        pos = tl.find(q) if len(q) >= 12 else -1
        if pos >= 0 and acted_on(transcript, pos + len(q), claim):
            out.add(ref)
    return out


def _mark_queries(con, sid, refs):
    """Write-back the schema always promised (store.py: "acted_on set later by the signal harvester") and
    nothing ever did: flip queries.acted_on for the ask row that actually SERVED an acted-on ref.

    JOIN: queries.session == served.session AND queries.query_text == served.question — app.recall logs the
    served refs and apiserver.ask logs the query row from the SAME redacted topic string, so the text is an
    exact key; attributing to every query in the session instead would inflate the rate.
    TIE-BREAK: a session that asked the identical question twice flips only the EARLIEST row (MIN(rowid) ==
    insertion order == chronological) — that injection is what put the fact in front of the model; a re-serve
    of context already present adds nothing.
    IDEMPOTENT: the same row is chosen on every re-index and only ever set to 1; never 1 -> 0.

    HONESTY — read the resulting rate as a weak LOWER BOUND, never as ground truth: `acted_on` is token
    overlap (>=2 distinctive claim tokens after the recall), so it undercounts silent use (the model reads a
    fact and quietly avoids a mistake) and over-counts echoed jargon (the words were going to appear anyway).
    It also only sees sessions that were shipped AND indexed, and the transcript is head+tail truncated."""
    if not refs:
        return
    q, s = queries.alias("q"), served.alias("s")
    earliest = (select(func.min(literal_column("q.rowid")))
                .select_from(q.join(s, and_(s.c.session == q.c.session,
                                            s.c.question == q.c.query_text)))
                .where(q.c.session == sid, s.c.ref.in_(sorted(refs)))
                .group_by(q.c.query_text))
    db.run(con, update(queries).values(acted_on=1)
           .where(literal_column("rowid").in_(earliest)))


def backfill(con):
    """Opt-in one-shot (`csa.py queries --backfill`) for sessions indexed BEFORE this write-back existed.
    Only the marker/MCP path can be recovered: raw transcripts are not stored, so `_hook_acted`'s prompt
    anchor cannot be recomputed after the fact — historical hook rows stay 0 until those sessions re-index.
    Returns the number of `queries` rows flipped."""
    flipped = select(func.count()).select_from(queries).where(queries.c.acted_on == 1)
    n = db.run(con, flipped).fetchone()[0]
    for (sid,) in db.run(con, select(_SIG.c.session_id).distinct()
                         .where(_SIG.c.kind == "acted_on")).fetchall():
        _mark_queries(con, sid, {r for (r,) in db.run(con, select(_SIG.c.ref)
                      .where(_SIG.c.kind == "acted_on", _SIG.c.session_id == sid))})
    con.commit()
    return db.run(con, flipped).fetchone()[0] - n


def harvest(con, sess, data):
    """Single entry point (called from store.index_session): write this session's usage signals."""
    sid, transcript = sess["session_id"], sess.get("transcript") or ""
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def sig(ref, kind):
        db.run(con, insert(_SIG).prefix_with("OR IGNORE")
               .values(ref=ref, kind=kind, session_id=sid, at=at))

    for eid, name in dominant_entities(sess, data):
        db.run(con, update(mentions).values(dominant=1)
               .where(mentions.c.session_id == sid, mentions.c.entity_id == eid))
        for (ref,) in db.run(con, select(hidden_context.c.ref)   # facts of THIS session mentioning it
                .where(hidden_context.c.session_id == sid,
                       func.lower(func.ifnull(hidden_context.c.claim, "") + " " +
                                  func.ifnull(hidden_context.c.evidence, ""))
                       .like(f"%{name.lower()}%"))):
            sig(ref, "dominant_entity")
    acted = set()
    for ref, pos in recalled_refs(transcript):
        sig(ref, "recalled_in_session")
        row = db.run(con, select(hidden_context.c.claim)
                     .where(hidden_context.c.ref == ref).limit(1)).fetchone()
        if row and acted_on(transcript, pos, row[0]):
            sig(ref, "acted_on")
            acted.add(ref)
    _mark_queries(con, sid, acted | _hook_acted(con, sid, transcript))   # the "usage confirms" leg, per ASK
