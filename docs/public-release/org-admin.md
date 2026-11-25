# Org admin — running CSA for more than one company

CSA is **one central deployment**; tenant isolation happens on our side, never by giving a customer their
own server. This is the server administrator's CLI runbook: create an org, hand out its invite, revoke a
token, delete an org — and what changes for self-host design partners (nothing).

> **v1 scope:** we create orgs by hand. No self-serve signup and no billing. Sign-in through an identity
> provider is [`../sso.md`](../sso.md).

## The model in one paragraph

Every authenticated request resolves to a **principal**: `{email, org_id, role, service}`
(`analyzer/auth.py`). `role` says who the holder is **inside** their org (`admin` = manages members,
invite and taxonomy, or `member`); `service` marks a service token an admin minted for an agent or a
pipeline. No credential reaches more than one org, and there is no operator plane over HTTP.
`org_id` comes **only**
from the credential — never a header, never a request body. It selects the tenant's **corpus file**
(`data/orgs/<org>/analyzer.db`). That file *is* the isolation boundary: cross-org leakage is prevented by
which file gets opened, not by remembering `WHERE org_id = ?` on ~256 queries in 12 modules. (Two paths
made the predicate approach unworkable here: `analyzer/ask.py` has an LLM **author the SQL at request
time**, and fact ids are content hashes — `sha1(claim)` — so an identical claim in two companies collides
in every ref-keyed table. Per-org files make both non-issues.)

Two planes, two files, deliberately separate:

| Plane | File | Holds |
|---|---|---|
| Control | `data/control.db` | orgs, invites (hashed), user tokens (hashed), admin audit log |
| Data | `data/orgs/<org>/analyzer.db` | that org's corpus. Plus its own `extracted/` + `pending/` |
| Data (self-host) | `data/analyzer.db` | the **`default`** org — the historical layout, byte-for-byte |

## Auth modes

There are **two**.

| `CSA_AUTH` | Use | Identity | Org |
|---|---|---|---|
| `off` | loopback dev only (boot-refuses on a public bind) | none | `default` |
| `org` | everything else, self-host and SaaS alike | verified, per token | per token |

`token` and `cloudflare` are no longer modes; the server refuses to boot with them. An `.env` that says
`CSA_AUTH=token` changes to `org` and nothing else about that install changes. CSA does not read a
Cloudflare Access JWT; verified identity comes from the identity provider set with `csa.py sso set`
([`../sso.md`](../sso.md)).

On a deployment that hosts tenants, `CSA_AUTH_TOKEN` is **refused**: every request gets a 401 telling
the client to run `csa-client setup`, from the moment the first org exists or `default` is adopted.
There is no deployment-wide HTTP administration API for it to reach. `csa.py org tokens` lists every
machine that holds a per-user token; one missing from that list still sends the shared secret.

On a **self-host** box (no provisioned orgs, `default` not adopted) the shared secret is not an
"operator credential" at all — it is *the* credential, the one every `csa-client` on that team sends.
Nothing there changes.

**A server can no longer get stuck refusing to boot because an org was created by accident.** That
failure mode, and the `csa.py org delete <org>` recovery it needed, both belong to the mode split. There
is no single-tenant mode to be half-migrated out of any more.

What creating an org DOES change, and this is worth knowing before you do it on a private box: the
deployment becomes multi-tenant, which switches off the env-invite path (`/cli-auth` then accepts only
real org invites) and switches the signed-out front door to the marketing landing with its public
"Request a workspace" form. Set the `request_webhook` setting first. Deleting the org reverses both.

## Create an org

```bash
# On the server, from server/. Creating an org makes this deployment MULTI-TENANT, which makes
# CSA_AUTH_TOKEN refused everywhere, closes the env-invite path, and flips the signed-out front door
# to the public landing. See the section above before running this on a box that has users.
uv run python csa.py org create "Acme Corp"
#   org:    acme-corp
#   invite: inv_XCq3…            <- shown ONCE. Only a hash is stored; we cannot recover it.
```

Give the invite to the customer. Their developers run the one-liner
(`curl -sSL https://<server>/install | sh`), paste the invite on the Authorize page, and each gets their
own token bound to `acme-corp` + their email + hostname. See `install-client.md`.

