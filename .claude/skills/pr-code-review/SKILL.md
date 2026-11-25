---
name: pr-code-review
description: Perform a thorough code review on a PR — find logic bugs, security issues, race conditions, and unclear behavior. Posts findings as inline comments on the PR via GitHub.
---

# PR Code Review Skill

Perform a substantive code review focused on real problems — logic
bugs, security vulnerabilities, race conditions, data integrity
issues, and genuinely unclear behavior. Post findings as inline
PR comments on the exact lines that need attention.

**This is NOT a style/linting review.** Do not comment on
formatting, naming preferences, missing docstrings, or cosmetic
issues. Every comment must identify a real or potential problem
that could cause bugs, security issues, or confusion for the next
developer.

---

## Trigger

Use when the user asks to:
- Review PR code / do a code review
- Find bugs or issues in a PR
- `/pr-code-review` or `/pr-code-review <number>`

---

## Process

### Phase 1: Gather Context

Before creating any temporary worktree, record the checkout that invoked the skill:

```bash
ORIGINAL_REPO_ROOT=$(git rev-parse --show-toplevel)
```

**Do NOT assume the local checkout matches the PR branch.** Use a
git worktree to check out the PR code into a separate directory
so you can read files with `Read`, `Grep`, and `Glob` tools
without switching branches or affecting the working directory.

Run these in parallel where possible:

1. **Get PR metadata, extract owner/repo, fetch the PR ref, create
   a worktree, and post the placeholder comment** (so reviewers
   know a review is in progress while you work):
   ```bash
   gh pr view <number> --json number,title,body,url,baseRefName,headRefName,headRefOid
   ```

   **Save the `headRefOid` value** — you will need it later for
   the freshness check in Phase 5.

   **Extract `{owner}` and `{repo}` from the PR URL** returned
   above (e.g. `https://github.com/acme/web/pull/1835`
   → owner=`acme`, repo=`web`). Use these for all
   subsequent `gh api` calls. Do NOT guess — always parse from
   the URL.

   ```bash
   # Fetch the PR head into a local ref
   git fetch origin pull/<number>/head:pr-<number>

   # Create a worktree checkout (does NOT affect the main working dir)
   # Remove stale worktree first if it exists from a previous run
   git worktree remove /tmp/pr-review-<number> 2>/dev/null; git worktree add /tmp/pr-review-<number> pr-<number>
   ```

   In parallel, post the placeholder comment:
   ```bash
   gh pr comment <number> --body "> 🤖 **Code review in progress...** via \`pr-code-review\`"
   ```
   Save the comment ID from the response for later
   editing/deletion.

   The worktree at `/tmp/pr-review-<number>/` is now a full
   checkout of the PR code. Use `Read`, `Grep`, and `Glob` tools
   on paths under `/tmp/pr-review-<number>/` to read files.

2. **Get file list, then full diff:**
   ```bash
   gh pr diff <number> --name-only
   gh pr diff <number>
   ```

3. **Get changed files:**
   ```bash
   gh pr view <number> --json files --jq '.files[].path'
   ```

4. **Extract ticket from branch name.** If it matches `ENG-XXXX`
   or similar, look it up in the team's tracker for intent context.

5. **Read full file contents from the worktree** for key changed
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

   Skip files that are auto-generated or binary. Reading
   surrounding code is critical — many bugs are only visible when
   you see what the changed code interacts with.

6. **Read the PR description** (already fetched in step 1) — it
   may explain intent and design decisions that affect whether
   something is a bug or intentional.

