# Fact Core v2 — time, trust, supersession, traces

Design + implementation plan from the 2026-07-31 research round (Graphiti mechanics, truth-discovery
literature, prior-art authority heuristics, Langfuse/Phoenix evaluation). Covers every agreed item; the
appendix maps each discussion bullet to its section so nothing silently drops.

## The problems

1. **Unreliable source** — a fact's weight ignores who asserted it and their standing in that domain.
2. **Temporal validity** — "use SQLite not PG" was true *at a point in time*; nothing models when a
   fact stopped being true. Serving a reversed decision as current is our worst failure mode
   (`app.py:386` already says so).
3. **Supersession** — newer facts/decisions must override older ones, visibly, with the loser kept.
4. **Entity noise** (user report) — per-session entity extraction emits many entities that are never
   relevant again and pollute serving.

## Locked decisions (from research)

- **Sessions are episodes.** Transcripts are retained, every fact points to its source session(s) and
  carries an evidence quote — that is Graphiti's episode contract; we formalize it, we don't rebuild it.
- **Four temporal fields on facts** (Graphiti's bi-temporal model): `created_at`/`expired_at` (system
  timeline), `valid_at`/`invalid_at` (world timeline). `invalid_at IS NULL` *is* "still current" — the
  base representation even though point-in-time queries are not a v1 goal.
- **Detection is semantic, resolution is deterministic.** One LLM call detects "these facts conflict";
  a pure function of fact metadata resolves the winner (arXiv 2606.01435: deterministic beats
  LLM-judged freshness by 10–21 pts; also gives convergent re-runs).
- **Supersede = edge + closed validity window, never delete, never bare-hide.** Wikidata-rank
  semantics: current (preferred) / superseded (deprecated-with-reason) / conflict (both served,
  bannered).
- **Authority is per-(person, domain), binned RED/YELLOW/GREEN** — port of prior-art
  `project_distance` (self-relative quantiles + recency override both directions + unknown→RED),
  with a tiny declared org overlay that inference can't learn (CEO ≠ code authority).
- **Registries beat everything.** Facts checkable against a system of record (org roster, GitHub org,
  git history) resolve deterministically — no LLM, no votes. Absence from a registry counts only when
  the registry is known complete (a prior-art `_assess_employment_likelihood` gate).
- **No labels required.** The labeling economy failed (interview round); trust derives from implicit
  signals only. Human input is reserved for the (small) conflict queue.
- **Keep the stack, restage the pipeline.** SQLite + stdlib is the moat; the defect is structure
  (durable/derived blur, no stages, batch-time invariants). NOT adopting: embeddings, TruthFinder
  fixpoint iteration, Prefect/Neo4j, Langfuse self-host on the lab box (6 containers / 8–16 GiB,
  ClickHouse mandatory).
- **Instrument once with OTel semantics, two sinks:** spans always land in our own SQLite (powers the
  product Search/trace page — must work in every tenant compose); OTLP-export to Arize Phoenix (one
  container, SQLite backend, ~2–3 GB, ELv2 = fine internally) on the research box. Langfuse remains a
  config-swap option later — MIT, ingests exact `claude -p` costs — if a beefier box ever exists.

## Schema v2 (durable core)

Additive migration in `store.py` (SCHEMA + migration list). Durable tables below; everything derived
(`claim_clusters`, `docs` FTS, rollups, taint) stays rebuildable and is never the system of record.

