# research/ — the referee for fact-core v2

Pipeline-correctness golden cases for `research/fact-core-v2.md` (temporal validity, authority-gated
supersession, registries, taint), run against a **frozen snapshot** of any corpus, each case seeding its own material. It sits beside
`server/eval_recall.py`, which stays the question-routing gate; this suite is what the sibling tasks
(resolution engine, registries, taint, entity-noise gate, authority A/B, otel traces) must turn
green **before touching prod**.

## First green — from a clean checkout

Needs Python 3 only. **No Phoenix, no docker, no LLM access, no `uv`, no snapshot** for the
self-contained check:

```bash
python3 research/run.py --selfcheck     # fixture corpus; proves the referee itself referees
```

For the real suite, freeze a snapshot once, then run:

```bash
sh research/snapshot.sh /path/to/analyzer.db   # your corpus — see below
python3 research/run.py                        # latest snapshot, all cases, offline, deterministic
```

With no argument, snapshot.sh defaults to `server/data/analyzer.db` **of this checkout** — which only
exists where a server has actually run (`server/data/` is untracked). On a fresh clone or a git
worktree, pass your corpus path explicitly (on a CSA dev box that's typically the main checkout's
`server/data/analyzer.db`).

Expected on today's code: every runnable check PASS, every future-mechanism check XFAIL naming its
owner task, exit 0. A `fail`/`xpass`/`error` line is a real problem — see exit codes below.

## Layout

```
research/
  snapshot.sh        freeze a corpus db (+ extraction caches for sessions.txt) → corpus/<date>/, read-only
  sessions.txt       session prefixes whose FACTS the frozen db must contain (case material). The
                     extraction caches for them are frozen too when present — best-effort S1 records;
                     "N named session(s) had no extraction cache" is informational, not a failure
  cases.py           the 10 golden cases as data (this is the file you edit)
  run.py             the runner: replay, evaluate, exit codes, --otlp, --measure-sweep, --selfcheck
  sweep_prompt.txt   PROVISIONAL S4 contradiction prompt for --measure-sweep
  corpus/<date>/     frozen snapshots (gitignored; MANIFEST = sha256 provenance)
  results/           per-run JSON + sweep reports (gitignored)
  phoenix/           optional trace/eval sink (docker compose fragment; research-only)
```

## Running

```bash
python3 research/run.py [--snapshot research/corpus/2026-07-31] [--only case1-graph-store-supersession]
                        [--json] [--otlp] [--list]
```

- `--list` — case ids + which sibling task doc owns each xfail.
- `--only <id>` — a sibling task's inner loop; exit code reflects the subset.
- Results always land in `research/results/run-<ts>.json` (per-check status, snapshot, git sha).

