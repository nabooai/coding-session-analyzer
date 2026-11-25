"""LLM labeler for mined facts ('machines label, humans arbitrate') — ONE batched haiku pass assigns BOTH
the type (decision/gotcha/constraint/todo/preference -> hc_types pin, which doubles as the judged-marker)
and a quality label: gold -> signals(judge_gold), a capped rerank boost that never outranks human gold;
wrong/noise -> reviews (flagger='judge', hidden by build_fts, reversible via flag 'clear');
unimportant -> reviews (down-ranked x0.6); unsure -> untouched, stays in the human queue.
Same `claude -p` calling pattern as extract.py."""
import json, time

from sqlalchemy import and_, bindparam, exists, func, insert, literal, select, update

from analyzer import db, llm
from analyzer.models import (ClaimCluster, Fact, HcType, Review, SessionRow, Signal,
                             hidden_context)

_HC, _HT = hidden_context, HcType.__table__
_RV, _SIG = Review.__table__, Signal.__table__
_CC_T = ClaimCluster.__table__

TYPES = ("decision", "gotcha", "constraint", "todo", "preference")
LABELS = ("gold", "ok", "unimportant", "wrong", "noise", "unsure")

# opening prefix must keep matching ingest.INTERNAL_PROMPTS (startswith — self-mining guard)
PROMPT = """You review facts mined from Claude Code sessions. For EACH fact decide:
- type: one of decision|gotcha|constraint|todo|preference
- label: "gold" = specific, true, durable engineering knowledge that is likely undocumented — high recall value;
  "ok" = a real, durable piece of project/team context, but ordinary (the default);
  "unimportant" = true but low value (kept, ranked lower);
  "wrong" = false, hallucinated, or clearly stale;
  "noise" = not a durable fact at all (chit-chat, transient state, tool output);
  "unsure" = a human should decide.
A fact may carry "sessions" = how many DISTINCT sessions independently rediscovered it — corroborated
facts are stronger "gold" candidates and are rarely "noise".
Be conservative: prefer "ok" over "gold", and "unsure" over "wrong".
Return ONLY a JSON array (no prose, no markdown fence):
[{"ref": "...", "type": "...", "label": "..."}]

FACTS:
"""


# shares PROMPT's opening prefix: the ingest.INTERNAL_PROMPTS startswith guard covers this call too
STALE_PROMPT = """You review facts mined from Claude Code sessions. Below are CLUSTERS of near-duplicate
facts about the same finding, each fact with its session date, ordered oldest first.
Mark a fact "stale" ONLY when a NEWER fact in the same cluster supersedes or contradicts it (the state it
describes no longer holds). Restating the same still-true finding is NOT stale — that reaffirms it.
Return ONLY a JSON array of the stale facts (empty array if none), no prose, no markdown fence:
[{"ref": "..."}]

CLUSTERS:
"""


def _extract_array(text):
    i, j = text.find("["), text.rfind("]")     # bracket-slice ignores ``` fences & prose (mirrors extract.py)
    if i == -1 or j == -1:
        raise ValueError("no JSON array in model output")
    return json.loads(text[i:j + 1])


def judge_batch(facts, model="small", timeout=300, prompt=None):
    """facts = [{ref, claim, evidence}] -> (verdicts, cost_meta) via ONE model call (analyzer.llm)."""
    text, cost_usd, usage = llm.complete((prompt or PROMPT) + json.dumps(facts, indent=1), model, timeout)
    return _extract_array(text), {"cost_usd": cost_usd, "usage": usage}


def pending(con, limit=None, session=None):
    """Unlabeled facts, dedup by ref: no type pin, no review row (every labeled fact gets one or the other,
    so nothing is re-billed). Extractor confidence is NOT a gate — quality labeling covers the whole corpus.
    `session` scopes to one session's facts (the auto-judge path: label every fact as it is ingested)."""
    cc = _CC_T.alias("cc")
    stmt = (select(_HC.c.ref, func.min(_HC.c.claim), func.min(_HC.c.evidence),
                   func.ifnull(func.min(cc.c.sessions), 1))
            .select_from(_HC
                .outerjoin(_HT, _HT.c.ref == _HC.c.ref)
                .outerjoin(_RV, and_(_RV.c.kind == "hc", _RV.c.ref == _HC.c.ref))
                .outerjoin(cc, cc.c.ref == _HC.c.ref))   # broad view for the batch/ETL pass: cross-session
            .where(_HT.c.ref.is_(None), _RV.c.ref.is_(None),   # corroboration goes into the judge's prompt
                   ~exists(select(literal(1)).select_from(_SIG)
                           .where(_SIG.c.ref == _HC.c.ref, _SIG.c.kind == "human_cleared"))))
    #      ^ a human cleared a judge label on this ref: never machine-label it again (humans win rounds)
    if session:
        stmt = stmt.where(_HC.c.session_id == session)
    stmt = stmt.group_by(_HC.c.ref)
    if limit:
        stmt = stmt.limit(int(limit))
    return [{"ref": r, "claim": c, "evidence": e, **({"sessions": s} if s > 1 else {})}
            for r, c, e, s in db.run(con, stmt)]


