"""The CSA plugin's client for Claude Code. Stdlib only, Python 3.9 or newer, this file plus csa_commands.py and redact.py.

Hooks run `csa.py recall|ship|sweep`; the skills run the commands in csa_commands.py. The only network
call is _request(), and its host comes only from ~/.csa/config.json. Nothing here updates itself or downloads
anything. Hook stdout is the hook protocol: recall prints one JSON object or nothing, ship and sweep
print nothing.
"""
import json
import os
import re
import sys
import time

VERSION = "0.2.0"
RELEASES = "https://github.com/nabooai/coding-session-analyzer/releases"
REQUIRED_POLICY = 2
MAX_BODY = 32 * 1024 * 1024
SWEEP_CHUNK = 10
CLAIM_TTL = 300
SETTLE_SECONDS = 2       # Claude Code still appends to a transcript just after SessionEnd, as on /clear
AUTOMATIC, MANUAL = "plugin", "plugin-manual"
MODES = ("secrets", "privacy", "strict")
HERE = os.path.dirname(os.path.abspath(__file__))
SID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\Z", re.A)

CONTENT_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-./]{3,}")
CONTENT_STOP = frozenset("""about what when where which would could should there their that this these those
with from into your will does done have been here then they them please gonna wanna just like make made need
want show give tell using used also some thing stuff really actually maybe yourself yourslef""".split())
QUERY_REWRITES = [re.compile(p, re.I | re.A) for p in (
    r"""^\s*use\s+csa\s+to\s+answer:?\s*["']?(.+?)["']?\s*$""",
    r"^\s*what\s+did\s+we\s+decide\s+about\s+(.+?)\??\s*$",
    r"^\s*what\s+was\s+decided\s+about\s+(.+?)\??\s*$",
    r"^\s*what\s+do\s+we\s+know\s+about\s+(.+?)\??\s*$",
    r"^\s*has\s+any(?:one|body)\s+hit\s+(.+?)\s+before\??\s*$",
    r"^\s*did\s+any(?:one|body)\s+(?:hit|see|work\s+on|touch)\s+(.+?)\s+before\??\s*$",
    r"^\s*why\s+did\s+(.+?)\s+decide(?:d)?\s+about\s+(.+?)\??\s*$",
    r"^\s*why\s+(.+?)\s+decided?\s+about\s+(.+?)\??\s*$")]
QUERY_TRIM = " \t\r\n\"'`\u201c\u201d\u2018\u2019?.!"
RESIDUAL_SECRET = [re.compile(p, re.A) for p in (
    r"(?i)\bbearer\s+[a-z0-9._~+/\-]{16,}",
    r"://[^/\s:@]+:[^/\s@]{6,}@",
    r"(?i)(passw(or)?d|pwd|secret|api[_-]?key|access[_-]?key|token|credential)s?\s*(is|was|=|:)\s*\S{8,}",
    r"\b[A-Za-z0-9+/]{40}\b")]


# --- paths and small files --------------------------------------------------------------------------

