---
name: code-tests
description: Load BEFORE writing or running tests. Carries CSA's testing conventions per component (pytest for the server, vitest for the UI, go test for the client), where tests live, what a legacy module's self-check is, the ORM and raw-SQL pins, and the post-test checks. Trigger on "write a test", "add coverage", "fix this failing test", "run the tests".
---

# Code tests

Tests are how a change earns the word "done".

## Running

```bash
cd server && uv run python -m pytest tests/ -q            # the server suite (about 25 s)
cd server && uv run python -m analyzer.<module>           # a legacy module's __main__ self-check
cd server/ui && npm run check                             # tsc, eslint, vitest
cd client && go vet ./... && go test ./...                 # the Go client
```

## Placement and naming

- Server: `server/tests/test_<area>.py`, pytest, plain `assert`. Modules that hold no SQL (`dedup`,
  `redact`, `llm`, `ingest`, `project`, `queue`, `extract`, `etl`, `harness`, `improve`,
  `incremental`, `drain_loop`, `memories`, `tracer`) also keep a `__main__` self-check; new
  behaviour still gets a pytest test.
- UI: `server/ui/src/<module>.test.ts` beside the module, vitest in the node environment: a tested
  module must not touch `document` at import time (`routes.ts`, `graph.ts`, `landing.ts`,
  `theme.ts` are the pattern; `main.ts` is untestable by design).
- Client: `client/<file>_test.go`.

## Rules that matter here

- A new table gets a `CREATE TABLE IF NOT EXISTS`, a typed model, and a parity entry in
  `tests/test_models.py`; a new raw `con.execute` fails `tests/test_no_raw_sql.py` unless it is a
  documented carve-out.
- `tests/test_apiserver.py` pins the public and cookie-POST allowlists; a new route edits the pin.
- `landing.test.ts` pins the landing copy (budgets, a source per claim, no dashes, no real names);
  change the copy and fix the test, never loosen it silently.
- Bug fix: write the failing check first, watch it fail with the bug's signature, then fix.
- Tests seed data, never read the real corpus; `store.sqlite_db` on a temp path.

## After the test passes

1. `ruff check` (server) or `eslint` (ui) on the changed files.
2. `tsc --noEmit` for the UI (`npm run build` runs it).
3. The `gh pr create` hook runs the gates again and blocks when they are red.