MAX_HIDE_RATE = 0.6          # ponytail: plausibility guard — one bad/injected response must not hide a whole batch


def apply(con, known, verdicts, at):
    """Write one batch's verdicts. Hallucinated refs are ignored; bad type/label counts as unsure (stays
    queued). Guards: a batch (>=5 facts) whose verdicts would hide more than MAX_HIDE_RATE of it is
    skipped whole (misfire or prompt-injected transcript text — facts stay queued); judge writes are
    insert-if-absent, so a human label/type-pin written while the batch's LLM call ran is never
    overwritten. Returns {label: count}."""
    n = dict.fromkeys(LABELS, 0)
    good = [v for v in verdicts if v.get("ref") in known]      # hallucinated refs -> ignored
    hides = sum(1 for v in good if v.get("label") in ("wrong", "noise"))
    if len(known) >= 5 and hides > MAX_HIDE_RATE * len(known):
        print(f"judge batch SKIPPED: {hides}/{len(known)} verdicts would hide facts (misfire/injection guard)")
        return n
    for v in good:
        ref, ty, label = v["ref"], v.get("type"), v.get("label")
        if ty not in TYPES or label not in LABELS or label == "unsure":
            n["unsure"] += 1                       # stays in the human queue
            continue
        if db.run(con, select(literal(1)).select_from(_RV)
                  .where(_RV.c.kind == "hc", _RV.c.ref == ref)).fetchone():
            continue                               # a human labeled it mid-run — their row stands, not counted
        if label in ("wrong", "noise"):            # INSERT OR IGNORE: never REPLACE a concurrent human row
            db.run(con, insert(_RV).prefix_with("OR IGNORE")
                   .values(kind="hc", ref=ref, status=label, note="judge", flagger="judge"))
        else:
            if not db.run(con, select(literal(1)).select_from(_HT)
                          .where(_HT.c.ref == ref)).fetchone():
                db.run(con, insert(_HT).values(ref=ref, type=ty))   # pin = judged marker
                db.run(con, update(_HC).values(type=ty).where(_HC.c.ref == ref))
                db.run(con, update(Fact.__table__).values(type=ty)
                       .where(Fact.__table__.c.ref == ref))   # mirror like set_hc_type —
                #   else the durable fact row drifts from its retyped mentions until the next re-touch
            db.run(con, insert(_SIG).prefix_with("OR IGNORE")
                   .values(ref=ref, kind="judge_accept", session_id="judge", at=at))
            if label == "gold":                    # capped rerank boost (search._signal_factors), < human gold
                db.run(con, insert(_SIG).prefix_with("OR IGNORE")
                       .values(ref=ref, kind="judge_gold", session_id="judge", at=at))
            elif label == "unimportant":
                db.run(con, insert(_RV).prefix_with("OR IGNORE")
                       .values(kind="hc", ref=ref, status="unimportant",
                               note="judge", flagger="judge"))                # x0.6 rerank
        n[label] += 1
    con.commit()             # close the write txn NOW — the next judge_batch blocks other writers for ~30s otherwise
    return n


def run(con, limit=None, model="small", batch=20, session=None):
    """Label pending facts in batches of ~20/LLM call. Returns {label: count}."""
    from analyzer import store
    facts = pending(con, limit, session=session)
    totals = dict.fromkeys(LABELS, 0)
    touched = set()                                # refs whose label/type moved -> their docs rows are stale
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i in range(0, len(facts), batch):
        chunk = facts[i:i + batch]
        try:
            verdicts, meta = judge_batch(chunk, model=model)
            store.record_cost(con, "judge", model, None, meta["usage"], meta["cost_usd"])
        except Exception as e:
            print(f"judge batch {i // batch} failed ({str(e)[:120]}) — skipped, facts stay queued")
            continue
        applied = apply(con, {f["ref"] for f in chunk}, verdicts, at)
        for k, v in applied.items():
            totals[k] += v
        if sum(applied[k] for k in LABELS if k != "unsure"):
            touched |= {f["ref"] for f in chunk}
    if touched:
        # A label/retype changes a fact's docs row (status hides it, etype+text prefix carry the type). Refresh
        # THOSE rows, not the corpus: this ran store.build_fts — write_entity_stats + DROP/CREATE docs, ~3 min on
        # the prod corpus — once per judged session on the ETL path, which capped the drain at ~15 sessions/hour
        # (2026-09-01) for a handful of labels. Same incremental path index_session uses; a cold db with no
        # FTS yet builds the whole corpus once, exactly like index_session.
        if store.has_docs(con):
            store.refresh_fact_docs(con, touched)
        else:
            store.build_fts(con)
    con.commit()
    return totals


