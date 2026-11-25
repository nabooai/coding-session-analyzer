# Architecture

```text
Claude Code session hooks
  → local consent, project scope, and secret redaction
  → authenticated ingestion and durable queue
  → JSONL/session normalization
  → small-model extraction of typed facts, entities, relationships, and evidence
  → SQLite corpus, entity/temporal graph tables, and FTS5 index
  → lexical/BM25 retrieval, filters, deterministic reranking, and graph context
  → web search and MCP recall
  → background extraction, judging, deduplication/compaction, and retention workers
```

Exact technical terms, identifiers, paths, and errors are matched through FTS5/BM25, then ranked with
provenance, recency, and fact-type signals. Every result keeps person, project, date, session, and evidence
so a user can inspect the source rather than trust an unsupported summary.

## Runtime services

- **API** serves authentication, ingestion, search, settings, and the web UI.
- **MCP** gives connected coding agents authenticated recall.
- **ETL** drains the durable queue and performs extraction and maintenance.
- **Migrate** applies schema and bootstrap configuration before services start.
- **Proxy** is optional and terminates trusted HTTPS for network deployments.
- **Backup** is an optional profile for retained snapshots.

The solo install uses a local SQLite corpus. Runtime data, credentials, logs, and session archives are mounted
outside the image and must never be included in a release artifact.
