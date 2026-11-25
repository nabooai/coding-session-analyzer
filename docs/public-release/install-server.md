# Install the CSA server (on-prem runbook)

One CSA server for one company, inside that company's network: your model endpoint, your identity
provider, TLS terminated on the box, and no connection to anything else. Sections 0 to 5 are the
first-boot runbook, numbered, each step with what you should see. Sections 6 to 8 cover backup,
the model provider in depth, and releases. For a guided install run the `csa-install` skill.

> **All model work happens here, not on laptops.** The server extracts facts on ingest and helps
> recall, through the endpoint you point it at: a Claude credential in `.env` (an **Anthropic-format**
> gateway is `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`), or a provider registered on Admin > AI Models
> (Anthropic, OpenAI or an OpenAI-compatible endpoint, Azure OpenAI; AWS Bedrock and Google Vertex on an
> image you build with Claude Code, §7). Laptops send raw sessions and call no model. The exact model ids the server
> sends are in §4e, for the gateway allowlist.

## 0. Prerequisites

- **Docker Engine 24 or newer with Compose v2.20 or newer**: `docker compose version` prints
  `Docker Compose version v2.2x` or later (`profiles` and `service_completed_successfully` need it).
- **NTP.** Sign-in tokens carry timestamps and the server tolerates 60 seconds of skew, no more.
  `timedatectl show -p NTPSynchronized` prints `NTPSynchronized=yes`.
- **Local disk for `data/`.** SQLite on NFS or SMB corrupts under concurrent writers. `df -T data`
  (after §1) shows ext4, xfs or similar, not nfs.
- **`sha256sum`** (coreutils) to verify the offline bundle.
- **The kit**, from the offline bundle or the registry: the server image (it carries the client
  binaries laptops install), `docker-compose.trial.yml`, `Caddyfile`, `.env.example`, this file.
- **Network:** the box reaches your model endpoint and your identity provider; laptops reach the box
  on 443; nothing else. The identity provider never calls the box (the browser carries the redirect).
- **A DNS name for the box** (the one in `CSA_PUBLIC_URL`) and, optionally, a certificate for it from
  your CA. Without one the proxy issues its own from an internal CA (§3a).
- **The image.** Registry: `docker pull ghcr.io/nabooai/csa-server-slim:<version>`. Bundle:
  `docker load -i csa-server-slim-<version>-<arch>.tar` prints `Loaded image: csa-server-slim:<version>`.
  Either way, set `CSA_SERVER_IMAGE` in `.env` to what you have. Or build it from the root of a checkout
  with `docker build -f server/Dockerfile -t csa-server:local .`. None of these include Claude Code; an API
  key or a provider on AI Models does not need it. Bedrock, Vertex and subscription tokens do: §7.

Everything below runs in one directory holding the kit. Set these once per shell so the commands read
the same as here:

```bash
export COMPOSE_FILE=docker-compose.trial.yml COMPOSE_PROFILES=proxy
```

## 1. Lay out the directory

The server runs as uid 10001 inside every container. `migrate` hands `data/` to that uid on
first boot when it cannot write there, so make the directories and nothing more (colima cannot
create a missing bind-mount directory itself):

```bash
mkdir -p data certs config
ls -ln
```

Expected: the three directories, plus `docker-compose.trial.yml`, `Caddyfile` and `.env.example`.

- `data/` holds the control plane (`control.db`), the corpus, the queue and the transcripts.
- `certs/` holds your certificate for the box (§3a) and, when needed, your corporate CA root (§3b).
- `config/` may hold `config.json`, the file the Helm chart renders: `workspace.name`, `.domain` and
  `.adminEmail` stand in for §4a and §4d (see `docs/quickstart.md`).

If files inside `data/` belong to another uid (an image that once ran as root), `/api/health` answers
`{"ok": false, "control_plane": "unreadable"}` and every credential is refused with 401, because the
api cannot read the file every token is validated against: `sudo chown -R 10001:10001 data`.

## 2. Write `.env`

```bash
cp .env.example .env && chmod 600 .env
```

