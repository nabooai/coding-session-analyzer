# Getting started

## Requirements

- macOS or Ubuntu/Linux
- Docker Engine 24+ and Docker Compose v2.20+
- Claude Code and Python 3.9+ for the plugin
- An Anthropic or OpenAI API key when you are ready to extract sessions

## Install the server

```sh
curl -fsSL https://raw.githubusercontent.com/nabooai/coding-session-analyzer/master/install.sh | sh
```

The installer checks prerequisites, asks for the workspace, administrator, public URL and model path,
creates private configuration under `~/csa-server`, waits for health, and prints sign-in and connection
instructions. It is safe to rerun and will not replace existing credentials, configuration, or data.

To inspect the installer before running it:

```sh
curl -fsSLo csa-install.sh https://raw.githubusercontent.com/nabooai/coding-session-analyzer/master/install.sh
less csa-install.sh
sh csa-install.sh
```

Use `http://localhost:8766` for a private local server. A network server must use trusted HTTPS.
The deterministic, agent-friendly runbook is [`INSTALL.md`](https://github.com/nabooai/coding-session-analyzer/blob/master/INSTALL.md).

## Connect Claude Code

Run the marketplace commands printed by the installer. The plugin installs disabled so you can inspect it
before enabling it. Then run:

```text
/csa:connect http://localhost:8766
```

Verify the two-word code in the browser and click **Authorize**. Select manual mode if you do not want new
completed sessions to ship automatically.

## Verify

1. Run `/csa:status`.
2. Start a new session and record a distinctive synthetic decision such as `LOCAL_VERIFY_731 uses SQLite`.
3. End the session and wait for extraction.
4. Search for `LOCAL_VERIFY_731` in CSA and open its evidence.
5. Ask for the identifier in a new Claude Code session to verify MCP recall.

Old sessions are not uploaded by default. Extraction waits, without dropping queued sessions, until a model
is configured successfully.