```bash
uv run python csa.py org list          # orgs + active token counts
uv run python csa.py org tokens --org acme-corp
#   TOKEN_HASH   EMAIL              ORG        ROLE     HOST      LAST USED             STATE
#   9f2a1c4e…    dev@acme.com       acme-corp  member   MBP-3     2026-07-16T09:12:00Z  active
#   3b8e07af…    boss@acme.com      acme-corp  admin    MBP-1     2026-07-16T08:40:00Z  active
```

`last_used_at` exists so revoking is not guesswork — you can see what you are about to break. It is
refreshed at most hourly, so it is a "roughly when", not an audit-grade access log.

## Rotate an invite / revoke a token

```bash
uv run python csa.py org invite acme-corp     # new invite; the OLD one dies immediately
uv run python csa.py org revoke 9f2a1c4e…     # by TOKEN_HASH (plaintext is never stored)
```

**Rotating an invite does not sign anyone out.** It invalidates the invite for *future* `cli-auth`
approvals only; already-minted user tokens keep working. Rotate when an invite leaks; revoke tokens when a
person leaves. Both are recorded in `admin_log`.

A revoked token 401s everywhere (HTTP + MCP) immediately, and **never falls through** to the shared secret.

## Delete an org

```bash
uv run python csa.py org delete acme-corp     # prompts; --yes to skip
```

Removes the corpus, queue, extraction cache (one `rm -rf` of `data/orgs/acme-corp`), every token, and the
org row. The `admin_log` entry survives on purpose — it is the record that the deletion happened.

**What this does not reach**, and you must be able to say so on a call:
- **Backups / volume snapshots** — deletion is not retroactive. Decide your retention and disclose it.
- **The `default` org is refused** — that is the self-host corpus itself, not a tenant.

## Self-host design partners: nothing changes

A company running their own CSA runs `CSA_AUTH=org` + `CSA_AUTH_TOKEN`
and is the **`default`** org. The shared secret is NOT demoted there: with no provisioned orgs it is
that team's own credential, not an operator's, and there is nobody to isolate them from.
`default` *is* `data/analyzer.db`, the same file, same path, same layout as before the org layer. There is
**no migration**: existing installs and `server/e2e/trial.sh` behave exactly as they did. Client-asserted identity via `X-CSA-User` still works there, as documented.

For self-host, `CSA_ORG_INVITE` (or, unset, `CSA_AUTH_TOKEN`) is the invite the `cli-auth` page accepts,
onboarding the team into `default`. That env fallback switches **off** as soon as the deployment hosts
tenants — any provisioned org, or `default` itself adopted as one — so the shared secret can never double
as a live invite on a box that serves customers. The gate is **tenancy, not the auth mode**: every
self-host box runs `org` too, so gating on the mode would kill `/cli-auth` for all of them.

## Known ceilings in v1 (say these out loud to customers)

- **No cross-org view — for us either.** There is deliberately no super-view API, and the shared
  secret is *refused* on every route rather than merely lacking a listing endpoint. That is still a
  statement about the API surface, **not** an access control: we have shell on the box and the files
  are right there. Do not let that tempt you into upgrading this sentence.
- **The local overview CLI opens every tenant's corpus file.** `csa.py org overview` reads session
  counts, fact counts, spend and last activity. There is no network route for it, but someone with
  shell access to the server already has filesystem access. Do not claim host operators cannot read
  tenant files.
- **Improve is org-wide for workspace admins.** It used to be operator-only. A customer's own admin can
  now see every member's cost, idle time, failed calls and file paths *inside their own org*. That is
  the intended tenancy model, and it is a real change in who sees those numbers.
- **We are not a processor of your data at all; the endpoint you configure is.** Every model call goes
  through one seam (`analyzer/llm.py`) to the endpoint you configure, with your key: a provider on
  Admin > AI Models, or with none registered the Claude credential in `.env` (`ANTHROPIC_BASE_URL` for
  a gateway). Three prompt classes reach it: the redacted transcript of a session,
  at extraction; the extracted facts, at judging; the question plus the facts that matched it, in ask,
  route and search. Nothing else leaves the box (`egress.md`). On the hosted service the same three
  classes go to the operator's endpoint on the operator's key, and that belongs in the contract.
- **Redaction is best-effort.** [`redaction.md`](../redaction.md) says outright that redacted output is not
  "safe to share" on its own. Say so before signing, not after.
