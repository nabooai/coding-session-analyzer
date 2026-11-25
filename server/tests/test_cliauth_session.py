"""The /cli-auth cookie branch: a dashboard session authorizes a machine on the word code alone.

Driven through do_POST/_onboarding_post so the answer comes from the gate the browser actually hits,
not from calling cliauth.approve() with a hand-built identity. The three controls this branch owes
its existence to are each pinned by a negative test below: the CSRF check, the member-only mint, and
the refusal of a self-asserted session on a Google-required workspace.
"""
import io
import json

import pytest

import apiserver
from analyzer import auth, cliauth, orgs, store


@pytest.fixture()
def con(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_AUTH_TOKEN", "shared-secret")
    monkeypatch.delenv("CSA_ORG_INVITE", raising=False)
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    yield c
    c.close()


class _Http:
    """A request as do_POST / do_GET see it."""
    def __init__(self, path, headers, body=None):
        raw = json.dumps(body or {}).encode()
        self.path = path
        self.headers = auth._CIHeaders({**headers, "Content-Length": str(len(raw))})
        self.rfile = io.BytesIO(raw)
        self.sent = None
        self.file = None

    def _send(self, code, body, ctype="application/json", headers=()):
        self.sent = (code, body)

    def _send_file(self, code, data, ctype):
        self.file = data.decode()

    def _401(self, err=None):
        self.sent = (401, {"ok": False, "error": err, "code": "unauthorized"})

    _session_identity = apiserver.Handler._session_identity
    _onboarding_post = apiserver.Handler._onboarding_post
    _onboarding_get = apiserver.Handler._onboarding_get
    _base_url = apiserver.Handler._base_url
    do_POST = apiserver.Handler.do_POST


def _cookie(tok, site="same-origin"):
    h = {"Cookie": cliauth.session_cookie(tok).split(";")[0]}
    if site:
        h["Sec-Fetch-Site"] = site
    return h


def _approve(body, headers):
    h = _Http("/api/cli-auth/approve", headers, body)
    h.do_POST()
    return h.sent


def _signed_in(con, email="dev@acme.test", hostname="web-sso"):
    """A dashboard session cookie, the way cliauth.web_login mints one."""
    return orgs.mint_token(email, store.DEFAULT_ORG, hostname, con=con, verified=hostname == "web-sso")


def test_a_dashboard_session_authorizes_a_machine_on_the_code_alone(con):
    nonce, code, _ = cliauth.new("MacBook-Pro-3")
    tok = _signed_in(con)

    assert _approve({"nonce": nonce, "code": code}, _cookie(tok)) == (200, {"ok": True})

    creds = cliauth.claim(nonce)
    assert creds and creds["email"] == "dev@acme.test", creds
    machine = orgs.machine(creds["token"], con=con)
    assert machine["hostname"] == "MacBook-Pro-3", machine
    assert machine["org_id"] == store.DEFAULT_ORG


def test_the_word_code_is_still_required_on_the_cookie_path(con):
    """The one control that binds "the terminal I ran" to "the page I am clicking". A phished
    authorize link opened in a browser that never ran setup must not attach a machine."""
    nonce, code, _ = cliauth.new("attacker-box")
    tok = _signed_in(con)

    for wrong in ("", "JEEP-IONOSPHERE", code + "X", "café"):
        if wrong == code:
            continue
        st, body = _approve({"nonce": nonce, "code": wrong}, _cookie(tok))
        assert st == 200 and body["ok"] is False, (wrong, body)
        assert "verification code" in body["error"], body
    assert cliauth.claim(nonce) is None, "nothing was authorized, so nothing may be claimed"


def test_a_cross_site_post_is_refused_even_carrying_the_right_code(con):
    """SameSite=Strict does not cover the same-SITE sibling-subdomain case, and this route returns
    before do_POST's own CSRF check because it is public. So it makes the check itself."""
    tok = _signed_in(con)
    for site in ("cross-site", "same-site"):
        nonce, code, _ = cliauth.new("attacker-box")
        st, body = _approve({"nonce": nonce, "code": code}, _cookie(tok, site))
        assert st == 401 and body["code"] == "unauthorized", (site, body)
        assert cliauth.claim(nonce) is None, site


def test_a_cookie_approval_keeps_the_persons_role_only_when_the_issuer_vouched(con):
    """A `web-sso` session is a verified identity, so the laptop it pairs inherits the role; an
    invite-era `web` session is self-asserted and pairs a member whatever role it holds."""
    boss = orgs.mint_token("boss@acme.test", store.DEFAULT_ORG, "web-sso", con=con,
                           verified=True, role="admin")
    nonce, code, _ = cliauth.new("boss-laptop")
    assert _approve({"nonce": nonce, "code": code}, _cookie(boss)) == (200, {"ok": True})
    creds = cliauth.claim(nonce)
    assert orgs.lookup(creds["token"], con=con)[1]["role"] == "admin", "a verified pairing keeps the role"

    typed = orgs.mint_token("legacy@acme.test", store.DEFAULT_ORG, "web", con=con, role="admin")
    nonce, code, _ = cliauth.new("legacy-laptop")
    assert _approve({"nonce": nonce, "code": code}, _cookie(typed)) == (200, {"ok": True})
    creds = cliauth.claim(nonce)
    assert orgs.lookup(creds["token"], con=con)[1]["role"] == "member", "a self-asserted session never promotes"


def test_a_typed_credential_in_the_body_beats_the_cookie(con):
    """A different person typing their own invite in a browser that holds someone else's session
    must be minted as THEMSELVES, never silently as the cookie's owner."""
    orgs._create_org("Acme", "acme", con=con)
    invite = orgs.rotate_invite("acme", con=con)
    tok = _signed_in(con, "someone.else@acme.test")

    nonce, code, _ = cliauth.new("shared-desk")
    assert _approve({"nonce": nonce, "code": code, "email": "guest@acme.test", "invite": invite},
                    _cookie(tok)) == (200, {"ok": True})

    creds = cliauth.claim(nonce)
    assert creds["email"] == "guest@acme.test", creds
    assert orgs.machine(creds["token"], con=con)["org_id"] == "acme", "the invite chose the org, not the cookie"


def test_a_workspace_with_an_issuer_refuses_a_self_asserted_session(con):
    """`web` tokens minted before an SSO cutover stay live (orgs.lookup checks only revoked_at and
    expires_at), so without this the cookie branch is a way around mint_gate's refusal."""
    orgs.set_sso("https://idp.example", "cid", None, "Acme Okta", con=con)
    assert auth.sso_required(con)

    typed = _signed_in(con, "legacy@acme.test", hostname="web")
    nonce, code, _ = cliauth.new("legacy-laptop")
    st, body = _approve({"nonce": nonce, "code": code}, _cookie(typed))
    assert st == 200 and body["ok"] is False, body
    assert "requires signing in with Acme Okta" in body["error"], body
    assert cliauth.claim(nonce) is None

    sso = _signed_in(con, "current@acme.test", hostname="web-sso")
    nonce2, code2, _ = cliauth.new("current-laptop")
    assert _approve({"nonce": nonce2, "code": code2}, _cookie(sso)) == (200, {"ok": True})
    assert cliauth.claim(nonce2)["email"] == "current@acme.test"


def test_a_revoked_session_authorizes_nothing(con):
    tok = _signed_in(con)
    orgs.revoke(tok, con=con)
    nonce, code, _ = cliauth.new("stale-browser")
    st, body = _approve({"nonce": nonce, "code": code}, _cookie(tok))
    assert st == 200 and body["ok"] is False, body
    assert cliauth.claim(nonce) is None


def test_no_cookie_still_needs_a_credential(con):
    """The branch must not turn an anonymous approve into a successful one."""
    nonce, code, _ = cliauth.new("nobody")
    st, body = _approve({"nonce": nonce, "code": code}, {"Sec-Fetch-Site": "same-origin"})
    assert st == 200 and body["ok"] is False, body
    assert cliauth.claim(nonce) is None


def test_the_page_offers_the_session_it_holds_and_still_asks_for_the_code(con, monkeypatch):
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    tok = _signed_in(con, "dev@acme.test")
    nonce, _code, _ = cliauth.new("MacBook-Pro-3")

    h = _Http("/cli-auth", _cookie(tok))
    h._onboarding_get("/cli-auth", {"nonce": [nonce], "hostname": ["MacBook-Pro-3"]})
    assert "Authorize as dev@acme.test" in h.file
    assert 'id="code"' in h.file and "required" in h.file, "the code field is still there"
    # The other two ways in stay reachable but FOLDED: laying an invite form open under "nothing else
    # is asked for" puts a second Authorize button on the screen and both user sims picked the wrong one.
    assert "<details" in h.file and "Sign in as someone else" in h.file
    assert h.file.index("Authorize as dev@acme.test") < h.file.index("<details"), "the session comes first"

    anon = _Http("/cli-auth", {})
    anon._onboarding_get("/cli-auth", {"nonce": [nonce], "hostname": ["MacBook-Pro-3"]})
    assert "Authorize as" not in anon.file, "no session, no shortcut"


def test_the_session_email_is_escaped_into_the_page(con, monkeypatch):
    """The email comes from user_tokens, not the query string, but it reaches the page as text and
    orgs.mint_token does not validate its shape."""
    tok = orgs.mint_token('a"><script>alert(1)</script>@x.test', store.DEFAULT_ORG, "web-sso",
                          con=con, verified=True)
    nonce, _code, _ = cliauth.new("box")
    h = _Http("/cli-auth", _cookie(tok))
    h._onboarding_get("/cli-auth", {"nonce": [nonce], "hostname": ["box"]})
    assert "<script>alert(1)</script>" not in h.file
    assert "&lt;script&gt;" in h.file
