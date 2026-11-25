"""Deferred-extraction queue. Cheap always-on ingest persists a parsed+redacted session here; a deliberate
`csa.py drain` extracts them later. This decouples ingest from the `claude -p` spend so extraction — and the
usage-limit burn it draws on the miner's Claude login — is a controlled, rate-limited action instead of one
LLM call per POST (which re-extracted a growing session ~7x).

The files ARE the queue: data/pending/<session_id>.json, session_id-keyed so a re-POST of a still-growing
session overwrites — each distinct session is extracted once PER DRAIN. (A session that grows between drains
is still re-extracted in full — collapsing that repeat needs incremental/watermark extraction, a planned
follow-up.) Pure file plumbing: no LLM, no DB. drain_once takes extract/index as injected callables so this
module self-checks without either.
"""
import os, json, glob, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from analyzer import project

from analyzer import store, tracer

PENDING_DIR = "data/pending"   # under data/ so it rides the same docker volume as analyzer.db (survives redeploy);
#                                relative on purpose — _dir() anchors it to the cwd on every call, the way store.data_root does
_JSON = ".json"


def _iso(s):
    """Parse a session timestamp to an aware UTC datetime; None if absent/unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_since(s):
    """'7d'/'24h' (relative to now) or an ISO date/datetime -> aware UTC datetime. Raises ValueError on junk."""
    m = re.fullmatch(r"(\d+)\s*([dh])", s.strip(), re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return datetime.now(timezone.utc) - (timedelta(days=n) if unit == "d" else timedelta(hours=n))
    dt = _iso(s)
    if dt is None:
        raise ValueError(f"--since: expected 7d / 24h / an ISO date, got {s!r}")
    return dt


def _meta(path):
    """(owner, last-activity) of a queued session — (None, None) if unreadable. Time is `ended_at` (when the
    work actually happened), NOT file mtime: a backfilled old session is queued today but is not recent work."""
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        return None, None
    return d.get("user"), _iso(d.get("ended_at") or d.get("started_at"))


def _dir(org_id=None):
    """The default org keeps `data/pending` verbatim (module global, so the self-check can rebind it); a
    hosted org gets its own. The queue is keyed by session_id ALONE, and recall hands session ids out as
    provenance — so without this, org B queuing org A's session id would silently evict A's queued copy."""
    d = Path(PENDING_DIR).absolute() if not org_id or store.valid_org_id(org_id) == store.DEFAULT_ORG \
        else store.org_dir(org_id) / "pending"
    os.makedirs(d, exist_ok=True)
    return d


def queue_pending(sess, org_id=None):
    """Persist one parsed+redacted session for later drain. Atomic (tmp+replace) so a crash can't hand drain a
    truncated file; keyed by session_id so re-POSTs of a growing session collapse to one queued copy.
    session_id is validated (it is a path component — a '../' would write into another org's queue)."""
    dst = os.path.join(_dir(org_id), store.valid_session_id(sess["session_id"]) + _JSON)
    tmp = dst + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sess, fh)
    os.replace(tmp, dst)                     # ponytail: atomic rename, same idiom as csa._extract_sessions cache
    return dst


def discard(session_id, org_id=None):
    """Drop any queued copy of a session — a live (non-deferred) extract supersedes it, so flipping defer off
    can never double-extract what's already indexed. Also clears a quarantined copy. Org-scoped: deleting
    another tenant's queued session by id would be a cross-org write."""
    try:
        sid = store.valid_session_id(session_id)         # a '../' here would DELETE another org's queued file
    except ValueError:
        return                                            # a path-unsafe id could never have been queued -> nothing to drop
    base = _dir(org_id)
    for rel in (sid + _JSON, sid + _JSON + ".tmp", sid + _JSON + ".working",
                os.path.join("poison", sid + _JSON)):
        try:
            os.remove(os.path.join(base, rel))
        except OSError:
            pass


def stage(session_id, org_id=None):
    """Where a session sits in the queue RIGHT NOW: "queued" | "extracting" | "failed" | None (not here —
    drained, or never queued).

    This is the cross-process pipeline. Ingest and the ETL are different containers, so apiserver's
    in-memory _PIPELINE cannot see the drain's progress — but both share this directory on the data
    volume, and the queue already encodes the stage in the filenames: queue_pending writes <sid>.json,
    drain_once claims it by renaming to <sid>.json.working before calling the extractor, and a poisoned
    session lands in poison/. Reading it needs no lock, no new state, and no LLM: three stats.
    """
    try:
        sid = store.valid_session_id(session_id)
    except ValueError:
        return None                                       # path-unsafe id could never have been queued
    base = _dir(org_id)
    if os.path.exists(os.path.join(base, sid + _JSON + ".working")):
        return "extracting"                               # a drain has claimed it and is in `claude -p`
    if os.path.exists(os.path.join(base, sid + _JSON)):
        return "queued"                                   # waiting for a drain to pick it up
    if os.path.exists(os.path.join(base, "poison", sid + _JSON)):
        return "failed"                                   # quarantined: its own content broke extraction
    return None                                           # drained (or never ours)