```sql
-- facts become first-class rows; hidden_context rows become per-session MENTIONS of a fact
CREATE TABLE IF NOT EXISTS facts(
  ref TEXT PRIMARY KEY,          -- existing content-hash id; carry_refs still applies to mentions
  claim TEXT, type TEXT, scope TEXT,
  author TEXT,                   -- sessions.user of the FIRST asserting session
  domain TEXT,                   -- code-home repo when present, else session project
  created_at TEXT,               -- system: first extraction (min mention date)
  valid_at TEXT,                 -- world: became true (default created_at; extraction as_of overrides)
  invalid_at TEXT,               -- world: stopped being true; NULL = current
  expired_at TEXT,               -- system: when we retired it (may lag invalid_at)
  state TEXT DEFAULT 'extracted',-- extracted|verified|unverifiable|current|superseded|conflict
  confidence REAL);              -- derived, recomputed by the resolve stage

CREATE TABLE IF NOT EXISTS fact_edges(       -- durable; NEVER wiped by index passes
  src_ref TEXT, rel TEXT, dst_ref TEXT,      -- rel: supersedes|depends_on|contradicts|derived_from
  at TEXT, actor TEXT, note TEXT,            -- note: LLM rationale / registry evidence (the receipt)
  PRIMARY KEY(src_ref, rel, dst_ref));

CREATE TABLE IF NOT EXISTS registry(         -- synced systems of record
  kind TEXT, key TEXT, value TEXT,           -- (role, priya@…, junior-manager) / (member, jules@…, github:acme)
  as_of TEXT, source TEXT,                   -- source: sso|github|git|okta…
  PRIMARY KEY(kind, key, source));

CREATE TABLE IF NOT EXISTS authority(        -- derived rollup, rebuilt by ETL, kept for serving speed
  user TEXT, domain TEXT, provider TEXT,     -- provider: activity|git  (the A/B axis)
  n INT, first_at TEXT, last_at TEXT, bin TEXT,   -- bin: red|yellow|green
  PRIMARY KEY(user, domain, provider));

CREATE TABLE IF NOT EXISTS author_ledger(    -- durable per-author track record
  user TEXT, ref TEXT, kind TEXT, at TEXT,   -- kind: authored|reaffirmed_by_other|acted_on_by_other|
  PRIMARY KEY(user, ref, kind));             --       superseded|refuted|flag_confirmed|flag_extractor_error

CREATE TABLE IF NOT EXISTS spans(            -- OTel-shaped trace store (product sink)
  trace_id TEXT, span_id TEXT, parent_id TEXT,
  name TEXT, kind TEXT,                      -- kind: CHAIN|LLM|RETRIEVER|RERANKER|TOOL (OpenInference)
  at TEXT, dur_ms INT, status TEXT,
  attrs TEXT);                               -- JSON: model, tokens, cost_usd, num_turns, engine, planned_query, result_ids…
CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id);
```

Org config (the 5-line overlay), in the existing per-org config next to `custom_entity_types`:

```yaml
authority_overrides:
  sam@example.com: [business]        # declared beats inferred; carries what activity can't know
  jules@example.com:  [acme-ai, ledger]
```

Mentions: `hidden_context` keeps its shape and becomes the mention/evidence table (fact ref ↔
session, per-session date, evidence quote). `carry_refs` keeps operating on mentions; it no longer
re-dates the fact — `facts.created_at` stays min(mention dates). Fixes "one fact competes with itself
at N ages" (docs indexes one row per fact) and the silent re-dating.

## Pipeline restaging (the ETL answer)

Per-fact state machine: `extracted → verified|unverifiable → current|superseded|conflict → indexed`.
Each stage incremental, idempotent, and wrapped in a span. Stages run at INGEST (when the graph can
first become wrong), not at periodic-batch time.

- **S1 extract** (`extract.py`, exists) — add optional `as_of` ("the transcript states when this became
  true, if it does"). Bump `SCHEMA_VERSION` → 2, consolidating the unbumped additive fields
  (`repo`/`path`) in the same move. Cached extractions stay valid; only new sessions emit `as_of`.
- **S2 upsert** (`store.py`) — create/refresh the `facts` row, insert mention, `author_ledger(authored)`;
  reaffirmation signal becomes **distinct-author-aware** (N sessions by one person ≠ N sources —
  the truth-discovery popularity≠truth fix).
- **S3 verify** (`verify.py`, new) — fact classes with a registry mapping get checked: `verified`
  (confidence floor) / `refuted` (sets `invalid_at` = registry `as_of`, edge `supersedes` from a
  synthetic registry-fact, receipt in `note`) / `unverifiable`. Registry *sync* is a separate periodic
  job: roster from SSO (`hd` claim/orgs), `gh api /orgs/{org}/members`, `git log` rollups per repo.
- **S4 contradict** (`contradict.py`, new) — for new `decision`/`constraint` facts only: candidates =
  facts sharing (non-noise) entities via `mentions`/`relations` ∪ FTS top-k same-type. One haiku call:
  "does the new fact reverse any of these?" → `contradicts` edges + rationale. Bounded: one call per
  new decision, candidates capped. (This replaces the lexical-cluster blindness: reversals share
  entities even when they share no tokens.)
- **S5 resolve** (`resolve_rules.py`, new, pure function + selfcheck) — rules R0–R6 below. Writes
  `supersedes` edges, closes `invalid_at`/`expired_at`, sets `state`, recomputes `confidence`.
- **S6 index** (exists) — `docs` FTS becomes one row per FACT (validity/state columns included);
  `write_clusters` stays derived; taint (below) computed here and cached.
- **Retired:** `judge.py` staleness step (S4+S5 replace it — it was cluster-local, lossy, batch-time).
  The reaffirm signal moves into S2. Existing `note='stale'` reviews are converted where the winner is
  identifiable (newest cluster member), else left as-is.

## Resolution rules (deterministic, total)

Given conflicting facts OLD and NEW on the same subject:

- **R0 Registry wins.** If a system of record answers, that's the answer. No vote, no LLM.
- **R1 Self-supersession.** Same author → later `valid_at` wins automatically.
- **R2 Recency within authority.** NEW author's bin (in the fact's domain) ≥ OLD author's → NEW wins:
  `supersedes` edge, OLD.`invalid_at` = NEW.`valid_at`, OLD.state = superseded.
