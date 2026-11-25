"""D3: adopt `default` as a real org, and collapse the auth modes to one.

Two things are being pinned here, and they pull in opposite directions:

  * a HOSTED box must be able to name its built-in corpus as a tenant without moving a byte, and
  * a SELF-HOST box must not notice any of this happened.

The migration's whole safety argument is that keeping the id `default` keeps SSO routing and every
already-minted token on the same file. That claim gets a test rather than a comment."""

import pytest

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
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    yield c
    c.close()


# --- the modes -------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["token", "cloudflare"])
def test_former_modes_refuse_to_boot_and_deny_at_the_gate(monkeypatch, mode):
    setenv(monkeypatch, CSA_AUTH=mode, CSA_AUTH_TOKEN="s")
    assert auth.VALID_MODES == {"off", "org"}
    err = auth.boot_error("127.0.0.1")
    assert err and "not one of" in err and "'org'" in err
    ok, principal, err = auth.identify(auth._CIHeaders({"Authorization": "Bearer s"}))
    assert ok is False and principal is None and err, "the gate denies too, should boot_error be bypassed"


def test_org_boots_with_orgs_present(monkeypatch, con):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s")
    orgs._create_org("Acme", con=con)
    assert auth.boot_error("0.0.0.0") is None


def test_off_is_loopback_dev(monkeypatch):
    setenv(monkeypatch, CSA_AUTH="off")
    assert auth.current_mode() == "off"
    assert auth.boot_error("127.0.0.1") is None
    assert auth.boot_error("0.0.0.0"), "off on a non-loopback bind still refuses"
    ok, p, _ = auth.identify(auth._CIHeaders())
    assert ok and p["org_id"] == store.DEFAULT_ORG
    assert p["role"] == "admin", \
        "loopback dev is one person who is both; without role=admin the tenant-admin routes 403 locally"


# --- adopting `default` ----------------------------------------------------------------------------

