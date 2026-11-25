# CSA on Kubernetes

One image, one pod, one volume. No external database.

The chart installs `ghcr.io/nabooai/csa-server-slim`, a public image with no Claude Code. An API key or
a provider on AI Models needs nothing else. AWS Bedrock, Google Vertex and subscription tokens need
[an image you build](#build-the-image-yourself) with `--target with-claude-code`.

```
                    https://csa.your-company.example
                                 │
                        Ingress (TLS ends here)
                        /mcp ──────────┐   / ───────────┐
                                       ▼                ▼
   ┌─ pod: csa ────────────────────────────────────────────────────┐
   │  init: migrate      api :8766      mcp :58768      etl        │
   │            └──────────── /app/data (PVC, RWO) ────────────┘   │
   └───────────────────────────────────────────────────────────────┘
        egress: the model endpoint (api.anthropic.com or your gateway) and the identity provider
```

Chart: `server/helm/csa`. Same processes as `docker-compose.trial.yml`; the cluster's front door
(Ambassador by default, or an nginx Ingress) replaces the Caddy proxy.

## 1. What you need

| | |
|---|---|
| Kubernetes | any 1.25+, Helm 3, a StorageClass with ReadWriteOnce, and Emissary/Ambassador or nginx. The pod passes the `restricted` Pod Security Standard |
| TLS | a certificate for your host in a secret named `csa-tls`, or cert-manager (`tls.clusterIssuer`, nginx front door). A test cluster can use a self-signed one, see [handoff-quickstart.md](handoff-quickstart.md) |
| Model endpoint | an Anthropic API key, an Anthropic-format gateway (`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`), or neither yet: CSA starts without one and an admin adds a provider on AI Models (section 4) |
| Image | `ghcr.io/nabooai/csa-server-slim:<version>` (the chart `csa-<version>.tgz`, attached to each `server-v<version>` GitHub release, pins it). Or [build it yourself](#build-the-image-yourself) |
| Sign-in | built-in email by default (the `workspace.adminEmail` admin link, no IdP), or an Okta OIDC app, see [sso-okta.md](sso-okta.md) |

### Build the image yourself

From the root of a clone of this repository. The build compiles the laptop client served at
`/install` from the same checkout. `CSA_VERSION` is what `/api/health` and the client report; without
it both say `dev`. Add `--target with-claude-code` for Bedrock, Vertex or a subscription token: it
downloads Claude Code from npm into your image, pinned by version and checksum, under
[Anthropic's terms](https://code.claude.com/docs/en/legal-and-compliance), so push that image to a private registry.

```bash
kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.architecture}'   # amd64, arm64, or both
docker build --platform linux/<arch> --build-arg CSA_VERSION=<tag> \
    -f server/Dockerfile -t <registry>/csa-server:<tag> .
docker push <registry>/csa-server:<tag>     # a registry the nodes pull from, or load it into the node runtime
```

Build for the nodes, not for your laptop: `linux/amd64` for most cloud nodes, `linux/arm64` for arm
nodes and for kind or k3d on Apple Silicon. An image of the wrong architecture fails with
`exec format error`. Nodes of both kinds need one multi-arch image:
`docker buildx build --platform linux/amd64,linux/arm64 --build-arg CSA_VERSION=<tag> -f server/Dockerfile -t <registry>/csa-server:<tag> --push .`

On kind, skip the registry: `kind load docker-image <registry>/csa-server:<tag> --name <cluster>`, and
set `imagePullSecrets: []`.

The release's bundle is signed with Sigstore and ships an SBOM (see [bundle.md](bundle.md)). An image
you build here is neither, and its tag is mutable,
so record and pin the digest your nodes pull, or sign it yourself, if your security review needs that.

| Value | Set it to |
|---|---|
| `image.repository` | `<registry>/csa-server` |
| `image.tag` | `<tag>` |
| `imagePullSecrets` | `[]` when the nodes can already pull from the registry you pushed to (a public one, or one the node service account reads, e.g. Artifact Registry in the cluster's project). A private registry needs its own `docker-registry` secret, named here: `[<secret name>]` |

Install the chart from the clone: `server/helm/csa` in place of `csa-<version>.tgz` below. It has no
`appVersion`, so `image.tag` is required.

## 2. Install

```yaml
# my-values.yaml
host: csa.your-company.example
imagePullSecrets: []              # the image is public
workspace: {name: "Your Company", domain: your-company.example, adminEmail: it@your-company.example}
okta:
  issuer: https://your-company.okta.com/oauth2/<auth server id>
  clientId: <client id>
  adminGroup: CSA Admins          # members of this Okta group are CSA admins
```

First check the tag, in seconds, from any machine with Docker (a failed pull otherwise
surfaces only when `--wait` gives up):

```bash
docker manifest inspect ghcr.io/nabooai/csa-server-slim:<version> >/dev/null && echo pull ok
```

`manifest unknown` is the tag. Your own registry: the same command with its address, after `docker login`.

```bash
kubectl create namespace csa
kubectl -n csa create secret generic csa-llm --from-literal=ANTHROPIC_API_KEY=sk-ant-...   # optional
helm install csa csa-<version>.tgz -n csa -f my-values.yaml --wait --timeout 5m
```

The `csa-llm` Secret is optional. Without it the pod starts and the dashboard works, but extraction
and recall fail until an admin adds a provider on **Admin > AI Models** (section 4). A Secret created
later is read at the next pod start: `kubectl -n csa rollout restart deploy/csa`.

A healthy pod is ready in under a minute. On a slow node, api and mcp each get 5 minutes from their
own start before a probe restarts them; raise `--timeout` to match. If `--wait` times out (`context deadline exceeded`),
`kubectl -n csa get pods`, then `kubectl -n csa describe pod <pod>`: its Events name the real cause
(image pull, volume claim, `migrate`). Not `--atomic`: its rollback deletes that pod and its Events.

Name the release `csa`; the `deploy/csa` commands here assume it. Keep `--wait`: without it `helm install`
says `deployed` even when `migrate` fails. With it, a failed `migrate` fails the install; read
`kubectl -n csa logs deploy/csa -c migrate`. The install prints the next step (the admin sign-in or Okta).

That is the whole setup. Before the pod starts, `migrate` names the workspace, binds its email domain
and turns on Okta sign-in from the values; it does the same on every `helm upgrade`, so changing a
value is an upgrade. Anyone in `okta.adminGroup` is an admin from their first sign-in, and leaves the
role when Okta removes them from the group (at their next sign-in).

On nginx: `--set frontDoor=nginx` (and `tls.clusterIssuer` for cert-manager). The Ambassador Mappings
carry the timeouts Emissary needs (its default is 3 seconds) and keep the `/mcp` path, which Emissary
would otherwise rewrite to `/`.

### Values

| Value | | |
|---|---|---|
| `host` | required | the name people type, no `https://`. The chart refuses to render without it |
| `workspace.adminEmail` or `okta.*` | one of them | built-in sign-in for the first admin, or Okta ([sso-okta.md](sso-okta.md)). With neither, nobody can sign in |
| `workspace.name`, `workspace.domain` | recommended | the workspace and the email domain that may sign in |
| `image.tag` | chart directory only | a release's `.tgz` pins its image; the `server/helm/csa` directory has no version, so set it |
| `image.repository` | `ghcr.io/nabooai/csa-server-slim` | [your own build](#build-the-image-yourself); Bedrock, Vertex and subscription tokens need one with Claude Code |
| `imagePullSecrets` | `[ghcr]`, a secret a public image does not use | `[]` for a public image or one your nodes already pull; `[<secret>]` for a private registry |
| `frontDoor` | `ambassador` | `nginx` (an Ingress of class `nginx.className`) or `none` (route `/` to port 8766 and `/mcp` to 58768 yourself) |
| `tls.secretName`, `tls.clusterIssuer` | `csa-tls`, none | the certificate, or let cert-manager issue it (nginx) |
| `storage.size`, `storage.storageClass` | `20Gi`, cluster default | must be ReadWriteOnce |
| `storage.existingClaim` | none | mount a claim you made (a restored snapshot) instead of creating `<release>-data` |
| `networkPolicy.*` | off | see Hardening below |
| `resources.{api,mcp,etl}` | requests, memory limits | no CPU limits. etl's 4Gi fits the default 3 parallel extractions |

`values.schema.json` rejects a wrong type, a `host` with a scheme, or an unknown `frontDoor` before
anything reaches the cluster.

### No DNS yet (a test cluster)

On nginx with cert-manager, a [nip.io](https://nip.io) name resolves to the Ingress IP and gets a real
certificate, so laptops need no CA trust:

```yaml
host: csa.<ingress external IP>.nip.io
frontDoor: nginx
tls: {clusterIssuer: <your ACME http01 issuer>}
```

### No route yet: evaluate over port-forward

With `frontDoor: none` and the built-in sign-in (`workspace.adminEmail`), the dashboard works over a
port-forward before any DNS, Ingress or certificate exists. `host` is still required; set the name you
will route later.

```bash
kubectl -n csa port-forward svc/csa 8766:8766        # leave it running; the rest in a second terminal
kubectl -n csa logs deploy/csa -c migrate | grep "admin sign-in link"
# applied: admin sign-in link for it@your-company.example, valid 7 days: https://<host>/join/jn_...
```

Open that link in Chrome with `http://localhost:8766` in place of `https://<host>`:
`http://localhost:8766/join/jn_...`. **Continue as** signs you in as the admin and lands on the
first-run screen.
Chrome accepts the `Secure` sign-in cookies on `localhost` without TLS.

`migrate` runs on every install, upgrade and pod restart, and until the admin has signed in, each run
prints a new link and revokes the previous one. Use the last line the grep prints. Once the admin is
in, `migrate` prints none.

Only the dashboard works this way. The install one-liner, laptop `setup`, invite links and Okta
redirects all carry `https://<host>`, so laptops and Okta need the real route.

### Hardening

Every container runs as uid 10001 with a read-only root filesystem, no capabilities, no privilege
escalation and the `RuntimeDefault` seccomp profile, so the namespace can enforce
`pod-security.kubernetes.io/enforce=restricted`. Writes go to the data volume and two `emptyDir`s
(`/tmp`, and `/home/csa`, where the Claude CLI keeps its state on an image that has it). The pod mounts no service account token
and gets no Service environment variables.

`networkPolicy.enabled: true` lets only `networkPolicy.ingressFrom` connect, and lets the pod reach only
DNS and https (except the cloud metadata address). Name your front door's namespace:

```yaml
networkPolicy:
  enabled: true
  ingressFrom: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}}]
```

The default egress allows any address on 443. To allow only your model gateway and identity provider,
replace the list (Helm replaces a list, it does not merge it):

```yaml
networkPolicy:
  egress:
    - ports: [{port: 53, protocol: UDP}, {port: 53, protocol: TCP}]
    - to: [{ipBlock: {cidr: <gateway IP>/32}}, {ipBlock: {cidr: <identity provider range>}}]
      ports: [{port: 443, protocol: TCP}]
```

A NetworkPolicy matches addresses, not names: an endpoint whose IPs change (api.anthropic.com, a SaaS
identity provider) needs an egress proxy or your network plugin's FQDN policy instead. A gateway or
collector on another port needs its own rule. The policy takes effect only where the network plugin
enforces it (on GKE: Dataplane V2, or network policy enforcement turned on).

Verify:

```bash
curl -s https://csa.your-company.example/api/health      # {"ok": true, "version": "onprem-...", "schema": {...}}
curl -so /dev/null -w '%{http_code} %{size_download}\n' https://csa.your-company.example/latest/csa-client-linux-amd64   # 200 and a few MB
```

`/` serves the dashboard with **Sign in with Okta**, `/install` the laptop one-liner, `/mcp` answers
401 without a token. Check `/mcp` once WITH a token (an agent's, section 6): without one the gate
answers before routing, so a broken route looks healthy.

## 3. The command line, when you need it

Nothing in a normal install needs it. For backups, `org config` and the rest:

```bash
CSA="kubectl -n csa exec deploy/csa -c api -- /app/.venv/bin/python csa.py"
```

## 4. AI Models: the model providers, in the app

An admin opens **Admin > AI Models**, clicks **Add provider** and picks the type: Anthropic, OpenAI or
Azure OpenAI, or AWS Bedrock or Google Vertex on an image with Claude Code (on any other image the page
shows those two disabled and says why). Each asks for its key (Bedrock: region and
`<access key id>:<secret>`; Vertex: project, region and the service account's JSON key) and the model
ids it serves. A provider is saved only after its first model answers a one-line test call. Then two
pickers choose the **main model** (reads each session, answers questions) and the **small model**
(judges facts, routes questions).

Until an admin adds a provider, CSA runs on the Claude credential in the `csa-llm` Secret
(`ANTHROPIC_API_KEY`), on sonnet and haiku. With neither, extraction and recall fail and the api log says
`LLM provider: NONE`. On the published image `ANTHROPIC_API_KEY` goes to the Messages API over HTTPS; on
an image with Claude Code it runs through the binary. [install-server.md §7](install-server.md#7-llm-provider-how-the-model-is-authenticated)
has what each credential needs, and why a subscription token is not for a team server. Spend is uncapped by default. A workspace's daily cap is set from the command
line: `$CSA org config <org> --set daily_usd=<dollars>` (or `daily_tokens=<tokens>` for a model
without a list price). Past the cap, sessions wait in the queue for the next day; see
[the pilot budget](../pilot.md#budget).

Keys are stored in `control.db` on the data volume as entered and are never sent back to a browser.
Anyone who can read the volume can read them; protect the volume like the Secret.

**What the environment still carries.** Nothing else in the code reads a `CSA_*` variable:

| Variable | Set in | Default | What it is |
|---|---|---|---|
| `CSA_PUBLIC_URL` | ConfigMap | none, required | the URL people type; installers and sign-in redirects are built from it |
| `CSA_MCP_URL` | ConfigMap | host + `CSA_MCP_PORT` | what laptops and agents register; `<CSA_PUBLIC_URL>/mcp` behind the Ingress |
| `CSA_AUTH` | ConfigMap | `off` (loopback only) | `org` for any real install |
| `CSA_BIND` | ConfigMap | `127.0.0.1` | where the api listens; `0.0.0.0` in a pod |
| `CSA_API_PORT` | default | `8766` | the api port |
| `CSA_MCP_BIND`, `CSA_MCP_PORT` | default | `0.0.0.0`, `58768` | where the mcp server listens |
| `CSA_AUTH_TOKEN` | not used on-prem | none | the open-egress trial's shared invite |
| `CSA_ORG_INVITE` | not used on-prem | none | a self-host invite distinct from the token |
| `CSA_CLIENT_DIST` | the image | `/app/client-dist` | where the client binaries served at `/latest/` live |
| model credential | Secret `csa-llm`, optional | none | `ANTHROPIC_API_KEY`: the default until providers are added on AI Models |

**The chart's values are a file, not variables.** `workspace.*`, `okta.*` and `telemetry.otlpEndpoint`
are rendered into one JSON file in a ConfigMap (`<release>-config`) and mounted read-only at
`/etc/csa/config.json` in every container. `csa.py migrate` (the init container) reads it on each
install and upgrade and applies what changed; a box with no file, or a key left empty, is left as it
is. A file that is not valid JSON stops the pod before it starts, with a sentence naming the file.
Change a value, `helm upgrade`, done. There is no environment fallback.

`workspace.adminEmail` is for installs without Okta: `migrate` logs a one-person sign-in link that makes
that address the admin (see [handoff-quickstart.md](handoff-quickstart.md) §2). If the link is lost,
`$CSA org admin-link` prints a fresh one. With `okta.*` set it does
nothing; admins come from `okta.adminGroup`.

The chart writes both ConfigMaps from values; you never edit them by hand.

## 5. Laptops (people)

```bash
curl -sSL https://csa.your-company.example/install | sh
csa-client setup --server https://csa.your-company.example
```

`setup` opens the browser sign-in, then installs the hooks and registers the `csa` MCP server.
macOS and Linux; the binaries come from the pod, nothing on a laptop needs GitHub.

## 6. Agents (machines that cannot sign in)

An agent harness has no Okta account. In the dashboard, an admin opens **Admin > Service Tokens**,
names the machine (`atlas`) and the address it speaks as (`atlas@your-company.example`), and copies
the token. It is shown once. Everything the machine ships or asks is that address's. A service token
is always a member, even when its address is a teammate's, and no role change promotes it.

**Recall** from a Claude Agent SDK harness: one MCP server entry, the token in the header.

```python
ClaudeAgentOptions(
    mcp_servers={"csa": {"type": "http",
                         "url": "https://csa.your-company.example/mcp",
                         "headers": {"Authorization": "Bearer <token>"}}},
    allowed_tools=["mcp__csa__ask"])
```

The agent gets one tool, `ask`. First-person questions ("what did I decide") rank the asker's own
sessions first, so an agent that only recalls should ask about the team ("what did the team decide").

**Recall over plain HTTP**, without MCP: `POST /api/ask` with `Authorization: Bearer <token>` and a JSON
body. The asker is always the token's address. Every field and every answer tier:
[Ask API](../ask-api.md).

```bash
curl -s https://csa.your-company.example/api/ask -H "Authorization: Bearer <token>" \
    -d '{"query": "what did the team decide about payment retries"}'
```

**Ship** the harness's own sessions: the SDK writes transcripts to `~/.claude/projects/` like any
Claude Code session. Run the client as a watcher beside the harness, under its own `HOME` (the
watcher stops if the Claude login in `~/.claude.json` is a different person than the token's), with
`CSA_ROOT` pointing at the harness's transcripts:

```bash
curl -sSL https://csa.your-company.example/latest/csa-client-linux-amd64 -o /usr/local/bin/csa-client
chmod +x /usr/local/bin/csa-client
export HOME=/var/lib/csa-atlas CSA_ROOT=/home/harness/.claude/projects
csa-client init --yes --server https://csa.your-company.example --token <token> \
    --email atlas@your-company.example --backfill --install
csa-client            # foreground; runs until stopped
```

The hooks that `setup` and `init` install do not fire under the SDK (it loads no `settings.json`), so
`--install` tells `init` to skip them: the watcher is the shipping path. On Linux `--install` only
prints the systemd unit to copy; run the watcher under whatever supervises the harness. A machine token idles out like a laptop's (30 days unused; every ship or ask
resets the clock); `$CSA org config default --set token_ttl_days=0` turns expiry off for the
workspace. Revoke it from its row under Service Tokens.

## 7. Upgrade, backup, roll back

| | |
|---|---|
| Upgrade | `helm upgrade csa csa-<new version>.tgz -n csa -f my-values.yaml --wait --timeout 5m`. From the chart directory, add `--set image.tag=<new tag>`. `Recreate` stops the old pod, `migrate` runs, the new pod starts. Expect ~30 s of 503. Always pass your values file: without it the upgrade resets them to the defaults |
| Uninstall | `helm uninstall csa -n csa` keeps the `csa-data` claim; `helm install` with the same release name picks it up again |
| Backup | `$CSA backup --keep 7` writes a verified snapshot to `/app/data/backup-<UTC date-time>/` on the claim; pair it with your volume snapshots |
| Restore | scale to 0 (`kubectl -n csa scale deploy/csa --replicas=0`), run the restore from a one-off pod on the same claim: `/app/.venv/bin/python -m analyzer.backup restore /app/data/backup-<UTC date-time> /app/data`, scale back to 1. A claim restored from a volume snapshot mounts with `storage.existingClaim` |
| Roll back | `helm upgrade` with the previous tag. Schema migrations are forward-only: restore the backup taken before the upgrade first |

**Upgrading from a chart before the slim image.** The default `image.repository` is now
`ghcr.io/nabooai/csa-server-slim`, which has no Claude Code. Values that name `image.repository` render
exactly as before. Values that name only `image.tag` move to the slim package at the same tag: if a provider or
the `csa-llm` Secret uses Bedrock, Vertex or a subscription token, set `image.repository` to an image with
Claude Code first, or the queue parks with a sentence saying so.

**Upgrading from a build before AI Models.** That upgrade drops three old deployment settings,
`llm_provider`, `extraction_model` and `openai_model`, without copying them anywhere. They held a choice,
never a credential, so nothing secret is lost, but the choice is: after the upgrade both roles run on the
Claude credential in the `csa-llm` Secret, on sonnet and haiku. If you had picked another model or the
OpenAI path, register that provider on AI Models and set the two roles again. The environment variables that
fed the old OpenAI path (`CSA_LLM_PROVIDER`, `OPENAI_BASE_URL`, `CSA_OPENAI_MODEL`) are no longer read.

**Endpoints on AI Models are public https only.** An address that is not https, has a user name, or
resolves to a loopback, private, link-local or cloud-metadata address is refused when you save it and again
on every call, and calls ignore `HTTPS_PROXY`. A gateway on a private address belongs in the `csa-llm`
Secret (`ANTHROPIC_BASE_URL`), which only the operator can set.

## 8. When something is off

| Symptom | Cause |
|---|---|
| `/install` 421 | `CSA_PUBLIC_URL` not set or not `https://<host>` |
| `/latest/*` 404 | the image carries no client binaries: run one built from `server/Dockerfile` |
| pod `ImagePullBackOff` | your registry refused the pull: `imagePullSecrets` does not name a secret it accepts, or the tag is wrong. `kubectl -n csa describe pod <pod>` shows the registry's answer |
| `LLM provider: NONE: ... needs Claude Code` in the api log, `/api/health` says `"stalled": true` | a credential or provider that needs an image with Claude Code. Use an API key or an HTTPS provider, or [build one](#build-the-image-yourself). Sessions wait in the queue |
| `helm install --wait`: `context deadline exceeded` | the pod never got ready: `kubectl -n csa describe pod <pod>` names why |
| the admin sign-in link does not open (`frontDoor: none`) | nothing routes `https://<host>` yet: [port-forward](#no-route-yet-evaluate-over-port-forward) and open it on `http://localhost:8766` |
| `/mcp` hangs or drops | a buffering proxy in front; the manifest's nginx annotations turn buffering off and lift the read timeout |
| pod `Pending` | no RWO StorageClass, or the previous pod still holds the volume (wait for `Recreate`) |
| health 503 | control plane unreadable: `kubectl -n csa logs deploy/csa -c api` |
| "LLM provider: NONE" in the api log | at the api's start there was no provider on AI Models, and `csa-llm` was missing or had no `ANTHROPIC_API_KEY` key (a wrong key name looks healthy otherwise). The line is written once: a provider added since shows after `kubectl -n csa rollout restart deploy/csa` |
| sessions arrive, nothing recalled | extraction stalled, usually an invalid key: `kubectl -n csa logs deploy/csa -c etl` |
| pod `Init:Error` | `migrate` failed: `kubectl -n csa logs deploy/csa -c migrate` |
| pod `Init:CreateContainerConfigError` | a Secret or ConfigMap the pod names is missing: `kubectl -n csa describe pod <pod>` names it |
| api or mcp restarting, `Exit Code 137`, reason not `OOMKilled` | its probe killed it: no answer within 5 minutes of starting, or none for 2 minutes after. Usually a starved node; the pod's Events show the probe failures |
| `/api/health` says `"version": "dev"` | an image built without `--build-arg CSA_VERSION=<tag>` |
| `exec format error` in the pod log | the image is for another CPU architecture than the node: [rebuild](#build-the-image-yourself) |
| `helm install`: `host is required` | set `host` in your values file |
| `helm install`: `values don't meet the specifications of the schema` | the line under it names the value: a `host` with `https://`, a `frontDoor` other than `ambassador`, `nginx` or `none` |
| `helm install`: `networkPolicy.ingressFrom is required` | `networkPolicy.enabled` needs the front door's pods named |
| front door 502 or timeout with `networkPolicy.enabled` | `ingressFrom` does not match the front door's namespace |
| `curl` or a browser rejects the certificate | a self-signed `csa-tls`: trust it, see [install-client.md](install-client.md#on-prem-servers-a-private-ca-an-egress-proxy) |
| no Admin group in the sidebar (Analytics, AI Models, Service Tokens) | you are not in `okta.adminGroup`, or the Okta app sends no `groups` claim in the ID token (add a groups claim rule); sign out and in again |
| sign-in refused | the issuer's discovery names a different `iss`, or the redirect URI is not registered; see [sso-okta.md](sso-okta.md) |
