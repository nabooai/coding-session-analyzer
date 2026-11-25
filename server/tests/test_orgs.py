"""orgs._self_check ported scenario-for-scenario into the suite (P4), plus the map's gap tests
(legacy-token import, per-org TTL, delete_org disk removal, org_count fails OPEN, the sliding-
renewal/_epoch assertions). Assertions kept verbatim wherever the original had them; the
env-mutating blocks use monkeypatch instead of the original save/restore dict.

Tests may raw-`con.execute` on the fixture connection (fixtures/inspection are not the
control plane's write path)."""
import os
import sqlite3
import time

import pytest

from analyzer import orgs, store, tenant

_ENV_KEYS = ("CSA_AUTH", "CSA_AUTH_TOKEN", "CSA_ORG_INVITE")


def setenv(monkeypatch, **kw):
    """The original _self_check's setenv: clear the whole auth env, then set exactly `kw`."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def con(monkeypatch, tmp_path):
    """In-memory control db (the original ran against control_db(':memory:')). chdir into
    tmp_path: store paths are cwd-relative, so mint_token's per-org TTL probe and delete_org's
    rmtree land in the tmp dir, never in the checkout."""
    monkeypatch.chdir(tmp_path)
    c = orgs.control_db(":memory:")
    yield c
    c.close()


def _row(con, token):
    return con.execute("SELECT last_used_at, expires_at FROM user_tokens WHERE token_hash=?",
                       (orgs._hash(token),)).fetchone()


# --- org + invite lifecycle; verify_invite is the MINT gate: all four branches ---------------

def test_env_invite_works_on_a_single_tenant_box(con, monkeypatch):
    """Branch 2, the self-host path. NO provisioned orgs here on purpose — that is what makes this box
    single-tenant, and the env invite is gated on tenancy now, not on the auth mode."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    assert orgs.verify_invite("shared-secret", con=con) == store.DEFAULT_ORG, "self-host: the admin secret is the invite"
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret", CSA_ORG_INVITE="env-invite")
    assert orgs.verify_invite("env-invite", con=con) == store.DEFAULT_ORG, "CSA_ORG_INVITE takes precedence"
    assert orgs.verify_invite("shared-secret", con=con) is None, "CSA_ORG_INVITE set -> the auth token is NOT an invite"
    # gating branch 2 on the auth mode would kill /cli-auth on every self-host box; tenancy is the gate
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret", CSA_ORG_INVITE="env-invite")
    assert orgs.verify_invite("env-invite", con=con) == store.DEFAULT_ORG, \
        "a single-tenant box keeps minting after the mode collapse"


