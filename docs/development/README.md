# Developing CSA

What a contributor needs beyond [CONTRIBUTING.md](../../CONTRIBUTING.md): the change checklist, the
review conventions, and one page per area where the code has a trap that is not obvious from reading
it. Read the matching page before starting work in that area, not after getting stuck.

| Page | Read before... |
|---|---|
| [Change checklist](checklist.md) | declaring any change done. The ORM and database rules live here too |
| [Review conventions](../../.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md) | opening any PR: what review has flagged before, with the quotes |
| [Schema migrations](schema-migrations.md) | adding or changing a table or column, reading a generated revision, a second branch that also changes the schema |
| [SQLite concurrency](sqlite-concurrency.md) | writing to any corpus table, attaching a database, adding a corpus-wide pass, changing FTS |
| [Settings](settings.md) | adding or moving a setting, choosing who may write it, reading one on a hot path |
| [Process state](process-state.md) | adding any module-level dict, list or set, a cache, a rate limiter, or anything a user waits on across a request |
| [Web UI](web-ui.md) | any change under `server/ui`: routes, the canvas graph, theme, hidden elements, sticky offsets, browser screenshots |
| [Landing copy](landing-copy.md) | changing a sentence on the public landing or sign-in: what may be claimed, and what the audit rejects |
| [Operating traps](operations.md) | changing a boot check, compose files, `ui/dist` shipping, the client's hooks or retries |
| [Agent workflow](agent-workflow.md) | working with Claude Code in this repo: the skills, agents and hooks under `.claude/` |

Each page names a trap, why it bites, and the rule that prevents it, tied to a symbol or a test you
can check. A component's `CLAUDE.md` carries a one-line pointer to the pages that apply to it. When
the code that made a lesson true is gone, delete the lesson.
