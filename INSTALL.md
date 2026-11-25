# Install CSA

This runbook installs one CSA server and connects the readable Claude Code plugin.
The guided installer is safe to rerun: it preserves configuration, credentials, and data.

## Prerequisites

- macOS or Ubuntu/Linux
- Docker Engine 24+ with a running daemon
- Docker Compose v2.20+
- Claude Code and Python 3.9+ for the plugin
- An Anthropic or OpenAI API key when you are ready to extract sessions

Check Docker before continuing:

```sh
docker version
docker compose version
```

Stop if either command fails. Do not install system software without the user's approval.

## Server + Claude Code plugin

### 1. Inspect and run the installer

Primary path:

```sh
curl -fsSL https://raw.githubusercontent.com/nabooai/coding-session-analyzer/master/install.sh | sh
```

To review it first:

```sh
curl -fsSLo csa-install.sh https://raw.githubusercontent.com/nabooai/coding-session-analyzer/master/install.sh
less csa-install.sh
sh csa-install.sh
```

The installer asks for workspace name, administrator email, public URL, and model setup. Use
`http://localhost:8766` for a private local installation. It stores files under `~/csa-server` with
private configuration permissions, starts CSA, waits for health, and prints the administrator sign-in
link. Re-running the command does not replace existing configuration or credentials.

**Credential stop point:** ask before entering a model credential. Team servers should use provider API
credentials. Anthropic and OpenAI keys are entered privately and tested during setup; OpenAI also asks
for main/small model IDs with safe defaults. OpenAI subscription login is unavailable. Claude subscription
login is not in the public image and is only appropriate for a person's own local server under the
provider's terms.

### 2. Sign in and finish model setup

Open the printed `/join/...` URL. If you deferred model setup or need a custom provider, open **Settings →
AI Models**, add the provider, endpoint and model IDs, then use its test action. Without a working model,
CSA keeps accepted sessions queued but cannot extract searchable facts yet.

### 3. Connect Claude Code

Use the exact commands printed by the installer:

```sh
claude plugin marketplace add nabooai/coding-session-analyzer#plugin-v0.2.0 --sparse .claude-plugin csa-plugin
claude plugin install csa@csa
claude plugin enable csa@csa
```

Then run `/csa:connect http://localhost:8766` in Claude Code (use the server's HTTPS origin remotely).
Open the browser pairing page, verify the two-word code, and click **Authorize**.

**Consent stop point:** explain that automatic mode sends locally redacted prompts for recall and locally
redacted completed sessions to this CSA server. Ask before enabling it. Do not upload history, change
project scope, or manually send a session without separate approval. Only newly completed sessions ship
automatically; old sessions are not backfilled by default.

## Verify

1. Run `/csa:status` and confirm the server and selected project are connected.
2. Start a new Claude Code session and record a distinctive synthetic decision, such as
   `LOCAL_VERIFY_731 uses SQLite`.
3. End the session and wait for extraction.
4. Open the server, search for `LOCAL_VERIFY_731`, and open its evidence.
5. Ask for the identifier in a new connected session to verify MCP recall.

Exact identifiers, paths, and errors are retrieved through lexical FTS5/BM25 search. Extraction requires
a working model.

## Five common failures

1. **Docker check fails:** start Docker Desktop or the Docker daemon and require Compose v2.20+.
2. **No sign-in link:** run `cd ~/csa-server && docker compose logs migrate`; validate
   `config/config.json` and rerun the installer.
3. **Server is unhealthy or a port is busy:** run `cd ~/csa-server && docker compose ps -a` and
   `docker compose logs migrate api etl`. Local ports 8766 and 58768 must be free.
4. **No result after a session:** inspect `docker compose logs etl`. `LLM provider: NONE` means model
   setup is incomplete; an authentication error means the key, endpoint, model ID, network, or provider
   usage mode must be corrected.
5. **Plugin cannot connect:** confirm the URL, run `/csa:status`, and reconnect. Remote servers require
   trusted HTTPS; do not bypass an untrusted certificate warning.

## Stop or recover

```sh
cd ~/csa-server
docker compose down             # keeps data
docker compose ps -a
docker compose logs migrate api etl
```

Do not add `--volumes` during recovery. Advanced TLS, SSO, backup, and upgrade operations are in the
[on-prem runbook](docs/public-release/install-server.md).