def needs_drain(org_id=None):
    """True if this org has anything a drain must touch: a queued session, OR an orphaned `.working` claim
    left by a drain that was killed mid-extract (redeploy, OOM). `depth()` globs `*.json` only, so it misses
    `.working` — and drain_once reclaims `.working` only when it RUNS, which the loop gates on this. Without
    counting `.working`, a session claimed as the org's LAST queued item and then interrupted is stranded:
    depth()==0 forever, the loop never resubmits, and it reads 'extracting' in the pipeline indefinitely."""
    d = _dir(org_id)
    if glob.glob(os.path.join(d, "*" + _JSON)):
        return True
    return bool(glob.glob(os.path.join(d, "*" + _JSON + ".working")))


def peek(session_id, org_id=None):
    """Read a queued or QUARANTINED session back without claiming or removing it; None if it isn't here.

    The drain needs this to salvage deterministic evidence (harvest) from a session whose LLM extraction
    failed: by the time drain_once reports the failure it has already moved the file into poison/, so
    "the session dict the extractor choked on" is otherwise unreachable to the caller.
    """
    try:
        sid = store.valid_session_id(session_id)
    except ValueError:
        return None
    base = _dir(org_id)
    for rel in (sid + _JSON, sid + _JSON + ".working", os.path.join("poison", sid + _JSON)):
        try:
            with open(os.path.join(base, rel)) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue                                      # missing, or a torn/half-written file — not fatal
    return None


def depth(org_id=None, user=None, since=None):
    """(count, total_bytes) of ACTIVE queued sessions (excludes quarantined) — the free, no-LLM answer to
    'how much will draining cost?'. The non-recursive glob skips the poison/ subdir on purpose.
    `org_id` picks the org's own queue; `user`/`since` scope the count so --dry-run prices exactly the set a
    scoped drain would extract."""
    files = _pending_paths(org_id, user, since)
    return len(files), sum(os.path.getsize(f) for f in files)


def poison_depth(org_id=None):
    """How many sessions are quarantined (extraction failed — e.g. unparseable model output). Non-zero means a
    human should look: after fixing the cause, revive with `mv <org pending>/poison/* <org pending>/`."""
    return len(glob.glob(os.path.join(_dir(org_id), "poison", "*" + _JSON)))


def _quarantine(work_path, sid, org_id=None):
    """Park a failed session OUT of the active queue so a poison record can't re-burn usage on every drain.
    Reversible: the file is intact under poison/. Guarded — discard() may have raced us via a live re-POST."""
    pd = os.path.join(_dir(org_id), "poison")
    try:
        os.makedirs(pd, exist_ok=True)
        os.replace(work_path, os.path.join(pd, sid + _JSON))
    except OSError:
        pass


def _pending_paths(org_id=None, user=None, since=None):
    """Queued session files for one ORG, oldest-first. `user`/`since` narrow to one owner / to work done after a
    cutoff. Unfiltered stays a pure glob (no JSON reads); filtering costs one read per file, which is why the
    scope lives here — depth() and drain_once() must select the SAME set or --dry-run would misprice the drain.
    A session with no readable owner/timestamp can't be proven in-scope, so a filtered pass leaves it queued."""
    paths = sorted(glob.glob(os.path.join(_dir(org_id), "*" + _JSON)), key=os.path.getmtime)   # oldest first
    if user is None and since is None:
        return paths
    # ponytail: json.load of every queued file per filtered call (~65 MB / 1k sessions, a few seconds, and it
    # runs next to a minutes-long LLM drain). Index the owner/ts into the filename or a sidecar only if the
    # queue outgrows that — the unfiltered path above never pays it.
    out = []
    for p in paths:
        owner, ts = _meta(p)
        if user is not None and owner != user:
            continue
        if since is not None and (ts is None or ts < since):
            continue
        out.append(p)
    return out


