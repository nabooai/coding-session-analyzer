# Task: Native OTel-shaped traces — spans table + OTLP export, no SDK

**Status:** ready to start · **Owner:** unassigned (hand to a fresh Claude Code session)
**Motivation/benchmark:** the founder wants professional traces per question — model, cost, turns,
di-graph of actions, tools, reasoning — to verify e.g. that "which session talked about PR 123" ran
SQL on `pr_links`. Today that evidence is scattered across three disconnected tables (`queries`,
`costs`, `served`) with no parent/child structure and no UI. This task creates the single trace
substrate that both the future product Search page and the research env's Phoenix consume.

## Working protocol (binding — read before the code)

1. Work in a **separate git worktree** off the default branch — use the **`/goal`** flow to bootstrap it
   (worktree + worklog.md; overwrite any inherited worklog). Never work on a shared checkout.
2. Before implementing, **spawn a Senior SWE subagent AND a Senior PM subagent concurrently**: the SWE
   challenges the design below against the codebase (especially MCP stdout discipline and thread
   safety); the PM challenges which spans a human debugging a bad answer actually needs. Reconcile,
   then build.
3. After implementing, **launch 3+ concurrent "user" subagents**: one asks real questions through the
   MCP `ask` path and inspects the resulting trace via SQL for completeness; one runs with
   `CSA_OTLP_ENDPOINT` pointed at a local Phoenix and confirms the tree renders with costs; one
   hammers concurrent asks and checks traces don't interleave/corrupt. Fix friction, re-run.
4. Use **`/loop`** for the concurrency soak and any long replay instead of foreground waits.
5. **Follow AGENT_CHECKLIST discipline** — `docs/development/checklist.md`.

## Research conclusions (verified 2026-07-31 — do not redo)

- **No SDK.** Emit OTLP/HTTP **protobuf** with stdlib `urllib` from a background batch thread.
  CORRECTION (verified live 2026-07-31 against Phoenix 19.11.0 by research-env-golden-cases):
  Phoenix's `POST /v1/traces` is protobuf-only — OTLP/HTTP **JSON returns 415** Unsupported Media
  Type, and a naive implementation would count every export as failed while looking wired. A
  working ~60-line hand-rolled proto3 encoder (`_pb_varint`/`_pb_ld`/`_pb_f64`/`_pb_attr`/`_pb_span`)
  already exists in `research/run.py` — LIFT it into tracer.py (and have run.py import it back;
  product code must not import from research/). Two hard-won details inside it: span ids must be
  globally unique across runs (Phoenix silently DROPS duplicate span ids — mix the trace_id into the
  hash), and Phoenix routes projects on the resource attribute `openinference.project.name`, not
  `service.name`. Langfuse's SDK is OTel underneath, so the same emission config-swaps later.
  Adding an OTel/Langfuse dependency would break the no-key/no-dep client moat.
- **Span kinds**: use OpenInference `openinference.span.kind` values — CHAIN (route/classify),
  RETRIEVER (fts/sql/cypher engine run), RERANKER (search.rerank), LLM (`claude -p` calls), TOOL —
  they map 1:1 onto the pipeline and Phoenix renders them natively.
