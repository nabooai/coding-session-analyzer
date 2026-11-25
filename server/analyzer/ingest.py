"""jsonl session files -> clean per-session transcripts."""
import os, re, json, glob
from analyzer import redact, harvest, harness, project, improve

_ID_URLS = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/(?:(?:pull|issues)/\d+|commit/[0-9a-fA-F]{7,40})")

ROOT = os.path.expanduser("~/.claude/projects")

# ponytail: extend as needed — first-user-message prefixes that mark a session as the miner's OWN `claude -p`
# call (extraction, judging, ...), not team work. One list, checked at the ingest front door.
INTERNAL_PROMPTS = ("You analyze a Claude Code session transcript",
                    "You review facts mined from Claude Code sessions")


def first_user_text(transcript):
    """First user turn's opening line — transcript turns are 'USER: ...' / 'ASSISTANT: ...' (_session_from_lines)."""
    for line in (transcript or "").splitlines():
        if line.startswith("USER: "):
            return line[6:].strip()
    return ""


def is_self_session(sess):
    """True when the session IS one of our own internal prompts (self-mining loop guard)."""
    return first_user_text(sess.get("transcript")).startswith(INTERNAL_PROMPTS)


def _text_from_content(content, mode="secrets", deny=(), allow=()):
    """Flatten a message's content (str or list of blocks) to plain text, REDACTING BEFORE ANY CAP.

    Order is load-bearing. Every level-0 pattern is anchored on a length floor ({20,}, {35}, {36}, {86})
    or a terminator (BEGIN...END). A cap applied FIRST destroys that anchor, so the fragment is stored raw
    AND no pattern can see it afterwards -- redaction fails silently and a corpus scan reports 0.
    Measured: a 2048-bit PEM written via a `Bash` heredoc lost its -----END----- to the 1500-char cap and
    put 1421 contiguous bytes of key material into hidden_context/docs/the DB file, with the scan clean.
    The same class of bug is why github_token was lowered to {4,} (see redact.py) -- that fixed one
    pattern's floor; this fixes the cause for all of them. The caller re-redacts the joined line, which is
    idempotent, so double-redacting costs nothing but CPU.
    """
    r = lambda s: redact.redact(s, mode, deny, allow)[0] if s else s   # redact, THEN the caller may cap
    if isinstance(content, str):
        return content                              # uncapped -> the caller's redact() already covers it
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append(b.get("text", ""))           # uncapped -> caller redacts
        elif t == "thinking":                       # reasoning = hidden-context gold
            out.append("[thinking] " + r(b.get("thinking", ""))[:2000])
        elif t == "tool_use":                       # what was actually done. Bash keeps more: the 300-char cap
            cap = 1500 if b.get("name") == "Bash" else 300   # beheaded `gh pr create --title/--head` evidence
            out.append(f"[tool:{b.get('name')}] " + r(json.dumps(b.get("input", {})))[:cap])
        elif t == "tool_result":                    # skipped as noise — EXCEPT recall output, which carries
            txt = _tool_result_text(b)              # the fact ids the usage-signal harvester needs
            if "# Recalled context for" in txt:
                out.append(r(txt)[:8000])
            else:                                   # ... and identifier URLs (`gh pr create` stdout = the PR's
                urls = _ID_URLS.findall(txt)        # ground-truth URL) — keep JUST those, drop the rest as noise
                if urls:
                    out.append("[tool-result urls] " + r(" ".join(dict.fromkeys(urls)))[:500])
        # skip images / attachments — noise + base64
    return "\n".join(x for x in out if x)


