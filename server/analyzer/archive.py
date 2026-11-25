"""Retained session transcripts: the redacted text, kept for a bounded window. Put at ingest, read by
the owner or a workspace admin, purged on a clock. The corpus itself keeps derived facts only.

1. Off unless asked for: `tenant_config.archive_days` defaults to 0, store nothing (`csa.py org config
   <org> --set archive_days=...`). The client discloses the number at setup (cliauth.policy).
2. The window is enforced on read: `get()` refuses a record past its retention even while the file is
   still on disk, so a stalled purge cannot extend retention.
3. The bytes are the queue's: `put()` stores the same already-redacted turns `queue.queue_pending`
   writes. Fed from anything else, it needs its own `redact.redact()`.

One JSON file per session under the org's own root, written atomically (tmp + replace).
"""
import json, os, glob, tempfile
from datetime import datetime, timedelta, timezone

from analyzer import store, tenant

ARCHIVE_DIR = "archive"          # under the org's data root (store.org_dir)
_JSON = ".json"


def retention_days(org_id=None):
    """How many days this org keeps a transcript: 0 = none, tenant.KEEP_FOREVER = no expiry. Read from the
    org's tenant_config on every call: apiserver and the ETL are separate processes, and a change
    must not wait for either to restart."""
    return tenant.get_config(store.org_db(org_id)).archive_days


def _keeps(days):
    return days == tenant.KEEP_FOREVER or days > 0


def enabled(org_id=None):
    return _keeps(retention_days(org_id))


def _now():
    return datetime.now(timezone.utc)


def _iso(s):
    """Parse a stored timestamp to an aware UTC datetime; None if absent/unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dir(org_id=None, create=False):
    d = os.path.join(store.org_dir(org_id), ARCHIVE_DIR)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _path(sid, org_id=None, create=False):
    """The file for one session. `sid` is validated because it is a PATH COMPONENT — a '../' here
    would read or delete another org's archive (same guard, same reason, as queue._dir + store)."""
    return os.path.join(_dir(org_id, create), store.valid_session_id(sid) + _JSON)


def put(sess, org_id=None, now=None):
    """Archive one parsed+redacted session. Returns the path written, or None when archiving is off.

    `sess` is the dict `ingest.load_content` produced and `queue.queue_pending` is about to write, so
    its `turns` are already redacted (see the module docstring, property 3). Only the fields the
    transcript view needs are copied — not the whole dict — so a future field added for the extractor
    cannot silently land in long-term storage without someone naming it here."""
    if not enabled(org_id):
        return None
    turns = sess.get("turns") or []
    if not turns:                                     # nothing to show; don't create an empty record
        return None
    # Structured messages are what the transcript view actually renders (ingest._blocks_from_content):
    # which tool ran, with what input, and what came back. `turns` is kept beside them because it is
    # what the EXTRACTOR consumes and what every record written before this existed contains — the
    # reader falls back to it, so an old archive stays readable instead of going blank.
    messages = sess.get("messages") or []
    rec = {"session_id": sess.get("session_id"), "user": sess.get("user"), "project": sess.get("project"),
           "git_branch": sess.get("git_branch"), "started_at": sess.get("started_at"),
           "ended_at": sess.get("ended_at"), "n_user": sess.get("n_user"), "n_asst": sess.get("n_asst"),
           # The mode these bytes were ACTUALLY redacted at — apiserver stamps it before queueing.
           # Shown next to the text so a reader knows what was stripped from what they are reading.
           "redaction_mode": sess.get("redaction_mode"),
           "stored_at": (now or _now()).isoformat(),
           "turns": turns, "messages": messages}
    dst = _path(sess["session_id"], org_id, create=True)
    # A UNIQUE temp name, not `dst + ".tmp"`. apiserver is a ThreadingHTTPServer and a re-POST of a
    # growing session is the documented normal case, so two writers for one session id are ordinary
    # — and a shared temp path lets them interleave into one file that os.replace then publishes as
    # the record. The corrupt result reads as "no transcript" (get swallows the ValueError) and is
    # then deleted by purge as unreadable-therefore-expired: a retained transcript silently gone.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=os.path.basename(dst) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, dst)                          # ponytail: atomic rename, as queue.queue_pending
    except Exception:
        try:
            os.remove(tmp)                            # never leave a half-written orphan behind
        except OSError:
            pass
        raise
    return dst


