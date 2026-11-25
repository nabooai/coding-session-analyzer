"""Sign-in with an OpenID issuer: the browser's round trip over real HTTP against an in-process
issuer, every way the callback must refuse, the claim rules per issuer, and what an sso row closes.

The issuer stub (tests/oidc_stub.py) is Okta-shaped: no `hd`, so the address's domain is the claim.
The Google shape (`hd` decides) is pinned separately by signing tokens as accounts.google.com.
"""
import argparse
import html
import http.client
import io
import json
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

import apiserver
import csa
from analyzer import auth, cliauth, orgs, sso, store
from tests.oidc_stub import OidcStub


@pytest.fixture(scope="module")
def idp():
    stub = OidcStub()
    yield stub
    stub.close()


class Box:
    """The api over real HTTP, so cookies and redirects are what a browser would see."""

    def __init__(self, base, idp):
        self.base, self.idp = base, idp

    def get(self, path, cookie=""):
        u = urllib.parse.urlsplit(self.base)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        r = c.getresponse()
        out = (r.status, r.getheaders(), r.read())
        c.close()
        return out

    def post(self, path, body, cookie="", site="same-origin"):
        u = urllib.parse.urlsplit(self.base)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        h = {"Content-Type": "application/json", "Sec-Fetch-Site": site}
        if cookie:
            h["Cookie"] = cookie
        c.request("POST", path, body=json.dumps(body), headers=h)
        r = c.getresponse()
        out = (r.status, r.getheaders(), json.loads(r.read() or b"{}"))
        c.close()
        return out

    def start(self, return_path="/", cookie=""):
        """GET /api/sso/start -> (the issuer's authorize URL, the state cookie to send back)."""
        st, hdrs, body = self.get("/api/sso/start?" + urllib.parse.urlencode({"return": return_path}), cookie)
        assert st == 302, (st, body)
        state_cookie = [v for k, v in hdrs if k == "Set-Cookie" and v.startswith(sso.STATE_COOKIE)][0]
        return dict(hdrs)["Location"], state_cookie.split(";")[0]

    def authorize(self, location):
        """The issuer's hop: it answers with the callback URL and a fresh code."""
        u = urllib.parse.urlsplit(location)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("GET", u.path + "?" + u.query)
        r = c.getresponse()
        assert r.status == 302, r.status
        back = r.getheader("Location")
        c.close()
        assert back.startswith(self.base + "/api/sso/callback?"), back
        return back

    def callback(self, back, cookie):
        u = urllib.parse.urlsplit(back)
        return self.get(u.path + "?" + u.query, cookie)

    def sign_in(self, return_path="/", cookie=""):
        location, state_cookie = self.start(return_path, cookie)
        return self.callback(self.authorize(location), state_cookie)


def _session(hdrs):
    return [v.split(";")[0] for k, v in hdrs if k == "Set-Cookie" and v.startswith(auth.SESSION_COOKIE)]


