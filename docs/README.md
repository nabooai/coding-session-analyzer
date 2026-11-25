# CSA documentation

## Use it

| Page | For |
|---|---|
| [Run a pilot](pilot.md) | a 2 to 4 week evaluation: who, setup, what CSA measures, cost to run, exit criteria |
| [Quickstart](quickstart.md) | a first server and a first laptop, top to bottom |
| [Install the server](public-release/install-server.md) | Docker Compose on one box: TLS, identity provider, model endpoint, backup, upgrade |
| [Install on Kubernetes](public-release/install-kubernetes.md) | the Helm chart in `server/helm/csa` |
| [Install the client](public-release/install-client.md) | `csa-client` on a laptop, joining a workspace |
| [Sign-in with an identity provider](sso.md), [Okta](public-release/sso-okta.md) | OpenID Connect sign-in and who gets into which workspace |
| [Administer workspaces](public-release/org-admin.md) | workspaces, invites, tokens, retention, the audit log and its limits |
| [Offline bundle](public-release/bundle.md) | installing without registry access, verifying signatures |
| [Trial guide](public-release/05-trial-guide.md) | a self-hosted evaluation with the E2E harness |

## Trust it

| Page | For |
|---|---|
| [Security policy](../SECURITY.md) | reporting a vulnerability, what leaves a laptop |
| [Redaction](redaction.md) | the three modes, the patterns, who sets the floor |
| [Egress](public-release/egress.md) | every outbound connection, measured |
| [Data at rest](public-release/data-at-rest.md) | every file CSA writes, who reads it, what deletes it |

## Build on it

| Page | For |
|---|---|
| [Architecture](architecture.md) | the client, the three server processes, storage, recall |
| [Contributing](../CONTRIBUTING.md) | dev setup, tests, conventions |
| [Developing CSA](development/README.md) | the change checklist and one page per area with a trap |
| [Ask API](ask-api.md) | `POST /api/ask`: recall over plain HTTP, every request field and response tier |
| [Harnesses](harnesses.md) | how a coding agent's transcripts reach CSA, and adding one |
| [Trace attributes](traces-attrs.md) | the span attribute names, a published contract |
| [Design system](design/palette.md) | UI tokens, type, the graph palette (`tokens.json` beside it) |

`redaction_samples.json` is not a page: it is the fixture both redaction implementations are tested
against. The end-to-end harnesses the install guides cite are in [`../server/e2e/`](../server/e2e/).
