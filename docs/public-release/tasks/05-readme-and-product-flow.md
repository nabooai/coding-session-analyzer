# Task 05: Short README, product Why, installation prompt and architecture flow

**Status:** open · **Owner:** docs + product · **Priority:** P0

## Objective

Explain why CSA exists, how to install it, and how it works in a short public README that assumes readers
will skim or hand installation to Claude.

## Required README shape

Target 80–120 lines:

1. Product name and one-sentence promise.
2. Three short Why examples.
3. One current, synthetic-data screenshot of the clean search UI.
4. Requirements.
5. Start the server (one guided command).
6. Connect Claude Code (plugin commands + browser Authorize).
7. “Install using Claude Code” copyable prompt.
8. How it works (concise flow).
9. What leaves the machine / privacy defaults.
10. Verify first search.
11. Development, security and license links.

Move deployment matrices, internal research, detailed client alternatives, admin operations and roadmap to
focused docs. Remove badge walls and Observe/Discover/Improve marketing sections.

## Opening Why copy

Use these ideas in a short, playful but clear opening:

```text
Why CSA?

“Dude, why did you choose Postgres instead of SQLite?” — “Ask my session.”

What is the most important skill we should create from our repeated work? Use CSA and find out.

Why not just give teammates access to every session? Choose exactly which projects and sessions
become shared knowledge.
```

Edit for grammar and truth, but preserve the concrete examples. Do not add a long mission statement.

## Required architecture flow

Include a compact diagram/list that matches the merged implementation:

```text
Claude Code session hooks
  → local consent, project scope and secret redaction
  → authenticated ingestion + durable queue
  → JSONL/session normalization
  → small-model extraction of typed facts, entities, relationships and evidence
  → SQLite corpus + entity/temporal graph tables + FTS5 index
  → lexical/BM25 retrieval + filters/deterministic reranking + graph context
  → clean web search and MCP recall
  → background extraction, judging, deduplication/compaction and retention workers
```

Name the model's main/small roles where useful. Do not claim Dreaming, semantic search, autonomous skills,
or other unmerged capabilities. Explain lexical search in one sentence: exact technical terms, identifiers,
paths and errors are matched with BM25, then ranked with provenance/recency/type signals.

## Claude installation prompt

Include a block users can paste directly:

```text
Install CSA by following
https://github.com/nabooai/coding-session-analyzer/blob/master/INSTALL.md.

Use the “server + Claude Code plugin” path. Explain each command before running it.
Ask before using my model credentials, uploading existing sessions, changing project-sharing scope,
or enabling automatic sharing.
```

Create `INSTALL.md` as the deterministic agent-friendly runbook: prerequisites, exact commands, expected
output, credential/consent stop points, verification and five common failures. README and INSTALL must not
diverge.

## Acceptance

- [ ] README is 80–120 lines, opens with the Why examples, and has one obvious installation path.
- [ ] Claude and OpenAI setup instructions exactly match tested provider support.
- [ ] The flow explicitly covers hooks, redaction, queue, extraction, entities, facts/evidence, graph tables,
      FTS5/lexical BM25, background workers, UI and MCP.
- [ ] Claude prompt and `INSTALL.md` are tested from a clean machine by giving the prompt to Claude Code.
- [ ] All links work anonymously and use the final company organization.
- [ ] Screenshots match the release UI and contain synthetic data only.
- [ ] No private infrastructure, client names, internal roadmap, stale claims or unimplemented features.
- [ ] Privacy defaults and history/backfill behavior are stated in plain language.

## Merge requirement

Merge README, INSTALL and screenshot updates to `origin/master` after the related install/UI tasks land so the
public text describes actual behavior.
