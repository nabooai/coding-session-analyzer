"""The auth gate: token compare, precedence, cookie containment, the JWT gate, the issuer config and
the boot invariant. Ported from auth's former `__main__` self-check; the id_token claim rules and
the round trip are tests/test_sso.py."""
import base64
import json
import time

import pytest

from analyzer import auth, orgs

_ENV = ("CSA_AUTH", "CSA_AUTH_TOKEN")
H = auth._CIHeaders
C = auth.SESSION_COOKIE


def setenv(monkeypatch, **kw):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def con(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    yield c
    c.close()


def _jwt(alg):
    h = base64.urlsafe_b64encode(json.dumps({"alg": alg, "kid": "k"}).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps({"email": "a@x"}).encode()).rstrip(b"=").decode()
    return f"{h}.{p}.AAAA"


# --- modes and the shared secret ------------------------------------------------------------------

def test_off_mode_allows_with_no_identity(monkeypatch):
    setenv(monkeypatch, CSA_AUTH="off")
    ok, principal, err = auth.identify(H())
    assert ok and principal["email"] is None and err is None


def test_shared_secret_missing_wrong_right(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s3cret")
    assert auth.identify(H())[0] is False, "missing token must deny"
    assert auth.identify(H({"X-CSA-Token": ""}))[0] is False, "empty token must deny"
    assert auth.identify(H({"Authorization": "Bearer wrong"}))[0] is False
    ok, principal, err = auth.identify(H({"Authorization": "Bearer s3cret"}))
    assert ok and principal["email"] is None and err is None
    assert auth.identify(H({"authorization": "bearer s3cret"}))[0] is True, "case-insensitive scheme"
    assert auth.identify(H({"X-CSA-Token": "s3cret"}))[0] is True, "fallback header allows"
    assert auth.identify(H({"Authorization": "Bearer wrong", "X-CSA-Token": "s3cret"}))[0] is True, \
        "either present-and-correct authenticates"


def test_non_ascii_bearer_denies_never_raises(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s3cret")
    assert auth.identify(H({"Authorization": "Bearer ÿtok"}))[0] is False
    assert auth.identify(H({"X-CSA-Token": "ÿ"}))[0] is False


def test_no_shared_secret_is_a_valid_stricter_configuration(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org")
    assert auth.boot_error("0.0.0.0") is None
    assert auth.identify(H({"Authorization": "Bearer anything"}))[0] is False


@pytest.mark.parametrize("mode", ["token", "cloudflare"])
def test_former_modes_are_not_modes(monkeypatch, con, mode):
    setenv(monkeypatch, CSA_AUTH=mode, CSA_AUTH_TOKEN="x")
    err = auth.boot_error("127.0.0.1")
    assert err and "not one of" in err and "'org'" in err
    assert auth.identify(H({"Authorization": "Bearer x"}))[0] is False, \
        "the gate denies too, should boot_error ever be bypassed"


# --- the session cookie ----------------------------------------------------------------------------

def test_session_cookie_is_read_but_never_a_bearer(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s3cret")
    assert auth._session_cookie_candidates(H()) == []
    assert auth._session_cookie_candidates(H({"Cookie": ""})) == []
    assert auth._session_cookie_candidates(H({"Cookie": C + "=csa_abc"})) == ["csa_abc"]
    assert auth._session_cookie_candidates(H({"Cookie": "a=1; " + C + "=csa_abc; b=2"})) == ["csa_abc"]
    assert auth._session_cookie_candidates(H({"Cookie": "csa_session=csa_abc"})) == [], \
        "the unprefixed name is not our cookie"
    assert auth._bearer_candidates(H({"Cookie": C + "=s3cret"})) == [], "a cookie is never a bearer candidate"
    assert auth.identify(H({"Cookie": C + "=s3cret"}))[0] is False, \
        "the shared secret presented in a cookie must not authorize"


def test_headers_outrank_the_ambient_cookie(monkeypatch, con):
    """A self-host box (no org rows), where the shared secret is still a credential."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s3cret")
    utok = orgs.mint_token("sam@self.host", "default", con=con)
    both = H({"Authorization": "Bearer s3cret", "Cookie": C + "=" + utok})
    ok, p, _ = auth.identify(both, allow_cookie=True)
    assert ok and p["email"] is None and p["role"] == "admin", \
        "a deliberately presented shared secret must outrank an ambient session cookie"
    ok, p, _ = auth.identify(H({"Cookie": C + "=" + utok}), allow_cookie=True)
    assert ok and p["email"] == "sam@self.host"
    ok, p, _ = auth.identify(H({"Authorization": "Bearer " + utok, "Cookie": C + "=" + utok}),
                             allow_cookie=True)
    assert ok and p["email"] == "sam@self.host", "a per-user token in the header still outranks the shared secret"
    junk = H({"Authorization": "Bearer wrong", "Cookie": C + "=" + utok})
    ok, p, _ = auth.identify(junk, allow_cookie=True)
    assert ok and p["email"] == "sam@self.host", "a junk bearer beside a valid cookie still authenticates off the cookie"
    assert auth.identify(junk)[0] is False, "...and header-only it fails"
    assert auth.identify(H({"Cookie": C + "=" + utok}))[0] is False, "allow_cookie defaults False"


def test_the_shared_secret_is_refused_once_the_box_hosts_tenants(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s3cret")
    orgs._create_org("Acme", "acme", con=con)
    ok, p, err = auth.identify(H({"Authorization": "Bearer s3cret"}))
    assert ok is False and p is None and "csa-client setup" in err


# --- the JWT gate (no signing key, no network) -----------------------------------------------------

_CFG = auth.SsoConfig("https://idp.example", "cid", None, "IdP")


def test_attacker_chosen_alg_never_verifies(monkeypatch):
    """The alg check runs before any key is fetched: nothing here reaches the network."""
    monkeypatch.setattr(auth, "discovery", lambda issuer, force=False: auth.Discovery("a", "t", "http://127.0.0.1:1/jwks"))
    assert auth.verify_id_token(_jwt("none"), _CFG, "n") == (None, None, "unsupported jwt alg 'none'")
    assert auth.verify_id_token(_jwt("HS256"), _CFG, "n")[0] is None
    assert auth.verify_id_token("not.a.jwt.at.all", _CFG, "n") == (None, None, "malformed jwt")
    assert auth.verify_id_token("", _CFG, "n")[0] is None and auth.verify_id_token(None, _CFG, "n")[0] is None


def test_check_time_requires_exp():
    future = time.time() + 3600
    assert auth._check_time({"exp": future}) is None
    assert auth._check_time({"exp": time.time() - 3600}) == "jwt expired"
    assert auth._check_time({}) == "jwt missing exp"
    assert auth._check_time({"exp": future, "nbf": future}) == "jwt not yet valid"
    assert auth._check_time({"exp": future, "iat": future}) == "jwt issued in the future"


def test_the_issuer_is_read_from_control_db_on_every_call(monkeypatch, con):
    """No module cache: `csa.py sso set` on the box takes effect on the next request."""
    assert auth.sso_config(con) is None and auth.sso_required(con) is False
    orgs.set_sso("https://idp.example", "cid", None, "IdP", con=con)
    assert auth.sso_config(con) == _CFG and auth.sso_required(con) is True, "configuring an issuer closes the invite path"
    orgs.set_sso("https://idp.example", "cid2", "sec", "IdP 2", con=con)
    assert auth.sso_config(con) == auth.SsoConfig("https://idp.example", "cid2", "sec", "IdP 2"), "replaced whole"
    assert orgs.clear_sso(con=con) is True and orgs.clear_sso(con=con) is False
    assert auth.sso_config(con) is None


# --- the boot invariant ----------------------------------------------------------------------------

def test_off_must_be_loopback(monkeypatch):
    setenv(monkeypatch, CSA_AUTH="off")
    assert auth.boot_error("127.0.0.1") is None
    assert auth.boot_error("localhost") is None
    assert auth.boot_error("0.0.0.0"), "non-loopback + off must refuse"
    assert auth.boot_error("10.0.0.5")
    assert auth.boot_error(""), "empty bind (== INADDR_ANY) + off must refuse"
    assert auth.is_loopback("") is False


def test_org_boots_on_any_bind_and_unknown_modes_refuse(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="x")
    assert auth.boot_error("0.0.0.0") is None
    setenv(monkeypatch, CSA_AUTH="bogus")
    assert auth.boot_error("127.0.0.1"), "unknown mode must refuse even on loopback"


def test_sso_configured_with_nobody_allowed_refuses(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="x")
    orgs.set_sso("https://idp.example", "cid", None, "IdP", con=con)
    err = auth.boot_error("0.0.0.0")
    assert err and "csa.py org domain" in err, "an issuer with no org domain would deny every sign-in"
    orgs._create_org("Acme", "acme", domain="acme.com", con=con)
    assert auth.boot_error("0.0.0.0") is None
    setenv(monkeypatch, CSA_AUTH="off")
    orgs.set_domain("acme", "", con=con)
    assert auth.boot_error("127.0.0.1") is None, "loopback dev never signs in: the invariant is org mode's"


def test_open_paths_is_exactly_health():
    assert "/api/health" in auth.OPEN_PATHS
    assert "/api/facts" not in auth.OPEN_PATHS and "/api/pipeline" not in auth.OPEN_PATHS

