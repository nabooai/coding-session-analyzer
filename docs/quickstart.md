# CSA quickstart

Ten minutes from nothing to your teammate's decision answered in your Claude Code session, plus
about six minutes if you build the image from source. One box runs the server with Docker; every laptop runs
one line. The server needs a model key, not Claude Code.

```
 1 server ──▶ 2 sign in ──▶ 3 connect ──▶ 4 invite ──▶ 5 ask
   4 min        1 min        2 min        1 min        2 min
```

For an on-prem install behind your own TLS and identity provider, use
[public-release/install-server.md](public-release/install-server.md). For Kubernetes,
[public-release/install-kubernetes.md](public-release/install-kubernetes.md).

## What you need

| | |
|---|---|
| Docker | Engine 24+ with Compose v2.20+ (`docker compose version`). No `compose` subcommand? The standalone `docker-compose` v2 binary runs every command below the same way |
| The image | `ghcr.io/nabooai/csa-server-slim`: public, the compose pulls it. Or `docker load` the offline bundle, or [build it from source](#build-the-image-from-source) |
| The kit | `server/docker-compose.trial.yml` (from the repo or the bundle) |
| A model | an Anthropic API key. Or skip it now and add a provider on **AI Models** after step 2. [Which need Claude Code](#which-models-need-claude-code) |
| Laptops | macOS or Linux with Claude Code |

**Pick the URL first.** Every installer and sign-in link is built from it, so changing it later
means re-running setup on every laptop.

| Who reaches it | `CSA_PUBLIC_URL` | `CSA_MCP_URL` |
|---|---|---|
| only this machine (a solo try) | `http://localhost:8766` | `http://localhost:58768/mcp` |
| your team | `https://<host>`: your reverse proxy or tunnel, sending `/mcp` to port 58768 and the rest to 8766 | leave it out |

Plain `http` works only on `localhost`: the sign-in cookie is `Secure`, so a browser drops it on any
other `http` address.

### Which models need Claude Code

The image does not include Claude Code. An API key or a provider on **AI Models** does not need it.

| Model | Image |
|---|---|
| `ANTHROPIC_API_KEY`, with `ANTHROPIC_BASE_URL` for an Anthropic-format gateway | this one |
| **AI Models**: Anthropic, OpenAI or an OpenAI-compatible endpoint, Azure OpenAI | this one |
| AWS Bedrock or Google Vertex, on **AI Models** or in `.env` | built by you with `--target with-claude-code` |
| A subscription token, `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` | built by you with `--target with-claude-code` |

A subscription token is for one person's own use, not for a server a team shares. [Anthropic's
terms](https://code.claude.com/docs/en/legal-and-compliance) say OAuth login "is designed to support ordinary use of Claude Code and other native
Anthropic applications", that plan limits "assume ordinary, individual usage", and that developers may
not "route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their users". A team
server uses an API key or a cloud provider.

### Build the image from source

From the root of a clone of this repository:

```bash
docker build -f server/Dockerfile -t csa-server:local .
```

It takes about six minutes on a laptop. Then add `CSA_SERVER_IMAGE=csa-server:local` to `.env` in
step 1. The build compiles the laptop client from the same checkout, so `/install` hands out a client
that matches the server. For Bedrock, Vertex or a subscription token add `--target with-claude-code`:
it downloads Claude Code from npm into your image, pinned by version and checksum, under Anthropic's
terms, so keep that image in a private registry.

## 1. Start the server

```bash
mkdir csa && cd csa
cp <kit>/docker-compose.trial.yml .
mkdir -p config data
cat > .env <<'EOF'
CSA_PUBLIC_URL=http://localhost:8766
CSA_MCP_URL=http://localhost:58768/mcp
# optional: a model key, or add a provider on AI Models after step 2
# ANTHROPIC_API_KEY=sk-ant-...
EOF
cat > config/config.json <<'EOF'
{"workspace": {"name": "Acme", "adminEmail": "you@acme.com"}}
EOF
export COMPOSE_FILE=docker-compose.trial.yml
docker compose up -d
docker compose logs migrate | grep "sign-in link"
```

Expected, within a minute:

```
applied: admin sign-in link for you@acme.com, valid 7 days: http://localhost:8766/join/jn_...
```

Without a key or an AI Models provider, the api logs `LLM provider: NONE` and reads no session, so
Memory and `ask` stay empty.

`config/config.json` names the workspace and its first admin. Add `"domain": "acme.com"` to admit
only that domain's addresses. Nothing else needs setting: redaction, retention and the rest have
defaults and live in the app.

## 2. Sign in

Open the link and click **Continue as you@acme.com**. You land on **Three steps to your first
recall**, which checks each step off as it happens:

1. **AI model.** Checked already if `.env` has a key. If not: **AI Models**, add a provider, then pick
   its models as the main model and the small model.
2. **Connect this machine.** The command is on the card.
3. **Invite a teammate.**

## 3. Connect your machine

Paste the line from the card into a terminal on the machine you code on:

```bash
curl -sSL http://localhost:8766/install | sh
```

It shows what it would collect and waits for your yes. In a script or any shell without a terminal,
run `csa-client setup --server http://localhost:8766 --yes` instead: `--yes` accepts the same notice. Then it prints a two word code and opens your
browser: type the code, click **Authorize**. The terminal ends on `All set` and your server's URL.
It installed three Claude Code hooks and registered the `csa` MCP server. Reload the welcome page:
step 2 is checked.

## 4. Invite a teammate

**Team** > **Invite**, type their email, **Create link**, **Copy**. What you copied carries the
link, when it expires, and the same install line. Your teammate opens the link, clicks
**Continue**, and runs the line.

The install line carries `CSA_PUBLIC_URL`. With the solo-try `http://localhost:8766`, only this
machine can install: a teammate's laptop needs the team URL, set as in
[install-server.md §2](public-release/install-server.md#2-write-env).

## 5. Ask

1. One of you finishes a Claude Code session that decided something.
2. About a minute later it is on **Memory**, credited to them.
3. The other opens a new Claude Code session and asks: *what did we decide about the payment retries?*
   Claude calls `csa` and answers with the decision, who made it and when.

## If something is off

| Symptom | Fix |
|---|---|
| No `sign-in link` line | `config/config.json` is missing or not valid JSON (`docker compose logs migrate` says which), or you already signed in. A fresh link: `docker compose exec api /app/.venv/bin/python csa.py org admin-link` (once your machine is connected it prints the `org role` command to run instead) |
| No `sign-in link` line, and `migrate` warns `/etc/csa is mounted but holds no config.json` (or `org admin-link` says `workspace.adminEmail is not set`) | Docker mounted an empty `config/`: its VM cannot see the folder. Usual with Colima or Docker Desktop when the project lives outside your home directory. Move it under `~`, or add its path to the VM's shared folders, then `docker compose up -d` |
| `error while creating mount source path` | run the `mkdir -p config data` line, then `up -d` again |
| `up` stops with `CSA_PUBLIC_URL is missing` | `.env` is not beside the compose file |
| Step 1 on the card stays unchecked | no model: `docker compose logs api \| grep "LLM provider"` says `NONE`. Add a key on AI Models |
| `LLM provider: NONE: ... needs Claude Code`, and `/api/health` says `"stalled": true` | the credential in `.env` or the provider on AI Models needs the `with-claude-code` image. Use an API key, or [build it](#build-the-image-from-source). Sessions wait in the queue meanwhile |
| A session ended but Memory is empty | `docker compose logs etl`. `drained 1 session(s)` means it landed; a provider error means a bad key |
| Sign-in does not stick: you land back on Sign in | the URL is plain `http` on an address other than `localhost`. Put https in front |
| `csa-client: command not found` | the installer printed the `export PATH=...` line for your shell. Or run `~/.local/bin/csa-client` |

Stop it with `docker compose down`. Everything lives in `./data`; `docker compose up -d` brings it back.
