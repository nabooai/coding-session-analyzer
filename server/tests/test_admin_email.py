"""No SSO: the typed sign-in admits only the workspace's domain, and workspace.adminEmail is the first
admin, reaching the Admin tab by a one-person link that `csa.py migrate` prints."""
import http.client
import json
import re
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

import apiserver
from analyzer import auth, bootstrap, cliauth, orgs, store
from tests.test_sso import Box


@pytest.fixture()
def box(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    auth._discovery.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    monkeypatch.setenv("CSA_PUBLIC_URL", base)
    path = tmp_path / "config.json"
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", path)
    path.write_text(json.dumps({"workspace": {"name": "Acme", "domain": "acme.test", "adminEmail": "Boss@Acme.test"}}))
    yield Box(base, None)
    srv.shutdown()
    srv.server_close()


def _link(changes):
    (line,) = [c for c in changes if c.startswith("admin sign-in link")]
    return re.search(r"/join/(\S+)$", line).group(1)


def _cookie(hdrs):
    return [v for k, v in hdrs if k == "Set-Cookie" and v.startswith(auth.SESSION_COOKIE)][0].split(";")[0]


def _sign_in(box, email="", invite="", join=""):
    cookie = ""
    if join:
        _, hdrs, _ = box.get("/join/" + join)
        cookie = [v for k, v in hdrs if k == "Set-Cookie"][0].split(";")[0]
    return box.post("/api/ui-login", {"email": email, "invite": invite}, cookie=cookie)


def _role(box, hdrs):
    u = urllib.parse.urlsplit(box.base)
    c = http.client.HTTPConnection(u.hostname, u.port)
    c.request("GET", "/api/me", headers={"Cookie": _cookie(hdrs)})
    return json.loads(c.getresponse().read())["role"]


def test_the_admin_email_signs_in_by_its_link_and_is_an_admin(box):
    link = _link(bootstrap.apply())
    st, hdrs, _ = _sign_in(box, join=link)
    assert st == 200 and _role(box, hdrs) == "admin"
    cookie = _cookie(hdrs)
    assert box.get("/api/org/members", cookie=cookie)[0] == 200, "the Admin tab's data is reachable"
    assert box.get("/api/org/providers", cookie=cookie)[0] == 200
    st, _, body = box.post("/api/org/service-token/new", {"email": "agent@acme.test", "name": "ci"}, cookie=cookie)
    assert st == 200 and body["token"], "Service Tokens work from the admin's browser"
    assert box.post("/api/org/invite/new", {"email": "dana@acme.test"}, cookie=cookie)[0] == 200, "Members links too"


def test_the_admin_link_makes_an_admin_once(box):
    link = _link(bootstrap.apply())
    assert _role(box, _sign_in(box, join=link)[1]) == "admin"
    assert _role(box, _sign_in(box, join=link)[1]) == "member", "a leaked link is no longer an admin credential"


def test_everyone_else_stays_a_member(box):
    bootstrap.apply()
    invite = orgs.rotate_invite(store.DEFAULT_ORG)
    st, hdrs, _ = _sign_in(box, "dana@acme.test", invite)
    assert st == 200 and _role(box, hdrs) == "member"
    assert box.get("/api/org/members", cookie=_cookie(hdrs))[0] == 403
    st, hdrs, _ = _sign_in(box, "boss@acme.test", invite)
    assert st == 200 and _role(box, hdrs) == "member", "typing the admin's address with the shared invite is not enough"


def test_another_domain_is_refused_with_a_sentence(box):
    bootstrap.apply()
    invite = orgs.rotate_invite(store.DEFAULT_ORG)
    st, hdrs, body = _sign_in(box, "mallory@evil.test", invite)
    assert st == 401 and "admits only acme.test" in body["error"] and not [h for h in hdrs if h[0] == "Set-Cookie"]
    assert _sign_in(box, "carol@ACME.test", invite)[0] == 200


def test_a_workspace_without_a_domain_still_admits_any_address(box, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"workspace": {"name": "Acme"}}))
    bootstrap.apply()
    orgs.set_domain(store.DEFAULT_ORG, "")
    assert _sign_in(box, "x@elsewhere.test", orgs.rotate_invite(store.DEFAULT_ORG))[0] == 200


