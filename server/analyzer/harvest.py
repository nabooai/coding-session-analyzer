"""Gold from tool calls — deterministic evidence harvest from RAW session events (regex/field reads, NO LLM).

Scans every raw jsonl event pre-truncation (hooked into ingest._session_from_lines), collecting:
  * pr-link events — Claude Code writes them ONLY in the session that created the PR (verified 2026-07-09
    across all project dirs; viewers running `gh pr view` emit none). Earliest timestamp = creation moment.
  * Bash tool_use command strings — `gh pr create/merge`, `git worktree add`, `cd`, `git -C` — the only
    place worktree dirs and PR branches exist (session metadata cwd/gitBranch never leaves the main checkout).

scan() runs per event in ingest; finish() resolves matches + redacts; write() stores rows (called from
store.index_session next to signals.harvest, from csa.do_index, and from the `csa harvest` backfill)."""
import json, os, re

from sqlalchemy import and_, delete, exists, func, insert, literal, select

from analyzer import db, redact
from analyzer import models as m
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

_CREATE = re.compile(r"(?m)(?:^|[;&|])\s*gh pr create\b")   # anchored to line start OR after &&/;/| — the corpus
_HEAD = re.compile(r"--head[ =](\S+)")                       # chains commands ("cd x && gh pr create …") constantly
_MERGE = re.compile(r"(?m)(?:^|[;&|])\s*gh pr merge\b([^\n;&|]*)")   # arg tail; bare `gh pr merge` (current branch) skipped: number unknowable here
_PULL_N = re.compile(r"/pull/(\d+)")
# real corpus form: `git -C /path worktree add <path> -b <branch> <base>` — options AFTER the path, optional -C.
# Capture the argument tail, token-parse it (regex alone can't cover git's option-order freedom).
_WORKTREE_ADD = re.compile(r"(?m)(?:^|[;&|])\s*git\s+(?:-C\s+\S+\s+)?worktree add\s+([^\n;&|]+)")
_CD = re.compile(r"(?m)(?:^|&&|;)\s*cd\s+([^\s;&|)]+)")
_GIT_C = re.compile(r"git -C\s+(\S+)")
# ponytail: repo-dir filter = one substring; extend the list when a corpus keeps repos elsewhere
_REPO_DIR = "/Documents/Git/"


def new():
    """Empty accumulator for one session. Shape:
      pr        {(repo, pr_number): [first_seen, last_seen, event_count, url]}   from pr-link events
      creates   [(command_timestamp, head_branch_or_None)]                       from `gh pr create`
      merges    {pr_number}                                                      from `gh pr merge`
      dirs      {repo_dir_path}                                                  from cd / git -C / worktree add
      branches  {branch_name}                                                    from metadata + --head + worktree add
      asks      {tool_use_id: [question, ...]}                                    open AskUserQuestion prompts
      decisions [{claim, evidence}]                                              user's answers = DECISIONS"""
    return {"pr": {}, "creates": [], "merges": set(), "dirs": set(), "branches": set(),
            "asks": {}, "decisions": []}


def scan(acc, event):
    """One raw jsonl event -> accumulate evidence into `acc`. Cheap: regex only touches Bash commands."""
    if event.get("type") == "pr-link":                   # the jackpot: machine-labeled PR ownership
        _record_pr_link(acc, event)
        return
    if event.get("gitBranch"):
        acc["branches"].add(event["gitBranch"])          # metadata branch (free; catches mid-session switches)
    _scan_asks(acc, event)                               # AskUserQuestion tool_use + its answer -> decisions
    for command in _bash_commands(event):
        _scan_command(acc, command, event.get("cwd"), event.get("timestamp") or "")


def _scan_asks(acc, event):
    """AskUserQuestion is the single highest-signal DECISION in a session: Claude asks, the user PICKS. Both the
    tool_use (canonical question text) and the tool_result (the picked answer) are structured — harvest sees BOTH
    pre-truncation, so this never depends on the LLM or on a mid-session Q&A surviving the transcript cap.
    Claude Code's tool_result reads: Your questions have been answered: "<question>"="<label>" selected preview: …"""
    message = event.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return
    for block in message["content"]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
            questions = [(q.get("question") or "").strip()
                         for q in ((block.get("input") or {}).get("questions") or []) if isinstance(q, dict)]
            if block.get("id"):
                acc["asks"][block["id"]] = [q for q in questions if q]
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in acc["asks"]:
            text = _result_text(block.get("content"))
            for question in acc["asks"][block["tool_use_id"]]:
                answer = _answer_for(question, text)
                if answer:                               # skipped questions / format drift -> no phantom decision
                    acc["decisions"].append({"claim": f"{question} → {answer}",
                                             "evidence": f'User answered Claude: "{question}" = "{answer}".'})


