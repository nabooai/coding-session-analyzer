"""Role and revoke from the Team page: a verified admin's dashboard session may change membership,
a member's may not, the invite rotation still needs a bearer, and nobody revokes the credential
behind their own request. Driven through do_POST so the gate (_browser_session, _same_origin,
_COOKIE_POST_OK) is what answers, not the route body."""
import io
import json

import pytest

import apiserver
from analyzer import auth, cliauth, orgs


@pytest.fixture()
def con(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for k in ("CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CSA_AUTH", "org")
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    orgs._create_org("Acme", "acme", con=c)
    orgs._create_org("Globex", "globex", con=c)
    yield c
    c.close()


class _Http:
    """A request as do_POST sees it: path, headers, body; what it sends is captured."""
    def __init__(self, path, headers, body):
        raw = json.dumps(body).encode()
        self.path = path
        self.headers = auth._CIHeaders({**headers, "Content-Length": str(len(raw))})
        self.rfile = io.BytesIO(raw)
        self.sent = None

    def _send(self, code, body, **kw):
        self.sent = (code, body)

    def _401(self, err=None):
        self.sent = (401, {"ok": False, "error": err, "code": "unauthorized"})

    _403 = apiserver.Handler._403
    _org_admin_guard = apiserver.Handler._org_admin_guard
    _org_post = apiserver.Handler._org_post
    _presented = apiserver.Handler._presented
    do_POST = apiserver.Handler.do_POST


def cookie(tok, site="same-origin"):
    h = {"Cookie": cliauth.session_cookie(tok).split(";")[0]}
    if site:
        h["Sec-Fetch-Site"] = site
    return h


def post(path, body, headers):
    h = _Http(path, headers, body)
    h.do_POST()
    return h.sent


ROUTES = ("/api/org/member/role", "/api/org/member/revoke", "/api/org/invite/rotate")


def test_a_member_cookie_is_refused_on_every_mutation(con):
    dev = orgs.mint_token("dev@acme.com", "acme", "web-sso", con=con)
    victim = orgs.mint_token("sam@acme.com", "acme", "laptop", con=con)
    for p in ROUTES:
        code, body = post(p, {"email": "sam@acme.com", "role": "admin", "token": victim}, cookie(dev))
        assert code == 403 and body["code"] == "forbidden", p
    assert orgs.lookup(victim, con=con)[0] == "ok" and orgs.lookup(victim, con=con)[1]["role"] == "member"


def test_a_verified_admin_cookie_may_change_roles_and_revoke(con):
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    boss_web = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, verified=True)
    sam = orgs.mint_token("sam@acme.com", "acme", "laptop", con=con)
    code, body = post("/api/org/member/role", {"email": "sam@acme.com", "role": "admin"}, cookie(boss_web))
    assert code == 200 and body["ok"] and body["tokens_updated"] == 1
    assert orgs.lookup(sam, con=con)[1]["role"] == "admin"
    code, body = post("/api/org/member/revoke", {"token_hash": orgs._hash(sam)}, cookie(boss_web))
    assert code == 200 and body["ok"]
    assert orgs.lookup(sam, con=con)[0] == "revoked"


def test_rotate_stays_bearer_only(con):
    boss_web = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, role="admin")
    boss_cli = orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    code, body = post("/api/org/invite/rotate", {}, cookie(boss_web))
    assert code == 403 and "per-user token" in body["error"]
    code, body = post("/api/org/invite/rotate", {}, {"Authorization": "Bearer " + boss_cli})
    assert code == 200 and body["invite"].startswith("inv_")


def test_a_cross_site_page_cannot_use_the_cookie(con):
    boss_web = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, role="admin")
    sam = orgs.mint_token("sam@acme.com", "acme", "laptop", con=con)
    for site in ("cross-site", "same-site"):
        code, _ = post("/api/org/member/revoke", {"token": sam}, cookie(boss_web, site))
        assert code == 401, site
    assert orgs.lookup(sam, con=con)[0] == "ok"


