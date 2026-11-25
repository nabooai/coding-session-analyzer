# Architecture

CSA has two halves. A small Go client on each laptop ships finished Claude Code sessions and asks for
recall on each prompt. A Python server, one per company, turns the sessions into facts and answers
those asks. Everything heavy (models, storage, search) is on the server; the client holds no model,
no key and no database.

```
 LAPTOP                                        SERVER  (one image, three long-running processes)
 ──────                                        ──────

 Claude Code                                   ┌──────────── api  (apiserver.py) ────────────┐
   │                                           │ auth gate · web UI · /api/*                 │
   │ SessionEnd ──► csa-client ship ──redact──►│ POST /api/ingest ──► redact again at the    │
   │                                           │ workspace floor ──► pending/<sid>.json      │
   │ UserPromptSubmit ──► csa-client hook ────►│ POST /api/ask  (lexical,   │                │
   │ ◄── "CSA recalled 2 past-session facts" ──│ no model call)             │                │
   │                                           └────────────────────────────┼────────────────┘
   │                                                                        ▼
   │                                           ┌──────────── etl  (analyzer.drain_loop) ─────┐
   │                                           │ extract facts with the LLM ──► facts, quotes│
   │                                           │ index (FTS5) · judge · dedup · compact      │
   │                                           └────────────────────────────┬────────────────┘
   │                                                                        ▼
   │                                              data/  per workspace: analyzer.db, telemetry.db
   │                                                     control.db:    orgs, tokens, sign-in, settings
   │                                                                        ▲
   │ ask / fact / flag tools ─────────────────► mcp  (mcp_server.py --http, /mcp)
   │
 browser ─────────────────────────────────────► api: landing, sign-in, Memory, Sessions, Team, Admin
```

## The client (`client/`)

One static Go binary, stdlib only. It does three things, each wired as a Claude Code hook by
`csa-client setup`:

| Hook | Command | What it does |
|---|---|---|
| SessionEnd | `csa-client ship` | redacts the finished transcript and POSTs it to `/api/ingest`, in a detached child so the prompt never waits |
| SessionStart | `csa-client ship --sweep` | ships sessions a crash left behind and reads server directives (`/api/client-config`) |
| UserPromptSubmit | `csa-client hook` | POSTs the prompt to `/api/ask` with a 3 s budget and prints the served facts, which Claude Code adds to the session |

Nothing ships before the person accepts a consent notice that names the server, the redaction mode
and what is kept. `setup` pairs the machine through the browser: the person signs in, types a word
pair the terminal printed, and the machine receives a per-user token. Every later call carries it.

Other subcommands set up, inspect or undo that shipping: `forget` (delete a session and everything
mined from it), `ignore` / `only` (project filters), `redact` (mode and preview), `doctor`, `update`.

## The server (`server/`)

One container image, Python 3.12, run as three processes that share the data directory.

**api** (`apiserver.py`, stdlib `ThreadingHTTPServer`). The auth gate sits at the top of `do_GET` and
`do_POST`. It serves the web UI (`server/ui`, built to `ui/dist`) and every `/api/*` route. Ingest only
re-redacts the transcript at the workspace's floor, writes a queue file and returns, so a laptop
never waits on extraction. The recall route a hook calls
is deterministic: it classifies the prompt (self, a named person, the team), searches, reranks and
answers without a model call.

**etl** (`analyzer/drain_loop.py`). Drains every workspace's queue in parallel, one task per
workspace, so a large backlog in one never delays another. For each session it extracts facts
through the LLM, writes them, refreshes the full-text index and lets the judge label new facts. When a workspace's queue is empty it compacts: clusters paraphrases,
rebuilds derived tables, purges expired transcripts. A daily spend cap per workspace holds sessions
in the queue past the limit.

**mcp** (`mcp_server.py --http`). The MCP server Claude Code talks to for explicit questions: `ask`
(may use a small model to route an ambiguous question or plan a relational one), `fact` (a fact's
full text) and `flag` (label a fact; the same feedback channel as the UI buttons).

`csa.py` is the operator CLI: migrations, backups, workspace and token administration, sign-in setup.
Cross-workspace administration exists only here, never over HTTP.

### What a fact is

