"""What a person runs through the skills: connect, disconnect, preview, send-session and the rest.
csa.py imports this only for those commands, so a hook never loads it. Network calls go through
csa._request(), the only one there is.
"""
import json
import os
import sys
import time

from csa import (AUTOMATIC, MANUAL, MODES, RELEASES, REQUIRED_POLICY, VERSION, _request, claude_dir, conflict, conflict_notice,
                 consent_ok, csa_path, days_agreed, detach, glob_regex, ledger_rows, load_config, load_list, make_csa_dir, note_auth,
                 now_rfc3339, parse_floor, read_text, redacted_transcript, redaction_of, remove_quietly,
                 retention_exceeds_consent, retention_words, save_config, server_of, sha256, ship_one, SID, tls_advice,
                 transcripts, update_config, write_atomic)

POLL_SECONDS = 15 * 60


# --- connect and disconnect -------------------------------------------------------------------------

def run_claude(*args):
    import subprocess
    try:
        return subprocess.run(["claude"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_browser(url):
    import subprocess
    try:
        return subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def on_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_connect(args):
    from urllib.parse import quote, urlsplit
    manual = "--manual" in args
    urls = [a for a in args if not a.startswith("--")]
    prev = load_config() or {}
    server = (urls[0] if urls else server_of(prev)).strip().rstrip("/")
    try:
        parts = urlsplit(server)
        host_ok = parts.scheme in ("http", "https") and parts.hostname and not parts.username
    except ValueError:
        host_ok = False
    if len(urls) > 1 or not host_ok:
        print("Usage: /csa:connect <server-url> [--manual], with a URL like https://csa.example.com")
        return 2
    if sys.version_info < (3, 9):  # noqa: UP036
        print("This needs Python 3.9 or newer, and python3 here is %d.%d. Nothing was connected." % sys.version_info[:2])
        return 1
    where = conflict()
    if where:
        print(conflict_notice(where))
        return 1
    if prev.get("cf_access_client_id"):
        print("This machine reaches its server through Cloudflare Access credentials, which the plugin does not "
              "send. Keep using csa-client here.")
        return 1
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: %s is plain http, so your credential and transcripts cross the network unencrypted." % server)
    if manual:
        print("Connecting lets you send one session at a time to %s with /csa:send-session. Nothing else leaves "
              "this computer: no recall, no automatic shipping. Secrets are stripped here first; code, paths and "
              "names are kept. Nothing is sent until you type the code and click Authorize in your browser." % server)
    else:
        print("Connecting sends this computer's finished Claude Code sessions and each prompt to %s. Secrets are "
              "stripped here first; code, paths and names are kept. Nothing is sent until you type the code and "
              "click Authorize in your browser." % server)
    import socket
    hostname = socket.gethostname().strip()
    hostname = hostname[:-6] if hostname.endswith(".local") else hostname
    shipping = MANUAL if manual else AUTOMATIC
    status, reply, failure = _request(server, "POST", "/api/cli-auth/new", {"hostname": hostname, "shipping": shipping})
    if status == 404:
        reply = {"nonce": "", "consent_policy": None}
    elif failure or status != 200 or not isinstance(reply, dict) or not reply.get("nonce"):
        print("Could not reach a CSA server at %s (%s). Nothing was connected or written here."
              % (server, tls_advice(failure) if failure.startswith("tls") else failure or "HTTP %d" % status))
        return 1
    policy = reply.get("consent_policy")
    if not isinstance(policy, int) or policy < REQUIRED_POLICY:
        print("%s is too old to record your consent, so nothing was connected or written here. Ask whoever runs it "
              "to upgrade. Until then, its Connect page offers csa-client." % server)
        return 1
    if policy > REQUIRED_POLICY:
        print("%s needs consent to policy %d and this plugin knows policy %d. Nothing was connected or written here. "
              "Update the plugin: %s" % (server, policy, REQUIRED_POLICY, RELEASES))
        return 1
    make_csa_dir()
    write_atomic(csa_path("connect.pending"), json.dumps({"nonce": reply["nonce"], "server": server, "shipping": shipping}))
    url = "%s/cli-auth?nonce=%s&hostname=%s" % (server, quote(reply["nonce"], safe=""), quote(hostname, safe=""))
    print("\nVerification code: %s\nType it on the page that opens. It proves the page is yours." % reply.get("code"))
    terminal = on_terminal()
    opened = open_browser(url)
    if terminal:
        print(("Opened your browser.\n" if opened else "Open this URL:\n  %s\n" % url) + "Waiting for you to authorize...")
        sys.stdout.flush()
        return child_poll()
    if not opened:
        # the URL holds the claim secret, and this stdout goes to the model
        remove_quietly(csa_path("connect.pending"))
        print("No browser could be opened from here. Run this in a terminal instead:\n  python3 -I -S -B %s connect %s%s"
              % (os.path.abspath(__file__), server, " --manual" if manual else ""))
        return 1
    print("Opened your browser. Nothing is on until you click Authorize; your next prompt confirms it.")
    sys.stdout.flush()
    detach("_poll")
    return 0


def child_poll():
    """Wait for the Authorize click. Closing the tab leaves no config and no credential."""
    pending = json.loads(read_text(csa_path("connect.pending")) or "{}")
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        time.sleep(2)
        now = json.loads(read_text(csa_path("connect.pending")) or "{}")
        if now.get("nonce") != pending.get("nonce"):
            return 1                                  # a newer connect, or a disconnect, took over
        status, creds, _ = _request(pending["server"], "GET", "/api/cli-auth/" + pending["nonce"])
        if isinstance(creds, dict) and creds.get("status") == "approved" and creds.get("token"):
            return finish_connect(pending, creds)
    remove_quietly(csa_path("connect.pending"))
    if load_config() is None:                         # leave no trace of a connect nobody finished
        if not read_text(csa_path("ship.log")):
            remove_quietly(csa_path("ship.log"))
        try:
            os.rmdir(csa_path())
        except OSError:
            pass
    print("Timed out waiting for the browser. Nothing was connected. Run /csa:connect again.")
    return 1


def finish_connect(pending, creds):
    server, shipping = pending["server"], pending["shipping"]
    policy = creds.get("policy") if isinstance(creds.get("policy"), dict) else {}
    days = policy.get("archive_days") if isinstance(policy.get("archive_days"), int) else 0
    if creds.get("shipping") != shipping or creds.get("consent_policy") != REQUIRED_POLICY:
        _request(server, "POST", "/api/token/revoke", {}, creds["token"])
        remove_quietly(csa_path("connect.pending"))
        print("The server authorized something other than what this plugin asked for. Its credential was revoked "
              "and nothing was connected.")
        return 1
    prev = load_config() or {}
    if server_of(prev) and prev.get("auth_token"):    # overwritten below: nothing else could ever revoke it
        _request(server_of(prev), "POST", "/api/token/revoke", {}, prev["auth_token"])
    # Only signing in again under the same terms keeps the floor: what was queued is still consented to.
    resigned = prev.get("shipping") == AUTOMATIC == shipping and server_of(prev) == server and consent_ok(prev)
    floor = prev["sweep_from"] if resigned and parse_floor(prev.get("sweep_from")) else now_rfc3339()
    mode = redaction_of(prev)[0]
    floor_mode = policy.get("redaction_min")
    if floor_mode in MODES and MODES.index(floor_mode) > MODES.index(mode):
        mode = floor_mode
    email = creds.get("email") or ""
    cfg = dict(prev, server=server, identity=email, auth_token=creds["token"], shipping=shipping,
               redaction={"mode": mode}, sweep_from=floor, server_archive_days=days, backfill=False,
               consent={"policy_version": REQUIRED_POLICY, "accepted_at": now_rfc3339(), "method": "browser",
                        "identity_at_consent": email, "archive_days_at_consent": days})
    save_config(cfg)
    if shipping == MANUAL:
        if prev.get("shipping") == AUTOMATIC:
            run_claude("mcp", "remove", "csa", "-s", "user")
        message = "CSA connected as %s, manual mode: nothing leaves until you run /csa:send-session." % email
    else:
        message = "CSA connected as %s to %s. New sessions ship when they end, and each prompt is sent for recall. " \
                  "Transcripts kept: %s. Check with /csa:status." % (email, server, retention_words(days))
        register_mcp(cfg)
    write_atomic(csa_path("connected_notice"), message)
    remove_quietly(csa_path("connect.pending"))
    print(message)
    return 0


def register_mcp(cfg):
    from urllib.parse import urlsplit
    server = server_of(cfg)
    _, health, _ = _request(server, "GET", "/api/health", timeout=5)
    health = health if isinstance(health, dict) else {}
    parts = urlsplit(server)
    mcp = health.get("mcp_url") or (health.get("mcp_port") and "%s://%s:%s%s" % (
        parts.scheme, parts.hostname, health["mcp_port"], health.get("mcp_path") or ""))
    if not mcp:
        return
    if urlsplit(mcp).hostname != parts.hostname:      # the token only ever goes to the host you connected to
        print("The server names its MCP endpoint on another host (%s), so it was not registered." % urlsplit(mcp).hostname)
        return
    run_claude("mcp", "remove", "csa", "-s", "user")
    run_claude("mcp", "add", "--transport", "http", "csa", mcp, "-s", "user",
               "--header", "X-CSA-User: " + cfg["identity"], "--header", "Authorization: Bearer " + cfg["auth_token"])


def removal_steps():
    return ("To remove the plugin too:\n  claude plugin uninstall csa@csa\n  claude plugin marketplace remove csa\n"
            '  rm -rf "%s"' % os.path.join(claude_dir(), "plugins", "cache", "csa"))


def cmd_disconnect(_args):
    cfg = load_config()
    if cfg is None:
        import shutil
        shutil.rmtree(csa_path(), ignore_errors=True)
        print("Not connected: there is no ~/.csa/config.json.\n" + removal_steps())
        return 0
    if cfg.get("shipping") not in (AUTOMATIC, MANUAL):
        print("~/.csa/config.json belongs to csa-client, so nothing was changed. `csa-client uninstall` removes "
              "that client; /csa:disconnect only undoes a /csa:connect.")
        return 1
    server, token = server_of(cfg), cfg.get("auth_token")
    forgotten = set(load_list(csa_path("forgotten")))
    sent = sorted({r["sid"] for r in ledger_rows() if r.get("sid") not in forgotten})
    if token and server:
        status, reply, failure = _request(server, "POST", "/api/token/revoke", {}, token, timeout=10)
        if status == 409:
            print("Nothing was removed: %s Run /csa:disconnect again after that." % (isinstance(reply, dict) and reply.get("error")))
            return 1
        if status == 200 and isinstance(reply, dict) and reply.get("revoked"):
            print("Credential revoked on %s." % server)
        elif status == 200 and isinstance(reply, dict) and reply.get("ok"):
            print("%s holds no per-user credential for this machine to revoke." % server)
        elif status == 401:
            print("Credential was already dead on %s." % server)
        else:
            why = failure or (isinstance(reply, dict) and reply.get("error")) or "HTTP %d" % status
            print("Could NOT revoke the credential on %s (%s). It stays live until it idles out; an admin can revoke "
                  "it from Team." % (server, why))
    if not run_claude("mcp", "remove", "csa", "-s", "user") and cfg.get("shipping") == AUTOMATIC:
        print('Could not remove the MCP server. Run: claude mcp remove csa -s user')
    import shutil
    shutil.rmtree(csa_path(), ignore_errors=True)
    print("Removed ~/.csa.")
    if sent:
        print("%d session(s) stay on the server under your name, and this machine can no longer delete them; a "
              "workspace admin can: %s" % (len(sent), " ".join(sent[:50])))
    print(removal_steps())
    return 0


# --- what a person runs -----------------------------------------------------------------------------

def find_transcript(sid):
    hits = [path for _, s, path in transcripts() if s == sid]
    return hits[0] if len(hits) == 1 else None


def session_arg(args):
    if "--session" in args and args.index("--session") + 1 < len(args):
        return args[args.index("--session") + 1]
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


def cmd_preview(args):
    """The exact `content` string for a session, its sha256 and label counts. No network."""
    sid = session_arg(args)
    if not sid:
        all_sessions = transcripts()
        sid = all_sessions[-1][1] if all_sessions else ""
    path = find_transcript(sid) if SID.match(sid) else None
    if not path:
        print("No single transcript matches session %r under %s." % (sid, os.path.join(claude_dir(), "projects")))
        return 1
    cfg = load_config()
    content, labels, mode = redacted_transcript(cfg, path)
    out = args[args.index("--out") + 1] if "--out" in args and args.index("--out") + 1 < len(args) else None
    if not out:
        make_csa_dir()
        os.makedirs(csa_path("preview"), mode=0o700, exist_ok=True)
        out = csa_path("preview", sid + ".txt")
    write_atomic(out, content)
    print("session %s\nmode    %s\nbytes   %d\nsha256  %s\nfile    %s" % (sid, mode, len(content.encode("utf-8")), sha256(content), out))
    for label in labels:
        print("redacted %s: %d" % (label, content.count("[REDACTED:%s]" % label)))
    if not labels:
        print("redacted nothing")
    if sid == os.environ.get("CLAUDE_CODE_SESSION_ID"):
        print("This session is still growing, so what you send later hashes differently. Preview a finished one with "
              "--session <id> to compare with ~/.csa/sent.jsonl.")
    return 0


def list_sessions():
    print("Recent sessions on this machine, newest first (time, id, project folder, bytes):")
    for mtime, sid, path in transcripts()[-10:][::-1]:
        print("%s %s %s %d" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)), sid,
                               os.path.basename(os.path.dirname(path)), os.path.getsize(path)))
    return 0