def test_env_invite_is_off_once_the_box_hosts_tenants(con, monkeypatch):
    """Branch 2 disabled. Either signal is enough: a provisioned org, or `default` adopted as one.

    This is what keeps D5 meaningful — demoting the shared secret to operator-only buys nothing if the
    same secret can walk through /cli-auth and mint a corpus-reading token."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret", CSA_ORG_INVITE="env-invite")
    orgs._create_org("Acme Corp", con=con)
    assert orgs.verify_invite("env-invite", con=con) is None, "a provisioned org -> env invites are OFF"
    assert orgs.verify_invite("shared-secret", con=con) is None, "...and the admin secret never mints into default"

    orgs.delete_org("acme-corp", con=con)
    assert orgs.verify_invite("env-invite", con=con) == store.DEFAULT_ORG, "back to single-tenant"
    orgs.adopt_default_org("Acme", con=con)
    assert orgs.verify_invite("env-invite", con=con) is None, "adopting `default` also closes the env path"


def test_org_and_invite_lifecycle(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    oid, invite = orgs._create_org("Acme Corp", con=con)
    assert oid == "acme-corp" and invite.startswith(orgs.INVITE_PREFIX)
    assert orgs.verify_invite(invite, con=con) == "acme-corp"
    assert orgs.verify_invite("inv_wrong", con=con) is None and orgs.verify_invite("", con=con) is None
    assert orgs.verify_invite(None, con=con) is None                  # never raises
    assert orgs.verify_invite(invite, con=con) == "acme-corp", "a provisioned org invite always wins"
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret", CSA_ORG_INVITE="env-invite")
    assert orgs.verify_invite(invite, con=con) == "acme-corp", "org mode: real org invites still work"
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="")          # nothing configured, non-off mode
    assert orgs.verify_invite("anything", con=con) is None, "unset must never mean open"
    setenv(monkeypatch, CSA_AUTH="off")
    assert orgs.verify_invite("anything", con=con) == store.DEFAULT_ORG, "loopback dev (off, nothing configured) allows"

    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    with pytest.raises(ValueError):
        orgs._create_org("Acme Corp", con=con)                        # duplicate org must raise
    with pytest.raises(ValueError):
        orgs._create_org("x", org_id="default", con=con)              # 'default' must not be creatable
    with pytest.raises(ValueError):
        orgs._create_org("x", org_id="../escape", con=con)            # path-unsafe org id must raise


# --- mint + verify ---------------------------------------------------------------------------

def test_mint_and_verify(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    _oid, invite = orgs._create_org("Acme Corp", con=con)
    tok = orgs.mint_token("dev@acme.com", "acme-corp", "MacBook-Pro", con=con)
    assert tok.startswith(orgs.TOKEN_PREFIX) and len(tok) > 40
    assert orgs.verify(tok, con=con) == {"email": "dev@acme.com", "org_id": "acme-corp"}
    assert orgs.verify("csa_nope", con=con) is None and orgs.verify("", con=con) is None \
        and orgs.verify(None, con=con) is None
    assert orgs.verify(invite, con=con) is None, "an INVITE must never authenticate as a user token"
    assert orgs.mint_token("dev@acme.com", "acme-corp", con=con) != tok, "each mint is a distinct secret"
    with pytest.raises(ValueError):
        orgs.mint_token("x@y.com", "no-such-org", con=con)            # mint into a missing org must raise
    with pytest.raises(ValueError):
        orgs.mint_token("", "acme-corp", con=con)                     # mint without an email must raise


# --- invite rotation invalidates the INVITE but NOT already-minted tokens (acceptance) -------

def test_rotate_invite_invalidates_invite_not_tokens(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    _oid, invite = orgs._create_org("Acme Corp", con=con)
    tok = orgs.mint_token("dev@acme.com", "acme-corp", "MacBook-Pro", con=con)
    inv2 = orgs.rotate_invite("acme-corp", con=con)
    assert orgs.verify_invite(invite, con=con) is None, "rotated: the OLD invite must stop working"
    assert orgs.verify_invite(inv2, con=con) == "acme-corp"
    assert orgs.verify(tok, con=con), "invite rotation must NOT sign existing developers out"
    with pytest.raises(ValueError):
        orgs.rotate_invite("no-such-org", con=con)


# --- revocation: denies, is distinguishable from 'unknown', and never falls through ----------

def test_revocation_distinguishable_and_idempotent(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    tok = orgs.mint_token("dev@acme.com", "acme-corp", "MacBook-Pro", con=con)
    assert orgs.lookup(tok, con=con)[0] == "ok"
    assert orgs.revoke(tok, con=con) is True
    assert orgs.verify(tok, con=con) is None
    assert orgs.lookup(tok, con=con) == ("revoked", None), \
        "revoked != unknown (must not fall through to the shared secret)"
    assert orgs.lookup("csa_unknown", con=con) == (None, None)
    assert orgs.revoke(tok, con=con) is True, "revoke is idempotent"
    first_revoked_at = con.execute("SELECT revoked_at FROM user_tokens WHERE token_hash=?",
                                   (orgs._hash(tok),)).fetchone()[0]
    assert orgs.revoke(tok, con=con) is True and con.execute(
        "SELECT revoked_at FROM user_tokens WHERE token_hash=?",
        (orgs._hash(tok),)).fetchone()[0] == first_revoked_at, "re-revoking keeps the ORIGINAL timestamp"
    assert orgs.revoke("csa_never-existed", con=con) is False
    th = [t for t in orgs.list_tokens("acme-corp", con=con) if not t["active"]][0]["token_hash"]
    assert orgs.revoke(th, con=con) is True, "operator can revoke by token_hash (they never hold plaintext)"


# --- listing: no plaintext, ever; scoped by org; last_used_at written on use -----------------

def test_listing_no_plaintext_scoped_and_last_used(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    orgs.mint_token("dev@acme.com", "acme-corp", "MacBook-Pro", con=con)
    t2 = orgs.mint_token("other@acme.com", "acme-corp", con=con)
    rows = orgs.list_tokens("acme-corp", con=con)
    assert all(t2 not in str(r.values()) for r in rows), "list_tokens must never expose a plaintext token"
    assert {r["email"] for r in rows} == {"dev@acme.com", "other@acme.com"}
    orgs._create_org("Beta Inc", con=con)
    assert orgs.list_tokens("beta-inc", con=con) == [] and len(orgs.list_tokens(con=con)) == len(rows)
    assert orgs.org_count(con=con) == 2 and {o["id"] for o in orgs.list_orgs(con=con)} == {"acme-corp", "beta-inc"}
    # list_orgs keeps a per-org token count that reads 0 (never NULL) for a token-less org
    assert {o["id"]: o["tokens"] for o in orgs.list_orgs(con=con)} == {"acme-corp": 2, "beta-inc": 0}
    # --- last_used_at: written on use, throttled, and never fatal ---
    assert orgs.verify(t2, con=con) is not None
    assert [t for t in orgs.list_tokens("acme-corp", con=con)
            if t["email"] == "other@acme.com"][0]["last_used_at"] is not None, "verify() records last use"


# --- audit trail: every admin action is recorded (PM: 'where is that recorded?') -------------

def test_audit_trail(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    orgs.rotate_invite("acme-corp", con=con)
    tok = orgs.mint_token("dev@acme.com", "acme-corp", "MacBook-Pro", con=con)
    orgs.revoke(tok, con=con)
    acts = [r[0] for r in con.execute("SELECT action FROM admin_log ORDER BY rowid")]
    assert acts[:3] == ["create_org", "rotate_invite", "mint_token"] or "create_org" in acts, acts
    assert "revoke" in acts and "mint_token" in acts
    # audit_log(): newest-first, org-scopable (the operator console's history feed)
    full = orgs.audit_log(con=con)
    assert full and full[0]["at"] >= full[-1]["at"], "audit_log is newest-first"
    scoped = orgs.audit_log("acme-corp", con=con)
    assert scoped and all(r["org_id"] == "acme-corp" for r in scoped), "audit_log(org) scopes to that org"
    assert {r["action"] for r in scoped} <= {"create_org", "rotate_invite", "mint_token", "revoke", "delete_org"}
    # ties on `at` (same-second actions) resolve newest-ROW-first: rowid DESC, not just the timestamp
    assert [r["action"] for r in full][:2] == ["revoke", "mint_token"]


# --- revoke by the SHORT hash prefix the CLI actually prints ---------------------------------

def test_revoke_by_printed_hash_prefix(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    t3 = orgs.mint_token("prefix@acme.com", "acme-corp", con=con)
    th3 = orgs._hash(t3)
    assert orgs.revoke(th3[:12], con=con) is True, "revoke must accept the 12-char printed hash prefix"
    assert [r for r in orgs.list_tokens("acme-corp", con=con)
            if r["email"] == "prefix@acme.com"][0]["active"] is False
    ta = orgs.mint_token("amb1@acme.com", "acme-corp", con=con)
    tb = orgs.mint_token("amb2@acme.com", "acme-corp", con=con)
    ha, hb = orgs._hash(ta), orgs._hash(tb)
    common = os.path.commonprefix([ha, hb])
    if common:                                            # a shared prefix must refuse rather than revoke one
        with pytest.raises(ValueError):
            orgs.revoke(common, con=con)
    assert orgs.revoke("deadbeef", con=con) is False, "a prefix matching nothing returns False, never raises"


# --- token expiry: minted with one, denied past it, and NOT confusable with 'unknown' --------

def test_token_expiry(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    te = orgs.mint_token("ttl@acme.com", "acme-corp", con=con)
    assert _row(con, te)[1] is not None, "a minted token must carry an expiry — that was the whole bug"
    assert _row(con, te)[1] > orgs._now(), "...in the future"
    assert orgs.lookup(te, con=con)[0] == "ok"
    con.execute("UPDATE user_tokens SET expires_at=? WHERE token_hash=?", (orgs._now(-60), orgs._hash(te)))
    assert orgs.lookup(te, con=con) == ("expired", None), "past its expiry -> denied"
    assert orgs.verify(te, con=con) is None, "verify() denies an expired token"
    assert orgs.lookup(te, con=con)[0] is not None, "expired must be DISTINGUISHABLE from unknown, or the " \
        "gate would fall through to the shared secret and expiry would be bypassable"
    assert [t for t in orgs.list_tokens("acme-corp", con=con) if t["email"] == "ttl@acme.com"][0]["active"] is False, \
        "an expired token must not read as active in the operator console"


# --- sliding renewal: USING a token is what refreshes it (there is no refresh endpoint) ------

def test_sliding_renewal(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    ts = orgs.mint_token("slide@acme.com", "acme-corp", con=con)
    assert orgs.lookup(ts, con=con)[0] == "ok"
    # Look busy-but-stale: last used 2h ago (past the hourly throttle), expiry 60s out.
    con.execute("UPDATE user_tokens SET last_used_at=?, expires_at=? WHERE token_hash=?",
                (orgs._now(-7200), orgs._now(60), orgs._hash(ts)))
    before = _row(con, ts)[1]
    assert orgs.lookup(ts, con=con)[0] == "ok"
    assert _row(con, ts)[1] > before, "a used token must have its expiry slid forward — otherwise every " \
                                      "token in the fleet hard-expires one TTL after mint"
    assert _row(con, ts)[1] > orgs._now(86400), "...by the full TTL, not by a nudge"
    # The throttle must not block the slide from ever happening: a token used again within the hour
    # keeps its (already far-future) expiry, which is fine — but _epoch must read UTC, not local time.
    assert abs(orgs._epoch(orgs._now()) - time.time()) < 5, \
        "_epoch must round-trip _now() in UTC (time.mktime does not)"


# --- expiry can be disabled, and disabling must not resurrect an already-dated token ---------

def test_ttl_zero_disables_but_never_resurrects(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    te = orgs.mint_token("ttl@acme.com", "acme-corp", con=con)
    con.execute("UPDATE user_tokens SET expires_at=? WHERE token_hash=?", (orgs._now(-60), orgs._hash(te)))
    tenant.set_config(store.org_db("acme-corp"), token_ttl_days=0)
    assert orgs._ttl_seconds("acme-corp") == 0
    tn = orgs.mint_token("never@acme.com", "acme-corp", con=con)
    assert _row(con, tn)[1] is None, "TTL=0 -> minted without an expiry"
    assert orgs.lookup(tn, con=con)[0] == "ok"
    assert orgs.lookup(te, con=con) == ("expired", None), "TTL=0 must not un-expire an already-dated token"


# --- the upgrade path: pre-existing immortal tokens get dated from their LAST USE ------------

def test_date_undated_upgrade_path(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    tn = orgs.mint_token("never@acme.com", "acme-corp", con=con)
    ts = orgs.mint_token("slide@acme.com", "acme-corp", con=con)
    con.execute("UPDATE user_tokens SET expires_at=NULL WHERE token_hash IN (?,?)",
                (orgs._hash(tn), orgs._hash(ts)))
    con.execute("UPDATE user_tokens SET last_used_at=? WHERE token_hash=?", (orgs._now(-90 * 86400), orgs._hash(tn)))
    con.execute("UPDATE user_tokens SET last_used_at=? WHERE token_hash=?", (orgs._now(-3600), orgs._hash(ts)))
    assert orgs.date_undated(con) == 2
    assert orgs.lookup(tn, con=con) == ("expired", None), "quiet for 90d with a 30d TTL -> already cold"
    assert orgs.lookup(ts, con=con)[0] == "ok", "used an hour ago -> keeps working across the upgrade"
    assert orgs.date_undated(con) == 0, "idempotent: a dated token is not re-dated"
    assert con.execute("SELECT expires_at FROM user_tokens WHERE token_hash=?",
                       (orgs._hash(ts),)).fetchone()[0] > orgs._now(), \
        "the dated value is a real ISO timestamp, not sqlite's default 'YYYY-MM-DD HH:MM:SS' shape " \
        "(which sorts BEFORE every _now() and reads as expired)"


# --- SSO: the domain claim decides, and nothing configured never means open -------------------

def test_org_for_identity_domain_gate(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    assert orgs.org_for_identity("dev@acme.com", "acme.com", con=con) is None, "no rule yet -> DENY"
    assert orgs.sso_allowlist_configured(con=con) is False
    assert orgs.set_domain("acme-corp", "Acme.COM", con=con) == "acme.com", "normalized"
    assert orgs.sso_allowlist_configured(con=con) is True
    assert orgs.org_for_identity("dev@acme.com", "acme.com", con=con) == "acme-corp", "the org claims the domain"
    assert orgs.org_for_identity("dev@acme.com", "ACME.COM", con=con) == "acme-corp", "match is case-insensitive"
    # THE security property: a verified address at the domain is NOT the claim. For Google the claim
    # is `hd`, which a consumer account holding a verified @acme.com address does not have.
    assert orgs.org_for_identity("dev@acme.com", None, con=con) is None, \
        "a verified @acme.com address with NO domain claim must NOT reach acme-corp"
    assert orgs.org_for_identity("dev@acme.com", "", con=con) is None, "an empty claim is not a wildcard"
    assert orgs.org_for_identity("attacker@evil.com", "evil.com", con=con) is None, "unknown claim -> DENY"
    assert orgs.org_for_identity("", "acme.com", con=con) is None, "no email -> DENY"
    assert orgs.org_for_identity(None, None, con=con) is None, "never raises"
    assert orgs.org_for_identity("solo@gmail.com", "gmail.com", con=con) is None, \
        "no env allowlist: a person outside every org domain needs a per-person invite link"


# --- set_domain: validated, unique across orgs, unbindable -----------------------------------

def test_set_domain_validation_and_uniqueness(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    orgs._create_org("Beta Inc", con=con)
    assert orgs.set_domain("acme-corp", "acme.com", con=con) == "acme.com"
    for bad in ("not a domain", "acme", "http://acme.com", "a@b.com"):
        with pytest.raises(ValueError):
            orgs.set_domain("beta-inc", bad, con=con)
    with pytest.raises(ValueError):
        orgs.set_domain("beta-inc", "acme.com", con=con)  # one domain must not map to two orgs — coin flip
    with pytest.raises(ValueError):
        orgs.set_domain("no-such-org", "beta.com", con=con)
    assert orgs.set_domain("beta-inc", "", con=con) is None, "blank unbinds"
    assert orgs.set_domain("acme-corp", "acme.com", con=con) == "acme.com", "re-binding its OWN domain is fine"
    assert [o for o in orgs.list_orgs(con=con) if o["id"] == "acme-corp"][0]["domain"] == "acme.com"


# --- delete_org: refuses 'default'; ERRORS on an unknown org; keeps the audit row ------------

def test_delete_org_refuses_default_and_unknown(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Acme Corp", con=con)
    orgs._create_org("Beta Inc", con=con)
    with pytest.raises(ValueError):
        orgs.delete_org("default", con=con)               # delete_org('default') must refuse
    with pytest.raises(ValueError):
        orgs.delete_org("no-such-org-typo", con=con)      # delete_org(unknown) must raise, not no-op
    d = orgs.delete_org("beta-inc", con=con)
    assert d["org_id"] == "beta-inc" and orgs.org_count(con=con) == 1
    assert con.execute("SELECT COUNT(*) FROM admin_log WHERE action='delete_org'").fetchone()[0] == 1
    # The "creating an org would make this server REFUSE TO BOOT" guard is GONE, and its absence is
    # the point: it existed because `token` mode was single-tenant, and the mode collapse removed the
    # single-tenant mode rather than the org. Creating an org in a legacy `token` env now just works.
    orgs.create_org("No Longer Refused", con=con)
    assert orgs.org_count(con=con) == 2


# =============================================================================================
# Gap tests (the P4 map): behavior the __main__ self-check never covered.
# =============================================================================================

# --- _import_legacy_tokens: reads a FOREIGN pre-expiry corpus schema, one txn, retry-on-fail --

@pytest.fixture()
def legacy_corpus(tmp_path, monkeypatch):
    """A corpus db at the default org's path carrying PR #39's shim `user_tokens` table — the PRE-EXPIRY
    schema (6 columns, no expires_at), which is exactly what _import_legacy_tokens reads."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    old = sqlite3.connect(store.org_db())
    old.execute("CREATE TABLE user_tokens(token_hash TEXT PRIMARY KEY, email TEXT, org_id TEXT, "
                "hostname TEXT, created TEXT, revoked_at TEXT)")
    old.execute("INSERT INTO user_tokens VALUES ('aa11','dev@x.com','default','laptop','2026-01-01T00:00:00Z',NULL)")
    old.execute("INSERT INTO user_tokens VALUES ('bb22','ex@x.com','default','old-box','2025-01-01T00:00:00Z',"
                "'2025-06-01T00:00:00Z')")
    old.commit()
    old.close()
    return str(tmp_path / "control.db")