def test_a_second_deploy_changes_nothing_once_the_admin_is_in(box):
    st, hdrs, _ = _sign_in(box, join=_link(bootstrap.apply()))
    assert st == 200
    assert bootstrap.apply() == []
    assert [t["role"] for t in orgs.list_tokens(store.DEFAULT_ORG)] == ["admin"]


def test_an_unused_link_is_replaced_not_stacked(box):
    first = _link(bootstrap.apply())
    second = _link(bootstrap.apply())
    assert first != second and len(orgs.list_invites(store.DEFAULT_ORG)) == 1
    assert _sign_in(box, join=first)[0] == 401, "the replaced link is dead"
    assert _sign_in(box, join=second)[0] == 200


def test_sso_keeps_its_own_rules(box):
    orgs.adopt_default_org("Acme", domain="acme.test")
    orgs.set_sso("https://idp.test", "csa-client", None, "Okta", updated_by="test")
    assert [c for c in bootstrap.apply() if "sign-in link" in c] == []
    assert orgs.list_invites(store.DEFAULT_ORG) == []


@pytest.mark.parametrize("config, message", [
    ({"name": "Acme", "domain": "acme.test", "adminEmail": "boss@evil.test"}, "must be an address on acme.test"),
    ({"domain": "acme.test", "adminEmail": "boss@acme.test"}, "needs workspace.name"),
    ({"name": "Acme", "adminEmail": "boss"}, "must be an address"),
])
def test_a_bad_admin_email_stops_the_deploy(box, tmp_path, config, message):
    (tmp_path / "config.json").write_text(json.dumps({"workspace": config}))
    with pytest.raises(ValueError, match=message):
        bootstrap.apply()


def test_the_typed_gate_is_shared_with_the_laptop_pairing(box):
    bootstrap.apply()
    invite = orgs.rotate_invite(store.DEFAULT_ORG)
    assert cliauth.mint_gate(None, "mallory@evil.test", invite)[2].startswith("This CSA admits only acme.test")
    assert cliauth.mint_gate(None, "dana@acme.test", invite)[2] is None


def test_a_lost_admin_session_is_recovered_by_a_fresh_one_time_link(box):
    first = _link(bootstrap.apply())
    assert _role(box, _sign_in(box, join=first)[1]) == "admin"
    assert not [c for c in bootstrap.apply() if "sign-in link" in c], "a live session means no link at migrate"
    line = bootstrap.recovery_link()
    fresh = re.search(r"/join/(\S+)$", line).group(1)
    assert "boss@acme.test" in line
    assert not [t for t in orgs.list_tokens(store.DEFAULT_ORG) if t["active"]], "the lost browser session is gone"
    assert _role(box, _sign_in(box, join=fresh)[1]) == "admin"
    assert _role(box, _sign_in(box, join=fresh)[1]) == "member", "the link is one-time"


def test_recovery_refuses_when_the_admin_has_a_machine_token(box):
    bootstrap.apply()
    orgs.mint_token("boss@acme.test", store.DEFAULT_ORG, "laptop")
    with pytest.raises(ValueError, match="org role default boss@acme.test admin"):
        bootstrap.recovery_link()


def test_role_command_names_the_real_reason_for_a_web_only_admin(box, capsys):
    import csa
    _sign_in(box, join=_link(bootstrap.apply()))
    args = type("A", (), {"org_id": store.DEFAULT_ORG, "email": "boss@acme.test", "role": "admin"})
    with pytest.raises(SystemExit):
        csa.cmd_org_role(args)
    assert "org admin-link" in capsys.readouterr().out
