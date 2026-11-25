"""cliauth._self_check ported scenario-for-scenario into the suite (P4), plus the move of the
handshake out of process memory into control.db's `cli_pairings`: the state machine, the
exactly-once claim, what the table is allowed to hold, the mint gate, cookies, and page escaping.

The fixture chdirs into tmp_path so `data/control.db` (a cwd-relative path) lands in the tmp dir —
same code path as production, hermetic disk, and a fresh handshake table per test."""
import importlib
import os
import sqlite3
import threading

import pytest

from analyzer import auth, cliauth, orgs


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """org-mode env + tmp cwd. cliauth's control_db() calls open a connection per call, which
    resolves to `data/control.db` under tmp_path here."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_AUTH_TOKEN", "shared-secret")
    monkeypatch.delenv("CSA_ORG_INVITE", raising=False)
    yield


def _pairings():
    """Every column of every handshake row, straight off disk — what a reader of control.db sees."""
    con = sqlite3.connect(orgs.control_path())
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM cli_pairings")]
    finally:
        con.close()


def _backdate(nonce, seconds):
    """Push a handshake's `created` into the past, the way the clock would."""
    con = orgs.control_db()
    con.execute("UPDATE cli_pairings SET created=? WHERE nonce_hash=?",
                (orgs._now(-seconds), orgs._hash(nonce)))
    con.commit()
    con.close()


# --- code derivation -------------------------------------------------------------------------

def test_code_derivation(env):
    n, code, ttl = cliauth.new("MacBook-Pro-3")
    assert ttl == cliauth.TTL_SECONDS and len(n) >= 40, (ttl, n)
    assert code == cliauth.code_for(n) and code.count("-") == 1, code
    assert cliauth.code_for("a") != cliauth.code_for("b"), "code varies with nonce"
    for typed in (" jeep_ionosphere ", "Jeep Ionosphere", "JEEP--IONOSPHERE", "jeep-ionosphere"):
        assert cliauth._normalize_code(typed) == "JEEP-IONOSPHERE", typed   # forgiving to humans...
    assert cliauth._normalize_code("JEEP-IONOSPHER") != "JEEP-IONOSPHERE", "...but a typo still fails"


# --- happy path: approve then claim EXACTLY once ---------------------------------------------

def test_approve_then_claim_exactly_once(env):
    n, code, _ = cliauth.new("MacBook-Pro-3")
    assert cliauth.claim(n) is None, "not approved yet -> pending"
    ok, err = cliauth.approve(n, code.lower(), "dev@x.com", "shared-secret")
    assert ok, err
    creds = cliauth.claim(n)
    assert creds and creds["email"] == "dev@x.com" and creds["token"].startswith("csa_"), creds
    assert creds["policy"]["redaction_min"] in ("secrets", "privacy", "strict"), creds
    assert orgs.verify(creds["token"]) == {"email": "dev@x.com", "org_id": "default"}
    assert cliauth.claim(n) is None, "SINGLE-USE: a second poll gets nothing"
    orgs.revoke(creds["token"])


# --- NEGATIVE: every way this must fail closed -----------------------------------------------

def test_approve_fail_closed(env):
    n2, code2, _ = cliauth.new("host2")
    assert cliauth.approve(n2, "WRONG-CODE", "dev@x.com", "shared-secret")[0] is False, "bad code -> deny"
    # A non-ASCII code must be a clean mismatch, never a TypeError from compare_digest (public route).
    assert cliauth.approve(n2, "café-señor", "dev@x.com", "shared-secret")[0] is False, "non-ASCII code -> deny, not crash"
    assert cliauth.approve(n2, "🔑", "dev@x.com", "shared-secret")[0] is False, "emoji code -> deny, not crash"
    assert cliauth.approve(n2, code2, "dev@x.com", "wrong-invite")[0] is False, "bad invite -> deny"
    assert cliauth.approve(n2, code2, "", "shared-secret")[0] is False, "no email -> deny"
    assert cliauth.approve(n2, code2, "not-an-email", "shared-secret")[0] is False, "bad email -> deny"
    assert cliauth.approve("no-such-nonce", code2, "dev@x.com", "shared-secret")[0] is False, "unknown nonce -> deny"
    assert cliauth.claim(n2) is None, "a denied approve leaves NO credentials"
    assert cliauth.approve(n2, code2, "dev@x.com", "shared-secret")[0] is True, "still approvable after failures"
    assert cliauth.approve(n2, code2, "other@x.com", "shared-secret")[0] is False, "already approved -> deny"
    c2 = cliauth.claim(n2)
    assert c2["email"] == "dev@x.com", "the second approve did NOT overwrite the first"
    orgs.revoke(c2["token"])


# --- what the table may hold, and what a restart costs ---------------------------------------

