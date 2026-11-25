# Lesson: state that must not live in a process

A module-level `dict`, `list` or `set` mutated at runtime is per-process state. It dies on restart, a
second container never sees it, and it grows until someone hand-writes an eviction rule. The founder
caught one in review (PR #107, `cliauth._PENDING`); this page is the audit that followed, so the next
session inherits the answer instead of the search.

## The rule

Anything a user is waiting on, or that another process must agree about, belongs in a table with a
typed model. A cache of something re-derivable does not.

Ask three questions of every module-scope container:

1. **What happens on restart?** If a person has to start over, it is durable state.
2. **What happens with two containers?** Prod runs `csa-api`, `csa-mcp` and `csa-etl` from the same
   image. State one of them holds is invisible to the other two.
3. **Who prunes it?** A hand-written ceiling (`_MAX_PENDING` and friends) is a sign the data wanted a
   table with a TTL.

When the state is a credential: hash the lookup key the way `user_tokens` stores `token_hash`, keep any
plaintext only for the single claim that consumes it, and clear it in the same statement that claims it.

## The audit (2026-09-05, against master `63b01b4`)

| Where | What it holds | Verdict |
|---|---|---|
| `cliauth._PENDING` | the CLI pairing handshake, including the minted token between approve and poll | **Moved to a table** in PR #107. This is the one the founder found. |
| `apiserver._PIPELINE` | in-flight ingest stages behind the Pipeline page | **Structurally wrong in the deployed topology**: extraction happens in `csa-etl`, so the api's copy can never show the extracting or indexing stages, and a restart empties the page. The page is retired in redesign PR 5; if it is ever revived, it reads from a table the ETL writes. |
| `apiserver._WSREQ_HITS` / `_WSREQ_GLOBAL` | the rate limiter for the anonymous workspace request | **Known and bounded**: a restart resets the window and a second replica multiplies the allowance. The failure mode is a weaker limit, not lost user state. Its own comment already says a second replica needs a shared store. Revisit when the api scales past one container. |
| `drain_loop._REG_LAST`, `_COMPACT_LAST`, `_COMPACT_DONE`, `_DIRTY`, `_PURGE_LAST` | per-org scheduling timers in the ETL | Fine. One ETL container, and a restart only makes a sweep run early. |
| `apiserver._JUDGE_LOCK` | one judge at a time per process | Fine today (one api container). Two would run two judges: spend, not correctness. |
| `tracer._q`, `_last_prune` | finished spans awaiting the writer thread | Fine. Best-effort telemetry; a crash loses unflushed spans by design. |
| `auth._jwks` | the issuer's public keys with a fetch timestamp | Fine. A cache of something re-derivable. |
| `auth._discovery` | the issuer's openid-configuration endpoints with a fetch timestamp | Fine. Same class as `_jwks`: re-derivable from the issuer, refetched after `auth.JWKS_TTL`. The sign-in state itself (`state`, nonce, PKCE verifier) is the `sso_logins` table, never a global. |
| `store._SCHEMA_LOCKS`, `_TEL_MIGRATED` | in-process memo for schema setup | Fine. A real file lock is the guard; the memo only skips repeat work. |
| `search._SYN` | loaded synonyms | Fine. Configuration, read at startup. |
| `verify._warned_tokens`, `orgs._warned` | log-noise suppression | Fine. Losing it prints one extra warning. |
| `apiserver._DRV`, `mcp_server._DRV` | a cached Neo4j driver, or False once the connect had failed | **Gone** with Neo4j (on-prem subtask A, 2026-09-14). Nothing caches a driver any more. |

## Where this is enforced

The convention is in `.claude/skills/pr-code-review/HUMAN_REVIEW_CONVENTIONS.md`, which the
`pr-code-review` skill reads before every review, so a new module-level container in a diff is a
finding rather than a matter of taste.