- **R3 The authority gate.** NEW author's bin < OLD author's → **no auto-supersede**: `contradicts`
  edge, both state = conflict, both served with a conflict banner, queued for human review.
  (Employee's "off-board client" doesn't silently beat the CEO's "onboard"; Sam's database remark
  doesn't beat Jules's — Sam is RED on `acme-ai`.)
- **R4 Bins** from the authority module; declared org overlay forces GREEN for listed domains.
- **R5 Cold start.** Unknown/new author = YELLOW (CATD shrinkage: neutral prior, never zero).
  RED means *known absence* (has history, none in this domain).
- **R6 Total order.** Ties: (valid_at, created_at, ref) lexicographic — CRDT-style convergence.

## Authority module (`authority.py`, new)

- Rollup: `SELECT user, domain, COUNT(*), MIN(started_at), MAX(ended_at) FROM sessions … GROUP BY 1,2`
  where domain credit follows the fact's `repo` code-home when present (the prior art's `pr_related`
  cross-domain credit: working on `ledger` from any cwd builds `ledger` authority).
- Binning: a prior-art `project_distance` port (~40 lines): quantile bins over the user's OWN distribution;
  active-in-domain < 3 months ∧ n>1 → GREEN; GREEN-by-volume but idle > 3 months → YELLOW;
  never seen → RED. No cross-user normalization.
- Providers behind one interface — the A/B: `activity` (sessions + acted-on ledger; v1, zero new
  deps) vs `git` (git log/blame per repo, DOA-style; v2). Compared in the research env on conflict
  golden cases; metric = agreement with ground-truth resolutions. Ship the winner, keep the interface.
- **Consumer ships in the same change** (R2/R3 gate + capped serving multiplier) — the prior art's
  `user_distance` shipped as a dead always-None field for a year; we don't repeat that.

## Confidence without labels

`confidence = f(signals) × authority_bin_factor`, deterministic, recomputed in S5 (TruthFinder-lite:
one round, no fixpoint — iteration at team scale just re-derives "who talks most"):

1. registry `verified` (floor high) / `refuted` (floor zero)
2. reaffirmed by **distinct authors** (strongest organic signal)
3. `acted_on` by others (existing heuristic lower bound)
4. survival (served N times, never contradicted — weakest, small)
5. negatives: superseded, conflict, confirmed-wrong flag

Flag path (KBT lesson): on a `wrong` flag, one cheap check "does the evidence quote support the
claim?" — mismatch ⇒ `flag_extractor_error` (author untouched, extraction bug ledger); match ⇒
`flag_confirmed` (author track record moves). Extractor errors must never dock authors.

## Entity noise gate

- Derived `entity_stats(entity_id, sessions, distinct_authors, dominant_n, last_seen)` at S6.
- **Serving floor:** entities with 1 session, never dominant, never acted-on are excluded from recall
  graph-overlap scoring, entity pages, and S4 candidate retrieval (keeps candidate sets small and
  clean). They remain in the durable store for provenance; the gate is serving-side and reversible.
- Periodic GC demotes singletons older than N days from the serving index only.
- Extraction side: default entity-type descriptions get a "durable-referent only" tightening,
  validated in the research env before shipping (per-tenant `custom_entity_types` already lets
  tenants override).

## Propagation (taint)

When X becomes superseded/refuted: facts with an explicit `depends_on` edge to X, plus (v1 heuristic)
decisions/gotchas whose **dominant** entities are the object of X's claim, get a computed taint —
served with *"⚠ depends on a superseded fact: [X → superseded by Y]"*. One hop, decision/gotcha types
only, computed at serve/index time from durable edges — no cascade storms, nothing hidden. Answer to
"do we detach A when B is superseded?": **no — A stays served, visibly doubted, and a taint on a
high-confidence A also enqueues it for the conflict queue** so a human can either re-affirm A
(clears taint) or retire it. `depends_on` edges are written when extraction evidence references
another known fact (S2 best-effort) and by humans from the UI.

