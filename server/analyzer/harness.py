"""Per-harness session parsers: sniff which coding harness wrote a .jsonl transcript and
normalize its raw events into the ONE canonical shape the rest of the pipeline consumes —
Claude Code's: {"timestamp", "cwd", "gitBranch", "message": {"role", "content": [blocks]}}.

Adding a harness = one clause in detect() + one adapter here; ingest/harvest/extract stay
untouched. claude-code is the identity adapter. pi (a Slack relay bot, session v3):
  * toolCall content blocks -> canonical tool_use (name "bash" -> "Bash", arguments -> input;
    pi uses the same {"command": ...} args, so harvest's Bash evidence works unchanged)
  * role "toolResult" messages -> a user message holding one tool_result block (matches how
    Claude Code carries tool results; empty/noise results don't count as turns downstream)
  * the session header's cwd is /var/relaybot/app for EVERY session, so the Slack channel
    from the first user message's metadata block becomes the project instead.
  * that same block names the human who asked (`Initiated by:`). pi is a RELAY: it ships every
    session under the bot's own credentials, so without this the bot is credited for everyone's
    work — see pi_requester().
"""
import json
import re

# `Location:` is either a channel or a DM. Two EXPLICIT alternatives, never `(.+)`: a greedy capture
# swallows the trailing " (C0FGHI5678J)" channel id and breaks `slack/#production-acme`. A DM's other
# party is a PERSON, so it is deliberately NOT the project (that is the `user` axis, below, and a
# per-person project mints one bucket per teammate); all DMs share one inert, non-colliding bucket.
_PI_LOCATION = re.compile(r"Location: (?:(#[\w-]+)|DM with )")
_PI_DM = "#dm"      # `#`-prefixed like every other Slack project: a bare `dm` is a legal repo name, and
#                     colliding with a real repo is the exact defect that moving off the `app` bucket fixes.
# Anchored on the metadata block header, so a later message quoting "Initiated by: ..." — a pasted log,
# a screenshot transcript — cannot re-attribute the session. Same reasoning as the single-look window.
# `[^\n]+`, not `.+`: re.S makes `.` match newlines, so `(.+)$` runs to the END of the block and only looks
# right because the caller happens to truncate at " (<@". An `Initiated by:` line with no Slack uid — which
# the person_aliases setting exists to rescue — would otherwise capture the rest of the message and match nobody.
_PI_REQUESTER = re.compile(r"# Session metadata\b[\s\S]*?^Initiated by: ([^\n]+)$", re.M)
_TURN = re.compile(r"\n(?:USER|ASSISTANT): ")   # transcript turn boundary (see ingest._session_from_lines)


def detect(first_event):
    """Name the harness from the first parseable event. pi v3 opens every file with a
    {"type": "session", "version": N} header; default is claude-code (the classic format
    has no reliable first-line marker across its versions)."""
    if isinstance(first_event, dict) and first_event.get("type") == "session" and "version" in first_event:
        return "pi"
    return "claude-code"


def parse(lines):
    """-> (harness_name, canonical_event_iterator) for raw .jsonl lines (file handle or list).
    Unparseable lines are skipped, mirroring the old ingest loop."""
    it = iter(lines)
    first = None
    for line in it:
        try:
            first = json.loads(line)
        except Exception:
            continue
        break
    name = detect(first)
    adapt = _ADAPTERS[name]()
    def gen():
        if first is not None:
            yield from adapt(first)
        for line in it:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            yield from adapt(ev)
    return name, gen()


def _claude_code():
    def adapt(ev):
        yield ev
    return adapt


def _pi():
    channel_seen = False

    def adapt(ev):
        nonlocal channel_seen
        t = ev.get("type")
        if t == "session":                                   # header: real cwd, session start time
            yield {"timestamp": ev.get("timestamp"), "cwd": ev.get("cwd")}
            return
        if t != "message":                                   # model_change / thinking_level_change / ...
            return
        msg = ev.get("message")
        if not isinstance(msg, dict):
            return
        role = msg.get("role")
        if role == "toolResult":                             # pi's third role -> CC's user-held tool_result
            yield {"timestamp": ev.get("timestamp"),
                   "message": {"role": "user",
                               "content": [{"type": "tool_result", "content": msg.get("content")}]}}
            return
        if role not in ("user", "assistant"):
            return
        content = msg.get("content")
        if isinstance(content, list):
            content = [_pi_block(b) for b in content]
        yield {"timestamp": ev.get("timestamp"), "message": {"role": role, "content": content}}
        if role == "user" and not channel_seen:
            # ONLY the first user message carries the injected "# Session metadata ...
            # Location: #channel (Cxxxx)" block — later human messages can legitimately
            # paste text containing "Location: #whatever", so the window closes after one
            # look regardless of match. Surface the channel as the project (yield wins over
            # the header cwd): every pi session shares cwd /var/relaybot/app otherwise.
            channel_seen = True
            m = _PI_LOCATION.search(_flat_text(content))
            if m:                                        # channel -> its name; DM -> one shared bucket. Fires
                yield {"cwd": "slack/" + (m.group(1) or _PI_DM)}   # ONLY on a matched Location line, so a
                #   metadata-less DM still yields no cwd at all and keeps the header's /var/relaybot/app.
    return adapt