def drain_once(extract_fn, index_fn, limit=None, is_poison=None, org_id=None, user=None, since=None):
    """Extract+index queued sessions oldest-first: delete on success, and on failure either quarantine (poison)
    or leave-queued-and-stop (systemic). `limit` caps how many run this pass — the spend lever. extract_fn(sess)
    ->data and index_fn(sess, data) are injected so the file plumbing is testable without an LLM or a DB.

    `is_poison(exc)->bool` classifies a failure: True (the default when unset — every failure) => the session's
    OWN content is bad (unparseable output, corrupt file), so quarantine it to poison/ where it can't re-burn on
    the next drain. False (e.g. login expired / usage limit / transport down) => a systemic failure, so leave the
    session queued and STOP the batch — otherwise one outage would mass-quarantine an entire good backlog.

    `user`/`since` scope the pass to one owner / to sessions worked after a cutoff — out-of-scope sessions stay
    queued, untouched and unbilled, for a later drain.

    Concurrency: claim-by-rename (.working) makes a killed drain visible, but two concurrent drains CAN still
    double-spend — the no-overlap guarantee comes from the flock the caller (cmd_drain) holds. drain_once alone
    is not concurrency-safe. Returns (done, failed, poisoned): done=[(sid, sess, data)], failed=[(sid, error)],
    poisoned=[sid] — the subset of failures that were QUARANTINED (own content bad), NOT systemic ones that
    stay queued. The caller harvest-salvages only `poisoned`; harvesting a systemic-failed session would
    rebuild the corpus every drain for the whole outage while making no forward progress."""
    d = _dir(org_id)
    for w in glob.glob(os.path.join(d, "*" + _JSON + ".working")):    # reclaim a killed prior drain's in-flight file
        dst = w[: -len(".working")]
        try:
            if os.path.exists(dst):          # a fresh re-POST already replaced it — drop the stale snapshot
                os.remove(w)
            else:
                os.replace(w, dst)
        except OSError:
            pass                             # a concurrent discard()/drain raced us — nothing to reclaim, skip
    paths = _pending_paths(org_id, user, since)
    if limit is not None:
        paths = paths[:limit]
    done, failed, poisoned = [], [], []
    for p in paths:
        sid = os.path.basename(p)[: -len(_JSON)]
        work = p + ".working"
        try:
            os.replace(p, work)              # claim atomically; a racing drain that lost the file just skips
        except OSError:
            continue
        try:
            with open(work) as fh:
                sess = json.load(fh)
            if sess.get("cwd"):              # re-derive project from the queued cwd: `project` was frozen by
                sess["project"] = project.normalize_project(sess["cwd"])   # queue_pending at POST time, under
            #   whatever rule was live THEN. Without this, draining a session queued before the normalization
            #   fix re-INSERTs its stale project (load_sqlite: INSERT OR REPLACE) and silently un-does the backfill.
            # One trace per session's ingest. root=True: the ETL pool REUSES threads across orgs, so a
            # leaked span here would cross-link tenants (and their corpus files) — reset is mandatory.
            with tracer.span("ingest", "CHAIN", db=store.org_db(org_id), root=True,
                             **{"session.id": sid, "csa.org_id": org_id or store.DEFAULT_ORG,
                                "user.id": sess.get("user"), "csa.project": sess.get("project")}) as sp:
                with tracer.span("extract", "CHAIN"):    # the llm span (model/tokens/cost) nests under this
                    data = extract_fn(sess)
                with tracer.span("index", "CHAIN"):
                    index_fn(sess, data)
                sp.set(**{"csa.ingest.n_facts": len(data.get("hidden_context") or [])})
            os.remove(work)
            done.append((sid, sess, data))
        except Exception as e:
            failed.append((sid, str(e)))
            if is_poison is not None and not is_poison(e):   # systemic (login/limit/transport): don't mislabel
                try:
                    os.replace(work, p)      # put it back and stop — the rest of the backlog is fine
                except OSError:
                    pass
                break
            _quarantine(work, sid, org_id)   # genuine poison (unparseable content / corrupt file): park it
            poisoned.append(sid)             # ONLY these are the caller's to harvest-salvage; a systemic
            #                                  failure left the session queued for retry, so harvesting it
            #                                  would rebuild the corpus every tick for the whole outage.
    return done, failed, poisoned


