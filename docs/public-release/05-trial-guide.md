# Trial guide

How to evaluate a self-hosted CSA: stand it up, check that the gate is closed, and run the
end-to-end harnesses that prove the auth and tenant claims. Every command here runs against
`server/docker-compose.trial.yml` and `server/e2e/` as they are in this repository.

## 1. Stand it up

Follow [the quickstart](../quickstart.md): one box with Docker, `CSA_PUBLIC_URL` in `.env`, the
workspace and its first admin in `config/config.json`, the admin sign-in link from
`docker compose logs migrate`, a model on **Admin > AI Models** (or `ANTHROPIC_API_KEY` in `.env`),
then `curl -sSL <CSA_PUBLIC_URL>/install | sh` on each laptop and **Team** > **Invite** for each
teammate.

| Shape | Page |
|---|---|
| a solo try, or a small team behind your own proxy or tunnel | [quickstart.md](../quickstart.md) |
| on-prem behind the bundled TLS proxy and your identity provider | [install-server.md](install-server.md) |
| a small trusted team on one shared invite instead of per-person sign-in | [install-server.md §5a](install-server.md#5a-the-open-egress-trial-shared-invite) |
| Kubernetes | [install-kubernetes.md](install-kubernetes.md) |

The model runs on the server only. Laptops hold no model key; they ship redacted sessions and ask
over HTTPS with a per-user token that `csa-client setup` minted.

## 2. Check that the gate is closed

`/api/health` is the one open route. It answers liveness and where the MCP server is, and no corpus
content:

```bash
curl -fsS http://localhost:8766/api/health
# {"ok": true, "control_plane": "ok", "version": "...", "schema": {...}, "extraction": {...}, "mcp_port": 58768, "mcp_path": "/mcp", ...}
```

Every other route needs a token. On a laptop that ran `csa-client setup`, the token is
`auth_token` in `~/.csa/config.json`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8766/api/facts          # 401
TOKEN=$(jq -r .auth_token ~/.csa/config.json)
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" http://localhost:8766/api/facts                  # 200
```

The server also accepts `X-CSA-Token: <token>`, for a proxy that strips `Authorization`. Both the api
and `mcp_server.py --http` read the same `CSA_AUTH` and accept the same headers.

| `CSA_AUTH` | Meaning |
|---|---|
| `org` | the compose default: every request carries a per-user token, or a browser session cookie on the dashboard |
| `off` | the code default, loopback development only: the server refuses to start on any other address |

Any other value refuses to start. On a box that has a named workspace (the quickstart's
`config/config.json` names one), the shared secret `CSA_AUTH_TOKEN` is refused on every route.

## 3. Run the harnesses

`server/e2e/` holds the proof. Each harness boots the real server, drives it over HTTP and prints
one `PASS` or `FAIL` per check; exit 0 is green. What each proves and needs is in
[`server/e2e/README.md`](../../server/e2e/README.md).

```bash
# from the repository root, after `uv sync` in server/
server/e2e/run.sh            # org, onboarding, landing, trial: no Docker, a mocked `claude` CLI
server/e2e/run.sh trial      # one harness by name
server/e2e/run.sh onprem     # the compose behind its TLS proxy on a network with no route out (Docker, ~10 min)
```

`trial` asserts:

- **Boot refusals.** `CSA_AUTH=off` on a non-loopback bind (api and `mcp --http`) exits non-zero and
  binds nothing; the removed modes `token` and `cloudflare` and an unknown mode refuse too; the
  loopback default still boots.
- **The auth gate.** `/api/health` is open; every corpus route answers 401 without a token and 200
  with `Authorization: Bearer` or `X-CSA-Token`; an unauthenticated POST changes nothing; a body over
  32 MiB is a 413 and a malformed one a 400.
- **The MCP gate.** `/mcp` returns the same 401 without the bearer.
- **Three users.** Each on its own `HOME` runs `csa-client init`, ships a session through the watcher,
  and recalls its own fact.
- **`forget` ownership.** A user deletes their own session; forging another user's is refused.
- **Two deployments.** A second server with a different secret rejects the first one's.

A green run ends:

```
  60 passed, 0 failed, 0 skipped
  TRIAL E2E: GREEN
```

`org` proves workspace isolation on one deployment (an org B credential gets nothing of org A), and
`onprem` measures egress: see [egress.md](egress.md).

## 4. When something is off

| Symptom | Cause |
|---|---|
| `401 {"ok": false, ..., "code": "unauthorized"}` on every corpus route | the token is missing, revoked or expired. Re-run `csa-client setup`. The wording of `error` varies; the status, `ok` and `code` do not, and the 401 carries `WWW-Authenticate: Bearer` |
| `413 {"code": "payload_too_large"}` | the POST body is over 32 MiB, checked on `Content-Length` before the body is read. The limit is fixed |
| `refusing to start: bind=0.0.0.0 exposes the corpus but CSA_AUTH=off` | set `CSA_AUTH=org`, the compose default. The mcp service binds `0.0.0.0` unless `CSA_MCP_BIND=127.0.0.1`, so it refuses `off` too |
| `refusing to start: CSA_AUTH='<x>' is not one of ['off', 'org']` | `token` and `cloudflare` are no longer modes: set `CSA_AUTH=org` |
| `up` stops with `CSA_PUBLIC_URL is missing` | `.env` is not beside the compose file |
| the client exits 78, `no consent on record` | run `csa-client setup` (or `csa-client init` on a shared-secret server) |
| `ship.log` says `identity changed` | the machine's Claude account is not the one that consented: re-run `csa-client setup` |
| `/api/health` answers, nothing else does | the URL, DNS, VPN or tunnel is fine; the problem is the credential |
| no `sent` line in `~/.csa/ship.log` | a session ships when it ends. `csa-client doctor` checks the three hooks; `only` and `ignore` may be excluding the project folder |

Backup and restore: [install-server.md §6](install-server.md#6-back-up-and-restore).
