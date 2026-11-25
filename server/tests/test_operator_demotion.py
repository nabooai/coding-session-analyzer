"""The shared secret is refused once a box hosts tenants, and a tenant admin manages their own
workspace and nobody else's.

Mostly NEGATIVE tests: the failures that matter are a route answering when it should refuse, or
refusing in a way that tells the caller something true about another tenant."""
import pytest

import apiserver
from analyzer import auth, orgs, store

_ENV = ("CSA_AUTH", "CSA_AUTH_TOKEN", "CSA_ORG_INVITE")


def setenv(monkeypatch, **kw):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def con(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="sekret")
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    yield c
    c.close()


class _Fake:
    """Enough of Handler for the tenant-admin routes: it records what was sent."""
    def __init__(self):
        self.sent = None
        self.headers = auth._CIHeaders()

    def _send(self, code, body, **kw):
        self.sent = (code, body)

    _403 = apiserver.Handler._403
    _org_admin_guard = apiserver.Handler._org_admin_guard
    _presented = apiserver.Handler._presented


def post(p, body, principal):
    h = _Fake()
    apiserver.Handler._org_post(h, p, body, principal)
    return h.sent


def bearer(tok):
    return auth._CIHeaders({"Authorization": "Bearer " + tok})


# --- the demotion ----------------------------------------------------------------------------------

def test_shared_secret_is_refused_once_the_box_hosts_tenants(con):
    orgs._create_org("Acme", "acme", con=con)
    ok, p, err = auth.identify(bearer("sekret"))
    assert ok is False and p is None and "csa-client setup" in err


def test_shared_secret_keeps_the_default_corpus_on_a_self_host_box(con):
    ok, p, _ = auth.identify(bearer("sekret"))
    assert ok and p == {"email": None, "org_id": store.DEFAULT_ORG, "role": "admin", "service": False}


def test_a_user_token_is_still_a_tenant(con):
    orgs._create_org("Acme", "acme", con=con)
    tok = orgs.mint_token("dev@acme.com", "acme", con=con)
    ok, p, _ = auth.identify(bearer(tok))
    assert ok and p["org_id"] == "acme" and p["role"] == "member"


# --- the tenant-admin surface is scoped ------------------------------------------------------------

def test_revoke_is_org_scoped_and_fails_indistinguishably(con):
    """THE cross-tenant write primitive, closed: orgs.revoke is org-blind by default."""
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    a_tok = orgs.mint_token("dev@acme.com", "acme", con=con)
    b_tok = orgs.mint_token("dev@globex.com", "globex", con=con)
    b_hash = orgs._hash(b_tok)

    assert orgs.revoke(b_hash, con=con, org_id="acme") is False, "another org's token must not revoke"
    assert orgs.lookup(b_tok, con=con)[0] == "ok", "...and must still work"
    assert orgs.revoke("f" * 64, con=con, org_id="acme") is False, "a hash that exists nowhere answers the same"

    assert orgs.revoke(orgs._hash(a_tok), con=con, org_id="acme") is True, "its own org still works"
    assert orgs.lookup(a_tok, con=con)[0] == "revoked"


def test_the_revoke_route_passes_the_org_scope(con):
    """The route, not just the model: a tenant admin revoking another org's hash gets the same 404 a
    nonexistent hash gets, and the victim's token still works."""
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    orgs.mint_token("second@acme.com", "acme", "laptop", con=con, role="admin")
    victim = orgs.mint_token("dev@globex.com", "globex", con=con)
    boss = auth._principal("boss@acme.com", "acme", role="admin")

    code, body = post("/api/org/member/revoke", {"token_hash": orgs._hash(victim)}, boss)
    assert code == 404 and body["ok"] is False
    assert post("/api/org/member/revoke", {"token_hash": "f" * 64}, boss) == (code, body)
    assert orgs.lookup(victim, con=con)[0] == "ok"


