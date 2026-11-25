# API and MCP

CSA's HTTP API is intended for the official plugin, client, web UI, and MCP connection. Keep the server
behind trusted HTTPS and use per-user credentials; do not expose the corpus anonymously.

## Public bootstrap routes

These routes are intentionally available before sign-in:

- `GET /api/health` — service and schema health.
- `GET /install` — readable client/plugin connection installer.
- `GET /api/cli-auth/probe` — checks whether CLI pairing is available.
- `GET /latest/checksums.txt` — checksums for published client binaries when configured.

## Authenticated routes

The official clients obtain a per-user bearer token through browser pairing. Send it as:

```http
Authorization: Bearer <token>
```

Common authenticated operations include:

- `POST /api/ask` — recall/search context for a connected agent.
- `GET /api/facts` — scoped facts and evidence for the web UI.
- `POST /api/ingest` — submit one locally redacted session.
- `GET /api/client-config` — current consent, retention, and backfill policy.
- `POST /api/client-config/ack` — acknowledge a server directive.
- `POST /api/token/revoke` — revoke the current client credential.

The exact request and response shapes are versioned by the client and covered by repository tests. Clients must
not allow a caller-supplied identity to override the identity bound to the token.

## MCP

The server exposes Streamable HTTP MCP at the configured public origin's `/mcp` path. The plugin registers it
only after consent and authorization. Use the token created for that user; never share a workspace token in a
team-wide config file.

A bare `GET /mcp` is not a protocol health check. Check the service socket or use an MCP client handshake.

## Compatibility

Treat new response fields as additive. Handle `401` by asking the user to reconnect, `429` by backing off, and
`5xx` by retaining the local queue. Do not retry a session indefinitely when the server explicitly rejects its
content or scope.