Edit it. `CSA_PUBLIC_URL` is required and is the URL people will type (`https://csa.example.com`: no
port, since the proxy listens on 443 and refuses to start otherwise; no path; no trailing slash);
every installer, sign-in redirect and Connect one-liner is built from it, never from a request's Host
header, and laptops register the MCP server at `<CSA_PUBLIC_URL>/mcp`. Then keep at most one model path: an
Anthropic-format gateway (`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`), or none, and register any
provider (OpenAI and Azure OpenAI included) on Admin > AI Models after §4d. Set
`CSA_SERVER_IMAGE` to the image you loaded. There is no shared secret for an on-prem install: nobody
signs in until §4 connects the identity provider.

That is the whole file. Model providers are **Admin > AI Models** in the app once §4d makes you an
admin; redaction, retention and the rest are `csa.py org config default --set field=value` from this
directory, stored in `./data`, and read on their next use by every container, with no restart. See §4f.

Check it before booting:

```bash
docker compose config --services
```

Expected: `migrate`, `api`, `etl`, `mcp`, `proxy`. A missing `CSA_PUBLIC_URL` stops here with
`required variable CSA_PUBLIC_URL is missing a value: set CSA_PUBLIC_URL in .env ...`.

## 3. Bring it up

```bash
docker compose up -d
docker compose ps -a
```

`migrate` runs first and brings every data file to the current schema; `api`, `mcp` and `etl` are
declared to start only after it exits 0, and `proxy` after those. Expected in `ps -a`: `migrate`
with `Exited (0)`, `api` and `mcp` `healthy` (within about 30 seconds), `etl` and `proxy` `running`.
Then:

```bash
docker compose logs migrate
```

Expected: three lines, `control: <revision>`, `corpus: <revision>`, `telemetry: <revision>`. A file
that cannot be migrated exits non-zero, `up` stops with `service "migrate" didn't complete
successfully: exit 1`, the reason is in `docker compose logs migrate` (`migrate failed: ...`), and
none of the three services runs; nothing serves a half-migrated file. A second `up` is a no-op:
`migrate` finds every file at head and prints the same three lines.

```bash
curl -sk https://<your host>/api/health
```

Expected: a JSON object with `"ok": true`, `"control_plane": "ok"`, `"version"`, `"schema"`,
`"extraction": {"stalled": false, ...}`, `"mcp_port": 58768`, `"mcp_path": "/mcp"` and `"mcp_url":
"https://<your host>/mcp"`. `-k` only until §3a is done. Ports 8766 and 58768 answer on the box's
loopback only; the proxy on 443 is the one way in from the network.

### 3a. TLS

The proxy serves `CSA_PUBLIC_URL` on 443. If `certs/fullchain.pem` and `certs/privkey.pem` exist
(PEM, the chain first) it uses them; restart the proxy after dropping them in
(`docker compose restart proxy`). Otherwise it issues a certificate from its own internal CA, whose
root every laptop must trust once. Export it:

```bash
docker compose exec proxy cat /data/caddy/pki/authorities/local/root.crt > csa-root.crt
```

Expected: a PEM block beginning `-----BEGIN CERTIFICATE-----`. The root persists in a named volume,
so restarts and upgrades keep the same one. On a laptop: macOS
`sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain csa-root.crt`;
Linux `sudo cp csa-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`.
Claude Code itself reads `NODE_EXTRA_CA_CERTS=/path/to/csa-root.crt`; set it in the shell that
runs Claude Code or the MCP registration in §5 will not connect. The proxy makes no outbound
connection: no ACME, no OCSP.

### 3b. A private CA in front of the model endpoint or the identity provider

If either presents a certificate from your corporate CA, put that root (PEM) at `certs/ca.crt` and,
in `docker-compose.trial.yml`, uncomment the four `# ca:` environment lines and the `# ca:` certs
mount in each of `api`, `mcp` and `etl`, then `docker compose up -d`. `SSL_CERT_FILE` is read by
Python (the identity provider, discovery, every HTTPS model path) and is loaded beside the public
roots; `NODE_EXTRA_CA_CERTS` by the Claude Code binary, on an image that has it. `HTTPS_PROXY`
and `NO_PROXY` sit in the same block for a box that reaches its endpoint through an egress proxy.
These are the standard variables those programs read, not CSA settings.

## 4. First boot: name the workspace, connect the identity provider, sign in

### 4a. Name the workspace

