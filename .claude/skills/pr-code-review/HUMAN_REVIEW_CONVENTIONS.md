# Human review conventions

Evidence gathered from human PR comments by the `pr-code-review` skill. This is a living review aid,
not a replacement for repository instructions.

## How to use this file

- **explicit** — the reviewer stated a general rule; apply it.
- **repeated** — multiple independent comments support the convention; apply it.
- **candidate** — one comment suggests a preference; use it as a review question, not a blocking rule.
- Preserve exact quotes and stable links so every inference remains auditable.

## Convention summary

| Confidence | Convention | Evidence |
|---|---|---|
| explicit | Assertions and self-check scenarios belong in the test suite, not production modules. | PR #96 comment `3918801601` |
| candidate | Prefer a conventional, direct CLI entry point over bespoke wrappers that obscure simple dispatch. | PR #96 comment `3918799204` |
| candidate | Before adding a local helper, check for an existing equivalent and establish that the behavior is recurring rather than one-time. | PR #96 comment `3918805420` |
| explicit | Do not introduce new environment variables. Configuration goes through the knobs that already exist (or `tenant_config`); a PR that needs a new `CSA_*` variable must say why. | PR #97 comment `3928396526` |
| explicit | Do not over-explain a function through comments. A docstring carries only what a caller cannot read off the signature, plus one line of "why" for an invariant the code cannot express. Flag: a docstring longer than the contract, a comment that restates the line under it, a list of the fields the return value obviously has, a tour of the algorithm, a pointer to the PR or mock a line came from. | PR #97 comment `3928414705`, review `5106468852`; PR #108 comment `3941234857` |
| repeated | No legacy or backward-compatibility support: no aliases for old config values, no "removed mode" special cases, no deprecation windows or once-only deprecation warnings. Change the config and say so in the release note. | PR #97 comments `3928398808`, `3928399962`, `3928401172`, `3928417306`, `3928418756`, review `5106468852` |
| candidate | A function whose body is mostly `os.environ.get()` calls and branching on them is a smell; fold it into the one place that reads the config. | PR #97 comment `3928407727` |
| candidate | Prefer plain values and direct indexing over dict-literal plumbing (`{**a, **b}`, `(x or {}).get()`) and module-level constants for every string. | PR #97 review `5106468852` |
| explicit | Durable state lives in a database, not in a module-level variable. A dict, list or set at module scope that outlives one request is per-process state: it is lost on restart, invisible to a second container, and unbounded unless someone remembers to prune it. A new table with a typed model is the answer. | PR #107 comment `3941083597` |
| explicit | Database models use the modern typed declarative style: `DeclarativeBase`, `MappedAsDataclass`, `Mapped[...]` and `mapped_column`, with `Annotated` aliases for repeated column shapes. A bare Core `Table` is not the default. | PR #106 comment `3941076044` |
| explicit | Filesystem paths are absolute and built with `pathlib`, never raw relative strings handed to a library. Resolve at call time so a process that changes directory still lands in the right place. | PR #106 comment `3941078691` |
| explicit | Imports are absolute (`from analyzer import x`), never relative (`from . import x`): a relative import invites a circular import and reads ambiguously to the next person. | PR #107 comment `3941183666` |
| explicit | A result set is keyed by names taken from the query itself: `stmt.selected_columns.keys()` for a Core select, the cursor's `description` for raw SQL. A hand-maintained list of column names beside a select is a silent mislabel waiting for the next reorder. | PR #108 comment `3941232034` |
| explicit | Schema changes go through Alembic migrations generated from the models, not hand-written DDL strings and `ALTER TABLE` lists. | founder, 2026-09-05 (session) |

## Evidence ledger

### PR #96 — move assertions into tests

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/96#discussion_r3918801601
- **Database ID:** `3918801601`
- **Author:** the founder
- **Date:** 2026-09-02
- **Area:** `server/analyzer/backup.py`, embedded `_self_check()`
- **Exact wording:** “Move all asserts to tests”
- **Candidate convention:** Assertions and behavioral self-check fixtures should live under `tests/`,
  leaving production modules focused on runtime behavior.