def test_prefix_ambiguity_is_computed_within_the_org_only(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    tok = orgs.mint_token("dev@acme.com", "acme", con=con)
    th = orgs._hash(tok)
    # force a colliding prefix in the OTHER org: unscoped this would raise "matches 2 tokens"
    con.execute("INSERT INTO user_tokens(token_hash,email,org_id,created) VALUES(?,?,?,?)",
                (th[:8] + "0" * 56, "x@globex.com", "globex", orgs._now()))
    con.commit()
    assert orgs.revoke(th[:8], con=con, org_id="acme") is True, \
        "a stranger's token must not make my own prefix ambiguous"


def test_members_and_audit_are_scoped(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    orgs.mint_token("dev@acme.com", "acme", con=con)
    orgs.mint_token("dev@globex.com", "globex", con=con)
    assert {m["email"] for m in orgs.list_tokens("acme", con=con)} == {"dev@acme.com"}
    rows = orgs.audit_log("acme", con=con)
    assert rows and all(r["org_id"] == "acme" for r in rows), rows


def test_a_null_org_means_every_org_so_no_principal_may_carry_one(con):
    """None is the wildcard in orgs.list_tokens; every principal therefore carries a real org id."""
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    orgs.mint_token("dev@acme.com", "acme", con=con)
    orgs.mint_token("dev@globex.com", "globex", con=con)
    assert len(orgs.list_tokens(None, con=con)) == 2
    assert len(orgs.list_tokens("acme", con=con)) == 1
    assert auth._principal(None, None)["org_id"] == store.DEFAULT_ORG


# --- the last-admin guards -------------------------------------------------------------------------

def test_the_last_admin_guard_cannot_be_bypassed_with_a_plaintext_token(con):
    """The route accepts `token` OR `token_hash`; the guard must resolve the target exactly as revoke
    will, and plaintext is what an admin actually holds."""
    orgs._create_org("Acme", "acme", con=con)
    plaintext = orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    for ident in (plaintext, orgs._hash(plaintext), orgs._hash(plaintext)[:10]):
        row = orgs.resolve_token(ident, "acme", con=con)
        assert row and row["token_hash"] == orgs._hash(plaintext), ident
    assert orgs.resolve_token("nope", "acme", con=con) is None
    assert orgs.resolve_token("", "acme", con=con) is None
    boss = auth._principal("boss@acme.com", "acme", role="admin")
    code, body = post("/api/org/member/revoke", {"token": plaintext}, boss)
    assert code == 409 and body["code"] == "last_admin"
    assert orgs.lookup(plaintext, con=con)[0] == "ok"


def test_a_sole_admin_with_two_laptops_cannot_demote_themselves(con):
    """The guard counts PEOPLE who can still administer, not tokens: one admin on two machines is
    still one admin."""
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("alice@acme.com", "acme", "mbp", con=con, role="admin")
    orgs.mint_token("alice@acme.com", "acme", "desk", con=con, role="admin")
    orgs.mint_token("bob@acme.com", "acme", "laptop", con=con)
    with pytest.raises(ValueError, match="only admin"):
        orgs.set_role("alice@acme.com", "acme", "member", con=con)
    assert orgs.admin_count("acme", con=con, mutating_only=True) == 2
    orgs.mint_token("bob@acme.com", "acme", "laptop-2", con=con, role="admin")
    assert orgs.set_role("alice@acme.com", "acme", "member", con=con) == 2, "with a second person it goes through"


def test_promoting_someone_with_no_live_token_changes_nothing(con):
    """A promotion written onto expired rows would report success and evaporate on the next sign-in."""
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    orgs.mint_token("sam@acme.com", "acme", "laptop", con=con)
    con.execute("UPDATE user_tokens SET expires_at='2000-01-01T00:00:00Z' WHERE email='sam@acme.com'")
    con.commit()
    assert orgs.set_role("sam@acme.com", "acme", "admin", con=con) == 0
    assert orgs._role_of("sam@acme.com", "acme", con) is None
    code, body = post("/api/org/member/role", {"email": "sam@acme.com", "role": "admin"},
                      auth._principal("boss@acme.com", "acme", role="admin"))
    assert code == 404 and "sign in" in body["error"]


def test_set_role_updates_exactly_the_rows_it_matched(con):
    """Matching in Python and updating with SQLite's ASCII-only lower() disagreed on a non-ASCII
    email: 1 token reported updated, 0 changed."""
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("root@acme.com", "acme", "laptop", con=con, role="admin")
    tok = orgs.mint_token("Öz@acme.com", "acme", "laptop", con=con)
    assert orgs.set_role("öz@acme.com", "acme", "admin", con=con) == 1
    assert orgs.lookup(tok, con=con)[1]["role"] == "admin"


# --- the tenant-admin POST surface ---------------------------------------------------------------

def test_org_post_refuses_a_body_that_is_not_an_object(con):
    orgs._create_org("Acme", "acme", con=con)
    boss = auth._principal("boss@acme.com", "acme", role="admin")
    assert post("/api/org/member/revoke", [], boss)[0] == 400
    assert post("/api/org/member/revoke", "x", boss)[0] == 400


def test_org_post_tolerates_non_string_fields(con):
    orgs._create_org("Acme", "acme", con=con)
    boss = auth._principal("boss@acme.com", "acme", role="admin")
    assert post("/api/org/member/revoke", {"token_hash": 12345}, boss)[0] == 404
    code, body = post("/api/org/member/role", {"email": ["a@b"], "role": None}, boss)
    assert code == 400 and "invalid role" in body["error"]


def test_org_post_refuses_members(con):
    orgs._create_org("Acme", "acme", con=con)
    assert post("/api/org/member/revoke", {}, auth._principal("dev@acme.com", "acme"))[0] == 403


def test_rotate_on_an_unadopted_self_host_box_explains_itself(con):
    """The self-host shared secret is a `default` admin, but `default` has no org row to rotate."""
    _, p, _ = auth.identify(bearer("sekret"))
    code, body = post("/api/org/invite/rotate", {}, p)
    assert code == 400 and "adopt" in body["error"]


def test_members_admitted_counts_people_not_tokens(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    orgs.mint_token("boss@acme.com", "acme", "web", con=con, verified=True)
    orgs.mint_token("dev@acme.com", "acme", "laptop", con=con)
    code, body = post("/api/org/invite/rotate", {}, auth._principal("boss@acme.com", "acme", role="admin"))
    assert code == 200 and body["members_admitted"] == 2 and body["invite"].startswith("inv_")
