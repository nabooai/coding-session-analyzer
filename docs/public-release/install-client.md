# Install the CSA client

A copy-paste runbook for a **user** putting `csa-client` on their laptop and joining a tenant. Full
flag reference lives in `client/README.md`; how a trial is evaluated is in [`05-trial-guide.md`](05-trial-guide.md).

> **The client uses no LLM and no API key.** It only ships your raw sessions (token-stripped,
> redacted) to the server; **all** model work — extraction and recall — happens **server-side** on the
> operator's account. You are never asked for a model key, at any step. The LLM setup lives entirely
> in [`install-server.md`](install-server.md).

> **Would rather not install a binary?** [The Claude Code plugin](../../csa-plugin/README.md) is readable
> Python that installs disabled, can send only the sessions you choose, and removes completely. It needs `python3`
> 3.9 or newer (on Linux, `apt install python3`; on a Mac, `xcode-select --install`) and covers Claude Code on macOS
> and Linux. Windows, Codex, bot hosts and history backfill use the binary below.

## The one-liner

```bash
curl -sSL https://csa.your-company.example/install | sh
```

That is the whole install. The server serves its own installer, so there is no GitHub account, no
brew token, and no shared secret to paste. It:

1. Downloads the binary for your OS/arch from `<server>/latest/`, and **verifies its sha256** against
   the server's `checksums.txt`.
2. Installs to `~/.local/lib/csa/csa-client` with a `~/.local/bin/csa-client` symlink. **No sudo**,
   and those are the only two paths it writes — it prints both.
3. Hands over to `csa-client setup`, which asks for your consent **before** anything else happens.

Then, in the browser tab it opens, type the **verification code** your terminal is showing and
authorize: **Authorize as** the account this browser is already signed in with, **Sign in with** your
identity provider when the server has one, or, on a server with neither, your email and your team's
invite code. Done.

```
==> Downloading https://csa.your-company.example/latest/csa-client-darwin-arm64
==> Verified sha256
==> Installed ~/.local/bin/csa-client -> ~/.local/lib/csa/csa-client
==> Finishing setup...

  Verification code: JEEP-IONOSPHERE
  Type this code on the authorize page — it proves the page is yours.

→ Opened https://csa.your-company.example/cli-auth?nonce=…&hostname=MacBook-Pro in your browser.
  Waiting for you to confirm...
✓ Authorized as you@company.com
✓ Fetched org policy (redaction floor: secrets)
✓ wrote ~/.csa/config.json (mode 0600)
✓ Claude Code hooks installed in ~/.claude/settings.json
✓ All set — csa-client is connected to https://csa.your-company.example.
```

**Prefer to read it first?** It is a plain POSIX script served over your own server:

```bash
curl -sSL https://csa.your-company.example/install | less
```

### If `~/.local/bin` is not on your PATH

The installer tells you, and prints the line to add. It deliberately does **not** edit your shell
config — it touches exactly the two paths it printed, and nothing else. Add it yourself:

```bash
export PATH="$HOME/.local/bin:$PATH"    # in ~/.zshrc or ~/.bashrc
```

### Piped into `sh` with no terminal (CI, a Dockerfile, `ssh host 'curl … | sh'`)

The consent prompt needs a terminal, so the installer stops after installing the binary and prints:

```
==> Almost done — one step left, in a terminal:
      csa-client setup --server https://csa.your-company.example
```

Nothing ships until you run that. With no terminal to type `yes` into, pass `--yes` (it still prints
the notice): `csa-client setup --server https://csa.your-company.example --yes`. On a headless box add
`--no-browser` and open the printed URL wherever you do have a browser.

## What you are agreeing to

`setup` prints the full notice and refuses to continue until you type `yes`. Read it — this is the
part that matters:

- CSA ships **full transcripts** of your Claude Code sessions to the server, **where your team can
  read them**.
- Secrets (API keys, tokens, JWTs, private keys, the value in `password=...` and similar) are
  stripped **on your machine, before sending**.
  Your prompts, code, file paths, emails, and names are **kept on purpose** — that is what makes
  recall work. Strip more with `csa-client redact set privacy|strict`.
- Your **existing** sessions stay put. Only sessions you start from now on ship. Add `--backfill` to
  include your history.
- **All** your project folders are indexed. Scope that immediately with `csa-client only pick`.

Declining writes nothing, authorizes nothing, and mints no credential — the browser click is the last
irreversible step, and it never happens if you say no.

## What gets installed

