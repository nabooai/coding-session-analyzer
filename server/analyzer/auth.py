"""Fail-closed auth gate: the one entry point both servers call (apiserver.py and mcp_server.py --http).

Every authenticated request resolves to a principal {email, org_id, role, service}. `org_id` comes only
from the credential, and no credential names more than one workspace. The shared secret
(CSA_AUTH_TOKEN) is the team's credential on a self-host box and is refused once the deployment hosts
tenants.

Modes (`CSA_AUTH`): `off` is loopback-only dev, `org` the authenticated mode; anything else refuses to
boot. `identify()` never raises for a bad or absent credential. Logs to stderr only (MCP stdout is the
JSON-RPC channel).
"""
import base64, hmac, json, logging, os, time, urllib.request
from dataclasses import dataclass, fields

from sqlalchemy import select

from analyzer import db, orgs, store
from analyzer.tenant import Sso

_SSO = Sso.__table__

log = logging.getLogger("csa.auth")

VALID_MODES = {"off", "org"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}           # "" binds 0.0.0.0, so it is not loopback
OPEN_PATHS = {"/api/health"}                            # exact-match allowlist: anything else is denied
_CLAIM_LEEWAY = 60                                       # seconds of clock skew tolerated on exp/nbf/iat

# The dashboard's session cookie, holding a per-user token. `__Host-` makes the browser refuse a
# `Domain` attribute, so a sibling subdomain cannot plant or shadow it (session fixation).
SESSION_COOKIE = "__Host-csa_session"

SHARED_SECRET_RETIRED = ("this deployment hosts tenants, so the shared secret is not a credential any "
                         "more; run `csa-client setup` for a per-user token")


def current_mode() -> str:
    return os.environ.get("CSA_AUTH", "off").strip().lower()


def is_loopback(bind: str) -> bool:
    return (bind or "").strip().lower() in _LOOPBACK


def boot_error(bind: str) -> str | None:
    """A one-line reason to refuse boot, else None; each server calls it before binding its socket."""
    mode = current_mode()
    if mode not in VALID_MODES:
        return (f"refusing to start: CSA_AUTH={os.environ.get('CSA_AUTH')!r} is not one of "
                f"{sorted(VALID_MODES)}.")
    if mode == "off" and not is_loopback(bind):
        return (f"refusing to start: bind={bind} exposes the corpus but CSA_AUTH=off. "
                f"Set CSA_AUTH=org (per-user tokens; CSA_AUTH_TOKEN is the team's shared secret).")
    if mode == "org":
        try:
            con = orgs.control_db()
            unrouted = sso_config(con) is not None and not orgs.sso_allowlist_configured(con)
        except Exception:                                # an unreadable control plane is /api/health's to report
            unrouted = False
        if unrouted:
            return ("refusing to start: sign-in is configured (`csa.py sso show`) but no org claims a "
                    "domain, so every sign-in would be denied. Run `csa.py org domain <org> <domain>` "
                    "(or `csa.py org adopt-default <name> --domain <domain>`) to say who may sign in.")
    return None


def _h(headers, name, default=""):
    """Case-insensitive header read over http.server's Message, Starlette Headers, or our ASGI shim."""
    try:
        v = headers.get(name)
    except Exception:
        v = None
    return v if v else default


def _principal(email, org_id, role="member", service=False):
    return {"email": email, "org_id": store.valid_org_id(org_id), "role": role, "service": bool(service)}


def workspace_admin(principal):
    """A named admin of the workspace: the admin role on a per-user token. Loopback dev and the
    self-host shared secret carry the role with no email and are not one."""
    return principal.get("role") == "admin" and bool(principal.get("email"))


def _user_token(cands):
    """The first candidate the token store recognises decides, a revoked or expired one included: a
    dead token must deny outright, never fall through to the shared secret."""
    for c in cands:
        status, p = orgs.lookup(c)
        if status == "ok":
            return (True, _principal(p["email"], p["org_id"], role=p["role"], service=p.get("service")), None)
        if status == "revoked":
            return (False, None, "token revoked")
        if status == "expired":
            return (False, None, "token expired — run `csa-client setup` to sign in again")
    return None


def _shared_secret_ok(cands):
    secret = os.environ.get("CSA_AUTH_TOKEN", "")
    if not secret:
        return False
    sb = secret.encode("utf-8")
    # bytes: compare_digest raises on a non-ASCII str, and headers decode as latin-1
    return any(hmac.compare_digest(c.encode("utf-8"), sb) for c in cands)