```bash
docker compose exec api /app/.venv/bin/python csa.py org adopt-default "<Company>" --domain <company.com> --yes
```

Expected: `adopted: default is now the org '<Company>'` plus explanatory text. `--domain` is who may
sign in: an account the identity provider verifies as `<company.com>` lands in this workspace,
anything else is refused. `--yes` skips the confirmation prompt, which cannot be answered through
`docker compose exec` without a terminal.

### 4b. Connect the identity provider

Create an OpenID Connect application at your provider (Okta: "OIDC, Web application"; Entra: "Web";
Keycloak: a confidential or public client). It needs the `openid email profile` scopes and one
redirect URI, which the next command prints. Okta and Keycloak public clients need no secret; Entra
Web applications and Google do (`--client-secret-stdin`).

```bash
docker compose exec api /app/.venv/bin/python csa.py sso set \
  --issuer https://<your-tenant>.okta.com/oauth2/default --client-id <client id> --name "<Provider>"
# with a secret:  printf '%s' '<secret>' | docker compose exec -T api /app/.venv/bin/python csa.py sso set ... --client-secret-stdin
```

Expected:

```
sign-in:       <Provider> (https://<your-tenant>.okta.com/oauth2/default)
client id:     <client id>
client secret: none (public client, PKCE only)
redirect URI:  https://<your host>/api/sso/callback
```

Register that redirect URI with the provider. The command fetched the provider's discovery document
to prove the issuer names itself, so a wrong issuer or an untrusted certificate (§3b) fails here,
not at the first sign-in. No restart is needed: the issuer is read from `data/control.db` on every
request, and the next command works immediately. From this point the invite and typed-email sign-in
paths are refused; only the provider mints a session.

### 4c. First sign-in

```bash
curl -k https://<your host>/
```

Expected: the static app shell, with no `accounts.google.com` script tag anywhere in it (this is
generic OIDC, not the hosted service's Google-only sign-in) and no "Request a workspace" form (that
form belongs to the hosted service and appears only where the `request_webhook` setting is set, which an
on-prem box never does). curl proves the shell; the one sign-in button, "Sign in with <Provider>", is
confirmed by opening the same URL in a browser. The button completes the round trip at your provider
and returns to the workspace, signed in as the provider's verified address. Every first sign-in is a
member.

### 4d. The first admin

```bash
docker compose exec api /app/.venv/bin/python csa.py org role default <you@company.com> admin
```

Expected: `<you@company.com> is now admin in default (1 token(s) updated)`. The address must have
signed in once already (§4c), or the command says so and exits 1. Admins see the Admin and Team
pages, invite people outside the domain with per-person links, and change roles.

**Checkpoint.** The server itself is now verified: health, the workspace, the identity provider, a
completed sign-in and an admin. What is left needs things this box cannot supply on its own. The
gateway smoke in §4e needs the LLM endpoint reachable, and connecting a laptop in §5 needs a laptop
plus a person to sign in through the IdP. Both can be done later without redoing anything above.

### 4e. The model endpoint smoke

From the box, exactly as extraction runs it:

```bash
docker compose exec etl /app/.venv/bin/python -c "from analyzer import llm; print(llm.complete('reply OK')[0])"
```

Expected: `OK`. Extraction sends **`claude-sonnet-5`** and the judge and the router
**`claude-haiku-4-5-20251001`**, so those are the ids your gateway must allow (the main model on
Admin > AI Models changes the first, and runs one call like this before it saves).

A provider on Admin > AI Models runs the same one-line test call before it saves.

On a `with-claude-code` image (§7) the bare binary answers too:

```bash
docker compose exec etl claude -p 'reply OK' --model sonnet --output-format json
```

Expected: one JSON object with `"is_error": false`, `"result": "OK"` and a `modelUsage` key naming
the model the gateway saw. The ids are the same two. Before each call the binary sends `HEAD <base>/api/hello`; a gateway that
answers 404 to it still works.

### 4f. Models and settings

**Admin > AI Models** registers the model providers (Anthropic, AWS Bedrock, Google Vertex, OpenAI,
Azure OpenAI) and picks the main model (reads sessions, answers questions) and the small model
(judges facts, routes questions). A provider is saved only after a one-line test call answers. With
none registered, CSA uses the Claude credential in `.env` on sonnet and haiku.

