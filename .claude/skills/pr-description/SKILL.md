---
name: pr-description
description: Generate and iterate on PR descriptions. Fetches diff, reads files, asks clarifying questions, then writes and updates the PR description on GitHub.
---

# PR Description Skill

Generate comprehensive PR descriptions through an interactive process: gather context, ask clarifying questions, write the description, iterate with the author, and update the PR on GitHub.

---

## Trigger

Use when the user asks to:

- Write/generate a PR description
- Review a PR for description purposes
- Update a PR description
- `/pr-description` or `/pr-description <number>` or `/pr-description <github-url>`

---

## Process

### Phase 1: Gather Context

**Do NOT assume the local checkout matches the PR branch.** Use a
git worktree to check out the PR code into a separate directory
so you can read files with `Read`, `Grep`, and `Glob` tools
without switching branches or affecting the working directory.

Run these in parallel where possible to minimize round-trips:

1. **Determine the PR, fetch the ref, and create a worktree.** If
   PR number not provided, use the current branch:

   ```bash
   gh pr view --json number,title,body,url,baseRefName,headRefName,headRefOid
   ```

   If no PR exists yet, tell the user and stop.

   **Save the `headRefOid` value** — you will need it later for
   the freshness check in Phase 6.

   ```bash
   # Fetch the PR head into a local ref
   git fetch origin pull/<number>/head:pr-<number>

   # Create a worktree checkout (does NOT affect the main working dir)
   # Remove stale worktree first if it exists from a previous run
   git worktree remove /tmp/pr-review-<number> 2>/dev/null; git worktree add /tmp/pr-review-<number> pr-<number>
   ```

   The worktree at `/tmp/pr-review-<number>/` is now a full
   checkout of the PR code. Use `Read`, `Grep`, and `Glob` tools
   on paths under `/tmp/pr-review-<number>/` to read files.

2. **Get file list first, then full diff:**

   ```bash
   # File list first — to understand scope and triage which files matter
   gh pr diff <number> --name-only

   # Then the full diff
   gh pr diff <number>
   ```

   **IMPORTANT:** Always use `gh pr diff <number>` to get the diff, NEVER
   `git diff` against the local working tree. `git diff` includes
   uncommitted local changes that are not part of the PR and will
   pollute the description with unrelated changes.

   If the full diff is very large (>100KB), focus your reading on the
   most important files based on the file list. Skim auto-generated files
   (lock files, schema dumps, codegen) rather than reading line-by-line.

3. **Get the list of changed files:**

   ```bash
   gh pr view <number> --json files --jq '.files[].path'
   ```

4. **Read the current PR description** (already fetched in step 1
   via the `body` field — no separate call needed).

5. **Extract ticket number from branch name.** If the branch matches
   a pattern like `ENG-XXXX` or `PROJ-123`, immediately look it up
   in the team's tracker. This is almost always the most valuable
   context source.

6. **Read full file contents from the worktree** for key changed
   files. Use `Read`, `Grep`, and `Glob` tools on the worktree
   path:
   ```
   Read tool:  /tmp/pr-review-<number>/<file_path>
   Grep tool:  path="/tmp/pr-review-<number>/" pattern="..."
   Glob tool:  path="/tmp/pr-review-<number>/" pattern="**/*.py"
   ```

   This avoids the Bash tool's 50KB truncation limit and gives
   you full access to the file contents with line numbers.

   **Fallback:** If `Grep` or `Glob` fail on the worktree path
   (e.g. `rg` not found), fall back to `grep -rn` or `find` via
   the `Bash` tool on `/tmp/pr-review-<number>/`.

   Skip files that are:
   - Larger than 2000 lines (summarize from the diff instead)
   - Auto-generated (lock files, codegen `__generated__/*`, migrations
     with only version bumps)
   - Binary files

7. **Search what the team's tools expose** for related tickets,
   Slack discussions, or wiki pages. Look for ticket
   numbers in branch names, commit messages, and code comments.

### Phase 2: Assess PR Size

After gathering context, briefly assess the PR scope:

- **Small** (<100 lines changed, 1-3 files): skip Phase 3, go
  straight to writing.
- **Medium** (100-500 lines, single concern): ask 1-3 focused
  questions in Phase 3.
- **Large** (500+ lines or multiple concerns): ask 3-5 questions.
  If the PR clearly bundles unrelated changes, mention to the author
  that it might benefit from splitting — but still write the
  description for the PR as-is.

### Phase 3: Ask Clarifying Questions

Before writing anything, ask the human questions about things you
**cannot determine from the code alone**. Keep to **3-5 numbered
questions max** — the human should be able to answer in under a
minute.

Focus on:

