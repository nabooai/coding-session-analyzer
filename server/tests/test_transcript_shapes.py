"""Every content shape a real Claude Code session actually contains, end to end.

The shapes and their frequencies come from measuring 180 randomly sampled sessions in a real
`~/.claude/projects` (788 files):

    tool_result 8,327 | tool_use 8,321 | thinking 5,076 | text 3,457 | bare string 679 | image 30
    events carrying isSidechain 30,890 | toolUseResult 8,223 | isMeta 712 | isCompactSummary 4
    top tools: Bash 5,773, Edit 703, Read 670, Write 235, mcp__* , Agent, ToolSearch, Skill

That distribution is the point: tool calls and their results OUTNUMBER prose two to one, so a
transcript feature that only handles `text` handles the minority of a session. Each shape below gets
a case, from the raw .jsonl through ingest -> archive -> the paged block list the UI and GraphQL both
render.

`test_real_local_sessions_all_parse` runs the same pipeline over the machine's ACTUAL sessions when
there are any, and is skipped otherwise (CI, a fresh checkout). It asserts invariants rather than
content, so nobody's transcripts are copied into this repo.
"""
import glob
import json
import os

import pytest

from analyzer import archive, ingest, pagination

SECRET = "sk-ant-api03-" + "A" * 40          # shaped like a live key, and never a real one


def ev(role, content, **extra):
    return {"type": role, "timestamp": "2026-09-01T10:00:00Z",
            "message": {"role": role, "content": content}, **extra}