def test_an_admin_of_another_workspace_gets_the_same_answer_as_a_miss(con):
    globex_web = orgs.mint_token("ceo@globex.com", "globex", "web-sso", con=con, role="admin")
    sam = orgs.mint_token("sam@acme.com", "acme", "laptop", con=con)
    hit = post("/api/org/member/revoke", {"token_hash": orgs._hash(sam)}, cookie(globex_web))
    miss = post("/api/org/member/revoke", {"token_hash": "f" * 64}, cookie(globex_web))
    assert hit == miss and hit[0] == 404
    assert orgs.lookup(sam, con=con)[0] == "ok"
    hit = post("/api/org/member/role", {"email": "sam@acme.com", "role": "admin"}, cookie(globex_web))
    miss = post("/api/org/member/role", {"email": "nobody@acme.com", "role": "admin"}, cookie(globex_web))
    assert hit[0] == miss[0] == 404 and hit[1]["error"] == miss[1]["error"], "the body echoes the email, nothing else differs"
    assert orgs.lookup(sam, con=con)[1]["role"] == "member"


def test_nobody_revokes_the_credential_behind_their_own_request(con):
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    boss_web = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, verified=True)
    boss_cli = orgs.mint_token("boss@acme.com", "acme", "desk", con=con, verified=True)
    for ident in (boss_web, orgs._hash(boss_web), orgs._hash(boss_web)[:12]):
        code, body = post("/api/org/member/revoke", {"token": ident}, cookie(boss_web))
        assert code == 409 and body["code"] == "self_revoke", ident
    code, body = post("/api/org/member/revoke", {"token": boss_cli}, {"Authorization": "Bearer " + boss_cli})
    assert code == 409 and body["code"] == "self_revoke", "a bearer cannot revoke itself either"
    assert orgs.lookup(boss_web, con=con)[0] == "ok" and orgs.lookup(boss_cli, con=con)[0] == "ok"
    code, body = post("/api/org/member/revoke", {"token": boss_cli}, cookie(boss_web))
    assert code == 200, "another machine of the same person is fair game"


def test_the_last_admin_refusals_carry_a_code(con):
    boss_web = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, role="admin")
    code, body = post("/api/org/member/role", {"email": "boss@acme.com", "role": "member"}, cookie(boss_web))
    assert code == 409 and body["code"] == "last_admin" and "Promote" in body["error"]
    assert orgs.lookup(boss_web, con=con)[1]["role"] == "admin"


def test_a_typed_email_sign_in_never_becomes_a_mutating_admin(con):
    """The invite form mints `web` tokens from a self-asserted address. Promoting the person must
    not promote that token: anyone holding the shared invite can mint one under any email."""
    boss_cli = orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    sam_cli = orgs.mint_token("sam@acme.com", "acme", "laptop", con=con)
    impostor = orgs.mint_token("sam@acme.com", "acme", "web", con=con)
    assert orgs.set_role("sam@acme.com", "acme", "admin", con=con) == 1
    assert orgs.lookup(sam_cli, con=con)[1]["role"] == "admin"
    assert orgs.lookup(impostor, con=con)[1]["role"] == "member"
    assert not orgs.is_mutating_admin({"role": "admin", "active": True, "hostname": "web"})
    assert orgs.is_mutating_admin({"role": "admin", "active": True, "hostname": "web-sso"})
    assert orgs.admin_count("acme", con=con, mutating_only=True) == 2
    assert orgs.lookup(boss_cli, con=con)[1]["role"] == "admin"


def test_only_an_admin_edits_the_workspace_taxonomy(con):
    member = orgs.mint_token("dev@acme.com", "acme", "laptop", con=con)
    member_web = orgs.mint_token("dev@acme.com", "acme", "web-sso", con=con, verified=True)
    boss = orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    boss_web = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, verified=True)
    add = {"key": "client", "description": "a paying customer account; id = the company name"}
    for who in ({"Authorization": "Bearer " + member}, cookie(member_web)):
        for body in (add, {"key": "client", "delete": True}):
            code, resp = post("/api/entity-types", body, who)
            assert code == 403 and resp["code"] == "forbidden", body
    code, resp = post("/api/entity-types", add, {"Authorization": "Bearer " + boss})
    assert code == 200 and resp["ok"]
    code, resp = post("/api/entity-types", {"key": "client", "delete": True}, cookie(boss_web))
    assert code == 200 and resp["ok"]