## Serving semantics

- Default recall: `invalid_at IS NULL` facts. Superseded facts appear only when directly relevant,
  always with "superseded by [winner] on [date]" (they answer "why" and point-in-time questions).
- Conflict facts: both sides served with the conflict banner until resolved.
- Ranking: keep the 90-day recency decay, keyed on the fact's LAST mention date (revised 2026-07-31
  during the schema-v2 build, from "keyed on valid_at": decay-on-valid_at demotes exactly the facts a
  team keeps reaffirming — a standing rule first asserted 180 days ago and reaffirmed weekly would lose
  ~25% of its score, more than the corroboration cap can return — and an `as_of` back-date would bury an
  honest fact at birth. The staleness this clause was buying is carried by the validity filter itself:
  once superseded facts are excluded/annotated, freshness-by-last-reaffirmation is safe). `valid_at`
  still rides every payload for filtering/annotation; corroboration counts distinct authors; confidence
  and authority enter as small capped multipliers alongside the existing TYPE/TRUST/STATUS weights.
- Supersession closes CLUSTERS, not single refs: `ref` is a content hash, so the same fact usually
  exists as N paraphrase refs in one `claim_clusters` cluster (measured: 0 of 1,168 local refs shared,
  35 multi-session paraphrase clusters). Invalidating one wording while recall serves a sibling as
  current is a hole exactly the width of the extractor's paraphrasing — the resolution engine must
  close (or at least taint) the loser's whole cluster; `cluster_id` ships in the facts payloads for it.
- **Conflict review queue**: Operator/Team page section listing `contradicts` pairs + tainted
  high-confidence facts, one-click resolve (supersede / keep both / reaffirm). This is the ONLY human
  labeling surface — small by construction. Its POST route must join BOTH allowlists
  (`do_POST` + `_COOKIE_POST_OK`) — the known trap.

## Traces (product) + Search page

- `tracer.py` (new, stdlib): context-manager spans → `spans` table always; when `CSA_OTLP_ENDPOINT`
  is set, batch-POST OTLP/HTTP **protobuf** via urllib (no SDK dependency — keeps the no-key moat;
  Phoenix rejects OTLP JSON with 415 — lift the proto3 encoder from `research/run.py`, see
  otel-traces-native.md). OpenInference span kinds so Phoenix/Langfuse render natively.
- Instrumented: route.classify → engine runs (`ask.py`) → planned SQL → search+rerank → serving;
  every `llm.py` call attaches model, tokens, cost_usd, num_turns from the `claude -p` JSON (already
  parsed). Extraction pipeline stages S1–S6 also spanned. Existing `queries`/`costs`/`served` writers
  stay (backcompat) and gain a `trace_id`.
- **Search page** (UI): per-question trace — tree/DAG of spans (SVG from parent links), model + tokens
  + cost + turns per step, the planned SQL, result ids reached, acted-on outcome, and the LLM
  rationale notes. This is the product answer to "verify that 'which session talked about PR 123' ran
  SQL on pr_links WHERE pr=123" — that exact case already exists in `eval_recall.py` (locate shape).

## Research environment (the referee)

`research/` on the dev box:

- `snapshot.sh` — freeze a copy of the org DB + selected transcripts (SQLite backup API).
- **Phoenix**: `docker run arizephoenix/phoenix` (one container, SQLite backend). Spans arrive via
  the same OTLP export; datasets + experiments + deterministic Python evaluators, runs compared in UI.
- `cases.py` — the golden set to win (ground truth: expected edges/state/route/tool), initial ~10:
  1. SQLite→DuckDB graph-store supersession (real, in corpus, already an eval question)
  2. "ingest always queues" supersedes the older ETL design (`design-next.md:216`)
  3. "Do we still use Prefect?" — current-check via `invalid_at`
  4. Seeded CEO-vs-dev conflict → must land in conflict queue, NOT auto-resolve (R3)
  5. Registry refute: role/employment fact vs roster (Maria case)
  6. Taint: gotcha depending on the SQLite decision surfaces the banner after supersession
  7. "Which session talked about PR 123" → trace shows sql engine on pr_links (locate)
  8. Entity noise: singleton entity absent from serving + from S4 candidates
  9. Authority A/B: conflict case where `activity` and `git` providers disagree — ground truth decides
  10. Extractor-error flag: evidence-claim mismatch must NOT dock the author
