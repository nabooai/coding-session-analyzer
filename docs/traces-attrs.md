# Spans attrs contract (v1)

The `spans` table (`models.spans`, in the telemetry file): `trace_id, span_id, parent_id, name, kind, at, dur_ms, status, attrs`.
`kind` is the OpenInference value (`CHAIN|LLM|RETRIEVER|RERANKER`); `attrs` is a flat JSON object using
the names below. **These names are the contract — the Search page reads them; do not rename.**
Same attrs ride the OTLP export (Phoenix) verbatim.

## Every span
- `status`: `OK` | `ERROR` (column, not attr) · on ERROR the attrs carry `error.message` (truncated).
- Ordering: `at` is second-granularity; within a trace, rows are inserted in span-completion order,
  so order by `rowid` (children precede their parent). Nanosecond times ride the OTLP export only.

## Root spans (one per request; `parent_id` NULL)
| name | kind | when |
|---|---|---|
| `ask` | CHAIN | MCP `ask` tool call |
| `recall.hook` | CHAIN | ambient hook recall (apiserver) |
| `ingest` | CHAIN | one session's extract→index (ETL drain) |
| `judge` | CHAIN | post-drain autojudge, per session |

Root attrs (`ask` / `recall.hook`):
- `input.value` — the question (same redaction policy as the `queries` log on that path)
- `output.value` — the answer served, capped 2000 chars (ground truth for "user saw wrong")
- `session.id`, `user.id`, `csa.org_id`, `csa.entry` = `mcp_ask|hook|cli`
- `csa.outcome` = `served|table|card|miss|error`
- `csa.resolved_ids` — entity short-circuit hits (comma, capped); explains the `card` outcome
- The matching `queries` row carries the same `trace_id` (nullable column, additive).