# --- structured blocks (the retained transcript) --------------------------------------------------
# `_text_from_content` above flattens a message to ONE string for the extractor, and that is all the
# extractor ever needed. A person reading the session needs the structure back: which tool ran, with
# what input, and what came back. Measured over 180 real local sessions, tool_use (8,321) and
# tool_result (8,327) OUTNUMBER text (3,457) — so a transcript view built from the flattened string
# is showing the minority of what happened and dropping the majority.
#
# These blocks are what the ARCHIVE stores. They are redacted per block by the same `r()` the
# flattener uses, at the same mode, before anything is capped — the ordering the module docstring
# above explains at length, and for the same reason.
_TOOL_INPUT_CAP = 2000       # a tool's arguments: enough for a real Bash line or an Edit's old/new
_TOOL_RESULT_CAP = 4000      # its output: file contents and command stdout are unbounded otherwise
_THINKING_CAP = 4000


def _blocks_from_content(content, mode="secrets", deny=(), allow=()):
    """One message's content -> a list of structured, redacted blocks for display.

    Never raises on a shape it does not know: an unrecognised block becomes nothing at all rather
    than a crash in the ingest path. Images are RECORDED but never stored — a base64 payload is
    megabytes of noise, and the fact that a screenshot was there is the part worth keeping.
    """
    r = lambda x: redact.redact(x, mode, deny, allow)[0] if x else x
    if isinstance(content, str):
        t = r(content)
        return [{"type": "text", "text": t}] if t.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = r(b.get("text", "") or "")
            if txt.strip():
                out.append({"type": "text", "text": txt})
        elif t == "thinking":
            txt = r(b.get("thinking", "") or "")[:_THINKING_CAP]
            if txt.strip():
                out.append({"type": "thinking", "text": txt})
        elif t == "tool_use":
            raw = b.get("input", {})
            try:
                shown = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
            except (TypeError, ValueError):
                shown = str(raw)
            out.append({"type": "tool_use", "name": str(b.get("name") or "tool"),
                        "tool_id": str(b.get("id") or ""),
                        "input": r(shown)[:_TOOL_INPUT_CAP],
                        "truncated": len(shown) > _TOOL_INPUT_CAP})
        elif t == "tool_result":
            txt = _tool_result_text(b) or ""
            out.append({"type": "tool_result", "tool_id": str(b.get("tool_use_id") or ""),
                        "text": r(txt)[:_TOOL_RESULT_CAP],
                        "is_error": bool(b.get("is_error")),
                        "truncated": len(txt) > _TOOL_RESULT_CAP})
        elif t == "image":
            out.append({"type": "image"})          # noted, never stored: base64 is noise, not content
    return out


