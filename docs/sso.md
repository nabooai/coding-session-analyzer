# Sign-in with an identity provider

CSA signs people in through one OpenID Connect issuer: Okta, Microsoft Entra, Keycloak, Google
Workspace, or anything that serves `/.well-known/openid-configuration`. The issuer is configured in
`control.db` from the box with `csa.py sso set`; nothing about it lives in the compose env. An
id_token proves who you are once, in a browser, and is exchanged for the same per-user `csa_` token
every other path issues, so the request path, the MCP gate and the Go client are untouched.

```
browser                       CSA                                  issuer
  |  GET /api/sso/start?return=/#/team                                |
  |<-- 302 + __Host-csa_sso (Lax; the state's hash) ------------------|
  |  GET <authorization_endpoint>?code+PKCE(S256)+state+nonce  ------>|
  |<-- 302 /api/sso/callback?code&state -------------------------------|
  |  GET /api/sso/callback (cookie must equal sha256(state))          |
  |                              POST <token_endpoint> code+verifier ->|
  |                              <- id_token                           |
  |<-- 200 page: Set-Cookie __Host-csa_session (Strict); hops to return|
```

Configuring an issuer closes the shared-invite and typed-email sign-ins outright. There is no
migration window in which both work: a gate you can walk around is not a gate.

## Who gets in: one rule, two kinds of domain claim

`orgs.org_for_identity(email, domain_claim)` admits a verified identity into the org whose `domain`
equals the claim, and denies everything else. There is no allowlist and no env fallback; a person
outside every org's domain enters through a per-person invite link, which the sign-in carries
through the round trip and matches against the verified address. An admin makes one with
`POST /api/org/invite/new`; the Team page's **Invite** button shows only while no issuer is
configured. (`csa.py org invite` rotates the workspace's shared invite, which an issuer closes.)

What the claim is depends on the issuer, and this is the one place the two differ:

- **Google**: the claim is the `hd` (hosted domain) claim, not the address's domain. A consumer
  Google account can hold a verified address at any domain it receives mail for, so
  `alice@yourcompany.com` proves control of a mailbox, not membership of your Workspace. `hd` is
  the claim that means "this account is governed by that Workspace". A gmail.com account has no
  `hd` and can only enter through an invite link.
- **Every other issuer** (Okta, Entra, Keycloak): the claim is the verified address's domain. The
  issuer itself is the trust boundary: only that tenant's users can mint an id_token with CSA's
  `aud`, so the address the issuer vouches for is enough.

`email_verified` is refused when present and false, and admitted when absent (Entra sends none).
The domain rule still applies either way.

## Per provider

| Provider | App type | Client secret | `email_verified` | Issuer string |
|---|---|---|---|---|
| Okta | Native or Single-Page App (public, `token_endpoint_auth_method=none`) | none | sent with the `email` scope | `https://<org>.okta.com/oauth2/default` (custom auth server) or `https://<org>.okta.com` (org auth server); the discovery document must name the same string |
| Microsoft Entra | Web app | required (`--client-secret-stdin`) | not sent; admitted | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| Keycloak | public client | none | sent | `https://<host>/realms/<realm>` |
| Google Workspace | Web application | required (`--client-secret-stdin`); Google's token endpoint accepts only `client_secret_post` or `client_secret_basic` | sent | `https://accounts.google.com`, claim = `hd` |

Common to all: authorization code with PKCE (S256, always on), scope `openid email profile`,
redirect URI `<CSA_PUBLIC_URL>/api/sso/callback`. The redirect URI is built from `CSA_PUBLIC_URL`,
never from the request's `Host`, and `sso set` prints it.

## Turning it on

```bash
# on the box, inside the api container (`docker compose exec api /app/.venv/bin/python csa.py ...`)
csa.py org domain <org> <domain>            # or `org adopt-default <name> --domain <domain> --yes`
csa.py sso set --issuer https://<issuer> --client-id <id> --name "Okta"
# a confidential client (Google, Entra): the secret comes from stdin, never argv
printf '%s\n' "$SECRET" | csa.py sso set --issuer https://accounts.google.com --client-id <id> --name Google --client-secret-stdin
csa.py sso show                             # never prints the secret
csa.py sso clear                            # back to invite sign-in
```

`sso set` must run where `CSA_PUBLIC_URL` is set, which is the api container's own environment, not
an operator's shell (`docker compose exec api ...`). It refuses outright
if the variable is not there rather than printing a placeholder redirect URI. It needs no restart:
the row is read fresh on every request, so a `sso set` on a running box takes effect on the next
sign-in.

`sso set` validates the URL (https; no userinfo, query or fragment), fetches the discovery document,
refuses it unless its `issuer` equals the configured one byte for byte, and takes effect on the next
request: the row is read once per request, never cached in the process. Type the issuer exactly as
the provider's discovery document names it, trailing slash included (Auth0 names its with one,
Okta and Google without): `iss` is an exact string compare. Register the printed redirect URI with
the provider before anyone signs in.