def _result_text(content):
    """tool_result content is a plain string OR a list of blocks — flatten to searchable text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return ""


def _answer_for(question, text):
    """The picked label out of `"<question>"="<label>"`. Anchoring on the exact (canonical) question text avoids
    guessing the answer-string's own delimiters. ponytail: label with a literal `"` (rare 'Other' free-text)
    truncates at the quote — accept it; a full quote-aware parse isn't worth it until a real case shows up."""
    m = re.search('"' + re.escape(question) + '"="([^"]*)"', text)
    return _clean(m.group(1)) if m else None


def _record_pr_link(acc, event):
    """Track first/last timestamp + event count per (repo, pr_number)."""
    try:
        pr_number = int(event.get("prNumber"))
    except (TypeError, ValueError):                      # malformed event must not abort the whole file parse
        return
    repo, timestamp = event.get("prRepository"), event.get("timestamp")
    if not (repo and timestamp):
        return
    stats = acc["pr"].setdefault((repo, pr_number), [timestamp, timestamp, 0, event.get("prUrl")])
    stats[0] = min(stats[0], timestamp)                  # first_seen (events can arrive out of order)
    stats[1] = max(stats[1], timestamp)                  # last_seen
    stats[2] += 1                                        # event_count


def _bash_commands(event):
    """Yield the command string of every Bash tool_use block in the event's message (usually 0 or 1)."""
    message = event.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return
    for block in message["content"]:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
            yield (block.get("input") or {}).get("command") or ""


def _scan_command(acc, command, cwd, timestamp):
    """Pull PR / worktree / branch evidence out of one Bash command string.
    # ponytail: line-anchored regexes still see heredoc BODIES inside a command; harmless — a 'create'
    # only counts once a pr-link event confirms it, and junk dirs die on the _REPO_DIR filter."""
    if _CREATE.search(command):
        head_match = _HEAD.search(command)
        head_branch = _clean(head_match.group(1)) if head_match else None
        acc["creates"].append((timestamp, head_branch))
        if head_branch:
            acc["branches"].add(head_branch)
    for merge_match in _MERGE.finditer(command):
        _record_merge(acc, merge_match.group(1))
    for worktree_match in _WORKTREE_ADD.finditer(command):
        path, branch = _parse_worktree_args(worktree_match.group(1))
        if path:
            _record_dir(acc, path, cwd)
        if branch:
            acc["branches"].add(_clean(branch))
    for cd_match in _CD.finditer(command):
        _record_dir(acc, cd_match.group(1), cwd)
    for git_c_match in _GIT_C.finditer(command):
        _record_dir(acc, git_c_match.group(1), cwd)


def _record_merge(acc, arg_tail):
    """First number-or-PR-URL token after `gh pr merge` = the merged PR (flags may precede it: `--squash 311`)."""
    for token in arg_tail.split():
        token = _clean(token)
        url_match = _PULL_N.search(token)
        if token.isdigit() or url_match:
            acc["merges"].add(int(url_match.group(1) if url_match else token))
            return


def _record_dir(acc, path, cwd):
    """Record a path if it looks like a repo dir (absolute, or relative resolved against the event's cwd)."""
    path = os.path.expanduser(_clean(path))
    if not os.path.isabs(path):
        if not cwd:
            return
        path = os.path.normpath(os.path.join(cwd, path))
    if _REPO_DIR in path:
        acc["dirs"].add(path.rstrip("/"))


def _parse_worktree_args(arg_tail):
    """`git worktree add` argument tail -> (path, branch): first non-option token = path, `-b/-B <x>` or the
    following commit-ish = branch (a bare commit-ish is usually a branch name; origin/main noise is honest)."""
    tokens, path, branch = arg_tail.split(), None, None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-b", "-B") and i + 1 < len(tokens):
            branch, i = tokens[i + 1], i + 2
            continue
        if not token.startswith("-") and ">" not in token and "<" not in token:   # skip flags AND redirections (2>/dev/null)
            if path is None:
                path = token
            elif branch is None:
                branch = token
        i += 1
    return path, branch


