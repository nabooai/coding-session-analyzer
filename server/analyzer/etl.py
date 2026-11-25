"""Live-ETL watcher (client side): stdlib filesystem poller, NO Redis.

Polls ~/.claude/projects/**/*.jsonl mtimes every few seconds. The moment a session is NEW or has GROWN
(mtime advanced) *and has gone quiet*, it POSTs {"session_id": ...} to the server for live indexing — the
server owns the expensive `claude -p` extraction.

    uv run python -m analyzer.etl watch          # run forever
    uv run python -m analyzer.etl watch --once    # one scan (also: `csa watch [--once]`)
    uv run python -m analyzer.etl selfcheck       # offline change-detection assertions

Debounce: Claude Code appends live, so an active session's mtime moves constantly and each send costs a
server-side extraction. We treat a file as settled only once it's been quiet (mtime older than QUIET secs),
and we never send the same (session, mtime) twice (per-session last-sent mtime dict).
"""
import os, sys, json, time, glob, logging, urllib.request
from analyzer import ingest

PORT = int(os.environ.get("CSA_API_PORT", "8766"))
QUIET = 10                # ponytail: 10s settle window; raise if extraction storms
INTERVAL = 2.5
log = logging.getLogger("csa.etl")


def _entries():
    """[(session_id, path, mtime)] for every session .jsonl under ROOT, skipping ones rotated mid-scan."""
    out = []
    for f in glob.glob(os.path.join(ingest.ROOT, "**", "*.jsonl"), recursive=True):
        try:
            out.append((os.path.splitext(os.path.basename(f))[0], f, os.path.getmtime(f)))
        except OSError:                                    # removed/rotated between glob and stat -> skip
            continue
    return out


def _due(entries, sent, now, quiet=QUIET):
    """Pure change-detect: sessions that are SETTLED (quiet >= `quiet`) and NEW or GROWN vs last send."""
    return [(sid, path, m) for sid, path, m in entries
            if now - m >= quiet and sent.get(sid) != m]


def _send(sid):
    """POST the session id to the server; log its {ok, hidden_context, entities} reply. Never raises."""
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/ingest",
                                 data=json.dumps({"session_id": sid}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:   # ponytail: server runs `claude -p`; generous timeout
            resp = json.load(r)
        log.info("sent %-12s ok=%s hidden_context=%s entities=%s", sid[:12],
                 resp.get("ok"), resp.get("hidden_context"), resp.get("entities"))
        return True
    except Exception as e:                                 # server down / extraction error -> retry next scan
        log.warning("send %-12s FAILED: %s (retry next scan)", sid[:12], str(e)[:120])
        return False


def scan_once(sent, now=None):
    """One pass: send every due session, recording (sid -> mtime) only on success so failures retry."""
    now = time.time() if now is None else now
    due = _due(_entries(), sent, now)
    for sid, _path, m in due:
        if _send(sid):
            sent[sid] = m
    return due


def watch(once=False):
    # Seed current mtimes so startup does NOT re-index the whole backlog — fire only sessions that appear or
    # GROW after the watcher starts (that's "live"). Catching up un-indexed history is `csa build`'s job.
    sent = {sid: m for sid, _p, m in _entries()}          # session_id -> last-sent mtime (dedup + change-detect)
    log.info("watch: seeded %d sessions; polling %s every %.1fs (quiet=%ds, port=%d) — new/changed only",
             len(sent), ingest.ROOT, INTERVAL, QUIET, PORT)
    while True:
        scan_once(sent)
        if once:
            return sent
        time.sleep(INTERVAL)


def _selfcheck():
    now, sent = 1000.0, {}
    entries = [("new", "/x/new.jsonl", now - 20),          # quiet 20s -> due
               ("hot", "/x/hot.jsonl", now - 1)]           # written 1s ago -> debounced (still being written)
    assert [d[0] for d in _due(entries, sent, now)] == ["new"], "quiet file due; active file debounced"
    sent["new"] = now - 20
    assert _due(entries, sent, now) == [], "same (session, mtime) must not resend"
    grew = [("new", "/x/new.jsonl", now - 15)]             # mtime advanced -> jsonl grew -> due again
    assert [d[0] for d in _due(grew, sent, now)] == ["new"], "a grown file must resend"
    print("OK etl: quiet-debounce + (session,mtime) dedup + change-detect")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if cmd == "selfcheck":
        _selfcheck()
    elif cmd == "watch":
        watch(once="--once" in sys.argv)
    else:
        sys.exit("usage: python -m analyzer.etl watch [--once] | selfcheck")