def _tool_result_text(block):
    """Flatten a tool_result block's content (str or [{type:text,...}]) to plain text."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _pinned_identifier_urls(turns):
    """Whitelisted tool_result identifiers must survive the transcript cap; they are low-volume gold."""
    urls = []
    for turn in turns:
        for url in _ID_URLS.findall(turn):
            if url not in urls:
                urls.append(url)
    return "\n".join(f"[pinned tool-result url] {url}" for url in urls)


def _session_from_lines(sid, lines, path, min_user_msgs, max_chars, mode="secrets", deny=(), allow=()):
    """Parse .jsonl lines (a file handle OR a client-sent string's lines) into a session dict, or None if too thin.
    OSError during file iteration is caught by the caller (_session_from_file); a string never raises it.
    harness.parse sniffs which harness wrote the file (claude-code, pi, ...) and normalizes every raw
    event to the canonical claude-code shape — everything below is harness-agnostic.
    `mode`/`deny`/`allow` drive the write-time redaction gate (see analyzer.redact)."""
    cwd = branch = None
    ts, turns, n_user, n_asst = [], [], 0, 0
    messages = []                                  # structured mirror of `turns` — see _blocks_from_content
    acc = harvest.new()                            # gold-from-tool-calls: deterministic evidence, PRE-truncation
    eff = improve.new()                            # per-session cost/turn/time counters — same single walk
    hname, events = harness.parse(lines)
    for ev in events:
        harvest.scan(acc, ev)                      # BEFORE the message check: pr-link events have no `message`
        improve.scan(eff, ev)                      # ditto: usage/tool_use live on events we skip below
        cwd = ev.get("cwd", cwd)
        branch = ev.get("gitBranch", branch)
        if ev.get("timestamp"):
            ts.append(ev["timestamp"])
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        # The structured mirror is collected FIRST, and on its own terms. `_text_from_content` drops
        # tool_result blocks as extractor noise, so a message carrying ONLY a tool result flattens to
        # "" and is skipped below — and that is the single most common message shape there is (8,327
        # of them in a 180-session sample, more than `text`). Collecting after the skip meant the
        # transcript stored the conversation with every command's OUTPUT missing.
        #
        # `messages` may therefore be LONGER than `turns`. That is correct and deliberate: the
        # extractor's view of a session and a human's are different, and this is the difference.
        _blocks = _blocks_from_content(msg.get("content"), mode, deny, allow)
        if _blocks:
            messages.append({"role": role, "at": ev.get("timestamp") or "", "blocks": _blocks,
                             # A subagent's transcript is interleaved with the main one. Marked, not
                             # dropped: it is often where the actual work happened.
                             "sidechain": bool(ev.get("isSidechain")),
                             "meta": bool(ev.get("isMeta"))})
        txt = _text_from_content(msg.get("content"), mode, deny, allow)   # redacts per-block BEFORE capping
        if not txt.strip():
            continue
        n_user += role == "user"
        n_asst += role == "assistant"
        turns.append(redact.redact(f"{role.upper()}: {txt}", mode, deny, allow)[0])   # redact PER TURN, mode-aware: `turns` is written to disk
        #   (pending queue) + sliced for incremental extraction, so it must be secret-clean, not just the join.
    if n_user < min_user_msgs:
        return None
    # `turns` = full redacted per-message list (len == n_user+n_asst); the incremental watermark slices it.
    transcript = "\n\n".join(turns)                # already redacted; a secret can't span the "\n\n" join boundary
    pinned = _pinned_identifier_urls(turns)
    if len(transcript) > max_chars:                # ponytail: head+tail truncation; chunk if sessions get huge
        half = max_chars // 2
        transcript = transcript[:half] + "\n\n...[TRUNCATED]...\n\n" + transcript[-half:]
        if pinned and pinned not in transcript:
            transcript += "\n\n" + pinned
    transcript, _ = redact.redact(transcript, mode, deny, allow)   # write-time gate; turns already per-turn mode-redacted, this belt-and-suspenders the join
    return {"session_id": sid, "path": path, "harness": hname, "redaction_mode": mode,
            "project": project.normalize_project(cwd), "cwd": cwd, "git_branch": branch,
            "started_at": min(ts) if ts else None, "ended_at": max(ts) if ts else None,
            "n_user": n_user, "n_asst": n_asst, "transcript": transcript, "turns": turns,
            "messages": messages,
            "harvest": harvest.finish(acc, mode, deny, allow),   # decisions honor the session's mode+deny, not just 'secrets'
            "metrics": improve.finish(eff)}      # counted here because this is the only place raw usage/tool_use exists:
    #   the queue stores the PARSED session, and the corpus never keeps the transcript, so a later pass has nothing to count


def cwd_from_file(f):
    """The cwd `_session_from_lines` would attribute this file to, without the redaction/harvest cost — LAST
    event wins, exactly like the `cwd = ev.get("cwd", cwd)` fold above (a session that cd's ends up attributed
    to where it finished). The project backfill re-derives cwd through THIS, so it can never disagree with
    ingest. None if the file has no cwd at all."""
    cwd = None
    try:
        with open(f) as fh:
            _, events = harness.parse(fh)
            for ev in events:
                cwd = ev.get("cwd", cwd)
    except OSError:                                # rotated/removed mid-scan -> caller treats as unknown
        return None
    return cwd


def _session_from_file(f, min_user_msgs=2, max_chars=60000, mode="secrets", deny=(), allow=()):
    """Parse one .jsonl file into a session dict, or None if too thin / rotated mid-scan."""
    sid = os.path.splitext(os.path.basename(f))[0]
    try:
        with open(f) as fh:
            return _session_from_lines(sid, fh, f, min_user_msgs, max_chars, mode, deny, allow)
    except OSError:                                # file rotated/removed mid-scan (Claude Code writes live) -> skip
        return None


def load_content(session_id, raw, min_user_msgs=1, max_chars=60000, mode="secrets", deny=(), allow=()):
    """Parse raw .jsonl TEXT a remote client POSTs (cross-PC ingest) — same parse + redaction gate as a local file.
    min_user_msgs=1 so a thin but real target isn't dropped (mirrors load_one). apiserver passes the effective
    mode (max of the client's declared mode and the server minimum) + the operator deny/allow backstop."""
    return _session_from_lines(session_id, raw.splitlines(), None, min_user_msgs, max_chars, mode, deny, allow)


def _local_policy():
    """Redaction floor for LOCAL-disk ingest = the DEFAULT org's tenant policy (this is the self-host
    corpus), falling back to the operator env floor exactly like remote POSTs."""
    from analyzer import store, tenant                   # late: ingest is also imported standalone
    return tenant.redaction_policy(db_path=store.org_db())


def load_sessions(limit=None, min_user_msgs=2, max_chars=60000):
    """Return most-recent-first list of session dicts with a flattened transcript. Local-disk ingest is gated at
    the tenant/server policy minimum (tenant_config min_redaction + deny/allow), same floor as remote POSTs."""
    files = sorted(glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True),
                   key=os.path.getmtime, reverse=True)
    mode, deny, allow = _local_policy()
    out = []
    for f in files:
        s = _session_from_file(f, min_user_msgs, max_chars, mode, deny, allow)
        if s:
            out.append(s)
        if limit and len(out) >= limit:
            break
    return out


def load_one(session_id, min_user_msgs=1, max_chars=60000):
    """Load a single session by id (for per-job ETL). min_user_msgs=1 so a thin target isn't dropped.
    Gated at the server policy minimum, like load_sessions."""
    hits = glob.glob(os.path.join(ROOT, "**", f"{session_id}.jsonl"), recursive=True)
    if not hits:
        return None
    mode, deny, allow = _local_policy()
    return _session_from_file(hits[0], min_user_msgs, max_chars, mode, deny, allow)


def grep_sessions(terms, exclude_ids=(), k=3, min_terms=None):
    """Rank un-extracted sessions by how many of `terms` appear in their RAW transcript; recency breaks ties
    (load_sessions is mtime-desc). Powers lazy on-demand extraction: pick the few sessions worth the LLM for a
    cold query. No LLM, stdlib only. AND-ish bar (need ALL terms, or >=2 when there are >=2) so a vague one-word
    query selects ~nothing rather than k random sessions."""
    terms = [t for t in {t.lower() for t in terms} if t]
    if not terms:
        return []
    need = min_terms if min_terms is not None else (2 if len(terms) >= 2 else 1)
    exclude = set(exclude_ids)
    scored = []
    for s in load_sessions(limit=None):
        if s["session_id"] in exclude:
            continue
        tl = s["transcript"].lower()
        cov = sum(1 for t in terms if t in tl)
        if cov >= need:
            scored.append((cov, s))
    scored.sort(key=lambda cs: -cs[0])               # stable -> ties keep the recency order
    return [s for _, s in scored[:k]]


if __name__ == "__main__":
    s = load_sessions(limit=3)
    assert s and s[0]["transcript"] and s[0]["n_user"] >= 2, "ingest produced no usable sessions"
    one = load_one(s[0]["session_id"])
    assert one and one["session_id"] == s[0]["session_id"], "load_one failed"
    assert isinstance(s[0].get("harvest"), dict) and "prs" in s[0]["harvest"], "harvest missing from parsed session"
    urls = _text_from_content([{"type": "tool_result", "content":
        "noise\nhttps://github.com/o/r/pull/1\nhttps://github.com/o/r/issues/2\n"
        "https://github.com/o/r/commit/abcdef1234567890"}])
    assert "pull/1" in urls and "issues/2" in urls and "commit/abcdef1234567890" in urls, urls
    assert "noise" not in urls, urls
    long_raw = "\n".join([
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "user", "content": "start"}}),
        json.dumps({"timestamp": "2026-01-01T00:00:01Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "A" * 70000}]}}),
        json.dumps({"timestamp": "2026-01-01T00:00:02Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "assistant", "content": [{"type": "tool_result",
                                "content": "created https://github.com/o/r/pull/42"}]}}),
        json.dumps({"timestamp": "2026-01-01T00:00:03Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "Z" * 70000}]}}),
    ])
    assert "https://github.com/o/r/pull/42" in load_content("long", long_raw)["transcript"]
    # `turns`: carried, redacted per-message, and len == n_user+n_asst (the incremental watermark invariant)
    secret_raw = "\n".join([
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "user", "content": "use key sk-abcdEFGH1234567890ijklmnop please"}}),
        json.dumps({"timestamp": "2026-01-01T00:00:01Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "done, deployed"}]}}),
    ])
    sc = load_content("secret", secret_raw)
    assert sc["turns"] and len(sc["turns"]) == sc["n_user"] + sc["n_asst"], "turns len must equal message count"
    assert not any("sk-abcdEFGH" in t for t in sc["turns"]), "turns must be redacted before they touch disk"
    assert "sk-abcdEFGH" not in sc["transcript"], "transcript must stay redacted"
    # REDACT BEFORE CAP. _text_from_content caps thinking/tool_use blocks; capping FIRST beheads the
    # anchor every pattern needs (a PEM loses -----END-----, an sk- key drops under {20,}), so the
    # fragment stored raw is invisible to a PATTERNS scan too — redaction fails silently, scan says 0.
    # Measured before the fix: a 2048-bit PEM in a `Bash` heredoc left 1421 contiguous bytes of key
    # material in hidden_context/docs/the DB file. Non-repeating body so no window matches by accident.
    import hashlib as _h
    _b, _d = "", _h.sha256(b"seed").hexdigest()
    while len(_b) < 1700:
        _d = _h.sha256(_d.encode()).hexdigest()
        _b += _d
    _pem = "-----BEGIN RSA PRIVATE KEY-----\n" + _b[:1700] + "\n-----END RSA PRIVATE KEY-----"
    for _tool in ("Bash", "Write"):                       # 1500-char and 300-char caps: the two real paths
        _t = _text_from_content([{"type": "tool_use", "name": _tool,
                                  "input": {"command": f"cat > id_rsa <<EOF\n{_pem}\nEOF"}}])
        assert "BEGIN RSA PRIVATE KEY" not in _t, f"{_tool}: PEM survived the cap — redaction ran AFTER truncation"
        assert _b[:200] not in _t, f"{_tool}: raw key material survived the cap"
    _t = _text_from_content([{"type": "thinking", "thinking": "x" * 1990 + " sk-abcdEFGH1234567890ijklmnop"}])
    assert "sk-abcdEFGH" not in _t, "thinking: key straddling the 2000-char cap survived"
    # PROVENANCE: a session run inside an agent worktree attributes to the REPO, not the worktree dir. Modelled
    # on the real incident fixture (session agent-ab12cd34ef56ab78c, cwd .../ledger/.claude/worktrees/agent-ab12...,
    # stored as project `agent-ab12cd34ef56ab78c` on master). Synthetic so it runs anywhere, not just the box
    # that has that transcript. cwd is the LAST event's — this session cd's into a subdir, like the real one.
    wt_raw = "\n".join([
        json.dumps({"timestamp": "2026-07-16T00:00:00Z", "cwd": "/Users/u/Documents/Git/ledger",
                    "message": {"role": "user", "content": "check the etl3 prefect deploy"}}),
        json.dumps({"timestamp": "2026-07-16T00:00:01Z",
                    "cwd": "/Users/u/Documents/Git/ledger/.claude/worktrees/agent-ab12cd34ef56ab78c/server",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "looking"}]}}),
    ])
    wt = load_content("agent-ab12cd34ef56ab78c", wt_raw)
    assert wt["project"] == "ledger", wt["project"]                 # master stores 'server' here -> this FAILS pre-fix
    assert wt["session_id"] == "agent-ab12cd34ef56ab78c", "session_id is untouched — recall links key on it"
    lc = load_content(one["session_id"], open(one["path"]).read())   # what a remote client POSTs (cross-PC)
    assert lc and lc["session_id"] == one["session_id"] and lc["transcript"], "load_content parses raw text"
    assert lc["harness"] == "claude-code", lc["harness"]
    pi_raw = "\n".join([                             # a pi (Slack relay bot) session through the same door
        json.dumps({"type": "session", "version": 3, "timestamp": "2026-07-12T11:00:00Z", "cwd": "/var/relaybot/app"}),
        json.dumps({"type": "message", "timestamp": "2026-07-12T11:00:01Z", "message": {"role": "user", "content": [
            {"type": "text", "text": "# Session metadata\nLocation: #production-acme (C0ABCD1234E)\nfix the bug"}]}}),
        json.dumps({"type": "message", "timestamp": "2026-07-12T11:00:02Z", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "gh pr create --head fix-bug"}}]}}),
        json.dumps({"type": "message", "timestamp": "2026-07-12T11:00:03Z", "message": {"role": "toolResult", "toolName": "bash",
            "content": [{"type": "text", "text": "https://github.com/o/r/pull/7"}]}}),
    ])
    ps = load_content("session_pi_check", pi_raw)
    assert ps["harness"] == "pi" and ps["project"] == "#production-acme", (ps["harness"], ps["project"])
    assert "[tool:Bash]" in ps["transcript"] and "gh pr create" in ps["transcript"], "pi tool calls must reach the transcript"
    assert "pull/7" in ps["transcript"], "pi toolResult identifier URLs must survive"
    assert "fix-bug" in ps["harvest"]["branches"], f'pi Bash evidence must reach harvest: {ps["harvest"]}'
    g = grep_sessions(["claude"], k=2)               # 'claude' appears in every Claude Code transcript
    assert all("claude" in x["transcript"].lower() for x in g), "grep_sessions returned a non-match"
    import uuid                                      # runtime-random term: a hardcoded one ends up IN the
    assert grep_sessions([uuid.uuid4().hex]) == [], "grep_sessions should find nothing for a nonsense term"  # corpus (this repo's own sessions) and self-matches
    assert is_self_session({"transcript": "USER: You analyze a Claude Code session transcript and extract..."})
    assert not is_self_session({"transcript": "USER: fix the login bug\n\nASSISTANT: ok"})
    # strict policy through load_content: deny codename + privacy internal-hostname are redacted at write time
    strict_raw = "\n".join([
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "user", "content": "deploy Bluefin to db01.internal"}}),
        json.dumps({"timestamp": "2026-01-01T00:00:01Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "assistant", "content": [{"type": "text",
                                "text": "shipped codename Bluefin on db01.internal; Bluefin looks good"}]}}),
    ])
    sc = load_content("strict-check", strict_raw, mode="strict", deny=["Bluefin"])
    assert sc["redaction_mode"] == "strict", sc.get("redaction_mode")
    assert "Bluefin" not in sc["transcript"] and "[REDACTED:denylisted]" in sc["transcript"], sc["transcript"]
    assert "db01.internal" not in sc["transcript"] and "[REDACTED:hostname]" in sc["transcript"], sc["transcript"]
    assert not any("Bluefin" in t or "db01.internal" in t for t in sc["turns"]), \
        "turns must honor the session mode too — they hit disk (pending queue) + feed incremental slicing"
    sc0 = load_content("strict-check", strict_raw)                   # default secrets mode keeps both
    assert "Bluefin" in sc0["transcript"] and "db01.internal" in sc0["transcript"], sc0["transcript"]
    print(f"OK ingest: {len(s)} sessions; load_one + load_content OK; grep_sessions OK; strict deny+hostname gated; first={s[0]['project']} chars={len(s[0]['transcript'])}")
