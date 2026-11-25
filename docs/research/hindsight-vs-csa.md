# Hindsight vs CSA

Research from 2026-11-03/04 on [Hindsight](https://hindsight.vectorize.io/) by Vectorize, the closest
open-source project to CSA. Code claims were read at commit `f14cc1e` of
[vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) and `28b644b` of
[vectorize-io/agent-memory-benchmark](https://github.com/vectorize-io/agent-memory-benchmark), and npm
package 0.8.5 was inspected without running it. No benchmark was rerun. Dollar figures marked
"estimate" are ours, not published numbers.

## Summary

- Hindsight already ships CSA's hook-plus-MCP pattern across 22 coding tools, so "memory for coding
  agents" does not set CSA apart.
- CSA's real difference: facts attributed to people on a team, redaction on the laptop before anything
  is sent, recall on each prompt with no model call, and typed facts that cite a quote.
- Hindsight is ahead on install trust, tool coverage, contradiction handling and published benchmarks.
- It looks more trustworthy because of how it is packaged, while protecting developers less.

## What Hindsight is

An MIT-licensed agent-memory server, about 58.5k GitHub stars, created October 2025. Each user, agent
or repo gets an isolated "bank" holding world facts, experience facts, observations (beliefs built from
many facts) and mental models. Three operations: `retain`, `recall`, `reflect`. Storage is Postgres
with pgvector. It can be self-hosted (Docker, Helm, pip, embedded), or used through a Cloud or a
single-tenant Enterprise tier.

| Same | They have, CSA does not | CSA has, they do not |
|---|---|---|
| Hooks plus MCP | Automatic handling of contradicted facts | Redaction on the laptop, before sending |
| Extraction from transcripts | 22 tools from one install | Facts attributed to a person, and a team view |
| Entity graph, Helm, Bedrock/Vertex/Azure | Learning from git history; knowledge pages | Recall with no model call on each prompt |
| Self-hosted server | Semantic search, re-ranking, token budget | Typed facts with a valid/invalid timeline |
| Facts cite their evidence | Published benchmarks; managed Cloud | Per-session cost and waste; OIDC SSO in the free product |

## Contradictions

A background job, on by default, runs after `retain`. It loads 50 facts at a time and sends 8 per model
call ([config](https://github.com/vectorize-io/hindsight/blob/f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24/hindsight-api-slim/hindsight_api/config.py#L1932)).

1. **Search.** One recall per fact, over observations only, in the same tag scope, capped at 512 tokens
   ([consolidator](https://github.com/vectorize-io/hindsight/blob/f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L3634)).
2. **Label.** A model labels each fact and observation pair `unrelated`, `repeats`, `changes` or `ends`
   ([prompts](https://github.com/vectorize-io/hindsight/blob/f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24/hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py#L116)).
3. **Compare dates.** Plain code does this, not the model
   ([to_actions](https://github.com/vectorize-io/hindsight/blob/f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L979)),
   so a late-imported old fact cannot overwrite a newer one.
4. **Write.** The observation text is rewritten and its source ids extended. The old text moves to
   `observation_history`, trimmed to a maximum, with no reason stored. The visible code does not edit
   raw facts. There is no `invalid_at`: the change is recorded in prose.

Example from their prompt: the observation says "owned by Atlas" and a newer fact says "Nimbus". The
verdict is `changes`, and the text becomes "owned by Nimbus; earlier by Atlas". A late-imported older
fact, "Zephyr", is appended in brackets as an earlier state, and Nimbus stays current.

Known weak spots:
- A contradicted observation outside the 512-token window is probably never compared, so both stand
  (our inference).
- Their own evals score a small model at 36-38 of 40.
- Failed batches lose their writes, and the failure counters can look clean.
- Deleting an observation also deletes its history.

### How the search works, and what it costs

It is the same `recall_async` path users get, without the re-ranker.

| Step | Method | Model | Cost per call |
|---|---|---|---|
| Semantic | Vector similarity | `bge-small-en-v1.5`, 384 dimensions, local | None |
| Lexical | BM25 | None | None |
| Graph | Semantic seeds, then entity, similarity and causal links | None | None |
| Time | Only when the text contains a date | None | None |
| Merge | Round-robin; no cross-encoder here | None | None |
| Label | One call per 8 facts | `gpt-4o-mini` by API, configurable | The only per-call cost |

Estimate: **$0.27-0.96 per 1,000 facts**, from 125 calls at 9-39k input and 1.3-3k output tokens, at
$0.15/$0.60 per million. The `gpt-4o-mini` price is from memory and token counts are not measured. A
cheaper triage flag exists, but its provider defaults to `none`, so it is effectively off. Running
Postgres and the embedding model is an extra cost.

### What to take for CSA

Keep "the model labels, code compares dates", and write the outcome to `invalid_at` and `fact_edges`
instead of prose. CSA has both columns, but nothing writes `fact_edges` and `invalid_at` is only written
by the opt-in registry check.

Cheapest version, using what exists: candidates from `fts_candidates` plus shared entities, capped at
20; a Haiku-class labeler in batches of 8; code compares dates.

| | Estimate |
|---|---|
| One run over about 67,000 facts | $11-106, depending on what `haiku` resolves to |
| Per new session, at about 9 facts each | About $0.002-0.014, on top of about $0.07 for extraction |

It would miss reworded reversals with no shared word or entity ("moved off Postgres" vs "the DB is
DuckDB"). Entity matching is also coarse, because `mentions` is per session rather than per fact. Build
this first and measure the miss rate on a hand-labeled sample. Add embeddings only if that justifies
them.

## Benchmarks

**LongMemEval and LoCoMo.** Published results are 473/500 and 1,417/1,540
([manifest](https://github.com/vectorize-io/agent-memory-benchmark/blob/28b644bd4dd653c0207c7507280439390c829155/results-manifest.json)).
One Gemini model answers and a second Gemini call judges against gold answers. Retrieved context
averages 36-44k tokens per query. Their [blog](https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark)
says naive context stuffing now scores competitively on these datasets.

**sdebench, the coding benchmark** ([dataset](https://github.com/vectorize-io/sde-bench),
[harness](https://github.com/vectorize-io/agent-memory-benchmark/blob/28b644bd4dd653c0207c7507280439390c829155/sdebench/harness/run.py)).
Both repos are Vectorize's own.
- 61 bug-fix tasks, all in one repo (boltons), 52 of them in modules Vectorize planted.
- The obvious fix passes the visible test and fails a hidden one. The deciding rule sits in a commit
  (28 tasks), a chat (27) or an amended chat (6), among 140 model-written decoy chats.
- Docker, 3 runs per setup, Claude Sonnet for the Claude Code setup.
- pytest is the judge. "Corrections" are simulated: on failure, test output goes back to the agent, up
  to 5 times.

We recomputed their Claude Code numbers from the published output files, and they match: corrections
0.85 to 0.36, cost per task $0.445 to $0.338.

| Decision source | No memory | Hindsight |
|---|---|---|
| Git history | 0.20 | 0.10 |
| Chat | 1.42 | 0.53 |
| Amended chat (n=6) | 1.28 | 0.83 |

Weaknesses: the vendor owns the harness, the data and the winning product; tasks are built so memory
matters; one repo, 3 runs, no confidence intervals; test output leaks the answer, so 60-61 tasks get
solved either way; and the headline cost leaves out memory's own bill.

**For a CSA benchmark.**
- Copy: tests as judge, machine-proven traps, amended decisions, decoys, per-category results,
  published trajectories.
- Do not copy: vendor-only planted tasks, a single repo, cost without the memory bill.
- Add what theirs does not measure: a decision made by person A and needed by person B on another
  machine; attribution on person-scoped questions; two people disagreeing, where newest is not
  automatically right; lag from session end to recall; quote validity; secret leaks; tenant isolation.

## Learning from git history

At each session start, a detached process reads the last 300 non-merge commits (short hash, date,
author name, subject, body, no diffs) and sends them as one document
([git.ts](https://github.com/vectorize-io/hindsight/blob/f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24/hindsight-integrations/coding-agents/src/core/git.ts#L316)).
It is safe to repeat and only does missing work. An optional `full` mode adds author email and the
uncapped diff per commit, 50 commits per run; the code calls it "Expensive". PRs are not read. The
client calls no model. Comments say the server extracts after `retain`; we did not trace that step. So a
new repo is useful before any session has shipped. Cost is not published.

## One installer

`npx @vectorize-io/hindsight-coding-agents install all` is one command that filters per-tool adapters to
the tools detected on the machine
([installer.ts](https://github.com/vectorize-io/hindsight/blob/f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24/hindsight-integrations/coding-agents/src/installer.ts#L2479)).
Each adapter writes that tool's native config and keeps a backup, and `uninstall` removes only what was
added. For CSA, this only matters once more harnesses ship.

## Install and trust

| | Hindsight | CSA Go client |
|---|---|---|
| Install | npm package; npm signatures and SLSA provenance | `curl \| sh`, unsigned binary, no Apple notarization |
| Runs on each hook | Readable bundled JS: `node <file>.js`, calling their HTTP API | Compiled Go |
| Consent screen | None | Yes, versioned |
| Redaction in Claude Code hooks | None; server-side scrubbing is opt-in per bank | On the laptop |
| Auto-update | Daily, on by default | Only when the user runs `csa-client update` |
| Sent | Prompts; on Stop, user and assistant text plus tool name and target; commit messages | Redacted transcript; redacted prompt query |

Developers distrust a compiled binary they cannot read, even with better protections. Copy the
packaging: readable code, a known registry, exact uninstall. Keep the redaction and consent, and do not
copy unattended auto-update.

## Harness notes

| Tool | What Hindsight does | Note for CSA |
|---|---|---|
| Codex CLI | Hooks in `~/.codex/hooks.json`, MCP in `config.toml`, imports past sessions | Easiest next tool |
| Cursor CLI | `~/.cursor/hooks.json` and `mcp.json`; history in SQLite, so captures on Stop | No backfill |
| GitHub Copilot | CLI hooks in `~/.copilot/hooks/`; VS Code gets only MCP and instructions | Recall differs by surface |
| OpenCode | Plugin entry in `opencode.json`, native tools | History in SQLite |
| Aider | Wrapper command, memory file via `--read` | Recall once per session |
| Gemini CLI | Not supported | Parked: no demand, and consumers moved to Antigravity |

## Not verified

Hindsight Cloud's prices and models; their benchmark numbers on an independent rerun; the net cost of
memory in sdebench; `gpt-4o-mini` pricing and real token counts in our estimates; what `haiku` resolves
to; HNSW index size; whether the Postgres path declines `full_recall`; CSA's real miss rate; the depth of
the check on Hindsight's hook-level redaction, which was searched and not audited. Re-check all of this
before making any public claim about Hindsight.