def test_adopt_creates_the_row_without_touching_data(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org")
    assert orgs.default_is_adopted(con) is False
    assert orgs.adopt_default_org("Acme", domain="example.com", con=con) == store.DEFAULT_ORG
    assert orgs.default_is_adopted(con) is True
    row = [o for o in orgs.list_orgs(con) if o["id"] == "default"][0]
    assert row["name"] == "Acme" and row["domain"] == "example.com"
    # the corpus file is exactly where it always was — that is what makes this a zero-byte migration
    assert store.org_db("default") == store.data_root() / "analyzer.db"
    assert store.org_dir("default") == store.data_root()


def test_adopt_stores_no_invite_by_default(con, monkeypatch):
    """THE trap. verify_invite branch 1 matches a stored invite_hash BEFORE any mode or window check,
    so seeding this row with sha256(CSA_AUTH_TOKEN) — the obvious reading of "keep the current invite
    working" — would make the old shared secret a permanent mint credential living in control.db,
    outliving any rotation of the env var."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="the-old-shared-secret")
    orgs.adopt_default_org("Acme", con=con)
    assert con.execute("SELECT invite_hash FROM orgs WHERE id='default'").fetchone()[0] is None
    assert orgs.verify_invite("the-old-shared-secret", con=con) is None, \
        "the demoted secret must not mint a corpus token through /cli-auth"


def test_adopt_refuses_twice(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org")
    orgs.adopt_default_org("Acme", con=con)
    with pytest.raises(ValueError):
        orgs.adopt_default_org("Acme Again", con=con)     # would rotate a live invite out from under it
    with pytest.raises(ValueError):
        orgs.adopt_default_org("", con=con)


def test_create_org_still_refuses_the_default_id(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org")
    with pytest.raises(ValueError):
        orgs._create_org("Acme", org_id="default", con=con)


def test_adopting_does_not_arm_a_boot_refusal(con, monkeypatch):
    """org_count() excludes `default`, which is why the INSERT cannot make a running server refuse its
    next boot half-way through a cutover — and why rolling back to the previous image stays safe."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="s")
    before = orgs.org_count(con)
    orgs.adopt_default_org("Acme", con=con)
    assert orgs.org_count(con) == before == 0
    assert auth.boot_error("0.0.0.0") is None


# --- SSO routing lands on the same file the tokens already name -------------------------------------

def test_sso_routing_lands_in_default_after_adoption(con, monkeypatch):
    """Conclusion 6's load-bearing claim. The org row that claims the domain IS `default`, the
    string every already-minted token carries, so a sign-in and a laptop read the same file. Giving
    `default` a NEW id is what would split the brain: tokens would keep reading the old file while
    sign-ins minted into a new one. Two half-populated corpora, no error, green health check."""
    setenv(monkeypatch, CSA_AUTH="org")
    assert orgs.org_for_identity("mara@example.com", "example.com", con=con) is None, "no row, no rule: DENY"

    orgs.adopt_default_org("Acme", domain="example.com", con=con)
    assert orgs.org_for_identity("mara@example.com", "example.com", con=con) == store.DEFAULT_ORG
    tok = orgs.mint_token("mara@example.com", store.DEFAULT_ORG, "mara-mac", con=con)
    assert orgs.verify(tok, con=con)["org_id"] == store.DEFAULT_ORG, "the same string a laptop token carries"

    # ...and still fail-closed for everyone else
    assert orgs.org_for_identity("x@other.com", "other.com", con=con) is None
    assert orgs.org_for_identity("mara@example.com", None, con=con) is None, "the claim, not the email domain"


def test_the_domain_is_now_owned_and_cannot_be_reassigned(con, monkeypatch):
    """A side effect worth a runbook line: once `default` claims example.com, no future org can."""
    setenv(monkeypatch, CSA_AUTH="org")
    orgs.adopt_default_org("Acme", domain="example.com", con=con)
    orgs._create_org("Other", con=con)
    with pytest.raises(ValueError):
        orgs.set_domain("other", "example.com", con=con)


# --- de-duplication: `default` must be listed ONCE ---------------------------------------------------

def test_corpus_orgs_lists_default_once_before_and_after(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org")
    orgs._create_org("Acme", "acme", con=con)
    assert orgs.corpus_orgs(con) == ["default", "acme"]
    orgs.adopt_default_org("Acme", con=con)
    got = orgs.corpus_orgs(con)
    assert got.count("default") == 1, got
    assert sorted(got) == ["acme", "default"], got


def test_is_multi_tenant(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org")
    assert orgs.is_multi_tenant(con) is False, "a bare self-host box"
    orgs._create_org("Acme", "acme", con=con)
    assert orgs.is_multi_tenant(con) is True
    orgs.delete_org("acme", con=con)
    assert orgs.is_multi_tenant(con) is False
    orgs.adopt_default_org("Acme", con=con)
    assert orgs.is_multi_tenant(con) is True, "an adopted default is a tenant too"


# --- an unreadable control plane -------------------------------------------------------------------
# From the self-host simulation: `chmod 000 data/control.db` (the classic after an image pull changes
# the uid on a bind mount) made EVERY request 403 and flipped a private box's front door to the public
# marketing landing, while /api/health kept answering 200 and the uptime monitor stayed green.

@pytest.fixture()
def broken_control(monkeypatch):
    def boom(path=None):
        raise OSError("unable to open database file")
    monkeypatch.setattr(orgs, "control_db", boom)


def test_on_error_is_the_caller_s_decision(broken_control):
    """"The restrictive answer" points in OPPOSITE directions depending on the question, which is why
    a single fail-closed default got this wrong."""
    assert orgs.is_multi_tenant() is True, "auth/invite: cannot tell -> do not hand out a corpus"
    assert orgs.is_multi_tenant(on_error=False) is False, \
        "the front door: cannot tell -> keep the private sign-in, not a public write form"


def test_health_reports_the_control_plane(broken_control):
    assert orgs.control_plane_ok() is False


def test_health_is_ok_when_the_control_plane_reads(con):
    assert orgs.control_plane_ok(con) is True


def test_the_env_invite_still_refuses_when_the_control_plane_is_down(broken_control, monkeypatch):
    """The security half must stay fail-CLOSED even though the front-door half does not."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="sekret", CSA_ORG_INVITE="env-invite")
    assert orgs.verify_invite("env-invite") is None


def test_the_env_invite_asks_tenancy_with_the_fail_closed_default(con, monkeypatch):
    """verify_invite's tenancy gate must keep is_multi_tenant's on_error=True: a regression to False
    would re-open the env invite on an unreadable control plane, and the test above cannot see it
    because the invite lookup fails first."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="sekret", CSA_ORG_INVITE="env-invite")
    calls, real = [], orgs.is_multi_tenant

    def spy(c=None, on_error=True):
        calls.append(on_error)
        return real(c, on_error=on_error)

    monkeypatch.setattr(orgs, "is_multi_tenant", spy)
    assert orgs.verify_invite("env-invite", con=con) == store.DEFAULT_ORG
    assert calls == [True]


def test_on_error_is_honoured_when_the_QUERY_fails_not_just_the_open(monkeypatch, tmp_path):
    """The first version composed org_count() + default_is_adopted(), which swallow their own
    exceptions and return 0 / True. So a control plane that OPENED but failed to READ (SQLITE_BUSY
    under a long writer, a corrupt page) never reached the on_error branch at all — it answered True,
    which is the private-box-shows-a-public-form failure the parameter exists to prevent."""
    monkeypatch.chdir(tmp_path)
    c = orgs.control_db(":memory:")
    monkeypatch.setattr(orgs, "control_db", lambda path=None: c)
    c.close()                                             # opens fine, every query now raises
    assert orgs.is_multi_tenant(on_error=False) is False
    assert orgs.is_multi_tenant(on_error=True) is True
    assert orgs.control_plane_ok() is False


def test_adopt_validates_the_domain_before_committing(con, monkeypatch):
    """set_domain used to run AFTER the commit, so a typo left the deployment adopted — shared secret
    demoted, env invite closed, front door flipped — with a traceback for output and no way to retry,
    because adopting twice is refused."""
    setenv(monkeypatch, CSA_AUTH="org")
    with pytest.raises(ValueError):
        orgs.adopt_default_org("Acme", domain="not a domain", con=con)
    assert orgs.default_is_adopted(con) is False, "a rejected domain must leave NOTHING behind"
    orgs._create_org("Other", "other", domain="taken.com", con=con)
    with pytest.raises(ValueError):
        orgs.adopt_default_org("Acme", domain="taken.com", con=con)
    assert orgs.default_is_adopted(con) is False
    orgs.adopt_default_org("Acme", domain="example.com", con=con)     # ...and a good one still works
    assert orgs.default_is_adopted(con) is True


def test_role_of_and_admin_count_agree_on_expired_tokens(con, monkeypatch):
    """Two definitions of "is an admin": _role_of filtered on revoked_at alone while admin_count used
    `active`, so an org could hold a promotable ghost and believe it had no admins at once."""
    setenv(monkeypatch, CSA_AUTH="org")
    orgs._create_org("Acme", "acme", con=con)
    orgs.mint_token("boss@acme.com", "acme", "laptop", con=con, role="admin")
    con.execute("UPDATE user_tokens SET expires_at='2000-01-01T00:00:00Z'")   # fixture: expire it
    con.commit()
    assert orgs.admin_count("acme", con=con) == 0
    fresh = orgs.mint_token("boss@acme.com", "acme", "new-laptop", con=con, verified=True)
    assert orgs.lookup(fresh, con=con)[1]["role"] == "member", \
        "an EXPIRED admin token must not keep promoting new ones while admin_count reads zero"
