"""Per-person invite links: one link admits ONE NAMED PERSON until it expires.

The two halves that matter are here. `orgs` decides who a link admits, and it is the one thing that
can let someone in whom `org_for_identity`'s domain gate would refuse, which is the entire reason a
design partner or a contractor can be invited at all. The routes are driven through do_GET/do_POST so
the answer comes from the gate a browser actually hits. The link riding the issuer round trip is
tests/test_sso.py.
"""
import io
import json

import pytest

import apiserver
from analyzer import auth, cliauth, orgs


@pytest.fixture()
def con(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    for k in ("CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
        monkeypatch.delenv(k, raising=False)
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    orgs._create_org("Acme", "acme", con=c)
    orgs._create_org("Rival", "rival", con=c)
    yield c
    c.close()


class _Http:
    def __init__(self, path, headers, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.path = path
        self.headers = auth._CIHeaders({**headers, "Content-Length": str(len(raw))})
        self.rfile = io.BytesIO(raw)
        self.sent = None
        self.headers_out = []

    def _send(self, code, body, ctype="application/json", headers=()):
        self.sent = (code, body)
        self.headers_out = list(headers)

    def _send_file(self, code, data, ctype):
        self.sent = (code, data)

    def _401(self, err=None):
        self.sent = (401, {"ok": False, "error": err, "code": "unauthorized"})

    _403 = apiserver.Handler._403
    _header = apiserver.Handler._header

    def send_header(self, k, v):
        self.headers_out.append((k, v))
    _base_url = apiserver.Handler._base_url
    _org_admin_guard = apiserver.Handler._org_admin_guard
    _org_get = apiserver.Handler._org_get
    _org_post = apiserver.Handler._org_post
    _providers_post = apiserver.Handler._providers_post
    _providers_view = apiserver.Handler._providers_view
    _presented = apiserver.Handler._presented
    _session_identity = apiserver.Handler._session_identity
    _onboarding_get = apiserver.Handler._onboarding_get
    _onboarding_post = apiserver.Handler._onboarding_post
    do_POST = apiserver.Handler.do_POST


def _cookie(tok, site="same-origin"):
    h = {"Cookie": cliauth.session_cookie(tok).split(";")[0]}
    if site:
        h["Sec-Fetch-Site"] = site
    return h


def _post(path, body, headers):
    h = _Http(path, headers, body)
    h.do_POST()
    return h.sent


def _admin(con, email="boss@acme.test", org="acme"):
    return orgs.mint_token(email, org, "web-sso", con=con, verified=True, role="admin")


# --- who a link admits ---------------------------------------------------------------------------

def test_a_link_admits_the_one_address_it_names(con):
    _id, token = orgs.create_invite("acme", "Dave@Partner.IO", "boss@acme.test", con=con)
    assert orgs.redeem_invite(token, "dave@partner.io", con=con) == "acme", "the named person, any case"
    assert orgs.redeem_invite(token, "someone.else@partner.io", con=con) is None
    assert orgs.redeem_invite(token, "", con=con) is None
    assert orgs.redeem_invite("jn_not-a-real-token", "dave@partner.io", con=con) is None


def test_the_same_person_may_redeem_it_again_for_a_second_machine(con):
    """`used_at` is a first-use STAMP, never a gate. Their dashboard and their laptop are two
    redemptions of a link that is still theirs until it expires."""
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    assert orgs.redeem_invite(token, "dave@partner.io", con=con) == "acme"
    first = orgs.list_invites("acme", con=con)[0]["used_at"]
    assert first, "the first redemption is stamped"
    assert orgs.redeem_invite(token, "dave@partner.io", con=con) == "acme", "still theirs"
    assert orgs.list_invites("acme", con=con)[0]["used_at"] == first, "the stamp records the FIRST use"


def test_expiry_and_revoke_both_close_a_link(con):
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", ttl_days=1, con=con)
    apiserver.orgs.db.run(con, apiserver.orgs._INV.update()
                          .values(expires_at=orgs._now(-60)))
    con.commit()
    assert orgs.redeem_invite(token, "dave@partner.io", con=con) is None, "expired"

    _id2, live = orgs.create_invite("acme", "eve@partner.io", "boss@acme.test", con=con)
    assert orgs.revoke_invite(_id2, "acme", actor="boss@acme.test", con=con)
    assert orgs.redeem_invite(live, "eve@partner.io", con=con) is None, "revoked"
    assert orgs.revoke_invite(_id2, "acme", con=con) is False, "idempotent"


def test_a_link_is_scoped_to_its_own_workspace(con):
    _id, _t = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    assert orgs.revoke_invite(_id, "rival", con=con) is False, "another tenant's id answers like a miss"
    assert [i["email"] for i in orgs.list_invites("rival", con=con)] == []


def test_a_workspace_cannot_hold_unbounded_pending_links(con, monkeypatch):
    """The standing surface, not the request rate, is what is worth bounding: an XSS holding an
    admin's session needs only ONE create to admit an outsider."""
    monkeypatch.setattr(orgs, "MAX_PENDING_INVITES", 3)
    for i in range(3):
        orgs.create_invite("acme", f"p{i}@partner.io", "boss@acme.test", con=con)
    with pytest.raises(ValueError, match="unused invite links"):
        orgs.create_invite("acme", "one-too-many@partner.io", "boss@acme.test", con=con)
    # Revoking one makes room again: the cap counts what is STANDING, not what was ever issued.
    orgs.revoke_invite(orgs.list_invites("acme", con=con)[0]["id"], "acme", con=con)
    orgs.create_invite("acme", "room-again@partner.io", "boss@acme.test", con=con)


def test_a_bad_address_is_refused_before_a_link_exists(con):
    for bad in ("", "   ", "not-an-email", "a@" + "x" * 300):
        with pytest.raises(ValueError):
            orgs.create_invite("acme", bad, "boss@acme.test", con=con)
    assert orgs.list_invites("acme", con=con) == []


# --- the gate: a link overrides the Workspace domain rule -----------------------------------------

DAVE = ("dave@partner.io", "partner.io")                  # what the issuer vouches for: a partner's address


def _issuer(con):
    orgs.set_sso("https://idp.example", "cid", None, "Acme Okta", con=con)


def test_a_link_admits_someone_the_domain_gate_would_refuse(con):
    """THE point of the feature. org_for_identity admits only a domain an org claims. Without this
    branch an invited contractor is refused and the link is a URL shortener for people who could
    already sign in."""
    _issuer(con)
    orgs.set_domain("acme", "acme.test", con=con)

    assert orgs.org_for_identity(*DAVE, con=con) is None, "the gate refuses him"
    assert cliauth.mint_gate(DAVE, "", "", "")[2], "...and so does the mint gate, with no link"

    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    email, org_id, err = cliauth.mint_gate(DAVE, "", "", orgs._hash(token))
    assert (email, org_id, err) == ("dave@partner.io", "acme", None)


def test_a_link_opened_by_the_wrong_account_is_refused(con):
    _issuer(con)
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    email, org_id, err = cliauth.mint_gate(("mallory@evil.test", "evil.test"), "", "", orgs._hash(token))
    assert email is None and org_id is None and "not usable" in err, err


def test_every_unusable_link_fails_with_the_same_sentence(con):
    """Expired, revoked, never existed and wrong account must be indistinguishable, or the endpoint
    is an oracle for which workspaces and which people exist."""
    _issuer(con)

    _rid, revoked = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    orgs.revoke_invite(_rid, "acme", con=con)
    _eid, expired = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    apiserver.orgs.db.run(con, apiserver.orgs._INV.update()
                          .where(apiserver.orgs._INV.c.id == _eid).values(expires_at=orgs._now(-60)))
    con.commit()
    _wid, wrong_person = orgs.create_invite("acme", "someone@partner.io", "boss@acme.test", con=con)

    errs = {cliauth.mint_gate(DAVE, "", "", orgs._hash(t))[2]
            for t in (revoked, expired, wrong_person, "jn_never-existed", "jn_" + "z" * 43)}
    assert len(errs) == 1, errs
    assert "not usable" in errs.pop()
    # No link at all is a DIFFERENT journey (an ordinary sign-in), not a fifth refusal: the visitor
    # already knows whether they opened an invite. The one-sentence rule is about a link that was
    # presented and did not work.
    assert "not usable" not in cliauth.mint_gate(DAVE, "", "", "")[2]


def test_without_sso_the_link_itself_names_who_signs_in(con):
    """A self-host server has nothing to verify an identity with, so the link IS the credential and
    the address it carries is who signs in. Nothing is typed, which makes this narrower than the
    shared invite beside it, where the email is whatever the form contained."""
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    assert cliauth.mint_gate(None, "", "", orgs._hash(token)) == ("dave@partner.io", "acme", None)
    ok, tok = cliauth.web_login("", "", token)
    assert ok, tok
    assert orgs.machine(tok, con=con)["hostname"] == "web", "recorded as a browser sign-in"
    assert orgs.lookup(tok, con=con)[1] == {"email": "dave@partner.io", "org_id": "acme", "role": "member",
                                            "service": False}
    # Nothing was typed: the address came off the link, so the shared invite's "sign in as a
    # colleague" hole does not exist on this path.
    assert cliauth.mint_gate(None, "someone.else@acme.test", "", orgs._hash(token))[0] == "dave@partner.io"


def test_with_an_issuer_the_link_alone_is_not_a_credential(con):
    """Once the issuer can verify who opened the link, the bare link stops signing anyone in: the
    address is what the issuer vouches for, matched against the link (tests/test_sso.py)."""
    _issuer(con)
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    email, org_id, err = cliauth.mint_gate(None, "", "", orgs._hash(token))
    assert email is None and "requires signing in with Acme Okta" in err
    h = _Http("/api/ui-login", {"Sec-Fetch-Site": "same-origin",
                                "Cookie": "%s=%s" % (cliauth.JOIN_COOKIE, token)}, {"email": "", "invite": ""})
    h._onboarding_post("/api/ui-login")
    assert h.sent[0] == 401 and not any(v.startswith(auth.SESSION_COOKIE) for k, v in h.headers_out)


def test_an_invited_person_is_a_member_never_an_admin(con):
    orgs.set_role("boss@acme.test", "acme", "admin", con=con)
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    ok, tok = cliauth.web_login("", "", token)
    assert ok and orgs.lookup(tok, con=con)[1]["role"] == "member"


# --- the routes -----------------------------------------------------------------------------------

def test_an_admin_creates_a_link_from_the_browser_with_no_shared_invite(con):
    """An adopted default workspace has no shared invite at all (adopt_default_org takes invite=None), which is exactly the
    workspace the old panel could not help."""
    assert orgs.has_invite("acme", con=con), "the fixture org has one"
    code, body = _post("/api/org/invite/new", {"email": "dave@partner.io", "ttl_days": 7},
                       _cookie(_admin(con)))
    assert code == 200 and body["ok"], body
    assert body["link"].startswith("https://csa.example.com/join/jn_"), body["link"]
    assert body["invite"]["email"] == "dave@partner.io"

    h = _Http("/api/org/invites", {})
    h._org_get("/api/org/invites", {}, {"org_id": "acme", "role": "admin", "email": "boss@acme.test"})
    assert [i["email"] for i in h.sent[1]["invites"]] == ["dave@partner.io"]
    assert all("token" not in k for i in h.sent[1]["invites"] for k in i), "never the token"


def test_a_member_cannot_create_or_revoke_a_link(con):
    member = orgs.mint_token("dev@acme.test", "acme", "web-sso", con=con, verified=True)
    for path, body in (("/api/org/invite/new", {"email": "x@y.test"}),
                       ("/api/org/invite/revoke", {"id": "abc"})):
        code, out = _post(path, body, _cookie(member))
        assert code == 403 and out["code"] == "forbidden", (path, out)


def test_a_cross_site_create_is_refused(con):
    for site in ("cross-site", "same-site"):
        code, out = _post("/api/org/invite/new", {"email": "x@y.test"}, _cookie(_admin(con), site))
        assert code == 401, (site, out)


def test_rotate_is_still_barred_from_a_browser_session(con):
    assert "/api/org/invite/rotate" not in apiserver._COOKIE_POST_OK
    code, out = _post("/api/org/invite/rotate", {}, _cookie(_admin(con)))
    assert code == 403 and out["code"] == "forbidden", out


def test_an_oversize_or_non_json_body_is_refused_without_being_read(con):
    h = _Http("/api/org/invite/new", _cookie(_admin(con)))
    h.headers = auth._CIHeaders({**h.headers._d, "Content-Length": str(64 * 1024 * 1024)})
    h.do_POST()
    assert h.sent[0] == 413, h.sent

    bad = _Http("/api/org/invite/new", _cookie(_admin(con)))
    bad.rfile = io.BytesIO(b"not json at all")
    bad.headers = auth._CIHeaders({**bad.headers._d, "Content-Length": "15"})
    bad.do_POST()
    assert bad.sent[0] == 400, bad.sent


def test_join_answers_identically_for_any_token_and_keeps_it_out_of_the_url(con):
    """It validates NOTHING: only the sign-in POST can know whether the account matches, so the ONE
    refusal has to come from there. A 302 that differs would be an existence oracle."""
    real = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)[1]
    seen = []
    for token in (real, "jn_never-existed", "../../etc/passwd"):
        h = _Http("/join/" + token, {})
        h._onboarding_get("/join/" + token, {})
        headers = dict(h.headers_out)
        seen.append((h.sent[0], headers["Location"]))
        assert headers["Referrer-Policy"] == "no-referrer", "the path IS a credential"
        assert headers["Set-Cookie"].startswith("__Host-csa_join="), headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    assert len(set(seen)) == 1, seen
    assert seen[0] == (302, "/#/connect"), "a browser session ships nothing, so land them on Connect"


def test_a_token_shaped_like_a_header_is_not_echoed_into_one(con):
    """CRLF injection, found by an adversarial probe of this branch and live before the fix.

    `/join/<token>` is PUBLIC and put the raw path segment into a Set-Cookie. unquote() decodes
    %0d%0a, .strip() only trims the ends, and http.server writes "%s: %s\r\n" with no validation, so
    an anonymous caller wrote headers of their own: a planted __Host-csa_session is a silent login as
    THEM, which walks straight past the login-CSRF guard on /api/ui-login because nothing ever posts.
    """
    attack = "x\r\nSet-Cookie: %s=csa_ATTACKER; Secure; Path=/" % auth.SESSION_COOKIE
    h = _Http("/join/" + attack, {})
    h._onboarding_get("/join/" + attack, {})
    cookies = [v for k, v in h.headers_out if k == "Set-Cookie"]
    assert len(cookies) == 1, cookies
    assert auth.SESSION_COOKIE not in cookies[0], cookies
    assert "Max-Age=0" in cookies[0], "a token that is not one carries nothing forward"
    for k, v in h.headers_out:
        assert "\r" not in v and "\n" not in v, (k, v)


def test_no_header_this_server_sends_can_end_its_own_line(con):
    """The class, not just the one route: _send is the only place headers are emitted."""
    h = _Http("/x", {})
    with pytest.raises(ValueError, match="CR or LF"):
        h._header("X-Test", "ok\r\nX-Injected: pwned")
    with pytest.raises(ValueError, match="CR or LF"):
        h._header("X-Test", "ok\nX-Injected: pwned")
    h._header("X-Test", "a normal value")     # and does not get in the way of one


def test_only_a_real_link_shape_reaches_the_cookie(con):
    live = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)[1]
    for token, carried in ((live, True), ("jn_" + "a" * 43, True),
                           ("../../etc/passwd", False), ("<script>alert(1)</script>", False),
                           ("jn_" + "a" * 500, False), ("not-prefixed", False), ("", False)):
        h = _Http("/join/" + token, {})
        h._onboarding_get("/join/" + token, {})
        cookie = dict(h.headers_out)["Set-Cookie"]
        assert (("Max-Age=0" not in cookie) is carried), (token[:30], cookie)


def test_opening_a_link_while_signed_in_arms_nothing(con):
    """An admin checking a link before sending it must not arm their OWN browser.

    Found by a user sim: the join cookie outlived the session, so when that session ended the sign-in
    card offered "Continue as <the invitee>" and one click made the admin that person. Reading the
    caller's own session here is not an oracle: the response shape does not change with the token.
    """
    token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)[1]
    signed_in = orgs.mint_token("boss@acme.test", "acme", "web-sso", con=con, verified=True)

    h = _Http("/join/" + token, _cookie(signed_in))
    h._onboarding_get("/join/" + token, {})
    cookie = dict(h.headers_out)["Set-Cookie"]
    assert "Max-Age=0" in cookie, cookie
    assert dict(h.headers_out)["Location"] == "/#/connect", "and the response is otherwise identical"

    anon = _Http("/join/" + token, {})
    anon._onboarding_get("/join/" + token, {})
    assert "Max-Age=0" not in dict(anon.headers_out)["Set-Cookie"], "a signed-out visitor still arms"