def jsonl(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def parse(*events, mode="secrets"):
    """Raw events -> the session dict ingest produces (2 user messages minimum)."""
    lead = [ev("user", "start the work"), ev("assistant", "on it")]
    return ingest.load_content("shape-test", jsonl(*lead, *events), mode=mode)


def blocks_of(*events, **kw):
    """...and the flat display blocks the archive serves for it."""
    sess = parse(*events, **kw)
    assert sess, "the fixture must produce a session"
    return archive.blocks({"messages": sess["messages"]})


# --- the block types -------------------------------------------------------------------------------

def test_bare_string_content():
    """679 messages in the sample carry a plain string instead of a block list."""
    bs = blocks_of(ev("user", "just a plain string, no blocks"))
    assert any(b["type"] == "text" and "plain string" in b["text"] for b in bs)


def test_text_and_thinking():
    bs = blocks_of(ev("assistant", [{"type": "thinking", "thinking": "let me reason about this"},
                                    {"type": "text", "text": "here is the answer"}]))
    kinds = [b["type"] for b in bs if b["role"] == "assistant"]
    assert "thinking" in kinds and "text" in kinds, kinds
    th = [b for b in bs if b["type"] == "thinking"][0]
    assert th["text"] == "let me reason about this"


def test_tool_use_carries_name_and_input():
    bs = blocks_of(ev("assistant", [{"type": "tool_use", "id": "tu_1", "name": "Bash",
                                     "input": {"command": "pytest -q", "description": "run tests"}}]))
    tu = [b for b in bs if b["type"] == "tool_use"][0]
    assert tu["name"] == "Bash" and tu["tool_id"] == "tu_1"
    assert "pytest -q" in tu["input"], "the arguments are what makes a tool call readable"


@pytest.mark.parametrize("tool,inp", [
    ("Edit", {"file_path": "/x/store.py", "old_string": "a", "new_string": "b"}),
    ("Read", {"file_path": "/x/apiserver.py", "offset": 10, "limit": 40}),
    ("Write", {"file_path": "/x/new.py", "content": "print(1)"}),
    ("Agent", {"description": "review the diff", "prompt": "look for bugs"}),
    ("Skill", {"skill": "deploy-staging"}),
    ("ToolSearch", {"query": "select:Read,Edit"}),
    ("mcp__chrome-devtools__evaluate_script", {"function": "() => document.title"}),
    ("mcp__csa__ask", {"query": "what broke the lab"}),
])
def test_every_common_tool_survives(tool, inp):
    """The top tools measured in the real corpus, including MCP-namespaced ones (a `__` name must
    not be mistaken for anything special) and Agent (whose transcript is a sidechain)."""
    bs = blocks_of(ev("assistant", [{"type": "tool_use", "id": "t", "name": tool, "input": inp}]))
    tu = [b for b in bs if b["type"] == "tool_use"][0]
    assert tu["name"] == tool
    assert any(str(v)[:12] in tu["input"] for v in inp.values()), "some argument must survive"


def test_tool_result_as_string_and_as_list():
    """7,708 results are a bare string; 619 are a list of blocks. Both must read the same."""
    a = blocks_of(ev("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "277 passed"}]))
    b = blocks_of(ev("user", [{"type": "tool_result", "tool_use_id": "t1",
                               "content": [{"type": "text", "text": "277 passed"}]}]))
    for bs in (a, b):
        tr = [x for x in bs if x["type"] == "tool_result"][0]
        assert tr["text"] == "277 passed" and tr["tool_id"] == "t1"


def test_tool_result_error_is_marked():
    bs = blocks_of(ev("user", [{"type": "tool_result", "tool_use_id": "t1",
                                "content": "command not found: pytest", "is_error": True}]))
    tr = [b for b in bs if b["type"] == "tool_result"][0]
    assert tr["is_error"] is True, "a failed tool call must be visibly different from a successful one"


def test_tool_use_and_result_pair_by_id():
    """The pairing is what lets a UI fold a result under the call that produced it."""
    bs = blocks_of(ev("assistant", [{"type": "tool_use", "id": "tu_42", "name": "Bash",
                                     "input": {"command": "ls"}}]),
                   ev("user", [{"type": "tool_result", "tool_use_id": "tu_42", "content": "a.py"}]))
    use = [b for b in bs if b["type"] == "tool_use"][0]
    res = [b for b in bs if b["type"] == "tool_result"][0]
    assert use["tool_id"] == res["tool_id"] == "tu_42"


def test_image_is_noted_but_never_stored():
    """30 image blocks in the sample. The base64 is megabytes of noise; that one was there is not."""
    bs = blocks_of(ev("user", [{"type": "image",
                                "source": {"type": "base64", "media_type": "image/png",
                                           "data": "iVBORw0KGgo" + "A" * 5000}},
                               {"type": "text", "text": "what is wrong with this screenshot"}]))
    img = [b for b in bs if b["type"] == "image"]
    assert len(img) == 1 and "data" not in img[0] and "source" not in img[0]
    assert not any("iVBORw0KGgo" in json.dumps(b) for b in bs), "base64 must not reach storage"


# --- message-level flags ----------------------------------------------------------------------------

def test_sidechain_and_meta_are_marked_not_dropped():
    """isSidechain appears on 30,890 events: a subagent's work is often where the real work is.
    Marked so a UI can separate it, never dropped."""
    bs = blocks_of(ev("assistant", [{"type": "text", "text": "subagent thinking"}], isSidechain=True),
                   ev("assistant", [{"type": "text", "text": "meta note"}], isMeta=True))
    side = [b for b in bs if b.get("sidechain")]
    meta = [b for b in bs if b.get("meta")]
    assert side and side[0]["text"] == "subagent thinking"
    assert meta and meta[0]["text"] == "meta note"
    assert not side[0]["meta"] and not meta[0]["sidechain"], "the two flags are independent"


def test_blocks_know_which_message_they_came_from():
    bs = blocks_of(ev("assistant", [{"type": "thinking", "thinking": "hm"},
                                    {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}},
                                    {"type": "text", "text": "done"}]))
    tail = [b for b in bs if b["message_index"] == max(x["message_index"] for x in bs)]
    assert len(tail) == 3, "one message's three blocks share a message_index, so a UI can regroup them"
    assert len({b["role"] for b in tail}) == 1


# --- robustness: shapes that must not crash ingest ---------------------------------------------------

@pytest.mark.parametrize("content", [
    [], "", "   ", None, 42, {"not": "a list"},
    [None, 3, "loose string"],
    [{"type": "unknown_future_block", "payload": "?"}],
    [{"no_type": True}],
    [{"type": "tool_use"}],                                   # no name, no input
    [{"type": "tool_result"}],                                # no content
    [{"type": "text"}],                                       # no text
])
def test_malformed_content_never_crashes(content):
    """A shape this parser has not seen must cost that block, never the ingest. New block types ship
    in Claude Code before they ship here."""
    sess = parse(ev("assistant", content))
    assert sess is not None, "the session must still parse"
    archive.blocks({"messages": sess["messages"]})             # and must still render


def test_unserialisable_tool_input_degrades_to_text():
    bs = blocks_of(ev("assistant", [{"type": "tool_use", "id": "t", "name": "Odd",
                                     "input": "a bare string, not an object"}]))
    tu = [b for b in bs if b["type"] == "tool_use"][0]
    assert "bare string" in tu["input"]


# --- redaction: the gate that matters most, since this is long-term storage --------------------------

def test_secrets_are_stripped_from_every_block_kind():
    """The archive is the one place a secret would sit for 30 days. Every block kind that carries
    text is redacted at the same mode, before any cap."""
    bs = blocks_of(
        ev("user", f"my key is {SECRET}"),
        ev("assistant", [
            {"type": "thinking", "thinking": f"the key {SECRET} looks live"},
            {"type": "tool_use", "id": "t", "name": "Bash",
             "input": {"command": f"curl -H 'Authorization: Bearer {SECRET}' https://x"}},
            {"type": "text", "text": f"I used {SECRET}"}]),
        ev("user", [{"type": "tool_result", "tool_use_id": "t", "content": f"echoed back {SECRET}"}]))
    dumped = json.dumps(bs)
    assert SECRET not in dumped, "a live-looking key reached long-term storage"
    assert "REDACTED" in dumped


def test_truncation_is_flagged_not_silent():
    """A reader must be able to tell a short tool result from a truncated one."""
    huge = "x" * 50_000
    bs = blocks_of(ev("assistant", [{"type": "tool_use", "id": "t", "name": "Bash",
                                     "input": {"command": huge}}]),
                   ev("user", [{"type": "tool_result", "tool_use_id": "t", "content": huge}]))
    tu = [b for b in bs if b["type"] == "tool_use"][0]
    tr = [b for b in bs if b["type"] == "tool_result"][0]
    assert tu["truncated"] is True and len(tu["input"]) <= ingest._TOOL_INPUT_CAP
    assert tr["truncated"] is True and len(tr["text"]) <= ingest._TOOL_RESULT_CAP


# --- the whole path, and its pagination ---------------------------------------------------------------

def test_end_to_end_archive_and_page(tmp_path, monkeypatch):
    from analyzer import store, tenant
    monkeypatch.chdir(tmp_path)
    store.sqlite_db(store.org_db(None)).close()
    tenant.set_config(store.org_db(None), archive_days=30)
    events = []
    for i in range(12):
        events.append(ev("assistant", [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                        "input": {"command": f"echo {i}"}}]))
        events.append(ev("user", [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"{i}"}]))
    sess = parse(*events)
    sess["redaction_mode"] = "secrets"
    assert archive.put(sess) is not None
    rec = archive.get("shape-test")
    all_blocks = archive.blocks(rec)
    assert len(all_blocks) >= 24

    seen, cur, pages = [], None, 0
    while True:
        p = pagination.page(all_blocks, limit=5, cursor=cur)
        seen += p["items"]
        pages += 1
        if not p["has_more"]:
            break
        cur = p["next_cursor"]
        assert pages < 50, "pagination did not terminate"
    assert len(seen) == len(all_blocks), "a full walk must visit every block exactly once"
    assert [b["type"] for b in seen] == [b["type"] for b in all_blocks], "and preserve order"


