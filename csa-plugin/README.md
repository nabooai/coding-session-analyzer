# CSA for Claude Code, with no binary

This plugin connects Claude Code to your team's CSA server using plain Python you can read before it
runs. It installs disabled. It does nothing until you enable it, and sends nothing until you run
`/csa:connect` and click Authorize in your browser.

| You want | Do this |
|---|---|
| Team memory on every prompt, and finished sessions shipped | `/csa:connect <server-url>` |
| Only the sessions you choose, and nothing else leaving this computer | `/csa:connect <server-url> --manual` |
| Windows, Codex, a bot or CI host, or history backfill | the [`csa-client` binary](../client/README.md) |

**Needs:** Claude Code, `python3` 3.9 or newer, and `git` to add the marketplace. macOS and Linux. On a Mac
without the Command Line Tools, `python3` and `git` are stubs that ask to install them: run
`xcode-select --install` first.

## Read it

Everything that runs is in this folder.

| File | What it is |
|---|---|
| [`hooks/hooks.json`](hooks/hooks.json) | Three shell lines. With no `~/.csa/config.json`, Python never starts. |
| [`scripts/csa.py`](scripts/csa.py) | The hooks. `_request()` is the only network call, and its host comes only from your config. |
| [`scripts/csa_commands.py`](scripts/csa_commands.py) | What the skills run. A hook never loads it. |
| [`scripts/redact.py`](scripts/redact.py) | The redactor. It is byte for byte the server's: `cmp csa-plugin/scripts/redact.py server/analyzer/redact.py`, and CI enforces it. |
| [`skills/`](skills/) | Nine commands. The model cannot start any of them: each has `disable-model-invocation: true`. |

There is no `git`, `pip`, `curl` or download anywhere in the scripts, and nothing updates itself.

## Install, check, enable

The first tag, `plugin-v0.2.0`, is cut when the acceptance run on clean machines passes. Until that tag exists
and the repository is public, add the marketplace from a checkout: `claude plugin marketplace add /path/to/checkout`.
The release is source-only. A maintainer runs `scripts/validate-plugin-release.sh`, merges the reviewed commit to
`master`, then dispatches **Plugin release** from that exact commit with `publish` enabled. The workflow refuses a
non-master commit, an existing tag, a version mismatch, a symlink or a compiled artifact. It creates
`plugin-v<manifest version>` only after the Python 3.9 tests, redaction parity, manifest validation and reproducible
source archive checks pass.

```bash
claude plugin marketplace add nabooai/coding-session-analyzer#plugin-v0.2.0 --sparse .claude-plugin csa-plugin
claude plugin install csa@csa                 # installs disabled
claude plugin list --json                     # installPath is where the code landed
diff -r "<installPath>" "<checkout at the tag>/csa-plugin"    # must print nothing
claude plugin enable csa@csa
```

`csa.py` runs with `-B`, so no bytecode lands beside it and that `diff` stays empty after use.

## Connect

`/csa:connect <server-url>` prints what it will send, opens your server's authorize page, and prints a
two word code. Type the code on the page and click **Authorize**. That click is your consent: the page
states what ships and how long this workspace keeps it, and a model cannot complete it without your
sign-in. Close the tab instead and no credential exists, no config is written and nothing ships; the waiting
child removes its files when the code expires 15 minutes later. Your next prompt confirms the connection.
`/csa:status` shows where it stands and names the one thing to fix.

Only sessions you start from now on ship. Older transcripts stay here, and so does a session that was already
open when you connected, unless you send it yourself with `/csa:send-session`.

A server too old to record this consent is refused before any browser opens. Ask whoever runs it to
upgrade, or use `csa-client` on that server.

## What leaves, and what you can check

| | Automatic | Manual |
|---|---|---|
| Each prompt | redacted, then `POST /api/ask` for recall | nothing |
| A session ends | redacted transcript, `POST /api/ingest` | nothing |
| A session starts | `GET /api/client-config`, then `POST /api/client-config/ack` if an admin asked for history, always declined; crashed sessions catch up, 10 at a time | nothing |
| `/csa:send-session` | that session, after `GET /api/client-config` for the retention | the same |

Secrets are stripped on this machine first. Prompts, code, file paths, names and emails are kept on
purpose, unless you raise the mode. Redaction is regex: it catches keys and tokens, and misses a coworker's name
or a secret written as prose. See [docs/redaction.md](../docs/redaction.md); `~/.csa/redact.deny` and
`~/.csa/redact.allow` apply on every path.

Who sees it: teammates see the facts and quotes extracted from your sessions, under your name. You and
workspace admins can read a transcript the workspace retains. The server's own model reads it to extract
facts. All requests go to the one server in your config, with a `csa-plugin/<version>` user agent. In automatic
mode the plugin also registers that server's MCP endpoint with Claude Code, which keeps the entry and your token
in `~/.claude.json` and may keep copies in its own backups. A disconnect revokes the token, so copies are dead.
An endpoint on another host is not registered.