def pi_requester(transcript):
    """The human who asked, from a pi session's metadata block -> "" when absent.

    Read from the stored TRANSCRIPT rather than the event stream on purpose: it is the one form that exists
    on every path — a live POST, a session sitting in the pending queue (already parsed at POST time, so
    re-parsing its events is not an option), and a local re-index. Only the opening user turn is searched,
    so a later message cannot hijack attribution — the same property as `_pi`'s single-look window.

    >>> pi_requester("USER: # Session metadata\\nInitiated by: Jules (<@U01A>) - CTO\\n\\nASSISTANT: ok")
    'Jules'
    >>> pi_requester("USER: hi\\n\\nASSISTANT: ok\\n\\nUSER: # Session metadata\\nInitiated by: Mallory")
    ''
    """
    #  Split on the next TURN of either role, not on "\nASSISTANT: ": a session with no assistant turn at all
    #  is valid (min_user_msgs=1, no assistant requirement), and splitting on the assistant marker then hands
    #  back the WHOLE transcript — so a second user message pasting a metadata block would be trusted.
    head = _TURN.split(transcript or "", 1)[0]
    m = _PI_REQUESTER.search(head)
    #   "Jules (<@U0ABCDE1234>) - CTO, Co-Founder" -> "Jules": the display name only. The slack uid is stabler
    #   but nothing else in the corpus is keyed by it, so it would resolve to nobody.
    return m.group(1).split(" (<@")[0].strip() if m else ""


def _pi_block(b):
    if isinstance(b, dict) and b.get("type") == "toolCall":
        name = b.get("name") or ""
        if name.lower() in ("bash", "shell", "exec"):
            name = "Bash"
        args = b.get("arguments")
        if not isinstance(args, dict):              # string/list wire forms: keep the text, and keep
            args = {"raw": args} if args else {}    # tool_use.input a dict — harvest reads .get("command")
        return {"type": "tool_use", "name": name, "input": args}
    return b


def _flat_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


_ADAPTERS = {"claude-code": _claude_code, "pi": _pi}