def test_the_table_holds_no_secret(env):
    """A read of control.db must not yield anything that can claim a pairing — before the Authorize
    click or after it. The nonce is stored as its sha256, and no token is ever written."""
    nonce, code, _ = cliauth.new("MacBook-Pro-3")
    assert [r["nonce_hash"] for r in _pairings()] == [orgs._hash(nonce)], "the nonce is hashed, like a token"
    assert cliauth.approve(nonce, code, "dev@x.com", "shared-secret")[0] is True
    row = _pairings()[0]
    assert row["approved_at"] and row["email"] == "dev@x.com" and row["org_id"] == "default"
    assert row["verified"] == 0, "an invite sign-in is not a verified identity"
    on_disk = " ".join(str(v) for v in row.values())
    assert nonce not in on_disk, "the secret that claims this pairing is not in the row"
    assert "csa_" not in on_disk, "and neither is a token"
    creds = cliauth.claim(nonce)
    assert creds["token"].startswith("csa_"), creds
    assert "csa_" not in " ".join(str(v) for r in _pairings() for v in r.values()), \
        "the claim mints the token and hands it straight back — it never lands here"
    orgs.revoke(creds["token"])


def test_a_restart_between_approve_and_claim_still_completes(env):
    """The point of the move: the Authorize click and the claiming poll may be served by different
    processes, so a restart (or a second api replica) mid-setup no longer strands the user."""
    nonce, code, _ = cliauth.new("MacBook-Pro-3")
    assert cliauth.approve(nonce, code, "dev@x.com", "shared-secret")[0] is True
    importlib.reload(cliauth)                            # every scrap of module state, gone
    assert not hasattr(cliauth, "_PENDING"), "no process-local handshake store may come back"
    creds = cliauth.claim(nonce)
    assert creds["email"] == "dev@x.com" and creds["token"].startswith("csa_"), creds
    assert cliauth.claim(nonce) is None, "and it is still exactly-once across the restart"
    orgs.revoke(creds["token"])


def test_the_handshake_writes_keep_control_db_in_rollback_journal(env):
    """The pairing writes are new control-plane writers, and control.db's on-disk format is pinned:
    routing one through db.engine_for would flip the file to persistent WAL (test_orgs_models.py)."""
    nonce, code, _ = cliauth.new("host")
    assert cliauth.approve(nonce, code, "dev@x.com", "shared-secret")[0] is True
    creds = cliauth.claim(nonce)
    con = sqlite3.connect(orgs.control_path())
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete", "control.db must stay rollback-journal"
    con.close()
    assert not os.path.exists(f"{orgs.control_path()}-wal"), "a -wal sibling means something WAL-ized control.db"
    orgs.revoke(creds["token"])


# --- expiry, and the prune that clears the table ---------------------------------------------

def test_nonce_expiry(env):
    n3, code3, _ = cliauth.new("host3")
    _backdate(n3, cliauth.TTL_SECONDS + 1)
    assert cliauth.approve(n3, code3, "dev@x.com", "shared-secret")[0] is False, "expired -> deny"
    assert cliauth.claim(n3) is None, "expired -> no credentials"
    assert len(_pairings()) == 1, "still on disk until something prunes it"
    cliauth.new("host4")
    assert [r["hostname"] for r in _pairings()] == ["host4"], "the next `new` prunes the expired row"


def test_a_claimed_row_is_pruned(env):
    n, code, _ = cliauth.new("host")
    assert cliauth.approve(n, code, "dev@x.com", "shared-secret")[0] is True
    creds = cliauth.claim(n)
    assert creds["token"].startswith("csa_")
    assert _pairings() == [], "a spent handshake is dropped by the claim itself, not left as a tombstone"
    orgs.revoke(creds["token"])


def test_a_failed_mint_leaves_the_handshake_claimable(env, monkeypatch):
    """The mint runs BEFORE the row is claimed. If it fails, the pairing must still be there: the CLI
    polls again two seconds later, rather than hearing about it as a 15-minute expiry timeout."""
    n, code, _ = cliauth.new("host")
    assert cliauth.approve(n, code, "dev@x.com", "shared-secret")[0] is True
    real, down = orgs.mint_token, [True]
    def flaky(*a, **k):
        if down[0]:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(orgs, "mint_token", flaky)
    assert cliauth.claim(n) is None, "a mint failure hands back no credentials"
    assert _pairings()[0]["claimed_at"] is None, "...and claims nothing"
    down[0] = False
    creds = cliauth.claim(n)
    assert creds and creds["token"].startswith("csa_"), "the next poll completes the pairing"
    orgs.revoke(creds["token"])