- **One api container by design.** One api, one mcp, one etl ([`process-state.md`](../development/process-state.md)): the
  judge lock and the request-rate window live in the api process, the drain timers in the etl process.
  One SQLite writer per file (corpus, telemetry, control). The `backup` compose profile snapshots
  daily to `data/backup-*/`, verified, pruned to 7, on the same disk as the live data
  (`install-server.md` §6). RPO is the last verified snapshot; restore is a manual drill; nothing
  copies a snapshot off the box for you. `data-at-rest.md` carries the numbers.
- **Budget queues, never denies.** `tenant_config.daily_usd` (default 0, which means no cap; `csa.py org config`) and
  `daily_tokens` (default 0, off) cap one org's daily extraction spend at the configured endpoint, the default org included
  (`csa.py org config <org> --set daily_usd=… daily_tokens=…`). Past either, sessions are **stored and
  queued**, never dropped: the client dedups on a marker in
  `~/.csa/shipped/`, so denying could lose a session permanently. Drain them with `csa.py drain` once
  you've talked to the customer. Per-customer burn is visible: each org's `costs` table *is* its bill.

## Audit-log limits (disclose these to customers)

`admin_log` records every *mutating* admin action (among them `create_org`, `rotate_invite`,
`mint_token`, `revoke`, `set_role`, `config_set`, `sso_set`, `provider_saved`, `delete_org`) with a
timestamp. Its limits, which a DPA/security questionnaire will ask about:

- **No administrator-actor column.** The log records *what* changed, not which person at the host ran
  a CLI command. Tenant token mints still carry the developer's email and hostname. Named host-admin
  identity is a follow-up if a remote operator plane is ever rebuilt.
- **Reads are not logged.** `list_orgs`, `list_tokens` and local CLI reads leave no record, so "did
  someone with shell access view our metadata" is unanswerable from this log.
- **`delete_org` leaves residual PII in the log by design.** The surviving `mint_token` rows still hold the
  deleted org's developer emails + hostnames — that *is* the record of what existed, but it is customer PII
  that outlives the corpus, so account for it in your retention policy.

`GET /api/org/audit` hands a tenant admin their own slice of this log, and **declares both gaps in its own
payload** (`"limits": {"actor": false, "reads_logged": false}`) rather than leaving a customer to infer
them from a blank column.

## The tenant-admin surface: `/api/org/*`

A customer managing their **own** workspace. It is the only HTTP administration surface and can
never be pointed at another org.

| route | does |
|---|---|
| `GET /api/org/members` | who is connected, with the token hash to revoke by |
| `GET /api/org/invite` | whether an invite exists and when it last changed. A READ: the invite itself cannot be shown again (only its sha256 is stored), and learning that used to require rotating it |
| `GET /api/org/audit` | this org's admin history, with its own limits declared in the payload |
| `POST /api/org/member/role` | promote or demote a colleague. Keyed on the EMAIL, because the role belongs to the person: someone with a laptop and a browser session holds several tokens and must not end up half-admin |
| `POST /api/org/member/revoke` | cut off one laptop |
| `POST /api/org/invite/rotate` | new invite; already-minted tokens keep working, and the response says how many people the old one admitted so a leak can be cleaned up |
| `GET /api/config` | the workspace's settings: every field's effective value, its default, whether it was set here, its scope (workspace or deployment) and whether this caller may change it. A webhook shows as its host only |
| `POST /api/config` | `{field: value}` for one or more fields, `null` to go back to the default. Every value is checked before any is written: one refused field is a 400 with a sentence per field and nothing is stored. A changed model is tried with one short call first. Each change is an audit line, `config_set` |

**Settings have two scopes.** Workspace fields (budget, redaction, retention, sign-out, bot accounts,
judging) live in the workspace's own db and belong to its admins. Deployment fields (model provider and
names, sessions read at once, the OTLP collector, the request webhook) are shared by every workspace on
the server: `POST /api/config` lets an admin change them only when the server hosts exactly one
workspace (an on-prem box), and refuses them otherwise, where `csa.py org config <org> --set`
on the box is the way. Model provider and names are set on Admin > AI Models. The OTLP collector and the request webhook send data off the server, so a
browser session can never set them, on any server; the CLI or a bearer token can.

**You cannot revoke or demote the last admin who can actually change anything.** A dashboard session
signed in through the invite form (a typed email, hostname `web`) is a member, except the chart's
`workspace.adminEmail` arriving by its link, and is never promoted by `member/role`; it does not count
as an admin who can change membership; a session the identity provider verified (`web-sso`) or a machine token can change
membership. Both guards refuse with the remedy named (`code: last_admin`). If a sole admin's tokens idle
out, `csa.py org role <org> <email> admin` on the server restores the role.