- **Cost/usage already parsed**: `analyzer/llm.py` returns `(text, cost_usd, usage)` — llm.py:49-50,
  :126 (`out.get("total_cost_usd")`, `usage` in Anthropic key shape; openai path normalizes,
  llm.py:152-154). `claude -p --output-format json` also carries `num_turns` — llm.py currently drops
  it; attach it to the LLM span attrs (verify the key in the CLI JSON, don't guess).
- **Existing partial substrate** (keep writers, link them): `queries` (store.py:39 — engine,
  query_text, planned_query, row_count, latency_ms, result_ids, acted_on; the schema comment there
  documents result_ids semantics), `costs` (store.py:34), `served` (store.py:35). `log_query` at
  store.py:505-517; hook-path caller apiserver.py:791.
- **The ask pipeline to instrument**: `route.classify` (analyzer/route.py:288-331 scope; :217
  date_window), engines in `analyzer/ask.py` (:184 returns `(engine, planned_query, cols, rows)` with
  ONE self-repair retry — the retry is its own span), `search.rerank` (search.py:200-256, score line
  :232-233), recall assembly `app.py:164-538`.
- **MCP discipline**: the MCP server speaks JSON-RPC on stdout — the tracer must NEVER print
  (ask.py:184 notes "Quiet (no stdout — MCP protocol)"). Failures swallow silently into a counter.
- **Trap**: any NEW POST route must join BOTH allowlists — `do_POST` dispatch (apiserver.py:1552,
  :1573) and `_COOKIE_POST_OK` (apiserver.py:68). This task needs no new POST route (spans are written
  server-side; the future UI reads via GET) — keep it that way.

## Deliverables

1. `server/analyzer/tracer.py`: `span(name, kind, **attrs)` context manager; thread-local span stack
   (each request/ingest thread gets its own tree); trace_id/span_id via `uuid4().hex`; always writes
   the `spans` table; when `CSA_OTLP_ENDPOINT` is set, a daemon batch thread POSTs OTLP protobuf
   (encoder lifted from research/run.py — JSON gets 415, see Research conclusions; batch ≤100 spans /
   flush ≤5s), failures counted not raised. Self-check `__main__`.
2. `spans` table migration in store.py SCHEMA (DDL in fact-core-v2.md §Schema v2) + index on trace_id.
3. Instrumentation of the ask/recall path end-to-end: one trace per ask — CHAIN(route) →
   RETRIEVER(engine, planned_query, result_ids, row_count) → RERANKER(top refs + scores) →
   LLM spans (model, input/output/cache tokens, cost_usd, num_turns) → serve (refs served).
4. Instrumentation of the ingest pipeline stages (extract → load → index → judge) with session_id attr.
5. `trace_id` column added to `queries`, `served`, `costs` writers (nullable, additive migration).
6. Retention: spans pruned with the same policy family as `queries` (config `CSA_SPANS_KEEP_DAYS`,
   default 30) — a growing-forever table on the lab box is a regression.

## Acceptance (all must pass)

- [ ] One MCP `ask` produces one connected tree in `spans`: single trace_id, every parent_id resolves,
      depth ≥3 (chain→retriever→…), and the matching `queries` row carries the same trace_id.
- [ ] LLM spans carry model + all three token counts + cost_usd (and num_turns when the CLI provides
      it) for a real `claude -p` call — verified against the `costs` row.
- [ ] With `CSA_OTLP_ENDPOINT` set to a local Phoenix (or the stub server in the self-check), spans
      arrive and render as a tree; kill the endpoint mid-run → asks still succeed, failure counter
      increments, zero raised exceptions.
- [ ] **Negative:** with no `CSA_OTLP_ENDPOINT`, zero outbound network attempts (assert in self-check
      by monkeypatched urlopen).
- [ ] **Negative:** MCP stdout byte-identical with tracing on vs off (protocol untouched).
- [ ] 20 concurrent asks (the soak) → 20 disjoint well-formed trees, no cross-linked parents.
- [ ] Per-span overhead <5ms p50 in the self-check microbench.
- [ ] `eval_recall.py` still exits 0 (serving behavior unchanged).

## Pointers

- Master design: `research/fact-core-v2.md` §Traces. Consumers: `docs/tasks/research-env-golden-cases.md`
  (Phoenix sink), `research/tasks/taint-confidence-search-page.md` (the Search page UI reads `spans` —
  coordinate the attrs contract with that doc).
- `server/analyzer/llm.py:49-126`, `ask.py:184`, `route.py:288-331`, `search.py:200-256`,
  `app.py:164-538`, `store.py:34-39,505-517`, `apiserver.py:68,791,1552`.