def expired(rec, days, now=None):
    """True when a record is past a `days` retention window. A record with no readable `stored_at` is
    treated as EXPIRED, not as fresh: an unreadable age must not mean unlimited retention. Nothing
    expires under tenant.KEEP_FOREVER."""
    if days == tenant.KEEP_FOREVER:
        return False
    if days <= 0:
        return True                                   # archiving off -> anything still on disk is stale
    at = _iso(rec.get("stored_at") if isinstance(rec, dict) else None)
    if at is None:
        return True
    return (now or _now()) - at > timedelta(days=days)


def get(sid, org_id=None, now=None, days=None):
    """One archived transcript, or None — missing, unreadable, archiving off, or past retention.
    `days` lets a caller that already resolved the org's window (a listing) skip re-reading it.

    The retention check lives HERE and not only in purge() on purpose (property 2): purge runs in the
    ETL container, and an ETL that is down, wedged or never deployed must not quietly turn a 30-day
    promise into forever."""
    if days is None:
        days = retention_days(org_id)
    if not _keeps(days):
        return None
    try:
        with open(_path(sid, org_id)) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):                     # missing, truncated, or a path-unsafe id
        return None
    if not isinstance(rec, dict) or expired(rec, days, now):
        return None
    return rec


def blocks(rec):
    """A record -> a flat list of display blocks, oldest first, whatever era wrote it.

    THE COMPATIBILITY SEAM. Records written before structured messages existed have only `turns`
    ("USER: ...", "ASSISTANT: ..."), so they are re-shaped into one text block per message rather
    than rendering as nothing. Flat, not nested, because that is what pages: a page boundary can
    fall in the middle of a long assistant message's blocks, and a nested shape would have forced
    the pager to choose between splitting a message and overshooting the page size."""
    if not isinstance(rec, dict):
        return []
    out = []
    msgs = rec.get("messages")
    if isinstance(msgs, list) and msgs:
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            role = m.get("role") or "user"
            for b in (m.get("blocks") or []):
                if isinstance(b, dict) and b.get("type"):
                    out.append({**b, "role": role, "message_index": i,
                                "at": m.get("at") or "", "sidechain": bool(m.get("sidechain")),
                                "meta": bool(m.get("meta"))})
        return out
    for i, t in enumerate(rec.get("turns") or []):     # legacy record: one text block per turn
        raw = str(t)
        role = "assistant" if raw.startswith("ASSISTANT:") else "user"
        text = raw.split(":", 1)[1].lstrip() if ":" in raw[:12] else raw
        out.append({"type": "text", "text": text, "role": role, "message_index": i,
                    "at": "", "sidechain": False, "meta": False})
    return out


def drop(sid, org_id=None):
    """Delete one session's archived transcript. Idempotent, and never raises: it is called from
    `apiserver.forget`, where a missing file (already purged, or never archived) is a normal outcome
    and must not fail the delete of everything else derived from that session."""
    try:
        p = _path(sid, org_id)
    except ValueError:
        return False                                  # a path-unsafe id could never have been written
    gone = False
    try:
        os.remove(p)
        gone = True
    except OSError:
        pass
    # ...and any half-written sibling. A crashed put() leaves `<sid>.json.<rand>.tmp` holding the
    # full redacted turns, which the `*.json` glob in purge() does not match and this delete used to
    # miss — so a session someone asked us to FORGET kept its transcript on disk indefinitely.
    for orphan in glob.glob(p + ".*.tmp"):
        try:
            os.remove(orphan)
            gone = True
        except OSError:
            pass
    return gone