Everything else (redaction level and hidden terms, transcript retention, sign-out window, parallel
extractions, an optional daily spend cap, which defaults to none) is `csa.py org config`:

```bash
docker compose exec api /app/.venv/bin/python csa.py org config default                       # every field, its scope and value
docker compose exec api /app/.venv/bin/python csa.py org config default --set min_redaction=privacy
```

## 5. Connect a laptop

Laptops install with `curl -sSL https://<your host>/install | sh`. The server serves the installer
and the binaries itself; nothing on a laptop needs GitHub or a registry. The image builds the five
client binaries (`csa-client-{darwin,linux}-{amd64,arm64}`, `csa-client-windows-amd64.exe`) and
`checksums.txt` from the same commit as the server, stamped with the server's version, so upgrading
the image is what ships a newer client. The installer verifies each download against
`checksums.txt`, and `csa-client update` on a laptop compares itself with the same file and fetches
from `/latest/`.

Verify from the box:

```bash
curl -sSL https://<your host>/install | head -3
curl -so /dev/null -w '%{http_code} %{size_download}\n' https://<your host>/latest/csa-client-linux-amd64
```

Expected: a POSIX `sh` script whose second line names the server, then `200` and the file's size. A 421 on `/install` means `CSA_PUBLIC_URL` is not set on the api service; a 404 on
`/latest/...` means the image carries no client binaries: run one built from `server/Dockerfile`.

On a laptop that trusts the root from §3a:

```bash
curl -sSL https://<your host>/install | sh
csa-client setup --server https://<your host>
```

`setup` prints the consent notice, opens the browser at the sign-in from §4c, and once the person
authorizes the machine it writes `~/.csa/config.json`, installs the three Claude Code hooks and
registers the `csa` MCP server at `https://<your host>/mcp`. Expected last line:
the client reporting it is connected to `https://<your host>`. `/install`, `/latest/*`, `/cli-auth` and
`/api/cli-auth/*` are pre-auth on purpose (a laptop that has never met CSA holds no credential) and
expose only the installer, the public binaries and the pairing page; every corpus route stays gated.

### 5a. The open-egress trial (shared invite)

The same file without `--profile proxy` is the self-host trial for a small trusted team: api and
mcp answer on `127.0.0.1:8766` and `127.0.0.1:58768` for an SSH tunnel, a reverse proxy of your own
that terminates TLS, or a Cloudflare tunnel (route `/mcp` to 58768 and everything else to 8766, and
let `/install`, `/latest/*`, `/cli-auth` and `/api/cli-auth/*` through unauthenticated at the edge).
Set `CSA_AUTH_TOKEN` in `.env` (`openssl rand -hex 32`): it is the shared invite every laptop types
once at `csa-client setup`, and the email each person types is self-asserted. The moment you run
`adopt-default` or create an org that secret is refused everywhere and the box is on the runbook
above. `CSA_PUBLIC_URL` is still required. `CSA_MCP_URL` is derived from it, so a tunnel or proxy
must route `/mcp` by path; a loopback trial with no proxy sets
`CSA_MCP_URL=http://127.0.0.1:58768/mcp` in `.env` so laptops register the mcp port directly.

## 6. Back up and restore

**Your corpus is not the only file that matters.** `data/control.db` holds every user token your
developers' clients authenticate with, plus the org and audit rows; `data/*/pending/` holds sessions
a client has already shipped and had accepted but that are not yet extracted (the client dedups on
its own marker and will never resend them); `data/*/archive/` holds retained transcripts for any org
with `archive_days` set; and once a deployment hosts tenants each one has its own
`data/orgs/<org>/analyzer.db`. Copying `analyzer.db` alone restores the corpus, strands every
credential, and silently drops the queue and any retained transcripts.

**Automatic, daily, pruned.** `csa.py backup --keep N` (`server/csa.py`, `cmd_backup`) wraps
`analyzer/backup.py`'s snapshot and verify, then prunes to the newest N verified snapshots. The
`backup` compose profile runs it once a day. Bring it up alongside `proxy`:

```bash
docker compose -f docker-compose.trial.yml --profile proxy --profile backup up -d
```

