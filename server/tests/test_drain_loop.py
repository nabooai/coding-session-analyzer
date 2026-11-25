"""drain_loop._compact_tick: queued work wins over compaction.

Compaction is O(corpus) and shares the per-org lock with the drain. On 2026-09-01 a ~25-min pass ran after
every 5-session chunk, so a 190-session backfill drained at 5 per half hour and the whole team's afternoon
queued behind it. The policy under test: a dirty org compacts when its queue is EMPTY (past the min spacing),
or — idle or not — once compact_max minutes have passed since its last COMPLETED pass (the starvation valve).
The spacing/dirty/deprovisioned rules stay covered by the module's __main__ selfcheck."""
import pytest

from analyzer import drain_loop as dl


class _Pool:
    def __init__(self):
        self.seen = []

    def submit(self, fn, org):
        self.seen.append(org)
        return None


ORGS = lambda: ["a", "b"]
MIN, MAX = 30, 180                      # minutes: the loop's defaults


@pytest.fixture(autouse=True)
def _clean_state():
    dl._DIRTY.clear(); dl._COMPACT_LAST.clear(); dl._COMPACT_DONE.clear()
    yield
    dl._DIRTY.clear(); dl._COMPACT_LAST.clear(); dl._COMPACT_DONE.clear()


def _tick(pool, now, idle, compact_max=MAX, compact_min=MIN):
    return dl._compact_tick(pool, ORGS, compact_min, now=now, compact_max=compact_max, idle=lambda org: idle)


def test_busy_queue_defers_compaction_and_keeps_the_org_dirty():
    p = _Pool()
    dl._DIRTY.add("a")
    _tick(p, now=1000, idle=False)
    assert p.seen == [] and "a" in dl._DIRTY, "work queued -> no compaction, and the org stays dirty for later"
    _tick(p, now=1000 + 60 * 60, idle=False)      # an hour later, still draining: still waits (< compact_max)
    assert p.seen == []


def test_idle_queue_compacts_past_the_min_spacing():
    p = _Pool()
    dl._DIRTY.add("a")
    _tick(p, now=1000, idle=True)
    assert p.seen == ["a"] and "a" not in dl._DIRTY
    dl._DIRTY.add("a")                            # a drain landed and finished
    _tick(p, now=1000 + 10 * 60, idle=True)       # inside the spacing: no storm, even when idle
    assert p.seen == ["a"]
    _tick(p, now=1000 + 31 * 60, idle=True)
    assert p.seen == ["a", "a"]


def test_starvation_valve_compacts_a_never_idle_org_at_compact_max():
    p = _Pool()
    dl._DIRTY.add("a")
    t0 = 1000
    _tick(p, now=t0, idle=False)                  # first sight stamps the "done" clock at t0
    _tick(p, now=t0 + (MAX - 1) * 60, idle=False)
    assert p.seen == [], "one minute short of the valve, a busy org still waits"
    _tick(p, now=t0 + MAX * 60, idle=False)
    assert p.seen == ["a"], "at compact_max the pass runs even though the queue has work"


def test_valve_measures_completed_passes_not_submissions():
    # A submitted pass that was skipped (drain held the lock) must not reset the valve: _retry drops
    # _COMPACT_LAST but _COMPACT_DONE only moves when _compact_one actually finishes.
    p = _Pool()
    dl._DIRTY.add("a")
    _tick(p, now=1000, idle=True)                 # submitted...
    dl._retry("a")                                # ...but skipped; re-armed for the next tick
    _tick(p, now=1000 + 10, idle=False)           # queue got work meanwhile: waits (retry does not bypass idle)
    assert p.seen == ["a"] and "a" in dl._DIRTY
    _tick(p, now=1000 + 20, idle=True)            # queue empties: retries right away, not a cadence later
    assert p.seen == ["a", "a"]
    dl._COMPACT_DONE["a"] = 1000 + 20             # the pass completes (what _compact_one records)
    dl._DIRTY.add("a")
    _tick(p, now=1000 + 20 + MAX * 60 - 1, idle=False)
    assert p.seen == ["a", "a"], "the valve counts from the COMPLETED pass"
    _tick(p, now=1000 + 20 + MAX * 60, idle=False)
    assert p.seen == ["a", "a", "a"]


def test_compact_max_none_is_the_old_cadence_only_behaviour():
    # No valve configured -> compact_max == compact_min: a busy org waits ONE cadence from first sight,
    # then compacts busy or not — the pre-policy rhythm, minus the immediate pass on a fresh process.
    p = _Pool()
    dl._DIRTY.add("a")
    dl._compact_tick(p, ORGS, MIN, now=1000, idle=lambda org: False)
    assert p.seen == [], "first sight of a busy org: wait a cadence, do not compact on boot"
    dl._compact_tick(p, ORGS, MIN, now=1000 + MIN * 60, idle=lambda org: False)
    assert p.seen == ["a"], "past the cadence with no valve configured -> compact, busy or not"


def test_once_path_forces_a_pass_regardless_of_queue():
    p = _Pool()
    dl._DIRTY.add("a")
    dl._compact_tick(p, ORGS, 0, now=1000, compact_max=0, idle=lambda org: False)
    assert p.seen == ["a"]


def test_default_idle_asks_the_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(dl.queue, "needs_drain", lambda org=None, *a, **k: (calls.append(org), True)[1])
    p = _Pool()
    dl._DIRTY.add("a")
    dl._compact_tick(p, ORGS, MIN, now=1000, compact_max=MAX)
    assert calls == ["a"] and p.seen == [], "the default idle predicate is queue.needs_drain (work -> wait)"


def test_compact_max_never_below_compact_min():
    assert dl.COMPACT_MAX_MIN >= dl.COMPACT_MIN