7. **Read existing review comments and their state:**
   ```bash
   # Get all review comments (inline code comments)
   gh api "repos/{owner}/{repo}/pulls/<number>/comments" \
     --jq '.[] | {id, path, line, body, user: .user.login, state: .state, in_reply_to_id}'

   # Get top-level PR reviews (approved, changes requested, etc.)
   gh api "repos/{owner}/{repo}/pulls/<number>/reviews" \
     --jq '.[] | {id, state, body, user: .user.login}'

   # Get conversation-level comments (non-inline)
   gh api "repos/{owner}/{repo}/issues/<number>/comments" \
     --jq '.[] | {id, body, user: .user.login}'
   ```

   Also fetch review thread resolution state via GraphQL:
   ```bash
   gh api graphql -f query='
   {
     repository(owner: "{owner}", name: "{repo}") {
       pullRequest(number: <number>) {
         reviewThreads(first: 50) {
           nodes {
             id
             isResolved
             comments(first: 1) {
               nodes { body databaseId }
             }
           }
         }
       }
     }
   }'
   ```

   Use existing comments to:
   - **Don't duplicate** — if someone already flagged an issue,
     don't post the same finding again
   - **Build on discussions** — if there's an unresolved thread
     about a concern, you can add to it rather than starting a
     new one
   - **Understand reviewer intent** — when a reviewer flags a
     code path, they're usually asking "shouldn't this path also
     have X?" Don't dismiss the concern just because the current
     code doesn't exercise it. Ask yourself: *what does the
     reviewer want the code to become?* — not just what it
     currently does. If a reviewer says "why not here?", they're
     almost certainly right that the fix is incomplete.
   - **Resolve addressed threads** — if a comment raised an issue
     that has since been fixed in the current diff, resolve the
     thread via GraphQL mutation:
     ```bash
     gh api graphql -f query='
     mutation { resolveReviewThread(input: {threadId: "<thread_id>"}) {
       thread { isResolved }
     }}'
     ```
     Do this automatically for any unresolved thread where the
     code change clearly addresses the feedback. No need to ask.
   - **Respect already-resolved threads** — don't reopen the same
     topic unless the fix introduced a new problem
   - **Note outstanding requests** — if a reviewer requested
     changes that haven't been addressed, mention them in the
     summary

### Phase 1.25: Capture Human Review Conventions

Use the `ORIGINAL_REPO_ROOT` captured at the start of Phase 1. Human feedback must be written to:

```
<original-repo-root>/.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md
```

The temporary `/tmp/pr-review-*` worktree is deleted at the end, so never write the conventions file
there.

From the inline reviews, top-level reviews, and conversation comments fetched above, collect comments
written by humans. Exclude:

- GitHub bot accounts
- Comments beginning with `> 🤖 **Automated review**`, `> 🤖 **Automated code review**`, or
  `> 🤖 **Code review in progress` (the posting account may be human even though the text came from
  this skill)
- Comments whose stable GitHub URL or database ID is already in the conventions file

Append each new item to the evidence ledger with:

- PR number and stable comment URL/ID
- author and date
- exact human wording (never silently rewrite the quote)
- affected path or area
- a concise **candidate convention** inferred from it
- confidence: `explicit`, `repeated`, or `candidate`

One terse comment is evidence, not automatically a universal rule. Mark it `candidate`; promote it to
`repeated` only when independent comments support the same preference, or `explicit` when the reviewer
states it as a general rule. Never invent rationale the human did not provide.

Read the whole conventions file before Phase 2. Apply `explicit` and `repeated` conventions during the
review. Use `candidate` conventions as questions/checks, not as grounds for a blocking finding by
themselves. Mention newly captured entries in the final chat summary so the file does not change
silently.

### Phase 1.5: Detect Re-review

If existing review comments (from step 7) include comments posted
by this skill (identified by the `> 🤖 **Automated review**`
prefix), this is a **re-review** — the user pushed fixes and
wants a fresh look.

In a re-review:

1. **Resolve fixed threads.** For each unresolved thread from a
   previous automated review, check whether the current diff
   addresses the feedback. If it does, resolve the thread via
   GraphQL mutation immediately — no need to ask.
2. **Check for regressions.** Verify the fix didn't introduce new
   problems (e.g., fixing a typo but breaking a different call
   site).
3. **Skip resolved topics.** Don't re-flag issues that were
   already resolved, unless the fix introduced a new problem.
4. **Focus on deltas.** Compare the current diff against the
   previous review's findings. Only flag genuinely new issues.

### Phase 2: Analyze the Code