if __name__ == "__main__":
    import tempfile, shutil
    tracer.disable()                         # self-check must not write spans into the real data/analyzer.db
    PENDING_DIR = tempfile.mkdtemp()         # rebinds the module global the helpers read
    try:
        # stage(): the cross-process pipeline. Must track the queue's own filenames exactly.
        assert stage("nope") is None, "a session that was never queued has no stage"
        queue_pending({"session_id": "st1", "transcript": "t", "project": "p"})
        assert stage("st1") == "queued", "a queued session reads as queued"
        os.replace(os.path.join(PENDING_DIR, "st1" + _JSON), os.path.join(PENDING_DIR, "st1" + _JSON + ".working"))
        assert stage("st1") == "extracting", "drain_once's .working claim must read as extracting"
        os.makedirs(os.path.join(PENDING_DIR, "poison"), exist_ok=True)
        os.replace(os.path.join(PENDING_DIR, "st1" + _JSON + ".working"),
                   os.path.join(PENDING_DIR, "poison", "st1" + _JSON))
        assert stage("st1") == "failed", "a quarantined session reads as failed"
        os.remove(os.path.join(PENDING_DIR, "poison", "st1" + _JSON))
        assert stage("st1") is None, "a drained session leaves the queue -> no stage"
        assert stage("../../etc/passwd") is None, "a path-unsafe id must never stat outside the queue"

        # peek(): the drain salvages harvest from a POISONED session, so it must read poison/ too.
        assert peek("nope") is None, "a session that isn't queued peeks as None"
        queue_pending({"session_id": "pk1", "transcript": "body", "project": "p"})
        assert peek("pk1")["transcript"] == "body", "a queued session peeks back whole"
        os.makedirs(os.path.join(PENDING_DIR, "poison"), exist_ok=True)
        os.replace(os.path.join(PENDING_DIR, "pk1" + _JSON), os.path.join(PENDING_DIR, "poison", "pk1" + _JSON))
        assert peek("pk1")["transcript"] == "body", "a QUARANTINED session must still peek (harvest salvage)"
        assert peek("../../etc/passwd") is None, "a path-unsafe id must never read outside the queue"
        discard("pk1")

        # needs_drain(): an orphaned .working (killed drain) must still trigger a drain even when depth()==0,
        # or the session is stranded 'extracting' forever.
        assert needs_drain() is False, "empty queue needs no drain"
        queue_pending({"session_id": "nd1", "transcript": "t", "project": "p"})
        assert needs_drain() is True and depth()[0] == 1, "a queued session needs a drain"
        os.replace(os.path.join(PENDING_DIR, "nd1" + _JSON), os.path.join(PENDING_DIR, "nd1" + _JSON + ".working"))
        assert depth()[0] == 0, "depth() globs *.json only — a .working is invisible to it (the trap)"
        assert needs_drain() is True, "an orphaned .working MUST still trigger a drain (reclaim), depth can't see it"
        os.remove(os.path.join(PENDING_DIR, "nd1" + _JSON + ".working"))
        assert needs_drain() is False, "reclaimed -> nothing left to drain"

        queue_pending({"session_id": "s1", "transcript": "hello", "project": "p"})
        queue_pending({"session_id": "s1", "transcript": "hello world", "project": "p"})   # re-POST overwrites
        assert depth()[0] == 1, "session_id-keyed: a re-POST must collapse to one queued file"
        queue_pending({"session_id": "s2", "transcript": "t2", "project": "p"})
        queue_pending({"session_id": "s3", "transcript": "boom", "project": "p"})           # will fail extract

        indexed = []
        def fake_extract(sess):
            if "boom" in sess["transcript"]:
                raise RuntimeError("extract failed")
            return {"hidden_context": [1], "_cost_usd": 0.01}
        def fake_index(sess, data):
            indexed.append(sess["session_id"])

        done, failed, pois = drain_once(fake_extract, fake_index, limit=1)
        assert len(done) + len(failed) == 1, "limit=1 must process exactly one session"
        done2, failed2, pois2 = drain_once(fake_extract, fake_index)                          # drain the rest
        ok_ids = {sid for sid, _, _ in done + done2}
        bad_ids = {sid for sid, _ in failed + failed2}
        assert ok_ids == set(indexed), "index_fn must run for exactly the drained-ok sessions"
        assert bad_ids == {"s3"}, f"poison session must fail and no other, got {bad_ids}"
        assert set(pois + pois2) == {"s3"}, "poisoned list = exactly the quarantined sids (harvest salvage targets)"
        remaining = {os.path.basename(p)[: -len(_JSON)] for p in _pending_paths()}
        assert remaining == set(), f"drained removed + poison quarantined -> active queue empty, got {remaining}"
        assert poison_depth() == 1 and depth()[0] == 0, "failed session is quarantined OUT of the active queue"
        assert not glob.glob(os.path.join(PENDING_DIR, "*.working")), "no .working left behind"
        discard("s3")
        assert poison_depth() == 0, "discard() also clears a quarantined copy (live re-POST supersedes)"

        # systemic failure (is_poison=False): leave the batch queued and STOP — never mass-quarantine a good backlog
        queue_pending({"session_id": "sysA", "transcript": "boom", "project": "p"})   # fails first (oldest)
        queue_pending({"session_id": "sysB", "transcript": "ok", "project": "p"})
        d3, f3, p3 = drain_once(fake_extract, fake_index, is_poison=lambda e: False)
        assert len(f3) == 1 and depth()[0] == 2, "systemic failure keeps the whole batch queued and stops"
        assert poison_depth() == 0, "systemic failure must NOT quarantine (it's not the session's fault)"
        assert p3 == [], "systemic failure poisons nothing -> nothing to harvest-salvage (no rebuild storm)"

        # --user/--since scope: drain ONE owner's recent work; everything else stays queued and unbilled.
        # Fixed dates + an explicit cutoff keep this deterministic (no dependence on today's date).
        # (org_id is the first param now, so user/since MUST be keyword — depth("tomasz") would be an org id.)
        for sid, who, when in (("g_new", "tomasz@example.com", "2026-07-15T10:00:00Z"),
                               ("g_old", "tomasz@example.com", "2026-01-01T10:00:00Z"),
                               ("s_new", "hermes@example.com", "2026-07-15T10:00:00Z")):
            queue_pending({"session_id": sid, "transcript": "ok", "project": "p", "user": who, "ended_at": when})
        assert depth(user="tomasz@example.com")[0] == 2, "depth(user) counts only that owner (prices what --user drains)"
        cutoff = parse_since("2026-07-01")
        assert depth(user="tomasz@example.com")[0] == 2 and len(_pending_paths(user="tomasz@example.com", since=cutoff)) == 1, \
            "--since must drop the owner's pre-cutoff session"
        d4, f4, _p4 = drain_once(fake_extract, fake_index, user="tomasz@example.com", since=cutoff)
        assert [sid for sid, _, _ in d4] == ["g_new"] and not f4, f"scoped drain must take only g_new, got {d4}"
        assert depth(user="tomasz@example.com")[0] == 1, "the owner's out-of-window session stays queued (unbilled)"
        assert depth(user="hermes@example.com")[0] == 1, "another owner's session must never be touched by a --user drain"
        assert len(_pending_paths()) == 4, "sysA/sysB (no owner/ts) can't be proven in-scope -> left queued"
        # relative forms resolve against now; ISO forms are absolute
        assert abs((datetime.now(timezone.utc) - parse_since("7d")).total_seconds() - 7 * 86400) < 5, "7d -> a week ago"
        assert abs((datetime.now(timezone.utc) - parse_since("24h")).total_seconds() - 86400) < 5, "24h -> a day ago"
        for junk in ("", "week", "7x", "7 days"):
            try:
                parse_since(junk); assert False, f"--since must reject {junk!r}, not silently drain everything"
            except ValueError:
                pass

        # org isolation composes with user/since: a hosted org's queue is a DIFFERENT dir from `default`,
        # drainable on its own, so a queued hosted session is never stranded (the data-loss the org layer fixed).
        import tempfile as _tf
        _root = _tf.mkdtemp()
        _cwd = os.getcwd()
        os.chdir(_root)                                  # store.org_dir() anchors data/ to the CURRENT cwd
        try:
            queue_pending({"session_id": "h1", "transcript": "t", "project": "p"}, org_id="acme")
            assert depth(org_id="acme")[0] == 1 and _dir("acme") != _dir(None), "hosted queue is a distinct dir"
            assert _dir("acme").is_absolute() and _dir("acme").parts[-4:] == ("data", "orgs", "acme", "pending")
            hi = []
            drain_once(lambda s: {"hidden_context": [1]}, lambda s, d: hi.append(s["session_id"]), org_id="acme")
            assert hi == ["h1"] and depth(org_id="acme")[0] == 0, "a hosted org's queue drains on its own (no data loss)"
        finally:
            os.chdir(_cwd)
            shutil.rmtree(_root, ignore_errors=True)
        print("OK queue: dedup write, limit clamp, poison-quarantine, systemic-keep-and-stop, depth/discard, "
              "user+since scope (other owners untouched, junk --since rejected), per-org queue drains independently")
    finally:
        shutil.rmtree(PENDING_DIR, ignore_errors=True)
