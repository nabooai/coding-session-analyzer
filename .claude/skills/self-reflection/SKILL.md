---
name: self-reflection
description: Write the post-change reflection for the current branch into its PR body from the session's evidence (worklog, review findings, simulation reports, the user's corrections), and mark what should be promoted to docs/development. Use as the last step of every change, or when the user says "reflect", "write the reflection", "/self-reflection".
---

# Self-reflection

Suffer once, not twice. The reflection is written from evidence, not from memory, and lives in the
PR body under a `Reflection` heading, so it travels with the change without adding a file to the repo.

## Steps

1. **Gather the evidence.** The worktree's `worklog.md`; the SWE and PM round; every user-simulation
   report (both rounds); the fresh-context review's findings; the user's corrections during the
   session (quote them); the commits on the branch (`git log origin/onprem-main..HEAD --oneline`).
2. **Answer the three questions** from `TEMPLATE.md` beside this skill:
   what did I miss (each item: the file, the sentence, what it cost), what surprised me (the
   assumption that was wrong), what would I do differently (the approach change, and the doc, hook
   or skill that would carry it).
3. **Mark what to promote.** For each recurring item, name the `docs/development/` page it belongs in. If
   the lesson is load-bearing and you have the time, promote it now (`promote-lessons`).
4. **Put it in the PR body** (`gh pr edit --body-file`), under `## Reflection`. The wrap-up checklist requires it.

## Rules

- Specific over general: "the theme default did not land because the old boot persisted 'system'"
  beats "theme handling was tricky".
- No blame, no narrative: what happened, what it cost, what prevents it.
- No em dashes.

Related: `docs/development/agent-workflow.md`.
