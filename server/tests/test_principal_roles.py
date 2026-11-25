"""The principal is {email, org_id, role}: who you are, which workspace, what you may do inside it.
There is no axis for "reaches more than one workspace" because no HTTP plane needs one."""
import pathlib

import pytest

import apiserver
from analyzer import auth, orgs


# --- the principal's shape -------------------------------------------------------------------------

def test_tenant_principal_keeps_its_org():
    p = auth._principal("dev@acme.com", "acme", role="admin")
    assert p == {"email": "dev@acme.com", "org_id": "acme", "role": "admin", "service": False}


def test_role_defaults_to_member():
    assert auth._principal("a@b.c", "acme")["role"] == "member"


# --- there is no control plane -----------------------------------------------------------------------

def test_no_cross_workspace_http_route_exists():
    """The isolation is structural, not guarded: no HTTP route spans workspaces, so there is nothing
    to mis-wire later."""
    src = pathlib.Path(apiserver.__file__).read_text()
    routed = [ln for ln in src.splitlines() if '"/api/admin/' in ln or "'/api/admin/" in ln]
    assert not routed, "a cross-workspace route came back:\n  " + "\n  ".join(routed)
    assert not hasattr(apiserver.Handler, "_admin_guard")
    assert not hasattr(apiserver, "_admin_overview")
    assert not hasattr(apiserver, "_org_usage"), "the only HTTP code that opened more than one tenant's corpus"


def test_the_principal_has_no_cross_workspace_axis():
    p = auth._principal("dev@acme.com", "acme", role="admin")
    assert set(p) == {"email", "org_id", "role", "service"}, p


def test_every_org_route_is_scoped_to_the_callers_own_workspace():
    """No /api/org/* handler may read an org id from the request."""
    src = pathlib.Path(apiserver.__file__).read_text()
    block = src[src.index("def _org_admin_guard"):src.index("def _serve_ui")]
    for bad in ('qs.get("org_id"', "body.get(\"org_id\"", "body.get('org_id'", '_text(body, "org_id")'):
        assert bad not in block, f"/api/org/* must not read {bad} from the request"


# --- the role rides the token ----------------------------------------------------------------------

@pytest.fixture()
def con(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    c = orgs.control_db(":memory:")
    yield c
    c.close()


def test_role_round_trips_through_mint_and_lookup(con):
    orgs._create_org("Acme", "acme", con=con)
    boss = orgs.mint_token("boss@acme.com", "acme", con=con, role="admin")
    dev = orgs.mint_token("dev@acme.com", "acme", con=con)
    assert orgs.lookup(boss, con=con)[1]["role"] == "admin"
    assert orgs.lookup(dev, con=con)[1]["role"] == "member", "minting defaults to member"


def test_mint_refuses_an_unknown_role(con):
    orgs._create_org("Acme", "acme", con=con)
    with pytest.raises(ValueError):
        orgs.mint_token("x@acme.com", "acme", con=con, role="superuser")


def test_pre_upgrade_tokens_read_as_member_not_admin(con):
    """`role` is ALTER-added, so every token minted before this shipped is NULL. Defaulting NULL to
    admin would silently promote an entire existing fleet on upgrade."""
    orgs._create_org("Acme", "acme", con=con)
    tok = orgs.mint_token("old@acme.com", "acme", con=con)
    con.execute("UPDATE user_tokens SET role=NULL")               # fixture: emulate a pre-upgrade row
    con.commit()
    assert orgs.lookup(tok, con=con)[1]["role"] == "member"
    assert orgs.list_tokens("acme", con=con)[0]["role"] == "member"


def test_a_garbage_role_in_the_db_reads_as_member(con):
    """Defence in depth: a bad value written by anything must deny, not escalate."""
    orgs._create_org("Acme", "acme", con=con)
    tok = orgs.mint_token("x@acme.com", "acme", con=con)
    con.execute("UPDATE user_tokens SET role='admin '")            # trailing space: not a role
    con.commit()
    assert orgs.lookup(tok, con=con)[1]["role"] == "member"


def test_verify_keeps_its_published_two_key_shape(con):
    """orgs.verify is a documented interface; role must not leak into it."""
    orgs._create_org("Acme", "acme", con=con)
    tok = orgs.mint_token("x@acme.com", "acme", con=con, role="admin")
    assert orgs.verify(tok, con=con) == {"email": "x@acme.com", "org_id": "acme"}


# --- the role follows the PERSON, not the token ------------------------------------------------------
# From the tenant-admin simulation: a workspace admin who signed into the DASHBOARD came back as a
# member, because the browser login mints a fresh token and fresh tokens started at member. The whole
# admin surface was unreachable from the one place a non-technical admin would look.

def test_a_verified_sign_in_inherits_the_person_s_role(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "cli", con=con, role="admin")
    browser = orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, verified=True)
    assert orgs.lookup(browser, con=con)[1]["role"] == "admin", \
        "signing into the dashboard with a verified identity must not demote a workspace admin"


