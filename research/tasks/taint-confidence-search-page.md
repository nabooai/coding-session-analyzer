# Task: Taint propagation + label-free confidence + the Search trace page

**Status:** ready to start · **Owner:** unassigned · **Prereq:** `docs/tasks/fact-core-schema-v2.md` +
`research/tasks/resolution-engine.md` merged; `research/tasks/otel-traces-native.md` for the page's data
**Motivation/benchmark:** three visibility gaps left after the resolution engine lands: (1) facts that
DEPEND on a superseded fact keep serving as if nothing happened — the founder's ask: propagate doubt
to dependents, visibly, without hiding them; (2) ranking still trusts a two-valued extractor string
(`confidence` high|low) and session-count corroboration — one person repeating themselves 5× scores
like 5 people agreeing; (3) users can't see WHY an answer happened — no per-question trace surface
(model, cost, turns, DAG, planned SQL), though the `spans` substrate records it all.

## Working protocol (binding — read before the code)

1. Work in a **separate git worktree** off the default branch — use the **`/goal`** flow to bootstrap it
   (worktree + worklog.md; overwrite any inherited worklog). Never work on a shared checkout.
2. Before implementing, **spawn a Senior SWE subagent AND a Senior PM subagent concurrently**: the SWE
   challenges taint scope (one hop, dominant entities only — where does it false-positive on the real
   corpus?) and the rank-multiplier band; the PM challenges the Search page against the founder's
   words: "verify that 'which session talked about PR 123' ran SQL on PRs WHERE PR=123". Reconcile,
   then build.
3. After implementing, **launch 3+ concurrent "user" subagents**: one supersedes a load-bearing
   decision and audits every dependent fact's banner (and that nothing got hidden); one compares
   rankings for claims reaffirmed by 1 vs 3 authors; one uses ONLY the Search page to debug a wrong
   answer end-to-end and reports every missing breadcrumb. Fix friction, re-run.
4. Use **`/loop`** for corpus-wide taint sweeps and ranking A/B replays.
5. **Follow AGENT_CHECKLIST discipline** — `docs/development/checklist.md`.

## Research conclusions (verified 2026-07-31 — do not redo)

- **Taint decision** (fact-core-v2.md §Propagation): dependents = explicit `depends_on` edges ∪ (v1
  heuristic) decisions/gotchas whose DOMINANT entities (mentions.dominant, migration store.py:158)
  are the object of the superseded claim. ONE hop, decision/gotcha types only, computed at
  serve/index time from durable edges — no transitive cascades, nothing hidden. The founder's answer
  to "detach A when B is superseded?": NO — A stays served with "⚠ depends on a superseded fact:
  [B → superseded by C]", and a taint on a high-confidence A also enqueues A in the conflict queue
  for human re-affirm/retire. Rendering surfaces: recall payloads (app.py:164-538), the ambient hook
  banner (app.py:386-400 — its comment already names superseded decisions as the worst injection,
  11% of multi-fact fires span >30 days), and the UI fact views.
- **Confidence function** (label-free — the labeling economy failed; §Confidence in fact-core-v2.md):
  deterministic `f(signals) × authority_factor`, recomputed in S5. Inputs, strongest first: registry
  verdict floors; reaffirmed-by-DISTINCT-AUTHORS (today corroboration = cluster session count,
  `claim_clusters.sessions` store.py:804-824 → `corrob = min(1.3, 1+0.2*log2(sessions))`
  search.py:230 — replace the input with distinct `sessions.user` count per cluster); acted_on
  (signals, store.py:33 family — a heuristic lower bound, keep it weak); survival (served often,
  never contradicted — weakest); negatives (superseded/conflict/confirmed-wrong). One round, NO
  fixpoint iteration — TruthFinder-style iteration at team scale re-derives "who talks most".
- **Ranking integration**: score line search.py:232-233; new multipliers must stay in-band with
  TYPE(≤1.35)/TRUST(1.2)/STATUS(≤1.25)/corrob(≤1.3) — the PROJECT_BOOST comment (search.py:14-17)
  states the law: never out-multiply an exact cross-project lexical match. Extractor `confidence`
  string stays a routing hint only.
