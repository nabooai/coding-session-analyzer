"""cwd -> project name: one pure function for ingest, session detail and the ask-side same-project
boost, so both sides of the boost agree. Explicit, ordered rules, no fuzzy matching; anything unmatched
passes through byte-identical.
"""
import posixpath
import re

_WORKTREE_MARKER = "/.claude/worktrees/"
# A Windows path. Only one gets `\`->`/`: a posix dir may legally be named `my\repo`.
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
# A home directory by shape alone (the server never sees the client's disk), after `\`->`/`.
_HOME_ROOT = re.compile(r"^(?:[A-Za-z]:)?/(?:Users|home)/[^/]+$|^(?:[A-Za-z]:)?/root$", re.I)


def _scaffold_base(parts):
    """Is the last path component worktree scaffolding rather than a chosen name: `.claude`, or
    `worktrees` directly under `.claude` (a repo named `worktrees` keeps its name)."""
    base = parts[-1]
    if base in ("", "."):
        return True
    if base == ".claude":
        return True
    return base == "worktrees" and len(parts) >= 2 and parts[-2] == ".claude"


def normalize_project(cwd):
    """Derive the project name from a session's working directory.

    Rule table (first match wins):

    1. no cwd (None/""/whitespace)                    -> "?"
    2. `<...>/<repo>/.claude/worktrees/<anything>[/...]` -> `<repo>`: every worktree and its subdirs.
    3. the same, but `<repo>` is a home root (`/Users/<x>`, `/home/<x>`, `/root`) -> the worktree name:
       `~/.claude/worktrees/` belongs to no repo, and a username is not a project.
    4. anything else                                  -> `basename(cwd)`, except a scaffolding
       basename (`.claude`, or `worktrees` directly under `.claude`) -> its nearest non-scaffold ancestor.

    Never returns "": an empty project would match every other empty project in the boost.

    Deliberately not rules: a repo subdir (`<repo>/server` -> "server") is not resolved to its repo,
    since that needs corpus state and this function must stay pure; container dirs (`~/Documents/Git`)
    and an `agent-<hex>` without the marker pass through. The cwd is the session's last event's.

    >>> normalize_project("/Users/x/Git/ledger")
    'ledger'
    >>> normalize_project("/Users/x/Git/ledger/.claude/worktrees/agent-ab12cd34ef56ab78c")
    'ledger'
    >>> normalize_project(None)
    '?'
    """
    if not cwd or not str(cwd).strip():
        return "?"                                     # rule 1
    p = str(cwd).strip()
    if _WINDOWS_PATH.match(p):
        p = p.replace("\\", "/")
    p = p.rstrip("/") or "/"                           # trailing slash must not empty the basename

    marker = p.find(_WORKTREE_MARKER)                  # rule 2, on the FIRST marker: a nested worktree
    if marker != -1:                                   # belongs to the outer repo
        prefix = p[:marker]
        repo = posixpath.basename(prefix)
        if repo and not _scaffold_base(prefix.split("/")) and not _HOME_ROOT.match(prefix):
            return repo
        wt = p[marker + len(_WORKTREE_MARKER):].split("/")[0]   # rule 3: read after the marker, so a
        if wt:                                         # subdir under it keeps the worktree name
            return wt

    parts = p.split("/")                               # rule 4
    if _scaffold_base(parts):
        for i in range(len(parts) - 1, 0, -1):         # the same parent-aware test on the way up
            if parts[i - 1] and not _scaffold_base(parts[:i]):
                return parts[i - 1]
        return "?"                                     # scaffolding all the way up
    return parts[-1] or "?"                            # basename("/") is "" -> "?", never empty


