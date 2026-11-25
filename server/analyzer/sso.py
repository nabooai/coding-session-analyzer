"""The browser's OpenID sign-in: GET /api/sso/start and GET /api/sso/callback.

Both return (status, html, headers) for the handler to send and never raise: every failure is a
page on this origin. The callback answers with a page rather than a 302 because the session cookie
is SameSite=Strict, which a browser does not send on the navigation that follows a cross-site
redirect; the page's own hop to `return_path` is same-site.
"""
import base64
import hashlib
import html
import json
import logging
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields

from sqlalchemy import column, delete, func, insert, select

from analyzer import auth, bootstrap, cliauth, db, orgs
from analyzer.tenant import SsoLogin

_SL = SsoLogin.__table__

log = logging.getLogger("csa.sso")

TTL_SECONDS = 600                                        # a sign-in that takes longer starts over
_MAX_LOGINS = 10000                                      # `start` is public; the TTL bounds normal use, this a flood
STATE_COOKIE = "__Host-csa_sso"
SCOPE = "openid email profile"
GROUPS_SCOPE = SCOPE + " groups"
GENERIC = "Sign-in is not available right now. Whoever runs this CSA can check the server log."
NOT_STARTED_HERE = ("This sign-in did not start in this browser, or it took longer than ten minutes. "
                    "Go back and sign in again.")
ALREADY_USED = "This sign-in was already completed or has expired. Go back and sign in again."


def redirect_uri(base):
    return base + "/api/sso/callback"


def valid_return(path):
    """A path on this origin: one leading slash, so `//host` and `/\\host` cannot send the browser
    elsewhere, and nothing a header could be split on."""
    return bool(path) and path.startswith("/") and not path.startswith(("//", "/\\")) \
        and "\r" not in path and "\n" not in path and len(path) <= 2000


def _state_cookie(state_hash, max_age=TTL_SECONDS):
    """Lax, not Strict: it must ride the top-level GET back from the issuer. HttpOnly, Secure and
    Path=/ are what `__Host-` requires; the value is a hash, so the cookie alone claims nothing."""
    return "%s=%s; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=%d" % (STATE_COOKIE, state_hash, max_age)


def _prune(con):
    db.run(con, delete(_SL).where(_SL.c.created < orgs._now(-TTL_SECONDS)))
    live = db.run(con, select(func.count()).select_from(_SL)).fetchone()[0]
    if live >= _MAX_LOGINS:
        db.run(con, delete(_SL).where(_SL.c.state_hash.in_(
            select(_SL.c.state_hash).order_by(_SL.c.created, column("rowid"))
            .limit(live - _MAX_LOGINS + 1))))


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def start(con, cfg, base, return_path, join):
    """-> (302, b"", headers) to the issuer, or a same-origin failure page."""
    if not valid_return(return_path):
        return _page(400, "That return address is not on this server.")
    try:
        disc = auth.discovery(cfg.issuer)
    except Exception as e:
        log.error("sso start: discovery for %s failed: %s", cfg.issuer, str(e)[:200])
        return _page(502, GENERIC)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    _prune(con)
    db.run(con, insert(_SL).values(state_hash=orgs._hash(state), nonce=nonce, verifier=verifier,
                                   join_token=orgs._hash(join) if join else None,
                                   return_path=return_path, created=orgs._now()))
    con.commit()
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": cfg.client_id, "redirect_uri": redirect_uri(base),
        "scope": GROUPS_SCOPE if bootstrap.admin_group() else SCOPE, "state": state, "nonce": nonce,
        "code_challenge": _challenge(verifier), "code_challenge_method": "S256"})
    location = disc.authorization_endpoint + ("&" if "?" in disc.authorization_endpoint else "?") + q
    return (302, b"", (("Location", location), ("Set-Cookie", _state_cookie(orgs._hash(state))),
                       ("Cache-Control", "no-store")))


@dataclass(frozen=True)
class Login:
    nonce: str
    verifier: str
    join_token: str | None                               # sha256 of the /join link, or None
    return_path: str


def _claim(con, state):
    """The row this state opened, deleted as it is read, or None: replayed, expired or never ours."""
    stmt = delete(_SL).where(_SL.c.state_hash == orgs._hash(state),
                             _SL.c.created >= orgs._now(-TTL_SECONDS)) \
        .returning(*(_SL.c[f.name] for f in fields(Login)))
    row = db.run(con, stmt).fetchone()
    con.commit()
    return Login(**dict(zip(stmt.exported_columns.keys(), row))) if row else None


