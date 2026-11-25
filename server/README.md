# coding-session-analyzer (server)

Mine agent sessions — Claude Code today (`~/.claude/projects/*.jsonl`), any harness next — for
**golden context**: undocumented decisions/gotchas, entities, and resolved links — then hand the
connected context back to any future session, human-driven or agent-run.

Runs as a **team memory server** (the normal setup — many devs' sessions, attributed per person) or
**single-user/local** for a personal corpus. No API key — reuses the `claude` CLI login.

## Quickstart — recall past context
```bash
uv sync
./csa recall "the ETL work I did earlier"
```
`recall` returns a markdown context blob (Decisions / Gotchas / Constraints + session summaries, with sources
and `⟳N` when a finding recurs across N sessions). **Nothing to set up first:** if a topic has no indexed
context yet, `recall` lazily extracts the few most-relevant sessions on the spot (Sonnet, capped at 3, a few
cents) and the cache grows as you ask — so the second question about a topic is instant and free.

Flags: `-k N` (items to return) · `--no-fill` (pure read, never extract) · `--fill-k N` (cap auto-extractions).

## Use from any Claude Code session (MCP) — recommended

**Team mode (the normal setup):** run the MCP server next to `apiserver.py` on the central server —
it serves the shared, eagerly-extracted corpus over streamable HTTP:
```bash
uv run python mcp_server.py --http          # :58768/mcp (CSA_MCP_BIND / CSA_MCP_PORT to change)
```
Each teammate registers with one line — no clone, no uv, no Python on their machine
(`csa-client init` runs this for them):
```bash
claude mcp add --transport http csa http://your-server:58768/mcp -s user --header "X-CSA-User: <email>"
```
Now just ask naturally in any session — *"how do we release to production?"*, *"what did we decide
about X?"*. With the client hook enabled, Claude Code gets relevant CSA context automatically before
the model answers. MCP remains available for richer follow-ups: `ask(query)` handles explicit recall
or structured questions, `fact(id)` fetches a fact's full text, and `flag(id, label)` corrects the
corpus (`gold`/`wrong`/`unimportant`/`clear`). Set `CSA_PUBLIC_URL` on the server to make recall's fact
ids clickable links into the web UI. Cold-fill is off in team mode — the corpus is already extracted
at ingest.

The hook calls `POST /api/ask` on `apiserver.py`. That endpoint uses the fast grouped-provenance recall
path only (`fill=false`): no extraction, no SQL planner, no surprise LLM call on a user's prompt
submission path.

**Single-user local mode (stdio):** the original setup still works for a personal corpus — Claude Code
spawns the server locally and it reads your own `data/`; cold topics auto-extract on first ask
(~30-90s, a few cents):
```bash
uv sync
claude mcp add csa -s user -- uv run --directory "$(pwd)" python mcp_server.py
```

_No-MCP fallback:_ any session can also just run `` `/abs/path/to/coding-session-analyzer/csa recall "<topic>"` `` via Bash.

## Going deeper (optional)
```bash
./csa extract --dry-run          # coverage: which sessions are extracted / stale / missing
./csa build --limit 30           # bulk extract (claude) + index everything (uses Opus; costs money/time)
./csa harvest                    # deterministic raw-evidence backfill only: pr-links, PR URLs, worktrees/branches
./csa stats                      # overview | projects | hidden | entities | dups | deploys
./csa ask "which sessions deployed to prod last week?"
./csa search "sqlite write amplification"
```

## Pipeline
`ingest` (jsonl→transcript + deterministic raw-event harvest) → `extract` (claude→JSON, cached in
`data/extracted/`) → `index` (SQLite + FTS, cheap/replayable) → `recall`/`search`/`ask`.

For historical local corpora, run `./csa harvest` to backfill `pr_links`, PR/workspace FTS docs, and
worktree/branch visibility without re-extraction or LLM calls. Team-server ingest applies the same harvest
on each posted session; if LLM extraction fails, the raw deterministic evidence is still written and FTS is
rebuilt for that session's PR/workspace facts.

## Multi-user, consent, redaction — built
Each dev's client ships only after recorded, policy-version-gated consent (`csa-client init`), strips
secrets/tokens before anything leaves the machine (emails/hostnames kept on purpose), and every session
is attributed to its owner (`sessions.user`; shown in recall/`fact`/the Team view). Self-service delete:
`csa-client forget <id>`. How it fits together: [docs/architecture.md](../docs/architecture.md).

## Still gated (not built)
Per-user access control / tenant isolation — today anyone who can reach the server recalls the **whole**
team's corpus, and `X-CSA-User` is self-asserted attribution, not an access boundary (a Cloudflare Access
tunnel is the current network-level gate). Also the downstream context-system feed.