| To | Run |
|---|---|
| See the exact text before anything is sent | `/csa:preview`, or `python3 csa.py preview --session <id> --out <file>`. No network, and it works before connecting. It prints a sha256, label counts and a file for you to read. |
| See what was sent | `/csa:sent`, or read `~/.csa/sent.jsonl`: time, session, server, mode, bytes, labels, sha256. Same sha256 as the preview of a finished session; a live session keeps growing, so its next send differs. |
| Keep a project out | `/csa:scope ignore '*repo*'`, or `/csa:scope only '*repo*'`. Neither its sessions nor its prompts leave. |
| Take a session back | `/csa:forget <id>`, while still connected. It deletes the session and the facts drawn from it, and no hook sends it again. Only `/csa:send-session <id>` can. |

Manual mode: `/csa:send-session` sends the session as it stands, and later turns stay here unless you send
again. Running it again sends again if the transcript grew, which it does each time you invoke a command in that
session. It works on a past session too: `/csa:send-session list` shows the recent ids, and `/csa:send-session <id>`
sends one. In bypass-permissions mode nothing stops a
model running the script itself, so do not use that mode where this promise matters.

## Turn it off, and remove it

**Off:** `claude plugin disable csa@csa`. The hooks and skills stop, and a ship already in flight finishes.
The MCP entry and its credential stay until you disconnect.

**Remove, in this order.** The skills need the plugin enabled: if you disabled it, enable it first or use the by-hand
block below.

```bash
/csa:disconnect                     # inside Claude Code: revokes the credential, removes the MCP entry, deletes ~/.csa
claude plugin uninstall csa@csa
claude plugin marketplace remove csa
rm -rf ~/.claude/plugins/cache/csa   # or under $CLAUDE_CONFIG_DIR; /csa:disconnect prints the right path
```

Run `/csa:forget` first for anything you want deleted from the server: after a disconnect this machine can no
longer delete, and a workspace admin can. `/csa:disconnect` prints which sessions remain.

**If you uninstalled first,** the skills are gone. Do it by hand:

```bash
SERVER=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.csa/config.json")))["server"])')
TOKEN=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.csa/config.json")))["auth_token"])')
curl -sS -X POST "$SERVER/api/token/revoke" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
claude mcp remove csa -s user
rm -rf ~/.csa
```

If the workspace has only one admin credential and it is this one, the revoke is refused so nobody is locked out
of the workspace, and `/csa:disconnect` removes nothing until you make someone else an admin. If the server cannot be
reached it removes everything and says the credential stays live until it idles out.

## Update

Nothing changes until you act: third-party marketplaces do not auto-update, and the manifest pins the version.
Do not turn auto-update on for this one without reading what it would run. New versions are at
[releases](https://github.com/nabooai/coding-session-analyzer/releases), and `/csa:status` prints yours.

```bash
claude plugin marketplace remove csa
claude plugin marketplace add nabooai/coding-session-analyzer#plugin-v<new> --sparse .claude-plugin csa-plugin
claude plugin install csa@csa       # a new directory: diff it against the tag again, then enable it if it is off
```

Your config, consent and rules stay in `~/.csa`. Run `/reload-plugins` in a session that was open.

## Moving from `csa-client`

The two never ship together. While `csa-client` is wired the plugin stands down and warns at most hourly, and
while the plugin is enabled `csa-client setup`, `init` and `install` refuse. To switch:

```bash
csa-client uninstall               # not --purge: that deletes your only, ignore and deny lists
claude plugin enable csa@csa
/csa:connect <server-url>          # new consent; your redaction mode and lists are kept
```

`uninstall` revokes the old credential, and a later `csa-client setup` revokes the plugin's. A new `/csa:connect` to
another server, or in another mode, revokes the one it replaces and starts from that moment. Sessions `csa-client` already shipped are not sent again. Anything it had
not shipped stays on this machine: the plugin starts from the moment of the new consent. To go back, disable the
plugin and run `csa-client setup`; a config it writes parks the plugin until the next `/csa:connect`.

## When something is wrong

| You see | Why | Fix |
|---|---|---|
| "this plugin is paused because csa-client is also installed" | both clients on one machine | the section above, or `claude plugin disable csa@csa` |
| "the plugin is enabled but not connected" | no consent of its own yet | `/csa:connect` |
| "credential is no longer accepted" | idle expiry or a revoke | `/csa:connect`. Sessions wait here. |
| "CSA stopped shipping ... older policy" | what ships changed since you agreed | `/csa:connect`, or `/csa:disconnect` |
| "CSA paused: this server keeps transcripts" | the workspace raised retention past what you agreed to. No session ships; recall still works | `/csa:connect` shows the new terms. It resumes on its own if the server lowers it. |
| "the server's TLS certificate is not trusted" | Python does not read the macOS Keychain | set `SSL_CERT_FILE` to a PEM holding your CA in the environment Claude Code starts from |
| "No browser could be opened" | SSH or a headless box | run the printed command in a terminal |

The detail of every ship is in `~/.csa/ship.log`.

## Scripts

To ship from a script, with no plugin and no client, use [the recipe](../docs/public-release/install-client.md#scripts-plain-curl).