def csa_path(*parts):
    return os.path.join(os.path.expanduser("~"), ".csa", *parts)


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def log(msg):
    sys.stderr.write(time.strftime("%Y/%m/%d %H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def now_rfc3339():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def write_atomic(path, text):
    """0600 from birth, replaced whole, so a closed laptop cannot leave half a file. Never makes the
    directory: a detached child must not bring ~/.csa back after a disconnect."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def make_csa_dir():
    os.makedirs(csa_path(), mode=0o700, exist_ok=True)


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def load_config():
    try:
        with open(csa_path("config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def save_config(cfg):
    write_atomic(csa_path("config.json"), json.dumps(cfg, indent=2) + "\n")


def update_config(**fields):
    """Read, change, write, so a sweep cannot put back a config deleted or replaced since it started."""
    cfg = load_config()
    if cfg is None:
        return
    cfg.update(fields)
    save_config(cfg)


def load_list(path):
    return [s.strip() for s in (read_text(path) or "").split("\n") if s.strip() and not s.strip().startswith("#")]


def due(path):
    """At most hourly: a stamp inside the last hour says no, anything else stamps it and says yes."""
    text = read_text(path)
    if text and time.time() - os.path.getmtime(path) < 3600:
        return False
    try:
        write_atomic(path, now_rfc3339())
    except OSError:
        return False
    return True


# --- gates ------------------------------------------------------------------------------------------

def consent_ok(cfg):
    consent = cfg.get("consent")
    return isinstance(consent, dict) and isinstance(consent.get("policy_version"), int) \
        and consent["policy_version"] >= REQUIRED_POLICY


def days_agreed(cfg):
    consent = cfg.get("consent")
    return (consent.get("archive_days_at_consent") or 0) if isinstance(consent, dict) else 0


def retention_exceeds_consent(cfg):
    server, agreed = cfg.get("server_archive_days") or 0, days_agreed(cfg)
    return agreed != -1 and (server == -1 or server > agreed)


def retention_words(days):
    if days == -1:
        return "until its owner or a workspace admin deletes it"
    return "for %d days" % days if days > 0 else "none"


def server_of(cfg):
    server = str(cfg.get("server") or "").rstrip("/")
    return server if re.match(r"https?://[^/@\s]+", server) else ""


def hook_binary(command, has_args):
    s = (command or "").strip()
    if has_args or not s:
        return s
    if s[0] == '"':
        return s[1:].split('"', 1)[0]
    return s.split(None, 1)[0]


def is_csa_client_hook(hook):
    args = hook.get("args")
    binary = hook_binary(hook.get("command") if isinstance(hook.get("command"), str) else "",
                         isinstance(args, list) and len(args) > 0)
    return binary in ("csa-client", "csa-client.exe") or binary.endswith(("/csa-client", "/csa-client.exe", "\\csa-client.exe"))


def conflict():
    """Where csa-client is wired on this machine, or "". The plugin stands down on any hit."""
    home = os.path.expanduser("~")
    for settings in {os.path.join(home, ".claude", "settings.json"), os.path.join(claude_dir(), "settings.json")}:
        try:
            hooks = json.loads(read_text(settings) or "{}").get("hooks") or {}
            for entries in hooks.values():
                for entry in entries:
                    if any(is_csa_client_hook(h) for h in entry.get("hooks") or []):
                        return "csa-client hooks in " + settings
        except (ValueError, AttributeError, TypeError):
            continue
    unit = os.path.join(home, ".config", "systemd", "user", "csa-client.service")
    if os.path.exists(unit):
        return "the csa-client watcher " + unit
    agents = os.path.join(home, "Library", "LaunchAgents")
    if os.path.isdir(agents):
        for name in os.listdir(agents):
            if "csa-client" in name and name.endswith(".plist"):
                return "the csa-client watcher " + os.path.join(agents, name)
    return ""


def conflict_notice(where):
    return ("CSA: this plugin is paused because csa-client is also installed (%s); csa-client keeps shipping. "
            "To switch, run `csa-client uninstall` (not --purge, which deletes your folder rules), then "
            "/csa:connect. To stay, run `claude plugin disable csa@csa`." % where)


def ready_to_ship(cfg):
    return bool(cfg and cfg.get("shipping") == AUTOMATIC and consent_ok(cfg) and server_of(cfg) and not conflict())


# --- what may be sent -------------------------------------------------------------------------------

def should_recall(prompt):
    p = prompt.strip().lower()
    if not 20 <= len(p.encode("utf-8", "replace")) <= 4000 or p.startswith(("/", "<")):
        return False
    return sum(1 for tok in CONTENT_TOKEN.findall(p) if tok not in CONTENT_STOP) >= 2


def ambient_query(prompt):
    q = prompt.strip()
    for _ in range(2):
        for pat in QUERY_REWRITES:
            m = pat.search(q)
            if m:
                q = " ".join(g for g in m.groups()).strip().strip(QUERY_TRIM)
                break
        else:
            break
    q = q.strip(QUERY_TRIM)
    return q or prompt.strip()


def looks_secret(s):
    return any(pat.search(s) for pat in RESIDUAL_SECRET)


def _glob_escape(p, i):
    """One class member at p[i] -> (char, next index), or (None, i) on a malformed pattern."""
    if i >= len(p) or p[i] in "-]":
        return None, i
    if p[i] == "\\":
        i += 1
        if i >= len(p):
            return None, i
    return p[i], i + 1


def glob_regex(p):
    """Go filepath.Match as a regex, or None for a malformed pattern, which matches nothing: * and ?
    never cross `/`, classes negate with ^. fnmatch would match *client* across /Users/client/."""
    out, i = [], 0
    while i < len(p):
        c = p[i]
        i += 1
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "\\":
            if i >= len(p):
                return None
            out.append(re.escape(p[i]))
            i += 1
        elif c == "[":
            negated = i < len(p) and p[i] == "^"
            i += 1 if negated else 0
            ranges, count = [], 0
            while i >= len(p) or p[i] != "]" or count == 0:
                lo, i = _glob_escape(p, i)
                if lo is None:
                    return None
                hi = lo
                if i < len(p) and p[i] == "-":
                    hi, i = _glob_escape(p, i + 1)
                    if hi is None:
                        return None
                if lo <= hi:
                    ranges.append(re.escape(lo) + "-" + re.escape(hi))
                count += 1
            i += 1
            out.append(("[%s%s]" % ("^" if negated else "", "".join(ranges))) if ranges
                       else ("." if negated else "(?!)"))
        else:
            out.append(re.escape(c))
    return re.compile("".join(out), re.S)


def ignored_by(path, patterns):
    """The first pattern matching the project folder, the file name or the full path, or ""."""
    names = (os.path.basename(os.path.dirname(path)), os.path.basename(path), path)
    for pattern in patterns:
        rx = glob_regex(pattern)
        if rx and any(rx.fullmatch(n) for n in names):
            return pattern
    return ""


def excluded_reason(path):
    ignores, only = load_list(csa_path("ignore")), load_list(csa_path("only"))
    pattern = ignored_by(path, ignores)
    if pattern:
        return "matches ignore '%s'" % pattern
    if only and not ignored_by(path, only):
        return "not in the ~/.csa/only allowlist"
    return ""


def started_before_floor(cfg, path):
    """Whether this transcript began before the consent's floor. It may have grown since, but what it
    held before the person agreed was never theirs to have agreed to, so no hook ships it."""
    floor = parse_floor(cfg.get("sweep_from"))
    if floor is None:
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    stamp = json.loads(line).get("timestamp")
                except (ValueError, AttributeError):
                    continue
                began = parse_floor(stamp) if isinstance(stamp, str) else None
                if began is not None:
                    return began < floor
    except OSError:
        pass
    return False


def redaction_of(cfg):
    """(mode, deny, allow): the mode this machine chose, and the deny and allow lists, read per use."""
    r = (cfg or {}).get("redaction")
    mode = r.get("mode") if isinstance(r, dict) else None
    return (mode if mode in MODES else "secrets", load_list(csa_path("redact.deny")), load_list(csa_path("redact.allow")))


def redactor():
    sys.path.append(HERE)
    import redact
    return redact


def redacted_transcript(cfg, path):
    """(content, labels, mode): the exact string a POST carries."""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    mode, deny, allow = redaction_of(cfg)
    content, labels = redactor().redact(text, mode, deny, allow)
    return content, labels, mode


def ledger_rows():
    """The rows of ~/.csa/sent.jsonl, skipping a line a crash left half written."""
    rows = []
    for line in (read_text(csa_path("sent.jsonl")) or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def sha256(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- the network ------------------------------------------------------------------------------------

def _request(server, method, path, body=None, token=None, timeout=20):
    """The one network call -> (status, parsed JSON or None, failure). failure is "" on any HTTP answer,
    else "tls: ..." or "transport: ...". A redirect is an answer, never followed: urllib would carry the
    bearer token to whatever host it names."""
    import ssl
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    cafile = None
    if not os.environ.get("SSL_CERT_FILE") and ssl.get_default_verify_paths().cafile is None \
            and os.path.exists("/etc/ssl/cert.pem"):
        cafile = "/etc/ssl/cert.pem"
    urllib.request.install_opener(urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile))))
    headers = {"User-Agent": "csa-plugin/" + VERSION}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(server + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:
        reason = getattr(e, "reason", e)
        kind = "tls" if isinstance(reason, ssl.SSLCertVerificationError) else "transport"
        return 0, None, "%s: %s" % (kind, reason)
    try:
        return status, json.loads(raw), ""
    except ValueError:
        return status, None, ""


def tls_advice(failure):
    return ("the server's TLS certificate is not trusted (%s). If a company CA signs it, set SSL_CERT_FILE to a "
            "PEM file holding that CA in the environment Claude Code starts from: Python does not read the "
            "macOS Keychain." % failure[5:])


def note_auth(status):
    """The 401 and TLS markers are shared with csa-client, for the next prompt to surface."""
    if status == 401:
        if not os.path.exists(csa_path("auth_broken")) and os.path.isdir(csa_path()):
            write_atomic(csa_path("auth_broken"), "")
    elif 200 <= status < 300:
        remove_quietly(csa_path("auth_broken"))
        remove_quietly(csa_path("tls_broken"))


def note_failure(failure):
    if failure.startswith("tls") and os.path.isdir(csa_path()):
        write_atomic(csa_path("tls_broken"), tls_advice(failure))


def send(cfg, sid, path):
    """Redact and POST one transcript -> sent | rejected | too_large | unauthorized | down. Only the last
    two say the server cannot take anything right now."""
    server = server_of(cfg)
    content, labels, mode = redacted_transcript(cfg, path)
    body = json.dumps({"session_id": sid, "content": content, "redaction_mode": mode}, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY:
        return "too_large"
    status, reply, failure = _request(server, "POST", "/api/ingest", body, cfg.get("auth_token"), timeout=180)
    if failure:
        note_failure(failure)
        log("send %s FAILED: %s" % (sid, failure))
        return "down"
    note_auth(status)
    if status == 401:
        log("send %s FAILED: HTTP 401. This machine's credential is no longer accepted. Run /csa:connect. "
            "Nothing is lost: sessions wait here and ship once you do." % sid)
        return "unauthorized"
    if status == 413:
        return "too_large"
    if not 200 <= status < 300 or not isinstance(reply, dict):
        log("send %s FAILED: HTTP %d (will retry)" % (sid, status))
        return "down"
    ok = bool(reply.get("ok"))
    if os.path.isdir(csa_path()):
        fd = os.open(csa_path("sent.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": now_rfc3339(), "sid": sid, "server": server, "mode": mode, "bytes": len(content.encode("utf-8")),
                                "labels": labels, "sha256": sha256(content), "ok": ok}) + "\n")
    if ok:
        log("sent %s ok=true" % sid)
        return "sent"
    log('sent %s ok=false error="%s" (server rejected; not retrying until the file changes)' % (sid, reply.get("error")))
    return "rejected"


def claim_ship(sid):
    """The same O_EXCL claim csa-client takes, so two clients or two hooks cannot both POST one session."""
    path = csa_path("shipped", sid + ".inflight")

    def take():
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except OSError:
            return False

    if take():
        return path
    try:
        if time.time() - os.path.getmtime(path) > CLAIM_TTL:
            os.rename(path, path + ".stale")          # atomic: one stealer wins, O_EXCL settles the rest
            remove_quietly(path + ".stale")
    except OSError:
        pass
    return path if take() else None


def ship_one(cfg, sid, path, explicit=False):
    """-> skipped | excluded | forgotten | claimed | sent | rejected | too_large | unauthorized | down.
    `explicit` is a person sending it by name, the one thing that may send a forgotten session again."""
    if not SID.match(sid):
        return "skipped"
    forgotten = load_list(csa_path("forgotten"))
    if sid in forgotten and not explicit:
        return "forgotten"
    reason = excluded_reason(path)
    if reason:
        log("ship: skipping %s (%s)" % (sid, reason))
        return "excluded"
    try:
        size = os.path.getsize(path)
    except OSError:
        return "skipped"
    marker = csa_path("shipped", sid)

    def shipped():
        try:
            return sid not in forgotten and size <= int((read_text(marker) or "").strip())
        except ValueError:
            return False

    if not os.path.isdir(csa_path()) or shipped():
        return "skipped"
    if not os.path.isdir(csa_path("shipped")):
        os.mkdir(csa_path("shipped"), 0o700)
    claim = claim_ship(sid)
    if not claim:
        return "claimed"
    try:
        if shipped():
            return "skipped"
        outcome = "too_large" if size > MAX_BODY else send(cfg, sid, path)
        if outcome == "too_large":
            with open(csa_path("oversize"), "a", encoding="utf-8") as f:
                f.write("%s %s %d\n" % (now_rfc3339(), sid, size))
            log("ship: %s is too large to send (%d bytes). It is skipped until it grows; see ~/.csa/oversize." % (sid, size))
        if outcome in ("sent", "rejected", "too_large") and os.path.isdir(csa_path("shipped")):
            write_atomic(marker, str(size))
        if outcome == "sent" and sid in forgotten:
            write_atomic(csa_path("forgotten"), "".join(s + "\n" for s in forgotten if s != sid))
        return outcome
    finally:
        remove_quietly(claim)


# --- hooks ------------------------------------------------------------------------------------------

def read_event():
    """The hook's JSON. Stops at the closing brace: Claude Code may hold the pipe open, so reading to EOF
    can hang. Empty after a second, or on a terminal."""
    import select
    if sys.stdin.isatty():
        return {}
    buf, deadline = b"", time.monotonic() + 1.0
    while True:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([0], [], [], left)[0]:
            return {}
        chunk = os.read(0, 1 << 20)
        buf += chunk
        if buf.rstrip().endswith(b"}") or not chunk:
            try:
                event = json.loads(buf.decode("utf-8", "replace"))
                return event if isinstance(event, dict) else {}
            except ValueError:
                if not chunk:
                    return {}


def emit(system_message=None, context=None):
    out = {}
    if context:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    if system_message:
        out["systemMessage"] = system_message
    print(json.dumps(out))


def hook_recall():
    cfg = load_config()
    if cfg is None:
        return
    where = conflict()
    if where:
        if due(csa_path("warned_conflict")):
            emit(conflict_notice(where))
        return
    if cfg.get("shipping") not in (AUTOMATIC, MANUAL):
        if due(csa_path("warned_parked")):
            emit("CSA: the plugin is enabled but not connected, so nothing is sent. Run /csa:connect to connect it.")
        return
    notice = read_text(csa_path("connected_notice"))
    if notice:
        remove_quietly(csa_path("connected_notice"))
        emit(notice)
        return
    if not consent_ok(cfg):
        if due(csa_path("warned_consent")):
            emit("CSA stopped shipping. Your consent here covers an older policy; this plugin needs policy %d. "
                 "Nothing was sent. Run /csa:connect to read what changed and agree again, or /csa:disconnect to stop."
                 % REQUIRED_POLICY)
        return
    if cfg["shipping"] == MANUAL or not server_of(cfg):
        return
    if os.path.exists(csa_path("auth_broken")) and due(csa_path("auth_broken")):
        emit("CSA: this machine's credential is no longer accepted. Run /csa:connect to sign in again. "
             "Nothing is lost: sessions wait here and ship once you do.")
        return
    advice = read_text(csa_path("tls_broken"))
    if advice and due(csa_path("warned_tls")):
        emit("CSA: the last ship failed. " + advice)
        return
    if retention_exceeds_consent(cfg) and due(csa_path("warned_retention")):
        emit("CSA paused: this server keeps transcripts %s and you agreed to %s. No session is being sent; recall "
             "still works. Run /csa:connect to see the new terms. Shipping resumes on its own if the server lowers it again."
             % (retention_words(cfg.get("server_archive_days") or 0), retention_words(days_agreed(cfg))))
        return
    event = read_event()
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not should_recall(prompt):
        return
    if isinstance(event.get("transcript_path"), str) and excluded_reason(event["transcript_path"]):
        return
    mode, deny, allow = redaction_of(cfg)
    query = redactor().redact(ambient_query(prompt), mode, deny, allow)[0]
    if looks_secret(query):
        return
    import signal
    signal.signal(signal.SIGALRM, signal.SIG_DFL)     # urllib's timeout skips DNS; this budget is total
    signal.setitimer(signal.ITIMER_REAL, 1.8)
    status, reply, failure = _request(server_of(cfg), "POST", "/api/ask", {
        "query": query, "session": event.get("session_id"), "asked_by": cfg.get("identity"),
        "cwd": event.get("cwd"), "k": 6}, cfg.get("auth_token"), timeout=1.8)
    signal.setitimer(signal.ITIMER_REAL, 0)
    if failure:
        return
    note_auth(status)
    if isinstance(reply, dict) and 200 <= status < 300 and reply.get("ok") and reply.get("found") and reply.get("context"):
        emit(reply.get("system_message"),
             "CSA ambient team memory found relevant past-session context for this prompt.\n"
             "Use it as background evidence when it helps. Preserve the provenance in the CSA lines; "
             "do not claim a person personally made a decision unless the evidence says that.\n\n" + reply["context"])


def detach(*args):
    """Hand the work to a child in its own session, holding neither of Claude Code's pipes."""
    logfd = os.open(csa_path("ship.log"), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    if os.fork():
        return
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 0)
        os.dup2(null, 1)
        os.dup2(logfd, 2)
        os.execv(sys.executable, [sys.executable, "-I", "-S", "-B", os.path.abspath(__file__)] + list(args))
    finally:
        os._exit(0)


def retention_paused(cfg):
    if not retention_exceeds_consent(cfg):
        return False
    log("ship: paused. This server keeps transcripts %s and you agreed to %s. No session is being sent and nothing "
        "is lost. Run /csa:connect to see the new terms. Shipping resumes on its own if the server lowers it again."
        % (retention_words(cfg.get("server_archive_days") or 0), retention_words(days_agreed(cfg))))
    return True


def hook_ship():
    cfg = load_config()
    if not ready_to_ship(cfg):
        return
    event = read_event()
    sid, path = event.get("session_id"), event.get("transcript_path")
    if isinstance(sid, str) and isinstance(path, str) and SID.match(sid) and path.endswith(".jsonl"):
        detach("_ship", sid, path)


def child_ship(sid, path):
    time.sleep(SETTLE_SECONDS)
    cfg = load_config()
    if ready_to_ship(cfg) and not retention_paused(cfg):
        if started_before_floor(cfg, path):
            log("ship: skipping %s: it began before you connected, so it stays here. /csa:send-session sends it." % sid)
        else:
            ship_one(cfg, sid, path)


def hook_sweep():
    if ready_to_ship(load_config()):
        event = read_event()
        detach("_sweep", str(event.get("session_id") or ""))


def transcripts():
    """(mtime, sid, path) of each top-level transcript, oldest first. Subagent files sit a level deeper
    and are left out on purpose: they are half the files and billed per extraction."""
    root, out = os.path.join(claude_dir(), "projects"), []
    try:
        projects = [e.path for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return out
    for project in projects:
        try:
            for e in os.scandir(project):
                if e.is_file() and e.name.endswith(".jsonl"):
                    out.append((e.stat().st_mtime, e.name[:-6], e.path))
        except OSError:
            continue
    return sorted(out)


def parse_floor(stamp):
    """An RFC 3339 instant as epoch seconds, or None when unreadable, which must never read as "ship everything"."""
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(re.sub(r"\.\d+", "", str(stamp)).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def child_sweep(current):
    time.sleep(SETTLE_SECONDS)
    cfg = load_config()
    if not ready_to_ship(cfg):
        return
    server = server_of(cfg)
    status, reply, failure = _request(server, "GET", "/api/client-config", token=cfg.get("auth_token"), timeout=8)
    if failure:
        note_failure(failure)
    note_auth(status)
    if isinstance(reply, dict) and 200 <= status < 300:
        days = reply.get("archive_days")
        if isinstance(days, int) and days != (cfg.get("server_archive_days") or 0):
            update_config(server_archive_days=days)
            cfg["server_archive_days"] = days
        for d in reply.get("directives") or []:
            log("fleet: an administrator asked for your session history. The plugin ships no history, so nothing "
                "was sent and they have been told.")
            _request(server, "POST", "/api/client-config/ack",
                     {"id": d.get("id"), "status": "declined", "shipped": 0,
                      "detail": "history sharing is off on this machine"}, cfg.get("auth_token"), timeout=8)
    if retention_paused(cfg):
        return
    floor = parse_floor(cfg.get("sweep_from")) if cfg.get("sweep_from") else None
    if floor is None:
        log("ship: unreadable sweep_from, sweeping nothing. Run /csa:connect.")
        return
    shipped = 0
    for mtime, sid, path in transcripts():
        if sid == current or mtime < floor or started_before_floor(cfg, path):
            continue
        outcome = ship_one(cfg, sid, path)
        shipped += outcome == "sent"
        if outcome in ("down", "unauthorized") or shipped >= SWEEP_CHUNK:
            break
    if shipped:
        log("ship: swept %d catch-up session(s)" % shipped)


def cmd_redact(args):
    """stdin to stdout at this machine's mode and lists, or --mode. For the scripted recipe in the docs."""
    mode, deny, allow = redaction_of(load_config())
    if "--mode" in args and args.index("--mode") + 1 < len(args):
        mode = args[args.index("--mode") + 1]
    text, labels = redactor().redact(sys.stdin.buffer.read().decode("utf-8", "replace"), mode, deny, allow)
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stderr.write("redacted: %s\n" % (", ".join(labels) or "nothing"))
    return 0


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "recall":
        hook_recall()
    elif cmd == "ship":
        hook_ship()
    elif cmd == "sweep":
        hook_sweep()
    elif cmd == "_ship" and len(argv) == 3:
        child_ship(argv[1], argv[2])
    elif cmd == "_sweep" and len(argv) == 2:
        child_sweep(argv[1])
    elif cmd == "redact":
        return cmd_redact(argv[1:])
    else:
        sys.path.append(HERE)
        import csa_commands
        return csa_commands.main(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