- **Why** — what problem does this solve? Who requested it?
- **How to test** — specific verification steps (don't guess)
- **Non-obvious decisions** — reference specific code you saw and
  ask about the reasoning

**Rules for questions:**

- Only ask questions whose answers are NOT obvious from the
  diff/code/Jira ticket
- Number the questions for easy reference in answers
- Be specific — reference actual code changes you saw
- If the Jira ticket or existing PR description already answers a
  question, don't ask it
- The user can answer with just numbers and short phrases (e.g.
  "1. a customer asked, 2. yes, 3. skip")

**Keep asking** until you have enough context to write a good
description. Usually 1 round is enough. The user can say "that's
enough" or "just write it" to move on.

### Phase 4: Generate the PR Description

Use the following template strictly:

```markdown
# tl;dr 🚀

- 1-3 bullet points summarizing the PR

# Why 🤔

- Core motivation, problem solved, business/user value
- Link relevant Jira tickets, issues, discussions

# How to Test / Verification Steps ✅

- Specific steps to verify the change works
- If unclear, add: `> HUMAN_FIXME: describe how to test this`

# What ✨

- Technical implementation: key changes, new functions/classes,
  logic changes
- Reference specific files/modules
- If there are UI changes: embed screenshots inline (see "Embedding
  screenshots" below). Don't leave a `HUMAN_FIXME` stub if you took
  Chrome MCP screenshots during verification — embed them directly.

# Other changes 🛠️

- Secondary changes not related to the main goal
- "None" if there aren't any

# Important Notes 📝

- Impact assessment, risks, edge cases
- Areas needing closer review
- Deployment considerations (migrations, feature flags, env vars)
- Follow-up work

# Questions for HUMAN ❓

- Remaining ambiguities that would help the reviewer
- Format:
  - Question text?
    > INSERT_ANSWER_HERE
- Omit this section entirely if no questions remain
```

**Rules for the description:**

- Emojis ONLY in section titles (as shown above)
- Use descriptive link text: `[ENG-1280 Filter Results](https://...)`
- Never link to the PR itself
- Never mention AI/LLM/generation process
- Use `HUMAN_FIXME` where human input is needed
- Integrate any human remarks/answers from previous iterations
  directly into the relevant sections
- Group related file changes together by concern, not alphabetically

### Phase 5: Iterate

Show the generated description to the user. They may:

- Ask for changes to specific sections
- Answer remaining questions
- Add context you missed
- Say "looks good" / "ship it" / "put it"

When the user provides answers or feedback:

- Integrate answers into the relevant sections (not just the Q&A
  section)
- Remove answered questions from `Questions for HUMAN ❓`
- Keep any `HUMAN_FIXME` markers the user hasn't addressed yet
- Preserve the user's own wording when they provide specific text

### Phase 6: Freshness Check

Before updating the PR, verify that it hasn't advanced since you
started. New commits may have been pushed (e.g., a code-review fix
commit by another developer).

```bash
gh pr view <number> --json headRefOid --jq '.headRefOid'
```

Compare this SHA to the `headRefOid` you saved in Phase 1.

- **If they match** — proceed to Phase 7.
- **If they differ** — the PR has new commits. You MUST:
  1. Tell the user: "The PR has new commits since I started
     (was `<old_sha_short>`, now `<new_sha_short>`). The
     description may be missing context from the new changes.
     Want me to re-read the diff and update, or proceed as-is?"
  2. If the user wants an update: clean up and re-fetch:
     ```bash
     git worktree remove /tmp/pr-review-<number> --force
     git fetch origin pull/<number>/head:pr-<number> --force
     git worktree add /tmp/pr-review-<number> pr-<number>
     ```
     Then re-read the diff and revise the description.

### Phase 7: Update the PR

When the user approves (including short confirmations like "put it",
"ship it", "looks good", "lgtm", "yes"), update the PR immediately.

**Always use `--body-file <path>`, not `--body "$(cat <<EOF...)"`.** Long PR
bodies via shell heredoc 504 from GitHub's API on the first attempt;
`--body-file` works instantly and is also easier to inspect / re-run if the
call fails midway. Write the body to `/tmp/pr_body_<number>.md` first, then:

```bash
gh pr edit <number> --body-file /tmp/pr_body_<number>.md
```

The same applies to `gh pr create --body-file <path>` for new PRs. The
heredoc form below is shown only for completeness — prefer `--body-file`.

```bash
# Heredoc form — works for short bodies; expect 504s on long ones
gh pr edit <number> --body "$(cat <<'PREOF'
<the final description>
PREOF
)"
```

**PR title:** If the current title is generic (e.g. "do it", "wip",
"fix", branch name), also update it using conventional format:

```bash
gh pr edit <number> --title "<type>: <concise description>"
```

Conventional title prefixes:

- `feat:` — new feature or capability
- `fix:` — bug fix
- `refactor:` — code restructuring without behavior change
- `chore:` — maintenance, deps, CI, docs
- `perf:` — performance improvement

Keep titles under 70 characters. Use lowercase after the prefix.

---

## Important Notes

- **Human remarks are sacred.** Any text the user explicitly wrote
  or corrected must be preserved verbatim in future iterations.
- **Don't over-ask.** If the diff is self-explanatory (e.g., a typo
  fix), skip Phase 2 and go straight to writing.
- **Incorporate existing description.** If the PR already has a
  description, use it as input — don't discard it.
- **Look tickets up proactively.** If you see ticket numbers
  (ENG-XXXX) in branch names or commits, look them up immediately.
- **Multiple iterations are fine.** The goal is a description that
  helps reviewers, not speed.
- **Triage large diffs.** Read stat summary first, then focus on
  the files that carry the core logic change. Skim generated files.
- **Always clean up the worktree** when done (after updating the
  PR or on error):
  ```bash
  git worktree remove /tmp/pr-review-<number> --force 2>/dev/null
  git branch -D pr-<number> 2>/dev/null
  ```