def _clean(token):
    return token.strip("'\"`")


def finish(acc, mode="secrets", deny=(), allow=()):
    """acc -> the session's harvest dict: creates matched to PRs by time, everything redacted at the SESSION's
    effective policy (same mode/deny/allow the transcript got — ingest passes them). Decisions are arbitrary user
    free-text at organizational scope, so they MUST honor privacy/strict + the operator deny list, not just the
    'secrets' floor. Default args reproduce the old 'secrets' behavior for any caller that omits them."""
    prs = [{"repo": repo, "n": number, "first": first_seen, "last": last_seen, "count": event_count,
            "url": url, "head": None, "created": False}
           for (repo, number), (first_seen, last_seen, event_count, url) in acc["pr"].items()]
    prs.sort(key=lambda pr: pr["first"])
    # each create command claims the earliest pr-link stream at/after it (ISO strings compare chronologically)
    for command_time, head_branch in sorted(acc["creates"], key=lambda c: c[0]):   # key=timestamp only: tied
        for pr in prs:                                   # timestamps must not compare a None head against a str
            if not pr["created"] and pr["first"] >= command_time:
                pr["created"], pr["head"] = True, head_branch or pr["head"]
                break
    redact_one = lambda s: redact.redact(s, mode, deny, allow)[0] if s else s
    for pr in prs:
        pr["url"], pr["head"], pr["repo"] = redact_one(pr["url"]), redact_one(pr["head"]), redact_one(pr["repo"])
    decisions = [{"claim": redact_one(d["claim"]), "evidence": redact_one(d["evidence"])}
                 for d in acc["decisions"]]
    return {"prs": prs, "merges": sorted(acc["merges"]),
            "dirs": sorted(redact_one(d) for d in acc["dirs"]),
            "branches": sorted(redact_one(b) for b in acc["branches"] if b),
            "decisions": decisions}


def _ensure_entity(con, eid, name, attrs):
    """Upsert a harvest entity WITHOUT clobbering extractor data: existing type/name win, attributes merge."""
    ent = m.Entity.__table__
    row = db.run(con, select(ent.c.attributes).where(ent.c.id == eid)).fetchone()
    merged = {**(json.loads(row[0]) if row and row[0] else {}), **attrs}
    ins = sqlite_insert(ent).values(id=eid, type="pr", name=name, attributes=json.dumps(merged))
    db.run(con, ins.on_conflict_do_update(index_elements=[ent.c.id], set_={
        "type": func.coalesce(ent.c.type, ins.excluded.type),
        "name": func.coalesce(ent.c.name, ins.excluded.name),
        "attributes": ins.excluded.attributes}))


def _guarded(con, table, cols, vals, keycols):
    """INSERT iff no row with the same key exists — shared tables (mentions/actions/links) have no PK and are
    DELETE+INSERTed by the extractor's load_sqlite; harvest must never duplicate or wipe them.
    `table` is a Core Table; `cols`/`keycols` are column-name tuples (same shape as the raw era)."""
    by_name = dict(zip(cols, vals))
    guard = ~exists(select(literal(1)).select_from(table)
                    .where(and_(*(table.c[k] == by_name[k] for k in keycols))))
    cur = db.run(con, insert(table).from_select(
        list(cols), select(*(literal(v) for v in vals)).where(guard)))
    return cur.rowcount > 0


def _clear_harvest_rows(con, sid):
    """Remove rows this harvester owns for one session; extractor-owned rows are left alone.
    Marker tables exist because mentions/actions/links predate harvest and do not carry provenance."""
    ha, hm, hl = m.HarvestAction.__table__, m.HarvestMention.__table__, m.HarvestLink.__table__
    for verb, target_id, context in db.run(con, select(ha.c.verb, ha.c.target_id, ha.c.context)
                                           .where(ha.c.session_id == sid)).fetchall():
        db.run(con, delete(m.actions).where(m.actions.c.session_id == sid, m.actions.c.verb == verb,
                                            m.actions.c.target_id == target_id,
                                            m.actions.c.context == context))
    for entity_id, in db.run(con, select(hm.c.entity_id).where(hm.c.session_id == sid)).fetchall():
        db.run(con, delete(m.mentions).where(m.mentions.c.session_id == sid,
                                             m.mentions.c.entity_id == entity_id))
    for url, title in db.run(con, select(hl.c.url, hl.c.title).where(hl.c.session_id == sid)).fetchall():
        db.run(con, delete(m.links).where(m.links.c.session_id == sid, m.links.c.url == url,
                                          m.links.c.title == title))
    for table in (ha, hm, hl, m.PrLink.__table__, m.SessionWorkspace.__table__):
        db.run(con, delete(table).where(table.c.session_id == sid))


