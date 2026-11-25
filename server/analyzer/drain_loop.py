"""The ETL service (`csa-etl`): drains every org's queue, continuously and in parallel. /api/ingest only
writes a queue file, so nobody waits on extraction.

Each tenant gets its own task in a long-lived pool, so a big backlog never delays another org: the drain
lock is per org and each corpus is its own SQLite file. analyzer.budget is the limiter: an org over its
daily spend is skipped until tomorrow.

drain_workers is a quota decision, not a CPU one: every extraction goes through the same model
account. It and the extraction model are read from control.db every tick, so a Settings change lands
within one tick.
"""
import logging, pathlib, time
from concurrent.futures import ThreadPoolExecutor

from analyzer import archive, budget, orgs, queue, store, tenant, tracer

log = logging.getLogger("drain_loop")

CHUNK = 5                 # sessions per drain_once call
TICK_S = 10
REGISTRY_MIN = 60         # registry sweep cadence
COMPACT_MIN = 30          # corpus-wide re-cluster + FTS: minimum spacing, runs when the queue is idle
COMPACT_MAX_MIN = 180     # ...and at most this long apart, idle or not
PURGE_MIN = 60            # retention sweep for kept transcripts
UPKEEP_WORKERS = 3        # registry, compaction and purge share this pool, never the drains
HEARTBEAT = pathlib.Path("/tmp/etl-heartbeat")   # touched each tick; the k8s etl liveness probe restarts a stalled loop


def _orgs():
    """Every tenant with a queue: the built-in default plus each provisioned org. A new tenant is drained
    because it exists — there is no ETL-side registry to keep in sync, which is the whole surface."""
    try:
        return orgs.corpus_orgs()
    except Exception as e:                                    # control plane unreadable -> still serve default
        log.warning("org list unavailable (%s) — draining %s only", str(e)[:80], store.DEFAULT_ORG)
        return [store.DEFAULT_ORG]


def _drain_one(org, model, chunk):
    """One org's turn. Never raises: a thrown task would kill this org's slot for the process's lifetime."""
    import csa                                               # the single drain implementation, shared with the CLI
    try:
        con = store.sqlite_db(store.org_db(org))             # own connection: not shareable across threads
        try:
            spend = budget.over_budget(con)
        finally:
            con.close()
        if spend.over:
            log.info("[%s] over daily %s budget ($%.2f of $%.2f, %d of %d tokens), skipping until tomorrow",
                     org, spend.over, spend.usd, spend.usd_cap, spend.tokens, spend.tokens_cap)
            return 0
        done, failed, poisoned, _actions, busy = csa.drain_org(org, model, chunk, compact=False)   # _compact_tick
        if busy:
            return 0                                          # a `csa.py drain` holds the lock; it'll be free next tick
        pset = set(poisoned)
        stalled = False
        for sid, err in failed:
            if sid in pset:                                   # bad content: set aside, the backlog keeps moving
                log.warning("[%s] quarantined %s: %s", org, sid[:12], err[:120])
            else:                                             # systemic: the whole backlog is stuck. This is the alert
                log.error("[%s] extraction STALLED — no progress, backlog not draining. Cause: %s",
                          org, err[:200])
                orgs.set_extraction_stall(org, err)
                stalled = True
        if done:
            if not stalled:
                orgs.clear_extraction_stall(org)
            log.info("[%s] drained %d session(s)", org, len(done))
            _DIRTY.add(org)                                   # something to compact; _compact_tick decides when
            _judge(org, [sid for sid, _sess, data in done if (data.get("hidden_context") or [])])
        return len(done)
    except Exception as e:
        log.warning("[%s] drain failed: %s", org, str(e)[:200])
        return 0


def _judge(org, session_ids):
    """Label the facts this drain just produced, unless the workspace's autojudge setting is off. A
    failure never touches the drain."""
    if not session_ids:
        return
    try:
        from analyzer import judge
        con = store.sqlite_db(store.org_db(org))
        try:
            if not tenant.config(con).autojudge:
                return
            for sid in session_ids:
                # its own trace per session (root=True: a reused pool thread); session.id joins them
                with tracer.span("judge", "CHAIN", db=store.org_db(org), root=True,
                                 **{"session.id": sid, "csa.org_id": org}) as sp:
                    n = judge.run(con, session=sid)
                    sp.set(**{"csa.judge.n_reviewed": sum(n.values()) if isinstance(n, dict) else n})
        finally:
            con.close()
    except Exception as e:
        log.warning("[%s] autojudge failed: %s", org, str(e)[:200])