## `card` (TOOL, child of ask) — the entity short-circuit
- `csa.resolved_ids` — resolved entity ids (comma, capped)
- `db.table` = `pr_links` — present ONLY when the pr_links provenance card fired (keyed on the
  card's own evidence marker); the trace proof that "which session created PR #N" was answered
  deterministically, before the planner (research golden case 7 keys on this span)
- `output.value` — the card text (capped 1000)

## `route` (CHAIN, child of ask)
`csa.route.scope` (`self|person|team`) · `csa.route.shape` (`lookup|relational|locate|miss`) ·
`csa.route.person` · `csa.route.escalated` (bool) · `csa.route.missed_ids`

## RETRIEVER spans: `retrieve.fts` / `retrieve.sql` / `retrieve.<engine>` / `digest`
- `csa.engine` = `fts|sql|hook|digest`, or the name the planner emitted for an engine this server
  does not run
- `db.statement`: the planned SQL or FTS match string (the founder's "verify it ran SQL on
  pr_links" attr)
- `csa.row_count` · `csa.result_ids` (sql only: distinct first-column values reached, same
  cap/semantics as `queries.result_ids`; on the fts path the reached set is `reranker.output_documents`)
- `csa.date.since` / `csa.date.until` / `csa.project` — the hard filters actually applied
- `csa.executed` = false when the planner named an engine this server does not run (returned un-run,
  not zero rows; the caller answers from lexical recall)
- `csa.index.rebuilding` = true when docs was mid-rebuild (empty result is infra, not truth)
- `csa.retry.attempt` = 1|2 — self-repair: attempt 1 keeps `status=ERROR` + `error.message`,
  attempt 2 is a sibling with the same name

## RERANKER span: `rerank`
- `reranker.top_k` · `csa.input_count` (candidates in) · `csa.output_count` (survivors out)
- `reranker.output_documents` — JSON, capped at the TOP 10 survivors regardless of k (payload cap):
  `[{"id", "score", "rel", "rec", "ov", "corrob"}]` — the score breakdown rerank already computes;
  a composite score alone explains nothing
- `csa.input_count>0` with `csa.output_count==0` ⇒ the relevance floor buried everything

## LLM spans: `llm` (child of whatever stage spent it: route/planner/extract/judge/answer)
- `llm.model_name` — the REAL dated model id from the CLI's `modelUsage` envelope when available
  (Phoenix/Langfuse price books key on it; verified: Phoenix cost rollup goes non-zero), else the alias
- `llm.provider` (`claude|openai`)
- `llm.token_count.prompt` · `llm.token_count.completion` · `llm.token_count.prompt_details.cache_read`
  · `llm.token_count.prompt_details.cache_write` (without cache_write a first call reads "2 prompt
  tokens" and external dollar rollups run ~27x low on cache-heavy calls — both measured)
- `csa.cost_usd` · `csa.num_turns` (top-level key in `claude -p --output-format json`; verified live)
- `output.value` (capped 1000) · `csa.prompt_chars` — never the full prompt body (size + a fresh
  redaction surface for zero debug value)
- Cold-fill extractions run on pool threads; `tracer.capture()`/`resume()` hands the ask's context
  across, so their `extract`→`llm` spans join the ask trace (parented under `recall`), written as a
  fragment — same trace_id as the `costs` rows those calls bill.

## `recall` (CHAIN — the assembly stage; child of ask/hook root, or its own root from the CLI)
- `csa.k` · `csa.person` · `csa.digest` (bool) · `csa.locate` (bool)
- `csa.top_score` — top hit's rerank score (reported on abstains too; threshold refits need negatives)
- `csa.served.refs` — refs actually rendered (capped 40); joins `served` via `trace_id`
- `csa.n_served`
- `csa.miss_reason` (only on abstain/miss) =
  `person_unknown | no_candidates | ambient_floor | coverage_floor | empty_date_window |
   person_no_activity`
  — "rerank buried it" and "index mid-rebuild" are NOT enum values; derive them from the same
  trace: retriever `csa.row_count>0` + rerank `csa.output_count==0` ⇒ rel floor;
  `csa.index.rebuilding=true` ⇒ infra. "LLM ignored it" is the residual (served.refs non-empty +
  output says don't-know).

## Ingest trace (`ingest` root → `extract` → `index` children; `judge` separate trace)
- root: `session.id` · `csa.org_id` · `csa.ingest.n_facts` · `user.id` · `csa.project`
  (outcome = the span's `status` + the drain log; quarantine/systemic classification lives in the
  drain, not the trace)
- `extract` child carries the `llm` span
- `judge` root: `session.id` · `csa.org_id` · `csa.judge.n_reviewed` (ETL drain and apiserver
  autojudge/boot-sweep paths both)
- The `costs` rows for extract/judge LLM calls carry the same `trace_id`.

## Existing tables (additive, nullable)
`queries.trace_id`, `served.trace_id`, `costs.trace_id` — stamped automatically from the active trace.

## Scope notes (audited by user-sims 2026-07-31)
- `user.id` is present in team/HTTP mode (the gate's verified email). stdio local mode has no
  verified identity — absent there, and `session.id` is a per-process random id.
- The `costs` ledger covers extract/judge steps only (pre-existing billing scope); planner/route/
  answer spend lives on their `llm` spans (`csa.cost_usd`) with no ledger row.
- Locate and table answers write no `served` rows (deliberate — they serve pointers, not facts), so
  they carry no `csa.served.refs`.
- `queries.row_count` counts RENDERED lines; the retriever span's `csa.row_count` counts engine
  rows. They legitimately differ (e.g. a 1-row COUNT(*) rendered as a 6-line card).
- `costs.at` is `YYYY-MM-DD HH:MM:SS` (sqlite datetime('now')); `spans.at`/`queries.at` are ISO-Z.
  Time-window joins across them must normalize (pre-existing wart).

## OTLP export
- Encoding: OTLP/HTTP **protobuf** (spec-mandatory; Phoenix 415s the optional JSON encoding).
- Resource attrs: `service.name=csa` + `openinference.project.name=csa` (fixed, `analyzer/tracer.py:305`).
  Phoenix routes projects on the latter, not on service.name (verified live).

## Deliberately NOT in v1 (add when a consumer needs it)
A separate serve span (root attrs carry it), full prompt bodies, per-candidate factors beyond
top-10, judge sub-metrics, reject-at-ingest traces for `min_user_msgs` sessions (follow-up — needs
an apiserver ingest-route span; the Sessions page "attempted, rejected because X" story).
(TOOL kind was originally cut as YAGNI; the research referee's golden case 7 turned out to be a
real consumer, so the `card` span above carries it.)