@pytest.fixture()
def box(tmp_path, monkeypatch, idp):
    """org mode, `default` adopted as Acme with domain acme.test, the stub as its issuer, over HTTP."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    idp.reset()
    auth._discovery.clear()
    auth._jwks.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    monkeypatch.setenv("CSA_PUBLIC_URL", base)
    orgs.adopt_default_org("Acme", domain="acme.test")
    orgs.set_sso(idp.issuer, "csa-client", None, "Acme Okta", updated_by="test")
    yield Box(base, idp)
    srv.shutdown()
    srv.server_close()


# --- the round trip ---------------------------------------------------------------------------------

def test_the_round_trip_signs_in_and_the_page_hops_home(box, idp):
    location, state_cookie = box.start("/cli-auth?nonce=abc&hostname=mac")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert location.startswith(idp.issuer + "/authorize?")
    assert q["response_type"] == ["code"] and q["client_id"] == ["csa-client"]
    assert q["scope"] == [sso.SCOPE], "no groups scope without okta.adminGroup" and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == [box.base + "/api/sso/callback"], "built from CSA_PUBLIC_URL"
    assert "SameSite=Lax" in dict(box.get("/api/sso/start?return=/")[1])["Set-Cookie"]
    assert state_cookie == sso.STATE_COOKIE + "=" + orgs._hash(q["state"][0]), "the cookie holds the hash"

    st, hdrs, body = box.callback(box.authorize(location), state_cookie)
    assert st == 200, body
    assert b'location.replace("/cli-auth?nonce=abc\\u0026hostname=mac")' in body, body
    assert b'content="0;url=/cli-auth?nonce=abc&amp;hostname=mac"' in body, "the no-script hop"
    h = dict(hdrs)
    assert h["Cache-Control"] == "no-store" and h["Referrer-Policy"] == "no-referrer"
    cookies = [v for k, v in hdrs if k == "Set-Cookie"]
    session = [c for c in cookies if c.startswith(auth.SESSION_COOKIE)]
    assert session and "SameSite=Strict" in session[0] and "HttpOnly" in session[0], cookies
    assert any(c.startswith(sso.STATE_COOKIE + "=;") and "Max-Age=0" in c for c in cookies), "state cookie spent"
    assert any(c.startswith(cliauth.JOIN_COOKIE + "=;") for c in cookies), "join cookie spent"
    assert "code_verifier" in idp.token_requests[-1] and "client_secret" not in idp.token_requests[-1]

    st, _, me = box.get("/api/me", cookie=session[0].split(";")[0])
    me = json.loads(me)
    assert st == 200 and me["email"] == "alice@acme.test" and me["org_id"] == store.DEFAULT_ORG, me
    assert me["hostname"] == "web-sso" and me["verified"] is True


def test_the_positive_needs_only_the_org_domain(box, idp):
    """adopt-default with a domain plus `sso set` with no other flag admits the domain's people."""
    idp.email = "bob@acme.test"
    st, hdrs, _ = box.sign_in()
    assert st == 200 and _session(hdrs)


def test_a_verified_address_outside_the_org_domain_is_refused(box, idp):
    idp.email = "mallory@evil.test"
    st, hdrs, body = box.sign_in()
    assert st == 403 and not _session(hdrs)
    assert b"mallory@evil.test isn&#x27;t allowed to sign in" in body, body
    assert b"ask your admin for an invite link" in body, body


def test_a_stored_secret_is_posted_to_the_token_endpoint(box, idp):
    orgs.set_sso(idp.issuer, "csa-client", "s3cret", "Acme Okta")
    st, hdrs, _ = box.sign_in()
    assert st == 200 and _session(hdrs)
    assert idp.token_requests[-1]["client_secret"] == "s3cret"
    assert idp.token_requests[-1]["code_verifier"], "PKCE stays on with a secret"


# --- every way the callback refuses -----------------------------------------------------------------

def test_a_replayed_state_finds_no_row(box):
    location, cookie = box.start()
    back = box.authorize(location)
    assert box.callback(back, cookie)[0] == 200
    st, hdrs, body = box.callback(back, cookie)
    assert st == 400 and sso.ALREADY_USED.encode() in body and not _session(hdrs)


def test_an_expired_state_is_refused(box):
    location, cookie = box.start()
    con = orgs.control_db()
    con.execute("UPDATE sso_logins SET created=?", (orgs._now(-sso.TTL_SECONDS - 1),))   # fixture: age the row
    con.commit()
    st, hdrs, body = box.callback(box.authorize(location), cookie)
    assert st == 400 and sso.ALREADY_USED.encode() in body and not _session(hdrs)


def test_a_callback_without_the_browser_cookie_is_refused(box):
    location, cookie = box.start()
    back = box.authorize(location)
    for wrong in ("", sso.STATE_COOKIE + "=deadbeef", sso.STATE_COOKIE + "=" + orgs._hash("other")):
        st, hdrs, body = box.callback(back, wrong)
        assert st == 400 and sso.NOT_STARTED_HERE.encode() in body and not _session(hdrs), wrong
    assert box.callback(back, cookie)[0] == 200, "the refusals consumed nothing"


@pytest.mark.parametrize("field,value,reason", [
    ("aud", "someone-elses-client", "audience"),
    ("iss", "https://evil.example", "issuer"),
    ("nonce", "not-the-one-we-sent", "nonce"),
])
def test_operator_side_failures_show_one_sentence_and_log_the_reason(box, idp, caplog, field, value, reason):
    setattr(idp, field, value)
    st, hdrs, body = box.sign_in()
    assert st == 502 and sso.GENERIC.encode() in body and not _session(hdrs)
    assert value.encode() not in body, "the page never says which check failed"
    assert any(reason in r.getMessage() for r in caplog.records if r.name == "csa.sso"), caplog.text