Two properties hold by construction. The org comes from `principal["org_id"]` and **nothing else**: no
route here reads an `org_id` from a query string or a body, so there is no parameter to tamper with. And
the Team page's controls are exactly `member/role` and `member/revoke` (in `_COOKIE_POST_OK`, same-origin,
role re-checked per call, and a credential can never revoke itself: `code: self_revoke`).

**What a cookie session can now do, and the tradeoff.** Same-origin and `SameSite=Strict` stop a forged
cross-site POST; they do not stop an XSS in the dashboard itself, which already rides the cookie. So an
XSS in a workspace admin's tab can now change roles and revoke machines. That is deliberate: both are
reversible, both are audited, both refuse to remove the last admin or the caller's own credential, and
the alternative was a Team page whose controls no admin could use from a browser. `invite/rotate` stays
out precisely because it is not reversible in that sense: its response hands back a credential that keeps
working after the stolen session is gone.
**`invite/rotate` stays bearer-only**: its response carries the new invite in plaintext, a credential
that would outlive the session an XSS stole it from. Rotate on the server (`csa.py org invite <org>`) or
with the per-user token `csa-client` keeps in `~/.csa/config.json`:

```bash
curl -X POST https://csa.example.com/api/org/member/revoke \
  -H "Authorization: Bearer $(jq -r .auth_token ~/.csa/config.json)" \
  -H "Content-Type: application/json" -d '{"token_hash": "<from /api/org/members>"}'
```

Requires `role="admin"` on the caller's token (`csa.py org tokens` shows roles). This grants authority
inside exactly that workspace; it never grants cross-workspace access.

## Also run the SaaS in org mode

`docker-compose.trial.yml` defaults to `CSA_AUTH=org`. A budget is each workspace's own
(`csa.py org config <org> --set daily_usd=<budget>`; the default 0 means no cap). `CSA_AUTH_TOKEN` is only the self-host team's
credential; on a box that hosts tenants it is refused. Everything else — bind mount, health check,
MCP — is identical.

Turning an existing single-tenant deployment into a named tenant is `csa.py org adopt-default`
([install-server.md](install-server.md) §4a): one INSERT, zero bytes moved. Deleting the `default`
org row in `control.db` undoes it.

## Get notified when someone requests a workspace

Set the **`request_webhook`** setting (`csa.py org config <org> --set request_webhook=https://...`; a
deployment setting, so any org id works) and the server POSTs a one-line JSON message (`{"text": …}`,
Slack-incoming-webhook shaped) to it each time a NEW request lands from the landing page — carrying the
company, email, and the exact `csa.py org approve <id>` command. Unset (the default) means no notice;
the request still lands as a pending row you list with `csa.py org requests`. It fires only on a genuinely
new request (duplicate re-submits are silent), in a background thread (a slow/dead webhook never delays or
breaks the request), and is fail-open (errors are logged, never surfaced). The URL is yours — a Slack
channel, or any endpoint you route from.

## Cross-workspace administration is CLI-only

There is no deployment-wide HTTP administration namespace and no global secret in the browser.
Run these on the server, directly or over SSH:

| Command | Does |
|---|---|
| `csa.py org overview` | all workspaces, token counts, activity and spend |
| `csa.py org create/list/delete` | provision and remove workspaces |
| `csa.py org tokens/revoke/role/invite` | access and workspace-admin bootstrap |
| `csa.py org requests/approve/audit` | process requests and inspect the audit log |
| `csa.py org fleet <org>` | list machines and backfill directives |
| `csa.py org fetch <org> [--since …] [--email …] [--hostname …]` | request history |
| `csa.py org fetch-revoke <id>` | cancel a live directive |

A future remote console must use named, per-user host-administrator identities and record its actor;
it must not restore a shared deployment secret.

## Fleet: asking machines for history

Configuration used to flow one way — a laptop decided what it shipped and the server had no say. A
**directive** is one admin instruction, `{who, since}`: *ship me everything from this date*. Issue it
with `csa.py org fetch <org>` over SSH and monitor it with `csa.py org fleet <org>`.

- `email` empty targets **every machine in the org**; with a `hostname` it narrows to one laptop. A
  bare hostname is refused, since hostnames are not unique across people.
- `since` is a date (`2026-08-19`) or an ISO instant; empty means all history. It is validated when
  the directive is created, because a client that cannot parse a floor ships **nothing**.