def purge(org_id=None, now=None):
    """Delete every record past the retention window. Returns how many went.

    Runs unconditionally of `enabled()`: turning archiving OFF must clear what is already stored
    (archive_days 0 makes `expired` true for everything), otherwise the disable leaves a permanent
    copy of the last window behind, the opposite of what the operator asked for. Under tenant.KEEP_FOREVER
    only orphaned temp files are swept; no record is opened or aged."""
    days, gone = retention_days(org_id), 0
    # Orphaned temp files first: they hold the same text as a record, are matched by no `*.json`
    # glob, and are written exactly by the failure this module says it tolerates ("a full disk must
    # cost the archive, never the ingest"). One that survives is a transcript kept past its window.
    for orphan in glob.glob(os.path.join(_dir(org_id), "*" + _JSON + ".*.tmp")):
        try:
            if (_now() if now is None else now) - datetime.fromtimestamp(
                    os.path.getmtime(orphan), timezone.utc) > timedelta(hours=1):
                os.remove(orphan)                     # an hour is far longer than any write takes
                gone += 1
        except OSError:
            pass
    if days == tenant.KEEP_FOREVER:
        return gone
    for p in glob.glob(os.path.join(_dir(org_id), "*" + _JSON)):
        try:
            with open(p) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            rec = {}                                  # unreadable -> expired (see `expired`) -> removed
        if expired(rec, days, now):
            try:
                os.remove(p)
                gone += 1
            except OSError:
                pass
    return gone


def stats(org_id=None):
    """(count, bytes) for the operator console — how much this org is holding right now."""
    n = total = 0
    for p in glob.glob(os.path.join(_dir(org_id), "*" + _JSON)):
        try:
            total += os.path.getsize(p)
            n += 1
        except OSError:
            pass
    return n, total