def test_a_second_link_for_the_same_person_replaces_the_first(con):
    """Two live links for one address leaves a second unused door nobody has a reason to notice, and
    "Replace link" for someone who never copied theirs is exactly this call."""
    _id1, first = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    _id2, second = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)

    assert [i["email"] for i in orgs.list_invites("acme", con=con)] == ["dave@partner.io"]
    assert orgs.redeem_invite(first, "dave@partner.io", con=con) is None, "the old link is dead"
    assert orgs.redeem_invite(second, "dave@partner.io", con=con) == "acme"


def test_replacing_does_not_trip_the_cap(con, monkeypatch):
    monkeypatch.setattr(orgs, "MAX_PENDING_INVITES", 2)
    orgs.create_invite("acme", "a@partner.io", "boss@acme.test", con=con)
    orgs.create_invite("acme", "b@partner.io", "boss@acme.test", con=con)
    orgs.create_invite("acme", "b@partner.io", "boss@acme.test", con=con)   # a replacement, not a third
    assert len(orgs.list_invites("acme", con=con)) == 2
    with pytest.raises(ValueError, match="unused invite links"):
        orgs.create_invite("acme", "c@partner.io", "boss@acme.test", con=con)


def test_a_joined_link_does_not_block_a_fresh_one(con):
    """Replacement is scoped to UNUSED links: the one that admitted someone stays as the record that
    they joined through it."""
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    assert orgs.redeem_invite(token, "dave@partner.io", con=con) == "acme"
    orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    rows = orgs.list_invites("acme", con=con)
    assert len(rows) == 2 and sum(1 for r in rows if r["used_at"]) == 1, rows
    assert orgs.redeem_invite(token, "dave@partner.io", con=con) == "acme", "and it still works"


