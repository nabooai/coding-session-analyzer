# Lesson: SQLite concurrency

SQLite allows exactly one writer per FILE. Every rule below follows from that, and each was learned
from a stalled or 502ing server.

## One file per contention source

- `analyzer.db` (corpus, attached as `main`): what questions are answered from. Written by ingest and
  compaction, read by serving.
- `telemetry.db` (attached as `tel`, one per org): what the system did (`served`, `queries`, `spans`).
  Written on every request. A table belongs here if the serving path writes it on every request; put
  it in the corpus and an `ask` queues behind an ingest.
- `control.db`: orgs and tokens, `orgs.py` only.

**Rule:** a new table goes in the file whose writer already owns that write pattern, as a typed model
in that plane's MetaData plus an Alembic revision for that environment
([schema-migrations.md](schema-migrations.md)).

## Compute first, then write, and write first inside the transaction

`write_entity_stats` once computed inside its transaction and held the corpus writer for 81.8 s per
session on prod; every concurrent write past `busy_timeout` failed. Pattern: `fetchall` everything,
then `BEGIN`, make the FIRST statement a write, commit. Scope the refresh to what changed; a
corpus-wide pass belongs in compaction, which waits for an empty queue.

## Never `BEGIN IMMEDIATE` on an attached set

`BEGIN IMMEDIATE` takes the writer lock on EVERY attached database, so a corpus write would take the
telemetry writer too and undo the split. Use a deferred `BEGIN` and write first.
`tests/test_telemetry_split.py::test_build_fts_does_not_lock_the_telemetry_file` is the tripwire.

## WAL and `busy_timeout` are set on open

A corpus opened without WAL plus one long writer 502'd prod (PR #49). `store.sqlite_db` sets both;
open corpora through it, never with a bare `sqlite3.connect`.

## Two reads of the same table can race a writer

`write_clusters` read `hidden_context` twice around a rebuild and a `KeyError` killed `/api/forget`.
Take ONE snapshot per derived-table rebuild. "Works locally" says nothing about this class.

## An indexed FTS5 column rescores the whole corpus

bm25 normalises by total row length, so adding an indexed column to the `docs` table moved 1,461 of
2,319 hits and dropped a control out of the cut. Append a candidate source instead; calibrate
synthetic scores on prod-shaped data (the lab corpus had no code-home facts).

## Seeding a scratch box

`store.sqlite_db(store.org_db(org))` from a cwd that holds `data/`; insert `sessions` and
`hidden_context`, then `store.refresh_facts` and `store.build_fts`. That open also attaches the
telemetry sibling as `tel` and brings both files to head, so telemetry rows go in on the same
connection. The `scratch-server` skill does this.