Each run writes a consistent snapshot of every plane into `data/backup-<UTC date-time>/` (SQLite's
online backup API, which folds in the `-wal` sidecar; a plain `cp` of a WAL database silently drops
everything committed since the last checkpoint), verifies it (a sha256 per file plus `PRAGMA
integrity_check`, a checksum alone would happily certify a torn database), and prunes to the newest 7
verified snapshots, ordered by each manifest's `created` field, never directory mtime. A snapshot that
fails verify is deleted immediately and never counts toward the 7. The retained count is the `--keep`
argument on the `backup` service's `command` in `docker-compose.trial.yml`, not an environment
variable: edit that line and recreate the service to change it.

**Copy snapshots off the box.** `data/backup-*/` sits on the same disk as everything it protects, so
this profile is not a backup against disk failure, ransomware or `rm -rf data`: copy each verified
snapshot, or at least the newest, to separate storage on the same schedule.

**Manual snapshot**, outside the daily schedule:

```bash
docker compose -f docker-compose.trial.yml exec api /app/.venv/bin/python csa.py backup --keep 7
```

Expected: `snapshot: /app/data/backup-<timestamp> (N files, 0 queued)` then `verified:
/app/data/backup-<timestamp>`.

**Restore stops the writers first.** A snapshot is consistent per file, not a single point in time
across files: corpora are copied before the queue, so a session landing mid-snapshot can, rarely, be
missing from both.

```bash
docker compose -f docker-compose.trial.yml stop api mcp etl
docker compose -f docker-compose.trial.yml run --rm api \
  /app/.venv/bin/python3 -m analyzer.backup restore /app/data/backup-<UTC date-time> data
docker compose -f docker-compose.trial.yml up -d
```

**Verify the restore landed**, not only that the command exited 0:

```bash
docker compose -f docker-compose.trial.yml exec api /app/.venv/bin/python csa.py org overview
```

Expected: the `SESSIONS` and `FACTS` columns match the counts from before the restore (note them when
you take the snapshot, or read them off the pre-restore `csa.py org overview`).

There is no headless way to mint a token on an SSO-only box: tokens are minted only through the IdP.
Use a token that already exists instead, for example the operator's own laptop token from
`~/.csa/config.json` (the `auth_token` field; read it with `jq -r .auth_token ~/.csa/config.json` or by
opening the file):

```bash
curl -sS -H "Authorization: Bearer <token>" https://<host>/api/me
```

Expected: the same email on the restored box as it answered on the source box before the restore.
`control.db`, where every token is checked, came back from the same snapshot, so the identity behind
that token must match.

```bash
docker compose -f docker-compose.trial.yml exec api /app/.venv/bin/python csa.py drain --dry-run
```

Expected: the same total pending count `csa.py backup` had queued at snapshot time, proving
`data/pending/` came back, not only the corpus.

```bash
docker compose -f docker-compose.trial.yml exec api /app/.venv/bin/python3 \
  -c "from analyzer import archive; print(archive.get('<a session id you know was archived>'))"
```

Expected: a dict with the transcript's messages, not `None`, proving `data/*/archive/` (retained
transcripts) came back too. This check applies only when `archive_days` is set and the corpus has
sessions to archive; skip it otherwise. Checking `/api/facts` alone would miss all three of these.

The graph is rebuildable from the corpus, so it does not need a separate backup.

## 7. LLM provider (how the model is authenticated)

Every model call is **server-side** and goes through one seam, `complete()` in `analyzer/llm.py`:
extraction (`analyzer/extract.py`) and fact judging (`judge.py`) on `etl`, scope routing and relational
question planning (`mcp_server.py`, `ask.py`) on `mcp`, and the judge's boot sweep on `api`. There is
**no per-client model config**: clients never call a model. The trial compose passes the same
credential to all three services through its `x-llm-provider` block.

Two roles pick what each call runs on: **main** (reads sessions, answers questions) and **small**
(judges facts, routes questions). Admin > AI Models sets both (`analyzer/providers.py`):

