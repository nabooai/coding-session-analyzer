# csa-client

Ships your Claude Code sessions to your team's CSA server, and brings the team's past
sessions back into your prompts.

```
 your machine                                         CSA server
 ~/.claude/projects/*.jsonl
   | session ends  -> redact on your machine -> POST /api/ingest   -> facts, team memory
   | you type      -> redact the prompt      -> POST /api/ask      -> context added to Claude
```

Nothing ships until you accept a consent notice. Secrets are stripped before anything leaves
your machine.

For Claude Code on macOS or Linux there is also [a plugin](../csa-plugin/README.md): readable Python instead of a
binary, disabled until you enable it. The two never ship together; `setup`, `init` and `install` refuse while the
plugin is enabled. `uninstall --purge` deletes your `only`, `ignore` and deny lists along with the rest of `~/.csa`.

## Install

Your CSA server hands out the client. Get the server URL from your admin, then:

```bash
curl -sSL https://your-csa-server/install | sh
```

The installer downloads the build for your OS, checks its sha256 against the server's
`checksums.txt`, writes `~/.local/lib/csa/csa-client` plus a `~/.local/bin/csa-client` symlink,
and runs `csa-client setup`. It touches nothing else.

Other ways in:

| how | command |
|---|---|
| Download by hand | the server's **Connect a machine** page lists every build with its SHA-256 |
| Homebrew (macOS, Linux) | `brew tap csa/csa https://github.com/nabooai/coding-session-analyzer && brew install csa-client` |
| Windows | download `csa-client-windows-amd64.exe` from `<server>/latest/`, then run `csa-client.exe setup --server <url>` |
| From source (Go 1.22+) | `cd client && go build -o csa-client .` |

The binary is not signed by Apple: a browser download needs
`xattr -d com.apple.quarantine csa-client` before it runs.

## Set up

```bash
csa-client setup --server https://your-csa-server
```

1. **Consent.** The notice says what ships, how it is redacted, who can read it and how long the
   server keeps it. Type `yes`, or nothing is written.
2. **Sign in.** Your browser opens the server's authorize page. Type the code the terminal shows.
3. **Wire.** setup writes the config, the hooks and the recall tool, then lists every path it wrote.

| flag | effect |
|---|---|
| `--backfill` | also ship the sessions already on this machine, once. Default: only sessions from now on. |
| `--no-browser` | print the authorize URL instead of opening a browser (SSH, headless). |
| `--yes` | accept the notice without a prompt. It is still printed. |

Re-run `setup` any time: it keeps your redaction level and server, and never narrows what you
already agreed to ship.

### What setup writes

| path | what |
|---|---|
| `~/.csa/config.json` | server, identity, per-user token, redaction level, consent record (mode 0600) |
| `~/.csa/bin/csa-client` | the binary the hooks run |
| `~/.claude/settings.json` | three hooks: `UserPromptSubmit` (recall), `SessionEnd` (ship), `SessionStart` (catch up) |
| `~/.claude.json` | the `csa` MCP server, so Claude can call `ask`, `fact` and `flag` |
| `~/.csa/shipped/` | one marker per shipped session, so nothing ships twice |

Shipping is asynchronous: a hook hands the upload to a background process and returns in
milliseconds. A session reaches the server shortly after it ends.

## What gets sent

| when | what | where |
|---|---|---|
| a session ends | the full transcript (prompts, replies, code, paths), redacted, with your email | `POST /api/ingest` |
| a session starts | transcripts whose `SessionEnd` never fired (crashes) | `POST /api/ingest` |
| you send a prompt | a redacted query from that prompt, your working directory, your email | `POST /api/ask` |
| a session starts | a check for admin requests and the workspace's retention | `GET /api/client-config` |

The ambient recall skips short or contentless prompts, and skips the prompt entirely if a secret
survives redaction. Teammates see the facts and quotes drawn from what ships, and you and workspace admins can
read a transcript the workspace keeps. Treat redacted output as "safe for your team", not "safe to publish".

**History stays home unless you say so.** Without `--backfill`, sessions older than your setup
never ship, even if an admin asks the machine for them.

**Retention is part of consent.** If the workspace later keeps transcripts longer than the notice
you accepted said, shipping pauses until you re-run `setup` and accept the new terms.

## Redaction

Redaction runs on your machine before anything is sent. The server redacts again, and a
workspace can set a minimum level, so it only ever strips more.

| level | strips | keeps |
|---|---|---|
| `secrets` (default) | API keys, GitHub/Slack/AWS/GCP/Stripe/npm/... tokens, JWTs, private keys, the value in `password=...`, `api_key: ...` and similar | emails, names, hostnames, code, paths, `$ENV` references |
| `privacy` | the above, plus emails, `user:pass@` URLs, `Bearer` tokens, `token=` and `pwd=` assignments, internal hostnames (`*.internal`, `*.corp`, `*.local`) | names, code, paths |
| `strict` | the above, plus every term in `~/.csa/redact.deny` | everything else |

```bash
csa-client redact                          # current level, deny/allow counts
csa-client redact check --mode privacy     # preview a level on a built-in sample
csa-client redact check --file t.jsonl     # ...or on your own text
csa-client redact set strict               # change the level, no re-consent needed
```

`~/.csa/redact.deny` holds terms to strip (case-insensitive substring), `~/.csa/redact.allow`
terms to keep when a pattern over-matches (exact match). One term per line, `#` comments,
reloaded on every send.

## Choose what ships

```bash
csa-client only pick                 # choose project folders from a numbered list
csa-client only '*work-repo*'        # ship ONLY folders matching this glob
csa-client ignore '*secret-repo*'    # never ship folders matching this glob
csa-client only                      # show the allowlist and the folder names globs match
```

