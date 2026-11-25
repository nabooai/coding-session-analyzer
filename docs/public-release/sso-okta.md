# Sign in with Okta

People sign in to CSA through your Okta. CSA holds no password and no client secret: the browser does
an OpenID Connect authorization-code flow with PKCE against Okta, CSA verifies the ID token against
Okta's keys, and the verified email decides the workspace. Agents and CI machines do not sign in;
they get a service token from Admin > Service Tokens (see [install-kubernetes.md](install-kubernetes.md) §6).

```
 browser ──(1) GET /api/sso/start──▶ CSA ──(2) redirect + PKCE──▶ Okta
 browser ◀─(4) session cookie────── CSA ◀─(3) code → ID token ── Okta
```

## What your Okta admin creates

| | |
|---|---|
| Application | **OIDC, Native** (or Single-Page App). A public client: `token_endpoint_auth_method = none`, no secret |
| Grant type | Authorization Code, PKCE on (Okta forces it for these app types) |
| Sign-in redirect URI | `https://csa.your-company.example/api/sso/callback` (exact string, https) |
| Scopes | `openid email profile` |
| Assignments | the people who should reach CSA (a group is easiest) |
| Authorization server | either works. Custom: issuer `https://<org>.okta.com/oauth2/<id>`; org: `https://<org>.okta.com` |
| Groups claim | for admins from Okta: a claim rule that puts `groups` in the **ID token**. CSA asks for the `groups` scope only when `okta.adminGroup` is set. Okta's org authorization server defines it; on a custom authorization server, create a scope named `groups` first or Okta refuses every sign-in with `invalid_scope`. Use a filter that always matches (for example regex `.*`): Okta leaves the claim out when nothing matches, and CSA then keeps the role as it was instead of demoting, so removing someone from the admin group demotes them only while the claim is present |

The `email` scope makes Okta send `email` and `email_verified`; CSA refuses a token whose
`email_verified` is false.

## What we need back

- the **issuer** string, as `GET <issuer>/.well-known/openid-configuration` prints it in `issuer`
- the **client ID**
- the **admin group** name, if admins come from Okta

## Turning it on

Three values in the Helm chart (see [install-kubernetes.md](install-kubernetes.md) §2), with the
workspace's email domain beside them:

```yaml
workspace: {name: "Your Company", domain: your-company.example}
okta:
  issuer: https://<org>.okta.com/oauth2/<id>
  clientId: <client id>
  adminGroup: CSA Admins
```

The chart renders them into `/etc/csa/config.json`, and `helm install` or `helm upgrade` applies it before the pod starts. From then on the invite and
typed-email sign-ins are refused; only Okta accounts on `your-company.example` get in. The issuer's
discovery document is fetched first and must name the issuer byte for byte, so a typo stops the
deploy (the `migrate` init container fails and says why) instead of failing at the first sign-in.

Members of `adminGroup` are admins from their first sign-in; removing someone from the group in Okta
makes them a member at their next sign-in. The last admin is never demoted that way. Leave
`adminGroup` empty to manage roles in CSA's Team page instead.

A compose box puts the same `workspace` and `okta` sections in `config/config.json`, which the
`migrate` service applies at `docker compose up`; or it runs `csa.py sso set` and `csa.py org role` by
hand.

## If sign-in fails

| Okta or CSA says | Fix |
|---|---|
| Okta: `redirect_uri` not in the allowlist | register the exact `https://<host>/api/sso/callback` on the app |
| Okta: user not assigned | assign the person or group to the app |
| CSA: "isn't allowed to sign in" | the email's domain is not the workspace domain (`org domain default <domain>`) |
| `migrate` fails: names another issuer | `okta.issuer` must equal the discovery document's `issuer` byte for byte |
| Okta: `invalid_scope` for every sign-in | `okta.adminGroup` is set and the custom authorization server has no `groups` scope: add one, or clear `adminGroup` |
| signed in but no Admin tab | not in `okta.adminGroup`, or the ID token has no `groups` claim. The server log then says `has no groups claim` and the role is left as it was; add the claim rule |

Reference for the flow, the checks on the token and other providers: `docs/sso.md`.