| path | what |
|---|---|
| `~/.local/lib/csa/csa-client` + `~/.local/bin/csa-client` | the binary and its symlink (the installer's only writes) |
| `~/.csa/config.json` (**0600**) | server URL, your identity, your per-user token, consent record |
| `~/.claude/settings.json` | **three** hook entries, added alongside anything already there |
| `~/.csa/shipped/` | one small marker per shipped session (dedup) |

The three hooks, and why:

| hook | runs | why |
|---|---|---|
| `UserPromptSubmit` | on each prompt | ambient recall — adds relevant team context |
| `SessionEnd` | when a session ends | ships that session |
| `SessionStart` | when a session starts | catch-up: ships sessions that **crashed** (SessionEnd never fires for those) |

There is **no background daemon** in this install.

## Verify

```bash
csa-client team     # the roster from the server — proves your token authenticates
```

Then use Claude Code briefly and end the session; `~/.csa/shipped/<session-id>` appears once it has
shipped. Recall happens **inside Claude Code**: setup registered the CSA MCP server, so ask its `ask`
tool (*"what has the team decided about X"*).

## Undo

```bash
csa-client only pick        # index only specific folders
csa-client ignore '*repo*'  # keep one project out (not retroactive)
csa-client forget <id>      # delete an already-shipped session from the server
csa-client uninstall        # remove ALL THREE hooks, the MCP registration, and the binary
csa-client uninstall --purge  # ...and wipe ~/.csa (config, consent record, markers)
```

## Manual install (self-hosted / air-gapped / an existing tenant)

The one-liner needs a server that publishes client binaries. If your operator runs a self-hosted CSA
with a **shared token** instead, use `init`. `$CSA_AUTH_TOKEN` is the same shared secret the server
runs:

```bash
csa-client init --yes \
  --server https://csa.your-company.example \
  --token "$CSA_AUTH_TOKEN" \
  --only '*your-repo*' \
  --backfill=false
csa-client doctor
```

`init` writes the same `~/.csa/config.json` (0600), sends `Authorization: Bearer <token>` on every
request, registers MCP with that header, and wires the same three hooks as `setup`. When it prints
`Nothing more to run`, the machine ships: each session goes to the server when it ends. `doctor`
confirms it with three wired hooks and no failures. Handy flags (full list in `client/README.md`):

| flag | meaning |
|---|---|
| `--server URL` | the CSA server; checked against `/api/health` before writing |
| `--token SECRET` | shared bearer secret. Defaults to `$CSA_AUTH_TOKEN` |
| `--only 'GLOB[,GLOB…]'` | allowlist of project folders to index |
| `--backfill=false` | skip uploading existing history (with `--yes`, backfill defaults on) |
| `--email` | override identity (default: your Claude account email) |
| `--cf-id` / `--cf-secret` | Cloudflare Access **service-token** headers (edge), independent of the bearer |
| `--install` | bot/CI hosts: ship through a login **watcher** instead of the hooks (`--ambient` adds the recall hook) |

`init` stores **`--token` > `$CSA_AUTH_TOKEN` > the previous config** as `auth_token`; at run time
`$CSA_AUTH_TOKEN` overrides the stored one.
Hooks and a watcher never run together, or every session would ship twice: wiring the hooks retires a
login watcher, and `csa-client install` refuses on a machine whose hooks are wired.

## Scripts: plain curl

For a pipeline with no plugin and no client. **Get a token:** a workspace admin mints one on **Service Tokens**.
A person can use the same browser pairing the clients use:

```bash
curl -sS -X POST "$SERVER/api/cli-auth/new" -H 'Content-Type: application/json' -d '{"hostname":"my-script"}'
# open the "url" it returns, type the "code", click Authorize. The page names csa-client; the token is the same kind.
curl -sS "$SERVER/api/cli-auth/<nonce>"       # repeat until it stops saying "pending"; "token" comes once, so save it
```

**Send a session.** `csa.py` and `redact.py` come from `csa-plugin/scripts/` at the tag. Copy both side by side; they
need nothing installed and honor `~/.csa/redact.deny`:

```bash
python3 csa.py redact < "$F" > "$F.red"
jq -n --rawfile c "$F.red" --arg id "$SID" '{session_id:$id,content:$c,redaction_mode:"secrets"}' \
  | curl -sS --fail-with-body -X POST "$SERVER/api/ingest" -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' --data-binary @-
```

`$SID` becomes a file name on the server: letters, digits, `.`, `_` and `-`. `--fail-with-body` makes a 401 or 413 a
non-zero exit; without it `curl` exits 0. The body limit is 32 MiB, and a larger one gets a 413. `{"ok":true}` means queued.
Check `.ok` too: `$F` must be a Claude Code transcript with at least one user message, or the reply is HTTP 200 with
`{"ok":false}`.

**Skipping the redact step sends unredacted text.** The server strips secrets before storing, but raw text crosses
every proxy and reaches server memory first, and your deny list and stricter mode do not apply. No consent is
recorded on this path: running it yourself is the decision.

## On-prem servers (a private CA, an egress proxy)

<a id="trust-the-servers-certificate"></a>

Your company's CSA runs inside its network (`install-server.md`). What changes on the laptop:

- **The one-liner is the only install path.** No Homebrew, no GitHub: the server publishes the
  binaries itself, and `curl -sSL https://<host>/install | sh` reaches it through the proxy on 443.
  There is no `init --token` on such a box: the workspace is adopted, the shared secret is refused,
  and the browser step of `setup` is a sign-in at your identity provider.
- **Trust the server's certificate in the OS store.** If the box uses the proxy's internal CA
  (`install-server.md` §3a), get the root from whoever runs the box; they export it with

  ```bash
  docker compose --profile proxy cp proxy:/data/caddy/pki/authorities/local/root.crt csa-root.crt
  ```

  Install it in the OS store (macOS: `sudo security add-trusted-cert -d -r trustRoot -k
  /Library/Keychains/System.keychain csa-root.crt`; Linux: copy it under
  `/usr/local/share/ca-certificates/` and run `sudo update-ca-certificates`). That covers `curl`,
  the installer, the Go client and its three hooks, and Claude Code's own MCP client too: its
  default certificate store is `bundled,system`, read by the native installer and by npm installs
  on Node 22.15 or later. An npm install on an older Node reads only its bundled set; there, put
  the same file in `NODE_EXTRA_CA_CERTS` in the environment Claude Code starts from, or the `ask`
  tool cannot connect while the hooks keep working. The same applies to a corporate CA that signed
  the server's certificate.

  **Do not rely on environment variables for the client.** Claude Code runs the hooks, and they do not
  inherit `SSL_CERT_FILE` or `NODE_EXTRA_CA_CERTS` from your shell, so a variable that fixes `curl` in
  your terminal leaves `UserPromptSubmit`, `SessionEnd` and `SessionStart` failing on the certificate
  and nothing ships. On macOS the Go client reads only the **system keychain** (the `security
  add-trusted-cert ... System.keychain` line above); a certificate in your login keychain or in
  `SSL_CERT_FILE` is not enough. On Linux the client reads `/etc/ssl/certs` after
  `update-ca-certificates`. A self-signed test certificate is trusted the same way: use `tls.crt`
  in place of `csa-root.crt`.
- **An egress proxy:** set `HTTPS_PROXY` (and `NO_PROXY` for the box's own name when it sits
  inside the network); `curl`, `csa-client` and Claude Code all honour it.
- **Checks.** `csa-client doctor` reads `~/.csa/config.json` and the hooks and probes
  `GET /api/health` and `GET /api/team`; it does not exercise the MCP path. `claude mcp list` shows
  whether the `csa` server connects, and the first `ask` in a Claude Code session is the real test.
- **Updates** come from the server: `csa-client update` fetches `/latest/`, which carries the
  client built with the server's image, so it changes when the operator upgrades the server.

## Troubleshooting

- **`csa-client: command not found` right after installing** — `~/.local/bin` is not on your PATH.
  See above, or call it by full path: `~/.local/bin/csa-client`.
- **The browser tab never opened** — the URL is always printed too; open it manually, or re-run with
  `--no-browser`. On a remote box, open the URL on your laptop; only the *code* has to match.
- **"This authorization request has expired"** — the code is good for 15 minutes. Re-run
  `csa-client setup --server …` for a fresh one. Nothing was written.
- **"That verification code does not match"** — you are looking at a page someone else started. If
  your terminal is not showing a code, **close the page**: someone sent you that link.
- **`401 ... "code":"unauthorized"` on every request** — your token was revoked, or you are pointed at
  a different server. Re-run `csa-client setup --server …`. (The server accepts `Authorization:
  Bearer` **or** `X-CSA-Token` — some proxies strip `Authorization`.)
- **Exit 78 (`EX_CONFIG`), "no consent on record"**: the client refuses to run without current
  consent. Re-run `csa-client setup` (or `init` on a shared-token server).
- **The installer says the server is unreachable** — behind a corporate proxy, set `HTTPS_PROXY`;
  both `curl` and `csa-client` honor it.
