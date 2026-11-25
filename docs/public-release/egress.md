# Egress: every outbound connection

What leaves a CSA on-prem installation, measured, per server process and per laptop trigger.
The knob column says what turns each connection off or points it elsewhere. No connection on
this page goes anywhere the operator did not configure, and the harness below fails when one
appears.

## What was measured

The on-prem harness, `server/e2e/onprem.sh` in the CSA repository, run on 2026-09-15 against the compose in this bundle
(`--profile proxy`) on a Docker network with no route out. A DNS sinkhole answered every name
lookup and logged it. A sniffer on the network bridge logged every TCP SYN and UDP datagram. The
model endpoint was an Anthropic-format stub named `stub`, the identity provider an OpenID stub
named `idp`, the proxy `csa`. One laptop installed the client, signed in, paired, shipped three
sessions and asked for recall over HTTP and over MCP. 64 checks passed.

| Observed | Value |
|---|---|
| Names asked at the DNS sinkhole | `csa`, `idp`, `stub` (the compose service names are answered by Docker's own resolver) |
| TCP connections, source to destination | proxy to api:8766; proxy to mcp:58768; api to idp:443; etl to stub:8080; laptop to proxy:443; laptop to idp:443 |
| UDP | api, etl and the laptop to the resolver on port 53. Nothing else |
| Requests at the model stub | 3 `HEAD /api/hello`; 3 `POST /v1/messages?beta=true`, model `claude-sonnet-5`, streamed, `tools=0 mcp_servers=0` |
| Requests at the identity provider | 2 `GET /.well-known/openid-configuration`; 1 `GET /authorize` (the browser's); 1 `GET /jwks`; 1 `POST /token` |

Nothing else asked for a name or opened a connection: not mcp, not migrate, not the proxy. The
judge was switched off for that run, so its `haiku` calls do not appear in it; the switch is now the
workspace setting `autojudge`. The image-only
harness (`server/e2e/onprem-egress.sh`, run on 2026-09-14) measured both aliases:
`sonnet` is sent as `claude-sonnet-5`, `haiku` as `claude-haiku-4-5-20251001`, with no model
catalog fetch.

The figures above are from an image with the Claude Code binary, which is where `HEAD /api/hello` and
`?beta=true` come from. The published image has no binary. The same harness on it (2026-11-05, 70 checks) saw the
same names and destinations, and at the stub only `POST /v1/messages`, with the same model ids, not streamed,
`tools=0 mcp_servers=0`. `server/e2e/onprem-egress.sh` adds that a subscription token set on that image never
reaches the endpoint. A subscription token pasted as an API key or a provider's key is refused before any
request, on either image.

## The server, by process

Every model call goes through one seam, `complete()` in `server/analyzer/llm.py`. A call names a
role, `main` or `small`. With no provider registered on **Admin > AI Models**, both roles run on the
credential in the environment. On the published image, which has no Claude Code, that is
`ANTHROPIC_API_KEY` going to `ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`) through Python
`urllib`, one `POST <base>/v1/messages` per call. On an image built with `--target with-claude-code` it is
the Claude Code binary with `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX`. Either way the ids sent are `claude-sonnet-5` and
`claude-haiku-4-5-20251001`. The rows below say "through the Claude Code binary"; on the published image read
"through Python `urllib`", same host, same trigger. A registered provider changes the host per its kind; see the
AI Models rows below. The three long-running services share the image and the `.env`.

| Process | Host | Port | Purpose | Trigger | Cache or frequency | Knob |
|---|---|---|---|---|---|---|
| api | the identity provider (`sso.issuer`) | 443 | `GET <issuer>/.well-known/openid-configuration`: where the authorization, token and JWKS endpoints are | `csa.py sso set` (once, to check the issuer names itself); then any sign-in that finds no cached document, or one older than the TTL | in-process cache, 3600 seconds (`JWKS_TTL`, `analyzer/auth.py:180`, `:314`) | the `sso` row: `csa.py sso set` and `sso clear`. Without a row the server never contacts an issuer |
| api | the issuer's `token_endpoint` | 443 | `POST` the authorization code, the PKCE verifier, the client id, the redirect URI, and the client secret when one is stored (`analyzer/sso.py:110`) | every sign-in callback | once per sign-in | the `sso` row |
| api | the issuer's `jwks_uri` | 443 | `GET` the signing keys that verify the id_token | the sign-in callback | cached 3600 seconds; one forced refetch when a token names a `kid` the cache lacks (`analyzer/auth.py:207`), then the sign-in fails | the `sso` row |
| api, through the Claude Code binary | the model endpoint, `ANTHROPIC_BASE_URL` | as configured | the `small` role (`haiku` by default): the judge's boot sweep labels facts never labelled and re-checks near-duplicate clusters (`_spawn_boot_sweep` in `apiserver.py`) | once at api boot, per workspace whose `autojudge` is on | zero calls when there is nothing to label | `csa.py org config <org> --set autojudge=off` disables it; `ANTHROPIC_BASE_URL` points it |
| mcp, through the Claude Code binary | the model endpoint | as configured | the `small` role to classify a question whose scope is ambiguous between the asker, a named person and the team (`mcp_server.py:173`, the question redacted at the workspace floor first); the `main` role to plan a relational question into one read-only query, plus one repair retry when that query fails (`analyzer/ask.py:194`) | an `ask` tool call from Claude Code | per qualifying question; a plain lookup, a `flag` and a `fact` call make none | the endpoint knobs |
| etl, through the Claude Code binary | the model endpoint | as configured | the `main` role (`sonnet` by default): extraction of one drained session (`analyzer/extract.py:91`); the `small` role: the judge on each drained batch (`_judge` in `analyzer/drain_loop.py:76`) | every 10 seconds (`TICK_S`, `analyzer/drain_loop.py:20`) while `pending/` holds sessions | one extraction per session; `tenant_config.daily_usd` and `daily_tokens` hold sessions past the day's cap in the queue | the AI Models role choices, `autojudge`, the endpoint knobs |
| api, mcp, etl, an **Anthropic** provider on AI Models, through Python `urllib` | the provider's endpoint, else `https://api.anthropic.com` | 443 | `POST <endpoint>/v1/messages` with the same prompts (`_anthropic` in `analyzer/llm.py:160`); the binary is not run for that role | the same triggers, for each role set to this provider | the same frequencies | the provider and the role choices on AI Models |
| api, mcp, etl, an **OpenAI** provider on AI Models (OpenAI or any OpenAI-compatible endpoint), through Python `urllib` | the provider's endpoint, else `https://api.openai.com/v1` | 443 | `POST <endpoint>/chat/completions` (`_openai` in `analyzer/llm.py:155`) | the same | the same | the same. Not exercised by the harness |
| api, mcp, etl, an **Azure OpenAI** provider on AI Models, through Python `urllib` | the provider's endpoint (required) | 443 | `POST <endpoint>/openai/deployments/<model>/chat/completions?api-version=<version>`, version `2024-10-21` unless the provider names one (`_azure` in `analyzer/llm.py:185`) | the same | the same | the same. Not exercised by the harness |
| api, mcp, etl, an **AWS Bedrock** or **Google Vertex** provider on AI Models, through the Claude Code binary, on an image that has it | the cloud's endpoint for the provider's region, chosen by the binary | 443 | the binary run with that provider's credential only: `CLAUDE_CODE_USE_BEDROCK=1` and `AWS_REGION`, or `CLAUDE_CODE_USE_VERTEX=1`, `CLOUD_ML_REGION` and `ANTHROPIC_VERTEX_PROJECT_ID` (`claude_env` in `analyzer/providers.py:172`) | the same | the same | the same. Not exercised by the harness |
| api, mcp, etl | the `otlp_endpoint` setting, seeded from the `telemetry.otlpEndpoint` chart value | as configured | `POST /v1/traces`, protobuf spans (`_post` in `analyzer/tracer.py:238`) | every span flush | off by default: unset means no attempt (`analyzer/tracer.py:189`) | `csa.py org config <org> --set otlp_endpoint=`; `telemetry.otlpEndpoint` on Kubernetes |
| api | the `request_webhook` setting | as configured | `POST` one JSON line when the hosted landing page receives a workspace request (`_notify_new_request` in `apiserver.py`) | never on an on-prem box: without the setting the route answers 404 | off by default | `csa.py org config <org> --set request_webhook=` |
| etl, through a `gh` subprocess | GitHub's API (`gh api`, so `api.github.com` unless `GH_HOST` says otherwise) | 443 | `gh api /orgs/<org>/members` when a workspace's `tenant_config.registry_sources` names `github:<org>` (`_gh_members` in `analyzer/verify.py:107`) | every 60 minutes (`REGISTRY_MIN`, `analyzer/drain_loop.py:21`) | off by default (the field is empty). The image ships no `gh`, so the call fails before anything leaves | `csa.py org config <org> --set registry_sources=` |
| proxy (Caddy) | none | | `ocsp_stapling off`, no ACME (`tls internal`, or the certificate in `certs/`), `skip_install_trust` (`server/Caddyfile`) | | | it reaches only `api:8766` and `mcp:58768` |
| migrate | none | | | | | |
| every container | the host's DNS resolver | 53 UDP | resolving the model endpoint, the issuer and the compose service names, through Docker's embedded resolver | | | the host's resolver; the harness pointed it at the sinkhole |

The `/authorize` request is the browser's, not the server's: the api answers a sign-in start with
a redirect and the person's browser follows it (`install-server.md` §4b). The laptop's own hook call,
`POST /api/ask`, makes no model call on the server: the route is deterministic, recall is lexical
and the fallback ladder stays lexical (`_hook_ask` and `_fallback` in `apiserver.py`).

An endpoint typed on AI Models must be a public `https` address: the server resolves it, refuses any
internal address, connects to the address it checked, and follows no redirect and no proxy
(`analyzer/netguard.py`). A model gateway on a private address is reached through
`ANTHROPIC_BASE_URL` in the environment instead. That path is the operator's own: it may be private or plain
`http` and honours `HTTPS_PROXY`, and it follows no redirect.

NTP is the host's concern, not a container's: sign-in tokens tolerate 60 seconds of skew
(`analyzer/auth.py:27`).

## The Claude Code binary, on an image built with it

The published image and the bundle have no Claude Code. `docker build --target with-claude-code` adds
a third-party binary: `@anthropic-ai/claude-code` 2.1.270, installed from its
npm platform package (`@anthropic-ai/claude-code-linux-x64` or `-arm64`) as
`/usr/local/bin/claude`, sha256 pinned (`server/Dockerfile`). Its npm licence field says
`SEE LICENSE IN LICENSE.md`: Anthropic's commercial terms, not an open source licence. The
SBOM of that image lists it as a package with that version and checksum.

| Fact | Value |
|---|---|
| Invocation | `claude -p --model <alias> --output-format json --safe-mode --strict-mcp-config --tools "" --no-session-persistence`, the prompt on stdin, the working directory a temp dir (`analyzer/llm.py:89`, `:127`) |
| When both roles run on an Anthropic, OpenAI or Azure OpenAI provider | present in that image, never executed |
| Requests | `HEAD <ANTHROPIC_BASE_URL>/api/hello` before each call, then `POST <ANTHROPIC_BASE_URL>/v1/messages?beta=true`, streamed. A gateway that answers 404 to the HEAD still works |
| Model ids | `sonnet` is sent as `claude-sonnet-5`, `haiku` as `claude-haiku-4-5-20251001`. A gateway allowlist needs the ids, not the aliases. `--model sonnet` resolved with no catalog fetch |
| Tools | `tools=0 mcp_servers=0` on every call the server makes; the binary's own base system prompt rides along |
| Credential | `ANTHROPIC_API_KEY` goes to the endpoint you configure. `CLAUDE_CODE_OAUTH_TOKEN` (a Claude subscription login) reaches only the binary, was not measured against a real endpoint, and is refused on an image without it. `CLAUDE_CODE_USE_BEDROCK` and `CLAUDE_CODE_USE_VERTEX` send the binary to those clouds instead and were not measured |
| Opt-outs baked into that image | `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER`, `DISABLE_BUG_COMMAND`, `DISABLE_FEEDBACK_SURVEY`, `DISABLE_INSTALLATION_CHECKS`, `DISABLE_COST_WARNINGS`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `CLAUDE_CODE_DISABLE_ORG_MEMORY`, `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL`, all set to `1`. Each name is present in the 2.1.270 binary (`strings`, 2026-09-14); what they stop is what the harness measures, not what the names promise |
| Hosts named inside the binary that were never asked for | `downloads.claude.ai`, `sentry.io`, `datadoghq.com`, `storage.googleapis.com`, `api.anthropic.com`, `platform.claude.com`, `registry.npmjs.org` |
| Private CA, egress proxy | `NODE_EXTRA_CA_CERTS`, `HTTPS_PROXY` and `NO_PROXY` on the api, mcp and etl services (the `# ca:` lines in the compose). Python reads `SSL_CERT_FILE` for the issuer and the AI Models HTTPS providers, which do not use `HTTPS_PROXY` |

## Laptops

The client is `csa-client` (`client/*.go`), a Go binary with no model and no key. It talks to
`CSA_PUBLIC_URL` and nothing else: no GitHub, no registry, no telemetry. Every call after pairing
carries `Authorization: Bearer <per-user token>` (`client/main.go:473`), and `curl`, the client
and Claude Code honour `HTTPS_PROXY`.

| Call | Trigger | What it carries | Code |
|---|---|---|---|
| `GET /api/health` | `csa-client setup` and `init`, before consent; `doctor` | nothing, no credential | `client/init.go:569` |
| `GET /api/policy` | `setup`, before consent: the workspace's redaction floor, for the notice | the previous token when one exists | `client/setup.go:407` |
| `POST /api/cli-auth/new`, then `GET /api/cli-auth/<nonce>` | `setup`, the pairing | the machine's hostname; the poll returns the token once the browser approved | `client/setup.go:215`, `:372` |
| `GET /api/client-config` | SessionStart (`ship --sweep`) | the token; the answer is the retention window and any history directive | `client/ship.go:301` |
| `POST /api/client-config/ack` | after a directive was applied or declined | the directive id, a status, the number of sessions shipped, one detail line | `client/ship.go:327` |
| `POST /api/ask` | UserPromptSubmit | the prompt, after client-side redaction in `secrets` mode and query rewriting; skipped when the prompt has fewer than two content words or a secret survives redaction (`client/hook.go:63` to `:92`); the session id, the identity, the working directory, the project name, `k=6`; 1.8 s timeout | `client/hook.go:169` |
| `POST /api/ingest` | SessionEnd (`ship`); the SessionStart sweep for sessions that crashed, at most 10 per sweep; `init` sends one `dry_run` line | the session id; the transcript, redacted at the configured mode (floor `secrets`, plus `~/.csa/redact.deny` and `redact.allow`); the owner identity; the redaction mode. The hostname is not in the body, the server reads it off the token. The working directory is inside the transcript lines | `client/main.go:562`, `client/ship.go:224`, `client/init.go:736` |
| `GET /latest/checksums.txt` | `csa-client version` on an interactive terminal with a stamped build (the update notice); `update` | the token, since the client adds its auth headers to every request (`client/update.go:158`) | `client/update.go:196`, `:229`; `client/main.go:719` |
| `GET /latest/<asset>` | `update`; the installer (`curl <server>/install \| sh`) | the token from `update`; nothing from the installer's `curl` | `client/update.go:309`; `server/install.sh` |
| `GET /api/team` | `csa-client team`, `doctor`, `init` | the token | `client/init.go:853`, `client/doctor.go:231` |
| `POST /api/forget` | `csa-client forget` | session ids, the owner identity | `client/main.go:799` |
| `POST /mcp` | Claude Code's own MCP client: on start, and on each `ask`, `flag` or `fact` tool call | the `Authorization: Bearer` and `X-CSA-User` headers registered with `claude mcp add` (`client/init.go:624`) | Claude Code, not the client |

The seven automatic calls, the ones nobody types, are health and policy before consent,
client-config and its ack at SessionStart, ask at UserPromptSubmit, ingest at SessionEnd, and the
checksums fetch on an interactive `version`.

### The Claude Code plugin

The plugin (`csa-plugin/scripts/`) is the other client, in Python, with no compiled artifact. Installing it is
Claude Code's work: `claude plugin marketplace add` clones from GitHub, or from wherever your own mirror is. Once
it is on the machine, one function makes every call (`_request` in `csa.py`), to the `server` in
`~/.csa/config.json` and nowhere else. It follows no redirect, so a bearer token cannot be carried to another
host. Every call has the user agent `csa-plugin/<version>` and honours `HTTPS_PROXY`.

| Call | Trigger | What it carries | Code |
|---|---|---|---|
| `POST /api/cli-auth/new`, then `GET /api/cli-auth/<nonce>` | `/csa:connect` | the machine's hostname and the mode; the poll returns the token once the browser approved | `cmd_connect`, `child_poll` |
| `GET /api/health` | after the claim, automatic mode: where the MCP endpoint is | nothing, no credential | `register_mcp` |
| `POST /api/token/revoke` | `/csa:disconnect`, a re-connect replacing an old token | the token being ended | `cmd_disconnect`, `finish_connect` |
| `POST /api/ask` | UserPromptSubmit, automatic mode | the prompt, after query rewriting and redaction at your mode with your deny and allow lists; skipped when it has fewer than two content words, a secret survives, or the project is excluded by `only` or `ignore`; the session id, the identity, the working directory, `k=6`; 1.8 s total | `hook_recall` |
| `POST /api/ingest` | SessionEnd of a session started after you connected, automatic mode; the SessionStart sweep, at most 10 per sweep; `/csa:send-session` | the session id; the transcript, redacted at your mode with your lists; the mode. Nothing else. The owner comes from the token | `send` |
| `GET /api/client-config` | SessionStart, automatic mode; `/csa:send-session` | the token; the answer is the retention window and any history directive | `child_sweep`, `cmd_send_session` |
| `POST /api/client-config/ack` | a history directive, always `declined`: the plugin ships no history | the directive id, `declined`, `shipped: 0`, one detail line | `child_sweep` |
| `GET /api/me` | `/csa:status` | the token | `cmd_status` |
| `GET /api/team`, `POST /api/forget` | `/csa:team`, `/csa:forget` | the token; session ids and the identity | `cmd_team`, `cmd_forget` |
| `POST /mcp` | Claude Code's own MCP client, automatic mode only | the bearer and `X-CSA-User` headers registered with `claude mcp add` | Claude Code, not the plugin |

Before `/csa:connect`, Python does not start and nothing leaves. In manual mode the hooks return at once:
the only calls are the ones `/csa:send-session`, `/csa:status`, `/csa:team`, `/csa:forget` and `/csa:disconnect`
make when you run them.

## What the harness refuses

`server/e2e/onprem.sh` fails the run when the sinkhole sees any name other than
`csa`, `stub`, `idp` and the compose service names, or when any packet leaves the internal
subnet. The forbidden list it names explicitly: `downloads.claude.ai`, `sentry.io`,
`datadoghq.com`, `storage.googleapis.com`, `registry.npmjs.org`, `api.anthropic.com`,
`claude.ai`, `platform.claude.com`, `api.openai.com`, `pypi.org`, `files.pythonhosted.org`,
`deb.debian.org`, `ghcr.io`, `github.com`, `accounts.google.com`, `googleapis.com`,
`letsencrypt.org`, any `ocsp.` host, and `statsig`.

On 2026-09-15 the harness observed only `csa`, `idp` and `stub`.