def _stale_candidates(con):
    """Multi-fact clusters worth a staleness check: >=2 distinct refs, at least one member not yet checked
    (a new member re-opens the whole cluster — it may supersede an already-checked older fact). Returns
    {cluster_id: [(ref, claim, date), ...oldest first]}."""
    cc, s = _CC_T.alias("cc"), SessionRow.__table__.alias("s")
    date_expr = func.ifnull(func.min(s.c.started_at), "")
    multi = (select(_CC_T.c.cluster_id).group_by(_CC_T.c.cluster_id)
             .having(func.count(func.distinct(_CC_T.c.ref)) >= 2))
    rows = db.run(con, select(cc.c.cluster_id, cc.c.ref, func.min(_HC.c.claim), date_expr)
        .select_from(cc
            .join(_HC, _HC.c.ref == cc.c.ref)
            .outerjoin(s, s.c.session_id == _HC.c.session_id)
            .outerjoin(_RV, and_(_RV.c.kind == "hc", _RV.c.ref == cc.c.ref)))
        .where(_RV.c.ref.is_(None),
               ~exists(select(literal(1)).select_from(_SIG)
                       .where(_SIG.c.ref == cc.c.ref, _SIG.c.kind == "human_cleared")),
               cc.c.cluster_id.in_(multi))
        .group_by(cc.c.cluster_id, cc.c.ref)
        .order_by(date_expr, cc.c.ref)).fetchall()       # ref tiebreak: stable 'newest' member
    checked = {r for (r,) in db.run(con, select(_SIG.c.ref)
                                    .where(_SIG.c.kind == "etl_checked"))}
    by_cluster = {}
    for cid, ref, claim, date in rows:
        by_cluster.setdefault(cid, []).append((ref, claim, date))
    return {cid: mem for cid, mem in by_cluster.items()
            if len(mem) >= 2 and any(ref not in checked for ref, _c, _d in mem)}


def etl_pass(con, model="small", batch=8):
    """The batch/ETL side of labeling — ONLY verification + staleness (initial labeling is the live,
    per-ingest pass):
    1. REAFFIRM (no LLM): every fact whose claim-cluster spans >=2 sessions was independently
       rediscovered -> signals('reaffirmed'). Rank already reflects this via the corroboration boost;
       the signal is the durable audit record.
    2. STALENESS (haiku): near-dup clusters oldest->newest; the judge marks older members SUPERSEDED or
       CONTRADICTED by newer ones -> reviews 'wrong' (note='stale', flagger='judge', hidden, reversible).
       Guard: the newest member of a cluster is never staled. All sent refs get signals('etl_checked')
       so an unchanged cluster is never re-billed. Returns {reaffirmed, stale, clusters}."""
    from analyzer import store
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = con.total_changes
    db.run(con, insert(_SIG).prefix_with("OR IGNORE").from_select(
        ["ref", "kind", "session_id", "at"],
        select(_CC_T.c.ref, literal("reaffirmed"), literal("etl"), literal(at))
        .where(_CC_T.c.sessions >= 2)))
    reaff = con.total_changes - t0
    cands = _stale_candidates(con)
    stale = 0
    items = list(cands.items())
    for i in range(0, len(items), batch):
        chunk = dict(items[i:i + batch])
        payload = [{"cluster": cid, "facts": [{"ref": r, "claim": c, "date": d[:10]} for r, c, d in mem]}
                   for cid, mem in chunk.items()]
        newest = {mem[-1][0] for mem in chunk.values()}            # never stale the newest member
        undated = {r for mem in chunk.values() for r, _c, d in mem if not d}   # unordered -> un-staleable
        known = {r for mem in chunk.values() for r, _c, _d in mem}
        try:
            verdicts, meta = judge_batch(payload, model=model, prompt=STALE_PROMPT)
            store.record_cost(con, "judge-etl", model, None, meta["usage"], meta["cost_usd"])
        except Exception as e:
            print(f"etl batch {i // batch} failed ({str(e)[:120]}) — skipped, clusters stay unchecked")
            continue
        hits = {v.get("ref") for v in verdicts} & known
        if len(known) >= 5 and len(hits) > MAX_HIDE_RATE * len(known):
            print(f"etl batch SKIPPED: {len(hits)}/{len(known)} marked stale (misfire/injection guard)")
            continue                                               # clusters stay unchecked for the next sweep
        for ref in hits - newest - undated:                        # INSERT OR IGNORE: a concurrent human row wins
            t0 = con.total_changes
            db.run(con, insert(_RV).prefix_with("OR IGNORE")
                   .values(kind="hc", ref=ref, status="wrong", note="stale", flagger="judge"))
            stale += con.total_changes - t0
        db.run_many(con, insert(_SIG).prefix_with("OR IGNORE")
                    .values(ref=bindparam("ref"), kind=bindparam("kind"),
                            session_id=bindparam("session_id"), at=bindparam("at")),
                    [{"ref": r, "kind": "etl_checked", "session_id": "etl", "at": at} for r in known])
        con.commit()          # same: never hold the write lock across the next batch's LLM call
    if stale:
        store.build_fts(con)                                       # stale facts leave the index
    con.commit()
    return {"reaffirmed": reaff, "stale": stale, "clusters": len(cands)}
