"""Chart values as a config file: `csa.py migrate` sets up the workspace and Okta, and an Okta group
decides who is admin, so a Helm install needs no command."""
import json
import sys

import pytest

import csa
from analyzer import auth, bootstrap, orgs, store
from tests.oidc_stub import OidcStub
from tests.test_sso import Box, _session, box  # noqa: F401  (box is the fixture)


@pytest.fixture(scope="module")
def idp():
    stub = OidcStub()
    yield stub
    stub.close()


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", tmp_path / "absent.json")
    auth._discovery.clear()
    c = orgs.control_db()
    yield c
    c.close()


def _values(monkeypatch, tmp_path, **sections):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(sections))
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", path)


def test_nothing_set_changes_nothing(con):
    assert bootstrap.apply(con) == []
    assert not orgs.default_is_adopted(con) and auth.sso_config(con) is None


def test_values_set_up_the_workspace_and_okta_once(con, monkeypatch, tmp_path, idp):
    idp.reset()
    _values(monkeypatch, tmp_path, workspace={"name": "Acme", "domain": "Acme.com"},
            okta={"issuer": idp.issuer, "clientId": "0oa-acme"})
    assert len(bootstrap.apply(con)) == 2
    (org,) = orgs.list_orgs(con=con)
    assert (org["id"], org["name"], org["domain"]) == (store.DEFAULT_ORG, "Acme", "acme.com")
    cfg = auth.sso_config(con)
    assert (cfg.issuer, cfg.client_id, cfg.display_name) == (idp.issuer, "0oa-acme", "Okta")
    assert bootstrap.apply(con) == [], "a second deploy with the same values writes nothing"


def test_a_changed_value_is_applied_on_the_next_deploy(con, monkeypatch, tmp_path, idp):
    idp.reset()
    _values(monkeypatch, tmp_path, workspace={"name": "Acme", "domain": "acme.com"},
            okta={"issuer": idp.issuer, "clientId": "0oa-acme"})
    bootstrap.apply(con)
    _values(monkeypatch, tmp_path, workspace={"name": "acme.com", "domain": "acme.io"},
            okta={"issuer": idp.issuer, "clientId": "0oa-new"})
    assert len(bootstrap.apply(con)) == 3
    (org,) = orgs.list_orgs(con=con)
    assert (org["name"], org["domain"]) == ("acme.com", "acme.io")
    assert auth.sso_config(con).client_id == "0oa-new"


def test_an_issuer_that_names_itself_differently_stops_the_deploy(con, monkeypatch, tmp_path, idp, capsys):
    idp.reset()
    idp.discovery_issuer = idp.issuer + "/other"
    _values(monkeypatch, tmp_path, okta={"issuer": idp.issuer, "clientId": "x"})
    monkeypatch.setattr(sys, "argv", ["csa", "migrate"])
    with pytest.raises(SystemExit) as e:
        csa.main()
    assert e.value.code == 1 and "migrate failed" in capsys.readouterr().err
    assert auth.sso_config(con) is None


def test_an_okta_group_decides_who_is_admin(box, idp, monkeypatch, tmp_path):  # noqa: F811
    _values(monkeypatch, tmp_path, okta={"adminGroup": "CSA Admins"})
    idp.groups = ["Everyone", "CSA Admins"]
    st, hdrs, _ = box.sign_in()
    assert st == 200 and orgs.lookup(_session(hdrs)[0].split("=", 1)[1])[1]["role"] == "admin"

    idp.email, idp.groups = "bob@acme.test", ["Everyone"]
    st, hdrs, _ = box.sign_in()
    assert orgs.lookup(_session(hdrs)[0].split("=", 1)[1])[1]["role"] == "member"

    idp.email, idp.groups = "alice@acme.test", ["Everyone"]
    box.sign_in()
    assert orgs._role_of("alice@acme.test", store.DEFAULT_ORG, orgs.control_db()) == "admin", \
        "the last admin is kept, not locked out"
    idp.email, idp.groups = "bob@acme.test", ["CSA Admins"]
    box.sign_in()
    idp.email, idp.groups = "alice@acme.test", []
    box.sign_in()
    assert orgs._role_of("alice@acme.test", store.DEFAULT_ORG, orgs.control_db()) == "member", \
        "removed from the group in Okta, demoted at her next sign-in"