- **Confidence:** explicit

### PR #96 — avoid fluffy CLI wrappers

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/96#discussion_r3918799204
- **Database ID:** `3918799204`
- **Author:** the founder
- **Date:** 2026-09-02
- **Area:** `server/analyzer/backup.py`, `_cli(argv)`
- **Exact wording:** “Fluffy AI function”
- **Candidate convention:** Prefer standard, direct command parsing and dispatch over a bespoke helper
  whose structure adds little clarity.
- **Confidence:** candidate

### PR #96 — challenge one-off helpers

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/96#discussion_r3918805420
- **Database ID:** `3918805420`
- **Author:** the founder
- **Date:** 2026-09-02
- **Area:** `server/analyzer/backup.py`, `_connect_ro(path)`
- **Exact wording:** “Is it needed? I saw similar not relevant functions, I think they was one-time”
- **Candidate convention:** Search for an existing equivalent before adding a helper, and retain the
  helper only when it serves recurring production behavior rather than a one-time migration.
- **Confidence:** candidate

### PR #97 — no new environment variables

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928396526
- **Database ID:** `3928396526`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `configured_mode()` reading `CSA_AUTH`
- **Exact wording:** “I'm not sure why we need an environment variable for this? Add to our pr conventions to avoid using environment variables.”
- **Candidate convention:** Do not add environment variables. Configuration rides the knobs that already
  exist or the org's `tenant_config` row; a PR that needs a new `CSA_*` variable must justify it.
- **Confidence:** explicit

### PR #97 — no legacy aliases for config values

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928398808
- **Database ID:** `3928398808`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `_LEGACY_MODES = {"token": "org"}`
- **Exact wording:** “Don't want to support legacy code”
- **Candidate convention:** An old config value is not aliased to the new one. The server refuses it with
  the new value named, and the release note carries the change.
- **Confidence:** repeated (with `3928399962`, `3928401172`, `3928417306`, `3928418756`, `5106468852`)

### PR #97 — no "removed mode" special cases

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928399962
- **Database ID:** `3928399962`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `_REMOVED_MODES = {"cloudflare"}`
- **Exact wording:** “WTF?”
- **Candidate convention:** A removed option gets no dedicated set, branch, or bespoke refusal message. It
  is simply not a valid value.
- **Confidence:** repeated (see `3928398808`)

### PR #97 — no once-only deprecation warning state

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928401172
- **Database ID:** `3928401172`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `_warned_legacy = set()`
- **Exact wording:** “not understand”
- **Candidate convention:** Module-level state whose only purpose is to rate-limit a deprecation warning
  is scaffolding for a compatibility path that should not exist.
- **Confidence:** repeated (see `3928398808`)

### PR #97 — a function that is mostly environment reads

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928407727
- **Database ID:** `3928407727`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `boot_error(bind)`
- **Exact wording:** “Super redundant functionm, I don't understand what it does. A lot of os.environ.get(), for nothing.”
- **Candidate convention:** A function built from `os.environ.get()` calls and branches on their values
  is a smell. Read each setting once, in one place, and keep the guard to the checks that can fail.
- **Confidence:** candidate