def test_an_UNVERIFIED_mint_never_inherits_a_role(con):
    """THE escalation this closes. On a box without Google SSO, cliauth.mint_gate accepts a TYPED
    email — so inheriting from it would let any invite-holder type their admin's address (visible in
    /api/team, the member list and the audit log) and mint themselves admin."""
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "cli", con=con, role="admin")
    impostor = orgs.mint_token("boss@acme.com", "acme", "attacker-laptop", con=con)   # verified=False
    assert orgs.lookup(impostor, con=con)[1]["role"] == "member", \
        "a self-asserted email must never inherit a role"


def test_inheritance_is_per_org_and_case_insensitive(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    orgs.mint_token("boss@acme.com", "acme", con=con, role="admin")
    same = orgs.mint_token("BOSS@Acme.com", "acme", con=con, verified=True)
    assert orgs.lookup(same, con=con)[1]["role"] == "admin"
    elsewhere = orgs.mint_token("boss@acme.com", "globex", con=con, verified=True)
    assert orgs.lookup(elsewhere, con=con)[1]["role"] == "member", \
        "being an admin at Acme must not make you one at Globex"


def test_a_revoked_admin_token_does_not_keep_granting_the_role(con):
    orgs._create_org("Acme", "acme", con=con)
    tok = orgs.mint_token("boss@acme.com", "acme", con=con, role="admin")
    orgs.revoke(tok, con=con)
    fresh = orgs.mint_token("boss@acme.com", "acme", con=con, verified=True)
    assert orgs.lookup(fresh, con=con)[1]["role"] == "member", \
        "revoking the admin token removes the role; minting again must not resurrect it"


def test_an_explicit_role_still_wins(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("dev@acme.com", "acme", con=con)                        # member
    promoted = orgs.mint_token("dev@acme.com", "acme", con=con, role="admin")
    assert orgs.lookup(promoted, con=con)[1]["role"] == "admin"


def test_admin_count_only_counts_live_admins(con):
    orgs._create_org("Acme", "acme", con=con)
    assert orgs.admin_count("acme", con=con) == 0
    a = orgs.mint_token("boss@acme.com", "acme", con=con, role="admin")
    orgs.mint_token("dev@acme.com", "acme", con=con)
    assert orgs.admin_count("acme", con=con) == 1
    orgs.revoke(a, con=con)
    assert orgs.admin_count("acme", con=con) == 0


# --- the last-admin guard counts admins who can actually ADMINISTER ---------------------------------
# From the tenant-admin re-run: two admin tokens (a laptop and a typed-email dashboard tab), revoked
# the laptop, passed a naive count at 2, and was left holding a session that 403s on every mutation.

def test_admin_count_can_exclude_typed_email_sessions(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    orgs.mint_token("boss@acme.com", "acme", "web", con=con, verified=True)   # inherits; unverified form
    assert orgs.admin_count("acme", con=con) == 2
    assert orgs.admin_count("acme", con=con, mutating_only=True) == 1, \
        "a typed-email session can read as admin but never changes membership"


def test_web_sso_sessions_count_as_mutating(con):
    """A Google-verified dashboard session changes roles and revokes from the Team page."""
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "web-sso", con=con, role="admin")
    assert orgs.admin_count("acme", con=con, mutating_only=True) == 1


# --- promoting a colleague, which the last-admin refusal tells you to do -----------------------------

def test_set_role_promotes_every_token_of_that_person(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    a = orgs.mint_token("dev@acme.com", "acme", "laptop", con=con)
    b = orgs.mint_token("dev@acme.com", "acme", "desktop", con=con)
    assert orgs.set_role("dev@acme.com", "acme", "admin", con=con) == 2
    assert orgs.lookup(a, con=con)[1]["role"] == "admin"
    assert orgs.lookup(b, con=con)[1]["role"] == "admin", \
        "the role belongs to the person; nobody may end up half-admin across their own machines"


def test_set_role_is_case_insensitive_and_scoped_to_the_org(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs._create_org("Globex", "globex", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    tok = orgs.mint_token("dev@acme.com", "acme", con=con)
    other = orgs.mint_token("dev@acme.com", "globex", con=con)       # same human, another workspace
    assert orgs.set_role("DEV@Acme.com", "acme", "admin", con=con) == 1
    assert orgs.lookup(tok, con=con)[1]["role"] == "admin"
    assert orgs.lookup(other, con=con)[1]["role"] == "member", \
        "promoting someone in one workspace must not promote them in another"


def test_set_role_refuses_to_demote_the_last_usable_admin(con):
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    with pytest.raises(ValueError, match="only admin"):
        orgs.set_role("boss@acme.com", "acme", "member", con=con)
    orgs.mint_token("second@acme.com", "acme", "laptop", con=con, role="admin")
    assert orgs.set_role("boss@acme.com", "acme", "member", con=con) == 1


def test_set_role_rejects_bad_input(con):
    orgs._create_org("Acme", "acme", con=con)
    with pytest.raises(ValueError):
        orgs.set_role("x@acme.com", "acme", "owner", con=con)
    with pytest.raises(ValueError):
        orgs.set_role("", "acme", "admin", con=con)
    assert orgs.set_role("nobody@acme.com", "acme", "admin", con=con) == 0, "unknown email is a no-op"