def test_an_unreachable_token_endpoint_is_operator_side(box, idp, caplog):
    idp.token_status = 500
    st, hdrs, body = box.sign_in()
    assert st == 502 and sso.GENERIC.encode() in body and not _session(hdrs)
    assert any("code exchange" in r.getMessage() for r in caplog.records), caplog.text


def test_email_verified_false_is_refused_and_absent_is_admitted(box, idp):
    idp.email_verified = False
    st, hdrs, body = box.sign_in()
    assert st == 403 and html.escape(auth.EMAIL_UNVERIFIED).encode() in body and not _session(hdrs)
    idp.email_verified = None                             # Entra sends no email_verified at all
    st, hdrs, _ = box.sign_in()
    assert st == 200 and _session(hdrs)


def test_an_id_token_without_an_email_is_refused(box, idp):
    idp.email = None
    st, hdrs, body = box.sign_in()
    assert st == 403 and auth.NO_EMAIL.encode() in body and not _session(hdrs)


def test_the_issuer_refusing_is_shown_to_the_person(box):
    location, cookie = box.start()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["state"][0]
    st, hdrs, body = box.get("/api/sso/callback?" + urllib.parse.urlencode(
        {"state": state, "error": "access_denied"}), cookie)
    assert st == 400 and b"refused the sign-in (access_denied)" in body and not _session(hdrs)


def test_return_off_origin_is_refused_before_a_row_exists(box):
    for bad in ("//evil.example/", "/\\evil.example", "https://evil.example/", "relative", "/x\r\nSet-Cookie: a=b"):
        st, hdrs, body = box.get("/api/sso/start?" + urllib.parse.urlencode({"return": bad}))
        assert st == 400 and b"not on this server" in body, bad
        assert not any(k == "Set-Cookie" and v.startswith(sso.STATE_COOKIE + "=") and "Max-Age=0" not in v
                       for k, v in hdrs), bad
    assert orgs.control_db().execute("SELECT COUNT(*) FROM sso_logins").fetchone()[0] == 0


def test_a_hostile_return_cannot_break_out_of_the_interstitial(box):
    st, _, body = box.sign_in("/x</script><script>alert(1)</script>")
    assert st == 200 and b"<script>alert(1)" not in body
    assert b"\\u003c/script\\u003e" in body


def test_the_routes_are_public_and_the_callback_is_a_get(box):
    assert apiserver._public("/api/sso/start") and apiserver._public("/api/sso/callback")
    assert "/api/sso/callback" not in apiserver._COOKIE_POST_OK
    assert box.post("/api/sso/callback", {})[0] == 404


def test_start_answers_404_without_an_issuer(box):
    orgs.clear_sso()
    st, _, body = box.get("/api/sso/start?return=/")
    assert st == 404 and b"not configured" in body


# --- what an sso row closes -------------------------------------------------------------------------

def test_the_typed_paths_are_refused_once_an_issuer_exists(box):
    invite = orgs.rotate_invite(store.DEFAULT_ORG)
    nonce, code, _ = cliauth.new("laptop")
    st, _, body = box.post("/api/cli-auth/approve", {"nonce": nonce, "code": code,
                                                     "email": "dev@acme.test", "invite": invite})
    assert st == 200 and body["ok"] is False and "requires signing in with Acme Okta" in body["error"], body
    assert cliauth.claim(nonce) is None
    st, hdrs, body = box.post("/api/ui-login", {"email": "dev@acme.test", "invite": invite})
    assert st == 401 and not _session(hdrs) and "requires signing in with Acme Okta" in body["error"], body

    _id, link = orgs.create_invite(store.DEFAULT_ORG, "dave@partner.io", "boss@acme.test")
    st, hdrs, body = box.post("/api/ui-login", {"email": "", "invite": ""},
                              cookie="%s=%s" % (cliauth.JOIN_COOKIE, link))
    assert st == 401 and not _session(hdrs), "a /join link alone is not a credential any more"


