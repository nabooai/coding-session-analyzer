"""coding-session-analyzer CLI:  extract | index | build | search | answer | ask | stats | entities | merge | review | classify | screen | retype | grade | judge"""
import argparse, contextlib, os, json, sqlite3, collections, logging, sys, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy import bindparam, func, select, text, update
from analyzer import ingest, extract, store, ask, search, memories, harvest, queue, incremental, entity_types, tracer
from analyzer import db
from analyzer import models as _m

log = logging.getLogger("csa")          # operational logs; cmd_* still print() user-facing results to stdout


def _out(sid):
    return str(incremental.extract_dir() / f"{sid}.json")


def _stale(s):
    o = _out(s["session_id"])      # re-extract if never done OR the .jsonl grew since (append-only)
    return not os.path.exists(o) or os.path.getmtime(s["path"]) > os.path.getmtime(o)


def coverage_stats():
    """Extraction coverage for `extract --dry-run`, by the same _stale() predicate that gates extract."""
    os.makedirs(incremental.extract_dir(), exist_ok=True)
    sessions = ingest.load_sessions(limit=None)
    have = [s for s in sessions if os.path.exists(_out(s["session_id"]))]
    stale = [s for s in have if _stale(s)]                          # cached but the .jsonl grew since
    missing = [s for s in sessions if not os.path.exists(_out(s["session_id"]))]
    return {"ingested": len(sessions), "covered": len(have) - len(stale), "stale_n": len(stale),
            "missing_n": len(missing), "stale": stale, "missing": missing,
            "sids": {s["session_id"] for s in sessions}}


def _coverage():
    """The explicit 'place' that shows extraction coverage: which sessions are covered / stale / missing."""
    c = coverage_stats()
    print(f"coverage: {c['ingested']} sessions | {c['covered']} covered | {c['stale_n']} stale | {c['missing_n']} missing")
    for s in c["stale"] + c["missing"]:
        why = "stale (jsonl grew)" if os.path.exists(_out(s["session_id"])) else "missing"
        print(f"  TO-EXTRACT  {s['session_id'][:12]}  {(s['project'] or '-')[:30]:30}  {why}")
    orphan = [f[:-5] for f in os.listdir(incremental.extract_dir()) if f.endswith(".json") and f[:-5] not in c["sids"]]
    if orphan:
        print(f"  ({len(orphan)} cached extractions whose source .jsonl is gone — orphaned)")
    todo = c["stale_n"] + c["missing_n"]
    print(f"\n`csa extract` will cover the {todo} above" + ("" if todo else " — all in scope ✓") +
          "; `--force` re-extracts everything.")


def _ro():
    return store.ro_connect(store.org_db())   # LLM-planner path: read-only + ATTACH denied (cross-file read guard)


# --- pipeline stages (extract is expensive/LLM; index is cheap/replayable) ---
def _extract_sessions(sessions, workers, model):
    """Extract `sessions` to the cache in parallel, each written atomically; returns the cost. Shared by
    `do_extract` and recall's cold fill."""
    os.makedirs(incremental.extract_dir(), exist_ok=True)
    cost, errors = 0.0, []
    ct = entity_types.custom(store.sqlite_db())
    tctx = tracer.capture()                               # pool threads join the caller's trace

    def work(s):
        with tracer.resume(tctx), tracer.span("extract", "CHAIN", **{"session.id": s["session_id"]}):
            for attempt in range(2):                      # retry once on transient CLI/JSON failure
                try:
                    d = extract.extract(s["transcript"], model=model, custom_types=ct)
                    break
                except Exception:
                    if attempt:
                        raise
        tmp = _out(s["session_id"]) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, _out(s["session_id"]))
        return s, d

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(work, s): s for s in sessions}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                _, d = fut.result()
                cost += d.get("_cost_usd") or 0
                store.record_cost(store.sqlite_db(), "extract", model, s["session_id"],
                                  d.get("_usage"), d.get("_cost_usd"))
                log.info("[%d/%d] %-14s hidden=%d ent=%d rel=%d $%.3f", i, len(sessions), s['project'][:14],
                         len(d.get('hidden_context', [])), len(d.get('entities', [])),
                         len(d.get('relations', [])), d.get('_cost_usd') or 0)
            except Exception as e:
                errors.append(s["session_id"])
                log.warning("ERR %-14s %s", s['project'][:14], str(e)[:80])
    if sessions:
        log.info("extraction cost ~$%.2f%s", cost, f"; FAILED {len(errors)}: {errors}" if errors else "")
    return cost


def do_extract(limit, workers, model, force=False):
    sessions = ingest.load_sessions(limit=limit)
    log.info("ingested %d sessions", len(sessions))
    todo = sessions if force else [s for s in sessions if _stale(s)]
    log.info("%d fresh, extracting %d via --model %s", len(sessions) - len(todo), len(todo), model)
    _extract_sessions(todo, workers, model)
    return sessions


def do_index(sessions=None):
    if sessions is None:
        sessions = ingest.load_sessions(limit=None)
    con = store.sqlite_db()

    # pass 1: collect canonicalized records + per-entity type votes (LLM types are inconsistent across sessions)
    records, votes, n, m, bad = [], {}, 0, 0, 0

    def collect(sess, d, is_mem):
        nonlocal n, m
        d = store.canonicalize(con, d)
        for e in d.get("entities", []):
            eid, ty = (e.get("id") or "").strip(), e.get("type")
            if eid and ty:
                votes.setdefault(eid, collections.Counter())[ty] += 1
        records.append((sess, d))
        if is_mem:
            m += 1
        else:
            n += 1

    for s in sessions:                                   # (a) extracted transcript sessions
        f = _out(s["session_id"])
        if not os.path.exists(f):
            continue
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception as e:
            bad += 1
            log.warning("skip corrupt %s: %s", s['session_id'][:8], e)
            continue
        collect(s, d, False)
    for sess, d in memories.load_memories():             # (b) Claude Code curated memories (high-trust, no LLM)
        collect(sess, d, True)

    # reconcile each entity's type by MAJORITY vote across its mentions (deterministic tie-break by type name)
    majority = {eid: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for eid, c in votes.items()}
    for eid, ty in db.run(con, select(_m.EntityTypePin.id, _m.EntityTypePin.type)):   # HITL retype wins over the vote
        majority[eid] = ty

    # pass 2: load with reconciled types
    from analyzer import signals
    for sess, d in records:
        for e in d.get("entities", []):
            eid = (e.get("id") or "").strip()
            if eid in majority:
                e["type"] = majority[eid]
        store.load_sqlite(con, sess, d)
        # load_sqlite reinserts mentions with dominant=0: restore only that flag, not signals.harvest,
        # whose signals rows the live drain owns
        for eid, _name in signals.dominant_entities(sess, d):
            db.run(con, update(_m.mentions).values(dominant=1)
                   .where(_m.mentions.c.session_id == sess["session_id"],
                          _m.mentions.c.entity_id == eid))
    for s in sessions:                                   # every session, extracted or not; after pass 2, whose
        harvest.write(con, s)                            # DELETE+INSERT would wipe guarded rows
    store.refresh_facts(con)                              # sweeps zero-mention leftovers corpus-wide
    dups = store.write_clusters(con)                      # before build_fts, which bakes corroboration into docs
    store.build_fts(con)
    nfuzz = _build_fuzzy(con)
    log.info("indexed %d sessions + %d memories (majority-typed %d entities, %d near-dup clusters) into SQLite + FTS%s%s",
             n, m, len(majority), dups, f" ({bad} corrupt)" if bad else "",
             f" + fuzzy({nfuzz})" if nfuzz else "")


def _build_fuzzy(con):
    """Build the fuzzy identifier index into the ``<db>.fuzzy`` sibling. Best-effort, since it only
    accelerates resolve_everywhere. Returns the surface count (0 on skip)."""
    try:
        from analyzer import fuzzy_index as fx
        idx = fx.build_index_from_store(con)
        idx.save(fx.fuzzy_sibling(store.org_db()))
        return idx.n_surfaces
    except Exception as e:
        log.warning("fuzzy index build skipped: %s", e)
        return 0


def cmd_extract(a):
    if a.dry_run:
        return _coverage()
    do_extract(a.limit, a.workers, a.model, a.force)


def _drain_orgs(a):
    """Which orgs to drain: `--org`, else every org with a corpus (the queue is per org)."""
    if getattr(a, "org", None):
        return [store.valid_org_id(a.org)]
    from analyzer import orgs
    return orgs.corpus_orgs()