- `run.py` — replays pipeline stages on the snapshot, deterministic evaluators, exit codes
  (`eval_recall.py` pattern; eval_recall itself stays the question-routing harness and keeps gating).
- The retro contradiction sweep (below) runs here FIRST to measure LLM cost before prod.

## Migration & rollout

1. Additive schema + backfill `facts` from `hidden_context ⋈ sessions` (no LLM: author = first
   session's user, created_at = min date, valid_at = created_at, domain = repo|project).
2. Registry initial sync; authority rollup compute; org overlay config.
3. Retro contradiction sweep over existing decisions/constraints oldest→newest (bounded LLM cost,
   measured on the research snapshot first).
4. Convert `note='stale'` reviews → `supersedes` edges where the winner is identifiable.
5. Serving/ranking switch behind a flag until golden cases pass on the research env.
6. Lab deploy realities: build image on the box (arm64), replace `ui/dist` CONTENTS (bind-mount
   inode trap), new POST routes join both allowlists.

## Phases

- **P0 — Referee + quick wins:** research env (snapshot, Phoenix, cases, runner), `tracer.py` + spans
  + OTLP, entity-noise serving gate (immediate user pain, independent).
- **P1 — Fact core:** `facts` + four fields + `fact_edges` + backfill; mentions semantics; one-row-
  per-fact index; serving validity defaults.
- **P2 — Trust machinery:** registries + S3 verify; authority v1 (activity provider + overlay) with
  its R2/R3 consumer; S4 contradict + S5 resolve; conflict queue UI; retire judge staleness.
- **P3 — Inheritance + visibility:** taint propagation; confidence function into ranking;
  distinct-author corroboration; Search/trace page in the UI.
- **P4 — Comparative + hardening:** git authority provider + A/B on golden cases; extractor-vs-author
  flag attribution; migration-debt consolidation (SCHEMA_VERSION discipline, allowlist unification);
  optional Langfuse sink.

## Appendix — coverage map (discussion bullet → section)

| Discussed | Lands in |
|---|---|
| Facts have a time / four fields (`valid_at`…) | Schema v2, Locked decisions |
| Episodes / facts point to source episodes | Locked decisions (sessions=episodes), mentions |
| Supersession as edge, not hide | Schema (`fact_edges`), Pipeline S5 |
| Dependency propagation / partial supersede | Propagation (taint) |
| Ingest-time (not batch) contradiction | Pipeline S4/S5 |
| Entity+FTS candidates, LLM detects reversal | Pipeline S4 |
| Deterministic rules | Resolution rules R0–R6 |
| Authority-gated resolution (Sam case) | R3 + Authority |
| Per-(person,domain) authority from session aggregates | Authority (rollup + `project_distance` port) |
| prior-art `project_distance` / heuristics | Authority; anti-patterns noted |
| 5-line org config | Schema (org overlay), R4 |
| Merge-rate vs activity A/B | Authority providers + research case 9 |
| Okta/GitHub deterministic verification | Registries + S3 verify + R0 |
| How to confirm facts without labels | Confidence without labels |
| Users don't label / too many | Locked decisions; conflict queue = only human surface |
| Extractor error ≠ author error | Confidence (flag path), research case 10 |
| Entity noise from every session | Entity noise gate, research case 8 |
| Criticize supersession v1, rebuild from fundamentals | Pipeline (S4/S5 replace judge staleness; retirement step) |
| ETL structure not good enough | Pipeline restaging (state machine, durable/derived split) |
| Re-think tech stack | Locked decisions (keep stack, restage; explicit not-adopting list) |
| Research env + minimal use-cases to win | Research environment (10 cases) |
| Search page with professional traces (model/cost/turns/DAG/reasoning/evals) | Traces + Search page |
| Langfuse | Locked decisions + Traces (Phoenix now, Langfuse config-swap later) |
| Four fields even without point-in-time queries | Locked decisions (base representation) |
| Wikidata rank semantics | Locked decisions, Serving semantics |
| Superseded facts still answer "why"/history | Serving semantics |
| Distinct-author corroboration | S2, Confidence |
| Author track record | Schema (`author_ledger`), Confidence |
| Cold start neutral | R5 |
| Conflict review queue | Serving semantics |
| POST-allowlist trap | Serving semantics, Rollout |
| eval_recall golden set graduates | Research environment |