| provider on AI Models | how it is called | needs |
|---|---|---|
| **Anthropic** | the Messages API over HTTPS, `<endpoint or https://api.anthropic.com>/v1/messages` | a key; an optional endpoint for an Anthropic-format gateway |
| **AWS Bedrock** | the Claude Code binary with `CLAUDE_CODE_USE_BEDROCK=1` | a `with-claude-code` image; a key (an access key pair `id:secret`, or a Bedrock API key) and a region |
| **Google Vertex** | the Claude Code binary with `CLAUDE_CODE_USE_VERTEX=1` | a `with-claude-code` image; the service account's JSON key, a region and a project |
| **OpenAI** | `POST <endpoint or https://api.openai.com/v1>/chat/completions` | a key; an optional endpoint for any OpenAI-compatible server |
| **Azure OpenAI** | `POST <endpoint>/openai/deployments/<model>/chat/completions` | a key and the endpoint; the model id is the deployment name |

A provider is saved only after a one-line test call answers. With **nothing registered**, both roles
run on the Claude credential in `.env`, as `claude-sonnet-5` (main) and `claude-haiku-4-5-20251001`
(small). Set at most one:

| `.env` | published image, no Claude Code | `with-claude-code` image |
|---|---|---|
| `ANTHROPIC_API_KEY=sk-ant-...`, plus `ANTHROPIC_BASE_URL=` for a gateway | the Messages API over HTTPS | the Claude Code binary |
| `CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)` | refused, one sentence naming the fix | the Claude Code binary |
| `CLAUDE_CODE_USE_BEDROCK=1`, plus AWS credentials you pass through to the containers | refused | the Claude Code binary |
| `CLAUDE_CODE_USE_VERTEX=1`, plus GCP credentials you pass through to the containers | refused | the Claude Code binary |

A key beside a subscription token uses the key, on either image. A key beside a Bedrock or Vertex switch
is refused on the published image rather than sent to Anthropic instead of the cloud you asked for.
A refused path parks the queue: sessions wait, none is lost, and it shows in the `LLM provider` log line,
on AI Models, and on `/api/health` as `"extraction": {"stalled": true, ...}`, until you change the
credential or the image.

**The `with-claude-code` image.** Claude Code is proprietary, so no public image carries it. You build
yours from the root of a checkout, push it to a private registry if you share it, and set
`CSA_SERVER_IMAGE` to it:

```bash
docker build -f server/Dockerfile --target with-claude-code -t csa-server:with-claude-code .
docker run --rm csa-server:with-claude-code sh -c 'command -v claude'    # /usr/local/bin/claude
```