def test_the_sign_in_card_is_told_which_workspace_invited_them(con):
    token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)[1]
    assert orgs.invite_for(token, con=con) == {"email": "dave@partner.io", "org_id": "acme"}
    assert orgs.invite_for("jn_nope", con=con) is None
    assert orgs.invite_for("", con=con) is None


def test_the_join_cookie_is_what_signs_them_in_and_is_spent_either_way(con):
    token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)[1]

    h = _Http("/api/ui-login", {"Sec-Fetch-Site": "same-origin",
                                "Cookie": "%s=%s" % (cliauth.JOIN_COOKIE, token)},
              {"email": "", "invite": ""})
    h._onboarding_post("/api/ui-login")
    assert h.sent == (200, {"ok": True}), h.sent
    cookies = [v for k, v in h.headers_out if k == "Set-Cookie"]
    assert any(c.startswith(auth.SESSION_COOKIE) for c in cookies), cookies
    assert any(c.startswith(cliauth.JOIN_COOKIE) and "Max-Age=0" in c for c in cookies), cookies


def test_the_audit_names_who_created_and_who_used_each_link(con):
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    orgs.redeem_invite(token, "dave@partner.io", con=con)
    orgs.revoke_invite(_id, "acme", actor="boss@acme.test", con=con)
    rows = orgs.audit_log("acme", con=con)
    by_action = {r["action"]: r["detail"] for r in rows}
    assert "boss@acme.test" in by_action["invite_new"] and "dave@partner.io" in by_action["invite_new"]
    assert "dave@partner.io" in by_action["invite_used"]
    assert "boss@acme.test" in by_action["invite_revoke"] and "dave@partner.io" in by_action["invite_revoke"]