def _exchange(cfg, disc, code, verifier, base):
    """The code for the issuer's token response. client_secret_post when a secret is stored; PKCE
    always. Raises on anything but a 2xx JSON body."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(base),
            "client_id": cfg.client_id, "code_verifier": verifier}
    if cfg.client_secret:
        form["client_secret"] = cfg.client_secret
    req = urllib.request.Request(disc.token_endpoint, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:            # noqa: S310 (the discovered endpoint)
        return json.loads(r.read())


def finish(con, cfg, base, state, code, idp_error, cookie):
    """The callback -> (status, html, headers). On success the page carries the session cookie and
    navigates to the row's return_path; every other outcome is a sentence on this origin."""
    if not cookie or not state or cookie != orgs._hash(state):
        return _page(400, NOT_STARTED_HERE)
    login = _claim(con, state)
    if login is None:
        return _page(400, ALREADY_USED)
    if idp_error:
        log.warning("sso callback: the issuer refused the sign-in: %s", idp_error[:80])
        return _page(400, "The identity provider refused the sign-in (%s). Go back and try again."
                     % idp_error[:80])
    try:
        disc = auth.discovery(cfg.issuer)
        tokens = _exchange(cfg, disc, code, login.verifier, base)
        id_token = tokens["id_token"]
    except Exception as e:
        log.error("sso callback: code exchange at %s failed: %s", cfg.issuer, str(e)[:200])
        return _page(502, GENERIC)
    email, claim, err = auth.verify_id_token(id_token, cfg, login.nonce)
    if err in (auth.NO_EMAIL, auth.EMAIL_UNVERIFIED):
        return _page(403, err)
    if err:
        log.error("sso callback: id_token from %s refused: %s", cfg.issuer, err)
        return _page(502, GENERIC)
    ok, result = cliauth.sso_login((email, claim), login.join_token or "")
    if not ok:
        # The link this browser carried is spent either way: a foreign or stale one must not keep
        # refusing a person the domain rule would admit for the rest of the join cookie's hour.
        return _page(403, result, (("Set-Cookie", cliauth.clear_join_cookie()),))
    if bootstrap.admin_group():
        _role_from_groups(email, result, id_token, bootstrap.admin_group())
    log.info("sso: signed in %s (domain=%s)", email, claim or "-")
    return (200, _interstitial(login.return_path), (
        ("Set-Cookie", cliauth.session_cookie(result)),
        ("Set-Cookie", cliauth.clear_join_cookie()),
        ("Set-Cookie", _state_cookie("", 0)),
        ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", "frame-ancestors 'none'")))


def _role_from_groups(email, token, id_token, group):
    """Okta decides who is admin: a member of `group` is promoted at sign-in, anyone else demoted.
    The id_token was verified above, so its payload is read as is."""
    seg = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    if "groups" not in claims:
        log.error("sso: the ID token for %s has no groups claim, so okta.adminGroup %r cannot be checked and "
                  "the role is left as it was. Add a groups claim to the Okta app (see sso-okta.md).", email, group)
        return
    groups = claims["groups"] or []
    role = "admin" if group in (groups if isinstance(groups, list) else [groups]) else "member"
    org_id = orgs.lookup(token)[1]["org_id"]
    try:
        orgs.set_role(email, org_id, role)
    except orgs.LastAdminError:
        log.warning("sso: %s left %r but is the last admin of %s; kept admin", email, group, org_id)


def _interstitial(to):
    """One page on this origin whose only job is to navigate: a script-initiated (or meta refresh)
    navigation from a same-origin document is same-site, so the Strict session cookie rides along."""
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Signed in</title>
<noscript><meta http-equiv="refresh" content="0;url=%s"></noscript></head>
<body><p>Signed in. <a href="%s">Continue</a></p>
<script>location.replace(%s);</script></body></html>
""" % (html.escape(to, quote=True), html.escape(to, quote=True), cliauth._js_string(to))).encode()


def _page(status, sentence, headers=()):
    """A failure the person can read, on this origin, with the state cookie cleared."""
    body = ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign-in failed</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>%s
body{font:15px/1.55 var(--sans);color:var(--ink);background:var(--paper);margin:0;padding:3rem 1.25rem}
main{max-width:30rem;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:1.75rem}
h1{font:600 1.5rem/1.25 var(--serif);margin:0 0 .4rem}
a{color:var(--accent)}</style></head>
<body><main><h1>Could not sign you in</h1><p>%s</p><p><a href="/">Back to sign in</a></p></main></body></html>
""" % (cliauth.PAGE_TOKENS, html.escape(sentence))).encode()
    return (status, body, (("Set-Cookie", _state_cookie("", 0)), ("Cache-Control", "no-store"),
                           ("Referrer-Policy", "no-referrer")) + tuple(headers))
