"""The `claude -p` isolation contract: every model call is text -> text and inherits nothing from the box.
llm.py documents why each flag exists; a change here is a change to what extraction can see."""
from analyzer import llm


def test_isolation_flags_are_pinned():
    assert llm._ISOLATION == ["--safe-mode", "--strict-mcp-config", "--tools", "", "--no-session-persistence"]


def test_claude_argv_is_pinned():
    assert llm._claude_argv("sonnet") == [
        "claude", "-p", "--model", "sonnet", "--output-format", "json",
        "--safe-mode", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
    ]


def test_claude_argv_never_reopens_a_surface():
    argv = llm._claude_argv("haiku")
    assert "--bare" not in argv
    assert "--mcp-config" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" not in argv
