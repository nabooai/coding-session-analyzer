---
name: research-referee
description: Operate the fact-core v2 research referee (research/) — run the golden-case suite, freeze a snapshot, add a golden case, flip an xfail when a sibling task lands, start/stop Phoenix and feed it a run, and cost the retro sweep. Use when the user says "run the golden cases", "run the referee", "run the suite", "add a golden case", "freeze the snapshot", "start phoenix", "feed phoenix", "flip the xfail", "measure the sweep", or "/research-referee".
---

# Operate the research referee (golden cases + Phoenix)

`research/` is the merge gate for every fact-core v2 sibling task: golden cases replayed against a
FROZEN corpus snapshot, deterministic, offline-first. Phoenix is an optional viewing sink — the
referee's truth is exit codes + `research/results/*.json`, never the dashboard.
Not this skill: `server/eval_recall.py` is the *question-routing* gate.
Reference doc: `research/README.md` (case anatomy, check kinds, seeded-vs-real and expect-picking
rules). This skill is the operating procedure and deliberately does NOT restate those rules; when
skill and README disagree, the README wins — fix the skill.

## Ground rules (violating these has bitten before)

- **Never point any tool at a live corpus.** The runner refuses paths outside `research/corpus/`
  (`store.org_db()` resolves `data/` against the cwd) — do not work around it.
- **Never edit anything under `research/corpus/`** — snapshots are immutable; changed source = new
  snapshot (`--new`).
- **Phoenix is compose-managed, research-only, and stays off the lab.** Never a hand-typed
  `docker run` (a bring-up that dropped an env var once lost a day of data to the container layer),
  and never in a product compose — ELv2 license, corpus text in span attributes.
- **`--measure-sweep` spends real money** (one haiku call per sampled fact). Don't run it casually.

## 1. Run the referee

```bash
python3 research/run.py --selfcheck     # referee-of-the-referee: no snapshot/docker/LLM needed
python3 research/run.py                 # full suite vs the LATEST snapshot | --only <case-id> for one
```

Exit codes: **0** green (runnable PASS + XFAILs failing as expected) · **1** pipeline wrong (FAIL /
XPASS / ERROR — the line names the case and detail) · **2** YOUR setup wrong (no snapshot, missing
case material, illegal path).

**Snapshots are frozen on demand, not per run or per day** — re-freezing silently changes what the
recall checks run against, defeating the determinism the suite exists for. Freeze only when exit 2
tells you to, or when case material genuinely changed:

```bash
sh research/snapshot.sh /path/to/analyzer.db          # worktrees/clean clones have no server/data —
sh research/snapshot.sh /path/to/analyzer.db --new    # same-day changed source: NEW sibling dir
```

Re-running on an intact snapshot prints "already frozen" and touches nothing — normal.

## 2. Add a golden case

