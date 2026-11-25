---
name: handoff
description: Write a self-contained task-handoff doc at .claude/tasks/<kebab-name>.md for another agent session, stamped with CSA's binding working protocol (worktree off origin/onprem-main with a worklog, concurrent senior-swe-reviewer and pm-reviewer agents, user-sim agents, long-running loops, the checklist). Use when the user says "/handoff", "hand this off", "write a task doc", or wants to delegate a feature; name a father task when subtasks depend on each other.
---

# Handoff

Turn the feature description (the skill args plus what this conversation established) into
`.claude/tasks/<kebab-name>.md`, ready for a fresh session with zero context.

## Rules

1. **Self-contained or it failed.** Fold established findings into "Research conclusions (verified
   <date>, do not redo)" with `file:line` pointers verified against the current tree.
2. **Ask at most three questions**, only for decisions that change the doc. Default the rest and say so.
3. **Acceptance criteria are executable**, with at least one negative case.
4. **Cross-reference sibling task docs** in `.claude/tasks/` that touch the same files.
5. **Dependent subtasks get a father task** that owns sequencing: one subagent per unblocked
   subtask, each blocked one launched when its blocker lands (`docs/development/agent-workflow.md`, "Larger programs").
6. **Copy in what is untracked.** A task doc or mock that is untracked on the fork point must be
   copied into the worktree and committed with the first PR.

## Doc skeleton

```markdown
# Task: <title>

**Status:** ready to start · **Owner:** unassigned (hand to a fresh agent session)
**Motivation/benchmark:** <the measured problem, 2 to 4 lines>

## Working protocol (binding, read before the code)

1. Work in a **separate git worktree off `origin/onprem-main`** (`git worktree add .claude/worktrees/<name> -b worktree-<name> origin/onprem-main`), with an untracked `worklog.md` holding THIS task's status (it is not committed; the reflection goes in the PR body). Never a shared checkout.
2. Before implementing, **spawn the `senior-swe-reviewer` AND `pm-reviewer` agents in one message**; reconcile, then build.
3. After implementing, **launch the `user-sim` agents** (3+, concurrent): <TASK-SPECIFIC SCENARIOS>. Fix, re-run them.
4. Use a **long-running loop** for <TASK-SPECIFIC long phases> instead of foreground waits.
5. **Follow `docs/development/checklist.md`** and `.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md`; end with `self-reflection`.

## Decisions (settled, do not re-litigate)
## Research conclusions (verified <date>, do not redo)
## Deliverables
## Acceptance (all must pass)
## Pointers
```

## After writing

Report the doc path and a one-line summary per section, and say plainly if the research section is
thin (the assigned session would pay for it).
