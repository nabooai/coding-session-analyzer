"""Per-session efficiency metrics + the findings a person can act on.

Answers the three questions a developer asks about a finished session — how do I make this cheaper,
shorter, and less wasteful — from data the transcript already carries: `message.usage` (tokens),
`tool_use` blocks (what was done), `tool_result.is_error` (what failed), and timestamps.

DETERMINISTIC ON PURPOSE. No LLM call. Every number here is counted, not inferred, so the pass is
free, instant, and cannot hallucinate a finding — and it runs inside the ingest walk that already
visits every event (see ingest._session_from_lines), so it costs one dict per session, not a re-read.
A model-written narration layer can sit on top later; it would have nothing to add to the counting.

ponytail: counters + thresholds, no scoring model. A threshold that fires on a session the developer
already knows was fine is a false positive they can ignore; a score they can't check is worse.
"""
import hashlib
import json
import time
import calendar

from sqlalchemy import case, or_

# $ per million tokens (input, output): the list prices Anthropic, OpenAI and the Gemini API published
# as of 2026-10-19, a Pro model at its rate for prompts up to 200k tokens. A model id belongs to the
# longest key it starts with when the next character ends the name (`_ENDS`), so a dated snapshot
# resolves to its family, gpt-5-mini is not gpt-5, and gpt-5.3 is unpriced rather than guessed.
# Cache writes bill above input (1.25x at the 5m TTL, 2x at 1h) and cache reads at 0.1x.
PRICES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "gpt-6-astra": (10.0, 50.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (2.50, 15.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-pro": (30.0, 180.0),
    "gpt-5.2": (1.75, 14.0),
    "gpt-5.2-pro": (21.0, 168.0),
    "gpt-5.1": (1.25, 10.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-pro": (15.0, 120.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (2.0, 8.0),
    "o3-mini": (1.10, 4.40),
    "o3-pro": (20.0, 80.0),
    "o4-mini": (1.10, 4.40),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3-flash": (0.50, 3.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
_ENDS = ("-", "@", ":")
CACHE_WRITE_5M, CACHE_WRITE_1H, CACHE_READ = 1.25, 2.0, 0.1
IDLE_GAP_S = 300                 # a gap this long is the human away, not the agent working
_DUP_CAP = 4000                  # distinct call signatures kept; a runaway loop is caught long before


def price(model):
    """(input, output) $ per million tokens, or None when CSA knows no list price for `model`. The one
    answer to "is this priced": a session's cost, CSA's own spend and every screen showing either."""
    model = model or ""
    keys = [k for k in PRICES if model == k or model.startswith(tuple(k + e for e in _ENDS))]
    return PRICES[max(keys, key=len)] if keys else None


def list_cost(model, fresh, cached, written, out):
    """$ at list price, or None when `model` has none: the ledger keeps the tokens, the cost stays unknown."""
    rates = price(model)
    if rates is None:
        return None
    pin, pout = rates
    return round((fresh * pin + (cached or 0) * pin * CACHE_READ
                  + (written or 0) * pin * CACHE_WRITE_5M + (out or 0) * pout) / 1e6, 6)


def known_cost(m):
    """`m.cost_usd` where `m.model` has a list price, else NULL: `price` as SQL, for stored metrics."""
    priced = or_(m.c.model.in_(list(PRICES)),
                 *(m.c.model.startswith(k + e, autoescape=True) for k in PRICES for e in _ENDS))
    return case((priced, m.c.cost_usd), else_=None).label("cost_usd")


def _epoch(ts):
    """ISO-8601 UTC -> epoch seconds. Claude Code stamps `...Z`; time.mktime would apply the LOCAL
    zone and shift every duration by the machine's offset (the bug that once hard-expired a fleet)."""
    if not isinstance(ts, str):
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def new():
    return {"tools": {}, "errors": {}, "n_errors": 0, "dups": {}, "reads": {}, "seen_msgs": set(),
            "in": 0, "out": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0,
            "models": {}, "first": None, "last": None, "idle_s": 0, "max_gap_s": 0,
            "n_asst": 0, "n_user": 0}


def _bump(d, k, n=1):
    if k:
        d[k] = d.get(k, 0) + n


def scan(acc, ev):
    """Fold one raw event. Never raises: a malformed event costs its own numbers, not the session's."""
    try:
        at = _epoch(ev.get("timestamp"))
        if at is not None:
            if acc["first"] is None or at < acc["first"]:
                acc["first"] = at
            if acc["last"] is not None and at > acc["last"]:
                gap = at - acc["last"]
                if gap > acc["max_gap_s"]:
                    acc["max_gap_s"] = gap
                if gap >= IDLE_GAP_S:
                    acc["idle_s"] += gap
            if acc["last"] is None or at > acc["last"]:
                acc["last"] = at

        msg = ev.get("message")
        if not isinstance(msg, dict):
            return
        role = msg.get("role")
        acc["n_asst"] += role == "assistant"
        acc["n_user"] += role == "user"

        # ONE API response is written across SEVERAL transcript lines — one per content block —
        # and every line repeats the whole `usage`. Counting per line double-counted the cost (measured
        # 2.0x on a real session). Bill each response ONCE, by message id; content blocks are still read
        # from every line below, because each line carries a DIFFERENT block (dedupe there would lose
        # tool calls: 267 real tool_use blocks vs 248 if only the last line of each response were read).
        u = msg.get("usage")
        mid = msg.get("id")
        if isinstance(u, dict) and not (mid and mid in acc["seen_msgs"]):
            if mid:
                acc["seen_msgs"].add(mid)
            _bump(acc["models"], msg.get("model") or "unknown")
            acc["in"] += int(u.get("input_tokens") or 0)
            acc["out"] += int(u.get("output_tokens") or 0)
            acc["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
            cc = u.get("cache_creation")
            if isinstance(cc, dict):                       # the TTL split, when the harness reports it
                acc["cache_5m"] += int(cc.get("ephemeral_5m_input_tokens") or 0)
                acc["cache_1h"] += int(cc.get("ephemeral_1h_input_tokens") or 0)
            else:                                          # older/other harness: total only -> price at 5m
                acc["cache_5m"] += int(u.get("cache_creation_input_tokens") or 0)

        content = msg.get("content")
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "tool_use":
                name = b.get("name") or "?"
                _bump(acc["tools"], name)
                # Signature = tool + its exact arguments. Two identical signatures in one session is
                # the same question asked twice — hashed, not stored, so a 200KB Write never sits in RAM.
                try:
                    raw = json.dumps(b.get("input", {}), sort_keys=True)
                except (TypeError, ValueError):
                    raw = str(b.get("input"))
                if len(acc["dups"]) < _DUP_CAP or (name, hashlib.sha1(raw.encode()).hexdigest()[:12]) in acc["dups"]:
                    _bump(acc["dups"], (name, hashlib.sha1(raw.encode()).hexdigest()[:12]))
                if name in ("Read", "read"):
                    inp = b.get("input")
                    if isinstance(inp, dict):
                        _bump(acc["reads"], inp.get("file_path") or inp.get("path"))
            elif t == "tool_result" and b.get("is_error"):
                acc["n_errors"] += 1
                _bump(acc["errors"], b.get("_tool_name") or "tool")
    except Exception:                                      # a metrics bug must never fail an ingest
        return


def finish(acc):
    """Accumulator -> the flat metrics row stored per session."""
    model = max(acc["models"], key=acc["models"].get) if acc["models"] else ""
    rates = price(model)
    tokens = acc["in"] + acc["out"] + acc["cache_read"] + acc["cache_5m"] + acc["cache_1h"]
    # No usage, or a model with no list price, is an unknown cost (the UI shows a dot), never $0.00.
    pin, pout = rates or (0, 0)
    cost = None if tokens == 0 or rates is None else round((acc["in"] * pin
            + acc["cache_5m"] * pin * CACHE_WRITE_5M
            + acc["cache_1h"] * pin * CACHE_WRITE_1H
            + acc["cache_read"] * pin * CACHE_READ
            + acc["out"] * pout) / 1_000_000, 4)
    wall = (acc["last"] - acc["first"]) if (acc["first"] is not None and acc["last"] is not None) else 0
    dup = sum(n - 1 for n in acc["dups"].values() if n > 1)          # repeats, not distinct calls
    # Reading a file twice is normal (read, edit, re-read). Count only what is beyond that, so the
    # number never jumps from 0 to 2 as a file crosses the threshold.
    reread = sum(n - 2 for n in acc["reads"].values() if n > 2)
    top_read = max(acc["reads"], key=acc["reads"].get) if acc["reads"] else ""
    return {
        "model": model,
        "tool_calls": sum(acc["tools"].values()),
        "tool_errors": acc["n_errors"],
        "dup_calls": dup,
        "reread_calls": reread,
        "top_read": top_read if reread else "",
        "top_read_n": acc["reads"].get(top_read, 0) if reread else 0,
        "top_tool": max(acc["tools"], key=acc["tools"].get) if acc["tools"] else "",
        "in_tokens": acc["in"], "out_tokens": acc["out"],
        "cache_read_tokens": acc["cache_read"],
        "cache_write_tokens": acc["cache_5m"] + acc["cache_1h"],
        "cost_usd": cost,
        "wall_s": max(0, wall), "idle_s": min(acc["idle_s"], max(0, wall)),
        "max_gap_s": acc["max_gap_s"],
        "n_user": acc["n_user"], "n_asst": acc["n_asst"],
    }


# --- findings: the measurement, the reasoning, and the instruction ---------------------------
# Every finding carries its own short chain of thought as three fields, because a number with no
# reasoning is unactionable and a recommendation with no evidence is unfalsifiable:
#
#   saw  — what was counted. The evidence. Checkable against the session.
#   why  — the one-line inference from that evidence. This is the step a reader would otherwise
#          have to guess, and the step they need in order to disagree with us.
#   do   — the instruction. Written as an imperative addressed to whoever works on this next,
#          human or agent, because that is literally who reads it (see project_lessons below).
#
# `rule` is the stable id the project rollup groups on — headlines carry per-session numbers and
# would never group.

def findings(m):
    out = []
    if not m:
        return out
    add = lambda *a: out.append(dict(zip(("kind", "rule", "headline", "saw", "why", "do"), a)))
    # A metrics row read back from SQLite has every column PRESENT but possibly NULL, and
    # `dict.get(k, 0)` returns None for that — not 0 — so every comparison below would raise
    # TypeError on a partially-written row. Coerce once, here, rather than at each use.
    m = {k: (0 if v is None and k not in ("model", "top_read", "top_tool") else v) for k, v in m.items()}
    calls, cost = m.get("tool_calls") or 0, m.get("cost_usd") or 0

    dup = m.get("dup_calls") or 0
    # Both bars: 3 absolute AND 5% of calls. On an absolute floor alone a 200-call session trips this at
    # 1.5% — noise, shown with the same weight as a real finding, which teaches the reader to skip it.
    if calls and dup >= max(3, calls // 20):
        add("turns", "repeated-calls", f"{dup} tool calls repeated work already done",
            f"{dup} of {calls} calls ({dup * 100 // calls}%) ran with byte-identical arguments to an "
            "earlier call in the same session.",
            "Identical arguments return identical results, so the answer was already in the transcript "
            "when the call was made.",
            "Before calling a tool, check whether the same call was already made this session and reuse "
            "that result.")

    reread = m.get("reread_calls") or 0
    if reread >= 2 and m.get("top_read"):
        short = m["top_read"].split("/")[-1]
        add("turns", "rereads", f"{short} was read {m['top_read_n']} times",
            f"{m['top_read']} was opened {m['top_read_n']} times; {reread} reads across the session were "
            "the third or later look at the same file.",
            "A file that has not been edited since the last read returns the same bytes, so those reads "
            "added no information and each one cost a turn.",
            "Read a file once and work from what you already have; re-read it only after editing it, and "
            "read a specific range rather than the whole file when you need one part.")

    errs = m.get("tool_errors") or 0
    if errs >= 3 and calls:
        add("time", "failed-calls", f"{errs} tool calls failed",
            f"{errs} of {calls} calls ({errs * 100 // calls}%) came back as errors.",
            "A failed call costs the call, the error, and the retry before any work happens — three turns "
            "spent to learn the invocation was wrong.",
            "When a command fails twice the same way, stop retrying and check the actual constraint "
            "(the path, the flag, the environment) instead of varying the guess.")

    cw, cr = m.get("cache_write_tokens") or 0, m.get("cache_read_tokens") or 0
    if cw + cr > 100_000 and cr < cw:
        add("cost", "cache-churn", "The prompt cache was rewritten more than it was read",
            f"{cw:,} tokens written to cache vs {cr:,} read from it.",
            "Cache writes bill above the input rate and reads at a tenth of it, so writes exceeding reads "
            "means the context prefix kept changing instead of being appended to.",
            "Keep what is stable at the front of the session and add new context at the end; editing or "
            "reordering earlier context invalidates everything after it.")

    if calls and (m.get("n_user") or 0) and calls / max(1, m["n_user"]) >= 25:
        add("turns", "exploration-heavy", f"{calls} tool calls for {m['n_user']} requests",
            f"About {calls // max(1, m['n_user'])} tool calls per thing that was asked for.",
            "That ratio means most of the work was finding out what the task was, not doing it — the goal "
            "arrived in pieces rather than up front.",
            "State the whole task, the constraints, and what done looks like in the first message, so the "
            "work can be planned instead of discovered.")

    wall, idle = m.get("wall_s") or 0, m.get("idle_s") or 0
    active = max(0, wall - idle)
    # Only fire when the gap looks like DRIFT, not like deliberately resuming one task the next day.
    # A long span with real work either side of the gap is a person coming back to finish something —
    # telling them to start fresh there would throw away the context that made the second pass fast.
    # The signal for drift is a big span with little to show for it; `active` bounds that.
    if wall >= 1800 and idle > active and active < 1800 and cost < 5:
        add("time", "cold-resume", f"{_dur(idle)} of the {_dur(wall)} was waiting, not working",
            f"Only {_dur(active)} of activity across {_dur(wall)}; longest single gap "
            f"{_dur(m.get('max_gap_s') or 0)}.",
            "A span this long with this little in it is usually a session left open rather than one "
            "being deliberately resumed — and every return re-reads the whole context first.",
            "If you are coming back to finish the same task, resuming is right and this can be ignored. "
            "If the tab was just left open, start the next sitting fresh and carry the conclusions over.")

    # A big session is not a problem. Firing "split this up" on every large session — byte-identical
    # SAW/WHY/DO regardless of what the session did — made this the weakest thing on the page: a fixed
    # disclaimer wearing a finding's clothes. It now fires only when the size came with measurable
    # WASTE, and it names that waste instead of restating the token count.
    waste = dup + reread + errs
    if cost >= 5 and calls and waste >= max(5, calls // 20):
        add("cost", "expensive-and-wasteful",
            f"${cost:.2f} of API-rate tokens, and {waste} of {calls} calls bought nothing",
            f"{waste} avoidable calls ({dup} repeated, {reread} re-reads, {errs} failed) in a session "
            f"costing ${cost:.2f} at {m.get('model') or 'list'} pay-as-you-go rates.",
            "Large is fine — large WITH a high avoidable share is not, because every wasted call also "
            "re-sends the whole context behind it, so the cost of a repeat is not one call, it is one "
            "call plus the conversation so far.",
            "Fix the repeated, re-read and failed calls first: each avoidable call re-sends the whole "
            "conversation behind it. The rest of the cost is the conversation itself being re-sent on every "
            "turn, which comes down by keeping sessions short and focused rather than by removing calls. "
            "(On a Claude subscription these tokens are covered by the plan: read the figure as the size of "
            "the session, not as money owed.)")
    return out


# --- project rollup: what a future session on this project should know ------------------------
# One session's finding is an anecdote. The same finding across several sessions in the same project
# is a property OF THE PROJECT — and that is the thing worth handing to whoever works on it next,
# human or coding agent. Grouped on `rule` (stable) and never on `headline` (per-session numbers).

# A lesson has to clear BOTH bars: enough sessions, and enough of the reviewed sessions. 2-of-8 (25%)
# was being printed under "what this project should do differently", which overstates the evidence.
RECUR_MIN = 3                    # absolute floor: three independent sessions
RECUR_SHARE = 0.30               # ...and at least this share of the project's reviewed sessions


def project_lessons(rows, recur_min=RECUR_MIN, recur_share=RECUR_SHARE):
    """[{project, sessions, lessons:[{rule, kind, do, why, sessions, share, session_ids}]}] worst-first.

    `share` is carried so the UI can print a denominator: "3 of 9 sessions" is a claim a reader can
    weigh, where a bare "3x" is not.
    """
    projects = {}
    for r in rows or []:
        proj = r.get("project") or "(no project)"
        p = projects.setdefault(proj, {"project": proj, "sessions": set(), "rules": {}})
        p["sessions"].add(r.get("session_id"))
        for f in r.get("findings") or []:
            k = f.get("rule")
            if not k:
                continue
            e = p["rules"].setdefault(k, {"rule": k, "kind": f.get("kind"), "do": f.get("do"),
                                          "why": f.get("why"), "session_ids": []})
            e["session_ids"].append(r.get("session_id"))
    out = []
    for p in projects.values():
        n_rev = max(1, len(p["sessions"]))
        lessons = [dict(e, sessions=len(e["session_ids"]), reviewed=n_rev,
                        share=round(len(e["session_ids"]) / n_rev, 3), session_ids=e["session_ids"][:5])
                   for e in p["rules"].values()
                   if len(e["session_ids"]) >= recur_min
                   and len(e["session_ids"]) / n_rev >= recur_share]
        if not lessons:
            continue
        lessons.sort(key=lambda e: -e["sessions"])
        out.append({"project": p["project"], "sessions": len(p["sessions"]), "lessons": lessons})
    out.sort(key=lambda p: -sum(l["sessions"] for l in p["lessons"]))
    return out


def _dur(s):
    s = int(s or 0)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 60}m" if s >= 60 else f"{s}s"


def _selfcheck():
    def ev(ts, role=None, content=None, usage=None, model=None):
        e = {"timestamp": ts}
        if role:
            e["message"] = {"role": role, "content": content or [], "usage": usage, "model": model}
        return e

    a = new()
    scan(a, ev("2026-01-01T00:00:00Z", "user", [{"type": "text", "text": "go"}]))
    for i in range(4):                                    # the SAME Read four times = 3 repeats
        scan(a, ev(f"2026-01-01T00:0{i + 1}:00Z", "assistant",
                   [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}],
                   {"input_tokens": 10, "output_tokens": 5,
                    "cache_creation": {"ephemeral_1h_input_tokens": 1000, "ephemeral_5m_input_tokens": 0}},
                   "claude-opus-5"))
    scan(a, ev("2026-01-01T00:06:00Z", "user", [{"type": "tool_result", "is_error": True}]))
    m = finish(a)

    assert m["tool_calls"] == 4, m
    assert m["dup_calls"] == 3, m["dup_calls"]            # 4 identical calls == 3 repeats
    assert m["reread_calls"] == 2, m["reread_calls"]      # 4 reads of one file, 2 beyond the allowed 2
    assert m["top_read"] == "/a.py" and m["top_read_n"] == 4, m
    assert m["tool_errors"] == 1, m
    assert m["model"] == "claude-opus-5", m
    # 40 in + 4000 cache-write@1h + 20 out, at 5/25 per MTok: (40*5 + 4000*5*2 + 20*25)/1e6
    assert abs(m["cost_usd"] - (40 * 5 + 4000 * 5 * 2 + 20 * 25) / 1e6) < 1e-9, m["cost_usd"]
    assert m["wall_s"] == 360, m["wall_s"]
    assert m["idle_s"] == 0 and m["max_gap_s"] == 120, m  # biggest gap 00:04->00:06; all under the idle floor
    assert finish(new())["cost_usd"] is None, "no usage in the transcript is unknown cost, not $0.00"

    # A UTC timestamp must measure the same duration in every zone — the mktime trap.
    b = new()
    scan(b, ev("2026-01-01T00:00:00Z"))
    scan(b, ev("2026-01-01T02:00:00Z"))
    assert finish(b)["wall_s"] == 7200, "wall clock must not depend on the server's local zone"

    # An idle gap is time the human was away: counted as idle, excluded from active.
    c = new()
    scan(c, ev("2026-01-01T00:00:00Z"))
    scan(c, ev("2026-01-01T01:00:00Z"))
    assert finish(c)["idle_s"] == 3600 and finish(c)["max_gap_s"] == 3600

    f = dict(finish(a), n_user=1)
    fs = findings(f)
    assert "turns" in [x["kind"] for x in fs]             # dup + reread both fire
    for x in fs:                                          # every finding carries its own reasoning
        assert x["saw"] and x["why"] and x["do"] and x["rule"], x
        assert x["do"][0].isupper(), x["do"]              # `do` is an instruction, not a fragment
    assert findings({}) == [] and findings(None) == []    # a session with no metrics yields no advice
    # A row read back from SQLite has every column present but possibly NULL. `.get(k, 0)` returns
    # None for that, so an unguarded comparison raises TypeError and takes the whole page down.
    assert findings({c: None for c in
                     ("tool_calls", "cost_usd", "dup_calls", "reread_calls", "tool_errors", "top_read",
                      "cache_write_tokens", "cache_read_tokens", "wall_s", "idle_s", "n_user",
                      "in_tokens", "out_tokens", "model", "max_gap_s")}) == [], "NULL columns must not raise"

    # One response written across several transcript lines must be BILLED ONCE but must not lose a
    # tool call — the bug that made a session read $115 instead of $58.
    g = new()
    for blocks in ([{"type": "thinking"}], [{"type": "tool_use", "name": "Bash", "input": {"c": 1}}],
                   [{"type": "tool_use", "name": "Bash", "input": {"c": 2}}]):
        scan(g, {"timestamp": "2026-01-01T00:00:00Z",
                 "message": {"role": "assistant", "id": "msg_same", "model": "claude-opus-5",
                             "content": blocks, "usage": {"input_tokens": 1000, "output_tokens": 100}}})
    mg = finish(g)
    assert mg["in_tokens"] == 1000 and mg["out_tokens"] == 100, mg   # billed once, not three times
    assert mg["tool_calls"] == 2, mg["tool_calls"]                   # both tool calls still counted

    # A lesson must clear BOTH bars: >= RECUR_MIN sessions AND >= RECUR_SHARE of those reviewed.
    # 2-of-8 was being printed as "what this project should do differently", which overstates it.
    def _row(sid, *rules):
        return {"session_id": sid, "project": "p",
                "findings": [{"rule": r, "kind": "turns", "do": "Do it.", "why": "Because."} for r in rules]}
    two_of_eight = [_row("s1", "rereads"), _row("s2", "rereads")] + [_row(f"f{i}") for i in range(6)]
    assert project_lessons(two_of_eight) == [], "2 of 8 sessions (25%) is not a project pattern"
    three_of_six = [_row(f"h{i}", "rereads") for i in range(3)] + [_row(f"g{i}") for i in range(3)]
    pl = project_lessons(three_of_six)
    assert len(pl) == 1 and [l["rule"] for l in pl[0]["lessons"]] == ["rereads"], pl
    assert pl[0]["lessons"][0]["sessions"] == 3 and pl[0]["lessons"][0]["reviewed"] == 6
    assert pl[0]["lessons"][0]["share"] == 0.5, pl   # denominator travels with the lesson
    assert project_lessons([]) == []

    # The cost finding must be CONTINGENT on waste, not stapled to every large session.
    big_clean = {"cost_usd": 40.0, "tool_calls": 400, "dup_calls": 0, "reread_calls": 0, "tool_errors": 0}
    assert [x["rule"] for x in findings(big_clean)] == [], "a large but clean session earns no cost finding"
    big_wasteful = dict(big_clean, dup_calls=30)
    assert "expensive-and-wasteful" in [x["rule"] for x in findings(big_wasteful)], findings(big_wasteful)

    # A long span with substantial work either side of a gap is deliberate resumption, not drift.
    resumed = {"wall_s": 150000, "idle_s": 140000, "active": 0, "cost_usd": 30.0,
               "tool_calls": 500, "max_gap_s": 100000}
    assert "cold-resume" not in [x["rule"] for x in findings(resumed)], \
        "resuming one task the next day is correct behaviour — do not advise starting fresh"
    drifted = {"wall_s": 40000, "idle_s": 39000, "cost_usd": 0.4, "tool_calls": 6, "max_gap_s": 38000}
    assert "cold-resume" in [x["rule"] for x in findings(drifted)], findings(drifted)

    # A model with no list price is an unknown cost, never a guess and never free.
    d = new()
    scan(d, ev("2026-01-01T00:00:00Z", "assistant", [], {"input_tokens": 1_000_000}, "llama-4-maverick"))
    assert finish(d)["cost_usd"] is None, finish(d)["cost_usd"]

    # Malformed events are survivable.
    e = new()
    scan(e, {"timestamp": None, "message": "not-a-dict"})
    scan(e, {"message": {"role": "assistant", "content": [None, {"type": "tool_use"}]}})
    assert finish(e)["tool_calls"] == 1

    print("OK improve: per-response billing (split-line dedupe), token/tool/error counting, UTC-safe "
          "durations, dup+reread detection, TTL-aware cost, an unpriced model is an unknown cost, malformed events, "
          "saw/why/do on every finding; project lessons need 3 sessions AND 30% share; the cost finding is contingent on waste; cold-resume does not fire on deliberate resumption")


if __name__ == "__main__":
    _selfcheck()