def cmd_send_session(args):
    if "--list" in args:
        return list_sessions()
    cfg = load_config()
    where = conflict()
    if where:
        print(conflict_notice(where))
        return 1
    if not cfg or cfg.get("shipping") not in (AUTOMATIC, MANUAL) or not server_of(cfg):
        print("Not connected. Run /csa:connect <server-url> first; add --manual to send only what you choose.")
        return 1
    if not consent_ok(cfg):
        print("Your consent covers an older policy, so nothing was sent. Run /csa:connect.")
        return 1
    status, reply, failure = _request(server_of(cfg), "GET", "/api/client-config", token=cfg.get("auth_token"), timeout=8)
    note_auth(status)
    if isinstance(reply, dict) and isinstance(reply.get("archive_days"), int):
        update_config(server_archive_days=reply["archive_days"])
        cfg["server_archive_days"] = reply["archive_days"]
    if retention_exceeds_consent(cfg):
        print("Nothing was sent: this server keeps transcripts %s and you agreed to %s. Run /csa:connect to see the "
              "new terms." % (retention_words(cfg.get("server_archive_days") or 0), retention_words(days_agreed(cfg))))
        return 1
    sid = session_arg(args)
    path = find_transcript(sid) if SID.match(sid) else None
    if not path:
        print("No single transcript matches session %r, so nothing was sent." % sid)
        return 1
    outcome = ship_one(cfg, sid, path, explicit=True)
    if outcome == "sent":
        last = ledger_rows()[-1]
        print("Sent %s to %s: %d bytes, labels %s, sha256 %s.\nLater turns stay here unless you send again. "
              "/csa:forget %s takes it back." % (sid, last["server"], last["bytes"], ",".join(last["labels"]) or "none",
                                                 last["sha256"], sid))
        return 0
    print({"skipped": "Nothing new since it was last sent.", "excluded": "This project is excluded by /csa:scope, so nothing was sent.",
           "claimed": "Another send of this session is in flight.", "too_large": "It is over the server's 32 MiB limit, so nothing was sent.",
           "unauthorized": "The server no longer accepts this credential. Run /csa:connect.",
           "rejected": "The server read it and refused it: see ~/.csa/sent.jsonl.",
           "down": "The server could not take it, for the reason above. Nothing is lost; try again."}[outcome])
    return 0 if outcome == "skipped" else 1