def identify(headers, allow_cookie=False):
    """(ok, principal, err). ok=False ⇒ 401 with `err` as a short human reason. Never raises for a bad
    credential.

    Precedence is header first, cookie last: a per-user token in the header, then the shared secret in
    the header, then (only with `allow_cookie`, the browser dashboard) the session cookie as a per-user
    token. A cookie can never authenticate as the shared secret, so the MCP gate and every non-browser
    caller keep the default."""
    try:
        mode = current_mode()
        if mode == "off":                                # boot invariant guarantees this is loopback-only dev
            return (True, _principal(None, store.DEFAULT_ORG, role="admin"), None)
        if mode == "org":
            bearer = _bearer_candidates(headers)
            cookie = _session_cookie_candidates(headers) if allow_cookie else []
            if not bearer and not cookie:
                return (False, None, "missing bearer token")
            hit = _user_token(bearer)
            if hit is not None:
                return hit
            if _shared_secret_ok(bearer):
                if orgs.is_multi_tenant():               # fail-closed: True when the control plane is unreadable
                    return (False, None, SHARED_SECRET_RETIRED)
                return (True, _principal(None, store.DEFAULT_ORG, role="admin"), None)
            hit = _user_token(cookie)
            if hit is not None:
                return hit
            return (False, None, "invalid or expired token")
    except Exception as e:                               # a thrown exception must read as deny, not a 500 leak
        log.warning("identify raised: %s", str(e)[:120])
        return (False, None, "authentication failed")
    return (False, None, "auth misconfigured")           # unreachable when boot_error() gated startup


def _bearer_candidates(headers):
    """Presented bearer secrets: `Authorization: Bearer <t>` (primary) and `X-CSA-Token: <t>` (proxy
    fallback). Either present-and-correct authenticates."""
    out = []
    auth = _h(headers, "Authorization")
    if auth[:7].lower() == "bearer ":
        tok = auth[7:].strip()
        if tok:
            out.append(tok)
    xt = _h(headers, "X-CSA-Token").strip()
    if xt:
        out.append(xt)
    return out


def _session_cookie_candidates(headers):
    """The browser session cookie as a candidate, or []. Apart from `_bearer_candidates` so a cookie
    only ever authenticates as a per-user token (which carries a verified email), never as the shared
    secret. Opt-in (`allow_cookie`): the MCP gate does not, since cookies are port-blind."""
    tok = _cookie(headers, SESSION_COOKIE)
    return [tok] if tok else []


# --- JWT verification ---------------------------------------------------------------------------------

_jwks = {}                                               # jwks_url -> {"keys": [...], "at": ts}


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:              # noqa: S310 (the operator-set issuer)
        return json.loads(r.read())


JWKS_TTL = 3600           # seconds the JWKS and discovery caches live; both re-derivable from the issuer


def _get_jwks(url, force=False):
    """Cached per issuer url."""
    now = time.time()
    ent = _jwks.get(url)
    if force or ent is None or (now - ent["at"]) > JWKS_TTL:
        _jwks[url] = ent = {"keys": _fetch_json(url).get("keys", []), "at": now}   # a raising fetch leaves the old entry alone
    return ent["keys"] or []


def _verify_jwt(tok, jwks_url):
    """(payload, err): proves the token was signed by a key the issuer publishes, nothing more. The
    claims (iss, aud) are the caller's job. The payload is decoded only after the signature verifies."""
    parts = (tok or "").split(".")
    if len(parts) != 3:
        return (None, "malformed jwt")
    try:
        header = json.loads(_b64d(parts[0]))
    except Exception:
        return (None, "malformed jwt")
    alg = header.get("alg")
    if alg not in ("RS256", "ES256"):                    # REJECT alg=none, HS*, and any attacker-chosen alg
        return (None, f"unsupported jwt alg {alg!r}")
    jwk = _find_key(header.get("kid"), _get_jwks(jwks_url))
    if jwk is None:                                      # unknown kid ⇒ refetch once (key rotation), then fail
        jwk = _find_key(header.get("kid"), _get_jwks(jwks_url, force=True))
    if jwk is None:
        return (None, "unknown jwt signing key")
    if not _verify_sig(jwk, alg, (parts[0] + "." + parts[1]).encode(), _b64d(parts[2])):
        return (None, "jwt signature invalid")
    return (json.loads(_b64d(parts[1])), None)           # trusted ONLY after the signature verified


def _find_key(kid, keys):
    for k in keys:
        if k.get("kid") == kid:
            return k
    return None


def _cookie(headers, name):
    for part in _h(headers, "Cookie").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v.strip()
    return ""


