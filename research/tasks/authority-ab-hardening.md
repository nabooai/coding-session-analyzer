# Task: Authority A/B (git vs activity) + extractor-vs-author flag attribution + debt consolidation

**Status:** ready to start · **Owner:** unassigned · **Prereq:** `research/tasks/resolution-engine.md`
merged (provider interface + conflict cases exist), `docs/tasks/research-env-golden-cases.md` running
**Motivation/benchmark:** the resolution engine ships with ONE authority provider (session activity).
The founder's hypothesis is that git signals (merge rate, per-repo authorship) may be the better
authority source — decide it by experiment, not taste. Plus two hardening debts from the design
round: (a) a human "wrong" flag currently dings the fact regardless of whether the AUTHOR was wrong
or the EXTRACTOR misread the transcript — authors must not lose credibility to extraction bugs
(Knowledge-Vault lesson); (b) migration/patch debt named in fact-core-v2.md §Phases P4.

## Working protocol (binding — read before the code)

1. Work in a **separate git worktree** off the default branch — use the **`/goal`** flow to bootstrap it
   (worktree + worklog.md; overwrite any inherited worklog). Never work on a shared checkout.
2. Before implementing, **spawn a Senior SWE subagent AND a Senior PM subagent concurrently**: the SWE
   challenges the git provider's data plumbing (which repos, whose clones, identity mapping
   commit-email→sessions.user); the PM challenges the A/B metric (agreement with ground truth on
   which cases; what margin justifies switching). Reconcile, then build.
3. After implementing, **launch 3+ concurrent "user" subagents**: one flags a fact whose evidence
   quote does NOT support the claim and checks the author's ledger is untouched; one flags a
   genuinely wrong assertion and checks it IS recorded; one reads the A/B report cold and must be
   able to state the decision + margin. Fix friction, re-run.
4. Use **`/loop`** for the A/B sweeps over the research corpus and any git-history rollups.
5. **Follow AGENT_CHECKLIST discipline** — `docs/development/checklist.md`.

## Research conclusions (verified 2026-07-31 — do not redo)

- **Git provider formula — DOA (Degree of Authorship)**, battle-tested closed form (Fritz et al.;
  Avelino et al. validated at Linux scale): `DOA(d, f) = 3.293 + 1.098·FA + 0.164·DL − 0.321·ln(1+AC)`
  — FA=1 if d created f, DL = d's changes, AC = others' changes. Normalize per file; ownership
  threshold commonly normalized>0.75 ∧ absolute>3.293. Pure `git log` arithmetic. Aggregate file→repo
  (or path-prefix) for CSA's domain granularity. Identity join: commit email → `sessions.user` via
  the roster (share the git-log rollup artifact with `docs/tasks/registries-verify.md` — build once).
- **A/B design**: both providers implement the `analyzer/authority.py` interface from
  `research/tasks/resolution-engine.md`, bins compared on the research env's labeled conflict cases
  (golden case 9 is the seeded disagreement; add ≥10 labeled historical conflicts mined from the
  corpus — the founder labels ground truth once). Metric: per-case agreement of the R2/R3 gate
  outcome with ground truth; report per-case table + decision. Deterministic evaluators via
  `research/run.py` (Phoenix experiment comparison for the UI view).
- **Flag attribution (KBT lesson — extractor error must not damn the author)**: flag path is
  `mcp_server.py:227-243` — label `wrong` explicitly means "false OR stale" with the distinction in a
  free-text note (:231): the conflation this splits. On `wrong`: ONE cheap `llm.py` haiku check —
  "does the evidence quote support the claim?" (evidence lives on the mention rows,
  store.py:17-19). Mismatch → `author_ledger(kind='flag_extractor_error')`, author confidence factor
  untouched, counts into an extraction-bug ledger (feeds the Improve tab's waste review someday).
  Match → `flag_confirmed`, author track record moves. `author_ledger` DDL is in fact-core-v2.md
  §Schema (landed by `docs/tasks/fact-core-schema-v2.md`).
- **Debt list (P4, from the design round)**: idempotent-ALTER pile in store.py:153-169 folded into
  SCHEMA where safe; SCHEMA_VERSION discipline documented at extract.py:6-15 (v2 bump lands with
  fact-core-schema-v2 — verify it happened, don't re-bump); the two-POST-allowlist trap
  (apiserver.py:68 `_COOKIE_POST_OK` + :1552 `do_POST`) unified behind one registration helper so a
  new route CANNOT join only one; optional Langfuse OTLP sink documented (config-swap only — MIT,
  ingests exact `claude -p` cost_usd, but 6 containers/8-16 GiB self-host: NEVER on the lab box;
  Phoenix stays the research sink).
- **Cold-start law holds for BOTH providers** (CATD shrinkage): a person with no git history AND no
  sessions bins YELLOW, never zero — R5 in resolve_rules must hold under provider swap (property
  test exists there; extend it to the git provider).

## Deliverables

1. `git` authority provider behind the existing interface: git-log rollup per repo (shared artifact
   with registries), DOA aggregation, same bin semantics (quantile + recency override + overlay).
2. A/B harness in `research/`: labeled conflict set (mine candidates, founder labels ground truth),
   `run.py --ab-authority` producing the per-case agreement table + a written decision section in
   this doc (or a sibling report doc) — which provider ships as default, by what margin.
3. Flag attribution: the evidence-supports-claim check on `wrong` flags + `author_ledger` writes +
   the split of stale-vs-false in the flag API (label `superseded` becomes a first-class flag routed
   to the resolution engine instead of `wrong`+note).
4. Debt: allowlist registration helper (with a test that a half-registered route fails loudly),
   ALTER-pile fold, Langfuse sink doc page.
5. Golden cases 9–10 flipped from xfail.

## Acceptance (all must pass)

- [ ] Both providers produce bins for every (user, domain) in the research corpus; provider swap
      changes ONLY R2/R3 gate outcomes (resolve_rules purity holds — rerun its property check under
      both).
- [ ] `run.py --ab-authority` emits the per-case table; the decision (default provider + margin) is
      written down and reproducible from a clean checkout.
- [ ] Golden case 10: flag on a fact whose evidence does NOT support the claim →
      `flag_extractor_error` ledger row, author confidence factor unchanged (**negative:** no
      `flag_confirmed` row), fact still handled (hidden per existing flag semantics).
- [ ] Genuine wrong-assertion flag → `flag_confirmed` recorded and the author's factor moves.
- [ ] **Negative:** a user with zero git history and zero sessions still bins YELLOW under the git
      provider (cold-start law survives the provider swap).
- [ ] Allowlist helper: a test route registered in only one list fails the self-check loudly.
- [ ] `eval_recall.py` exits 0; `research/run.py` fully green (no xfails left across the program —
      this is the last task; if any remain, name the owner doc in the worklog).

## Pointers

- Master design: `research/fact-core-v2.md` §§Authority, Confidence (flag path), Phases P4.
- Prereqs/siblings: `research/tasks/resolution-engine.md` (interface + gate),
  `docs/tasks/registries-verify.md` (shared git rollup), `docs/tasks/fact-core-schema-v2.md`
  (`author_ledger`), `docs/tasks/research-env-golden-cases.md` (harness + cases 9–10),
  `research/tasks/taint-confidence-search-page.md` (confidence factor consumer).
- `server/analyzer/store.py:17-19,153-169`, `extract.py:6-15`, `mcp_server.py:227-243`,
  `apiserver.py:68,1552,1573`, `llm.py:49-126`.