def selfcheck():
    n = normalize_project
    # rule 2: the incident — an agent worktree attributes to its repo, not `agent-<hex>`
    assert n("/Users/nb/Documents/Git/claude-session-analyzer/.claude/worktrees/agent-ab12cd34ef56ab78c") == \
        "claude-session-analyzer"
    assert n("/repo/.claude/worktrees/provenance-normalization") == "repo"      # named worktree, not agent-*
    assert n("/a/b/ledger/.claude/worktrees/agent-deadbeef12/server/analyzer") == "ledger"   # trailing subdirs
    assert n("/a/b/ledger/.claude/worktrees/x/.claude/worktrees/y") == "ledger"     # nested -> outermost real repo
    # rule 3: ~/.claude/worktrees is the user's GLOBAL claude dir -> a username is never the project; the
    # worktree NAME is, and a subdir under it must not lose that name (it did — caught by a user sim).
    assert n("/Users/mara/.claude/worktrees/side-project") == "side-project"
    assert n("/Users/mara/.claude/worktrees/side-project/server") == "side-project"
    assert n("/home/ubuntu/.claude/worktrees/some-wt") == "some-wt"             # lab box runs from home
    assert n("/root/.claude/worktrees/some-wt/deep/dir") == "some-wt"           # container/CI runs as root
    assert n("C:\\Users\\nb\\.claude\\worktrees\\wt") == "wt"                   # windows home — NOT the username
    assert n("/Users/nb/Documents/Git/.claude/worktrees/wt") == "Git"           # NOT home-shaped -> rule 2 applies
    assert n("/home/x/y/.claude/worktrees/wt") == "y"                           # a repo under a home dir: rule 2
    # rule 1 / never-empty
    assert n(None) == "?" and n("") == "?" and n("   ") == "?"
    assert n("/") == "?" and n("//") == "?"                                     # basename("/") is "" -> "?"
    # rule 2 is case-sensitive on the hex but doesn't care — the MARKER is the signal, not the dir name
    assert n("/a/b/ledger/.claude/worktrees/agent-AB12CD34EF") == "ledger"          # uppercase hex, still fine
    # rule 4: scaffolding is never a project name, even as a bare cwd
    assert n("/Users/nb/Git/ledger/.claude/worktrees") == "ledger"
    assert n("/Users/nb/Git/ledger/.claude") == "ledger"
    # ...but a repo legitimately NAMED `worktrees` keeps its name — the walk-up only fires under `.claude`.
    # Same principle as the agent-<hex> non-rule: applying it on the bare name corrupted a good repo to `Git`.
    assert n("/Users/nb/Documents/Git/worktrees") == "worktrees"
    assert n("/Users/nb/Documents/Git/worktrees/.claude/worktrees/x") == "worktrees"
    # ...including through the rule-4 WALK-UP, which is the only path that actually exercises the loop. A cold
    # reviewer caught this: the two cases above take rule 4's non-scaffold base and rule 2's marker, so they
    # both missed a walk-up that used the flat name set and skipped straight past the repo to `Git`.
    assert n("/Users/nb/Documents/Git/worktrees/.claude") == "worktrees"
    assert n("/Users/nb/Documents/Git/worktrees/.claude/worktrees") == "worktrees"
    assert n("/Users/nb/Documents/Git/ledger/.claude/worktrees") == "ledger"         # control: normal repo
    assert n("/.claude") == "?" and n("/.claude/worktrees") == "?"               # scaffolding all the way up
    # rule 3: pass-through is byte-identical — the old basename() behavior for every normal path
    assert n("/Users/nb/Documents/Git/ledger") == "ledger"
    assert n("/Users/nb/Documents/Git/acme-ai") == "acme-ai"                  # the incident's OTHER half:
    #   correct cwd, code lives in ledger/ — no path rule can fix that; it needs a per-fact code-home.
    assert n("/Users/nb/Git/ledger/") == "ledger"                                   # trailing slash
    assert n("C:\\Users\\nb\\Git\\ledger") == "ledger"                              # windows client
    assert n("C:\\Users\\nb\\Git\\ledger\\.claude\\worktrees\\agent-ab12cd34ef") == "ledger"
    # NEGATIVE: a repo named agent-<hex> keeps its name — no walk-up, no container-dir corruption.
    for name in ("agent-deadbeef1234", "agent-smith", "agent-1234", "agent-zzzzzzzzzz"):
        assert n(f"/Users/nb/Git/{name}") == name, name
    # NEGATIVE: a posix dir with a literal backslash is a NAME, not a windows path -> byte-identical
    assert n("/Users/nb/Git/my\\repo") == "my\\repo"
    # NEGATIVE: no rule ever yields empty
    for p in (None, "", "/", "//", "   ", "/a/b/c", "\\", "C:\\", "/.claude/worktrees/", "/.claude"):
        assert n(p), f"empty project for {p!r}"
    # NEGATIVE: the whole point — pass-through must be byte-identical to os.path.basename for normal paths
    import os.path
    for p in ("/a/b/c", "/Users/nb/Git/ledger", "/x/y/acme-ai", "/Users/nb/Git/agent-deadbeef1234"):
        assert n(p) == os.path.basename(p), p
    print("project selfcheck OK — worktree->repo, home/scaffold guards, pass-through byte-identical, never empty")


if __name__ == "__main__":
    selfcheck()