def test_import_legacy_tokens(legacy_corpus, monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)              # TTL falls to the built-in 30d default
    ctl = orgs.control_db(legacy_corpus)
    assert orgs._import_legacy_tokens(ctl) is True
    rows = {r[0]: r for r in ctl.execute(
        "SELECT token_hash,email,revoked_at,last_used_at,expires_at FROM user_tokens")}
    assert set(rows) == {"aa11", "bb22"}
    assert rows["aa11"][3] is None, "imported rows arrive with last_used_at NULL (the schema had no column to read)"
    # date_undated ran: the LIVE row is dated from created (+TTL), keeping the Z shape...
    exp = rows["aa11"][4]
    assert exp is not None and exp.endswith("Z") and orgs._epoch(exp) > 0, exp
    assert exp > "2026-01-01T00:00:00Z"
    # ...and the revoked row is left alone (dating it would be meaningless).
    assert rows["bb22"][2] == "2025-06-01T00:00:00Z" and rows["bb22"][4] is None
    # the import is audited under the default org
    audit = orgs.audit_log(con=ctl)
    assert [r for r in audit if r["action"] == "import_legacy_tokens"
            and r["org_id"] == store.DEFAULT_ORG and r["detail"] == "n=2"]
    # idempotent: INSERT OR IGNORE on the hash PK — a re-run adds nothing and changes nothing
    assert orgs._import_legacy_tokens(ctl) is True
    again = {r[0]: r for r in ctl.execute(
        "SELECT token_hash,email,revoked_at,last_used_at,expires_at FROM user_tokens")}
    assert again == rows
    ctl.close()


