# CSA on-prem quickstart

From a Kubernetes cluster to a laptop shipping sessions, with the built-in sign-in (a typed email and a
team invite code). No identity provider is needed. Follow it top to bottom. The detail lives in
[install-kubernetes.md](install-kubernetes.md) and [install-client.md](install-client.md); this page
only orders it.

```
 1 helm install ──▶ 2 invite + first admin ──▶ 3 sign in ──▶ 4 laptop: curl | sh ──▶ 5 verify
```

Read the [security note](#security-note-read-before-exposing-it) before you give the URL to anyone.

## What you bring

| | |
|---|---|
| Kubernetes | 1.25+ and Helm 3 |
| Storage | a StorageClass with ReadWriteOnce (the chart asks for 20Gi) |
| Front door | Emissary/Ambassador (default), an nginx Ingress, or your own route (`frontDoor: none`) |
| TLS | a certificate for your host in a secret named `csa-tls`, or cert-manager with nginx |
| Model | an Anthropic API key, or an Anthropic-format gateway (base URL + key). AWS Bedrock or Google Vertex need an image with Claude Code, which you build: [install-kubernetes.md](install-kubernetes.md#build-the-image-yourself) |
| DNS | a hostname, e.g. `csa.example.com`, pointing at the front door |

## What we send you

| | |
|---|---|
| Chart | `csa-1.1.3.tgz` (its `image.tag` defaults to the release it came from) |
| Registry token | a GitHub token with `read:packages` for `ghcr.io/nabooai/csa-server` |
| Version | release tag `server-v1.1.3`, image tag `1.1.3` |

## 1. Install

```yaml
# my-values.yaml
host: csa.example.com
workspace: {name: "Your Company", domain: example.com, adminEmail: admin@example.com}
# okta: leave it out. Empty okta.* means the built-in sign-in.
# nginx instead of Ambassador: frontDoor: nginx   (cert-manager: tls.clusterIssuer: <issuer name>)
```

```bash
kubectl create namespace csa
kubectl -n csa create secret docker-registry ghcr --docker-server=ghcr.io \
    --docker-username=<github user> --docker-password=<read:packages token>
kubectl -n csa create secret generic csa-llm --from-literal=ANTHROPIC_API_KEY=sk-ant-...   # optional, or a provider on AI Models
# gateway instead: add --from-literal=ANTHROPIC_BASE_URL=https://<gateway>
helm install csa ./csa-1.1.3.tgz -n csa -f my-values.yaml --wait --timeout 5m
```

Name the release `csa`: every `deploy/csa` command in these docs assumes it. With another release name,
use `deploy/<release>`.

Keep `--wait`: without it `helm install` prints `deployed` as soon as Kubernetes accepts the objects,
**even when `migrate` then fails**. If it times out, read the pod:

```bash
kubectl -n csa get pods                       # Running 3/3 is healthy; Init:Error or Init:CrashLoopBackOff is not
kubectl -n csa logs deploy/csa -c migrate     # the reason, in one sentence
kubectl -n csa describe pod <pod>             # Pending or ImagePullBackOff: its Events name the cause
```

`workspace.name` creates the workspace on first install; on an upgrade it renames the workspace. With the built-in sign-in, `workspace.domain` limits
the typed sign-in to that domain: `mallory@other.example` is refused with "This CSA admits only
example.com addresses." Set `workspace.adminEmail` (an address on that domain) to name the first admin.

### No real certificate or DNS yet (a test cluster)

The `csa-tls` secret must exist before the front door serves your host. For a test, make a self-signed one:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout tls.key -out tls.crt \
    -subj "/CN=csa.example.com" -addext "subjectAltName=DNS:csa.example.com"
kubectl -n csa create secret tls csa-tls --cert=tls.crt --key=tls.key
```

Without DNS, point the name at the front door from the machine you test from: add
`<front door IP> csa.example.com` to `/etc/hosts`, or for one request
`curl --resolve csa.example.com:443:<front door IP> --cacert tls.crt https://csa.example.com/api/health`.
A laptop must also trust `tls.crt`: see [install-client.md](install-client.md#on-prem-servers-a-private-ca-an-egress-proxy).

## 2. First admin and invite code

The first admin needs no command. When `workspace.adminEmail` is set, the `migrate` init container logs a
one-person sign-in link for that address on each deploy until the person has signed in:

```bash
kubectl -n csa logs deploy/csa -c migrate | grep "admin sign-in link"
# applied: admin sign-in link for admin@example.com, valid 7 days: https://<host>/join/jn_...
```

Open it in a browser. It signs that address in as an **admin**, and the Admin group (Analytics, AI
Models, Service Tokens, Entity types) appears in the sidebar; people and roles are on Team. The link works only for that address, makes an admin only the first time it is
opened (opened again it signs in a member), expires in 7 days, and a later deploy replaces an unused one. Once the admin has a live session, deploys print nothing more. If the session
idles out or the link is lost, use `csa.py org admin-link` below.

If the link is used up or lost and the admin cannot get back in, print a fresh one. This runs on the pod
only and is never served over HTTP; it replaces the admin's old browser sessions, and the link works once:

```bash
kubectl -n csa exec deploy/csa -c api -- /app/.venv/bin/python csa.py org admin-link
# admin sign-in link for admin@example.com, valid 7 days: https://<host>/join/jn_...
```

Everyone else joins from Team, which makes a link per person, or with the shared invite code.
A fresh install has **no invite code** until you make one (shown once, store it):

```bash
kubectl -n csa exec deploy/csa -c api -- /app/.venv/bin/python csa.py org invite default
# invite: inv_...
```

Typing the invite code with the admin's address gives a **member**, never an admin: only the link does.
Rotating the code later (same command) kills the old one and keeps everyone already signed in.

## 3. Sign in (browser)

The admin opens the link from section 2. Everyone else opens their own link from Team, or
opens `https://<host>/` and enters their email and the invite code. They land as members and can read the
team's memory. Expected: the page loads; a wrong code shows "Invalid invite credential."; an address
outside `workspace.domain` is refused.

## 4. Connect a laptop

macOS or Linux. Nothing here needs GitHub.

```bash
curl -sSL https://<host>/install | sh
```

The installer downloads the binary from your server, checks its sha256, and hands over to
`csa-client setup`, which asks for consent and opens a browser page. On that page, use your signed-in
session, or type the same email and the invite code. It then installs the hooks and the `csa` MCP
server. To run it again: `csa-client setup --server https://<host>` (the server is also remembered
from the first run).

Use `setup`, not `init`: this server hosts a workspace, which refuses the shared secret `init --token`
sends. `setup` wires all three hooks (`UserPromptSubmit`, `SessionEnd`, `SessionStart`); a machine
with fewer ships nothing, and `csa-client doctor` then ends with `NOT SHIPPING YET`.

Service tokens for agents with no browser are minted from **Service Tokens** in the Admin tab
([install-kubernetes.md](install-kubernetes.md) §6), which needs the Admin tab: sign in as the admin.

## 5. Verify it works

| Check | Command | Expected |
|---|---|---|
| Server is up | `curl -s https://<host>/api/health` | JSON starting `{"ok": true, "control_plane": "ok"` |
| Binaries are served | `curl -so /dev/null -w '%{http_code}\n' https://<host>/latest/csa-client-linux-amd64` | `200` |
| Sign-in config | `curl -s https://<host>/api/auth-config` | `"sso": null, "sso_required": false` |
| Admin sign-in | the link from `kubectl -n csa logs deploy/csa -c migrate` | the dashboard opens with an Admin tab |
| Member sign-in | email + invite code on `https://<host>/` | the dashboard opens, no Admin tab |
| Laptop paired | `csa-client setup` | `Authorized as <your email>` and the MCP server registered |
| Three hooks | `csa-client doctor` | no failed checks; `~/.claude/settings.json` lists `UserPromptSubmit`, `SessionEnd` and `SessionStart` |
| Extraction works | `kubectl -n csa logs deploy/csa -c api \| grep "LLM provider"`, then the ETL log after the first session | not `LLM provider: NONE`; no repeated auth errors from the ETL |
| A session lands | finish a Claude Code session on the laptop, wait a minute or two, then ask Claude about it, or `kubectl -n csa logs deploy/csa -c etl` | the ETL logs `drained 1 session(s)`; the fact is recalled |

Extraction **stalls** when the model key is wrong (an invalid key, or a `csa-llm` secret whose key is not
named exactly `ANTHROPIC_API_KEY`): the pod stays healthy, sessions arrive and queue, and nothing is
recalled. Read `kubectl -n csa logs deploy/csa -c etl` for the provider error, fix the secret
(delete and create `csa-llm` again), then `kubectl -n csa rollout restart deploy/csa`.

## Security note: read before exposing it

**The built-in sign-in is self-asserted.** The server cannot check who you are. Anyone who has the
invite code and an address on `workspace.domain` can sign in as that address. What this does and does
not give them (checked on a scratch install):

- They get a **member** session: they can read the workspace's memory. Typing the admin's address with
  the invite code is still a member. Only the one-person link for `workspace.adminEmail` makes an admin,
  so protect who can read the `migrate` log: until the admin opens the link, whoever has it is the admin.
If you change `workspace.adminEmail`, the previous admin keeps the role until you revoke their token
(`csa.py org revoke`).
- An address on another domain is refused, but that is a typo guard, not authentication: anyone inside
  your domain who has the code can claim a colleague's address.
- Their sessions and flags are **attributed to the address they typed**, so attribution is only as
  honest as your team.
- The invite code is the only gate. Treat it like a shared password and rotate it
  (`csa.py org invite default`) when someone leaves or it leaks.

So keep the URL **internal only** (VPN or private network) or turn on SSO before it is reachable from
the internet. What the chart gives you for this, and nothing more:

| Value | Effect |
|---|---|
| `frontDoor: none` | the chart creates no route. The pod is only reachable inside the cluster (a ClusterIP Service `csa`, ports 8766 and 58768). You attach your own private route. |
| `frontDoor: nginx` | one Ingress of class `nginx.className` (default `nginx`). Point `host` at a DNS name that resolves only on your network and set `nginx.className` to an internal class. |
| `frontDoor: ambassador` (default) | an Emissary Host and two Mappings on whatever listener your Emissary has. It is as private as that listener. |

The chart has no IP allowlist. Restrict by where the front door and DNS point, and with `networkPolicy.enabled` (see [install-kubernetes.md](install-kubernetes.md#hardening)) inside the cluster.
Remember the laptops must reach the same URL (VPN on).

## Optional later: SSO

When you can create an OIDC client (Okta works today; see [sso-okta.md](sso-okta.md)), set
`okta.issuer`, `okta.clientId` and optionally `okta.adminGroup`, then `helm upgrade`. From then on the
typed sign-in and the invite code are **refused**, `workspace.domain` limits who signs in to accounts the provider vouches for, and members
of `okta.adminGroup` get the Admin tab. Tokens made under the typed sign-in keep working until you
revoke them (`csa.py org revoke --minted-by web`).

## Known limits

| | |
|---|---|
| One pod | `replicas: 1`, strategy `Recreate`. No multi-replica, no rolling update |
| SQLite | the database is files on one RWO volume. No external database |
| Upgrade | about 30 s of 503 while the pod is replaced |
| Model providers | the `csa-llm` secret: an Anthropic API key or an Anthropic-format gateway. Bedrock, Vertex, OpenAI and Azure OpenAI are registered on Admin > AI Models instead; this page does not test them |
| Laptops | macOS and Linux |
| Built-in sign-in | self-asserted: the domain is checked but not proven, and one admin (`workspace.adminEmail`) |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Invalid invite credential" | no invite exists yet (run `csa.py org invite default`), or it was rotated |
| No admin sign-in link in the `migrate` log | `workspace.adminEmail` is empty, or that person already has a live session. `csa.py org admin-link` prints a new one (section 2) |
| Pod `Init:CrashLoopBackOff` mentioning `workspace.adminEmail` | it must be an address on `workspace.domain`, and `workspace.name` must be set |
| `/mcp` times out, Claude Code cannot connect | Emissary's default timeout is 3 s. The chart's Mappings raise it; if you route it yourself, set `timeout_ms` high |
| `/mcp` answers 404 or the wrong thing | Emissary rewrites the prefix `/mcp` to `/`. The chart's Mapping sets `rewrite: /mcp`; keep it |
| `/mcp` returns 401 | expected without a token. Test it with a service token, because the gate answers before routing and a broken route looks healthy |
| `/install` returns 421 | `CSA_PUBLIC_URL` unset or not `https://<host>`. The chart sets it from `host`; check `host` |
| `/latest/*` 404 | the image has no client binaries; use the published `1.1.3` image |
| Pod `ImagePullBackOff` | the `ghcr` secret is missing, in the wrong namespace, or the token lacks `read:packages` |
| Pod `Init:CreateContainerConfigError` | a Secret or ConfigMap the pod names is missing: `kubectl -n csa describe pod <pod>` names it |
| Pod `Init:Error` or `Init:CrashLoopBackOff` | the `migrate` init container failed: `kubectl -n csa logs deploy/csa -c migrate` |
| `helm install` says `image.tag is required` | you installed the chart directory, not the `.tgz`. Use the `.tgz` or set `image.tag` |
| Pod `Pending` | no RWO StorageClass, or the old pod still holds the volume |
| "LLM provider: NONE" in `kubectl -n csa logs deploy/csa -c api` | at the api's start there was no provider on AI Models, and `csa-llm` was missing, empty or had the wrong key name. The pod looks healthy anyway; this line is the only sign. It is written once: a provider added since shows after `kubectl -n csa rollout restart deploy/csa` |
| Sessions arrive but nothing is recalled | extraction is stalled, usually an invalid key: `kubectl -n csa logs deploy/csa -c etl` |
| `deploy/csa` not found | the release is not named `csa`; use `deploy/<release>` |
