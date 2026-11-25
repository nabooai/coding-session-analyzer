"""AI Models: providers an admin registers, the two roles that pick a model from them, and the
credential each call runs with. The routes are driven through do_POST/_org_get, as the page calls them."""
import json
import os

import pytest

from analyzer import llm, netguard, orgs, providers, store, tenant
from tests.test_invites import _cookie, _Http, _post

ANTHROPIC = {"kind": "anthropic", "name": "Acme Anthropic", "credential": "sk-ant-1",
             "models": ["claude-sonnet-4-5", "claude-haiku-4-5"]}


@pytest.fixture()
def box(monkeypatch, tmp_path):
    """One workspace, as an on-prem box is."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    for k in ("CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
        monkeypatch.delenv(k, raising=False)
    orgs.adopt_default_org("Acme", domain="acme.test")
    store.sqlite_db(store.org_db(store.DEFAULT_ORG)).close()
    monkeypatch.setattr(llm, "probe", lambda ref, **k: None)
    monkeypatch.setattr(netguard, "resolve", lambda host, port: ["93.184.216.34"])
    return tmp_path


def _admin():
    return _cookie(orgs.mint_token("boss@acme.test", "default", "web-sso", verified=True, role="admin"))


def _view():
    h = _Http("/api/org/providers", {})
    h._org_get("/api/org/providers", {}, {"org_id": "default", "role": "admin", "email": "boss@acme.test"})
    return h.sent[1]


@pytest.mark.parametrize("body, says", [
    ({**ANTHROPIC, "kind": "gemini"}, "provider type"),
    ({**ANTHROPIC, "name": ""}, "Name the provider"),
    ({**ANTHROPIC, "models": []}, "at least one model"),
    ({**ANTHROPIC, "credential": ""}, "needs a key"),
    ({"kind": "bedrock", "name": "b", "credential": "AKIA:s", "models": ["m"]}, "needs region"),
    ({"kind": "vertex", "name": "v", "credential": "{}", "region": "us-east5", "models": ["m"]}, "needs project"),
    ({"kind": "vertex", "name": "v", "credential": "not json", "region": "r", "project": "p", "models": ["m"]},
     "JSON key file"),
    ({"kind": "azure_openai", "name": "a", "credential": "k", "models": ["gpt-4o"]}, "needs endpoint"),
    ({"kind": "openai", "name": "o", "credential": "k", "endpoint": "http://x.test", "models": ["m"]}, "https://"),
])
def test_a_provider_the_form_cannot_store_gets_a_sentence(box, body, says):
    with pytest.raises(ValueError, match=says.replace("(", r"\(")):
        providers.checked(body)


def test_the_credential_is_stored_but_never_shown_and_kept_on_edit(box):
    code, out = _post("/api/org/provider/save", ANTHROPIC, _admin())
    assert code == 200, out
    (p,) = out["providers"]
    assert p["id"] == "acme-anthropic" and p["credential_set"] is True and "credential" not in p
    assert "sk-ant-1" not in json.dumps(out)
    code, out = _post("/api/org/provider/save", {**ANTHROPIC, "id": "acme-anthropic", "credential": "",
                                                 "models": ["claude-opus-4-5"]}, _admin())
    assert code == 200 and providers.get("acme-anthropic").credential == "sk-ant-1", "blank keeps the key"
    assert out["providers"][0]["models"] == ["claude-opus-4-5"]


def test_a_provider_that_does_not_answer_is_not_saved(box, monkeypatch):
    monkeypatch.setattr(llm, "probe", lambda ref, **k: f"{ref} did not answer: 401. Nothing was saved.")
    code, out = _post("/api/org/provider/save", ANTHROPIC, _admin())
    assert code == 400 and "401" in out["error"] and providers.list_all() == []


def test_roles_fall_back_to_the_environment_until_one_is_picked(box):
    assert providers.target("main") == providers.Target(None, "sonnet")
    assert providers.target("small") == providers.Target(None, "haiku")
    _post("/api/org/provider/save", ANTHROPIC, _admin())
    code, out = _post("/api/org/models", {"main_model": "acme-anthropic/claude-sonnet-4-5",
                                          "small_model": "acme-anthropic/claude-haiku-4-5"}, _admin())
    assert code == 200 and out["main_model"] == "acme-anthropic/claude-sonnet-4-5", out
    t = providers.target("main")
    assert (t.provider.id, t.model) == ("acme-anthropic", "claude-sonnet-4-5")
    assert providers.target("claude-opus-4-5") == providers.Target(None, "claude-opus-4-5"), "the CLI override"


def test_removing_a_provider_returns_its_roles_to_the_environment(box):
    _post("/api/org/provider/save", ANTHROPIC, _admin())
    _post("/api/org/models", {"main_model": "acme-anthropic/claude-sonnet-4-5"}, _admin())
    code, out = _post("/api/org/provider/remove", {"id": "acme-anthropic"}, _admin())
    assert code == 200 and out["providers"] == [] and out["main_model"] == ""
    assert providers.target("main") == providers.Target(None, "sonnet")


def test_each_claude_provider_runs_with_only_its_own_credential(box, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "the-helm-default")
    assert providers.claude_env(None) == (None, None), "the Helm default inherits the process env"
    env, _ = providers.claude_env(providers.checked({"kind": "bedrock", "name": "b", "region": "eu-central-1",
                                                     "credential": "AKIA1:se:cret", "models": ["m"]}))
    assert (env["CLAUDE_CODE_USE_BEDROCK"], env["AWS_REGION"], env["AWS_ACCESS_KEY_ID"],
            env["AWS_SECRET_ACCESS_KEY"]) == ("1", "eu-central-1", "AKIA1", "se:cret")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env, "the Helm default's credential never rides along"
    sa = json.dumps({"type": "service_account", "project_id": "p"})
    env, keyfile = providers.claude_env(providers.checked({"kind": "vertex", "name": "v", "region": "us-east5",
                                                           "project": "p", "credential": sa, "models": ["m"]}))
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == keyfile and open(keyfile).read() == sa
    os.unlink(keyfile)


def test_the_vertex_key_file_is_removed_after_the_call(box, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["path"] = kw["env"]["GOOGLE_APPLICATION_CREDENTIALS"]
        seen["existed"] = os.path.exists(seen["path"])
        return type("P", (), {"returncode": 0, "stdout": json.dumps({"result": "OK"}), "stderr": ""})()
    monkeypatch.setattr(llm.subprocess, "run", run)
    p = providers.checked({"kind": "vertex", "name": "v", "region": "r", "project": "p", "credential": "{}",
                           "models": ["m"]})
    assert llm._claude("hi", "m", 5, p)[0] == "OK"
    assert seen["existed"] and not os.path.exists(seen["path"])


def test_azure_addresses_the_deployment_in_the_path(box, monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_chat", lambda url, headers, body, timeout: calls.append((url, headers, body)) or ("ok", None, {}))
    p = providers.checked({"kind": "azure_openai", "name": "a", "credential": "az-key", "models": ["gpt-4o prod"],
                           "endpoint": "https://acme.openai.azure.com"} | {"models": ["gpt-4o"]})
    llm._azure("hi", "gpt-4o", 5, p)
    url, headers, body = calls[0]
    assert url == "https://acme.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
    assert headers == {"api-key": "az-key"} and "model" not in body


def test_only_an_admin_on_a_one_workspace_server_may_change_providers(box):
    member = _cookie(orgs.mint_token("dev@acme.test", "default", "web-sso", verified=True))
    assert _post("/api/org/provider/save", ANTHROPIC, member)[0] == 403
    assert _post("/api/org/provider/save", ANTHROPIC, _cookie(
        orgs.mint_token("boss@acme.test", "default", "web-sso", verified=True, role="admin"), "cross-site"))[0] == 401
    orgs._create_org("Other", "other")
    code, out = _post("/api/org/provider/save", ANTHROPIC, _admin())
    assert code == 403 and "several workspaces" in out["error"] and providers.list_all() == []
    assert _view()["editable"] is False


def test_the_view_says_what_runs_before_anything_is_registered(box, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    v = _view()
    assert v["providers"] == [] and v["main_model"] == "" and "from the environment" in v["default"]
    assert set(v["kinds"]) == {"anthropic", "bedrock", "vertex", "openai", "azure_openai"}
    assert tenant.deployment().main_model == ""
    assert v["installed"] == "an Anthropic API key"


def test_a_model_is_ready_only_when_the_main_role_has_something_to_call(box, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(k, raising=False)
    assert (_view()["installed"], llm.model_ready()) == ("", False)
    _post("/api/org/provider/save", ANTHROPIC, _admin())
    assert not llm.model_ready(), "a provider no role uses leaves the main role on the absent install credential"
    _post("/api/org/models", {"main_model": "acme-anthropic/claude-sonnet-4-5"}, _admin())
    assert llm.model_ready()
    _post("/api/org/models", {"main_model": ""}, _admin())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert llm.model_ready()


def test_an_anthropic_provider_calls_the_api_with_its_own_key_not_the_cli(box, monkeypatch):
    """The CLI prefers a login stored on the machine over a key it is handed, so a registered key
    could be silently bypassed: Anthropic goes straight to the Messages API."""
    import io
    sent = {}

    def urlopen(req, timeout):
        sent.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data))
        return io.BytesIO(json.dumps({"model": "claude-sonnet-4-5-x", "content": [{"type": "text", "text": "OK"}],
                                      "usage": {"input_tokens": 5, "output_tokens": 1}}).encode())
    monkeypatch.setattr(llm, "_open", urlopen)
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: pytest.fail("the CLI must not run"))
    text, cost, usage = llm._run(providers.Target(providers.checked(ANTHROPIC), "claude-sonnet-4-5"), "hi", 5)
    assert (text, cost, usage["input_tokens"], usage["model"]) == ("OK", 3e-05, 5, "claude-sonnet-4-5-x")
    assert sent["url"] == "https://api.anthropic.com/v1/messages" and sent["headers"]["X-api-key"] == "sk-ant-1"
    assert sent["body"]["model"] == "claude-sonnet-4-5"


def test_the_ledger_records_the_model_a_role_ran_on_not_the_role(box, monkeypatch):
    import io
    gemini = {"kind": "openai", "name": "Gemini", "credential": "g-key", "models": ["gemini-2.5-flash"],
              "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai"}
    assert _post("/api/org/provider/save", gemini, _admin())[0] == 200
    assert _post("/api/org/models", {"main_model": "gemini/gemini-2.5-flash"}, _admin())[0] == 200
    monkeypatch.setattr(llm, "_open", lambda req, timeout: io.BytesIO(json.dumps(
        {"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 1297, "completion_tokens": 289}}).encode()))
    text, cost, usage = llm.complete("hi", "main")
    con = store.sqlite_db(store.org_db(store.DEFAULT_ORG))
    store.record_cost(con, "extract-full", "main", "s1", usage, cost)
    assert con.execute("SELECT model, cost_usd FROM costs").fetchall() == [("gemini-2.5-flash", 0.001112)]


def test_bedrock_takes_an_api_key_or_an_access_key_pair(box):
    """A Bedrock API key (what a customer's test workspace uses) rides AWS_BEARER_TOKEN_BEDROCK; a pair the two AWS vars."""
    env, _ = providers.claude_env(providers.checked({"kind": "bedrock", "name": "b", "region": "eu-central-1",
                                                     "credential": "ABSKbearer", "models": ["m"]}))
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == "ABSKbearer" and "AWS_ACCESS_KEY_ID" not in env