def test_the_shared_workspace_invite_still_works(con):
    """A second, narrower door. The old one is untouched for existing tenants and self-host."""
    shared = orgs.rotate_invite("acme", con=con)
    assert orgs.verify_invite(shared, con=con) == "acme"
    assert cliauth.mint_gate(None, "dev@acme.test", shared) == ("dev@acme.test", "acme", None)


def test_a_link_is_refused_for_someone_already_in_the_workspace(con):
    """The link would sign whoever opens it in as someone who already HAS data here, and the panel
    would list them as waiting to join while the members table showed them joined."""
    orgs.mint_token("jules@acme.test", "acme", "laptop", con=con)
    with pytest.raises(ValueError, match="already in this workspace"):
        orgs.create_invite("acme", "Jules@Acme.test", "boss@acme.test", con=con)
    assert orgs.list_invites("acme", con=con) == []

    orgs.revoke(orgs.list_tokens("acme", con=con)[0]["token_hash"], org_id="acme", con=con)
    orgs.create_invite("acme", "jules@acme.test", "boss@acme.test", con=con)   # left, so invitable


def test_a_browser_that_followed_a_dead_link_is_told_so(con):
    """Their own cookie says the link did not resolve, not the token, so this is not an oracle: they
    learn the same thing by trying to sign in. Without it they meet a form asking for an invite code
    nobody ever gave them."""
    _id, token = orgs.create_invite("acme", "dave@partner.io", "boss@acme.test", con=con)
    orgs.revoke_invite(_id, "acme", con=con)

    h = _Http("/api/auth-config", {"Cookie": "%s=%s" % (cliauth.JOIN_COOKIE, token)})
    h._onboarding_get("/api/auth-config", {})
    assert h.sent[1]["join_stale"] is True and h.sent[1]["join_email"] is None, h.sent[1]

    none = _Http("/api/auth-config", {})
    none._onboarding_get("/api/auth-config", {})
    assert none.sent[1]["join_stale"] is False, "no cookie is not a dead link"

    live = orgs.create_invite("acme", "eve@partner.io", "boss@acme.test", con=con)[1]
    ok = _Http("/api/auth-config", {"Cookie": "%s=%s" % (cliauth.JOIN_COOKIE, live)})
    ok._onboarding_get("/api/auth-config", {})
    assert ok.sent[1] == {**ok.sent[1], "join_email": "eve@partner.io", "join_org": "Acme",
                          "join_stale": False}