@contextlib.contextmanager
def org_drain_lock(org):
    """Non-blocking per-org lock shared by the drain and compaction: yields False when held elsewhere,
    and the caller skips. Per org, so the ETL serves every tenant in parallel."""
    import fcntl
    lockf = open(os.path.join(queue._dir(org), ".drain.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockf.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN); lockf.close()


def compact_org(org):
    """The corpus-wide pass the drain defers: re-cluster claims (corroboration) and rebuild the whole FTS.
    Both are O(corpus), so they run on a cadence. False if another drain held the org's lock."""
    with org_drain_lock(org) as got:
        if not got:
            return False
        con = store.sqlite_db(store.org_db(org))
        try:
            store.write_clusters(con)
            store.build_fts(con)
        finally:
            con.close()
        return True


def drain_org(org, model, limit, user=None, since=None, compact=True):
    """Extract and index up to `limit` of one org's queued sessions, the one drain implementation (for
    `csa.py drain` and drain_loop). Returns (done, failed, poisoned, actions, busy).

    `poisoned` is the subset of `failed` quarantined for bad content; the rest failed systemically and
    stay queued. `busy` means another drain holds the org's lock: skip, do not wait. Opens its own
    connection (the ETL calls this from a thread pool). `compact=False` leaves the corpus-wide pass to
    compact_org's cadence."""
    with org_drain_lock(org) as got:
        if not got:
            return [], [], [], {}, True
        con = store.sqlite_db(store.org_db(org))
        actions = {}

        def _extract(sess):
            data, act = incremental.extract_session(sess, model, con, org_id=org)
            actions[sess["session_id"]] = act
            return data

        def _index(sess, data):
            store.index_session(con, sess, data)
            incremental.persist(sess, data, org_id=org)           # after a successful index

        done, failed, poisoned = queue.drain_once(_extract, _index, limit=limit, org_id=org, user=user,
                                                  since=since, is_poison=lambda e: isinstance(e, ValueError))
        # A poisoned session keeps its deterministic evidence (pr-links, branches; no LLM). Only the
        # poisoned: a systemic failure stays queued, and harvesting it would rebuild the FTS every tick.
        harvested = False
        for sid in poisoned:
            sess = queue.peek(sid, org_id=org)
            if not sess:
                continue
            try:
                harvested = harvest.write(con, sess) or harvested
            except Exception as he:
                log.warning("[%s] harvest fallback failed for %s: %s", org, sid[:12], str(he)[:120])
        if (done or harvested) and compact:
            store.write_clusters(con); store.build_fts(con)
        return done, failed, poisoned, actions, False


def cmd_drain(a):
    """Drain the extraction queue by hand: what drain_loop does continuously. `--dry-run` estimates the
    spend with no LLM; otherwise up to `--limit` sessions per org, oldest first. `--org`, `--user` and
    `--since` narrow the scope."""
    a.model = a.model or "main"
    try:
        since = queue.parse_since(a.since) if a.since else None
    except ValueError as e:
        print(e)                                                       # a typo must never widen the scope
        return
    scope = (f" owned by {a.user}" if a.user else "") + (f" worked since {a.since}" if a.since else "")
    orgs_to_drain = _drain_orgs(a)

    if a.dry_run:
        grand = 0
        for org in orgs_to_drain:
            n, sz = queue.depth(org, a.user, since)
            pois = queue.poison_depth(org)
            grand += n
            if n or pois:
                _recent = (select(_m.costs.c.cost_usd)
                           .where(_m.costs.c.step.like("extract%"), _m.costs.c.cost_usd > 0)
                           .order_by(_m.costs.c.at.desc()).limit(50).subquery())
                avg = db.run(store.sqlite_db(store.org_db(org)),
                             select(func.avg(_recent.c.cost_usd))).fetchone()[0] or 0.0
                est = f"≈${n * avg:.2f} at ~${avg:.3f}/session" if avg else "cost unknown (no prior extractions)"
                print(f"[{org}] {n} pending{scope} (~{sz // 1024} KB), {est}, model={a.model}. {pois} quarantined.")
        print(f"total {grand} pending{scope} across {len(orgs_to_drain)} org(s). No LLM called.")
        return

    total_done = total_poisoned = total_systemic = 0
    for org in orgs_to_drain:
        n, _ = queue.depth(org, a.user, since)
        if n == 0:
            continue
        if a.limit is None and not a.all:
            print(f"[{org}] {n} pending{scope} — refusing to drain a whole queue in one burst (the burn this "
                  f"prevents). Pass --limit N (recommended) or --all.")
            continue
        done, failed, poisoned, actions, busy = drain_org(org, a.model, a.limit, user=a.user, since=since)
        if busy:
            print(f"[{org}] another drain is already running — skipping.")
            continue

        mix, spent = collections.Counter(), 0.0
        for sid, sess, data in done:
            act = actions.get(sid, "full")
            mix[act] += 1
            cost = 0.0 if act == "skip" else (data.get("_cost_usd") or 0)
            spent += cost
            log.info("[%s] drained %-14s %-11s hidden=%d ent=%d $%.3f", org, (sess.get("project") or "-")[:14],
                     act, len(data.get("hidden_context", [])), len(data.get("entities", [])), cost)
        pset = set(poisoned)
        systemic = [sid for sid, _ in failed if sid not in pset]
        for sid, err in failed:
            if sid in pset:
                log.warning("[%s] quarantined %s (bad content): %s", org, sid[:12], err[:120])
            else:
                log.error("[%s] STALLED on %s — systemic, still queued: %s", org, sid[:12], err[:160])
        n2, _ = queue.depth(org, a.user, since)
        tail = f" ({', '.join(sid[:8] for sid in poisoned)})" if poisoned else ""
        stall = f", {len(systemic)} STALLED (systemic, backlog stuck)" if systemic else ""
        print(f"[{org}] drained {len(done)} ({mix['full']} full, {mix['incremental']} incr, {mix['skip']} skip, "
              f"${spent:.3f}), quarantined {len(poisoned)}{tail}{stall}, {n2} still pending{scope}, "
              f"{queue.poison_depth(org)} in poison/")
        total_done += len(done); total_poisoned += len(poisoned); total_systemic += len(systemic)
    if total_done or total_poisoned or total_systemic:
        stall = f", {total_systemic} STALLED (systemic)" if total_systemic else ""
        print(f"== total: drained {total_done}, quarantined {total_poisoned}{stall}{scope} across {len(orgs_to_drain)} org(s) ==")
    else:
        print(f"nothing to drain{scope}.")


def cmd_recall(a):
    import app
    from analyzer import resolve
    card, standalone = resolve.card_for(_ro(), a.topic)  # an identifier's evidence card answers outright
    if standalone:
        print(card); return
    print(app.recall(a.topic, k=a.k, fill=not a.no_fill, fill_k=a.fill_k))


def cmd_index(a):
    do_index()


def cmd_resolve(a):
    """A loose token -> the indexed values it resolves to (owner, entity, project, file, branch)."""
    from analyzer import fuzzy_index as fx
    con = _ro()
    idx = fx.load_index(store.org_db()) or fx.build_index_from_store(con)
    ents = fx.resolve_everywhere(idx, a.mention)
    if not ents:
        print(f'no identifier in the corpus resolves "{a.mention}"'); return
    for e in ents[:a.k]:
        tag = "RESOLVED" if e.resolved else (f"FAMILY×{e.n_members}" if e.n_members > 1 else "ambiguous")
        axes = ", ".join(sorted({x["axis"] for x in e.axes}))
        print(f"[{tag:12s}] {e.kind:13s} {e.value!r}  (score {e.best_score:.2f}; via {axes})")


def cmd_harvest(a):
    """Harvest every local transcript's deterministic evidence (pr-links, commands), no LLM. Idempotent;
    ingest does the same per session."""
    con = store.sqlite_db()
    sessions = ingest.load_sessions(limit=a.limit, min_user_msgs=0)   # 0: thin sessions still carry pr-links
    n = 0
    for i, s in enumerate(sessions, 1):
        n += bool(harvest.write(con, s))
        if i % 50 == 0:
            log.info("harvest: %d/%d sessions scanned (%d with evidence)", i, len(sessions), n)
    store.build_fts(con)
    prs, ws = (db.run(con, select(func.count()).select_from(t)).fetchone()[0]
               for t in (_m.PrLink.__table__, _m.SessionWorkspace.__table__))
    print(f"harvested {n}/{len(sessions)} sessions -> {prs} pr_links rows, {ws} workspace rows; FTS rebuilt")


def cmd_build(a):
    if a.dry_run:
        return _coverage()
    do_index(do_extract(a.limit, a.workers, a.model, a.force))


def cmd_stats(a):
    ask.stats(store.sqlite_db(), a.name)


def cmd_ask(a):
    from analyzer import resolve
    card, standalone = resolve.card_for(_ro(), a.question)
    if standalone:
        print(card); return
    if card:
        print(card + "\n")
    res = ask.ask(_ro(), a.question, model=a.model)
    if res[0] == "answer":
        print(res[1]); print(f"\n— {len(res[2])} sources")
    else:
        ask.print_table(res[1], res[2])


def cmd_queries(a):
    db = store.org_db(store.valid_org_id(a.org) if a.org else None)
    if a.backfill:
        from analyzer import signals
        print(f"backfilled acted_on for {signals.backfill(store.sqlite_db(db))} historical queries "
              "(marker/MCP path only — raw transcripts aren't stored, so historical hook rows can't be redone)")
    r = store.queries_readout(store.sqlite_db(db))
    print(f"queries logged: {r['total']}  (result-bearing {r['result_bearing']}, empty {r['empty']}, "
          f"acted-on {r['acted_on']})")
    print("  acted-on = token-overlap heuristic, a LOWER BOUND on visible use (misses silent use, counts "
          "echoed jargon) and only counts sessions already shipped+indexed — not ground truth.")
    print("by engine:", ", ".join(f"{k}={v}" for k, v in r["by_engine"].items()) or "(none)")


def cmd_search(a):
    hits = search.search(_ro(), a.question, k=a.k)
    for h in hits:
        tag = f" [{h['source']}/{h['status']}]" if (h['source'] != 'session' or h['status']) else ""
        corr = f" corrob×{h['corrob']:.2f}" if h.get('corrob', 1.0) > 1.0 else ""
        print(f"[{h['score']:.2f}] {h['project'] or '-'}/{search.src_label(h['session_id'])} ({h['type']}){tag}"
              f"  bm25={h['bm25']:.1f} rec={h['rec']:.2f} ov={h['ov']:.0f}{corr}")
        print(f"      {h['snippet']}")
    print(f"({len(hits)} hits)")


def cmd_answer(a):
    ans, hits = search.answer(_ro(), a.question, k=a.k, model=a.model)
    print(ans)
    print(f"\n— synthesized from {len(hits)} retrieved sources")


def cmd_entities(a):
    con = store.sqlite_db()
    if a.dedup:
        groups = store.suggest_merges(con)
        if not groups:
            print("no obvious duplicate entities")
            return
        print("suggested merges (review — same type; ids that normalize alike OR are typo-close):")
        for g in groups:
            print("  " + "   |   ".join(f"{i}({t})" for i, t, _ in g))
        print(f"\n{len(groups)} groups. merge with:  csa merge <from_id> <into_id>  then  csa index")
    else:
        _e, _mn = _m.Entity.__table__, _m.mentions
        _stmt = (select(_e.c.id, _e.c.type, func.count(_mn.c.session_id).label("mentions"))
                 .select_from(_e.outerjoin(_mn, _mn.c.entity_id == _e.c.id))
                 .group_by(_e.c.id).order_by(text("mentions DESC")).limit(40))
        ask.print_table(["id", "type", "mentions"], db.run(con, _stmt).fetchall())


def cmd_merge(a):
    store.merge_entity(store.sqlite_db(), a.from_id, a.into_id)
    print(f"merged {a.from_id} -> {a.into_id} (SQLite). run `csa index` to propagate to the graph + FTS.")


def cmd_review(a):
    con = store.sqlite_db()
    print("## entities needing review (most-mentioned, unreviewed or auto-proposed)")
    _e, _mn, _rv = _m.Entity.__table__, _m.mentions, _m.Review.__table__
    _stmt = (select(_e.c.id, _e.c.type, func.count(_mn.c.session_id).label("mentions"),
                    func.ifnull(_rv.c.status, "-").label("status"), func.ifnull(_rv.c.note, "").label("note"))
             .select_from(_e.outerjoin(_mn, _mn.c.entity_id == _e.c.id)
                          .outerjoin(_rv, (_rv.c.kind == "entity") & (_rv.c.ref == _e.c.id)))
             .where((_rv.c.status.is_(None)) | (_rv.c.note == "auto"))
             .group_by(_e.c.id).order_by(text("mentions DESC")).limit(25))
    ask.print_table(["id", "type", "mentions", "status", "note"], db.run(con, _stmt).fetchall())
    print("\nclassify with:  csa classify <id> shared|local|confirmed|rejected   then  csa index")


def cmd_classify(a):
    con = store.sqlite_db()
    kind = "entity" if db.run(con, select(1).select_from(_m.Entity.__table__)
                              .where(_m.Entity.id == a.ref)).fetchone() else "hc"
    store.set_review(con, kind, a.ref, a.status, note="human")
    print(f"set {kind} {a.ref} -> {a.status}. run `csa index` to apply to search + graph.")


def cmd_retype(a):
    con = store.sqlite_db()
    allowed = store.ENTITY_TYPES | {t["key"] for t in entity_types.custom(con)}
    if a.type not in allowed:
        print(f"unknown type '{a.type}'. choose: {', '.join(sorted(allowed))}")
        return
    if not db.run(con, select(1).select_from(_m.Entity.__table__)
                  .where(_m.Entity.id == a.entity_id)).fetchone():
        print(f"no entity '{a.entity_id}' (see `csa entities`)")
        return
    store.set_type(con, a.entity_id, a.type)
    print(f"retyped {a.entity_id} -> {a.type}. run `csa index` to apply.")


def cmd_entity_types(a):
    """Team taxonomy: list default + custom entity types; --add/--rm edit ONE org's custom set."""
    con = store.sqlite_db(store.org_db(a.org))
    if a.add:
        t = entity_types.upsert(con, a.add[0], a.add[1])
        print(f"added custom type '{t['key']}' — applies to sessions extracted from now on "
              f"(`csa reextract <sid>` re-runs one that matters)")
    elif a.rm:
        print(f"removed '{a.rm}'" if entity_types.remove(con, a.rm)
              else f"no custom type '{a.rm}' (defaults can't be removed)")
    else:
        for k in entity_types.DEFAULTS:
            print(f"  {k:<14} default")
        for t in entity_types.custom(con):
            print(f"  {t['key']:<14} custom   {t['description']}")


def cmd_status(a):
    """Image version, the three schema heads and control-plane reachability, as /api/health reports
    them. Exits non-zero when the control plane is unreadable."""
    from analyzer import migrations, orgs
    print(f"version: {migrations.image_version()}")
    for env in ("control", "corpus", "telemetry"):
        print(f"schema.{env}: {migrations.head(env)}")
    ok = orgs.control_plane_ok()
    print(f"control_plane: {'ok' if ok else 'unreadable'}")
    sys.exit(0 if ok else 1)


def cmd_migrate(a):
    """Bring control.db and every org's corpus and telemetry file to head, then print the three heads.
    Exits non-zero on a file that cannot be migrated (the compose `migrate` service gates on it)."""
    from analyzer import bootstrap, migrations, orgs, store
    try:
        con = orgs.control_db()
        if bootstrap.missing_config():
            print(f"warning: {bootstrap.missing_config()}", file=sys.stderr)
        for change in bootstrap.apply(con):
            print(f"applied: {change}")
        con.close()
        for org in orgs.corpus_orgs():
            store.sqlite_db(store.org_db(org)).close()
    except Exception as e:
        print(f"migrate failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    for env in ("control", "corpus", "telemetry"):
        print(f"{env}: {migrations.head(env)}")


def cmd_backup(a):
    """Snapshot data/ into `<dest>/backup-<UTC date-time>/`, verify it, then prune to the newest
    `--keep` verified snapshots by manifest `created`. An flock serializes concurrent runs."""
    if a.keep < 1:
        raise SystemExit("csa.py backup: --keep must be at least 1 (0 or negative would prune the "
                         "snapshot this run just verified)")
    import fcntl, glob, shutil, time
    from analyzer import backup
    dest_root = os.path.abspath(a.dest or "data")
    os.makedirs(dest_root, exist_ok=True)
    with open(os.path.join(dest_root, ".backup.lock"), "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)

        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        snap_dir = os.path.join(dest_root, f"backup-{stamp}")
        while os.path.exists(snap_dir):                  # two runs in the same second: never overwrite one
            stamp += "-"
            snap_dir = os.path.join(dest_root, f"backup-{stamp}")

        try:
            manifest = backup.snapshot("data", snap_dir)
        except Exception:
            shutil.rmtree(snap_dir, ignore_errors=True)  # a partial copy is not a snapshot; leave no debris
            raise
        print(f"snapshot: {snap_dir} ({len(manifest['files'])} files, {manifest['queued_files']} queued)")
        ok, problems = backup.verify(snap_dir)
        if not ok:
            print(f"verify failed for {snap_dir}: {'; '.join(problems)}", file=sys.stderr)
            shutil.rmtree(snap_dir)
            print(f"deleted: {snap_dir}", file=sys.stderr)
            raise SystemExit(1)
        print(f"verified: {snap_dir}")

        snaps = []
        for d in glob.glob(os.path.join(dest_root, "backup-*")):
            mpath = os.path.join(d, backup.MANIFEST)
            if not os.path.isdir(d) or not os.path.exists(mpath):
                continue
            with open(mpath) as f:
                snaps.append((json.load(f)["created"], d))
        snaps.sort()
        for _created, d in snaps[:-a.keep]:
            shutil.rmtree(d)
            print(f"pruned: {d}")


def cmd_scrub(a):
    """Redact the extraction cache in place (keys, tokens, emails only), then rebuild from it."""
    from analyzer import redact
    import glob
    def rd(x):
        return redact.redact(x)[0]
    n = 0
    for f in glob.glob(str(incremental.extract_dir() / "*.json")):
        with open(f) as fh:
            d = json.load(fh)
        before = json.dumps(d, sort_keys=True)
        d["summary"] = rd(d.get("summary"))
        d["worked_on"] = [rd(str(w)) for w in d.get("worked_on", [])]
        d["data_sources"] = [rd(str(x)) for x in d.get("data_sources", [])]
        for h in d.get("hidden_context", []):
            h["claim"], h["evidence"] = rd(h.get("claim")), rd(h.get("evidence"))
            if h.get("repo") or h.get("path"):                # code-home is model-authored text too
                h["repo"], h["path"] = rd(h.get("repo")), rd(h.get("path"))
        for e in d.get("entities", []):
            e["name"] = rd(e.get("name"))
            if isinstance(e.get("attributes"), dict):
                e["attributes"] = {k: rd(str(v)) for k, v in e["attributes"].items()}
        for a2 in d.get("actions", []):
            a2["context"] = rd(a2.get("context"))
        for r in d.get("relations", []):
            r["context"] = rd(r.get("context"))
        for l in d.get("links", []):
            l["url"], l["title"] = rd(l.get("url")), rd(l.get("title"))
        if json.dumps(d, sort_keys=True) != before:
            with open(f, "w") as fh:
                json.dump(d, fh)
            n += 1
    print(f"redacted {n} cache files; rebuilding db+FTS from clean cache (+ memories)...")
    do_index()
    print("WARNING: regex caught keys/tokens/emails ONLY. Coworker NAMES, hostnames, codenames, customer data, and")
    print("PROSE pointers ('a key lives in .env') REMAIN -> corpus is NOT safe to share. Needs NER + human review +")
    print("per-user access control + legal. The real sk- key still needs ROTATION (it's not in our stores).")


def _cwd_by_session():
    """session_id -> cwd from the transcripts on this machine: the database never stores cwd, so only
    local sessions can be repaired."""
    import glob
    out = {}
    for f in sorted(glob.glob(os.path.join(ingest.ROOT, "**", "*.jsonl"), recursive=True)):
        sid = os.path.splitext(os.path.basename(f))[0]
        cwd = ingest.cwd_from_file(f)
        if cwd:
            out[sid] = cwd
    return out


def _fts_drift(con):
    """docs rows whose (derived) project disagrees with their session's."""
    try:
        return con.execute(                              # raw: docs is FTS5
            "SELECT COUNT(*) FROM docs d JOIN sessions s ON s.session_id=d.session_id "
            "WHERE IFNULL(d.project,'') <> IFNULL(s.project,'')").fetchone()[0]
    except sqlite3.OperationalError:                      # never indexed
        return 0


def cmd_fix_projects(a):
    """Recompute sessions.project through normalize_project from the local transcripts' cwd. Each change
    is labelled `normalized` (the rule) or `drift` (the live transcript's cwd moved since ingest); a
    session with no local transcript is reported, never guessed. The FTS is rebuilt whenever docs
    drift from sessions, so an interrupted run heals. `--db` rehearses on an existing copy."""
    from analyzer.project import normalize_project
    if a.db and not os.path.exists(a.db):                 # sqlite_db() would create it and report a clean no-op
        print(f"no such db: {a.db}  (nothing was created — check the path)")
        raise SystemExit(2)
    con = store.sqlite_db(a.db) if a.db else store.sqlite_db()
    cwds = _cwd_by_session()
    rows = db.run(con, select(_m.SessionRow.session_id, _m.SessionRow.project,
                              _m.SessionRow.source)).fetchall()
    changes, unrepairable, skipped_memory = [], [], 0
    for sid, proj, source in rows:
        if source and source != "session":                # curated memories never had a cwd
            skipped_memory += 1
            continue
        cwd = cwds.get(sid)
        if not cwd:
            unrepairable.append((sid, proj))              # no cwd on this box -> report, don't guess
            continue
        new = normalize_project(cwd)
        if new != proj:
            why = "normalized" if new != os.path.basename(cwd) else "drift"
            changes.append((sid, proj, new, why))

    def _agents(vals):
        return sum(1 for v in vals if v and v.startswith("agent-"))
    sess_rows = [(sid, p) for sid, p, src in rows if not src or src == "session"]
    after = dict(sess_rows) | {sid: new for sid, _, new, _w in changes}
    n_norm = sum(1 for *_, w in changes if w == "normalized")
    print(f"db: {a.db or store.org_db()}")
    print(f"sessions: {len(sess_rows)} indexed ({skipped_memory} memory-source, skipped)")
    print(f"transcripts readable on this machine: {len(cwds)} under {ingest.ROOT} "
          f"(a cwd lookup table — nothing is ingested or billed)")
    print(f"distinct project names: {len({p for _, p in sess_rows})} -> {len(set(after.values()))}"
          f"   <- the real win: junk names collapse into repos")
    print(f"agent-* projects: {_agents(p for _, p in sess_rows)} -> {_agents(after.values())}"
          f"{'   (unmoved: their transcripts are not on this box — see UNREPAIRABLE)' if _agents(after.values()) else ''}")
    print(f"sessions re-attributed: {len(changes)}  ({n_norm} by the new rule, {len(changes) - n_norm} "
          f"'drift' — the transcript's cwd MOVED since ingest, so master would set the same value; "
          f"re-derived here as a side effect, not by normalization)")
    for sid, old, new, why in changes[:15]:
        print(f"  session {sid[:8]}…  project {old!r} -> {new!r}   [{why}]")
    if len(changes) > 15:
        print(f"  … and {len(changes) - 15} more")
    if unrepairable:
        print(f"UNREPAIRABLE (no transcript on this machine, cwd unknown): {len(unrepairable)} session(s)")
        for sid, proj in unrepairable[:5]:
            print(f"  {sid[:8]}  {proj!r}  <- ask the owner's client to re-ship it (free: no `claude -p`)")
        if len(unrepairable) > 5:
            print(f"  … and {len(unrepairable) - 5} more")
    subdirs = sum(1 for _, p in after.items() if p in ("server", "client", "ui", "src", "app", "lib", "web"))
    if subdirs:
        print(f"KNOWN GAP: {subdirs} session(s) keep a SUBDIR name (server/client/ui/…) — a session run from "
              f"`<repo>/server` is not resolved to its repo. Deliberate, not missed: see the non-rules in "
              f"analyzer/project.py. This is the biggest remaining source of junk project names.")
    drift = _fts_drift(con)
    if drift:
        print(f"FTS DRIFT: {drift} docs row(s) disagree with sessions.project — recall is serving a stale "
              f"project. A rebuild is needed even if 0 rows change (an earlier run was likely interrupted).")
    if a.dry_run:
        print(f"\n--dry-run: nothing written. Re-run without --dry-run to apply"
              f"{' + rebuild the FTS index' if (changes or drift) else ''}.")
        if changes or drift:
            try:
                print(f"  the rebuild is a full DROP+rebuild of `docs` ({con.execute('SELECT COUNT(*) FROM docs').fetchone()[0]} "
                      f"rows). Recall is degraded while it runs — on a live server, pick a window.")
            except sqlite3.OperationalError:
                pass
        return
    if changes:
        db.run_many(con, update(_m.SessionRow.__table__)
                    .where(_m.SessionRow.session_id == bindparam("b_sid"))
                    .values(project=bindparam("b_proj")),
                    [{"b_proj": new, "b_sid": sid} for sid, _, new, _w in changes])
        con.commit()
    if changes or drift:
        print("rebuilding FTS (full DROP+rebuild — recall is degraded until it finishes)…")
        store.build_fts(con)
        print(f"updated {len(changes)} row(s); FTS rebuilt ({_fts_drift(con)} drift remaining).")
    else:
        print("nothing to change and no FTS drift (idempotent no-op).")


def cmd_reextract(a):
    """Drop one session's extraction cache so the next ingest or drain re-extracts it in full (one LLM
    call, unlike the corpus-wide `build --force`)."""
    p = _out(a.session_id)
    if not os.path.exists(p):
        print(f"no cached extraction for {a.session_id} (nothing to drop; it will extract on next ingest)")
        return
    if a.dry_run:
        print(f"--dry-run: would remove {p} -> next ingest/drain re-extracts {a.session_id} (bills 1 LLM call)")
        return
    os.remove(p)
    print(f"removed {p}\nnext ingest/drain of {a.session_id} re-extracts it in full (bills 1 LLM call).")


def cmd_grade(a):
    """grade-the-gold: sample insights, human-label gold/known/wrong, report precision. Labels also curate."""
    con = store.sqlite_db()
    label = {"g": "gold", "k": "already-documented", "w": "rejected"}
    if not a.report:
        _h, _s = _m.hidden_context, _m.SessionRow.__table__
        _rv, _cc = _m.Review.__table__, _m.ClaimCluster.__table__
        rows = db.run(con, select(
            _h.c.ref, _h.c.type, _h.c.scope, _h.c.claim, _h.c.evidence, _s.c.project, _h.c.session_id, _cc.c.sessions)
            .select_from(_h.join(_s, _s.c.session_id == _h.c.session_id)
                         .outerjoin(_rv, (_rv.c.kind == "hc") & (_rv.c.ref == _h.c.ref))
                         .outerjoin(_cc, _cc.c.ref == _h.c.ref))
            .where(_rv.c.status.is_(None), _s.c.source == "session")   # memories are pre-curated
            .order_by(func.random()).limit(a.n)).fetchall()
        if not rows:
            print("nothing left to grade (sampled insights already reviewed).")
        else:
            print(f"Grading {len(rows)} insights — is each REAL & UNDOCUMENTED gold?")
            print("[g]old   [k]nown (true but already documented)   [w]rong (false)   [s]kip   [q]uit\n")
            done = skipped = 0
            for ref, ty, scope, claim, ev, proj, sid, dup_sessions in rows:
                print(f"\n--- {proj}/{(sid or '')[:8]}  [{ty}/{scope or '?'}] ---")
                if dup_sessions and dup_sessions > 1:
                    print(f"≈ likely DUP — same finding seen in {dup_sessions} sessions")
                print(f"CLAIM:    {claim}")
                if ev:
                    print(f"EVIDENCE: {ev}")
                try:
                    ans = input("grade [g/k/w/s/q]: ").strip().lower()
                except EOFError:
                    break
                if ans == "q":
                    break
                if ans in label:
                    store.set_review(con, "hc", ref, label[ans], note="grade")
                    done += 1
                elif ans == "s":                       # recorded, so it stops resurfacing
                    store.set_review(con, "hc", ref, "skip", note="grade")
                    skipped += 1
            print(f"\ngraded {done} this round" + (f", skipped {skipped} (recorded)" if skipped else "") + ".")
    _rv = _m.Review.__table__
    counts = dict(db.run(con, select(_rv.c.status, func.count())
                         .where(_rv.c.kind == "hc", _rv.c.note == "grade").group_by(_rv.c.status)))
    gold, known, wrong = counts.get("gold", 0), counts.get("already-documented", 0), counts.get("rejected", 0)
    skipped = counts.get("skip", 0)
    total = gold + known + wrong
    print(f"\n## grade-the-gold — {total} graded, {skipped} skipped")
    if total:
        print(f"  gold  (real, undocumented) : {gold:3}  ({gold / total:.0%})")
        print(f"  known (true, documented)   : {known:3}  ({known / total:.0%})")
        print(f"  wrong (false/hallucinated) : {wrong:3}  ({wrong / total:.0%})")
        print(f"  -> PRECISION (gold/graded) : {gold / total:.0%}")
        if skipped:
            print(f"  skip-rate (skipped/seen)   : {skipped/(total+skipped):.0%}   <- recorded; training set for auto-skip")
        print("  ('wrong'/'known' get suppressed, 'gold' boosted on next `csa index`)")


def cmd_judge(a):
    from analyzer import judge
    if a.etl:
        n = judge.etl_pass(store.sqlite_db(), model=a.model)
        print(f"etl pass: {n['reaffirmed']} reaffirmed (>=2-session clusters), "
              f"{n['stale']} staled (superseded by newer facts), over {n['clusters']} unchecked clusters")
        return
    n = judge.run(store.sqlite_db(), limit=a.limit, model=a.model)
    print("labeled: " + "  ".join(f"{k} {n[k]}" for k in judge.LABELS if k != "unsure")
          + f"  |  unsure {n['unsure']} (stay in the human queue)")


def cmd_watch(a):
    from analyzer import etl
    etl.watch(once=a.once)


def cmd_screen(a):
    n = store.screen(store.sqlite_db())
    print(f"auto-PROPOSED 'local' for {n} single-session file/env entities (candidate graph-drops).")
    print("'shared' = same entity across multiple users — set it manually (auto once sessions carry a user id).")
    print("Suggestions only: a human must `csa classify <id> local` to actually drop it from the graph.")
    print("review: `csa review`  ·  confirm/override: `csa classify <id> <status>`  ·  apply: `csa index`")


# --- org admin: the only cross-workspace administration surface --------------------------------------

def cmd_org_create(a):
    from analyzer import orgs
    first = orgs.org_count() == 0 and not orgs.default_is_adopted()
    org_id, invite = orgs.create_org(a.name, a.id, a.domain)
    print(f"org:    {org_id}\ninvite: {invite}\n\nGive that invite to the team — it is shown ONCE (stored as a hash).")
    if first:
        print("\nThis deployment now hosts tenants: CSA_AUTH_TOKEN is no longer a credential, and any\n"
              "client still sending it gets 401 telling it to run `csa-client setup`.")
    if a.domain:
        print(f"domain: {a.domain}. Anyone who signs in through the issuer on that domain "
              f"lands in this org, no invite needed.")


def cmd_org_adopt_default(a):
    """The migration: give the built-in `default` corpus an org row. Moves no data."""
    import secrets
    from analyzer import orgs, store
    if orgs.default_is_adopted():
        print(f"refusing: the {store.DEFAULT_ORG!r} org already has a row. "
              f"Use `csa.py org domain` / `csa.py org invite` to change it.")
        raise SystemExit(2)
    print(f"About to adopt the built-in {store.DEFAULT_ORG!r} corpus as the org {a.name!r}.")
    print(f"  corpus file:  {store.org_db(None)}   (UNCHANGED — this moves no data)")
    print(f"  domain (SSO): {a.domain or '(none)'}")
    print("  invite:       %s" % ("a NEW one will be minted" if a.mint_invite else "none (recommended)"))
    if not a.mint_invite:
        print("\n  No invite is stored: seeding one from CSA_AUTH_TOKEN would make that secret a permanent\n"
              "  mint credential. Run `csa.py org invite default` later if this deployment onboards by invite.")
    print("\n  From here on CSA_AUTH_TOKEN is not a credential; any client still sending it gets 401\n"
          "  telling it to run `csa-client setup` (`csa.py org tokens` lists who already holds a per-user\n"
          "  token). The signed-out front door stays the sign-in; it becomes the public landing with\n"
          "  its workspace-request form only where the request_webhook setting is set.")
    if not a.yes and not _confirm("\nProceed? [y/N] "):
        print("aborted")
        raise SystemExit(1)
    invite = (orgs.INVITE_PREFIX + secrets.token_urlsafe(32)) if a.mint_invite else None
    orgs.adopt_default_org(a.name, domain=a.domain, invite=invite)
    print(f"\nadopted: {store.DEFAULT_ORG} is now the org {a.name!r}")
    if invite:
        print(f"invite:  {invite}\n(shown ONCE — only its hash is stored)")
    print("Verify: an existing per-user token still ingests, an existing browser cookie still reads,\n"
          "and an SSO sign-in on that domain still lands in 'default'.")


def cmd_org_role(a):
    """Grant or remove workspace-admin inside one org; the operator bootstraps the first admin."""
    from analyzer import orgs
    n = orgs.set_role(a.email, a.org_id, a.role)
    if not n:
        browser_only = any(t["active"] and t["hostname"] in orgs.BROWSER_HOSTNAMES
                           and (t["email"] or "").lower() == a.email.strip().lower()
                           for t in orgs.list_tokens(a.org_id))
        print(f"{a.email!r} only has browser sign-ins in {a.org_id!r}, which never carry a role. "
              f"The workspace admin gets a new link from `csa.py org admin-link`; anyone else runs "
              f"`csa-client setup` first." if browser_only else
              f"nobody in {a.org_id!r} has a live token for {a.email!r}; they must sign in first "
              f"(csa.py org tokens --org {a.org_id} shows who has).")
        raise SystemExit(1)
    print(f"{a.email} is now {a.role} in {a.org_id} ({n} token(s) updated)")


def cmd_org_admin_link(a):
    from analyzer import bootstrap
    try:
        print(bootstrap.recovery_link())
    except ValueError as e:
        print(f"admin-link: {e}")
        raise SystemExit(1)


def cmd_org_requests(a):
    from analyzer import orgs
    rows = orgs.list_requests(None if a.all else "pending")
    if not rows:
        print("(no workspace requests)" if a.all else "(no pending workspace requests)")
        return
    print(f"{'ID':10} {'COMPANY':28} {'EMAIL':30} {'CREATED':21} STATUS")
    for r in rows:
        state = r["status"] + (f" -> {r['org_id']} at {r['handled_at']}" if r["status"] == "approved" else "")
        print(f"{r['id']:10} {(r['company'] or '')[:28]:28} {(r['email'] or '')[:30]:30} "
              f"{(r['created'] or ''):21} {state}")
    if any(r["status"] == "pending" for r in rows):
        print("\nApprove with: csa.py org approve <ID> [--domain company.com] [--id explicit-org-id]")


def cmd_org_approve(a):
    from analyzer import orgs
    r = orgs.approve_request(a.request_id, domain=a.domain, org_id=a.id)
    req = r["request"]
    print(f"request: {req['id']} — {req['company']} <{req['email']}>")
    print(f"org:     {r['org_id']}\ninvite:  {r['invite']}\n\n"
          f"Send that invite to {req['email']} — it is shown ONCE (stored as a hash).")
    if r["domain"]:
        print(f"domain:  {r['domain']}. Anyone who signs in through the issuer on that "
              f"domain lands in this org, no invite needed.")
    elif r["domain_error"]:                              # never raise: the invite was just printed once
        print(f"domain NOT bound: {r['domain_error']}")
    elif "@" in (req["email"] or ""):
        dom = req["email"].rsplit("@", 1)[1].lower()
        print(f"hint: the requester's email domain is {dom} — `csa.py org domain {r['org_id']} {dom}` "
              f"enables SSO sign-in for that domain. Not done automatically: the email is "
              f"self-asserted, and a domain bind admits the whole Workspace.")


def cmd_org_list(a):
    from analyzer import orgs
    rows = orgs.list_orgs()
    print(f"{'ORG':24} {'NAME':24} {'DOMAIN (SSO)':22} {'TOKENS':>6}  CREATED")
    if not orgs.default_is_adopted():                    # once adopted, list_orgs() carries it like any tenant
        print(f"{'default':24} {'(self-host corpus)':24} {'-':22} {'-':>6}  built-in")
    for o in rows:
        print(f"{o['id']:24} {(o['name'] or '')[:24]:24} {(o['domain'] or '-')[:22]:22} "
              f"{o['tokens']:>6}  {o['created']}")


def _org_usage(org_id):
    """Session and fact counts, spend and last activity for one org, read-only (a second process on a live
    box). An absent or unreadable db reads zeros, so one broken tenant cannot hide the table."""
    from analyzer import store
    out = {"sessions": 0, "facts": 0, "spend_month": 0.0, "spend_today": 0.0, "last_active": None}
    try:
        con = store.ro_connect(store.org_db(org_id))
    except Exception:
        return out
    try:
        q = con.execute
        out["sessions"] = q("SELECT COUNT(*) FROM sessions WHERE source='session'").fetchone()[0]
        out["facts"] = q("SELECT COUNT(*) FROM hidden_context").fetchone()[0]
        out["last_active"] = q("SELECT MAX(ended_at) FROM sessions WHERE source='session'").fetchone()[0]
        for key, since in (("spend_month", "start of month"), ("spend_today", None)):
            where = "date('now')" if since is None else "date('now','start of month')"
            out[key] = round(q(f"SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE at >= {where}")
                             .fetchone()[0] or 0.0, 4)
    except Exception:
        pass
    finally:
        con.close()
    return out


def cmd_org_overview(a):
    """Every workspace on this box, with what it costs (read from each tenant's own `costs` table)."""
    from analyzer import orgs, store
    ids = orgs.corpus_orgs()
    rows = {o["id"]: o for o in orgs.list_orgs()}
    print(f"{'ORG':20} {'NAME':22} {'SESSIONS':>8} {'FACTS':>7} {'TOKENS':>6} "
          f"{'$ TODAY':>8} {'$ MONTH':>8}  LAST ACTIVE")
    tot_today = tot_month = 0.0
    for oid in ids:
        row = rows.get(oid, {})
        u = _org_usage(oid)
        tot_today += u["spend_today"]
        tot_month += u["spend_month"]
        name = row.get("name") or ("(self-host corpus)" if oid == store.DEFAULT_ORG else "")
        print(f"{oid[:20]:20} {name[:22]:22} {u['sessions']:>8} {u['facts']:>7} "
              f"{row.get('tokens', 0):>6} {u['spend_today']:>8.2f} {u['spend_month']:>8.2f}  "
              f"{u['last_active'] or 'never'}")
    print(f"{'':20} {'TOTAL':22} {'':>8} {'':>7} {'':>6} {tot_today:>8.2f} {tot_month:>8.2f}")
    pending = orgs.count_requests("pending")
    if pending:
        print(f"\n{pending} pending workspace request(s) — `csa.py org requests` to read them, "
              f"`csa.py org approve <id>` to provision.")
    print("\nProvisioning a workspace is TWO commands: `org create` mints it, `org role <org> "
          "<email> admin` gives it someone who can administer it. An org with no admin cannot manage "
          "its own members.")


def cmd_org_audit(a):
    """Admin history for one org, or every org (`/api/org/audit` shows a tenant only its own). Rows carry
    no actor."""
    from analyzer import orgs
    rows = orgs.audit_log(a.org, min(int(a.limit), 1000))
    if not rows:
        print("(no admin actions recorded)" + (f" for {a.org!r}" if a.org else ""))
        return
    print(f"{'WHEN':21} {'ORG':20} {'ACTION':20} DETAIL")
    for r in rows:
        print(f"{(r['at'] or ''):21} {(r['org_id'] or '-')[:20]:20} {(r['action'] or '')[:20]:20} "
              f"{(r['detail'] or '')[:60]}")


def cmd_org_fleet(a):
    """Machines and backfill directives for one workspace."""
    from analyzer import fleet
    machines = fleet.machines(a.org_id)
    directives = fleet.list_directives(a.org_id, limit=min(max(int(a.limit), 1), 500))
    print(f"MACHINES ({len(machines)})")
    if machines:
        print(f"{'EMAIL':30} {'HOST':22} {'LAST USED':21} TOKEN")
        for m in machines:
            print(f"{m['email'][:30]:30} {m['hostname'][:22]:22} "
                  f"{(m['last_used_at'] or 'never')[:21]:21} {m['token_hash'][:12]}")
    else:
        print("(none)")
    print(f"\nDIRECTIVES ({len(directives)})")
    if directives:
        print(f"{'ID':18} {'TARGET':38} {'SINCE':25} {'ANSWERED':10} STATE")
        for d in directives:
            target = d['target_email'] or '*'
            if d['target_hostname']:
                target += "/" + d['target_hostname']
            answered = f"{d['answered']}/{d['targets']}"
            print(f"{d['id'][:18]:18} {target[:38]:38} {(d['since'] or 'all')[:25]:25} "
                  f"{answered:10} {d['state']}")
    else:
        print("(none)")


def cmd_org_fetch(a):
    """Ask matching clients in one workspace to re-ship history at their next SessionStart."""
    from analyzer import fleet
    d = fleet.create_directive(a.org_id, since=a.since, target_email=a.email,
                               target_hostname=a.hostname, created_by="cli")
    target = d['target_email'] or '*'
    if d['target_hostname']:
        target += "/" + d['target_hostname']
    print(f"directive: {d['id']}\norg:       {d['org_id']}\ntarget:    {target}\n"
          f"since:     {d['since'] or 'all history'}\n\n"
          "Matching machines will act once at their next SessionStart. Use `csa.py org fleet "
          f"{d['org_id']}` to watch acknowledgements.")


def cmd_org_fetch_revoke(a):
    from analyzer import fleet
    if not fleet.revoke_directive(a.directive_id):
        print(f"no such live directive: {a.directive_id}")
        raise SystemExit(1)
    print(f"revoked directive {a.directive_id}")


def cmd_org_config(a):
    """Show or edit an org's settings (analyzer/tenant.py), both scopes. `--set field=value` checks
    every value first and stores nothing if one is refused; `--set field=` clears it to the default.
    Deployment fields are shared by every workspace on this server."""
    from analyzer import store, tenant
    db_path = store.org_db(a.org_id)
    if a.set:
        store.sqlite_db(db_path).close()      # an org nobody has shipped to yet has no db file
        updates = {}
        for pair in a.set:
            field, _, raw = pair.partition("=")
            updates[field.strip()] = raw.strip() or None
        errors = tenant.apply(a.org_id, updates, "cli", deployment_editable=True)
        if errors:
            for field, msg in errors.items():
                print(f"{field}: {msg}", file=sys.stderr)
            sys.exit("nothing was saved")
    print(f"{'FIELD':18} {'SCOPE':11} {'OVERRIDE':18} RESOLVED")
    for f in tenant.describe(a.org_id, deployment_editable=True)["fields"]:
        override = f["value"] if f["source"] == "set" else "-"
        print(f"{f['name']:18} {f['scope']:11} {str(override):18} {f['value']}")


def cmd_org_domain(a):
    from analyzer import orgs
    d = orgs.set_domain(a.org_id, a.domain)
    if d:
        print(f"{a.org_id}: sign-in domain set to {d}.\n"
              f"Anyone the issuer vouches for on that domain (Google: the Workspace hd) can now sign in to this org.")
    else:
        print(f"{a.org_id}: domain cleared. Its people can no longer sign in through the issuer.")


def cmd_org_invite(a):
    from analyzer import orgs
    had = orgs.has_invite(a.org_id)
    tail = "The old invite is dead. Already-minted user tokens keep working." if had else "Shown once; store it."
    print(f"invite: {orgs.rotate_invite(a.org_id)}\n\n{tail}")


def cmd_org_tokens(a):
    from analyzer import orgs
    rows = orgs.list_tokens(a.org)
    if not rows:
        print("(no tokens)")
        return
    print(f"{'TOKEN_HASH':14} {'EMAIL':28} {'ORG':16} {'ROLE':7} {'HOST':16} {'LAST USED':21} STATE")
    for t in rows:
        if t["revoked_at"]:                              # three states: inactive is revoked OR expired
            state = "revoked " + t["revoked_at"]
        elif t["active"]:
            state = "active" + (f" (until {t['expires_at']})" if t["expires_at"] else " (no expiry)")
        else:
            state = "EXPIRED " + (t["expires_at"] or "")
        print(f"{t['token_hash'][:12]:14} {(t['email'] or '')[:28]:28} {t['org_id'][:16]:16} "
              f"{t['role']:7} {(t['hostname'] or '-')[:16]:16} {(t['last_used_at'] or 'never'):21} {state}")
    print("\nRevoke with: csa.py org revoke <TOKEN_HASH>   (plaintext is never stored)")
    print("An EXPIRED token needs no action — the owner re-runs `csa-client setup` to get a new one.")


def cmd_org_revoke(a):
    from analyzer import orgs
    if getattr(a, "minted_by", None):
        # The SSO cutover: enabling SSO leaves every token minted under self-asserted identity alive.
        label = a.minted_by
        rows = [t for t in orgs.list_tokens(a.org)
                if (t["hostname"] or "") == label and not t["revoked_at"]]
        if not rows:
            print(f"no active tokens minted by {label!r}")
            return
        print(f"{'TOKEN_HASH':14} {'EMAIL':30} {'ORG':14} LAST USED")
        for t in rows:
            print(f"{t['token_hash'][:12]:14} {(t['email'] or '')[:30]:30} {t['org_id'][:14]:14} "
                  f"{t['last_used_at'] or 'never'}")
        if a.dry_run:
            print(f"\n[dry-run] would revoke {len(rows)} token(s). Re-run without --dry-run to apply.")
            return
        n = sum(1 for t in rows if orgs.revoke(t["token_hash"]))
        print(f"\nrevoked {n} of {len(rows)} token(s) minted by {label!r}. "
              "Their owners sign in again, through the issuer if SSO is on.")
        return
    if not a.token:
        raise SystemExit("give a TOKEN_HASH, or --minted-by <label> to revoke a whole mint path")
    print("revoked" if orgs.revoke(a.token) else "no such token (use the TOKEN_HASH from `csa.py org tokens`)")


def _confirm(prompt, expect=None):
    """Read a confirmation from stdin, defaulting to NO on a closed or piped stdin (a bare input()
    raises EOFError there). `expect` requires that exact string; otherwise y/yes."""
    try:
        answer = input(prompt).strip()
    except EOFError:
        print("\n(no input available — aborting)")
        return False
    return answer == expect if expect is not None else answer.lower() in ("y", "yes")


def cmd_org_delete(a):
    from sqlalchemy import literal, select
    from analyzer import db, orgs, store
    from analyzer.tenant import Org, UserToken
    oid = store.valid_org_id(a.org_id)                   # every refusal comes before the prompt
    if oid == store.DEFAULT_ORG:
        raise ValueError("refusing to delete the 'default' org (that is the self-host corpus itself)")
    con = orgs.control_db()
    if not (os.path.isdir(store.org_dir(oid))
            or db.run(con, select(literal(1)).select_from(Org.__table__)
                      .where(Org.__table__.c.id == oid)).fetchone()
            or db.run(con, select(literal(1)).select_from(UserToken.__table__)
                      .where(UserToken.__table__.c.org_id == oid).limit(1)).fetchone()):
        raise ValueError(f"no such org {oid!r} — nothing to delete (check the id)")
    if not a.yes:
        print(f"This DELETES org {oid!r}: its entire corpus ({store.org_dir(oid)}), queue, extractions,\n"
              f"and every token minted into it. The admin_log entry survives as the record.")
        if not _confirm("Type the org id to confirm: ", expect=oid):
            print("aborted")
            raise SystemExit(1)
    print(orgs.delete_org(oid))


# --- sign-in with an OpenID issuer (control.db `sso`; docs/sso.md) -----------------------------------

def _redirect_uri():
    base = os.environ.get("CSA_PUBLIC_URL", "").strip().rstrip("/")
    return base + "/api/sso/callback" if base else "<CSA_PUBLIC_URL>/api/sso/callback (CSA_PUBLIC_URL is not set here)"


def cmd_sso_set(a):
    """Validate the issuer, prove its discovery document names it, store the row, print the redirect
    URI to register. The client secret is read from stdin only: argv lands in shell history."""
    from analyzer import auth, orgs
    if not os.environ.get("CSA_PUBLIC_URL", "").strip():
        raise ValueError("CSA_PUBLIC_URL is not set here; run this where the server's environment is "
                          "present (`docker compose exec api ...` / `docker exec csa-api ...`), or "
                          "export CSA_PUBLIC_URL first")
    secret = None
    if a.client_secret_stdin:
        secret = sys.stdin.readline().strip()
        if not secret:
            raise ValueError("--client-secret-stdin: nothing was read from stdin")
    issuer = orgs.valid_issuer(a.issuer)
    try:
        auth.discovery(issuer, force=True)
    except Exception as e:
        raise ValueError(f"cannot read {issuer}/.well-known/openid-configuration: {e}") from e
    name = a.name or urllib.parse.urlsplit(issuer).hostname
    orgs.set_sso(issuer, a.client_id, secret, name, updated_by="cli")
    print(f"sign-in:       {name} ({issuer})\n"
          f"client id:     {a.client_id}\n"
          f"client secret: {'stored' if secret else 'none (public client, PKCE only)'}\n"
          f"redirect URI:  {_redirect_uri()}\n\n"
          f"Register that redirect URI with the provider before anyone signs in. From now on the\n"
          f"shared invite and typed-email paths are refused; every org that signs in needs a domain\n"
          f"(`csa.py org domain <org> <domain>`), and the server refuses to boot until one has.")


def cmd_sso_show(a):
    from analyzer import auth, orgs
    cfg = auth.sso_config(orgs.control_db())
    if cfg is None:
        print("no issuer configured: sign-in is by invite (`csa.py sso set` to configure one)")
        return
    print(f"sign-in:       {cfg.display_name} ({cfg.issuer})\n"
          f"client id:     {cfg.client_id}\n"
          f"client secret: {'stored' if cfg.client_secret else 'none (public client, PKCE only)'}\n"
          f"redirect URI:  {_redirect_uri()}")


def cmd_sso_clear(a):
    from analyzer import orgs
    print("cleared: sign-in is by invite again" if orgs.clear_sso() else "no issuer was configured")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="csa")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, helptext, fn in [("extract", "LLM extraction -> cache", cmd_extract),
                               ("build", "extract + index", cmd_build)]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--limit", type=int, default=30)
        p.add_argument("--workers", type=int, default=6)
        p.add_argument("--model", default="opus")
        p.add_argument("--force", action="store_true", help="re-extract even if cached (e.g. schema changed)")
        p.add_argument("--dry-run", action="store_true", help="show coverage (covered/stale/missing), don't call the LLM")
        p.set_defaults(func=fn)

    sub.add_parser("index", help="(re)load cache + Claude Code memories -> SQLite+FTS (cheap, no LLM)").set_defaults(func=cmd_index)
    p = sub.add_parser("resolve", help="fuzzy identifier resolution: a loose token -> canonical corpus values")
    p.add_argument("mention"); p.add_argument("--k", type=int, default=10); p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("drain", help="deferred-extraction queue: extract+index pending sessions (--limit caps spend; --dry-run estimates, no LLM)")
    p.add_argument("--limit", type=int, default=None, help="max sessions to extract this pass (the spend lever)")
    p.add_argument("--all", action="store_true", help="drain the ENTIRE queue at once (bypasses the --limit guard) — use with care")
    p.add_argument("--model", default=None, help="a model name or <provider>/<model> (default: the main model on AI Models, as the ETL uses)")
    p.add_argument("--user", default=None, help="only drain sessions owned by this user (e.g. tomasz@example.com)")
    p.add_argument("--since", default=None, help="only drain sessions worked on since: 7d / 24h / an ISO date")
    p.add_argument("--dry-run", action="store_true", help="estimate cost from the ledger; call NO LLM")
    p.add_argument("--org", default=None, help="drain ONE org's queue (default: every org, incl. 'default')")
    p.set_defaults(func=cmd_drain)
    p = sub.add_parser("harvest", help="backfill: deterministic evidence harvest (pr-links, gh/git commands) over all raw local sessions — no LLM")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("recall", help="past-session context for a topic as markdown (the app-layer seam for Claude)")
    p.add_argument("topic"); p.add_argument("-k", type=int, default=12)
    p.add_argument("--no-fill", action="store_true", help="don't auto-extract on a cold topic (pure read)")
    p.add_argument("--fill-k", type=int, default=3, help="max sessions to auto-extract on a cold topic (sonnet)")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("stats", help="canned dashboards")
    p.add_argument("name", nargs="?", default="overview",
                   help="overview|projects|hidden|entities|relations|data|links|deploys|memory|reviews")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("ask", help="NL -> SQL (structured/aggregate questions)")
    p.add_argument("question"); p.add_argument("--model", default="sonnet"); p.set_defaults(func=cmd_ask)

    p = sub.add_parser("queries", help="readout: which ask paths/engines are popular (Task 3 measurement)")
    p.add_argument("--org", default=None, help="read/backfill ONE org's corpus (default: the 'default' org)")
    p.add_argument("--backfill", action="store_true",
                   help="one-shot: set queries.acted_on from existing signals for sessions indexed before "
                        "the write-back existed (marker/MCP path only)")
    p.set_defaults(func=cmd_queries)

    p = sub.add_parser("search", help="full-text search + statistical rerank")
    p.add_argument("question"); p.add_argument("-k", type=int, default=10)
    p.set_defaults(func=cmd_search)                      # search is lexical-only — no LLM, so no --model

    p = sub.add_parser("answer", help="RAG answer with citations")
    p.add_argument("question"); p.add_argument("-k", type=int, default=8)
    p.add_argument("--model", default="sonnet"); p.set_defaults(func=cmd_answer)

    p = sub.add_parser("entities", help="list entities (--dedup suggests merges)")
    p.add_argument("--dedup", action="store_true"); p.set_defaults(func=cmd_entities)

    p = sub.add_parser("merge", help="HITL: fold one entity id into another")
    p.add_argument("from_id"); p.add_argument("into_id"); p.set_defaults(func=cmd_merge)

    sub.add_parser("review", help="HITL: list entities needing value review").set_defaults(func=cmd_review)

    p = sub.add_parser("classify", help="HITL: set status (gold|confirmed|shared|local|rejected|noise|already-documented)")
    p.add_argument("ref"); p.add_argument("status"); p.set_defaults(func=cmd_classify)

    p = sub.add_parser("grade", help="grade-the-gold: sample insights, label gold/known/wrong, get a precision %%")
    p.add_argument("-n", type=int, default=40); p.add_argument("--report", action="store_true")
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("judge", help="LLM-label facts (haiku): type gotcha/decision/... + gold/ok/unimportant/"
                                     "wrong/noise; unsure stays for humans")
    p.add_argument("--limit", type=int, default=None, help="max facts to judge (default: all pending)")
    p.add_argument("--etl", action="store_true", help="batch pass instead: reaffirm rediscovered facts + stale-out superseded ones")
    p.add_argument("--model", default="haiku"); p.set_defaults(func=cmd_judge)

    sub.add_parser("screen", help="HITL: auto-propose shared/local (no LLM)").set_defaults(func=cmd_screen)
    sub.add_parser("scrub", help="redact secrets/PII from cache+db (regex; necessary-not-sufficient)").set_defaults(func=cmd_scrub)
    p = sub.add_parser("fix-projects", help="backfill: recompute sessions.project (worktree cwd -> real repo) + rebuild FTS")
    p.add_argument("--dry-run", action="store_true", help="report before/after counts, write nothing")
    p.add_argument("--db", default=None, help="path to the DB (default data/analyzer.db) — point it at a COPY to rehearse")
    p.set_defaults(func=cmd_fix_projects)
    p = sub.add_parser("reextract", help="drop ONE session's extraction cache -> next ingest/drain re-extracts it (1 LLM call)")
    p.add_argument("session_id")
    p.add_argument("--dry-run", action="store_true", help="show what would be dropped, remove nothing")
    p.set_defaults(func=cmd_reextract)
    sub.add_parser("status", help="image version, schema heads and control-plane reachability").set_defaults(func=cmd_status)
    sub.add_parser("migrate", help="bring control.db and every org's corpus + telemetry file to the current "
                                   "schema (the compose runs this before api, mcp and etl)").set_defaults(func=cmd_migrate)

    p = sub.add_parser("backup", help="snapshot + verify data/, then prune to the newest N verified "
                                      "snapshots (the compose backup profile runs this daily)")
    p.add_argument("--keep", type=int, required=True, help="how many verified snapshots to retain")
    p.add_argument("--dest", default=None, help="parent dir for backup-<timestamp>/ (default: data)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("watch", help="live-ETL: watch ~/.claude/projects and POST changed sessions to the server")
    p.add_argument("--once", action="store_true", help="single scan then exit (self-check)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("retype", help="HITL: override an entity's type (survives re-extract)")
    p.add_argument("entity_id"); p.add_argument("type"); p.set_defaults(func=cmd_retype)
    p = sub.add_parser("entity-types", help="team taxonomy: list default+custom entity types; "
                                            "--add KEY \"description\" / --rm KEY edit customs (--org scopes a tenant)")
    p.add_argument("--org", default=None); p.add_argument("--add", nargs=2, metavar=("KEY", "DESCRIPTION"))
    p.add_argument("--rm", metavar="KEY"); p.set_defaults(func=cmd_entity_types)

    p = sub.add_parser("org", help="org admin: create/list orgs, rotate invites, list/revoke user tokens (SaaS)")
    osub = p.add_subparsers(dest="orgcmd", required=True)
    q = osub.add_parser("create", help="create an org + print its invite ONCE")
    q.add_argument("name"); q.add_argument("--id", default=None, help="explicit org id (default: slug of name)")
    q.add_argument("--domain", default=None,
                   help="sign-in domain whose people land in this org (Google: the Workspace hd; see docs/sso.md)")
    q.set_defaults(func=cmd_org_create)
    q = osub.add_parser("config", help="show/edit an org's tenant config (entity floor, budget, redaction, ...)")
    q.add_argument("org_id")
    q.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                   help="override a field in the org's db; empty value clears it back to the default")
    q.set_defaults(func=cmd_org_config)
    q = osub.add_parser("domain", help="bind/clear the sign-in domain that lands in an org (docs/sso.md)")
    q.add_argument("org_id"); q.add_argument("domain", nargs="?", default="",
                                             help="e.g. acme.com; omit to clear")
    q.set_defaults(func=cmd_org_domain)
    osub.add_parser("overview", help="every workspace with sessions, facts, tokens and spend"
                    ).set_defaults(func=cmd_org_overview)
    q = osub.add_parser("audit", help="admin history for one org (or all): what changed, and when")
    q.add_argument("--org", default=None); q.add_argument("--limit", default=100)
    q.set_defaults(func=cmd_org_audit)
    q = osub.add_parser("fleet", help="list one workspace's machines and backfill directives")
    q.add_argument("org_id"); q.add_argument("--limit", default=50)
    q.set_defaults(func=cmd_org_fleet)
    q = osub.add_parser("fetch", help="ask workspace clients to re-ship history on next SessionStart")
    q.add_argument("org_id"); q.add_argument("--since", default="",
                                              help="YYYY-MM-DD or ISO instant; omit for all history")
    q.add_argument("--email", default="", help="target one person; omit for the whole workspace")
    q.add_argument("--hostname", default="", help="narrow --email to one machine")
    q.set_defaults(func=cmd_org_fetch)
    q = osub.add_parser("fetch-revoke", help="cancel a live backfill directive")
    q.add_argument("directive_id"); q.set_defaults(func=cmd_org_fetch_revoke)
    osub.add_parser("list", help="list orgs + their active token counts").set_defaults(func=cmd_org_list)
    q = osub.add_parser("adopt-default",
                        help="name the built-in `default` corpus as a real org (moves no data)")
    q.add_argument("name", help="the workspace's display name, e.g. Acme")
    q.add_argument("--domain", default=None, help="sign-in domain that lands in it (docs/sso.md)")
    q.add_argument("--mint-invite", action="store_true",
                   help="also mint an invite (NOT recommended; see the printed note)")
    q.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    q.set_defaults(func=cmd_org_adopt_default)
    q = osub.add_parser("role", help="grant/remove workspace-admin for one person in an org")
    q.add_argument("org_id"); q.add_argument("email")
    q.add_argument("role", choices=("admin", "member"))
    q.set_defaults(func=cmd_org_role)
    osub.add_parser("admin-link", help="print a fresh one-time sign-in link for workspace.adminEmail "
                    "(run on the server; ends that person's browser sessions)").set_defaults(func=cmd_org_admin_link)
    q = osub.add_parser("requests", help="list workspace requests from the landing page (pending first)")
    q.add_argument("--all", action="store_true", help="include handled (approved) requests")
    q.set_defaults(func=cmd_org_requests)
    q = osub.add_parser("approve", help="approve a workspace request: create its org + print the invite ONCE")
    q.add_argument("request_id")
    q.add_argument("--domain", default=None,
                   help="bind the sign-in domain at approval so its people sign in with SSO")
    q.add_argument("--id", default=None,
                   help="explicit org id (default: slug of the company name; use when two names collide)")
    q.set_defaults(func=cmd_org_approve)
    q = osub.add_parser("invite", help="rotate an org's invite (old invite dies; user tokens keep working)")
    q.add_argument("org_id"); q.set_defaults(func=cmd_org_invite)
    q = osub.add_parser("tokens", help="list minted user tokens (hashes only — plaintext is never stored)")
    q.add_argument("--org", default=None); q.set_defaults(func=cmd_org_tokens)
    q = osub.add_parser("revoke", help="revoke a user token by token_hash (from `org tokens`) or plaintext; "
                                       "or --minted-by web to cut off every self-asserted web login at SSO cutover")
    q.add_argument("token", nargs="?", default=None)
    q.add_argument("--minted-by", default=None, metavar="LABEL",
                   help="revoke ALL active tokens with this mint label ('web' = invite + typed email, "
                        "'web-sso' = the issuer). Use at SSO cutover: enabling SSO does not revoke tokens "
                        "already minted under a self-declared identity.")
    q.add_argument("--org", default=None)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_org_revoke)
    q = osub.add_parser("delete", help="DELETE an org: its whole corpus, queue, extractions and tokens")
    q.add_argument("org_id"); q.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    q.set_defaults(func=cmd_org_delete)

    p = sub.add_parser("sso", help="sign in with an OpenID issuer (Okta, Entra, Keycloak, Google); see docs/sso.md")
    ssub = p.add_subparsers(dest="ssocmd", required=True)
    q = ssub.add_parser("set", help="configure the issuer; closes the invite sign-in paths")
    q.add_argument("--issuer", required=True, help="https URL the provider names as its issuer, e.g. https://dev-1.okta.com/oauth2/default")
    q.add_argument("--client-id", required=True, help="the OAuth client id the provider issued for CSA")
    q.add_argument("--name", default=None, help="what the sign-in button says (default: the issuer's host)")
    q.add_argument("--client-secret-stdin", action="store_true",
                   help="read the client secret from stdin (Google and Entra Web apps need one; Okta and Keycloak public clients do not)")
    q.set_defaults(func=cmd_sso_set)
    ssub.add_parser("show", help="print the configured issuer (never the secret)").set_defaults(func=cmd_sso_show)
    ssub.add_parser("clear", help="remove the issuer; sign-in is by invite again").set_defaults(func=cmd_sso_clear)

    args = ap.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        print(f"error: {e}")                             # a refusal, for an operator: no stack trace
        raise SystemExit(1)


if __name__ == "__main__":
    main()