Extraction keeps what a session learned that is not written down anywhere else. Each fact has a type
(`decision`, `gotcha`, `constraint`, `todo`, `preference`), the claim, the quote from the transcript
that proves it, the person whose session it came from, the project and the date. Entities (repos,
services, tickets, people, and any custom types a workspace defines) are extracted beside the facts
and linked to them.

### How recall ranks

Search is SQLite FTS5 with BM25, then a rerank that weighs the fact type, recency, the same project,
and the labels people gave: `gold` is boosted, `unimportant` is pushed down, `wrong` is never served.
A question that names a person ("how did Mara deploy?") searches that person's sessions first, then
falls back to the team. Every served fact is recorded in `tel.served`, so the UI can show which facts
were recalled into which later sessions.

### Storage

SQLite allows one writer per file, so the file split is the concurrency design
([sqlite-concurrency.md](development/sqlite-concurrency.md)):

| File | Holds | Written by |
|---|---|---|
| `analyzer.db` (one per workspace) | sessions, facts, entities, labels, the FTS index | etl, and the api for labels and deletes |
| `telemetry.db` (beside it, attached as `tel`) | what recall served, the questions asked, trace spans | every request |
| `control.db` (one per deployment) | workspaces, tokens, sign-in config, deployment settings, admin log | the api and `csa.py` |

Each workspace has its own files under `data/orgs/<org>/` (the first one lives in `data/`), so
isolation is which file a request opens, not a `WHERE org_id = ?` that a query could forget. The
schema is the SQLAlchemy models; Alembic migrates all three file types on start
([schema-migrations.md](development/schema-migrations.md)). Workspace and deployment settings are rows
too, edited with `csa.py org config` or `GET`/`POST /api/config` by a workspace admin, never new environment variables
([settings.md](development/settings.md)).

### The model seam

Every model call goes through `complete()` in `analyzer/llm.py`. With no provider registered it runs
on the Claude credential in the server's environment: an API key over HTTPS, or, on an image built with
`--target with-claude-code`, the Claude Code CLI (`claude -p`, isolated: no tools, no MCP, no session
persistence), which is also the only path for a subscription token. Providers registered on Admin > AI Models
(`analyzer/providers.py`) take over per role: Anthropic is the Messages API over HTTPS, OpenAI and Azure
OpenAI are one HTTPS chat completion, AWS Bedrock and Google Vertex need that CLI, with that provider's
credential. Laptops never call a model.

## Identity and tenancy

`CSA_AUTH=off` is for loopback development only; the server refuses to start with it on a public
address. `CSA_AUTH=org` is everything else: every request carries a per-user token (`csa_...`) or a
browser session cookie, and the token decides the workspace and the role (`member` or `admin`).
People sign in through one OpenID Connect issuer (Okta, Entra, Keycloak, Google Workspace;
[sso.md](sso.md)) or, without one, through an invite. Agents and CI use service tokens minted by an
admin.

## Deployment

The same image runs under Docker Compose (`server/docker-compose.trial.yml`, with a Caddy proxy for
TLS) or Kubernetes (`server/helm/csa`, one pod with the three processes and one volume). An offline
bundle with signatures is attached to every server release. See
[install-server.md](public-release/install-server.md),
[install-kubernetes.md](public-release/install-kubernetes.md) and
[bundle.md](public-release/bundle.md). Every outbound connection the server and the client make is
listed and measured in [egress.md](public-release/egress.md); every file either side writes is in
[data-at-rest.md](public-release/data-at-rest.md).

## Where to read next in the code

| Question | Start at |
|---|---|
| How does a request get authorised? | `apiserver.py` `do_GET` / `do_POST`, `analyzer/auth.py` |
| What happens to a session after ingest? | `analyzer/drain_loop.py`, `analyzer/etl.py`, `analyzer/extract.py` |
| How does recall choose facts? | `apiserver.py` `_hook_ask`, `analyzer/route.py`, `analyzer/search.py` |
| How is a transcript from another agent read? | `analyzer/harness.py`, [harnesses.md](harnesses.md) |
| Where is a table defined? | `analyzer/models.py`, `analyzer/tenant.py` |
| What does the laptop send? | `client/main.go` `send`, `client/hook.go`, [redaction.md](redaction.md) |