_DRAIN_LAST = {}                                           # org -> monotonic time of its last drain submit
_REG_LAST = {}                                               # org -> monotonic secs of last registry sweep
# These cadence markers are in-process on purpose: a restart costs one redundant, idempotent pass.


def _registry_one(org):
    """One org's registry sync and verify sweep. Never raises; a no-op unless registry_sources is set.
    Not behind queue.needs_drain: registries change while ingest idles."""
    try:
        from analyzer import verify
        con = store.sqlite_db(store.org_db(org))
        try:
            n = verify.sync_and_sweep(con, org)
            if n:
                log.info("[%s] registry sweep: %d fact row(s) changed", org, n)
        finally:
            con.close()
    except Exception as e:
        log.warning("[%s] registry sweep failed: %s", org, str(e)[:200])


def _registry_tick(pool, orgs, reg_min, now=None):
    """Submit a sweep for every org whose last sweep is older than reg_min minutes. Marks BEFORE
    submitting — a slow sweep must not be resubmitted every scheduler pass."""
    now = time.monotonic() if now is None else now
    futs = []
    for org in orgs():
        if now - _REG_LAST.get(org, -1e12) >= reg_min * 60:
            _REG_LAST[org] = now
            futs.append(pool.submit(_registry_one, org))
    return futs


_COMPACT_LAST = {}                                           # org -> monotonic secs of last compaction, and
_DIRTY = set()                                               # the orgs that have indexed something since
_COMPACT_DONE = {}                                           # org -> monotonic secs of last COMPLETED pass:
#                                                              the starvation valve's measure, unlike _COMPACT_LAST


def _compact_one(org):
    """One org's corpus-wide pass. Never raises: without it the corpus is still servable, only lagging.
    A failed or skipped pass re-marks the org dirty, so the next tick retries."""
    import csa
    try:
        if not csa.compact_org(org):                          # a drain holds the lock — try again next tick
            _retry(org)
            return
        _COMPACT_DONE[org] = time.monotonic()
        log.info("[%s] compacted (clusters + FTS)", org)
    except Exception as e:
        _retry(org)
        log.warning("[%s] compaction failed: %s", org, str(e)[:200])


def _retry(org):
    """Re-arm an org that did NOT compact. Dropping _COMPACT_LAST is the half that matters: _compact_tick
    stamps `now` before submitting (so a slow pass isn't resubmitted every tick), and leaving that stamp on
    a pass that never ran makes the retry wait a full COMPACT_MIN — on a continuously-fed org, which
    always holds the drain lock, that starves compaction for hours. Compaction is what heals the corpus-wide
    stats the per-session refresh cannot (see store.entity_scope_for_session), so this is not just latency."""
    _DIRTY.add(org)
    _COMPACT_LAST.pop(org, None)


def _compact_tick(pool, orgs, compact_min, now=None, compact_max=None, idle=None):
    """Submit a compaction for every dirty org past compact_min minutes since its last pass whose queue
    is idle, or, idle or not, past compact_max minutes since its last completed pass. Queued work wins:
    compaction is O(corpus) and holds the drain's lock. compact_max=None means compact_min; 0 forces a
    pass. Marks and clears before submitting, so a slow pass is not resubmitted. `idle` is injectable."""
    now = time.monotonic() if now is None else now
    if compact_max is None:
        compact_max = compact_min
    if idle is None:
        idle = lambda org: not queue.needs_drain(org)
    known, futs = set(orgs()), []
    for org in list(_DIRTY):
        if org not in known:                                  # org deprovisioned mid-flight
            _DIRTY.discard(org)
            continue
        if now - _COMPACT_LAST.get(org, -1e12) < compact_min * 60:
            continue                                          # inside the minimum spacing — no compaction storm
        overdue = now - _COMPACT_DONE.setdefault(org, now) >= compact_max * 60
        if not overdue and not idle(org):
            continue                                          # queued work wins; compact when the queue empties
        _COMPACT_LAST[org] = now
        _DIRTY.discard(org)
        futs.append(pool.submit(_compact_one, org))
    return futs


_PURGE_LAST = {}                                             # org -> monotonic secs of last retention purge