def _verify_sig(jwk, alg, signing_input, sig) -> bool:
    """Verify with `cryptography`, building the public key from the JWK. Any failure ⇒ False (never raise)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, ec, utils
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers, SECP256R1
    bi = lambda b: int.from_bytes(_b64d(b), "big")       # b64url-encoded big-endian int
    try:
        if alg == "RS256":
            pub = RSAPublicNumbers(bi(jwk["e"]), bi(jwk["n"])).public_key()
            pub.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
        else:                                            # ES256: JWS sig is raw r||s; cryptography wants DER
            pub = EllipticCurvePublicNumbers(bi(jwk["x"]), bi(jwk["y"]), SECP256R1()).public_key()
            half = len(sig) // 2
            der = utils.encode_dss_signature(int.from_bytes(sig[:half], "big"),
                                             int.from_bytes(sig[half:], "big"))
            pub.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _check_time(payload):
    """exp/nbf/iat, with `exp` REQUIRED: a missing exp would accept a non-expiring assertion."""
    now = time.time()
    exp, nbf, iat = payload.get("exp"), payload.get("nbf"), payload.get("iat")
    if exp is None:
        return "jwt missing exp"
    if now > float(exp) + _CLAIM_LEEWAY:
        return "jwt expired"
    if nbf is not None and float(nbf) - _CLAIM_LEEWAY > now:
        return "jwt not yet valid"
    if iat is not None and float(iat) - _CLAIM_LEEWAY > now:
        return "jwt issued in the future"
    return None


# --- the OpenID issuer: config, discovery and the id_token gate ---------------------------------------
# An id_token proves who you are once, in a browser (analyzer/sso.py); cliauth exchanges it for the same
# per-user `csa_` token every other path issues.

GOOGLE_ISSUER = "https://accounts.google.com"            # the one issuer whose domain claim is `hd`, not the address
NO_EMAIL = "That account exposes no email address to CSA, so it cannot be signed in."
EMAIL_UNVERIFIED = "That account's email address is not verified by the identity provider."


@dataclass(frozen=True)
class SsoConfig:
    issuer: str
    client_id: str
    client_secret: str | None
    display_name: str


@dataclass(frozen=True)
class Discovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def sso_config(con) -> SsoConfig | None:
    """The configured issuer, or None when sign-in is by invite. Read once per request on the
    connection the caller holds: `csa.py sso set` on the box takes effect on the next request."""
    stmt = select(*(_SSO.c[f.name] for f in fields(SsoConfig))).where(_SSO.c.id == 1)
    row = db.run(con, stmt).fetchone()
    return SsoConfig(**dict(zip(stmt.selected_columns.keys(), row))) if row else None


def sso_required(con) -> bool:
    """An sso row closes the shared-invite mint path outright: a gate you can walk around is not one."""
    return sso_config(con) is not None


_discovery = {}                                          # issuer -> (Discovery, fetched-at)


def discovery(issuer, force=False) -> Discovery:
    """The issuer's endpoints from `<issuer>/.well-known/openid-configuration`, cached like the JWKS.
    The document must name the issuer byte for byte (a trailing slash included: Auth0 names its with
    one), so a redirect or a proxy cannot swap the endpoints. The issuer is operator-set (csa.py sso
    set), so a hostile URL here is an operator action, not an SSRF."""
    now = time.time()
    ent = _discovery.get(issuer)
    if force or ent is None or (now - ent[1]) > JWKS_TTL:
        doc = _fetch_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if doc.get("issuer") != issuer:
            raise ValueError(f"discovery at {issuer} names issuer {doc.get('issuer')!r}")
        _discovery[issuer] = ent = (Discovery(doc["authorization_endpoint"], doc["token_endpoint"],
                                              doc["jwks_uri"]), now)
    return ent[0]


def verify_id_token(tok, cfg: SsoConfig, nonce):
    """An id_token -> (email, domain_claim, err). `email` is set only when the signature, iss, aud,
    nonce, time and email_verified all pass. The claim is `hd` for Google (None for a consumer
    account) and the address's own domain for every other issuer, whose tenant is the trust boundary:
    only its users can mint an id_token with our aud. Never raises."""
    try:
        payload, err = _verify_jwt(tok, discovery(cfg.issuer).jwks_uri)
        if err:
            return (None, None, err)
        if payload.get("iss") not in (cfg.issuer, cfg.issuer.split("://", 1)[1]):
            return (None, None, f"id_token issuer {payload.get('iss')!r} is not {cfg.issuer}")
        aud = payload.get("aud")
        if aud != cfg.client_id and not (isinstance(aud, list) and cfg.client_id in aud):
            return (None, None, f"id_token audience {aud!r} is not this client")
        if payload.get("nonce") != nonce:
            return (None, None, "id_token nonce does not match the sign-in that started it")
        err = _check_time(payload)
        if err:
            return (None, None, err)
        email = (payload.get("email") or "").strip().lower()
        if "@" not in email:
            return (None, None, NO_EMAIL)
        if payload.get("email_verified") is False or payload.get("email_verified") == "false":
            return (None, None, EMAIL_UNVERIFIED)
        claim = (payload.get("hd") or "") if cfg.issuer == GOOGLE_ISSUER else email.rsplit("@", 1)[1]
        return (email, claim.strip().lower() or None, None)
    except Exception as e:                               # a thrown exception must read as deny, never a 500
        log.warning("verify_id_token raised: %s", str(e)[:120])
        return (None, None, "id_token verification failed")


class _CIHeaders:
    """Case-insensitive header mapping (mimics http.server / Starlette headers). The MCP gate builds one
    from the ASGI scope; the tests build them by hand."""
    def __init__(self, d=None):
        self._d = {k.lower(): v for k, v in (d or {}).items()}
    def get(self, name, default=None):
        return self._d.get(name.lower(), default)
