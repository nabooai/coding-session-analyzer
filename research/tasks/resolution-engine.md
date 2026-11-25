# Task: Resolution engine — ingest-time contradiction + authority-gated deterministic supersession

**Status:** ready to start · **Owner:** unassigned · **Prereq:** `docs/tasks/fact-core-schema-v2.md`
merged (`docs/tasks/registries-verify.md` for R0 — stub the verdict lookup if it lags)
**Motivation/benchmark:** supersession v1 (judge staleness) is structurally blind and lossy: it only
compares facts inside a lexical near-dup cluster (≥50% token overlap ∧ ≥6 shared tokens — reversals
worded differently NEVER meet), resolves by hiding the loser as `reviews(wrong, note='stale')` with no
pointer to the winner, and runs as a periodic batch while the corpus serves the reversed decision as
current. This task replaces it with the fact-core-v2 core: detect semantically at ingest, resolve
deterministically, gate by per-(person, domain) authority, keep everything visible.

## Working protocol (binding — read before the code)

1. Work in a **separate git worktree** off the default branch — use the **`/goal`** flow to bootstrap it
   (worktree + worklog.md; overwrite any inherited worklog). Never work on a shared checkout.
2. Before implementing, **spawn a Senior SWE subagent AND a Senior PM subagent concurrently**: the SWE
   challenges candidate retrieval + rule totality against the codebase (every conflicting pair must
   hit exactly one rule); the PM challenges the conflict-queue UX (the ONLY human surface — if it
   nags, it dies like labeling did). Reconcile, then build.
3. After implementing, **launch 3+ concurrent "user" subagents**: one replays the real SQLite→DuckDB
   history and asks "what DB does ledger use?" expecting DuckDB + a visible supersession trail; one
   plays the low-authority reversal (Sam-vs-Jules both directions) and must land in the conflict
   queue, then resolves it from the UI; one floods ingest with restatements (NOT contradictions) and
   verifies no false supersessions. Fix friction, re-run.
4. Use **`/loop`** for the retro sweep (research env first for $ measurement, then prod) and
   simulation rounds.
5. **Follow AGENT_CHECKLIST discipline** — `docs/development/checklist.md`.

## Research conclusions (verified 2026-07-31 — do not redo)

- **v1 to retire**: `judge.etl_pass` staleness step (judge.py:163-210; STALE_PROMPT :33-41;
  `_stale_candidates` :141-160 pulls only multi-ref clusters from `dedup.cluster` — dedup.py:28's
  overlap-coefficient law is the blindness). Keep the REAFFIRM signal (moves to ingest, made
  distinct-author-aware by the taint/confidence task). Convert existing `reviews(status='wrong',
  note='stale')` rows to `supersedes` edges where the winner is identifiable (newest cluster member),
  else leave them.
- **Detection (S4) — semantic, bounded**: candidates for a new `decision`/`constraint` fact =
  facts sharing served (non-noise) entities via `mentions`/`relations` (use `is_served_entity` from
  `docs/tasks/entity-noise-gate.md`) ∪ FTS top-k same-type. ONE `llm.py` haiku call: "does the new
  fact reverse/supersede any of these? return indices + one-line rationale" (Graphiti's mechanism —
  arXiv 2501.13956 — with lexical/entity retrieval instead of embeddings; their recall is bounded by
  candidate retrieval, ours too: log candidate-set size, cap ~20).
- **Supersession closes CLUSTERS, not single refs** (added 2026-07-31 by the schema-v2 build): `ref`
  is a content hash, so one real-world fact is usually N paraphrase refs sharing a `claim_clusters`
  cluster (0 of 1,168 local refs shared; 35 paraphrase clusters). Closing one wording while recall
  serves a sibling as current defeats the whole mechanism — apply the loser's `invalid_at`/edges
  across its cluster (or taint the siblings). `cluster_id` already rides the /api/facts payload.
- **Resolution (S5) — deterministic, total** (arXiv 2606.01435: deterministic beats LLM-judged
  freshness by 10–21 pts). Rules R0–R6 verbatim in `research/fact-core-v2.md` §Resolution rules: R0
  registry wins → R1 same-author later-valid_at wins → R2 recency-within-authority → R3 lower
  authority does NOT auto-supersede (conflict state, both served bannered, queued) → R5 cold-start
  YELLOW → R6 (valid_at, created_at, ref) total order. Pure function of fact metadata — same inputs,
  same outputs, every re-run.
