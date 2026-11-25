# server/e2e

End-to-end harnesses. Each one boots the real server (and, where it says so, the real image and
the real Go client), drives it over HTTP, and prints one PASS or FAIL per check. Exit 0 means green.

```bash
server/e2e/run.sh              # org, onboarding, landing, trial, plugin (no docker)
server/e2e/run.sh onprem       # one harness by name; env and arguments pass through
server/e2e/run.sh onprem with-claude-code   # onprem and onprem-egress take the image target: slim (default) or this
```

Run from anywhere: each harness finds the repo from its own path. CI runs the five hermetic ones and
`onprem`, on both image targets, on every pull request (`.github/workflows/ci.yml`).

## The harnesses

| Name | Proves | Needs | Time |
|---|---|---|---|
| `org` | One deployment, two orgs: an org B credential gets nothing of org A from any GET, POST or MCP route, by replayed id, spoofed header or planner ATTACH. Ingest identity comes from the token; per-org files; path traversal refused; org delete; cross-workspace admin is `csa.py org` only. | uv venv | ~1 min |
| `onboarding` | `curl /install \| sh` writes only what it prints; `csa-client setup` consent, browser approve, the three hooks; ship and sweep with a planted secret redacted; per-user token authn and revoke; dashboard sign-in; uninstall removes every hook. | uv venv, go, a built `server/ui/dist` | ~1 min |
| `landing` | Signed-out landing, the workspace request (duplicates, 413, 400, 429, XSS as data), `csa.py org approve`, the invite signing a browser in, the invite stored nowhere in plain text. | uv venv, a built `server/ui/dist` | ~30 s |
| `trial` | Boot refusals (`off` off loopback, removed modes), the auth gate on every route, 413 and 400, the MCP gate, three users through `init` and the watcher to recall, forget ownership, two deployments reject each other's secret. | uv venv, go | ~1 min |
| `plugin` | The Claude Code plugin with a mocked `claude`, through a proxy that records every request: no Python before connect, the browser device flow and its consent record, ship and sweep with every `secrets` shape planted, preview sha equal to the wire's, retention raised and lowered, a voided policy, manual mode's zero requests, both clients in either order, `only` globs, the scripted recipe, disconnect revoking the token. | uv venv, go, jq, `python3` | ~3 min |
| `onprem` | The compose behind its TLS proxy on a network with no route out: migrate before boot (stale and corrupt `data/`), SSO through a stub issuer, `curl https://csa/install \| sh`, setup approved from the browser session, three sessions drained through a stub model (over HTTPS on slim, through the CLI on `with-claude-code`, the same model ids either way), recall over HTTP and MCP, `csa-client update`, and a DNS and packet ledger showing nothing else was asked for. | docker with buildx and compose v2, go, openssl, uv venv | ~10 min |
| `onprem-egress` | The hardened image reaches only the operator's model endpoint: the bare CLI (`with-claude-code`) or the HTTPS path (slim), one isolated extraction, the research suite, a subscription token refused without leaving the box (slim); DNS and SYN ledgers. | docker with buildx; `CSA_E2E_UPSTREAM` + a key for proxy mode, which spends money | ~10 min |

"uv venv" means `uv sync` has run in `server/`. A built dist means `npm ci && npm run build` in
`server/ui`. The hermetic five mock the `claude` CLI and need no Anthropic account.

## Running two at once

Each harness works in its own temp directory. `onprem` and `onprem-egress` name their compose project,
containers, volume, image tag and network after their PID and take the first free `/24`, so two runs on
one docker daemon do not collide. The hermetic five bind fixed loopback ports (8791 to 8793, 8811 to
8822, 8840 to 8849, 8860 to 8861), so run them one at a time per machine, as `run.sh` does.

`CSA_E2E_KEEP=1` keeps the workspace of `org`, `onprem` and `onprem-egress` for a post-mortem.
`CSA_SERVER_IMAGE=<local tag>` makes `onprem` reuse an image instead of building one.