if __name__ == "__main__":
    import tempfile, shutil

    wd = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(wd)                                      # store.org_dir anchors data/ to the CURRENT cwd
    try:
        sess = {"session_id": "s1", "user": "mara@example.com", "project": "csa", "n_user": 1, "n_asst": 1,
                "redaction_mode": "secrets", "turns": ["USER: hi", "ASSISTANT: hello"],
                "transcript": "USER: hi\n\nASSISTANT: hello"}

        def set_days(value, org_id=None):
            store.sqlite_db(store.org_db(org_id)).close()          # the org db (and its dir) must exist
            tenant.set_config(store.org_db(org_id), archive_days=value)

        # --- off by default: nothing is stored, nothing is readable ---------------------------
        assert retention_days() == 0 and not enabled(), "archiving must default to OFF"
        assert put(sess) is None, "put() must store nothing while archiving is off"
        assert not os.path.exists(_dir()), "a disabled archive must not even create its directory"
        assert get("s1") is None

        set_days(30)
        assert retention_days() == 30 and enabled()

        # --- put / get round-trip --------------------------------------------------------------
        p = put(sess)
        assert p and os.path.exists(p)
        got = get("s1")
        assert got["turns"] == sess["turns"] and got["user"] == "mara@example.com"
        assert got["redaction_mode"] == "secrets", "the mode the bytes were redacted at must ride along"
        assert "transcript" not in got, "only named fields are archived — no whole-dict passthrough"

        # A session with no turns writes no record (an empty transcript view is worse than none).
        assert put({"session_id": "empty", "turns": []}) is None and get("empty") is None

        # --- retention is enforced on READ, not only by purge ----------------------------------
        later = _now() + timedelta(days=31)
        assert get("s1", now=later) is None, "past the window, get() must refuse even before a purge runs"
        assert os.path.exists(p), "...and it must NOT delete on read — purge owns deletion"
        assert get("s1", now=_now() + timedelta(days=29)) is not None, "inside the window it still reads"

        # A record whose age cannot be read is expired, never fresh.
        assert expired({"stored_at": "not-a-date"}, 30) and expired({}, 30), "unreadable age must expire"

        # --- purge ------------------------------------------------------------------------------
        assert purge() == 0, "nothing is expired yet"
        assert purge(now=later) == 1 and not os.path.exists(p), "purge removes past-window records"

        # Turning archiving OFF clears what is already stored — a disable is not a freeze.
        put(sess)
        set_days(0)
        assert purge() == 1, "retention 0 must purge the whole archive, not skip it"
        set_days(30)

        # --- deletes (the /api/forget path) -----------------------------------------------------
        put(sess)
        assert drop("s1") is True and get("s1") is None
        assert drop("s1") is False, "drop is idempotent and never raises on a missing file"
        assert drop("../../etc/passwd") is False, "a path-unsafe id is refused, not coerced to a basename"

        # --- org isolation: the FILE is the boundary, and the window is the org's own ------------
        set_days(30, "acme")
        set_days("forever", "other")
        put(sess, org_id="acme")
        put({**sess, "user": "someone@else.com"}, org_id="other")
        assert get("s1", org_id="acme")["user"] == "mara@example.com"
        assert get("s1", org_id="other")["user"] == "someone@else.com", "same id, different org, different file"
        assert get("s1") is None, "and neither leaks into the default org"
        drop("s1", org_id="acme")
        assert get("s1", org_id="acme") is None and get("s1", org_id="other") is not None, \
            "a delete in one org must not touch another's copy of the same session id"

        # --- a junk value is fail-closed, and each org keeps its own -------------------------------
        set_days("banana")
        assert retention_days() == 0, "unparseable retention must read as OFF, never as forever"
        set_days(-5)
        assert retention_days() == 0
        set_days(999999)
        assert retention_days() == tenant.MAX_ARCHIVE_DAYS, "an absurd retention is clamped, not honoured"
        assert retention_days("acme") == 30 and retention_days("other") == tenant.KEEP_FOREVER
        set_days(30)

        # --- structured messages, and the legacy fallback ----------------------------------------
        rich = {"session_id": "s-rich", "user": "n@x", "turns": ["USER: hi", "ASSISTANT: ok"],
                "messages": [
                    {"role": "user", "at": "t1", "blocks": [{"type": "text", "text": "run the tests"}]},
                    {"role": "assistant", "at": "t2", "sidechain": True, "blocks": [
                        {"type": "thinking", "text": "need to run pytest"},
                        {"type": "tool_use", "name": "Bash", "tool_id": "tu1", "input": "{\"command\":\"pytest\"}"},
                        {"type": "tool_result", "tool_id": "tu1", "text": "277 passed", "is_error": False},
                        {"type": "text", "text": "all green"}]}]}
        put(rich)
        bs = blocks(get("s-rich"))
        assert [b["type"] for b in bs] == ["text", "thinking", "tool_use", "tool_result", "text"], bs
        assert bs[0]["role"] == "user" and bs[1]["role"] == "assistant"
        assert bs[2]["name"] == "Bash" and bs[3]["text"] == "277 passed"
        assert bs[1]["sidechain"] is True and bs[0]["sidechain"] is False, "sidechain rides per message"
        assert {b["message_index"] for b in bs} == {0, 1}, "blocks carry which message they came from"

        # A record written before structured messages existed must still read, not render blank.
        legacy = {"session_id": "s-old", "user": "n@x", "turns": ["USER: hello", "ASSISTANT: hi there"]}
        put(legacy)
        lb = blocks(get("s-old"))
        assert [b["type"] for b in lb] == ["text", "text"]
        assert [b["role"] for b in lb] == ["user", "assistant"], "the role is recovered from the prefix"
        assert lb[1]["text"] == "hi there", "the 'ASSISTANT: ' prefix is stripped, not shown"
        assert blocks(None) == [] and blocks({}) == []
        drop("s-rich"); drop("s-old")

        n, size = stats(org_id="other")
        assert n == 1 and size > 0

        # --- half-written orphans are real transcripts, and must not outlive the record ----------
        put(sess)
        orphan = _path("s1") + ".abc123.tmp"
        with open(orphan, "w") as fh:
            json.dump({"turns": ["USER: a secret half-write"]}, fh)
        drop("s1")
        assert not os.path.exists(orphan), "forget must take the half-written sibling too"
        with open(orphan, "w") as fh:
            json.dump({"turns": ["USER: another"]}, fh)
        old_t = (_now() - timedelta(hours=2)).timestamp()
        os.utime(orphan, (old_t, old_t))
        assert purge() >= 1 and not os.path.exists(orphan), "purge must sweep stale temp files"
        # ...but not one that a put() is writing RIGHT NOW
        with open(orphan, "w") as fh:
            json.dump({"turns": ["USER: in flight"]}, fh)
        purge()
        assert os.path.exists(orphan), "a fresh temp file may be an in-flight write; leave it"
        os.remove(orphan)

        print("OK archive: off by default; put/get round-trip; retention enforced on READ (not only purge); "
              "unreadable age expires; disable purges; drop idempotent + path-safe; per-org files isolate; "
              "half-written temp files are dropped and purged (they hold the same text); "
              "structured blocks round-trip and a legacy turns-only record still reads")
    finally:
        os.chdir(cwd)
        shutil.rmtree(wd, ignore_errors=True)