def cmd_sent(_args):
    rows = ledger_rows()
    forgotten = load_list(csa_path("forgotten"))
    if not rows:
        print("Nothing has been sent from this machine (~/.csa/sent.jsonl is empty).")
    for r in rows[-20:]:
        print("%s %s %s %s %d bytes labels=%s sha256=%s ok=%s%s" % (
            r["time"], r["sid"], r["server"], r["mode"], r["bytes"], ",".join(r["labels"]) or "none", r["sha256"], r["ok"],
            " (forgotten)" if r["sid"] in forgotten else ""))
    return 0


def cmd_status(_args):
    cfg = load_config()
    print("plugin %s. Updates are yours to run: %s" % (VERSION, RELEASES))
    for managed in ("/Library/Application Support/ClaudeCode/managed-settings.json", "/etc/claude-code/managed-settings.json"):
        if '"allowManagedHooksOnly": true' in (read_text(managed) or "").replace(" :", ":"):
            print("Blocked: your organization's Claude Code settings allow only managed hooks, so this plugin's hooks do not run.")
    where = conflict()
    if where:
        print(conflict_notice(where))
    if os.path.exists(csa_path("connect.pending")):
        print("A connect is waiting for you to click Authorize in the browser.")
    if not cfg or cfg.get("shipping") not in (AUTOMATIC, MANUAL):
        print("Not connected. Run /csa:connect <server-url>." + (" csa-client's settings in ~/.csa are kept." if cfg else ""))
        return 0
    server, token = server_of(cfg), cfg.get("auth_token")
    print("connected to %s as %s, %s mode, redaction %s" % (server, cfg.get("identity"), "manual" if cfg["shipping"] == MANUAL else "automatic", redaction_of(cfg)[0]))
    if not consent_ok(cfg):
        print("Consent covers an older policy: nothing ships. Run /csa:connect.")
    elif retention_exceeds_consent(cfg):
        print("Paused: the server keeps transcripts %s and you agreed to %s. Run /csa:connect." % (retention_words(cfg.get("server_archive_days") or 0), retention_words(days_agreed(cfg))))
    status, _, failure = _request(server, "GET", "/api/me", token=token, timeout=5)
    if failure.startswith("tls"):
        print("Certificate problem: " + tls_advice(failure))
    elif failure:
        print("Server unreachable (%s). Check the URL and your VPN; sessions wait here." % failure)
    elif status == 401:
        print("The server no longer accepts this credential. Run /csa:connect.")
    elif not 200 <= status < 300:
        print("The server answered HTTP %d." % status)
    else:
        print("server reachable, credential accepted")
    for r in ledger_rows()[-3:]:
        print("last sent %s %s ok=%s" % (r["time"], r["sid"], r["ok"]))
    return 0