First read `.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md` from the original repository
root and incorporate its established conventions. Then review the diff systematically, checking for
each category below.
**Only flag issues you are confident about** — false positives
destroy review credibility.

**Does the fix match the intent?** Read the PR title, description,
and ticket to understand what problem the author set out to solve.
Then verify the diff actually solves that problem — completely, not
partially. Ask: *is this a proper fix, or a bandaid that patches
one occurrence while the same issue lurks elsewhere?* Search the
codebase for other places where the same pattern exists and check
whether those also need the fix.

#### Categories (ordered by severity)

**CRITICAL — must fix before merge:**
- Security vulnerabilities (injection, XSS, auth bypass, secrets
  in code, SSRF, path traversal)
- Data loss or corruption (wrong transaction boundaries, missing
  rollbacks, race conditions on shared state)
- Logic errors that produce wrong results (off-by-one, inverted
  conditions, wrong operator, missing null checks that will crash)

**WARNING — likely problem, should fix:**
- Race conditions or concurrency issues
- Error handling that swallows exceptions silently or masks the
  root cause
- Missing validation at system boundaries (user input, API
  responses, database results)
- Resource leaks (unclosed connections, files, missing cleanup)
- Incorrect async patterns (fire-and-forget without error
  handling, blocking the event loop)
- Breaking changes to public APIs or database schemas without
  migration strategy

**QUESTION — unclear intent, needs clarification:**
- Code that contradicts the stated PR intent or ticket
- Complex logic without comments where the "why" is not obvious
- Behavior changes that might be unintentional side effects
- Hardcoded values that look like they should be configurable
- TODOs or FIXMEs introduced without a tracking ticket

#### What NOT to flag

