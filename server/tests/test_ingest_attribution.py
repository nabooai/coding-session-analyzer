"""A service token may ship a session on behalf of another person: an `owner` email on
POST /api/ingest credits the session to that person while the token stays the `shipped_by`
credential. A per-user or SSO session cannot do this (a human can never credit a colleague).
`sessions.user` is an authz field, so the whole point is the gate: service token yes, human no."""
import pytest

from analyzer import archive, ingest, orgs, queue, store
from tests.test_invites import _post


@pytest.fixture()
def box(monkeypatch, tmp_path):
    """One workspace, org mode, as an on-prem box is."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    for k in ("CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
        monkeypatch.delenv(k, raising=False)
    orgs.adopt_default_org("Acme", domain="acme.test")
    store.sqlite_db(store.org_db(store.DEFAULT_ORG)).close()
    return tmp_path


def _capture(monkeypatch):
    """Stub the parse + persist so the test is about ATTRIBUTION, not transcript parsing: the queued
    session dict is captured for inspection."""
    seen = {}
    monkeypatch.setattr(ingest, "load_content", lambda sid, raw, **k: {"session_id": sid, "project": "p"})
    monkeypatch.setattr(ingest, "is_self_session", lambda s: False)
    monkeypatch.setattr(queue, "queue_pending", lambda sess, org_id=None: seen.update(sess) or "x")
    monkeypatch.setattr(archive, "put", lambda sess, org_id=None: None)
    return seen


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_session_owner_credits_the_person_and_keeps_the_token_as_shipper(box):
    con = store.sqlite_db(store.org_db(store.DEFAULT_ORG))
    try:
        assert store.session_owner(con, {"user": "atlas@acme.test", "on_behalf_of": "Dev@Acme.test"}) == (
            "dev@acme.test", "atlas@acme.test"), "user=the person, shipped_by=the token"
        assert store.session_owner(con, {"user": "atlas@acme.test"}) == ("atlas@acme.test", None), \
            "no on_behalf_of: the token's own session, shipped_by stays NULL"
    finally:
        con.close()


def test_a_service_token_ships_on_behalf_of_a_person(box, monkeypatch):
    seen = _capture(monkeypatch)
    svc = orgs.mint_service_token("default", "atlas@acme.test", "atlas")

    code, out = _post("/api/ingest", {"session_id": "s1", "content": "x", "owner": "Dev@Acme.test"}, _bearer(svc))
    assert code == 200 and out.get("queued"), out
    assert seen["user"] == "atlas@acme.test" and seen["on_behalf_of"] == "dev@acme.test"
    assert store.session_owner(store.sqlite_db(store.org_db("default")), dict(seen)) == (
        "dev@acme.test", "atlas@acme.test"), "credited to the developer, the token is shipped_by"

    code, out = _post("/api/ingest", {"session_id": "s2", "content": "x", "owner": "not-an-email"}, _bearer(svc))
    assert code == 400 and "valid email" in out["error"], out


def test_a_human_token_cannot_credit_a_colleague(box, monkeypatch):
    seen = _capture(monkeypatch)
    tok = orgs.mint_token("alice@acme.test", "default", "laptop", verified=True)

    code, out = _post("/api/ingest", {"session_id": "s3", "content": "x", "owner": "victim@acme.test"}, _bearer(tok))
    assert code == 200 and seen["user"] == "alice@acme.test", out
    assert "on_behalf_of" not in seen, "a human cannot delegate: the body owner is ignored"


def test_a_non_string_owner_is_handled_not_crashed(box, monkeypatch):
    """`owner` is attacker-controllable JSON: a non-string must be a clean answer, never an
    AttributeError on `.strip()` (do_POST's dispatch is not wrapped, so a raise drops the connection)."""
    _capture(monkeypatch)
    svc = orgs.mint_service_token("default", "atlas@acme.test", "atlas")
    code, out = _post("/api/ingest", {"session_id": "s4", "content": "x", "owner": 123}, _bearer(svc))
    assert code == 400 and "valid email" in out["error"], out  # coerced to "123", fails the email check

    tok = orgs.mint_token("alice@acme.test", "default", "laptop", verified=True)
    code, out = _post("/api/ingest", {"session_id": "s5", "content": "x", "owner": ["x"]}, _bearer(tok))
    assert code == 200, out  # a human token ignores the body owner; still no crash