def cmd_scope(args):
    if not args:
        for name in ("only", "ignore"):
            print("~/.csa/%s: %s" % (name, ", ".join(load_list(csa_path(name))) or "(empty)"))
        root = os.path.join(claude_dir(), "projects")
        print("Project folders globs match:\n  " + "\n  ".join(sorted(os.listdir(root)) if os.path.isdir(root) else []))
        return 0
    if args[0] == "clear" and len(args) == 2 and args[1] in ("only", "ignore"):
        remove_quietly(csa_path(args[1]))
        print("Cleared ~/.csa/%s." % args[1])
        return 0
    if len(args) != 2 or args[0] not in ("only", "ignore") or glob_regex(args[1]) is None:
        print("Usage: scope [only|ignore <glob>] | scope clear <only|ignore>")
        return 2
    make_csa_dir()
    path = csa_path(args[0])
    write_atomic(path, "\n".join(load_list(path) + [args[1]]) + "\n")
    print("Added %s to ~/.csa/%s. It applies to future shipping; /csa:forget deletes what already left." % (args[1], args[0]))
    return 0


def cmd_forget(args):
    cfg = load_config()
    if not cfg or not server_of(cfg) or not args or not all(SID.match(a) for a in args):
        print("Usage: forget <session-id> [<session-id>...], when connected. /csa:sent lists ids.")
        return 2
    failed = 0
    for sid in args:
        status, reply, failure = _request(server_of(cfg), "POST", "/api/forget", {"session_id": sid, "owner": cfg.get("identity")}, cfg.get("auth_token"), timeout=60)
        ok = status == 200 and isinstance(reply, dict) and reply.get("ok")
        failed += not ok
        if ok:
            with open(csa_path("forgotten"), "a", encoding="utf-8") as f:
                f.write(sid + "\n")
        print("%s %s" % (sid, "deleted" if ok else "NOT deleted: %s" % (failure or (isinstance(reply, dict) and reply.get("error")) or status)))
    return 1 if failed else 0


def cmd_team(_args):
    cfg = load_config()
    if not cfg or not server_of(cfg):
        print("Not connected. Run /csa:connect <server-url>.")
        return 1
    status, reply, failure = _request(server_of(cfg), "GET", "/api/team", token=cfg.get("auth_token"))
    if failure or status != 200 or not isinstance(reply, list):
        print("Could not read the team from %s: %s" % (server_of(cfg), failure or "HTTP %d" % status))
        return 1
    for m in reply:
        print("%s: %s session(s), last active %s" % (m.get("user"), m.get("sessions"), m.get("last_active")))
    return 0


COMMANDS = {"connect": cmd_connect, "disconnect": cmd_disconnect, "preview": cmd_preview, "send-session": cmd_send_session,
            "sent": cmd_sent, "status": cmd_status, "scope": cmd_scope, "forget": cmd_forget, "team": cmd_team}


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "_poll":
        return child_poll()
    if cmd in COMMANDS:
        return COMMANDS[cmd](argv[1:])
    print("usage: csa.py <%s|redact>" % "|".join(COMMANDS))
    return 2
