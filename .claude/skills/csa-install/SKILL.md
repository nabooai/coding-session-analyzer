---
name: csa-install
description: Install CSA end-to-end from a clean checkout — self-host the server (docker-compose.trial.yml, auth token, health/gate verify) and/or set up the csa-client on a laptop (brew/binary + noninteractive init + verify). Use when the user says "install CSA", "set up the CSA server", "install the client", "onboard onto CSA", "join the CSA tenant", or "/csa-install". Runs the documented commands and verifies each step; stops and reports on any failure.
---

# Install CSA (server and/or client)

Automates the manual runbooks in [`docs/public-release/install-server.md`](../../../docs/public-release/install-server.md)
and [`docs/public-release/install-client.md`](../../../docs/public-release/install-client.md). Model:
**one server = one tenant**; the shared `CSA_AUTH_TOKEN` is the tenant boundary.

## First: what to install

Ask the user (unless obvious from the request): **server**, **client**, or **both**?
- Standing up CSA for a company → server (then hand the URL + token to users for the client).
- Joining an existing CSA server on a laptop → client (you need the server URL + token first).

Run only the section(s) needed. **Verify every step; on any failure, stop and report — never continue
past a broken step or fabricate success.** Use the ponytail rule: the documented command, nothing more.

---

## A. Server

**Prereqs — check first, stop if missing:**
1. `docker compose version` works.
2. **One LLM provider** for the server (extraction + recall). Ask which the user wants: an **Anthropic
   API key** (`ANTHROPIC_API_KEY`), or a provider on Admin > AI Models after sign-in (OpenAI, Azure OpenAI,
   Anthropic). Bedrock, Vertex and a Claude login (`CLAUDE_CODE_OAUTH_TOKEN`, one person's own use) need an
   image built with `--target with-claude-code` (see install-server.md §7). Do not invent a key/token: have
   the user provide it.

**Steps:**
1. From the repo root: `cd server`.
2. Generate a strong auth token and write `server/.env` (gitignored). Never echo secrets into a
   committed file; `.env` is fine. Put the auth token AND the chosen provider in it, e.g. for an API key:
   ```bash
   cat > .env <<EOF
   CSA_PUBLIC_URL=http://127.0.0.1:8766          # the URL people will type; loopback for a local trial
   CSA_MCP_URL=http://127.0.0.1:58768/mcp        # no proxy in front, so laptops register the mcp port directly
   CSA_AUTH_TOKEN=$(openssl rand -hex 32)
   ANTHROPIC_API_KEY=<the user's key>
   EOF
   chmod 600 .env
   ```
   Only `CSA_PUBLIC_URL` is compose-required (the on-prem runbook needs no shared secret at all); a
   missing provider still boots but logs `LLM provider: NONE` and recall/extraction fail, so make sure
   exactly one provider is set. api and mcp publish on 127.0.0.1 only.
   If `openssl` is absent, use `head -c32 /dev/urandom | xxd -p -c32`.
3. Bring it up: `docker compose -f docker-compose.trial.yml up -d`
4. **Verify (all three must hold):**
   ```bash
   TOK=$(grep '^CSA_AUTH_TOKEN=' .env | cut -d= -f2)
   curl -fsS http://127.0.0.1:8766/api/health                                            # 200
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/facts               # 401
   curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOK" \
        http://127.0.0.1:8766/api/facts                                                   # 200
   ```
   If health is unreachable: `docker compose -f docker-compose.trial.yml logs api` and diagnose.
5. **Report to the user:** the server URL to hand out, and that they must distribute `CSA_AUTH_TOKEN`
   to users **out of band** (it is the tenant credential). Mention exposing it (Cloudflare Tunnel /
   reverse proxy) is their next step — point to install-server.md §5.

---

## B. Client

**Prereqs — get from the user first, stop if missing:** the **server URL** and the **shared token**
(`CSA_AUTH_TOKEN`). Skip the token only for a loopback-dev or Cloudflare-fronted server.

**Steps:**
1. Install the binary:
   - Homebrew: `brew tap csa/csa https://github.com/nabooai/coding-session-analyzer && brew install csa-client`.
   - If brew is unavailable or fails, fall back to the direct-download/build path in `client/README.md` §1.
   - Confirm: `csa-client --version` (or `csa-client help`) runs.
2. Configure noninteractively (fill server/token; ask for an `--only` allowlist glob, default to a
   sensible repo match or omit to index all):
   ```bash
   csa-client init --yes --server <URL> --token <TOKEN> --only '<glob>' --backfill=false
   ```
   Use `--backfill=false` unless the user wants their existing history uploaded.
   `init` also wires the three Claude Code hooks (recall, ship on SessionEnd, catch-up on
   SessionStart), the same as `setup`. Expect `Nothing more to run` at the end; there is no watcher
   to start.
3. **Verify:** run `csa-client doctor`. Expect the three hooks wired, the credential accepted, and no
   failures; `NOT SHIPPING YET` means the hooks are missing, so re-run init. (Recall itself runs inside
   Claude Code via the CSA MCP `ask` tool that `init` registered — there is no `csa-client ask` CLI.)
4. Tell the user each session ships when it ends (`~/.csa/ship.log` shows `sent <id> ok=true`), and
   recall inside Claude Code works because init added the MCP `--header` line.

**Common failures:** 401 → token mismatch, re-run init; exit 78 → consent, re-run `csa-client init`.

---

## Prove the whole thing (optional)

For a full fail-closed E2E (3 users, auth gate, forget, cross-tenant), run the harness from the repo
root: `server/e2e/run.sh trial` (needs `uv`/`server/.venv` and, for the client sections,
`go`). Expect `60 passed, 0 failed`.