def test_import_legacy_tokens_nothing_to_do(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctl = orgs.control_db(str(tmp_path / "control.db"))
    assert orgs._import_legacy_tokens(ctl) is True, "no corpus db at all -> done"
    os.makedirs("data", exist_ok=True)
    sqlite3.connect(store.org_db()).close()                # a corpus db with NO shim table
    assert orgs._import_legacy_tokens(ctl) is True, "no shim table -> done"
    assert ctl.execute("SELECT COUNT(*) FROM user_tokens").fetchone()[0] == 0
    ctl.close()


def test_import_legacy_tokens_transient_failure_returns_false(legacy_corpus):
    """False (NOT True) on a write failure, so the caller retries next boot — losing developers'
    tokens to a one-off lock collision would be the exact silent sign-out this exists to prevent."""
    orgs.control_db(legacy_corpus).close()                # create the control db file first
    ro = sqlite3.connect(f"file:{legacy_corpus}?mode=ro", uri=True)
    assert orgs._import_legacy_tokens(ro) is False, "a failed import must be retried, never marked done"
    ro.close()
    ctl = sqlite3.connect(legacy_corpus)
    assert ctl.execute("SELECT COUNT(*) FROM user_tokens").fetchone()[0] == 0, "nothing half-landed"
    ctl.close()


# --- _ttl_seconds: the per-org branch (tenant_config on the ORG db) vs the built-in default ---------

def test_ttl_seconds_per_org_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_TOKEN_TTL_DAYS", "7")
    assert orgs._ttl_seconds() == 30 * 86400, "no org -> the built-in 30d default; no env is consulted"
    assert orgs._ttl_seconds("ghost-org") == 30 * 86400, "an unreadable org db must not block token flows"
    path = store.org_db("acme")
    store.sqlite_db(path).close()                         # full org schema incl. tenant_config
    tenant.set_config(path, token_ttl_days=5)
    assert orgs._ttl_seconds("acme") == 5 * 86400, "per-tenant TTL comes from the ORG db's tenant_config"
    tenant.set_config(path, token_ttl_days=0)
    assert orgs._ttl_seconds("acme") == 0, "a stored 0 disables expiry (it used to collapse to 30d)"
    tenant.set_config(path, token_ttl_days=None)
    assert orgs._ttl_seconds("acme") == 30 * 86400, "clearing it is the built-in default"


# --- delete_org: the corpus DIR actually leaves the disk -------------------------------------

def test_delete_org_removes_corpus_dir(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    orgs._create_org("Beta Inc", con=con)
    orgs.mint_token("a@beta.com", "beta-inc", con=con)
    orgs.mint_token("b@beta.com", "beta-inc", con=con)
    d = store.org_dir("beta-inc")
    os.makedirs(os.path.join(d, "extracted"))
    with open(os.path.join(d, "analyzer.db"), "w") as f:
        f.write("corpus")
    out = orgs.delete_org("beta-inc", con=con)
    assert out == {"org_id": "beta-inc", "corpus_removed": True, "tokens_deleted": 2}
    assert not os.path.exists(d), "the org's whole corpus dir must leave the disk (right-to-delete)"
    assert con.execute("SELECT COUNT(*) FROM user_tokens WHERE org_id='beta-inc'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM orgs WHERE id='beta-inc'").fetchone()[0] == 0
    row = con.execute("SELECT detail FROM admin_log WHERE action='delete_org'").fetchone()
    assert row and row[0] == "tokens=2 corpus=removed", "the admin_log row SURVIVES the deletion"
    # an org that exists only as a directory (no row, no tokens) is still a real deletion
    ghost = store.org_dir("ghost")
    os.makedirs(ghost)
    out = orgs.delete_org("ghost", con=con)
    assert out == {"org_id": "ghost", "corpus_removed": True, "tokens_deleted": 0}
    assert not os.path.exists(ghost)


# --- org_count fails OPEN (and its fail-closed siblings) — pinned on purpose ------------------

class _Boom:
    """A connection whose every execute raises — 'the control plane is unreadable'."""
    def execute(self, *a, **kw):
        raise sqlite3.OperationalError("control plane unreadable")


def test_org_count_fails_open_pin(monkeypatch):
    """PINNED behavior, not an endorsement: org_count swallows exceptions -> 0, and 0 DISARMS the
    single-tenant boot guard (auth.boot_error sees 'no orgs'). Fail-open on purpose so a torn
    control.db can't brick every self-host boot; this test exists so a future change to either
    direction is a deliberate one."""
    assert orgs.org_count(con=_Boom()) == 0


def test_gates_fail_closed_when_control_plane_raises(monkeypatch):
    """The mint/SSO gates are the opposite quadrant: an unreadable control plane is a DENY (even
    for the env-secret branch that never needed the db: the read comes first and its exception
    reads as deny)."""
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="shared-secret")
    assert orgs.verify_invite("shared-secret", con=_Boom()) is None, \
        "verify_invite: a raised read is a global deny (silent, by design — never a 500)"
    assert orgs.org_for_identity("dev@acme.com", "acme.com", con=_Boom()) is None, \
        "org_for_identity hits the db first; a raise must deny"
    assert orgs.sso_allowlist_configured(con=_Boom()) is False, "unreadable -> not configured"
    assert orgs.lookup("csa_whatever", con=_Boom()) == (None, None), "lookup never raises: deny"


# --- workspace requests (landing page -> operator approval) ------------------------------------

def test_request_workspace_dedupes_and_validates(con):
    orgs.request_workspace("  Acme   Corp ", "A@Acme.com", con=con)
    orgs.request_workspace("acme corp", "a@acme.COM", con=con)       # same normalized pair -> no 2nd row
    rows = orgs.list_requests(con=con)
    assert len(rows) == 1 and rows[0]["company"] == "Acme Corp" and rows[0]["status"] == "pending"
    orgs.request_workspace("Acme Corp", "b@acme.com", con=con)       # a second developer IS a new row
    assert len(orgs.list_requests(con=con)) == 2
    with pytest.raises(ValueError):
        orgs.request_workspace("", "a@x.com", con=con)               # company required
    with pytest.raises(ValueError):
        orgs.request_workspace("X", "not-an-email", con=con)         # the mint gate's email rule
    with pytest.raises(ValueError):
        orgs.request_workspace("X", "a@" + "x" * 260, con=con)       # over-long REJECTED, never truncated
    # non-string company/email are a clean reject, not str()-coerced into a junk row (prober finding):
    # a POSTed object would otherwise store as "{'$ne': 1}" for an operator to read.
    with pytest.raises(ValueError):
        orgs.request_workspace({"$ne": 1}, "a@x.com", con=con)
    with pytest.raises(ValueError):
        orgs.request_workspace("X", 123, con=con)
    with pytest.raises(ValueError):
        orgs.request_workspace("X", True, con=con)
    with pytest.raises(ValueError):
        orgs.request_workspace(None, "a@x.com", con=con)             # None company -> "required" (missing, not a type error)
    before = len(orgs.list_requests(status=None, con=con))
    with pytest.raises(ValueError):
        orgs.request_workspace([1, 2], "a@x.com", con=con)
    assert len(orgs.list_requests(status=None, con=con)) == before, "a rejected type wrote no row"
    # control chars are stripped BEFORE storage — these strings get print()ed by the CLI
    orgs.request_workspace("Evil\x1b[31m Co", "evil@evil.com", con=con)
    stored = [r["company"] for r in orgs.list_requests(con=con) if "Evil" in r["company"]][0]
    assert "\x1b" not in stored
    # ...and beyond C0: C1, bidi override/isolate, zero-width, line/para separator, BOM all stripped
    sneaky = [0x9b, 0x202e, 0x2066, 0x200b, 0x2028, 0xfeff]   # CSI, RLO, LRI, ZWSP, LS, BOM
    dirty = "A" + "".join(chr(c) for c in sneaky) + "B"
    orgs.request_workspace(dirty, "bidi@evil.com", con=con)
    stored = [r["company"] for r in orgs.list_requests(con=con) if r["email"] == "bidi@evil.com"][0]
    assert not any(chr(c) in stored for c in sneaky), repr(stored)


def test_request_workspace_returns_new_row_once(con):
    # the return powers the operator notification: a real id (+ sanitized values) for a NEW request,
    # None for a duplicate — so an operator is pinged once, never on re-submits.
    new = orgs.request_workspace("Acme Corp", "dev@acme.com", con=con)
    assert new and new["id"] and new["company"] == "Acme Corp" and new["email"] == "dev@acme.com"
    dup = orgs.request_workspace("  acme   CORP ", "DEV@ACME.com", con=con)   # same normalized pair
    assert dup is None, "a duplicate returns None: nothing to notify on, and no second row"
    assert len(orgs.list_requests(con=con)) == 1
    other = orgs.request_workspace("Acme Corp", "second@acme.com", con=con)  # a new developer IS new
    assert other and other["id"] != new["id"]


def test_list_requests_limit_and_count(con):
    for i in range(6):
        orgs.request_workspace(f"Co {i}", f"c{i}@x.com", con=con)
    assert orgs.count_requests(con=con) == 6
    capped = orgs.list_requests(con=con, limit=2)
    assert len(capped) == 2, "limit caps the read so the admin payload can't be unbounded"
    assert {r["company"] for r in capped} <= {f"Co {i}" for i in range(6)}, "capped rows are real requests"
    assert len(orgs.list_requests(con=con)) == 6, "no limit returns all"
    assert orgs.count_requests("approved", con=con) == 0, "count respects the status filter"


def test_request_approve_lifecycle(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="admin-secret")   # the real deployment mode
    orgs.request_workspace("Acme Corp", "dev@acme.com", con=con)
    rid = orgs.list_requests(con=con)[0]["id"]
    r = orgs.approve_request(rid, domain="acme.com", con=con)
    assert r["org_id"] == "acme-corp" and r["invite"].startswith(orgs.INVITE_PREFIX)
    assert r["domain"] == "acme.com" and r["domain_error"] is None
    assert orgs.verify_invite(r["invite"], con=con) == "acme-corp", "the printed invite really signs in"
    assert orgs.org_for_identity("dev@acme.com", "acme.com", con=con) == "acme-corp"
    row = orgs.list_requests(status=None, con=con)[0]
    assert row["status"] == "approved" and row["org_id"] == "acme-corp" and row["handled_at"]
    assert orgs.list_requests(con=con) == [], "the pending view empties on approval"
    acts = [a["action"] for a in orgs.audit_log("acme-corp", con=con)]
    assert "approve_request" in acts and "create_org" in acts
    # resubmitting the approved pair is STILL a silent no-op — no new row, no status change
    orgs.request_workspace("ACME CORP", "DEV@acme.com", con=con)
    assert len(orgs.list_requests(status=None, con=con)) == 1
    # double-approve: readable refusal naming the org; no second org and no way to reprint the invite
    with pytest.raises(ValueError) as e:
        orgs.approve_request(rid, con=con)
    assert "already approved" in str(e.value) and "acme-corp" in str(e.value)
    assert len(orgs.list_orgs(con=con)) == 1


def test_request_slug_collision_and_explicit_id(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="admin-secret")
    orgs.request_workspace("Acme Corp", "a@one.com", con=con)
    orgs.request_workspace("acme CORP", "b@two.com", con=con)        # different email, same slug
    rid_a = [r["id"] for r in orgs.list_requests(con=con) if r["email"] == "a@one.com"][0]
    rid_b = [r["id"] for r in orgs.list_requests(con=con) if r["email"] == "b@two.com"][0]
    orgs.approve_request(rid_a, con=con)
    with pytest.raises(ValueError) as e:
        orgs.approve_request(rid_b, con=con)
    assert "--id" in str(e.value), "the collision advice names the flag that resolves it"
    row = [r for r in orgs.list_requests(status=None, con=con) if r["email"] == "b@two.com"][0]
    assert row["status"] == "pending", "a failed approve rolls back whole — the request stays pending"
    r2 = orgs.approve_request(rid_b, org_id="acme-corp-2", con=con)
    assert r2["org_id"] == "acme-corp-2"
    assert {o["id"] for o in orgs.list_orgs(con=con)} == {"acme-corp", "acme-corp-2"}


def test_request_approve_domain_conflict_reports_not_raises(con, monkeypatch):
    setenv(monkeypatch, CSA_AUTH="org", CSA_AUTH_TOKEN="admin-secret")
    orgs._create_org("Other", domain="taken.com", con=con)
    orgs.request_workspace("New Co", "n@new.com", con=con)
    rid = orgs.list_requests(con=con)[0]["id"]
    r = orgs.approve_request(rid, domain="taken.com", con=con)
    assert r["invite"].startswith(orgs.INVITE_PREFIX), "the invite survives the domain refusal"
    assert r["domain"] is None and "taken.com" in r["domain_error"]
    assert orgs.verify_invite(r["invite"], con=con) == "new-co"


def test_request_approve_unknown_id(con):
    with pytest.raises(ValueError):
        orgs.approve_request("zzzzzzzz", con=con)


def test_org_invite_cli_says_the_old_invite_is_dead_only_after_the_first(con, monkeypatch, capsys):
    import argparse

    import csa
    orgs.adopt_default_org("Acme", None, con=con)
    monkeypatch.setattr(orgs, "control_db", lambda path=None: con)
    csa.cmd_org_invite(argparse.Namespace(org_id="default"))
    first = capsys.readouterr().out
    assert "invite: inv_" in first and "old invite is dead" not in first
    csa.cmd_org_invite(argparse.Namespace(org_id="default"))
    assert "The old invite is dead" in capsys.readouterr().out
