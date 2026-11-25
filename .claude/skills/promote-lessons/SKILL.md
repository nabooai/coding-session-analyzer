---
name: promote-lessons
description: Fold recurring friction from PR-body reflections and memory notes into docs/development/ and add the one-line pointers in the relevant CLAUDE.md files, then verify every claim still resolves. Use periodically, after a run of reflections, or when the user says "promote the lessons", "/promote-lessons".
---

# Promote lessons

A lesson in `docs/development/` is inherited by every later session; a lesson in a reflection by no one.

## Steps

1. **Read** the reflections since the last promotion (`gh pr list --state merged --search "Reflection in:body" --json number,title,body`) and
   the project memory notes the sessions left.
2. **Cluster** the bullets by topic. A friction that appears once is a note; twice or costly is a
   lesson.
3. **Write** each lesson into the matching `docs/development/<topic>.md` (or start a topic and add its
   row to `docs/development/README.md`): the trap, why it bites, the rule, and the symbol or test that
   makes it checkable.
4. **Point** from the `CLAUDE.md` of the folder where the trap lives: one line, the trigger words
   inline, a link.
5. **Verify** with the `claim-verifier` agent: every path, symbol and command in the lesson resolves.
6. **Commit** as its own change ("lessons: ...").

## Rules

- Keep `CLAUDE.md` files light: link to the lesson, do not inline it.
- A lesson cites something checkable; "be careful with X" is not a lesson.
- Retire a lesson when the code that made it true is gone; do not let the library rot.

Related: `docs/development/agent-workflow.md`.