Globs match the folder names under `~/.claude/projects`, where `/` becomes `-`:
`~/Git/secret-repo` is `-Users-you-Git-secret-repo`, so `'*secret-repo*'` is the pattern.
Subagent transcripts follow their project's rule.
`ignore` wins over `only`. An empty `only` list ships every folder. Both are read on every ship,
before the transcript is opened, and neither is retroactive.

### Delete what already shipped

```bash
csa-client forget <session-id> [<id>...]             # the id is the .jsonl file name
csa-client forget --project '*secret-repo*'          # every local session in a folder; asks first
```

`forget` deletes the session and every fact mined from it. It is permanent. Flags go before ids.

## Commands

| command | does |
|---|---|
| `setup` | connect this machine (above) |
| `only`, `ignore`, `redact`, `forget` | choose what ships (above) |
| `doctor` | read-only health report: config, hooks, allowlist, credential, last ships. Send it to your admin. |
| `team` | who ships to this server, with session counts. Works offline from a cache. |
| `demo` | open Claude Code on a question the team memory can answer |
| `version` / `update` | print the version; replace the binary with the build your server publishes |
| `uninstall [--purge]` | remove the hooks, the MCP server, the watcher and the installed binary. `--purge` also deletes `~/.csa`. Shipped sessions stay; use `forget`. |
| `hook off` | remove every CSA hook. This stops shipping too, not only recall. |

`hook`, `ship` and `ship --sweep` are what Claude Code runs. You do not type them.

## Troubleshooting

Run `csa-client doctor` first. It checks everything below in one pass and names the fix.

| symptom | fix |
|---|---|
| Claude shows "this machine's credential is no longer accepted" | `csa-client setup`. Tokens expire after inactivity. Nothing is lost: sessions ship after you sign in. |
| `the server's TLS certificate is not trusted` | trust the CA in the OS certificate store (macOS keychain, Linux `update-ca-certificates`). `SSL_CERT_FILE` does not reach Claude Code hooks. |
| sessions never arrive | `doctor` lists the hooks. All three must be wired; `setup` wires them. Check `~/.csa/ship.log`. |
| some projects ship, others do not | an `only` pattern that matches nothing, or an `ignore` that matches too much. `csa-client only` shows both. |
| `ship: paused` in `~/.csa/ship.log` | the workspace now keeps transcripts longer than you agreed to. `csa-client setup` shows the new terms. |
| `identity changed` | Claude Code is logged in as a different email than the one that consented. `csa-client setup`. |
| exit code 78 | no current consent on record. `csa-client setup`. |
| `csa-client: command not found` | add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile. |

Logs: `~/.csa/ship.log` (hooks), `~/.csa/client.log` (watcher).

## Self-hosted server with a shared token

A self-hosted server that has no workspaces yet authenticates with one shared secret instead of
per-user sign-in. Machines connect with `init`, which wires the same three hooks as `setup`. When
it finishes, the machine ships; there is nothing else to run:

```bash
csa-client init --server https://your-csa-server --token "$SECRET"                         # interactive wizard
csa-client init --yes --server https://your-csa-server --token "$SECRET" --email you@team.com   # scripted
csa-client doctor                                                                            # confirm: three hooks, no failures
```

| init flag | effect |
|---|---|
| `--token SECRET` | the shared secret, sent as `Authorization: Bearer` (default `$CSA_AUTH_TOKEN`) |
| `--email E` | identity to attribute sessions to (default: your Claude Code login) |
| `--only GLOBS` | comma-separated allowlist, written to `~/.csa/only` |
| `--redact-mode M` | `secrets`, `privacy` or `strict` |
| `--backfill=false` | skip the one-time upload of existing sessions (default on for init) |
| `--install` | ship through a login watcher instead of the hooks (bot/CI hosts). `--ambient` adds the recall hook to it |
| `--cf-id`, `--cf-secret` | Cloudflare Access service token, for a server behind Access |
| `--delegated` | bot/CI host: attribute each session to the email in a `<session>.owner` file |

`install` starts the watcher at login (macOS launchd; on Linux it prints systemd steps). It
refuses on a machine whose hooks are wired, which would ship every session twice
(`csa-client hook off` removes them first). Running `setup` or `init` without `--install` on a
watcher machine retires the watcher.

On a `--delegated` host, a `<session>.owner` file beside the transcript, or a `<uuid>.owner` naming
a parent directory, attributes that session to the email on its first line. To delete such a
session: `csa-client forget --owner <that email> <id>`.

## Environment overrides

For development and CI. Normal use needs none of them.

| variable | default | effect |
|---|---|---|
| `CSA_SERVER` | config `server` | server URL |
| `CSA_AUTH_TOKEN` | config `auth_token` | bearer token |
| `CSA_ROOT` | `~/.claude/projects` | folder scanned for `.jsonl` transcripts |
| `CSA_QUIET` | `10s` | watcher: how long a transcript must be still before it ships |
| `CSA_INTERVAL` | `2.5s` | watcher: poll interval |
| `CSA_COOLDOWN` | `10m` | watcher: minimum gap between ships of one session |
| `CSA_RETRY_MAX` | `5m` | watcher: longest pause while the server is down |
| `CSA_HOOK_TIMEOUT` | `1.8s` | recall hook: how long to wait for `/api/ask` |

## Contributing

House rules, the ingest contract and the release process: [CLAUDE.md](CLAUDE.md).
Gates: `gofmt -l .` prints nothing, `go vet ./...` and `go test ./...` pass.