- **Authority — a prior-art `project_distance` port** (with a per-cluster activity rollup): bins from
  quantiles over the user's OWN per-domain counts (no cross-user normalization); overrides — active
  in domain <3mo ∧ n>1 → GREEN; GREEN-by-volume but idle >3mo → YELLOW; never seen → RED. CSA rollup
  = `SELECT user, domain, COUNT(*), MIN(started_at), MAX(ended_at) FROM sessions… GROUP BY 1,2` with
  domain = fact's `repo` code-home else session project (the prior art's `pr_related` cross-domain credit
  analog). Declared org overlay (5 lines, per-org config beside `custom_entity_types`) forces GREEN
  for listed domains — carries what activity can't know (CEO≠code). **Ship the consumer (R2/R3 gate +
  capped rank multiplier) in THIS task** — the prior art's `user_distance` shipped as a dead always-None
  field for a year (schema.py:1159 commented body); don't repeat it.
- **Serving**: superseded facts stay retrievable, rendered "superseded by [winner] on [date]" (docs
  columns land in `docs/tasks/fact-core-schema-v2.md`); conflict facts serve BOTH sides bannered.
  Ranking multiplier stays in-band with existing weights (search.py:8-17; corrob caps 1.3 — do not
  out-multiply exact lexical matches).
- **Conflict queue**: reviews-queue precedent exists; the POST route for resolve MUST join BOTH
  allowlists — `do_POST` (apiserver.py:1552,:1573) AND `_COOKIE_POST_OK` (apiserver.py:68) — the
  known two-allowlist trap.
- **Cost**: one haiku call per new decision/constraint only (not gotcha/todo/preference); retro sweep
  $ measured FIRST via `research/run.py --measure-sweep`
  (`docs/tasks/research-env-golden-cases.md`).

## Deliverables

1. `analyzer/authority.py`: rollup + bins + overlay + provider interface (`activity` provider now;
   interface consumed later by `research/tasks/authority-ab-hardening.md`). Self-check with a fixture org.
2. `analyzer/contradict.py` (S4): candidate retrieval + the one LLM call + `contradicts` edge writes
   with rationale notes; wired into ingest after fact upsert.
3. `analyzer/resolve_rules.py` (S5): pure `resolve(old_fact, new_fact, authority, registry_verdict) →
   action` + application (edges, invalid_at/expired_at, state, conflict queue rows). Property-based
   self-check: totality (every synthetic pair hits exactly one rule) + determinism (shuffled
   ingestion order converges to identical end state).
4. Conflict queue: GET list + POST resolve endpoints (both allowlists) + minimal UI section
   (Team/Operator page — repo's collapsed-tab layout; vitest for the component).
5. Judge staleness retirement + the stale-review conversion migration.
6. Retro sweep runner (research env first, then prod behind explicit `--yes-spend`).
7. Golden cases 1–4 flipped from xfail in `research/cases.py`.

## Acceptance (all must pass)

- [ ] Golden case 1: SQLite→DuckDB — old decision gets `supersedes` edge + `invalid_at` = new
      valid_at + state superseded; "what DB for the graph store?" serves DuckDB; the old fact is
      retrievable with its window and winner pointer.
- [ ] Golden case 3: "do we still use Prefect?" answers from validity, not vibes.
- [ ] Golden case 4 (**negative — the R3 gate**): RED-bin author's reversal of a GREEN-bin author's
      decision does NOT auto-supersede; both facts serve with conflict banners; the pair appears in
      the queue; resolving from the UI writes the edge and clears the banners.
- [ ] R1: same author reverses themselves → auto-supersede, no queue entry.
- [ ] **Negative:** a restatement (same meaning, different words) triggers NO contradiction edge
      (flood test from the user subagent), and re-ingesting the same session twice creates zero
      duplicate edges (idempotent).
- [ ] `resolve_rules` self-check passes: totality + order-shuffle convergence.
- [ ] Authority: fixture where the overlay forces GREEN for a zero-activity user in `business` works;
      unknown author bins YELLOW (**negative:** never RED-as-zero for a new hire).
- [ ] Cost log: S4 calls ≤1 per new decision/constraint, candidate set ≤ cap, visible in `costs`.
- [ ] `eval_recall.py` exits 0; judge etl_pass no longer writes `note='stale'` reviews.

## Pointers

- Master design: `research/fact-core-v2.md` §§Pipeline S4/S5, Resolution rules, Authority, Serving.
- Prereqs/siblings: `docs/tasks/fact-core-schema-v2.md` (DDL), `docs/tasks/registries-verify.md`
  (R0 verdicts), `docs/tasks/entity-noise-gate.md` (`is_served_entity`),
  `docs/tasks/research-env-golden-cases.md` (cases 1–4, cost measure),
  `research/tasks/taint-confidence-search-page.md` (renders what this writes),
  `research/tasks/authority-ab-hardening.md` (second provider behind your interface).
- `server/analyzer/judge.py:33-41,141-210`, `dedup.py:28-53`, `search.py:8-17,200-256`,
  `store.py:17-19,178-179,804-824`, `apiserver.py:68,1552,1573`, `llm.py:49-126`,
  `mcp_server.py:227-243` (flag path — note `wrong = false OR stale` conflation this design splits).