- **Search page data**: `spans` table from `research/tasks/otel-traces-native.md` (trace_id/parent links,
  kinds CHAIN/RETRIEVER/RERANKER/LLM, attrs carry model/tokens/cost_usd/num_turns/planned_query/
  result_ids) + `queries`/`served`/`costs` rows linked by trace_id. The PR-123 case is golden case 7
  (`server/eval_recall.py` locate shape). DAG = SVG from parent links — no chart/graph dependency.
- **UI reality**: tabs collapsed to Memory/Sessions/Team/Operator (PR #62); taxonomy UI logic has
  vitest coverage precedent. Read-only page → GET endpoints only → the two-allowlist POST trap
  doesn't apply (keep it that way). Lab deploy: replace `ui/dist` CONTENTS, never `mv` (bind-mount
  inode trap).

## Deliverables

1. Taint computation (index-time cache or serve-time walk — pick via corpus timing, note the choice)
   + banner data in recall payloads, hook output, and fact UI; high-confidence tainted facts enqueued
   to the conflict queue.
2. `depends_on` writers: extraction-evidence cross-reference (best-effort, deterministic — evidence
   quote contains another fact's distinctive claim head) + a UI affordance on a fact to link its
   dependency (GET page, POST joins both allowlists if added — apiserver.py:68,1552).
3. Confidence recompute in S5 + distinct-author corroboration + capped ranking multiplier; the old
   session-count corrob input replaced, band law respected.
4. **Search page** in the UI: trace list (recent asks: question, engine(s), cost, latency, hit/miss)
   → trace detail (span tree + DAG SVG, per-span model/tokens/cost/turns, planned SQL, result_ids,
   LLM rationale notes, served refs + acted_on outcome). Vitest for the tree/DAG component.
5. Golden cases 6 (taint) flipped from xfail; case 7 (PR-123 trace) asserted through the page's GET
   API, not just SQL.

## Acceptance (all must pass)

- [ ] Golden case 6: supersede the seeded SQLite decision → dependent gotcha shows the taint banner
      naming B and C; re-affirming A from the queue clears it.
- [ ] **Negative:** taint never hides — the tainted fact still serves (assert presence WITH banner);
      and a fact merely SHARING a non-dominant entity with B is NOT tainted (precision guard).
- [ ] Ranking: identical claim reaffirmed by 3 distinct authors outranks the same claim asserted 3×
      by one author (fixture test), with total multiplier within the band cap.
- [ ] **Negative:** confidence changes never resurrect facts excluded by review status, and never
      push a fuzzy match above an exact lexical match (the PROJECT_BOOST law, search.py:14-17).
- [ ] Search page: for the PR-123 ask, the page shows a RETRIEVER span with `engine='sql'` and
      planned_query containing `pr_links`… `123`, plus cost and turns on the LLM spans — the founder's
      verification scenario, clickable end-to-end.
- [ ] A user-subagent debugs a seeded WRONG answer using only the page and identifies the failing
      stage (the page is sufficient, not decorative).
- [ ] `eval_recall.py` exits 0; `vitest` green; UI-only deploy note present in the PR description
      (dist-contents rule).

## Pointers

- Master design: `research/fact-core-v2.md` §§Propagation, Confidence, Serving, Traces.
- Prereqs: `docs/tasks/fact-core-schema-v2.md` (edges, validity columns),
  `research/tasks/resolution-engine.md` (supersessions + conflict queue this task renders),
  `research/tasks/otel-traces-native.md` (spans contract — coordinate attr names, don't fork them),
  `docs/tasks/entity-noise-gate.md` (dominant/served entities feed the taint heuristic),
  `docs/tasks/research-env-golden-cases.md` (cases 6–7).
- `server/analyzer/search.py:8-17,200-256`, `app.py:19-48,164-538,386-400`, `store.py:33,158,804-824`,
  `server/eval_recall.py` (locate case), `docs/lab ui/dist` rule in `docs/tasks/` siblings.
