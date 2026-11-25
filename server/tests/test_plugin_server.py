"""What the plugin needs from the server, over real HTTP: the consent policy on the handshake, a page
that states the consent of the mode the pairing was started for, and a token revoking itself."""
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import apiserver
from analyzer import cliauth, llm, orgs, store, tenant

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def box(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_AUTH_TOKEN", "shared-secret")
    monkeypatch.delenv("CSA_ORG_INVITE", raising=False)
    monkeypatch.setattr(llm, "probe", lambda provider, model: None)
    orgs.adopt_default_org("Acme", domain="acme.test")
    store.sqlite_db(store.org_db(store.DEFAULT_ORG)).close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(base, path, body=None, token=None):
    headers = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    return status, (raw.decode() if path.startswith("/cli-auth") else json.loads(raw))


def start(base, shipping):
    return call(base, "/api/cli-auth/new", {"hostname": "laptop", "shipping": shipping})[1]


def test_the_consent_policy_constant_is_the_go_clients():
    go = (REPO / "client" / "config.go").read_text()
    assert int(re.search(r"const requiredPolicyVersion = (\d+)", go).group(1)) == cliauth.CONSENT_POLICY


def test_new_and_claim_carry_the_consent_policy_and_the_mode(box):
    tenant.set_config(store.org_db(store.DEFAULT_ORG), archive_days=30)
    s = start(box, "plugin")
    assert s["consent_policy"] == cliauth.CONSENT_POLICY
    st, reply = call(box, "/api/cli-auth/approve", {"nonce": s["nonce"], "code": s["code"], "ack_days": 30,
                                                    "email": "sam@acme.test", "invite": orgs.rotate_invite(store.DEFAULT_ORG)})
    assert st == 200 and reply["ok"], reply
    creds = call(box, "/api/cli-auth/" + s["nonce"])[1]
    assert creds["status"] == "approved" and creds["consent_policy"] == cliauth.CONSENT_POLICY
    assert creds["shipping"] == "plugin" and creds["policy"]["archive_days"] == 30


def test_an_unknown_mode_is_csa_client(box):
    s = start(box, "<script>alert(1)</script>")
    assert cliauth.shipping_of(s["nonce"]) == ""
    assert "alert(1)" not in call(box, "/cli-auth?nonce=" + s["nonce"])[1]


def test_the_page_states_the_consent_of_the_mode_in_the_row(box):
    tenant.set_config(store.org_db(store.DEFAULT_ORG), archive_days=30)
    manual = call(box, "/cli-auth?nonce=" + start(box, "plugin-manual")["nonce"])[1]
    assert "Authorizing is your consent" in manual and "/csa:send-session" in manual and "recall is off" in manual
    assert "for 30 days" in manual and "Every new Claude Code session" not in manual
    auto = call(box, "/cli-auth?nonce=" + start(box, "plugin")["nonce"])[1]
    assert "Every new Claude Code session" in auto and "/csa:disconnect" in auto and "for 30 days" in auto
    assert "Verification code printed by <code>/csa:connect</code>" in auto
    go = call(box, "/cli-auth?nonce=" + start(box, "")["nonce"])[1]
    assert "csa-client uninstall" in go and "Authorizing is your consent" not in go
    edited = call(box, "/cli-auth?shipping=plugin-manual&nonce=" + start(box, "plugin")["nonce"])[1]
    assert "Every new Claude Code session" in edited, "the link cannot change what the page promises"


def test_a_plugin_click_must_have_seen_the_workspace_retention(box):
    tenant.set_config(store.org_db(store.DEFAULT_ORG), archive_days="forever")
    invite = orgs.rotate_invite(store.DEFAULT_ORG)
    s = start(box, "plugin")
    body = {"nonce": s["nonce"], "code": s["code"], "email": "sam@acme.test", "invite": invite}
    reply = call(box, "/api/cli-auth/approve", dict(body, ack_days=None))[1]
    assert reply["needs_ack"] and reply["archive_days"] == -1 and "deletes it" in reply["error"]
    assert call(box, "/api/cli-auth/" + s["nonce"])[1] == {"status": "pending"}, "no credential yet"
    assert call(box, "/api/cli-auth/approve", dict(body, ack_days=-1))[1]["ok"]
    assert call(box, "/api/cli-auth/" + s["nonce"])[1]["status"] == "approved"


def test_csa_client_pairings_ask_for_no_retention_ack(box):
    tenant.set_config(store.org_db(store.DEFAULT_ORG), archive_days="forever")
    s = start(box, "")
    reply = call(box, "/api/cli-auth/approve", {"nonce": s["nonce"], "code": s["code"], "email": "sam@acme.test",
                                                "invite": orgs.rotate_invite(store.DEFAULT_ORG)})[1]
    assert reply["ok"], reply


def test_a_token_revokes_itself_and_nothing_else(box):
    mine = orgs.mint_token("sam@acme.test", store.DEFAULT_ORG, "laptop")
    other = orgs.mint_token("sam@acme.test", store.DEFAULT_ORG, "desktop")
    assert call(box, "/api/token/revoke", {"token_hash": orgs._hash(other)}, token=mine)[1] == \
        {"ok": True, "revoked": True}
    assert call(box, "/api/team", token=mine)[0] == 401
    assert call(box, "/api/team", token=other)[0] == 200


def test_self_revoke_needs_a_bearer_and_refuses_a_cookie(box):
    assert call(box, "/api/token/revoke", {})[0] == 401
    assert "/api/token/revoke" not in apiserver._COOKIE_POST_OK
    assert not apiserver._public("/api/token/revoke")
    req = urllib.request.Request(box + "/api/token/revoke", data=b"{}", headers={
        "Cookie": "__Host-csa_session=" + orgs.mint_token("sam@acme.test", store.DEFAULT_ORG, "web"),
        "Sec-Fetch-Site": "same-origin"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_auth_off_has_nothing_of_its_own_to_revoke(box, monkeypatch):
    monkeypatch.setenv("CSA_AUTH", "off")
    assert call(box, "/api/token/revoke", {})[1] == {"ok": True, "revoked": False}


def test_a_service_token_and_the_last_admin_are_refused(box):
    svc = orgs.mint_token("agent@acme.test", store.DEFAULT_ORG, "agent", service=True)
    assert call(box, "/api/token/revoke", {}, token=svc)[0] == 403
    assert call(box, "/api/team", token=svc)[0] == 200
    admin = orgs.mint_token("dana@acme.test", store.DEFAULT_ORG, "laptop", role="admin")
    st, reply = call(box, "/api/token/revoke", {}, token=admin)
    assert st == 409 and reply["code"] == "last_admin"
    assert call(box, "/api/team", token=admin)[0] == 200
    orgs.mint_token("dana@acme.test", store.DEFAULT_ORG, "desktop", role="admin")
    assert call(box, "/api/token/revoke", {}, token=admin)[1]["revoked"] is True