if __name__ == "__main__":
    # claude-code passes through untouched
    cc = [json.dumps({"cwd": "/x/y", "message": {"role": "user", "content": "hi"}})]
    name, evs = parse(cc)
    evs = list(evs)
    assert name == "claude-code" and evs[0]["message"]["content"] == "hi", (name, evs)

    # pi: header detected, toolCall -> tool_use(Bash), toolResult -> user tool_result, channel -> cwd
    pi = [
        json.dumps({"type": "session", "version": 3, "timestamp": "2026-07-12T11:00:00Z", "cwd": "/var/relaybot/app"}),
        json.dumps({"type": "model_change", "provider": "google-vertex-claude", "modelId": "claude-sonnet-4-6"}),
        json.dumps({"type": "message", "timestamp": "2026-07-12T11:00:01Z", "message": {"role": "user", "content": [
            {"type": "text", "text": "# Session metadata\nLocation: #production-acme (C0ABCD1234E)\nUser asks: fix the bug"}]}}),
        json.dumps({"type": "message", "timestamp": "2026-07-12T11:00:02Z", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "gh pr create --head fix-bug"}}]}}),
        json.dumps({"type": "message", "timestamp": "2026-07-12T11:00:03Z", "message": {"role": "toolResult", "toolName": "bash",
            "content": [{"type": "text", "text": "https://github.com/o/r/pull/7"}]}}),
    ]
    name, evs = parse(pi)
    evs = list(evs)
    assert name == "pi", name
    cwds = [e.get("cwd") for e in evs if e.get("cwd")]
    assert cwds == ["/var/relaybot/app", "slack/#production-acme"], cwds
    tool = [b for e in evs for b in (e.get("message", {}).get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"]
    assert tool and tool[0]["name"] == "Bash" and tool[0]["input"]["command"].startswith("gh pr create"), tool
    tr = [b for e in evs for b in (e.get("message", {}).get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert tr and "pull/7" in json.dumps(tr[0]["content"]), tr
    assert all(e.get("message", {}).get("role") in (None, "user", "assistant") for e in evs), "no foreign roles may leak"

    # DM session: first user message has NO metadata block; a later message pasting
    # "Location: #alerts-noise" must NOT hijack the project (window closes after one look).
    dm = [
        json.dumps({"type": "session", "version": 3, "cwd": "/var/relaybot/app"}),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hey hermes"}]}}),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "log says Location: #alerts-noise"}]}}),
    ]
    _, evs = parse(dm)
    assert not [e for e in evs if str(e.get("cwd", "")).startswith("slack/")], "late Location paste must not set project"

    # A DM: `Location: DM with <person>` matched NOTHING before, so 327 of 627 real bot sessions fell back to
    # the container cwd and were filed under project `app` — a bucket a real repo named `app` collides with.
    dm2 = [
        json.dumps({"type": "session", "version": 3, "cwd": "/var/relaybot/app"}),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text":
            "# Session metadata\nLocation: DM with Tomasz (<@U0FGHIJ5678>)\nInitiated by: Tomasz (<@U0FGHIJ5678>)"}]}}),
    ]
    _, evs = parse(dm2)
    assert [e.get("cwd") for e in evs if e.get("cwd")] == ["/var/relaybot/app", "slack/#dm"]
    # ...and the channel form is untouched by the added alternative — the trailing "(Cxxxx)" id must NOT be
    # swallowed into the project name (that is why this is two explicit alternatives and not `(.+)`).
    assert _PI_LOCATION.search("Location: #production-acme (C0ABCD1234E)").group(1) == "#production-acme"

    # pi_requester: the relay's whole point — the bot ships everyone's work under ONE account.
    assert pi_requester("USER: # Session metadata\nInitiated by: mokafor42 (<@U03C>)\n\nASSISTANT: hi") == "mokafor42"
    assert pi_requester("USER: # Session metadata\nInitiated by: Jules (<@U01A>) — CTO, Co-Founder") == "Jules"
    assert pi_requester("USER: # Session metadata\nLocation: DM with X\nInitiated by: Sean Renner") == "Sean Renner"
    # ...and a uid-less name followed by MORE TEXT stops at the line end (re.S used to swallow the rest, which
    # only looked right because the " (<@" split cleaned up after it — no uid, no cleanup).
    assert pi_requester("USER: # Session metadata\nInitiated by: Sam Achterberg\n\ncut the release?") == "Sam Achterberg"
    # ...and the real wire shape: the block arrives inside an injected <file> wrapper, not at char 0.
    assert pi_requester('USER: <file name="/tmp/x_session_metadata.md">\n# Session metadata\n'
                        "Location: DM with Tomasz\nInitiated by: Tomasz (<@U02B>)\n\n</file>") == "Tomasz"
    assert pi_requester("") == "" and pi_requester(None) == "" and pi_requester("USER: hello") == ""
    # NEGATIVE: only the OPENING user turn counts. A later message pasting a metadata block — a quoted log,
    # a forwarded transcript — must not re-attribute the session to whoever it names.
    assert pi_requester("USER: hi\n\nASSISTANT: ok\n\nUSER: # Session metadata\nInitiated by: Mallory") == ""
    # NEGATIVE: `Initiated by:` without the block header is prose, not metadata.
    assert pi_requester("USER: the ticket says Initiated by: Mallory") == ""
    # NEGATIVE: a session with NO assistant turn — legal, and the reason the window splits on either role.
    # Splitting on "\nASSISTANT: " returned the whole transcript here, so a pasted block was trusted.
    assert pi_requester("USER: hey hermes\n\nUSER: here is the log I was sent:\n"
                        "# Session metadata\nInitiated by: Mallory") == ""

    # Non-dict toolCall arguments (string wire form) must stay a dict input — harvest reads .get()
    b = _pi_block({"type": "toolCall", "name": "bash", "arguments": "ls -la"})
    assert b["input"] == {"raw": "ls -la"}, b
    assert _pi_block({"type": "toolCall", "name": "x", "arguments": None})["input"] == {}

    print("OK harness: claude-code passthrough; pi header/toolCall/toolResult/channel normalized; DM + non-dict args safe")