### PR #97 — do not over-explain a function in comments

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928414705
- **Database ID:** `3928414705`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `_principal(...)` and the comment block above it
- **Exact wording:** “Add to our Human Review Convention to not over explain the function through comments as you did.”
- **Candidate convention:** A docstring states the contract. It does not narrate history ("it used to
  be"), alternatives considered, or restate what the body already says. One line of "why" is allowed
  only for an invariant the code cannot express.
- **Confidence:** explicit (reinforced by review `5106468852`)

### PR #97 — remove the compatibility window

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928417306
- **Database ID:** `3928417306`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `LEGACY_UNTIL` / `_LEGACY_MAX_DAYS`
- **Exact wording:** “figure out how we can get rid of it”
- **Candidate convention:** A time-boxed compatibility window for a retired credential is legacy support.
  Retire the credential outright and document the operator step.
- **Confidence:** repeated (see `3928398808`)

### PR #97 — no legacy parsing paths

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/97#discussion_r3928418756
- **Database ID:** `3928418756`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** `server/analyzer/auth.py`, `_legacy_deadline()` date parsing
- **Exact wording:** “Don't want to support legacy”
- **Candidate convention:** Code that exists only to serve a compatibility path goes with that path.
- **Confidence:** repeated (see `3928398808`)

### PR #97 — review summary: comments, legacy, dicts and constants

- **Review:** https://github.com/nabooai/coding-session-analyzer/pull/97#pullrequestreview-5106468852
- **Database ID:** `5106468852`
- **Author:** the founder
- **Date:** 2026-09-03
- **Area:** whole PR (`server/analyzer/auth.py`, `server/analyzer/orgs.py`, `server/apiserver.py`)
- **Exact wording:** “1. A LOT of comments in the code around functions.
  2. A redundeant legacy support. Drop it. Not looks good.
  3. I didn't like the way we wrote the functions, a lot of dict appearnce ("{}") and const everywhere.”
- **Candidate convention:** (1) comment density around functions is itself a finding; (2) legacy support
  is dropped, not kept; (3) prefer plain values and direct indexing over `{**a, **b}` /
  `(x or {}).get()` plumbing and a module constant for every string.
- **Confidence:** (1) explicit, (2) repeated, (3) candidate

### PR #107 — durable state belongs in a database, not a module global

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/107#discussion_r3941083597
- **Database ID:** `3941083597`
- **Author:** the founder
- **Date:** 2026-09-05
- **Area:** `server/analyzer/cliauth.py`, `_PENDING` (the CLI pairing handshake store)
- **Exact wording:** “Is `_PENDING` a local variable? If so, it's bad - we should keep it in a new table
  inside a DB.”
- **Convention:** State that must survive a request lives in a table. A module-level `dict` / `list` /
  `set` mutated at runtime is per-process: it dies on restart (here, a user mid-`csa-client setup` has to
  start over), it is invisible to a second api container, and it needs a hand-written eviction ceiling
  that is itself a griefing hole. When the state is a credential, hash the lookup key the way
  `user_tokens` stores `token_hash`, keep any plaintext for the single claim that consumes it, and delete
  it in the same statement that claims it.
- **How to review for it:** grep the diff for module-scope `= {}` / `= []` / `= set()` and for a
  `threading.Lock()` beside a global; a lock next to a module-level container is the tell. Ask what
  happens to that data on restart and on a second replica. A cache of something re-derivable (JWKS keys,
  a warn-once set, an in-process memo behind a real file lock) is fine; anything a user is waiting on is
  not.
- **Confidence:** explicit

### PR #106 — models use the typed declarative ORM, not a bare Core Table

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/106#discussion_r3941076044
- **Database ID:** `3941076044`
- **Author:** the founder
- **Date:** 2026-09-05
- **Area:** `server/analyzer/models.py`, the `ui_events` table declared as a Core `Table`
- **Exact wording:** “Why not using sqlalchemy.orm along with: DeclarativeBase, MappedAsDataclass,
  mapped_column, Mapped?”
- **Convention:** New tables are typed declarative classes. The reference the founder pointed at is
  a sibling project's analytics models: a `Base` built from
  `MappedAsDataclass, DeclarativeBase`, `Annotated` aliases such as `intpk` for repeated column shapes,
  `Mapped[...]` annotations with `mapped_column(init=..., index=...)`, and indexes declared inline rather
  than as separate statements. A PK-less rowid table is not a reason to fall back to Core: a new table
  can carry an `INTEGER PRIMARY KEY`, which in SQLite is the rowid and costs nothing.
- **Confidence:** explicit

### PR #106 — paths are absolute and built with pathlib

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/106#discussion_r3941078691
- **Database ID:** `3941078691`
- **Author:** the founder
- **Date:** 2026-09-05
- **Area:** `server/analyzer/store.py`, `DB_PATH = "data/analyzer.db"`
- **Exact wording:** “Make sure the path is absolute. Use Pathlib or something. Not raw.”
- **Convention:** A path handed to sqlite3, `open()` or a subprocess is an absolute `pathlib.Path`.
  Resolve it at call time, not at import: several entry points (the server, `csa.py`,
  `rehearse_tenancy.py`, most tests) `chdir` into a box directory and expect `data/` to follow them, so a
  constant frozen at import would send them to the wrong file.
- **Confidence:** explicit

### PR #107 — imports are absolute, never relative

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/107#discussion_r3941183666
- **Database ID:** `3941183666`
- **Author:** the founder
- **Date:** 2026-09-05
- **Area:** `server/analyzer/cliauth.py`, `from .tenant import CliPairing`
- **Exact wording:** “I don't like the `.`, it can cause a circular import easily, or confuse future
  developers. I want a clean absolute path.”
- **Convention:** Sibling modules are imported by their full package path (`from analyzer import store`,
  `from analyzer.tenant import CliPairing`). At the time of the comment `cliauth.py` was the only module
  in `server/` using the relative form and the PR had added a second one; the other 29 modules in
  `analyzer/` were already absolute.
- **How to review for it:** `grep -rn "^from \.\|^ *from \. import" server/` over the diff. Any hit is
  a finding.
- **Confidence:** explicit

### PR #108 — a result set is keyed by the query, not by a parallel list

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/108#discussion_r3941232034
- **Database ID:** `3941232034`
- **Author:** the founder
- **Date:** 2026-09-05
- **Area:** `server/apiserver.py`, `rule_facts` (`cols = [...18 names...]` then `dict(zip(cols, r))`)
- **Exact wording:** “Why are the columns raw? What if one column is gonna change?”
- **Convention:** The names a row is keyed by come from the statement that produced it. For a Core
  select that is `stmt.selected_columns.keys()`; for one of the raw-SQL carve-outs it is
  `cursor.description`, which means the raw string has to alias its output columns (`... AS acted_n`).
  A second, hand-written list next to the select is coupled to it by POSITION only: insert or reorder
  one column and every field after it is silently relabeled, and the reader downstream
  (`improve.findings`, an INSERT's bindparams) gets wrong values with no error anywhere.
- **How to review for it:** grep the diff for `zip(` next to a list of quoted names. A list that the
  statement is BUILT from (`tenant._FIELDS` -> `select(*_CONFIG_COLS)`) is fine, because the two cannot
  drift; a list that merely mirrors a select's argument order is the finding.
- **Confidence:** explicit

### PR #108 — the over-explaining rule did not land

- **Comment:** https://github.com/nabooai/coding-session-analyzer/pull/108#discussion_r3941234857
- **Database ID:** `3941234857`
- **Author:** the founder
- **Date:** 2026-09-05
- **Area:** `server/apiserver.py`, the 7-line `graph()` docstring
- **Exact wording:** “Avoid over-explaining what the function does in comments, the function should be
  self-explanatory by its name and the reason we are gonna write it.

  Put this rule in our HUMAN REVIEW file”
- **Convention:** Same rule as PR #97 `3928414705`, restated because the PR broke it anyway. The name
  says WHAT; the docstring says only what the signature cannot (the gate a caller must know about, a
  key that is populated conditionally, an invariant a reader would otherwise misread). Everything else
  is deleted, including the fields the return dict obviously has and the walk through the body.
- **How to review for it:** flag a docstring longer than its contract, a comment that restates the line
  under it, an enumeration of return-value fields, a narration of the algorithm, and any reference to
  the PR number, mock, or alternative a line came from. It is a finding on its own, not a nit: the
  founder has now raised it on two PRs.
- **Confidence:** explicit (second occurrence; see PR #97 `3928414705` and review `5106468852`)