It is the same image plus the binary: the build downloads Claude Code from npm, pinned by version and
checksum, and sets its telemetry opt-outs. Your use of it falls under [Anthropic's terms](https://code.claude.com/docs/en/legal-and-compliance).

A subscription token is for one person's own use, not for a server a team shares. [Anthropic's
terms](https://code.claude.com/docs/en/legal-and-compliance) say OAuth login "is designed to support ordinary use of Claude Code and other native
Anthropic applications", that plan limits "assume ordinary, individual usage", and that developers may
not "route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their users". A team
server uses an API key or a cloud provider.

The server **boots either way**. The api and mcp log one line at start, `LLM provider: <what main
runs on>`; with no provider and no credential it reads `LLM provider: NONE: register a provider on AI
Models (or set ANTHROPIC_API_KEY); ...` and every extraction and recall model call fails until one is
set.

## 8. Upgrade and roll back

A new version is a `server-v<version>` tag, released as `csa-onprem-<version>.tar.gz`
(`docs/public-release/bundle.md`) on a machine that can reach GitHub, or as
`ghcr.io/nabooai/csa-server-slim:<version>` on a box that may reach GHCR (see that page's "Registry
alternative"). Everything below assumes the offline bundle, since an on-prem box by definition has
no route to either. Each step names its expected output; stop and read it before the next one. A box on
a `with-claude-code` image rebuilds it at the new tag instead of loading the bundle's image.

1. **Back up before you touch anything.**
   ```bash
   docker compose -f docker-compose.trial.yml exec api /app/.venv/bin/python csa.py backup --keep <N>
   ```
   Expected: `snapshot: ...` then `verified: ...` (§6). Note the timestamp it printed;
   `data/backup-<that timestamp>` is what rollback restores if this upgrade goes wrong.

2. **Verify the bundle's signature, offline**, against the Sigstore trusted root you fetched once on
   a connected machine (`docs/public-release/bundle.md`, "Verify", steps 1 to 2):
   ```bash
   tar -xzf csa-onprem-<version>.tar.gz && cd csa-onprem-<version>
   sha256sum sigstore-trusted-root.json     # matches the trusted root you fetched on a connected machine
   sha256sum -c SHA256SUMS                  # every line: OK
   IDENTITY='^https://github\.com/nabooai/coding-session-analyzer/\.github/workflows/server-release\.yml@refs/tags/server-v'
   ISSUER=https://token.actions.githubusercontent.com
   for f in SHA256SUMS csa-server-slim-<version>-<arch>.tar; do
     cosign verify-blob --bundle "$f.sigstore.json" --trusted-root sigstore-trusted-root.json \
       --certificate-identity-regexp "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$f"
   done
   ```
   Expected: `Verified OK` for each file. `<arch>` is `amd64` or `arm64` (`uname -m`: `x86_64` is
   `amd64`, `aarch64` is `arm64`). cosign 3 has no `--offline` flag; this `--trusted-root` form is
   its offline-capable equivalent and makes no network call, because the trusted root was already
   fetched once, on a connected machine, before this box saw the bundle.

3. **Load the images.**
   ```bash
   docker load -i csa-server-slim-<version>-<arch>.tar    # Loaded image: csa-server-slim:<version>
   docker load -i caddy-<caddy version>-<arch>.tar    # Loaded image: caddy:<caddy version>
   ```
   A loaded tarball carries no registry digest, so `docker-compose.trial.yml`'s `image:
   caddy@sha256:...` pin will not match what you just loaded. The bundle's
   `docker-compose.bundle.yml` overrides the proxy image to the loaded tag; add it to `COMPOSE_FILE`
   in the next step.

4. **Chown, once, before the first non-root image this box has ever run.**
   ```bash
   sudo chown -R 10001:10001 data certs
   ```
   Skip this only if the box already ran a non-root CSA image before (every install after its first
   on-prem image). The symptom of skipping it is not an error at `up`: `/api/health` answers
   `{"ok": false, "control_plane": "unreadable"}` and every credential is refused with 401, because
   the api cannot read `data/control.db` (§1).

5. **Point the compose at the loaded images.**
   ```bash
   sed -i "s#^CSA_SERVER_IMAGE=.*#CSA_SERVER_IMAGE=csa-server-slim:<version>#" .env   # or edit it by hand
   export COMPOSE_FILE=docker-compose.trial.yml:docker-compose.bundle.yml COMPOSE_PROFILES=proxy
   docker compose config --services
   ```
   Expected: `migrate`, `api`, `etl`, `mcp`, `proxy`, and no error about a missing or unreachable
   image.

6. **Diff the new compose kit against the box's running copy before replacing files.**
   `docker-compose.trial.yml`, `Caddyfile` and `.env.example` ship inside the bundle as tracked
   files, and if you uncommented the corporate-CA block (§3b) you edited that same file on the box.
   Copying the bundle's copy over it without diffing silently reverts to the commented-out defaults.
   ```bash
   diff docker-compose.trial.yml <path to the box's current copy>/docker-compose.trial.yml
   ```
   Expected: the only differences are ones you intend (an upstream version bump, or your `# ca:`
   lines missing from the new file, in which case re-apply them before continuing).

7. **Bring it up.**
   ```bash
   docker compose up -d
   docker compose ps -a
   ```
   `migrate` runs first, same as first boot (§3). Expected: `migrate` `Exited (0)`, then `api` and
   `mcp` `healthy`, `etl` and `proxy` `running`. A failed migration leaves `api`, `mcp` and `etl`
   down (`docker compose logs migrate` names the file and the reason); a second `up` after that
   fails does nothing new until the underlying problem is fixed, and once `migrate` does succeed a
   second `up` is a no-op (every file already at head).

8. **Verify the version actually changed.**
   ```bash
   curl -sk https://<your host>/api/health
   ```
   Expected: `"version": "<version>"` (the tag you just loaded) and `"schema"` carrying all three
   heads (`control`, `corpus`, `telemetry`), none `null`. The box-local equivalent, for when there is
   no HTTP client handy:
   ```bash
   docker compose exec api /app/.venv/bin/python csa.py status
   ```
   Expected: `version: <version>`, three `schema.<env>: <revision>` lines matching the ones
   `/api/health` returned, `control_plane: ok`, exit code 0.

9. **Roll back if step 8 does not match, or anything else looks wrong.** Never `alembic downgrade`:
   every migration's `downgrade()` in `server/migrations/` raises `NotImplementedError` on purpose,
   so there is no code path that undoes a schema change. The only rollback is the previous bundle
   plus the snapshot from step 1:
   ```bash
   docker load -i csa-server-slim-<previous version>-<arch>.tar
   sed -i "s#^CSA_SERVER_IMAGE=.*#CSA_SERVER_IMAGE=csa-server-slim:<previous version>#" .env
   docker compose stop api mcp etl
   docker compose run --rm api /app/.venv/bin/python3 -m analyzer.backup restore \
     /app/data/backup-<step 1's timestamp> data
   docker compose up -d
   ```
   A bundle from before the slim image names its file and its image `csa-server`, not `csa-server-slim`: use
   that name in the first two lines. Repeat steps 7 to 8 to confirm the rollback took.

## Troubleshooting

- **`up` fails with `required variable CSA_PUBLIC_URL is missing`**: `.env` absent or the var empty.
  That is the fail-closed guard; fill `.env` (§2).
- **Server logs `refusing to start: ... CSA_AUTH=off`**: you overrode `CSA_AUTH=off` on a `0.0.0.0`
  bind. Use `org`, the compose default.
- **`refusing to start: CSA_AUTH='token' is not one of ['off', 'org']`** (or `'cloudflare'`): those
  are no longer modes. Set `CSA_AUTH=org` in `.env`; nothing else about your install changes.
- **`/api/health` returns `"control_plane": "unreadable"`** and everything 401/403s: the server
  cannot read `data/control.db`, where every user token lives. Usually file ownership on the bind
  mount after an image pull changed the container uid. Fix the permissions; the box refuses
  credentials rather than guessing, and `ok` is false so your uptime check sees it.
- **`CSA_AUTH_TOKEN` is refused on `/api/facts`**: an org exists, so this deployment hosts tenants
  and the shared credential is retired. Re-run `csa-client setup` on that laptop for a per-user
  token (§5).
- **Extraction errors on ingest**: the model credential is missing, expired or refused.
  `docker compose logs etl` names the provider's reason; `/api/health` shows
  `"extraction": {"stalled": true}` while sessions wait. Fix the key in `.env` or on Admin > AI Models
  (a `CLAUDE_CODE_OAUTH_TOKEN` login expires: re-run `claude setup-token`).
- **`LLM provider: NONE: ... needs Claude Code`**: the credential in `.env` or a provider on AI Models needs
  the `with-claude-code` image and this one has no binary. Use an API key or an HTTPS provider, or build
  that image (§7). Sessions wait in the queue meanwhile; none is quarantined.
- **`docker compose ps` shows `api` unhealthy forever, but `curl /api/health` from outside answers
  fine**: the image has no `curl` (`server/Dockerfile`). The shipped `docker-compose.trial.yml`
  healthcheck is a python `urllib` probe on `api` and a socket probe on `mcp`; if a hand-edited copy
  of the compose (an older kit, or a box's own copy carried across an upgrade without diffing, §8
  step 6) still runs a `curl`-based check, replace it with the python form. `docker inspect --format
  '{{json .State.Health}}' <container>` shows the probe's own output, including `exec: "curl": not
  found` when this is the cause.
- **`csa.py sso set` refuses with `CSA_PUBLIC_URL is not set here`**: it reads the api container's
  own environment, not the shell you typed the command in. Run it through `docker compose exec api
  ...` (§4b), never a bare `python csa.py sso set` on the host, which has no such variable.
- **Server refuses to boot with `refusing to start: sign-in is configured (... sso show) but no org
  claims a domain`**: `sso set` ran before any org had a `domain`, or a domain was cleared with
  `csa.py org domain <org>` and no argument on a box that was already live (that command does
  nothing visible until the next restart, the same boot-time-only shape as the retired
  `CSA_AUTH=token` invariant). Run `csa.py org domain <org> <domain>` (or `org adopt-default ...
  --domain ...`, §4a) before, or immediately after, `sso set`.