**The server refuses to boot** with an sso row and no org domain: every sign-in would be denied,
and it is better to fail at deploy time than at 9am on rollout day. The message names
`csa.py org domain`.

That check runs at boot only, the same shape as the trap in
[`development/operations.md`](development/operations.md). Clearing an org's domain (`csa.py org domain <org>` with no domain
argument) on a running box does nothing visible; the landmine detonates on the next restart. Set the
domain before you `sso set`, and think twice before clearing one on a box that is already live.

**The client secret is the first plaintext credential in control.db.** Tokens and invites are
stored as hashes; the secret is sent to the token endpoint, so it cannot be. A control.db backup
now carries it, and rotating it is `sso set` again.

`sso set` replaces the whole row, not just the fields you pass. Rotating the client id or the
display name while keeping the same client secret still needs `--client-secret-stdin`; leave it off
and the stored secret is gone.

## What the round trip protects

- **Login CSRF.** `start` sets `__Host-csa_sso` (SameSite=Lax, HttpOnly, Secure) carrying the
  state's sha256; the callback requires the cookie to equal the hash of the `state` it received, so
  a sign-in can only complete in the browser that started it.
- **Replay.** The state row is claimed in the statement that deletes it. A second callback with the
  same state finds nothing. Rows expire after ten minutes and are pruned on every `start`.
- **Token substitution.** `iss` (with or without the scheme), `aud`, `nonce`, `exp` and `iat` are
  checked against what CSA sent, after the signature verifies against the discovered `jwks_uri`.
- **Cross-site cookies.** `__Host-csa_session` and `__Host-csa_join` are SameSite=Strict and are
  not sent on the navigation that follows a cross-site redirect. The join cookie is therefore read
  at `start` (same-site) and its hash stored in the state row (nothing redeemable at rest), and the
  callback's own page hops to the return path with `location.replace`, a same-site navigation. The
  pairing nonce for `csa-client setup` rides that return path; it never goes through the issuer.
- **A link that does not fit.** A browser carrying someone else's link (an admin previewing one, a
  link pasted in a channel) is still admitted when the domain rule admits the signed-in address; the
  link is spent on any refusal, so it cannot keep refusing for the rest of its hour.
- **Return address.** `return` must be a path on this origin (one leading slash, not `//`).

Failures are pages on CSA's origin, never a 500. A wrong `aud` or `iss`, an unreachable token
endpoint, or a discovery mismatch is an operator's problem: the page says sign-in is unavailable
and the server log says why. An expired or replayed state, an unverified address, or a domain no
org claims each get their own sentence.

## SSO does not replace the terminal binding

`csa-client setup` still prints a word pair you type into the page. The issuer proves who you are;
the word pair proves which terminal asked. A dashboard session that signed in through the issuer
(`web-sso`) can authorize a machine on the code alone, and the laptop's token inherits that person's
role. A session from the old invite form (`web`) is not taken once an issuer exists.

## Known ceilings

- **Idle expiry only.** A token in use never re-proves identity; a stolen, actively used token lives
  as long as the thief keeps using it. Revocation covers that today.
- **One group, Okta only.** With `okta.adminGroup` set in `config.json`, a member of that group is
  made admin at sign-in and anyone else member ([sso-okta.md](public-release/sso-okta.md)). Without
  it, everyone in an allowed domain gets the same access and roles are set in CSA (`csa.py org role`,
  the Team page).
- **One issuer per deployment.** A box hosting several workspaces signs all of them in through the
  same issuer; each org's domain decides where a person lands.