Exit codes: **0** green (runnable pass + xfails failing as expected) · **1** a check failed, an xfail
unexpectedly passed, or an evaluator crashed · **2** environment/usage error (no snapshot, snapshot
missing a case's material, path outside `research/corpus/`). 1 means "the pipeline is wrong",
2 means "your setup is wrong".

Statuses: `PASS` / `FAIL` (red) / `XFAIL` (expected until the named task lands) / `XPASS` (red — see
Flipping) / `ERROR` (red — evaluator crashed or an xfail failed for a non-pinned reason).

## Anatomy of a case (`cases.py`)

```python
dict(
  id="case2-etl-always-queues",        # stable, greppable; --only takes this
  fc2_case=2,                          # the number in research/fact-core-v2.md §Research environment
  title="...",
  needs=["seed-etl-old", "seed-etl-new"],  # session-id PREFIXES that must exist in the snapshot (after
                                       # seeding). Missing ⇒ exit 2 with a re-snapshot instruction.
  seed=[("sessions", {...}),           # rows inserted into the per-run SCRATCH COPY only —
        ("hidden_context", {...})],    # the frozen snapshot is never written
  checks=[
    dict(name="...", kind="exists", sql="SELECT COUNT(*) ...", min=1),           # runnable today
    dict(name="...", kind="route",  q="...", want=["team", "lookup"]),
    dict(name="...", kind="recall", q="...", gold=[["seed-graph-new", "(?i)duckdb"]]),
    dict(name="...", kind="pr_card", q="...", want_session="seed-pr438"),
    dict(name="...", kind="exists", sql="SELECT ... FROM fact_edges ...",        # future mechanism
         xfail=dict(owner="research/tasks/resolution-engine.md", expect="MISSING")),
  ])
```

Check kinds: `exists` (SQL, first cell ≥ `min`; an absent table/column reports MISSING), `route`
(deterministic `route.classify` must equal `want`), `recall` (production `search.search` top-5, hit =
session-prefix + regex, the `eval_recall.gold_rank` pattern), `pr_card` (deterministic
`resolve.cards` must surface the creating session).

`xfail=dict(owner=..., expect=...)`: expected to fail **until the owner task lands** — satisfied by
MISSING or a failure containing `expect`; a pass or any other failure turns the run red. Future
checks assert only against the agreed DDL in fact-core-v2 §Schema v2 (`facts`, `fact_edges`,
`registry`, `authority`, `author_ledger`, `spans`) — never against invented APIs. Pick `expect` for
the mid-program transition: if the schema task creates your check's tables but a LATER task fills
them, the failure changes from `MISSING: no such table` to `got 0, want >= 1` when the schema lands —
pin `expect="got 0"` (MISSING is accepted unconditionally, so it covers both regimes). Pin
`"MISSING"` only for checks on tables that no earlier task creates (spans, entity_stats,
authority, author_ledger).

## Adding a new case (worked example)

Say you found a new supersession: "we dropped the nightly rebuild" reverses "rebuild nightly".

1. Pick an id: `case11-nightly-rebuild`. Add `fc2_case` only if it enters the design doc; otherwise
   just omit it — the shape check and the runner derive every count, nothing is pinned to 10.
2. Write `seed` rows (copy case 2's shape — two sessions, two `hidden_context` rows with explicit
   refs and dates). `needs` lists your own seed-session prefixes (use a `seed...` prefix). Seed refs
   just need to be unique short strings; real refs are `sha1(claim)[:12]`, so 12 chars keeps the
   shape familiar. Seeds enter the docs FTS on the scratch copy, so `recall` and `pr_card` checks
   work over them; a case backed by your own corpus instead lists its real prefixes in `needs` and
   `sessions.txt`.
3. Encode the runnable floor: an `exists` precondition (both claims present), a `route` check if the
   question shape matters. `route.classify` is deterministic — clone the question shape of a similar
   existing case; if the route check then fails, either your expectation is wrong (compare against
   `server/eval_recall.py`'s GOLDEN routes) or you found a routing regression.
4. Encode the future truth as an `exists` over the v2 DDL with
   `xfail=dict(owner="research/tasks/resolution-engine.md", expect="got 0")` — "got 0" because the
   schema task creates `fact_edges` before the resolution engine fills it (the expect-picking rule
   above).
5. `python3 research/cases.py` (shape check) → `python3 research/run.py --only case11-nightly-rebuild`
   → expect PASS lines + your XFAIL naming the owner → run the full suite → still exit 0.
6. `--selfcheck` keeps working automatically for seeded cases (they carry their own material). A new
   REAL-corpus case is skipped there with a printed note unless you extend `_fixture_snapshot` in
   run.py — optional, and the only reason to touch run.py.

## Flipping an xfail (for sibling-task sessions)

When your task lands the mechanism, its case will **XPASS and the run turns red on purpose** — that
red is your reminder. The flip: **delete the check's `xfail=dict(...)` field in `cases.py` — one
line, nothing else** — and re-run. If a case XPASSes and you did *not* just land its owner task, the
evaluator is probably vacuous: run `--selfcheck` and investigate before flipping anything.

## Snapshots

- Create: `sh research/snapshot.sh [/path/to/analyzer.db]`. Uses SQLite's backup API (safe against a
  live WAL writer), freezes the db + extraction caches for `sessions.txt` entries, writes a sha256
  `MANIFEST`, then chmods everything read-only.
- Re-run on an intact snapshot: "already frozen", nothing touched (idempotent). Same-day changed
  source: `--new` creates `corpus/<date>-2` — an existing snapshot is **never** mutated.
- Every run re-verifies the MANIFEST hashes and prints which snapshot + git sha it ran against.
- run.py copies the frozen db to a scratch dir and pins its cwd there. **Never point any tool at a
  live corpus**: `store.org_db()` resolves `data/analyzer.db` against the CURRENT cwd — which is
  exactly how a bare call once seeded the real corpus. run.py refuses any
  path outside `research/corpus/` (exit 2).
- The server keeps no raw transcripts (facts ship; transcripts stay on client machines), so
  "transcripts" in a snapshot = the extraction caches; absences are listed in the MANIFEST.

## Phoenix (optional, research-only)

```bash
docker compose -f research/phoenix/docker-compose.yml up -d    # UI at http://localhost:6006
python3 research/run.py --otlp                                 # spans + per-case scores + experiment
```

Without `--otlp` the runner opens **zero sockets** (enforced by `--selfcheck`); the exit code never
depends on Phoenix. ELv2 license: internal research use only — never add Phoenix to a product
compose. A team-shared instance runs on an internal lab host behind an access gate; feed it via
SSH tunnel + `CSA_PHOENIX_BASE` (see the research-referee skill §4).

## --measure-sweep (spends real money)

```bash
python3 research/run.py --measure-sweep [--sample 10] [--prompt-file ...]
```

Costs the S4-style retro contradiction sweep: samples N of the snapshot's decision/constraint facts,
builds each one's candidate set (entity-overlap ∪ FTS same-type, capped 20), makes a **real haiku
call per sampled fact** through `analyzer/llm.py` (isolated argv, real `cost_usd`), and prints
per-fact mean/median/p90 + a projection for the whole population with a p90 ceiling. The report
lands in `research/results/sweep-<ts>.json` — cite it at the rollout-step-3 spend approval. The
prompt is **provisional**; resolution-engine re-measures with the real `contradict.py` prompt before
any prod sweep. Reads the frozen snapshot read-only; writes nothing anywhere else.

## Troubleshooting

- **"source db not found" from snapshot.sh** — you're on a fresh clone/worktree where `server/data/`
  doesn't exist; pass your corpus path: `sh research/snapshot.sh /path/to/analyzer.db`.
- **"no snapshot found"** — run the printed `snapshot.sh` line; `--selfcheck` needs none.
- **"snapshot lacks case material"** (exit 2) — neither the seeds nor the source db contain a `needs`
  session; fix the case's seed, or add the prefix to `sessions.txt` and re-snapshot from a corpus
  that has it (`--new` if same-day).
- **ModuleNotFoundError: analyzer** — run from the repo checkout; run.py inserts `server/` on
  sys.path itself, no `uv` needed (everything is stdlib).
- **A stray `data/analyzer.db` appeared somewhere** — you ran an analyzer module directly from the
  wrong cwd, not via run.py (which pins cwd in a scratch dir and brackets the live db). Delete the
  stray file; nothing in run.py does this.
- **Phoenix port busy / not up** — the run still completes; `--otlp` reports the push failure and
  moves on.
- **hash mismatch on snapshot.sh** — someone edited a frozen file; snapshots are immutable, make a
  `--new` one.