def test_racing_creates_for_one_address_leave_one_live_link(tmp_path, monkeypatch):
    """An adversarial probe raced eight creates for one address and got FIVE live links, because
    revoke, count and insert were three committed calls and both racers read the same "before".
    Revoking "the link" then cut off none of the siblings.

    A real file, not :memory:, and real threads: the writer lock this relies on is per FILE, and an
    in-memory connection shared by both threads would prove nothing about it.
    """
    import threading
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    orgs.control_db().close()                            # create + migrate the file once, up front
    orgs._create_org("Acme", "acme")

    errors = []

    def make():
        try:
            orgs.create_invite("acme", "dave@partner.io", "boss@acme.test")
        except Exception as e:                           # a busy writer is a refusal, not a duplicate
            errors.append(e)

    threads = [threading.Thread(target=make) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    live = [r for r in orgs.list_invites("acme") if not r["used_at"]]
    assert len(live) == 1, (len(live), errors)
    assert len(live) + len(errors) >= 1


# --- service tokens: a machine that cannot sign in, minted from the Team page ---------------------

def test_an_admin_mints_a_service_token_from_the_browser_under_sso(con):
    orgs.set_sso("https://idp.example", "cid", None, "Okta", con=con)
    assert cliauth.mint_gate(None, "atlas@acme.test", "")[2] == "This CSA requires signing in with Okta."
    code, body = _post("/api/org/service-token/new", {"email": " Atlas@Acme.test ", "name": "atlas"},
                       _cookie(_admin(con)))
    assert code == 200 and body["ok"], body
    assert body["token"].startswith(orgs.TOKEN_PREFIX)
    assert orgs.lookup(body["token"], con=con) == ("ok", {"email": "atlas@acme.test", "org_id": "acme",
                                                          "role": "member", "service": True})
    (t,) = [t for t in orgs.list_tokens("acme", con=con) if t["hostname"] == "atlas"]
    assert t["active"] and t["expires_at"], "a service token idles out like any other"
    assert orgs.revoke(t["token_hash"], org_id="acme", con=con)
    assert orgs.lookup(body["token"], con=con)[0] != "ok"


def test_a_service_token_is_always_a_member(con):
    code, body = _post("/api/org/service-token/new", {"email": "ci@acme.test", "name": "ci", "role": "admin"},
                       _cookie(_admin(con)))
    assert code == 200 and orgs.lookup(body["token"], con=con)[1]["role"] == "member"


def test_a_service_token_may_speak_as_a_teammate(con):
    orgs.mint_token("dev@acme.test", "acme", "web-sso", con=con, verified=True)
    code, body = _post("/api/org/service-token/new", {"email": "dev@acme.test", "name": "dev-bot"},
                       _cookie(_admin(con)))
    assert code == 200 and orgs.lookup(body["token"], con=con)[1]["email"] == "dev@acme.test"


def test_promoting_an_address_never_makes_its_service_token_admin(con):
    """A stolen admin session could mint a service token and promote it: an admin credential that
    outlives the session. The role change skips service tokens, the person's own token moves."""
    admin = _cookie(_admin(con))
    person = orgs.mint_token("dev@acme.test", "acme", "dev-mac", con=con, verified=True)
    _, made = _post("/api/org/service-token/new", {"email": "dev@acme.test", "name": "dev-bot"}, admin)
    code, out = _post("/api/org/member/role", {"email": "dev@acme.test", "role": "admin"}, admin)
    assert code == 200 and out["tokens_updated"] == 1, out
    assert orgs.lookup(person, con=con)[1]["role"] == "admin"
    assert orgs.lookup(made["token"], con=con)[1]["role"] == "member"
    _, solo = _post("/api/org/service-token/new", {"email": "bot@acme.test", "name": "bot"}, admin)
    code, out = _post("/api/org/member/role", {"email": "bot@acme.test", "role": "admin"}, admin)
    assert code == 404 and orgs.lookup(solo["token"], con=con)[1]["role"] == "member", out
    assert out["error"] == "a service token is always a member"
    assert [t["service"] for t in orgs.list_tokens("acme", con=con) if t["hostname"] == "bot"] == [True]


def test_a_bad_name_or_address_mints_nothing(con):
    admin = _cookie(_admin(con))
    before = len(orgs.list_tokens("acme", con=con))
    for email, name in (("atlas", "atlas"), ("x@acme.test", "web"), ("x@acme.test", "Web-SSO"),
                        ("x@acme.test", "  "), ("", "atlas")):
        code, _ = _post("/api/org/service-token/new", {"email": email, "name": name}, admin)
        assert code == 400, (email, name)
    assert len(orgs.list_tokens("acme", con=con)) == before


def test_a_member_or_a_cross_site_page_cannot_mint(con):
    member = orgs.mint_token("dev@acme.test", "acme", "web-sso", con=con, verified=True)
    code, out = _post("/api/org/service-token/new", {"email": "x@acme.test", "name": "x"}, _cookie(member))
    assert code == 403 and out["code"] == "forbidden", out
    code, _ = _post("/api/org/service-token/new", {"email": "x@acme.test", "name": "x"},
                    _cookie(_admin(con), "cross-site"))
    assert code == 401
    assert "/api/org/service-token/new" in apiserver._COOKIE_POST_OK