def write(con, sess):
    """Store one session's harvest (idempotent: own tables DELETE+INSERT, shared tables guarded).
    Safe for sessions with NO extraction output — inserts a sessions stub so FTS joins/provenance work.
    # ponytail: called in a plain loop over sessions — the writes take ~1s per 1,800 sessions and SQLite
    # serializes writers anyway; if the corpus grows 100x, parallelize the PARSE (per-file processes), not this."""
    harvested = sess.get("harvest")
    if not harvested:                                    # no harvest attached (e.g. curated memories)
        return False
    from analyzer import store                           # lazy: harvest is imported by store (cycle guard)
    sid = sess["session_id"]
    _clear_harvest_rows(con, sid)
    if not (harvested["prs"] or harvested["merges"] or harvested["dirs"] or harvested["branches"]):
        con.commit()
        return False
    db.run(con, insert(m.SessionRow.__table__).prefix_with("OR IGNORE")
           .values(session_id=sid, project=sess.get("project"), git_branch=sess.get("git_branch"),
                   started_at=sess.get("started_at"), ended_at=sess.get("ended_at"),
                   n_user=sess.get("n_user"), n_asst=sess.get("n_asst"),
                   source=sess.get("source", "session"),
                   user=sess.get("user") or store.owner_identity()["email"]))
    for pr in harvested["prs"]:
        db.run(con, insert(m.PrLink.__table__)
               .values(session_id=sid, repo=pr["repo"], pr_number=pr["n"], url=pr["url"],
                       first_seen=pr["first"], last_seen=pr["last"], n_events=pr["count"]))
        eid = f"PR-{pr['n']}"                            # matches the extractor's existing id convention
        attrs = {"repo": pr["repo"], "url": pr["url"], **({"head_branch": pr["head"]} if pr["head"] else {})}
        _ensure_entity(con, eid, f"{pr['repo']}#{pr['n']}", attrs)
        if _guarded(con, m.mentions, ("session_id", "entity_id"), (sid, eid), ("session_id", "entity_id")):
            db.run(con, insert(m.HarvestMention.__table__).prefix_with("OR IGNORE")
                   .values(session_id=sid, entity_id=eid))
        if pr["created"]:
            context = f"gh pr create (pr-link confirmed){' --head ' + pr['head'] if pr['head'] else ''}"
            if _guarded(con, m.actions, ("session_id", "verb", "target_id", "context"),
                        (sid, "create", eid, context), ("session_id", "verb", "target_id")):
                db.run(con, insert(m.HarvestAction.__table__).prefix_with("OR IGNORE")
                       .values(session_id=sid, verb="create", target_id=eid, context=context))
        if pr["url"]:
            title = f"{pr['repo']}#{pr['n']}"
            if _guarded(con, m.links, ("session_id", "url", "title"),
                        (sid, pr["url"], title), ("session_id", "url")):
                db.run(con, insert(m.HarvestLink.__table__).prefix_with("OR IGNORE")
                       .values(session_id=sid, url=pr["url"], title=title))
    for pr_number in harvested["merges"]:
        eid = f"PR-{pr_number}"
        _ensure_entity(con, eid, None, {})
        if _guarded(con, m.actions, ("session_id", "verb", "target_id", "context"),
                    (sid, "merge", eid, "gh pr merge"), ("session_id", "verb", "target_id")):
            db.run(con, insert(m.HarvestAction.__table__).prefix_with("OR IGNORE")
                   .values(session_id=sid, verb="merge", target_id=eid, context="gh pr merge"))
    for kind, values in (("dir", harvested["dirs"]), ("branch", harvested["branches"])):
        for value in values:
            db.run(con, insert(m.SessionWorkspace.__table__).prefix_with("OR IGNORE")
                   .values(session_id=sid, kind=kind, value=value))
    con.commit()
    return True