# --- the real thing ------------------------------------------------------------------------------------

def _local_sessions(n=40):
    """The most RECENT sessions plus the BIGGEST ones, deduped.

    Sorting by path (the obvious `sorted(glob(...))[-40:]`) selects alphabetically, which on a real
    machine means forty tiny scratchpad sessions and almost no tool variety. Recent finds shapes
    that only just started appearing; largest finds the long sessions where the rare block types
    (thinking, image, sidechain) actually live."""
    paths = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
    stat = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        stat.append((st.st_mtime, st.st_size, p))
    recent = [p for _, _, p in sorted(stat, reverse=True)[:n]]
    biggest = [p for _, _, p in sorted(stat, key=lambda x: x[1], reverse=True)[:n]]
    return list(dict.fromkeys(recent + biggest))


_LOCAL = _local_sessions()


@pytest.mark.skipif(not _LOCAL, reason="no local Claude Code sessions on this machine")
def test_real_local_sessions_all_parse():
    """Run the real pipeline over this machine's ACTUAL sessions.

    Invariants only — no content is asserted and none is copied into the repo. What this catches is
    the thing fixtures cannot: a block shape, a tool name, or an encoding that exists in practice and
    that nobody thought to write a fixture for. It is skipped wherever there are no local sessions.
    """
    sample = _LOCAL                            # recent + largest: see _local_sessions
    known = {"text", "thinking", "tool_use", "tool_result", "image"}
    parsed = total = 0
    seen_types, seen_tools = set(), set()
    for path in sample:
        try:
            with open(path, errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        sess = ingest.load_content(os.path.basename(path)[:-6], raw, mode="secrets")
        if not sess:
            continue                            # too thin to be a session; a legitimate outcome
        parsed += 1
        bs = archive.blocks({"messages": sess["messages"]})
        total += len(bs)
        for b in bs:
            seen_types.add(b["type"])
            assert b["type"] in known, f"unhandled block type {b['type']!r} in {path}"
            assert b["role"] in ("user", "assistant"), b["role"]
            assert isinstance(b.get("message_index"), int)
            if b["type"] == "tool_use":
                seen_tools.add(b["name"])
                assert isinstance(b["input"], str) and len(b["input"]) <= ingest._TOOL_INPUT_CAP
            if b["type"] == "tool_result":
                assert isinstance(b["text"], str) and len(b["text"]) <= ingest._TOOL_RESULT_CAP
            if b["type"] == "image":
                assert set(b) <= {"type", "role", "message_index", "at", "sidechain", "meta"}, \
                    "an image block must carry no payload"
        # The structured mirror is allowed to be LONGER than the extractor's view: a message that is
        # only a tool result flattens to "" and never becomes a turn, but it is exactly what a
        # person reading the session wants to see. What must hold is that every message it keeps
        # actually had blocks.
        assert all(m["blocks"] for m in sess["messages"]), "an empty message was stored"
    assert parsed >= 1, "no local session parsed at all"
    assert total > 0 and seen_types, f"parsed {parsed} sessions and produced no blocks"
    print(f"\n  real sessions: {parsed}/{len(sample)} parsed, {total} blocks, "
          f"types={sorted(seen_types)}, {len(seen_tools)} distinct tools")


@pytest.mark.skipif(not _LOCAL, reason="no local Claude Code sessions on this machine")
def test_real_local_sessions_carry_no_live_secrets_after_redaction():
    """The redaction gate, against real transcripts rather than a fixture. Same patterns
    analyzer/redact.py enforces, applied to the blocks that would actually be stored."""
    import re
    live = re.compile(r"sk-ant-api03-[A-Za-z0-9_\-]{40,}|ghp_[A-Za-z0-9]{36}|"
                      r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
    checked = 0
    for path in _LOCAL:
        try:
            with open(path, errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        sess = ingest.load_content(os.path.basename(path)[:-6], raw, mode="secrets")
        if not sess:
            continue
        checked += 1
        blob = json.dumps(archive.blocks({"messages": sess["messages"]}))
        hit = live.search(blob)
        assert not hit, f"a live-looking credential survived redaction in {path}: {hit.group()[:12]}..."
    assert checked >= 1
