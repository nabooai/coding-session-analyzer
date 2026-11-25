"""Ported verbatim from harvest.py's __main__ self-check (deleted with the P2 migration):
pr-link min/max + create-match + merge + dirs/branches; AskUserQuestion->decisions;
idempotent write; viewer=no credit; extractor wins; convergence to empty."""
import json

from analyzer import store
from analyzer.harvest import finish, new, scan, write

T = "2026-07-06T23:22:0{}.000Z".format

CREATOR = [
    {"timestamp": T(1), "cwd": "/Users/u/Documents/Git/ledger", "gitBranch": "main", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command":
            "cd /Users/u/Documents/Git/ledger-etl3-prefect\ngh pr create --head ledgerv2/etl3-prefect --title 'x' --body \"$(cat <<'B'\ngh pr create is quoted here\nB\n)\""}}]}},
    {"type": "pr-link", "prNumber": 322, "prRepository": "acme/ledger",
     "prUrl": "https://github.com/acme/ledger/pull/322", "timestamp": T(9)},
    {"type": "pr-link", "prNumber": 322, "prRepository": "acme/ledger",
     "prUrl": "https://github.com/acme/ledger/pull/322", "timestamp": T(5)},   # out of order -> first=min
    {"type": "pr-link", "prNumber": "not-a-number", "prRepository": "x/y", "timestamp": T(5)},   # malformed -> skipped
    {"timestamp": T(3), "cwd": "/Users/u/Documents/Git/ledger", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command":
            "git worktree add ../ledger-w2 ledgerv2/other && git -C /Users/u/Documents/Git/ledger status && cd /tmp/junk"}}]}},
    {"timestamp": T(3), "cwd": "/Users/u/Documents/Git/ledger", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command":
            "git -C /Users/u/Documents/Git/ledger worktree add /Users/u/Documents/Git/ledger-etl3-scheduler -b ledgerv2/etl3-scheduler origin/main 2>&1 | tail -3"}}]}},
    {"timestamp": T(6), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "cd /Users/u/Documents/Git/ledger && gh pr merge https://github.com/acme/ledger/pull/311 --squash"}}]}},
    {"timestamp": T(4), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "gh pr merge --squash 300"}}]}},  # flag BEFORE ref
]


def _creator_result():
    evidence = new()
    for event in CREATOR:
        scan(evidence, event)
    return finish(evidence)


def test_scan_finish_evidence():
    result = _creator_result()
    assert result["prs"] == [{"repo": "acme/ledger", "n": 322, "first": T(5), "last": T(9), "count": 2,
                              "url": "https://github.com/acme/ledger/pull/322", "head": "ledgerv2/etl3-prefect",
                              "created": True}], result["prs"]
    assert result["merges"] == [300, 311] and "/tmp/junk" not in " ".join(result["dirs"]), result
    assert "/Users/u/Documents/Git/ledger-w2" in result["dirs"]
    assert "/Users/u/Documents/Git/ledger-etl3-prefect" in result["dirs"]
    assert "/Users/u/Documents/Git/ledger-etl3-scheduler" in result["dirs"]   # -C prefix + -b-after-path form
    assert {"ledgerv2/etl3-prefect", "ledgerv2/other", "ledgerv2/etl3-scheduler", "main"} <= set(result["branches"])


def test_write_idempotent_and_convergent():
    con = store.sqlite_db(":memory:")
    result = _creator_result()
    sess = dict(session_id="creator", project="ledger", git_branch="main", started_at=T(0), ended_at=T(9),
                n_user=1, n_asst=1, harvest=result)
    assert write(con, sess) and write(con, sess)         # twice -> idempotent
    assert con.execute("SELECT COUNT(*) FROM pr_links").fetchone()[0] == 1
    assert con.execute("SELECT n_events FROM pr_links WHERE pr_number=322").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM actions WHERE verb='create' AND target_id='PR-322'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM actions WHERE verb='merge' AND target_id='PR-300'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM sessions WHERE session_id='creator'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM session_workspace WHERE kind='branch'").fetchone()[0] == 4

    # a viewer session (`gh pr view` only, no pr-link events) gets NO creation credit
    viewer_evidence = new()
    scan(viewer_evidence, {"timestamp": T(2), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "gh pr view 322 --json url"}}]}})
    viewer_result = finish(viewer_evidence)
    assert not viewer_result["prs"] and not write(con, dict(session_id="viewer", harvest=viewer_result))

    # extractor data survives: pre-existing entity name/type win, attributes merge
    con.execute("UPDATE entities SET name='rich extractor name', attributes='{\"x\":1}' WHERE id='PR-322'")
    write(con, sess)
    nm, at = con.execute("SELECT name, attributes FROM entities WHERE id='PR-322'").fetchone()
    assert nm == "rich extractor name" and json.loads(at)["x"] == 1 and json.loads(at)["repo"] == "acme/ledger"

    # Re-harvesting a session with no raw evidence converges to no harvest-owned rows.
    empty = dict(sess, harvest={"prs": [], "merges": [], "dirs": [], "branches": []})
    write(con, empty)
    for table in ("pr_links", "session_workspace", "actions", "mentions", "links"):
        assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id='creator'").fetchone()[0] == 0, table


def test_ask_user_question_decisions_and_redaction():
    ask = new()
    scan(ask, {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tuA", "name": "AskUserQuestion",
        "input": {"questions": [
            {"question": "How much of this trial task should I build now?", "header": "Scope"},
            {"question": "How should I run it?", "header": "Run"},
            {"question": "Which DB?", "header": "DB"}]}}]}})   # 3 asked; only 2 answered below
    scan(ask, {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tuA", "content":
        'Your questions have been answered: "How much of this trial task should I build now?"="Full 5-workstream build"'
        ' selected preview:\nSCOPE (all), "How should I run it?"="Multi-agent workflow" selected preview:\n5 agents.'
        ' You can now continue with these answers in mind.'}]}})
    ad = finish(ask)["decisions"]
    claims = {d["claim"] for d in ad}
    assert "How much of this trial task should I build now? → Full 5-workstream build" in claims, ad
    assert "How should I run it? → Multi-agent workflow" in claims, ad
    assert not any("Which DB" in c for c in claims), "unanswered question must not become a decision"
    assert all(d["evidence"].startswith("User answered Claude:") for d in ad), ad
    # decisions are arbitrary user free-text at org scope -> must honor the session's mode+deny
    leaky = new()
    scan(leaky, {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tL", "name": "AskUserQuestion",
        "input": {"questions": [{"question": "Which host?", "header": "H"}]}}]}})
    scan(leaky, {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tL",
        "content": 'Your questions have been answered: "Which host?"="ping mara@example.com use codename Bluefin"'}]}})
    assert "mara@example.com" in finish(leaky)["decisions"][0]["claim"], "secrets mode keeps low-risk text"
    guarded = finish(leaky, mode="privacy", deny=["Bluefin"])["decisions"][0]["claim"]
    assert "mara@example.com" not in guarded and "Bluefin" not in guarded

    # a tool_result NOT tied to an AskUserQuestion id must never mint decisions
    stray = new()
    scan(stray, {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "other",
        "content": 'noise "x"="y" noise'}]}})
    assert finish(stray)["decisions"] == [], "only AskUserQuestion tool_results become decisions"