def test_racing_polls_yield_exactly_one_live_token(env):
    """Four polls on one approved pairing: one gets credentials, and no loser leaves a live token."""
    n, code, _ = cliauth.new("host")
    assert cliauth.approve(n, code, "race@x.com", "shared-secret")[0] is True
    out, start = [], threading.Barrier(4)
    def poll():
        start.wait()
        out.append(cliauth.claim(n))
    threads = [threading.Thread(target=poll) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [c for c in out if c]
    assert len(won) == 1, out
    live = [t for t in orgs.list_tokens() if t["email"] == "race@x.com" and t["active"]]
    assert len(live) == 1 and live[0]["token_hash"] == orgs._hash(won[0]["token"]), live
    orgs.revoke(won[0]["token"])


def test_unclaimed_handshakes_stay_bounded(env, monkeypatch):
    """The TTL bounds the table in normal use; _MAX_PAIRINGS bounds a flood on this public route."""
    monkeypatch.setattr(cliauth, "_MAX_PAIRINGS", 5)
    keep = [cliauth.new("flood")[0] for _ in range(12)][-5:]
    assert len(_pairings()) == 5, len(_pairings())
    assert {r["nonce_hash"] for r in _pairings()} == {orgs._hash(n) for n in keep}, \
        "oldest first: the survivors are the newest handshakes"


# --- web_login: same invite gate as approve(), no nonce --------------------------------------

def test_web_login(env):
    ok, tok = cliauth.web_login("web@x.com", "shared-secret")
    assert ok and tok.startswith("csa_"), (ok, tok)
    assert orgs.verify(tok) == {"email": "web@x.com", "org_id": "default"}, \
        "the cookie's token IS a per-user token"
    bad_invite = cliauth.web_login("web@x.com", "wrong-invite")
    blank_invite = cliauth.web_login("web@x.com", "")
    none_invite = cliauth.web_login("web@x.com", None)
    assert bad_invite[0] is False and blank_invite[0] is False and none_invite[0] is False, "invite is the gate"
    assert bad_invite[1] == blank_invite[1] == none_invite[1], \
        "wrong / blank / absent invite are ONE message — no oracle for which part was wrong"
    assert cliauth.web_login("", "shared-secret")[0] is False, "blank email -> deny (no unattributable token)"
    assert cliauth.web_login("not-an-email", "shared-secret")[0] is False, "malformed email -> deny"
    assert cliauth.web_login("x@" + "y" * 300 + ".com", "shared-secret")[0] is False, "overlong email -> deny"
    assert cliauth.web_login("café@x.com", "wrong")[0] is False, "non-ASCII + bad invite -> clean deny, never a raise"
    cliauth.web_login("web2@x.com", "shared-secret")
    assert _pairings() == [], "web_login uses no nonce handshake — it writes no pairing row"
    orgs.revoke(tok)


# --- the cookie itself: every attribute the security argument depends on ---------------------

def test_session_cookie_attributes(env):
    ok, tok = cliauth.web_login("web@x.com", "shared-secret")
    assert ok, tok
    c = cliauth.session_cookie(tok)
    assert c.startswith(auth.SESSION_COOKIE + "=" + tok), c
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=%d" % cliauth.SESSION_MAX_AGE):
        assert attr in c, "session cookie must carry %s: %s" % (attr, c)
    assert "Domain=" not in c, "__Host- forbids Domain (that is what blocks a sibling-subdomain plant)"
    assert auth.SESSION_COOKIE.startswith("__Host-"), "the prefix IS the anti-fixation control"
    assert auth._session_cookie_candidates(
        auth._CIHeaders({"Cookie": auth.SESSION_COOKIE + "=" + tok})) == [tok], "round-trips to the gate's reader"
    lo = cliauth.logout_cookie()
    assert "Max-Age=0" in lo and lo.startswith(auth.SESSION_COOKIE + "="), lo
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
        assert attr in lo, "logout cookie must MATCH session_cookie's attributes or it won't overwrite: %s" % lo
    assert auth._session_cookie_candidates(auth._CIHeaders({"Cookie": auth.SESSION_COOKIE + "="})) == [], \
        "the cleared cookie presents no candidate"
    orgs.revoke(tok)


# --- mint_gate: an issuer closes the invite path, and a verified identity beats a typed one --

def test_mint_gate_sso(env):
    orgs.adopt_default_org("Acme", domain="acme.com")
    orgs.set_sso("https://idp.example", "cid", None, "Acme Okta")
    # With an issuer the invite is not a quieter way in: it is refused outright.
    assert cliauth.mint_gate(None, "dev@x.com", "shared-secret")[2] == "This CSA requires signing in with Acme Okta."
    assert cliauth.approve(*[cliauth.new("h")[0]] + ["ANY-CODE", "dev@x.com", "shared-secret"])[0] is False
    assert cliauth.web_login("dev@x.com", "shared-secret")[0] is False, "the dashboard's invite path closes too"
    # ...and the typed email is NEVER what gets minted when an identity is present.
    got = cliauth.mint_gate(("boss@acme.com", "acme.com"), "victim@acme.com", "shared-secret")
    assert got == ("boss@acme.com", "default", None), got
    ok, tok = cliauth.sso_login(("boss@acme.com", "acme.com"))
    assert ok and orgs.verify(tok)["email"] == "boss@acme.com", \
        "the token must carry the issuer's email, never a form's; that is the whole point of SSO"
    assert [t for t in orgs.list_tokens() if orgs._hash(tok) == t["token_hash"]][0]["hostname"] == "web-sso"
    orgs.revoke(tok)
    # The word code still gates the CLI flow: the issuer proves WHO, the code proves WHICH terminal.
    n4, code4, _ = cliauth.new("host4")
    session = ("boss@acme.com", "default", True)
    assert cliauth.approve(n4, "WRONG-CODE", "", "", session)[0] is False, \
        "a valid sign-in must NOT bypass the terminal binding (phished-link defence)"
    assert cliauth.approve(n4, code4, "", "", session)[0] is True
    c4 = cliauth.claim(n4)
    assert c4["email"] == "boss@acme.com"
    orgs.revoke(c4["token"])
    # An identity the issuer vouches for but no org claims is still a deny.
    assert cliauth.mint_gate(("outsider@evil.com", "evil.com"), "", "")[0] is None, \
        "verified, but no org claims that domain -> deny"
    assert cliauth.mint_gate(("solo@gmail.com", None), "", "")[0] is None, "a consumer account has no claim"


# --- the page reflects the configured mode ---------------------------------------------------

def test_page_reflects_configured_mode(env):
    cfg = auth.SsoConfig("https://idp.example", "cid", None, "Acme Okta")
    p_sso = cliauth.page("n", "host", cfg=cfg)
    assert b"Sign in with Acme Okta" in p_sso and b'<div class="sso-box">' in p_sso
    assert b'href="/api/sso/start?return=%2Fcli-auth%3Fnonce%3Dn%26hostname%3Dhost"' in p_sso, \
        "the round trip brings the browser back to this handshake"
    assert b'name="invite"' not in p_sso, "an issuer -> no invite field to walk around it"
    assert b"<script src=" not in p_sso and b"accounts.google.com" not in p_sso, "nothing external loads"
    p_off = cliauth.page("n", "host")
    assert b'name="invite"' in p_off and b'<div class="sso-box">' not in p_off
    assert b"<script src=" not in p_off
    p_mine = cliauth.page("n", "host", "dev@acme.com", cfg)
    assert p_mine.index(b"Authorize as dev@acme.com") < p_mine.index(b"<details"), "the session comes first"
    assert b"Sign in with Acme Okta" in p_mine, "...and the issuer is folded below it"


# --- page: attacker-controlled query string cannot inject ------------------------------------

def test_page_escapes_attacker_input(env):
    # Both values are attacker-controlled (they come from the query string). The property is that
    # no INJECTED MARKUP survives — the same bytes appearing as escaped text are inert.
    p = cliauth.page('"><script>alert(1)</script>', '<img src=x onerror=alert(1)>')
    assert b"<script>alert(1)</script>" not in p, "nonce must not close the script block"
    assert b"</script><" not in p.replace(b"</script>\n</body>", b""), "no early script close"
    assert b"<img" not in p, "hostname must not inject a tag"
    assert b"&lt;img src=x onerror=alert(1)&gt;" in p, "...it is escaped into text instead"
    assert b"\\u003cscript\\u003e" in p, "nonce reaches JS as an escaped literal"
    assert b"Authorize" in p and b"WORD-WORD" in p


def test_a_pairing_request_cannot_claim_a_dashboard_hostname(env):
    """`web` and `web-sso` decide whether a token counts as a workspace admin that can change
    membership. A phished /cli-auth link with hostname=web, approved by a real admin over Google,
    would otherwise mint a bearer that reads as a browser session and dodges set_role's demotion."""
    for claimed in ("web", "WEB", " web-sso "):
        nonce, code, _ttl = cliauth.new(claimed)
        assert cliauth.approve(nonce, code, "dev@x.com", "shared-secret")[0] is True
        creds = cliauth.claim(nonce)
        host = [t for t in orgs.list_tokens() if orgs._hash(creds["token"]) == t["token_hash"]][0]["hostname"]
        assert host.endswith(" (cli)") and host.lower() not in orgs.BROWSER_HOSTNAMES, claimed
        orgs.revoke(creds["token"])
    nonce, code, _ttl = cliauth.new("mara-mac")
    assert cliauth.approve(nonce, code, "dev@x.com", "shared-secret")[0] is True
    creds = cliauth.claim(nonce)
    assert [t for t in orgs.list_tokens() if orgs._hash(creds["token"]) == t["token_hash"]][0]["hostname"] \
        == "mara-mac", "an ordinary machine name is untouched"
    orgs.revoke(creds["token"])
