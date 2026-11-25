# Troubleshooting

## Docker or Compose check fails

Start Docker Desktop or the Docker daemon. CSA requires Docker Compose v2.20 or later.

## No administrator sign-in link

```sh
cd ~/csa-server
docker compose logs migrate
```

Validate `config/config.json`, then rerun the installer. It preserves valid existing files.

## Server does not become healthy

```sh
cd ~/csa-server
docker compose ps -a
docker compose logs migrate api etl
```

For a local install, ports 8766 and 58768 must be free. For a remote install, verify HTTPS certificates,
proxy routing, and firewall policy.

## Sessions arrive but search has no new results

Inspect ETL logs. `LLM provider: NONE` means setup is incomplete. Authentication, model-ID, endpoint, network,
or provider usage-mode errors must be corrected before the durable queue drains.

## Plugin is not sending

Run `/csa:status`. Common causes are missing browser authorization, an ignored project, manual mode, an expired
credential, an old consent policy, missing Python, or untrusted TLS. Reconnect rather than editing hooks by hand.

## Certificate is not trusted

Do not disable TLS verification. Install the organization CA for the process that starts Claude Code, or set
`SSL_CERT_FILE` to an approved PEM bundle. Public network servers should use a publicly trusted certificate.

## Get help safely

Include versions, status codes, and redacted logs. Never attach `.env`, tokens, private keys, session corpora,
private audit findings, or screenshots containing real client/session data.