def test_a_join_link_rides_the_round_trip_and_admits_the_named_person(box, idp):
    """The join cookie is Strict, so it is read at start (same-site) and carried in the row."""
    _id, link = orgs.create_invite(store.DEFAULT_ORG, "dave@partner.io", "boss@acme.test")
    idp.email = "dave@partner.io"                         # partner.io: no org claims it
    assert box.sign_in()[0] == 403, "without the link the domain gate refuses him"
    st, hdrs, _ = box.sign_in(cookie="%s=%s" % (cliauth.JOIN_COOKIE, link))
    assert st == 200 and _session(hdrs)
    assert any(v.startswith(cliauth.JOIN_COOKIE + "=;") for k, v in hdrs if k == "Set-Cookie"), "spent"
    idp.email = "mallory@evil.test"
    st, hdrs, body = box.sign_in(cookie="%s=%s" % (cliauth.JOIN_COOKIE, link))
    assert st == 403 and b"not usable for mallory@evil.test" in body and not _session(hdrs)
    assert any(v.startswith(cliauth.JOIN_COOKIE + "=;") for k, v in hdrs if k == "Set-Cookie"), \
        "the refusal spends the link, so the next attempt takes the domain rule"
    idp.email = "alice@acme.test"                         # domain-valid, carrying dave's link
    st, hdrs, _ = box.sign_in(cookie="%s=%s" % (cliauth.JOIN_COOKIE, link))
    assert st == 200 and _session(hdrs), "a link that does not fit does not lock out the domain rule"
    con = orgs.control_db()
    assert con.execute("SELECT COUNT(*) FROM sso_logins WHERE join_token = ?", (link,)).fetchone()[0] == 0
    row = con.execute("SELECT join_token FROM sso_logins").fetchone()
    assert row is None or row[0] != link, "the row keeps the link's hash, never the link"


def test_the_page_and_the_config_offer_the_issuer(box):
    st, _, cfg = box.get("/api/auth-config")
    cfg = json.loads(cfg)
    assert cfg["sso"] == {"name": "Acme Okta", "start_url": "/api/sso/start"} and cfg["sso_required"] is True
    assert "google_client_id" not in cfg and "google_required" not in cfg

    nonce, _code, _ = cliauth.new("mac")
    st, _, page = box.get("/cli-auth?nonce=%s&hostname=mac" % nonce)
    assert st == 200
    assert b"Sign in with Acme Okta" in page and b"accounts.google.com" not in page
    assert b'href="/api/sso/start?return=%2Fcli-auth%3Fnonce%3D' in page and b"%26hostname%3Dmac" in page
    assert b'name="invite"' not in page, "no second way in to walk around"

    orgs.clear_sso()
    cfg = json.loads(box.get("/api/auth-config")[2])
    assert cfg["sso"] is None and cfg["sso_required"] is False


def test_a_signed_in_browser_pairs_a_machine_with_its_role(box, idp):
    """The acceptance: the minted CLI token's role equals the person's existing role."""
    st, hdrs, _ = box.sign_in()
    session = _session(hdrs)[0]
    assert orgs.set_role("alice@acme.test", store.DEFAULT_ORG, "admin") == 1
    nonce, code, _ = cliauth.new("alice-mac")
    st, _, body = box.post("/api/cli-auth/approve", {"nonce": nonce, "code": code}, cookie=session)
    assert (st, body) == (200, {"ok": True}), body
    creds = cliauth.claim(nonce)
    assert creds["email"] == "alice@acme.test"
    assert orgs.lookup(creds["token"])[1]["role"] == "admin", "verified pairing inherits the role"


# --- the boot invariant -----------------------------------------------------------------------------