- Each machine acts on it **once**, at its next SessionStart, and acks back. The console shows
  `waiting` until something answers.
- **A directive does not override a local decline.** A machine set up with backfill off refuses and
  acks `declined`. The consent record on the laptop is the authority on what may leave it.

## Retention: keeping transcripts

Each workspace's `archive_days` (a `tenant_config` field in its own db; default **0** = keep none)
makes the server retain the redacted transcript of each session for that many days, or with the
literal **`forever`** until someone deletes it. Off, the corpus behaves exactly as it always has:
facts are extracted and the text is deleted.

| | |
|---|---|
| Who can read one | The session's **owner** and a **workspace admin** of that org (the admin role on a per-user token). Loopback development is open; on a self-host box the shared secret is the credential every client holds, so it never bypasses ownership. Never teammates. |
| Where | `data/orgs/<org>/archive/<sid>.json`, the org's own directory |
| Purged by | The ETL, every 60 minutes (`drain_loop.PURGE_MIN`), each workspace against its own window. Under `forever` nothing is aged out; only a forget removes a transcript. |
| Clock starts | When the session **reaches the server**, not when it was recorded. A backfilled six-month-old session is kept 30 days from upload. |
| Scope | **Per workspace.** Every org on a server carries its own value; turning it on for one tenant changes nothing for the others. |
| Deleted by | `/api/forget`, along with everything else derived from that session. The owner (or the credential that shipped it) may forget their own sessions; a workspace admin may forget any session in their org. An admin of another org gets the same answer as for an id that never existed. |

Turning it back to `0` **clears** what is already stored on the next purge: a disable is not a freeze.
Any value that is neither a whole number from 0 to 3650 nor `forever` is refused when it is set.

### Operator step: keep transcripts until deleted

The public landing says a session and everything from it goes when the person who shipped it or the
workspace admin deletes it, with no automatic expiry. The server only honours that once the operator
sets it for the workspace; a code deploy does not change retention on its own.

```bash
# per workspace; read on every request, so no restart
uv run python csa.py org config <org> --set archive_days=forever
uv run python csa.py org config <org> --set archive_days=30    # a window instead
uv run python csa.py org config <org> --set archive_days=      # back to none
uv run python csa.py org config <org>                          # show the row
```

`/api/me` and `/api/client-config` then report `archive_days: -1` for that workspace, the sign-in
page reads it as "kept until its owner deletes it", and the transcript page shows `retentionDays: -1`.
A server that hosts **one** workspace also states the number on the public `/api/auth-config` and
`/api/policy`; a server with several answers `null` there, the sign-in page says the workspace admin
sets it and shows it after sign-in, and `csa-client setup` learns it from the token the browser step
issues. Sessions shipped before the flip have no transcript on the server (the text was deleted at
extraction); it applies to sessions that arrive after it.

Release note: `CSA_ARCHIVE_DAYS` is gone. Nothing reads it; the value lives in each workspace's
`tenant_config.archive_days`, set with the command above.

The consent note below applies in full: `forever` is longer than any window a laptop consented to, so
every machine set up before the flip pauses until its person re-runs `csa-client setup --backfill`
and accepts the new terms. Clients older than the release that understands `-1` read it as "keeps
none" and keep shipping without asking; release the client first if that pause is the point.

The number is advertised on `/api/policy` and in the token the browser step issues, so `csa-client
setup` prints it in the consent notice and records it. On a server with several workspaces a
first-time setup holds no token yet: the notice says the admin sets it, the consent records none, and
if the workspace keeps any, setup says so right after authorizing and the laptop ships nothing until
its person re-runs `csa-client setup`, which now presents a token and shows the number.
**Turning it on pauses every laptop that consented to less.** This is the blast radius, and it is
the thing to weigh before flipping the switch: a client refuses to ship to a server that has raised
retention above what its user accepted, and says so only in `~/.csa/ship.log`, which nobody reads.
Team memory stops growing for that person until they re-run `csa-client setup --backfill` and accept
the new terms. On a team of twelve that is twelve people who must each act, and `csa-client doctor`
still reports green. Plan the announcement before the change.

Lowering it again needs nobody's permission, and un-pauses machines on their own: each one re-reads
the window at its next session start and resumes.

## Verify it

```bash
server/e2e/run.sh org        # two orgs, one deployment, every cross-org negative
server/e2e/run.sh trial      # the legacy single-tenant gate — must stay green
```