Do NOT comment on any of the following:
- Code style, formatting, or naming (that's what linters are for)
- Missing type hints or docstrings on unchanged code
- "Nit" or "suggestion" level feedback
- Import ordering
- Preferring one equivalent pattern over another when both are
  correct
- Test structure preferences (unless tests are actually wrong)
- "Consider using X instead of Y" when Y works fine

### Phase 3: Prepare Findings

For each finding, prepare:

1. **File and line** — the exact file path and line number in the
   diff where the issue appears
2. **Severity** — `CRITICAL`, `WARNING`, or `QUESTION`
3. **Title** — one-line summary (under 80 chars)
4. **Explanation** — what the problem is and why it matters. Be
   specific — reference variable names, function behavior, edge
   cases.
5. **Suggestion** — how to fix it (include a code snippet when
   possible using GitHub suggestion blocks)

Format each finding as a review comment body like this:

```
> 🤖 **Automated review** via `pr-code-review`

**[SEVERITY] Title**

Explanation of the problem.

Optional suggested fix:
\```suggestion
fixed code here
\```
```

The `> 🤖 **Automated review**` blockquote must be the **first
line** of every comment. This makes it immediately obvious the
comment was generated by the skill, not written manually by the
user whose account posted it.

Similarly, the top-level review body (the `event=COMMENT` body)
should start with:

```
> 🤖 **Automated code review** via `pr-code-review`

Found X issues (Y critical, Z warnings, W questions).
```

### Phase 4: Post Findings (No Approval Gate)

**Post comments directly.** Do not ask the user "should I post these?" — the user invoked this skill to *get a review on the PR*, and a stale `🤖 Code review in progress…` placeholder hanging on the PR while you wait on the user is worse than any wording mistake. The user can always delete or edit comments after the fact.

The only thing that gates posting is **Phase 5's freshness check** (PR head SHA still matches what you reviewed). Apply your own confidence threshold during Phase 2 — if a finding feels uncertain, downgrade to `QUESTION` severity or drop it before reaching here.

**No-findings path — edit the placeholder unconditionally and immediately.** If Phase 2 produced zero findings, do NOT ask the user whether to post an approval. Edit the placeholder via `PATCH` to one of the templates below in Phase 6, then surface the result. The placeholder must never outlive the review. Whether the user *also* wants a formal `event=APPROVE` review is a separate question — see the next paragraph.

**If you are posting a favorable / approval-style review, write the full body — not "OK" or "Looks good ✅".** A substantive approval explains *what you verified*: which categories you checked (logic, concurrency, error handling, security), what specifically you read end-to-end, what edge cases you considered, and any noteworthy non-issues you considered and dismissed. The reader should be able to tell that someone actually read the code. Three-sentence minimum; reuse the Phase 2 categories list as a verification checklist in the body.

Bad approval body:

```
> 🤖 Automated review — LGTM ✅
```

Good approval body:

```
> 🤖 **Automated code review** via `pr-code-review` — no issues found ✅

Verified end-to-end:
- **Logic / fix completeness**: traced `_ticket_from_board_item` through
  the new attribute-access path; all four call sites updated; no remaining
  `project["..."]` subscripts in this file or sibling modules.
- **Concurrency**: the change is purely transformation-side, no shared
  state or async boundaries touched.
- **Error handling**: missing `parent_item` returns `None` cleanly via
  the existing `getattr` fallback; no new exception paths.
- **Tests**: the new `test_ticket_from_board_item_with_parent` exercises
  the previously-uncovered crash branch and fails against the un-fixed
  code (verified locally).

Considered and dismissed:
- The remaining `$.data.*` json_extract calls in `sqlite3_graph.py` —
  out of scope for this PR per its description; tracked as follow-ups.
```

**Author-self-approve constraint.** When the account posting the review *is the PR author*, GitHub returns `422: Cannot approve your own pull request` if you use `event: "APPROVE"`. Default to `event: "COMMENT"` for any review this skill posts. The favorable-body content above is what makes it an approval in spirit; the `event` field is just the GitHub API ceremony. Only use `event: "APPROVE"` if the user explicitly asks for it AND the posting account is not the PR author.

### Phase 5: Freshness Check

Before posting, verify that the PR hasn't advanced since you
started the review. New commits may have been pushed (e.g., a
code-review fix commit by the author).

```bash
gh pr view <number> --json headRefOid --jq '.headRefOid'
```

Compare this SHA to the `headRefOid` you saved in Phase 1.

- **If they match** — proceed to Phase 6.
- **If they differ** — the PR has new commits. You MUST:
  1. Tell the user: "The PR has new commits since I started
     reviewing (was `<old_sha_short>`, now `<new_sha_short>`).
     Re-fetching and re-reviewing."
  2. Clean up the old worktree and re-fetch:
     ```bash
     git worktree remove /tmp/pr-review-<number> --force
     git fetch origin pull/<number>/head:pr-<number> --force
     git worktree add /tmp/pr-review-<number> pr-<number>
     ```
  3. Re-fetch the diff: `gh pr diff <number>`
  4. Go back to Phase 2 with fresh data. Your existing findings
     may still apply — but re-verify each one against the new
     code, as the new commits may have fixed some issues.
  5. Show updated findings for approval again (Phase 4).

### Phase 6: Post Review and Update Placeholder

Post findings as a **single PR review** with inline comments, not
as individual comments. This groups them into one notification.

Use the GitHub API to create a review with comments. **CRITICAL:
You must use a JSON file via `--input`** — the `-f` flag syntax
does NOT work for array fields and will return HTTP 422.

1. **Create `/tmp/pr_review.json`** with your payload. The
   `Write` tool requires reading a file first, so for new files
   use Bash to create it:
   ```bash
   cat > /tmp/pr_review.json << 'REVIEW_EOF'
   {
     "event": "COMMENT",
     "body": "...",
     "comments": [...]
   }
   REVIEW_EOF
   ```
   Alternatively, create an empty file first (`echo -n > /tmp/pr_review.json`),
   then `Read` it, then use `Write` to set the real content.

   Example payload:
```json
{
  "event": "COMMENT",
  "body": "> 🤖 **Automated code review** via `pr-code-review`\n\nFound X issues (Y critical, Z warnings, W questions).",
  "comments": [
    {
      "path": "path/to/file.py",
      "line": 42,
      "side": "RIGHT",
      "body": "> 🤖 **Automated review** via `pr-code-review`\n\n**[WARNING] Title**\n\nExplanation."
    }
  ]
}
```

2. **Then, use the `Bash` tool** to post the review:
```bash
gh api repos/{owner}/{repo}/pulls/<number>/reviews \
  --method POST \
  --input /tmp/pr_review.json
```

3. **After posting the review**, delete the placeholder comment:
```bash
gh api repos/{owner}/{repo}/issues/comments/<comment_id> \
  --method DELETE
```

If the review found no issues, **the placeholder edit happens unconditionally** (Phase 4 — no user gate). Use the substantive favorable-review body from Phase 4 (the "Verified end-to-end / Considered and dismissed" template), not a one-line ✅:

```bash
gh api repos/{owner}/{repo}/issues/comments/<comment_id> \
  --method PATCH \
  --input /tmp/pr_review_no_issues.json
```

where `/tmp/pr_review_no_issues.json` contains a `body` field with the multi-paragraph approval text. The one-liner `— no issues found ✅` is acceptable only as the comment's *header*; the body still needs to enumerate what was verified.

**`pull_request_review_thread.base` 422 fallback.** Occasionally GitHub returns `422: An internal error occurred, please try again` against every line on a PR — not line-specific, not body-specific. Retries don't help; a body-only `event=COMMENT` review against the same PR posts fine. When you hit this on the inline-comment path, **fold the inline findings into the top-level review body** with explicit `path:line` references and post that. Don't loop trying to recover — one fold-and-post is the documented fallback.

```json
{
  "event": "COMMENT",
  "body": "> 🤖 **Automated code review** via `pr-code-review`\n\nFound 2 issues (1 warning, 1 question). Inline comments could not be attached due to a GitHub server-side error against this PR's diff — folded inline:\n\n**[WARNING] backend/src/etl/x.py:42 — Title**\n\nExplanation.\n\n**[QUESTION] backend/src/etl/y.py:117 — Title**\n\nExplanation.",
  "comments": []
}
```

**Re-review with all prior issues resolved:** If this is a
re-review and all previously flagged issues have been addressed
(threads resolved), edit the placeholder to reflect this:
```bash
gh api repos/{owner}/{repo}/issues/comments/<comment_id> \
  --method PATCH \
  -f body="> 🤖 **Code review complete** via \`pr-code-review\` — no issues found ✅
>
> Previously flagged issues have been fixed. Threads resolved."
```

**Line number rules:**
- `line` is the line number in the **diff hunk**, corresponding
  to the line in the NEW version of the file (right side)
- Use `side=RIGHT` for comments on added/modified lines
- Use `side=LEFT` for comments on removed lines
- The line number must fall within a diff hunk — you cannot
  comment on unchanged lines that aren't in the diff context
- **CRITICAL: Verify each line is in the diff before commenting.**
  The GitHub API returns HTTP 422 "Line could not be resolved" if
  you try to comment on a line that exists in the file but is NOT
  within any `@@` hunk in the diff. Before adding a comment,
  confirm the target line appears as a `+` or context line in
  `gh pr diff`. If you find an issue on a line that's not in the
  diff (pre-existing code), mention it in the top-level review
  body instead of as an inline comment.
- For **new files** (entire file is `+` lines), the file line
  number equals the diff line number — every line is in the hunk.

**After posting**, clean up the worktree:
```bash
git worktree remove /tmp/pr-review-<number> --force 2>/dev/null
git branch -D pr-<number> 2>/dev/null
```

Then print the PR URL so the user can see the review. Also show
a brief summary of findings to the user in the conversation.

---

## Review Checklist (internal reference)

Use this as a mental checklist when scanning code. Not all apply
to every PR — focus on what's relevant to the changes.

### Security
- [ ] User input sanitized before use in queries/commands/HTML?
- [ ] Auth/permission checks on new endpoints or mutations?
- [ ] Secrets, tokens, or credentials hardcoded or logged?
- [ ] File paths validated against traversal (../../)?
- [ ] CORS, CSP, or security headers affected?

### Data Integrity
- [ ] Database transactions have correct boundaries?
- [ ] Migrations are reversible and safe for zero-downtime deploy?
- [ ] Nullable columns handled in code (no `None.attribute`)?
- [ ] Unique constraints or indexes needed for new columns?
- [ ] Cache invalidation needed for changed data?

### Concurrency
- [ ] Shared mutable state protected?
- [ ] async/await used correctly (no blocking in async context)?
- [ ] Database sessions not shared across tasks/threads?
- [ ] Race conditions in read-modify-write patterns?

### Error Handling
- [ ] Exceptions not swallowed silently?
- [ ] External API calls have timeout and retry logic?
- [ ] Partial failure states cleaned up (rollbacks, compensation)?
- [ ] Error messages don't leak sensitive information?

### Logic
- [ ] Edge cases: empty lists, None, zero, negative, boundary?
- [ ] Off-by-one in loops, slices, pagination?
- [ ] Boolean logic correct (De Morgan's, short-circuit)?
- [ ] New code reachable? Dead code introduced?
- [ ] Return types match what callers expect?
- [ ] Fix completeness: does the same bug/pattern exist elsewhere
      in the codebase? Is this a full fix or a bandaid?

---

## Important Notes

- **Confidence threshold.** Only post findings you're reasonably
  sure about. One false positive undermines the whole review.
  When uncertain, use `QUESTION` severity — it invites discussion
  rather than asserting a bug.
- **Self-review "probably acceptable but worth noting" is usually
  a real ask in disguise.** When you're reviewing your own PR
  (`/pr-code-review` against your own work) and a finding turns up
  a failure-mode flip from silent-broken to loud-broken (e.g. fix
  changes "silently propagates `None`" to "raises per-tenant on
  malformed data and aborts the batch"), the user reads "probably
  acceptable" as "you have one foot in fixing this." Default
  behavior: **fix it and write the test, don't just annotate.**
  Annotation is for true scoping decisions ("this could also affect
  X but X is out of scope"), not for "this could be more
  defensive." If you're unsure enough to write the QUESTION at
  all, the user usually wants the defensive fix.
- **Sabotage-test framing for verifying a fix to a review finding.**
  When a previous review said "tests mirror the production code
  path" or "X regression isn't caught by the suite," writing a test
  that *names* the concern (e.g. a class called
  `TestRedactionCallSiteContract`) is **not** the same as confirming
  the contract is enforced. The reliable check: write down the
  *adversarial mutation* the reviewer was worried about ("delete the
  redaction call site at stream.py:636"), apply it locally, and
  verify the suite turns red. If the suite stays green after the
  mutation, the test you added does not address the finding —
  regardless of how on-topic the test name reads. For each round of
  review that closes with "added test for X," explicitly state which
  mutation the test would catch. AST-based contract tests are
  particularly susceptible to this: a class name that contains
  "contract" can read as load-bearing while the body checks
  something orthogonal.
- **Read surrounding code.** Don't review the diff in isolation.
  Read the full file to understand context, invariants, and how
  the changed code interacts with the rest of the system.
- **Understand intent first.** Read the PR description, ticket,
  and commit messages before reviewing code. A "bug" might be an
  intentional behavior change.
- **Don't default to "this is fine".** When you encounter a
  reviewer's unresolved comment or a code path that *could* be
  problematic, resist the urge to explain it away by looking only
  at the current state ("no callers exercise this path today").
  Think about what the code *should* do, not just what it does
  now. A human reviewer who flags something is almost always
  seeing a real problem — take it seriously before dismissing it.
- **One review, not a stream.** Always batch comments into a
  single review submission. Multiple individual comments spam the
  PR author with notifications.
- **Actionable comments only.** Every comment should either (a)
  identify a problem with a suggested fix, or (b) ask a specific
  question about unclear intent. "This could be improved" without
  a concrete suggestion is not actionable.
- **Be direct, not harsh.** State what the issue is and why it
  matters. Don't soften with "maybe consider perhaps" — but also
  don't be condescending.
- **Always clean up the worktree** when done (after posting or on
  error):
  ```bash
  git worktree remove /tmp/pr-review-<number> --force 2>/dev/null
  git branch -D pr-<number> 2>/dev/null
  ```