def test_boot_refuses_an_issuer_nobody_can_reach(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    orgs.set_sso("https://idp.example", "cid", None, "IdP", con=c)
    err = auth.boot_error("0.0.0.0")
    assert err and "csa.py org domain" in err, err
    orgs.adopt_default_org("Acme", domain="acme.test", con=c)
    assert auth.boot_error("0.0.0.0") is None
    orgs.clear_sso(con=c)
    orgs.set_domain(store.DEFAULT_ORG, "", con=c)
    assert auth.boot_error("0.0.0.0") is None, "no issuer, no invariant"


# --- the claim rules, per issuer (no network: the signature step is stubbed) -------------------------

@pytest.fixture()
def claims(monkeypatch):
    seen = {}

    def fake_verify(tok, jwks_url):
        return (seen["payload"], None)
    monkeypatch.setattr(auth, "_verify_jwt", fake_verify)
    monkeypatch.setattr(auth, "discovery", lambda issuer, force=False: auth.Discovery("a", "t", "j"))

    def check(cfg, nonce="n", **payload):
        seen["payload"] = {"iss": cfg.issuer, "aud": cfg.client_id, "nonce": nonce, "exp": 4102444800,
                           "email": "Dev@Acme.com", "email_verified": True, **payload}
        return auth.verify_id_token("h.p.s", cfg, "n")
    return check


GOOGLE = auth.SsoConfig(auth.GOOGLE_ISSUER, "cid.apps.googleusercontent.com", "s", "Google")
OKTA = auth.SsoConfig("https://dev-1.okta.com/oauth2/default", "okta-cid", None, "Okta")


def test_google_uses_hd_and_every_other_issuer_the_address_domain(claims):
    assert claims(GOOGLE, hd="Acme.com") == ("dev@acme.com", "acme.com", None), "email and hd normalized"
    assert claims(GOOGLE) == ("dev@acme.com", None, None), "a consumer account: verified address, no claim"
    assert claims(GOOGLE, email="dev@acme.com", hd="evil.com")[:2] == ("dev@acme.com", "evil.com")
    assert claims(OKTA) == ("dev@acme.com", "acme.com", None), "Okta: the issuer is the trust boundary"
    assert claims(OKTA, hd="other.com") == ("dev@acme.com", "acme.com", None), "hd means nothing off Google"


def test_id_token_claim_gates(claims):
    assert claims(GOOGLE, iss="accounts.google.com")[0] == "dev@acme.com", "iss with or without the scheme"
    assert claims(OKTA, iss="dev-1.okta.com/oauth2/default")[0] == "dev@acme.com"
    assert "issuer" in claims(OKTA, iss="https://evil.example")[2]
    assert "audience" in claims(OKTA, aud="someone-elses-client-id")[2]
    assert claims(OKTA, aud=["okta-cid", "other"])[0] == "dev@acme.com", "a list aud that names us"
    assert "nonce" in claims(OKTA, nonce="other")[2]
    assert claims(OKTA, exp=1)[2] == "jwt expired"
    assert claims(OKTA, email_verified=False)[2] == auth.EMAIL_UNVERIFIED
    assert claims(OKTA, email_verified=None)[0] == "dev@acme.com", "absent means admitted"
    assert claims(OKTA, email="")[2] == auth.NO_EMAIL
    assert claims(OKTA, email="not-an-address")[2] == auth.NO_EMAIL


def test_the_nine_lookalike_cases_through_org_for_identity(tmp_path, monkeypatch, claims):
    """The hd gate, restated: with Google as the issuer a verified @acme.com address admits nobody
    unless the Workspace claim names acme.com, which a consumer account cannot forge."""
    monkeypatch.chdir(tmp_path)
    con = orgs.control_db(":memory:")
    orgs._create_org("Acme Corp", con=con)
    assert orgs.org_for_identity("dev@acme.com", "acme.com", con=con) is None, "no rule yet -> DENY"
    assert orgs.sso_allowlist_configured(con=con) is False
    orgs.set_domain("acme-corp", "Acme.COM", con=con)
    assert orgs.sso_allowlist_configured(con=con) is True
    for hd, want in (("acme.com", "acme-corp"), ("ACME.COM", "acme-corp"), (None, None), ("", None),
                     ("evil.com", None)):
        email, claim, _ = claims(GOOGLE, email="dev@acme.com", hd=hd)
        assert orgs.org_for_identity(email, claim, con=con) == want, (hd, want)
    email, claim, _ = claims(GOOGLE, email="attacker@evil.com", hd="evil.com")
    assert orgs.org_for_identity(email, claim, con=con) is None, "unknown hd -> DENY"
    assert orgs.org_for_identity("", "acme.com", con=con) is None, "no email -> DENY"
    assert orgs.org_for_identity(None, None, con=con) is None, "never raises"
    email, claim, _ = claims(OKTA, email="dev@acme.com")
    assert orgs.org_for_identity(email, claim, con=con) == "acme-corp", "Okta: the address domain"
    email, claim, _ = claims(OKTA, email="dev@evil.com")
    assert orgs.org_for_identity(email, claim, con=con) is None


# --- config and the CLI ------------------------------------------------------------------------------

def test_valid_issuer():
    assert orgs.valid_issuer(" https://dev-1.okta.com/oauth2/default ") == "https://dev-1.okta.com/oauth2/default"
    assert orgs.valid_issuer("https://tenant.auth0.com/") == "https://tenant.auth0.com/", "kept byte for byte"
    assert orgs.valid_issuer("https://accounts.google.com") == "https://accounts.google.com"
    assert orgs.valid_issuer("http://127.0.0.1:9411") == "http://127.0.0.1:9411", "a loopback stub may be http"
    for bad in ("http://idp.example", "https://u:p@idp.example", "https://idp.example/?x=1",
                "https://idp.example/#f", "idp.example", "", "https://"):
        with pytest.raises(ValueError):
            orgs.valid_issuer(bad)


def test_sso_set_show_clear_through_the_cli(tmp_path, monkeypatch, idp, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    monkeypatch.setattr("sys.stdin", io.StringIO("top-secret\n"))
    idp.reset()
    auth._discovery.clear()
    with pytest.raises(ValueError, match="names issuer"):                 # the stub names itself without a slash
        csa.cmd_sso_set(argparse.Namespace(issuer=idp.issuer + "/", client_id="cid", name=None,
                                           client_secret_stdin=True))
    monkeypatch.setattr("sys.stdin", io.StringIO("top-secret\n"))
    csa.cmd_sso_set(argparse.Namespace(issuer=idp.issuer, client_id="cid", name=None,
                                       client_secret_stdin=True))
    out = capsys.readouterr().out
    assert "redirect URI:  https://csa.example.com/api/sso/callback" in out and "top-secret" not in out
    assert "client secret: stored" in out
    cfg = auth.sso_config(orgs.control_db())
    assert cfg == auth.SsoConfig(idp.issuer, "cid", "top-secret", "127.0.0.1")
    csa.cmd_sso_show(None)
    assert "top-secret" not in capsys.readouterr().out
    csa.cmd_sso_clear(None)
    assert auth.sso_config(orgs.control_db()) is None
    assert [a["action"] for a in orgs.audit_log()][:2] == ["sso_clear", "sso_set"]
    assert "top-secret" not in json.dumps(orgs.audit_log())


def test_sso_set_refuses_a_discovery_document_that_names_another_issuer(tmp_path, monkeypatch, idp):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    idp.reset()
    idp.discovery_issuer = "https://someone.else"
    auth._discovery.clear()
    with pytest.raises(ValueError, match="names issuer"):
        csa.cmd_sso_set(argparse.Namespace(issuer=idp.issuer, client_id="cid", name="X",
                                               client_secret_stdin=False))
    assert auth.sso_config(orgs.control_db()) is None, "nothing stored"


def test_sso_set_needs_a_secret_on_stdin_when_asked_for_one(tmp_path, monkeypatch, idp):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(ValueError, match="nothing was read"):
        csa.cmd_sso_set(argparse.Namespace(issuer=idp.issuer, client_id="cid", name="X",
                                               client_secret_stdin=True))


def test_sso_set_refuses_without_csa_public_url(tmp_path, monkeypatch, idp):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CSA_PUBLIC_URL", raising=False)
    with pytest.raises(ValueError, match="CSA_PUBLIC_URL is not set here"):
        csa.cmd_sso_set(argparse.Namespace(issuer=idp.issuer, client_id="cid", name="X",
                                               client_secret_stdin=False))
    assert auth.sso_config(orgs.control_db()) is None, "nothing stored"


def test_unclaimed_logins_stay_bounded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    con = orgs.control_db(":memory:")
    cfg = auth.SsoConfig("https://idp.example", "cid", None, "IdP")
    monkeypatch.setattr(auth, "discovery", lambda issuer, force=False: auth.Discovery("https://idp.example/a", "t", "j"))
    monkeypatch.setattr(sso, "_MAX_LOGINS", 5)
    for _ in range(12):
        sso.start(con, cfg, "https://csa.example.com", "/", "")
    assert con.execute("SELECT COUNT(*) FROM sso_logins").fetchone()[0] == 5