def test_without_a_group_value_roles_stay_with_csa(box, idp):  # noqa: F811
    idp.groups = ["CSA Admins"]
    st, hdrs, _ = box.sign_in()
    assert orgs.lookup(_session(hdrs)[0].split("=", 1)[1])[1]["role"] == "member"


def test_the_telemetry_endpoint_comes_from_the_chart(con, monkeypatch, tmp_path):
    from analyzer import tenant
    _values(monkeypatch, tmp_path, telemetry={"otlpEndpoint": "https://otel.acme.test/"})
    assert bootstrap.apply(con) == ["telemetry to the OTLP endpoint"]
    assert tenant.deployment(con).otlp_endpoint == "https://otel.acme.test" and bootstrap.apply(con) == []
    _values(monkeypatch, tmp_path, telemetry={"otlpEndpoint": "otel.acme.test"})
    with pytest.raises(ValueError, match="telemetry.otlpEndpoint"):
        bootstrap.apply(con)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"okta": "x"}'])
def test_a_broken_config_file_stops_migrate_with_a_sentence(con, monkeypatch, tmp_path, capsys, text):
    path = tmp_path / "config.json"
    path.write_text(text)
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", path)
    monkeypatch.setattr(sys, "argv", ["csa", "migrate"])
    with pytest.raises(SystemExit) as e:
        csa.main()
    err = capsys.readouterr().err
    assert e.value.code == 1 and "migrate failed" in err and str(path) in err


def test_the_groups_scope_is_asked_for_only_with_an_admin_group(box, monkeypatch, tmp_path):  # noqa: F811
    import urllib.parse
    location, _ = box.start()
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["scope"] == ["openid email profile"]
    _values(monkeypatch, tmp_path, okta={"adminGroup": "CSA Admins"})
    location, _ = box.start()
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["scope"] == ["openid email profile groups"]


def test_a_token_without_a_groups_claim_is_logged_and_demotes_no_one(box, idp, monkeypatch, tmp_path, caplog):  # noqa: F811
    _values(monkeypatch, tmp_path, okta={"adminGroup": "CSA Admins"})
    idp.email, idp.groups = "bob@acme.test", ["CSA Admins"]
    box.sign_in()
    idp.email, idp.groups = "alice@acme.test", ["CSA Admins"]
    box.sign_in()
    idp.groups = None
    with caplog.at_level("ERROR", logger="analyzer.sso"):
        box.sign_in()
    assert "has no groups claim" in caplog.text and "okta.adminGroup" in caplog.text
    assert orgs._role_of("alice@acme.test", store.DEFAULT_ORG, orgs.control_db()) == "admin", "role left as it was"
    idp.groups = []
    box.sign_in()
    assert orgs._role_of("alice@acme.test", store.DEFAULT_ORG, orgs.control_db()) == "member", \
        "an empty list is a real answer: removed from the group"


def test_an_empty_config_mount_is_named_in_migrate_and_admin_link(con, monkeypatch, tmp_path, capsys):
    mount = tmp_path / "etc-csa"
    mount.mkdir()
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", mount / "config.json")
    monkeypatch.setattr(sys, "argv", ["csa", "migrate"])
    csa.main()
    assert "holds no config.json" in capsys.readouterr().err
    with pytest.raises(ValueError, match="shared folders"):
        bootstrap.recovery_link(con)
    monkeypatch.setattr(bootstrap, "CONFIG_PATH", tmp_path / "no-mount" / "config.json")
    assert bootstrap.missing_config() == "", "no mount at all is a box set up by command, not a mistake"