def _purge_one(org):
    """Enforce one org's transcript retention window. Never raises: a failure is logged, never a crashed
    worker that also stops extraction."""
    try:
        n = archive.purge(org)
        if n:
            log.info("[%s] archive: purged %d transcript(s) past the retention window", org, n)
    except Exception as e:
        log.warning("[%s] archive purge failed: %s", org, str(e)[:200])


def _purge_tick(pool, orgs, purge_min, now=None):
    """Submit a retention purge for every org whose last one is older than purge_min minutes, whether or
    not archiving is on: archive_days 0 must clear what is stored. Stamps before submitting."""
    now = time.monotonic() if now is None else now
    futs = []
    for org in orgs():
        if now - _PURGE_LAST.get(org, -1e12) >= purge_min * 60:
            _PURGE_LAST[org] = now
            futs.append(pool.submit(_purge_one, org))
    return futs


def _tick(pool, inflight, orgs, submit, workers=None):
    """One scheduling pass: for every org not already draining and with a non-empty queue, submit a task,
    while fewer than `workers` drains run. Pure control-flow (no sleep, injected `submit`), so the
    no-barrier invariant is unit-testable: an org whose task is still running is skipped, never joined.
    Under the cap the org drained longest ago goes first, so a busy org cannot starve a quiet one.
    Returns inflight for chaining."""
    for org in sorted(orgs(), key=lambda o: _DRAIN_LAST.get(o, 0)):
        f = inflight.get(org)
        if f and not f.done():
            continue                                         # this tenant is still draining — don't pile on
        if f:
            inflight.pop(org, None)
        if workers is not None and sum(not g.done() for g in inflight.values()) >= workers:
            break                                            # the rest wait for the next tick
        if queue.needs_drain(org):          # queued work OR an orphaned .working to reclaim (see needs_drain)
            inflight[org] = submit(org)
            _DRAIN_LAST[org] = time.monotonic()
    return inflight


def loop(once=False):
    cfg = tenant.deployment()
    log.info("ETL up: workers=%d model=%s (read every call) chunk=%d tick=%ds registry=%dm compact=%dm "
             "(when idle) / %dm (max)", cfg.drain_workers, cfg.main_model or "environment default", CHUNK, TICK_S,
             REGISTRY_MIN, COMPACT_MIN, COMPACT_MAX_MIN)
    pool = ThreadPoolExecutor(max_workers=tenant.MAX_DRAIN_WORKERS)   # drains, capped per tick by drain_workers
    upkeep = ThreadPoolExecutor(max_workers=UPKEEP_WORKERS)          # registry sweeps, compaction, purges
    inflight = {}                                            # org -> Future; the no-barrier bit
    while True:
        HEARTBEAT.touch()
        cfg = tenant.deployment()
        _tick(pool, inflight, _orgs, lambda org: pool.submit(_drain_one, org, "main", CHUNK),
              workers=cfg.drain_workers)
        reg_futs = _registry_tick(upkeep, _orgs, REGISTRY_MIN)
        comp_futs = _compact_tick(upkeep, _orgs, COMPACT_MIN, compact_max=COMPACT_MAX_MIN)
        purge_futs = _purge_tick(upkeep, _orgs, PURGE_MIN)
        if once:
            for f in list(inflight.values()) + reg_futs + comp_futs + purge_futs:
                f.result()
            for f in _compact_tick(upkeep, _orgs, 0, compact_max=0):  # the drain just finished and nothing else will
                f.result()                                   # tick: compact NOW, ignoring the cadence, so a
            return                                           # one-shot run still returns a consistent corpus
        time.sleep(TICK_S)