Follow `research/README.md` §"Adding a new case" (sim-tested; it owns the seed shape and the expect-picking rule — the subtlest rule here, taught only there and
in cases.py's docstring). Then verify, in order — all three green before you're done:

```bash
python3 research/cases.py                       # shape check (derived counts; nothing pinned to 10)
python3 research/run.py --only case<N>-<slug>   # your case: PASS lines + your XFAIL naming the owner
python3 research/run.py                         # full suite still exit 0
```

`--selfcheck` covers seeded cases automatically; a case that relies on your own corpus is skipped
there with a printed note unless you extend `_fixture_snapshot` (optional).

## 3. Flip an xfail (when your sibling task lands)

The run turning red with **XPASS** on your case IS the reminder. The flip: delete that check's
`xfail=dict(...)` field in `research/cases.py` — one line, nothing else — then re-run the full
suite (exit 0) and `--selfcheck`. If a case XPASSes and you did NOT just land its owner task, the
evaluator may be vacuous — investigate before flipping.

## 4. Start / stop Phoenix

```bash
docker compose -f research/phoenix/docker-compose.yml up -d    # colima start first if docker is down
# no compose plugin ("unknown shorthand flag: 'f'")? use the standalone binary:
docker-compose -f research/phoenix/docker-compose.yml up -d
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:6006/   # 200 = up (first boot ~30s)
test -s research/phoenix/data/phoenix.db || echo 'WARNING: DB not on host — data will not survive'
```

**If `up -d` fails with `container name "/phoenix" is already in use`**: a Phoenix started outside
compose holds the name. Do NOT obey Docker's "remove or rename" advice blindly — check where its
data lives first:

```bash
docker inspect phoenix --format '{{json .Mounts}} {{.Config.Image}}'
```

- `/mnt/data` bind-mounted from this checkout's `research/phoenix/data` → the DB is on the host and
  survives: `docker rm -f phoenix`, then compose `up -d` to converge on the pinned, compose-managed
  container.
- No host mount → the DB dies with the container: `docker cp phoenix:/mnt/data/phoenix.db
  research/phoenix/data/` first, then remove and converge.

Stop: `docker compose -f research/phoenix/docker-compose.yml down` (data persists in `./data`).
Note: `down` only stops compose-labeled containers — against a hand-started one it silently does
nothing; converge first.

The image tag is **pinned in the compose file** (Phoenix migrates its SQLite at boot, one-way).
Before bumping it: `compose down` (checkpoints the WAL — never `cp` a live WAL db), copy
`research/phoenix/data/phoenix.db` aside, then bump and `up -d`.
`research/phoenix/data/` is gitignored and **checkout-relative**: operating from a different
checkout (e.g. after the branch merges) starts with empty history unless you move the dir (down
first). Loopback-only, no auth — never rebind to 0.0.0.0.

**A remote Phoenix**: feed it from a laptop through SSH:
`ssh -L 16006:localhost:6006 <phoenix-host>`, then
`CSA_PHOENIX_BASE=http://localhost:16006 python3 research/run.py --otlp`.

## 5. Feed Phoenix

```bash
python3 research/run.py --otlp
```

Each invocation adds one **trace** in project `csa-research` (one trace = one RUN; every check is a
child span, so different checks' inputs share a trace id on purpose) and one complete **experiment**
on dataset `csa-golden-cases` — one run per case, one CODE evaluation per check; the dataset holds
ground truth and never grows from re-runs. The printed line reports the counts; an experiment with
`missing` runs means the REST leg failed mid-way — re-run `--otlp` (the run itself is unaffected;
exit codes never depend on Phoenix, and without `--otlp` the runner opens zero sockets).
Run-over-run comparison (the authority A/B workflow): dataset page → Experiments → select two →
Compare.

## 6. Cost the retro sweep (spends real money)

```bash
python3 research/run.py --measure-sweep --sample 10     # sample capped at 50
```

Prints per-fact median/p90 + projection over the snapshot's decision/constraint population; the
report lands in `research/results/sweep-*.json` — cite it at the rollout-step-3 approval. The
prompt is PROVISIONAL (`research/sweep_prompt.txt`); resolution-engine re-measures with the real
`contradict.py` prompt before any prod sweep.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `source db not found` from snapshot.sh | Worktree/clean clone has no `server/data/` — pass the corpus path explicitly |
| exit 2 "snapshot lacks case material" | Snapshot and seeds miss a `needs` session — fix the case's seed, or re-snapshot `--new` from a corpus that has it |
| exit 2 "hash mismatch" | Someone edited a frozen file — snapshots are immutable; make a `--new` one |
| `container name "/phoenix" is already in use` | Hand-started Phoenix holds the name — converge per §4, checking the data mount BEFORE removing |
| Phoenix push 415 | You're not on the protobuf path — never emit OTLP JSON; this Phoenix rejects it |
| Experiment shows missing runs | REST leg failed mid-way; re-run `--otlp` (dataset is idempotent, a new experiment is created) |
| Phoenix empty after restart | Container started outside compose (env lost), OR you brought it up from a different checkout — `./data` is checkout-relative |
| Stray `data/analyzer.db` appeared | An analyzer module ran directly from the wrong cwd — delete the stray at the repo root/scratch dir only; `server/data/analyzer.db` is always a REAL corpus, never touch it |
