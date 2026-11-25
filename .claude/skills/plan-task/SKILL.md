---
name: plan-task
description: Use BEFORE writing code for any multi-file or non-trivial change. Reads the relevant CLAUDE.md files and lessons, verifies a task doc's file:line pointers, surfaces unknowns and the class of bug this change usually ships, and emits a short plan that becomes the contract. Skip for one-line fixes, typos, and pure-docs edits.
---

# Plan task

The cheapest bug to fix is the one caught before the first edit.

## Steps

1. **Read the map.** The root `CLAUDE.md`, the `CLAUDE.md` of every directory the change touches,
   and each lesson those files point at.
2. **Verify the pointers.** A task doc's `file:line` references drift by a few lines per merge; grep
   the symbol names and note the drift in the worklog. Do not reopen a settled decisions table.
3. **Surface unknowns.** What is ambiguous, what edge cases exist, which decisions move the
   architecture. Ask the user about the ones that would change the approach; default the rest and
   say so.
4. **Name the risk shape** for this repo: a writer held during compute, a POST missing from
   `_COOKIE_POST_OK`, a boot-time invariant, a landing claim without a source, a UI view outside the
   router leaking a timer, a schema change without its model. Plan the check for it.
5. **Emit the plan**: goal, files, order, "done when", the gates from `docs/development/checklist.md`. For a
   handed-off task, launch the `senior-swe-reviewer` and `pm-reviewer` agents in one message against
   this plan and reconcile before building.

## Output

A `## Plan` block in the conversation and, for a task with a worktree, the same in `worklog.md`.

Related: `docs/development/agent-workflow.md`.