def _selfcheck():
    # The no-barrier invariant: a busy org is SKIPPED, never joined, so a slow tenant can't hold up a fast
    # one. This is the whole reason the loop isn't a pool.map() per cycle.
    import threading
    global queue
    real_needs = queue.needs_drain
    queue.needs_drain = lambda org=None, *a, **k: True       # every org always has work
    try:
        gate = threading.Event()
        submitted = []

        class SlowFuture:
            def done(self):
                return gate.is_set()                          # "still running" until we open the gate

        def submit(org):
            submitted.append(org)
            return SlowFuture()

        inflight = {}
        _tick(None, inflight, lambda: ["a", "b"], submit)
        assert submitted == ["a", "b"], f"first tick submits both orgs, got {submitted}"

        _tick(None, inflight, lambda: ["a", "b"], submit)     # a & b still 'running'
        assert submitted == ["a", "b"], "a busy org must be SKIPPED, not resubmitted or joined"

        gate.set()                                            # both finish
        _tick(None, inflight, lambda: ["a", "b"], submit)
        assert submitted == ["a", "b", "a", "b"], f"finished orgs are resubmitted next tick, got {submitted}"

        # A fast org still progresses while a slow one runs: freeze only 'slow'.
        class OrgFuture:
            def __init__(self, org):
                self.org = org
            def done(self):
                return self.org != "slow"

        inflight, seen = {}, []
        submit2 = lambda org: (seen.append(org), OrgFuture(org))[1]
        _tick(None, inflight, lambda: ["slow", "fast"], submit2)
        _tick(None, inflight, lambda: ["slow", "fast"], submit2)
        assert seen.count("fast") == 2, "fast org drains every tick while slow one is still running"
        assert seen.count("slow") == 1, "slow org is not resubmitted while its task is in flight"
    finally:
        queue.needs_drain = real_needs

    # Compaction cadence: only DIRTY orgs, only past the cadence, marked before submit (so a slow pass is
    # never resubmitted), and a drain landing during the pass re-dirties for the NEXT one. Every org is
    # idle here — the queue-vs-compaction policy has its own tests (tests/test_drain_loop.py).
    real_needs = queue.needs_drain
    queue.needs_drain = lambda org=None, *a, **k: False
    _DIRTY.clear(); _COMPACT_LAST.clear(); _COMPACT_DONE.clear()
    class _Pool:
        def __init__(self): self.seen = []
        def submit(self, fn, org): self.seen.append(org); return None
    p, orgs = _Pool(), lambda: ["a", "b"]
    assert _compact_tick(p, orgs, 30, now=1000) == [] and not p.seen, "nothing dirty -> no compaction"
    _DIRTY.add("a")
    _compact_tick(p, orgs, 30, now=1000)
    assert p.seen == ["a"], f"a dirty org past its cadence compacts, got {p.seen}"
    _DIRTY.add("a")                                          # a drain landed while the pass was running
    _compact_tick(p, orgs, 30, now=1000 + 29 * 60)
    assert p.seen == ["a"], "inside the cadence a dirty org waits — no compaction storm"
    _compact_tick(p, orgs, 30, now=1000 + 31 * 60)
    assert p.seen == ["a", "a"], f"past the cadence the re-dirtied org compacts again, got {p.seen}"
    _DIRTY.add("gone")
    _compact_tick(p, orgs, 30, now=1e9)
    assert "gone" not in p.seen and "gone" not in _DIRTY, "a deprovisioned org is dropped, not compacted"
    # a pass that did NOT run (drain held the lock) retries on the NEXT tick, not a cadence later — a
    # continuously-fed org always holds that lock, so stamping it as compacted starves it for hours
    _DIRTY.clear(); _COMPACT_LAST.clear()
    q = _Pool()
    _DIRTY.add("a")
    _compact_tick(q, orgs, 30, now=5000)
    _retry("a")                                              # what _compact_one does on skip/failure
    _compact_tick(q, orgs, 30, now=5000 + 60)                # one tick later, deep inside the cadence
    assert q.seen == ["a", "a"], f"a skipped compaction must retry next tick, got {q.seen}"
    _DIRTY.clear(); _COMPACT_LAST.clear(); _COMPACT_DONE.clear()
    queue.needs_drain = real_needs

    # --- retention purge: every org, on a cadence, whether or not archiving is on right now -------
    _PURGE_LAST.clear()
    pp = _Pool()
    list(_purge_tick(pp, orgs, 60, now=1000))
    assert pp.seen == ["a", "b"], f"every org is swept, not only dirty ones, got {pp.seen}"
    _purge_tick(pp, orgs, 60, now=1000 + 59 * 60)
    assert pp.seen == ["a", "b"], "inside the cadence nothing re-runs"
    _purge_tick(pp, orgs, 60, now=1000 + 61 * 60)
    assert pp.seen == ["a", "b", "a", "b"], f"past the cadence every org sweeps again, got {pp.seen}"
    _PURGE_LAST.clear()

    print("OK drain_loop: no-barrier scheduling — busy org skipped, fast org never blocked by slow org; "
          "compaction only for dirty orgs past the cadence (idle gate: tests/test_drain_loop.py); "
          "retention purge sweeps every org on its own cadence (unconditional — a disabled archive "
          "must still be cleared)")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "selfcheck" in sys.argv:
        _selfcheck()
    else:
        loop()
