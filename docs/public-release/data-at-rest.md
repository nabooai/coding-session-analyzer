# Data at rest

Every file CSA writes on the server and on a laptop: what it holds, who can read it, how long
it stays, and what deletes it. Measured on 2026-09-15 with the on-prem harness
(`server/e2e/onprem.sh` in the CSA repository):
after a boot on a stale data directory, `adopt-default`, `sso set`, one sign-in, one paired
laptop and three sessions drained, the box's `data/` held exactly the files marked "measured"
below. Anything the code can write but that run did not produce is marked as such.

## Who can read a file

The api, mcp and etl containers run as uid 10001 (`server/Dockerfile`), and `data/` is a bind
mount of a host directory owned by that uid (`docker-compose.trial.yml:77`, `install-server.md`
§1). Three parties therefore read everything below: the container processes, root on the host,
and any account that can use the Docker socket (a `docker run -v` runs as root, so the `docker`
group is root-equivalent). The proxy container runs Caddy as root and reads `certs/` and its own
volume. Backups written under `data/` inherit the same readers.

## The server

| Path | Holds | Retention | Deleted by |
|---|---|---|---|
| `data/control.db` (measured) | the control plane, table by table below. SQLite in rollback-journal mode, never WAL (`analyzer/orgs.py`) | until deleted | `rm -rf data/` for the whole box. `csa.py org delete <org>` removes one hosted org's rows; it refuses `default` |
| `data/control.db.migrate.lock` (measured) | empty; the file lock api, mcp and etl take while migrating at boot (`analyzer/migrations.py:108`) | stays, harmless | with `data/` |
| `data/analyzer.db` (measured) | the `default` workspace's corpus: `sessions` (id, project, git branch, times, owner, `shipped_by`, summary, redaction mode), `hidden_context` (every extracted claim with its verbatim evidence quote, repo and path), `facts`, `fact_edges`, `entities`, `mentions`, `relations`, `actions`, `links` (URLs a session touched), `aliases`, `entity_stats`, `entity_types`, `hc_types`, `signals`, `reviews` (human flags), `pr_links`, `worked_on`, `data_sources`, `session_workspace`, `session_metrics` (the Improve numbers), `costs` (tokens and USD per model call), `docs` (the FTS5 index over the same text), `tenant_config`, `custom_entity_types`, `registry`, `claim_clusters`, the `harvest_*` markers | until deleted | `/api/forget` per session; `rm -rf data/` |
| `data/analyzer.db-wal`, `data/analyzer.db-shm` (measured) | SQLite's write-ahead log and its shared-memory index: committed rows not yet folded into the main file (`analyzer/store.py:154`) | rolling | folded at checkpoint. A plain `cp` of the main file misses them, which is why backups go through `analyzer/backup.py` |
| `data/analyzer.db.migrate.lock` (measured) | empty file lock | stays | with `data/` |
| `data/telemetry.db` with `data/telemetry.db-wal`, `data/telemetry.db-shm`, `data/telemetry.db.migrate.lock` (measured) | the telemetry plane, one file per corpus: `queries` (every question asked: who asked, the query text, the engine, the planned query, latency), `served` (which fact was shown to whom for which question), `spans` (OTel-shaped traces whose `attrs` keep `input.value`, the redacted question, and `output.value`, the first 1000 characters of every model output, `analyzer/llm.py:57`), `ui_events` (`POST /api/track`: user, event, page) | spans: the workspace's `trace_days` setting, default 30, pruned by the writer at most hourly (`analyzer/tracer.py:227`). `queries`, `served` and `ui_events`: until deleted | `/api/forget` removes a session's `served` rows only. Otherwise `rm -rf data/` |
| `data/pending/<sid>.json` | a shipped session waiting for extraction: the redacted turns and messages, owner, project, working directory, redaction mode. Renamed `<sid>.json.working` while the etl extracts it (`analyzer/queue.py:6`). Absent at the end of the measured run because the three sessions had drained | minutes to hours; a session past the day's budget waits for the next day | removed the moment extraction succeeds; `/api/forget`, which drops a queued copy whether or not an earlier extraction of the session is already in the corpus |
| `data/pending/poison/<sid>.json` | a full redacted session whose extraction failed on its own content (unparseable model output). Never retried on its own (`analyzer/queue.py:163`). Not produced in the measured run | forever, until an operator moves it back into `pending/` or deletes it | `/api/forget` on that session (`queue.discard` clears the quarantined copy); `rm -rf` |
| `data/pending/parked/` | written by no code. An operator convention: files moved out of the queue by hand to defer their spend | as long as the operator leaves them | the operator |
| `data/pending/.drain.lock` (measured) | empty; the per-workspace drain file lock (`csa.py:203`) | stays | with `data/` |
| `data/extracted/<sid>.json` (measured) | the extraction cache: summary, worked-on items, the claims with their verbatim evidence quotes, entities, relations, actions, links (`analyzer/incremental.py:33`). Read to skip re-extracting a re-shipped session | until deleted | `/api/forget` on that session, extracted or still in flight (`incremental.drop`, with any half-written `.tmp`). `csa.py org delete` removes a hosted org's copy with its directory; `rm -rf data/` for `default` |
| `data/archive/<sid>.json` | the retained redacted transcript, written only when the workspace's `tenant_config.archive_days` is not 0 (`analyzer/archive.py:83`): owner, project, times, turn counts, redaction mode, the turns and the structured messages. Not produced in the measured run (retention was off) | `archive_days` days from arrival, or `forever`; the etl purges every 60 minutes (`PURGE_MIN`, `analyzer/drain_loop.py:24`), and a read past the window is refused before the purge runs | `/api/forget`; setting `archive_days` back to 0 clears the whole directory on the next purge |
| `data/orgs/<org>/` | for every hosted workspace besides `default`: its own `analyzer.db`, `telemetry.db`, `pending/`, `extracted/` and `archive/` with the same shapes (`analyzer/store.py:78`). An on-prem box that adopted `default` has none | as above | `csa.py org delete <org>`: one `rm -rf` of the directory, plus its tokens and org row |
| `data/analyzer.db.fuzzy` | the fuzzy identifier index sidecar, written by the CLI re-index (`do_index` in `csa.py`, `_build_fuzzy` at `csa.py:171`). Not produced in the measured run | rebuilt on the next re-index | with `data/` |
| `data/backup-<date>/` | a snapshot from `analyzer/backup.py`: `control.db`, every corpus and telemetry file copied through SQLite's online backup API, `pending/` and `extracted/` copied as files, and `manifest.json` with a sha256 per file. Not `archive/`. It carries the `sso` row's client secret and every `llm_providers` credential in plaintext | until deleted; on the same disk as the live data, so copy it off the box | the operator. Neither `/api/forget` nor `org delete` reaches a snapshot |
| `certs/` | `fullchain.pem` and `privkey.pem` (the box's TLS private key) when the customer supplies them; `ca.crt` for a private CA in front of the model endpoint or the issuer. Bind-mounted read-only | until replaced | the operator |
| `.env` | `CSA_PUBLIC_URL` and the model endpoint's key. `chmod 600` (`install-server.md` §2) | | the operator |
| the Caddy volume `caddy-data`, `/data/caddy/` inside the proxy (measured) | `/data/caddy/pki/authorities/local/root.crt` and `/data/caddy/pki/authorities/local/root.key` (the internal CA's private key), `/data/caddy/pki/authorities/local/intermediate.crt` and `/data/caddy/pki/authorities/local/intermediate.key`, `/data/caddy/certificates/local/<host>/<host>.crt`, `/data/caddy/certificates/local/<host>/<host>.key` and `/data/caddy/certificates/local/<host>/<host>.json` (the certificate served on 443), `/data/caddy/instance.uuid`, `/data/caddy/last_clean.json`. The CA files are absent when the customer supplies a certificate | the life of the volume, so laptops trust one root across restarts | `docker compose down -v`. No backup includes it |
| Docker's container logs, `/var/lib/docker/containers/<id>/<id>-json.log` | the stderr of every service: `cli-auth: minted token for <email> (host=<hostname>, org=...)`, `sso: signed in <email> (domain=...)`, `sign-in denied for <email>`, the mcp's `ask(<first 60 characters of the redacted question>) ... user=<email>` line (`mcp_server.py:219`), extraction and judge progress, tracebacks | the json-file driver keeps them unbounded unless the daemon sets `max-size` | removed with the container (`docker compose down`) |
| inside each container, on no volume, on an image built with Claude Code | what the Claude Code binary keeps under its home (`/home/csa/.claude.json`, `/home/csa/.claude/`): its settings and state, no transcripts (`--no-session-persistence`). The prompt goes to it on stdin, never through a file | the life of the container | `docker compose down`, or a recreate |

### control.db, table by table

| Table | Holds | Retention |
|---|---|---|
| `orgs` | id, display name, the invite's sha256, creation time, the sign-in domain | until `org delete`; `default` cannot be deleted |
| `user_tokens` | one row per credential: the token's sha256 (never the token), email, org, hostname (`web-sso` and `web` for browser sessions), created, `revoked_at`, `last_used_at` (refreshed at most hourly, `analyzer/orgs.py:28`), `expires_at` (a sliding idle window, `tenant_config.token_ttl_days`, default 30; 0 never expires), role | a revoked or expired token keeps its row |
| `invites` | per-person invite links: the invited email, the link's sha256, who issued it, expiry, first use, revocation | rows stay |
| `cli_pairings` | the `csa-client setup` handshake: the nonce's sha256, hostname, the approving email and org, whether the identity was verified, the claim time | 15 minutes (`analyzer/cliauth.py:59`), pruned on the next handshake |
| `sso` | one row: issuer URL, client id, display name, who set it, and the client secret in plaintext when the provider needs one (Google, Entra). Plaintext like the `llm_providers` credentials below; a backup carries it (`install-server.md` §4b) | until `csa.py sso clear`, or the next `sso set`, which replaces the row |
| `llm_providers` | one row per model provider an admin added on **AI Models**: name, kind, endpoint, region, project, API version, model ids, who changed it and when, and the credential in plaintext as entered (an API key, `<access key id>:<secret>` for Bedrock, a service account JSON key for Vertex). The API never returns the credential (`analyzer/providers.py`, `public`); anyone who can read the file can. A backup carries it | until an admin removes the provider on AI Models |
| `sso_logins` | a sign-in in flight: the state's sha256, the nonce, the PKCE verifier, the hash of a join link when one was carried, the return path, created | 10 minutes (`analyzer/sso.py:27`); claimed and deleted in one statement at the callback; pruned on every sign-in start; at most 10000 rows |
| `client_directives`, `client_directive_acks` | an admin's "ship me history since <date>" for an email or one machine, and each machine's answer (email, hostname, status, sessions shipped) | rows stay; a revoked directive keeps its row with `revoked_at` |
| `workspace_requests` | the hosted landing page's requests (company, email). Never written on an on-prem box: the route answers 404 while the deployment's `request_webhook` setting is empty | |
| `admin_log` | one row per mutating admin action: time, action, org, detail. `mint_token` rows carry the developer's email and hostname. Reads are not logged, and there is no column for which operator ran the command | outlives the token it records (`revoke` leaves it) and the org (`org delete` leaves it) |
| `alembic_version` | the schema revision | |

## Deletion paths

**`/api/forget`** (`csa-client forget <id>`; the session's owner, the credential that shipped
it, or a workspace admin; `forget` in `apiserver.py`). For an extracted session it deletes the session
row and every per-session row (claims, mentions, relations, actions, links, worked-on items, data
sources, the harvest markers, `served`, `signals`, `pr_links`, `session_workspace`,
`session_metrics`), removes entities and reviews nothing else references, recomputes the facts
the session asserted, rebuilds clusters and the FTS index, and removes `archive/<sid>.json`,
`extracted/<sid>.json` and any queued copy of a re-shipped session. For a session still in the
queue it removes `pending/<sid>.json`, a `poison/` copy, the archived transcript and any extraction
cache. Every file it removes is the caller's own workspace's: the same session id in another
workspace is that workspace's and stays. It does not reach `costs`, `queries` (the questions a
person asked), `spans`, `ui_events`, `admin_log`, the Docker logs, `pending/parked/`, or any
`backup-*/` snapshot. A session the etl is extracting at that moment can be indexed again when the
extraction finishes; forget it again once it shows up.

**`csa.py org revoke <hash>`** sets `revoked_at`; the row and its `admin_log` line stay.

**`csa.py org delete <org>`** removes `data/orgs/<org>/` in one `rm -rf`, the org's tokens and
its row; `admin_log` keeps the deletion and the earlier mint rows. It refuses `default`.

**Deleting everything on an on-prem box**, since `default` cannot be deleted through the CLI:
`docker compose --profile proxy down -v` (the containers, their logs and the Caddy volume), then
`rm -rf data/` on the host, plus `certs/`, `.env`, and any `backup-*/` copied elsewhere.

## What a workspace admin can see about a person

- **The Improve numbers**, for every member of the workspace (`session_metrics`, computed at
  ingest, never backfilled): model, tool calls, failed calls, repeated and re-read calls, the most
  re-read file path, tokens, cost, wall time, idle time, the longest gap.
- **Machines**: `GET /api/org/members` lists each person's credentials with hostname,
  `last_used_at` (hourly precision) and role; `csa.py org fleet` on the box shows the same.
- **Transcripts**, when the workspace keeps them (`archive_days`): the owner and a workspace admin
  can read a retained transcript; teammates cannot (`_transcript` in `apiserver.py`).
- **Fetch directives**: an admin can ask a person's machines for history since a date
  (`csa.py org fetch`); the laptop declines when its owner set backfill off, and the answer
  (`applied` or `declined`, and how many sessions) is recorded.
- **Usage events**: `POST /api/track` rows (page views, searches, flags, per user) feed the web app
  use section of Admin > Analytics (`/api/workspace/analytics`).
- **Analytics**: workspace admins see counts per week and per person from `queries`, `served`,
  `costs` and the credentials: who shipped, who contributed facts, how many teammates their facts
  reached, which facts were reused most. The analytics never show a question's text; the recall log
  below does.
- **The recall log**, to every member, not only admins: the text of every answered question and the
  session it came from (`GET /api/sessions`, `analytics.recall_log`), without the asker's name. A
  question that found nothing is shown only to its asker. Questions are redacted at the workspace
  floor before they are stored, from `/api/ask` and the MCP `ask` tool alike.
- **Flags**: a reviewer's `wrong` (or `noise`, `rejected`, `already-documented`) hides a fact from
  recall (the prompt hook, every `/api/ask` tier, a lookup on the MCP `ask` tool), as does a
  superseded or refuted validity window. The fact itself stays in `hidden_context` and `facts`,
  readable by its id with its label and by a relational MCP `ask` query, until the session is
  forgotten.
- **On the box, not through the API**: `queries` and `served` hold every question asked and every
  fact served, per person; `admin_log` and the Docker logs hold emails and hostnames.

## Availability

One api container, one mcp container, one etl container, by design: the api's judge lock and
request-rate window are in-process (`apiserver._JUDGE_LOCK`, `apiserver._WSREQ_HITS`) and the
etl's timers are per process (`drain_loop._COMPACT_LAST`, `_PURGE_LAST`). Two api containers would run two judges and double the rate
allowance; two etl containers would contend for the per-workspace drain lock. SQLite allows one
writer per file: the corpus, its telemetry sibling and `control.db` are three files so that a
write to one never queues behind another, and a second box cannot share them (SQLite on NFS
corrupts, `install-server.md` §0). The services carry `restart: unless-stopped`, and `migrate`
runs before them on every `up`.

Recovery point: the last verified snapshot. The `backup` compose profile runs `csa.py backup
--keep 7` once a day, writing a verified snapshot of every plane to `data/backup-<date>/`, on the
same disk as the live data it protects (`install-server.md` §6). The queue on disk survives a
crash, so a shipped session is extracted after a restart; spans not yet flushed are lost, by
design. Recovery time: the restore drill in `install-server.md` §6 is manual (stop the writers,
restore, verify, start).

`GET /api/health` answers before authentication (the `/api/health` branch of `do_GET` in
`server/apiserver.py`) with `ok`, `control_plane` (`ok` or `unreadable`), `version` (the image
build, `migrations.image_version()`), `schema` (the three Alembic heads, one per SQLite file:
control, corpus and telemetry, `_schema_heads()`), `mcp_port`, `mcp_path` and `mcp_url`. An
anonymous caller on the network learns the exact build and the exact migration state with no
credential. The box sits inside the customer's network, not the open internet; if even that is
unacceptable, front `/api/health` with the proxy's allowlist instead of routing it straight
through.

## Laptops

Everything `csa-client` and the installer write, and the two files Claude Code itself writes for
CSA. Measured on the harness laptop after install, setup, three ships and an update.

| Path | Holds | Who can read it | Retention, removal |
|---|---|---|---|
| `~/.csa/config.json`, mode 0600 (measured) | the server URL, the person's identity, the per-user bearer token (`auth_token`), optional Cloudflare Access ids, the backfill choice, `sweep_from`, the retention window the server last reported, the redaction policy, the consent record (policy version, identity and retention at consent) (`client/config.go`) | the user, and anyone with the user's files | `uninstall --purge` |
| `~/.csa/shipped/<sid>` (measured) | one marker per shipped session: the byte size shipped so far, the dedup key (`client/ship.go:96`). A `.inflight` sibling exists while a ship runs | the user | `uninstall --purge` |
| `~/.csa/ship.log`, mode 0600 (measured) | the stderr of every detached ship and sweep: session ids, byte counts, HTTP failures, the 401 notice. Appended, never rotated by the client (`client/ship.go:29`) | the user | `uninstall --purge` |
| `~/.csa/backfilled` | session ids the login watcher's backfill already sent (`init` flow only) | the user | `uninstall --purge` |
| `~/.csa/only`, `~/.csa/ignore` | project globs to index or skip (`csa-client only pick`, `csa-client ignore`) | the user | `uninstall --purge` |
| `~/.csa/redact.deny`, `~/.csa/redact.allow` | literal terms to strip or keep on every ship | the user | `uninstall --purge` |
| `~/.csa/directives/<id>` | how many sessions a history directive has shipped so far (`client/ship.go:367`) | the user | removed when the directive completes; `uninstall --purge` |
| `~/.csa/auth_broken` | an empty file, or a timestamp, after a 401; the next prompt's hook shows the sign-in-again notice (`client/authstatus.go:26`) | the user | cleared on the next accepted call; `uninstall --purge` |
| `~/.csa/team.json`, `~/.csa/client.log` | the cached roster from `/api/team`; the login watcher's log (`init` and `install` flows only) | the user | `uninstall --purge` |
| `~/.csa/bin/csa-client`, mode 0755 (measured) | the copy of the binary the hooks execute (`client/hook.go:360`) | the user | `uninstall --purge` |
| `~/.local/lib/csa/csa-client` (measured), `~/.local/bin/csa-client` | what `curl <server>/install \| sh` installed, and its symlink | the user | `uninstall` |
| `~/.claude/settings.json` (measured) | three hook entries owned by CSA, beside anything else in the file: `UserPromptSubmit` runs `csa-client hook` (3 s), `SessionEnd` runs `csa-client ship` (240 s), `SessionStart` runs `csa-client ship --sweep` (240 s) (`client/hook.go:253`) | the user | `uninstall` removes the three entries |
| `~/.claude.json` | Claude Code's own configuration, not measured: the harness laptop has no Claude Code binary, so this file never exists there and the harness run did not observe it. From the code instead: `registerMCP` in `client/init.go` runs `claude mcp add ... -s user` with `--header "X-CSA-User: <identity>"` and `--header "Authorization: Bearer <the same per-user token>"`; Claude Code writes that as a user-scope MCP server entry to `~/.claude.json`, headers in plaintext. The client also reads the account email from it | the user; Claude Code | `uninstall` runs `claude mcp remove csa -s user` |
| `~/.claude/projects/<project>/<sid>.jsonl` (measured) | Claude Code's own transcripts, the thing the client ships. Not CSA's file | the user | Claude Code |
| `<sid>.owner` beside a transcript | on a bot or CI host set up with `init --delegated` only: the requester's email, written by that host's launcher and read by the client to attribute the session (`client/main.go:457`). Never honoured on a default install | the user | the launcher |
| `~/Library/LaunchAgents/com.csa.client.plist` (an older `*.csa-client.plist` is replaced) | the login watcher (`init` and `install` flows only; the hook flow installs no daemon) | the user | `uninstall` |

`csa-client uninstall` revokes the machine's credential on the server, and removes the three hooks, the MCP
registration, the login watcher, and `~/.local/lib/csa` with its symlink. `uninstall --purge` also removes `~/.csa`
entirely: the config, the consent record, the markers, the logs, the allowlists and the hook binary. Neither touches
`~/.claude/projects` or anything already on the server; run `csa-client forget` before uninstalling, or ask a workspace
admin afterwards.

The Claude Code plugin writes the same `config.json`, `shipped/`, `ship.log`, `auth_broken`, `only`, `ignore`,
`redact.deny` and `redact.allow`, plus these. `/csa:disconnect` removes all of `~/.csa`.

| Path | Holds | Retention, removal |
|---|---|---|
| `~/.csa/sent.jsonl`, mode 0600 | one row per send: time, session id, server, mode, bytes, labels, sha256 of the content, the server's answer. No content | `/csa:disconnect` |
| `~/.csa/preview/<sid>.txt`, mode 0600 | the exact redacted text of a session, written by `/csa:preview` | until you delete it, or `/csa:disconnect` |
| `~/.csa/forgotten` | session ids `/csa:forget` deleted, so no hook sends them again | `/csa:disconnect` |
| `~/.csa/oversize` | time, session id and size of a transcript over the 32 MiB limit | `/csa:disconnect` |
| `~/.csa/connect.pending`, mode 0600 | the pairing nonce, while a connect waits for Authorize | removed on the claim, or when the code expires |
| `~/.csa/connected_notice`, `~/.csa/tls_broken`, `~/.csa/warned_*` | one-shot and hourly notices for the next prompt | removed or refreshed in use; `/csa:disconnect` |
| `~/.claude/plugins/` | Claude Code's copy of the plugin, and its marketplace clone | `claude plugin uninstall csa@csa`, `claude plugin marketplace remove csa`; Claude Code leaves `plugins/cache/csa` until you delete it |
| `~/.claude.json` | in automatic mode, the MCP entry with your bearer token, as for `csa-client`. Claude Code's own backups may hold copies, dead once the token is revoked | `/csa:disconnect` removes the entry and revokes the token |
