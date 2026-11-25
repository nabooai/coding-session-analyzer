"""How a model call runs with and without the Claude Code CLI in the image. No provider registered: the CLI when
it is there, whatever the credential; without it an API key goes over HTTPS and everything else fails, systemic,
with one sentence naming the fix. A subscription token never reaches the API."""
import io
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import apiserver
import csa
from analyzer import drain_loop, extract, llm, netguard, orgs, providers, queue, store, tenant
from tests.test_invites import _cookie, _Http, _post

ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
       "OPENAI_MAIN_MODEL", "OPENAI_SMALL_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
       "CLAUDE_CODE_USE_VERTEX")
BEDROCK = {"kind": "bedrock", "name": "Corp Bedrock", "region": "eu-central-1", "credential": "AKIA1:secret",
           "models": ["eu.anthropic.claude-sonnet-5"]}
VERTEX = {"kind": "vertex", "name": "Corp Vertex", "region": "us-east5", "project": "p", "credential": "{}",
          "models": ["claude-sonnet-5"]}
OK = {"model": "claude-sonnet-5", "content": [{"type": "text", "text": "OK"}],
      "usage": {"input_tokens": 5, "output_tokens": 1}}


class Endpoint(BaseHTTPRequestHandler):
    """A real socket, so the headers asserted are the ones on the wire. `reply` is (status, headers, body)."""
    calls = []
    reply = (200, {}, json.dumps(OK).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).calls.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        status, headers, out = type(self).reply
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture()
def endpoint(monkeypatch):
    monkeypatch.setattr(Endpoint, "calls", [])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def box(monkeypatch, tmp_path):
    """One workspace, no credential in the environment, no CLI on the PATH."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    for k in ENV + ("CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
        monkeypatch.delenv(k, raising=False)
    orgs.adopt_default_org("Acme", domain="acme.test")
    store.sqlite_db(store.org_db(store.DEFAULT_ORG)).close()
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: pytest.fail("the CLI must not run"))
    return tmp_path


def _with_cli(monkeypatch):
    """The CLI on the PATH, answering; returns the environments it was started with."""
    runs = []

    def run(argv, **kw):
        runs.append({"argv": argv, "env": kw["env"]})
        return type("P", (), {"returncode": 0, "stdout": json.dumps({"result": "from the cli"}), "stderr": ""})()
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", run)
    return runs


def _admin():
    return _cookie(orgs.mint_token("boss@acme.test", "default", "web-sso", verified=True, role="admin"))


def _view():
    h = _Http("/api/org/providers", {})
    h._org_get("/api/org/providers", {}, {"org_id": "default", "role": "admin", "email": "boss@acme.test"})
    return h.sent[1]


def _no_http(monkeypatch):
    monkeypatch.setattr(llm, "_open_operator", lambda *a: pytest.fail("nothing may leave for the API"))
    monkeypatch.setattr(llm, "_open", lambda *a: pytest.fail("nothing may leave for the API"))


def test_an_api_key_without_the_cli_goes_over_https_to_the_base_url(box, monkeypatch, endpoint):
    monkeypatch.setenv("ANTHROPIC_API_KEY", " sk-ant-env ")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    text, cost, usage = llm.complete("hi", "main")
    (call,) = Endpoint.calls
    assert (text, usage["model"], usage["input_tokens"]) == ("OK", "claude-sonnet-5", 5) and cost > 0
    assert call["path"] == "/v1/messages" and call["headers"]["x-api-key"] == "sk-ant-env"
    assert "authorization" not in call["headers"]


@pytest.mark.parametrize("role, model", [("main", "claude-sonnet-5"), ("small", "claude-haiku-4-5-20251001"),
                                         ("opus", "claude-opus-5"), ("claude-opus-4-8", "claude-opus-4-8")])
def test_the_api_gets_the_model_id_the_cli_would_have_sent_not_an_alias(box, monkeypatch, endpoint, role, model):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    llm.complete("hi", role)
    assert Endpoint.calls[0]["body"]["model"] == model


def test_an_openai_env_key_uses_guided_main_and_small_models(box, monkeypatch, endpoint):
    monkeypatch.setenv("OPENAI_API_KEY", " sk-openai-env ")
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    monkeypatch.setenv("OPENAI_MAIN_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_SMALL_MODEL", "gpt-5-mini")
    monkeypatch.setattr(llm, "_open", lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))
    monkeypatch.setattr(Endpoint, "reply", (200, {}, json.dumps({
        "model": "gpt-5", "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }).encode()))
    text, _, usage = llm.complete("hi", "main")
    assert (text, usage["model"]) == ("OK", "gpt-5")
    assert Endpoint.calls[0]["path"] == "/chat/completions"
    assert Endpoint.calls[0]["headers"]["authorization"] == "Bearer sk-openai-env"
    Endpoint.calls.clear()
    llm.complete("hi", "small")
    assert Endpoint.calls[0]["body"]["model"] == "gpt-5-mini"


def test_with_no_base_url_the_key_goes_to_anthropic(box, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    seen = {}

    def opened(req, timeout):
        seen["url"] = req.full_url
        raise OSError("no network in the test")
    monkeypatch.setattr(llm, "_open_operator", opened)
    with pytest.raises(OSError):
        llm.complete("hi", "main")
    assert seen["url"] == "https://api.anthropic.com/v1/messages"


def test_a_subscription_token_beside_a_key_stays_home(box, monkeypatch, endpoint):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-the-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    llm.complete("hi", "main")
    (call,) = Endpoint.calls
    assert call["headers"]["x-api-key"] == "the-key"
    assert "sk-ant-oat-the-token" not in json.dumps(call)


@pytest.mark.parametrize("var, value, what", [("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-the-token", "a login token"),
                                              ("CLAUDE_CODE_USE_BEDROCK", "1", "Amazon Bedrock"),
                                              ("CLAUDE_CODE_USE_VERTEX", "1", "Google Vertex")])
def test_a_credential_that_needs_the_cli_fails_with_the_fix_and_sends_nothing(box, monkeypatch, var, value, what):
    monkeypatch.setenv(var, value)
    _no_http(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        llm.complete("hi", "main")
    assert str(e.value).startswith(f"{what} needs Claude Code") and "--target with-claude-code" in str(e.value)
    assert llm.provider_summary() == f"NONE: {e.value}"
    assert llm.model_ready() is False and llm.install_credential() == "" and llm.blocked() == str(e.value)


@pytest.mark.parametrize("var", ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"])
def test_a_cloud_the_operator_asked_for_is_not_swapped_for_anthropic_because_a_key_is_there(box, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _no_http(monkeypatch)
    with pytest.raises(RuntimeError, match="needs Claude Code"):
        llm.complete("hi", "main")


@pytest.mark.parametrize("body", [BEDROCK, VERTEX])
def test_a_registered_provider_that_needs_the_cli_fails_with_the_fix(box, monkeypatch, body):
    p = providers.checked(body)
    with pytest.raises(RuntimeError, match=f"{providers.KINDS[p.kind]} needs Claude Code"):
        llm._run(providers.Target(p, p.models[0]), "hi", 5)
    assert llm.needs_cli(providers.Target(p, p.models[0]))


def test_nothing_set_up_says_so_as_a_systemic_failure(box):
    with pytest.raises(RuntimeError, match="no model is set up"):
        llm.complete("hi", "main")
    assert llm.provider_summary().startswith("NONE: register a provider") and llm.blocked() == ""


@pytest.mark.parametrize("var, value, installed", [("ANTHROPIC_API_KEY", "k", "an Anthropic API key"),
                                                   ("CLAUDE_CODE_OAUTH_TOKEN", "t", "a login token"),
                                                   ("CLAUDE_CODE_USE_BEDROCK", "1", "Amazon Bedrock"),
                                                   ("CLAUDE_CODE_USE_VERTEX", "1", "Google Vertex")])
def test_with_the_cli_every_environment_credential_still_runs_through_it(box, monkeypatch, var, value, installed):
    runs = _with_cli(monkeypatch)
    monkeypatch.setenv(var, value)
    _no_http(monkeypatch)
    text, _, usage = llm.complete("hi", "main")
    (run,) = runs
    assert text == "from the cli" and usage["model"] == "sonnet"
    assert run["argv"] == llm._claude_argv("sonnet") and run["env"] is None, "the process's own environment"
    assert llm.blocked() == "" and llm.needs_cli(providers.Target(None, "sonnet")) == ""
    assert llm.provider_summary() == f"Claude via {installed} from the environment (sonnet); nothing registered on AI Models"
    assert llm.model_ready() is True and llm.install_credential() == installed and llm.unavailable_kinds() == []


def test_with_the_cli_a_key_beside_the_token_still_goes_to_the_cli(box, monkeypatch):
    runs = _with_cli(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    _no_http(monkeypatch)
    llm.complete("hi", "small")
    assert runs[0]["argv"] == llm._claude_argv("haiku")


def test_the_probe_carries_the_whole_sentence(box, monkeypatch):
    p = providers.checked(BEDROCK)
    said = llm.probe(p.models[0], provider=p)
    assert "AWS Bedrock needs Claude Code" in said and "--target with-claude-code" in said
    assert said.endswith("Nothing was saved.")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert "--target with-claude-code" in llm.probe("main")


@pytest.mark.parametrize("reply, says", [
    ((200, {}, b"<html>a captive portal</html>"), "the model endpoint failed"),
    ((401, {}, json.dumps({"error": {"message": "invalid x-api-key"}}).encode()), "Anthropic said 401, invalid x-api-key"),
    ((503, {}, b"bad gateway"), "Anthropic said 503"),
])
def test_an_endpoint_that_misbehaves_is_systemic_never_content_poison(box, monkeypatch, endpoint, reply, says):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    monkeypatch.setattr(Endpoint, "reply", reply)
    with pytest.raises(RuntimeError, match=says):
        llm.complete("hi", "main")


@pytest.mark.parametrize("body", [{"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
                                  {"error": "the gateway is down"}, {"content": []},
                                  {"content": [], "stop_reason": "end_turn"}])
def test_a_200_with_no_answer_parks_the_queue_instead_of_quarantining_the_session(box, monkeypatch, endpoint, body):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    monkeypatch.setattr(Endpoint, "reply", (200, {}, json.dumps(body).encode()))
    with pytest.raises(RuntimeError, match="no text"):
        extract._run("hi", "main", 5)


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_an_empty_answer_this_session_caused_stays_content_poison(box, monkeypatch, endpoint, stop):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    monkeypatch.setattr(Endpoint, "reply", (200, {}, json.dumps({"content": [], "stop_reason": stop}).encode()))
    with pytest.raises(ValueError):
        extract._run("hi", "main", 5)


@pytest.mark.parametrize("body", [{"error": {"message": "busy"}}, {"choices": []},
                                  {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}])
def test_a_chat_endpoint_with_no_answer_is_systemic(box, monkeypatch, body):
    monkeypatch.setattr(llm, "_open", lambda req, timeout: io.BytesIO(json.dumps(body).encode()))
    p = providers.Provider("corp", "Corp", "openai", "", "", "", "", "k", ("m",))
    with pytest.raises(RuntimeError, match="no text"):
        llm._run(providers.Target(p, "m"), "hi", 5)


@pytest.mark.parametrize("finish", ["content_filter", "length"])
def test_an_empty_chat_answer_this_session_caused_stays_content(box, monkeypatch, finish):
    body = {"choices": [{"message": {"content": ""}, "finish_reason": finish}]}
    monkeypatch.setattr(llm, "_open", lambda req, timeout: io.BytesIO(json.dumps(body).encode()))
    p = providers.Provider("corp", "Corp", "openai", "", "", "", "", "k", ("m",))
    assert llm._run(providers.Target(p, "m"), "hi", 5)[0] == ""


def test_a_subscription_token_in_the_key_variable_is_never_sent(box, monkeypatch, endpoint):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-the-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    with pytest.raises(RuntimeError, match="subscription token"):
        llm.complete("hi", "main")
    assert Endpoint.calls == []


def test_a_subscription_token_in_a_provider_key_is_never_sent_with_or_without_the_cli(box, monkeypatch, endpoint):
    p = providers.Provider("corp", "Corp", "anthropic", endpoint, "", "", "", "sk-ant-oat01-the-token", ("m",))
    monkeypatch.setattr(llm, "_open", llm._open_operator)                     # a plain-http test endpoint
    for _ in ("without", "with"):
        with pytest.raises(RuntimeError, match="subscription token"):
            llm._run(providers.Target(p, "m"), "hi", 5)
        assert "sk-ant-oat01-the-token" not in llm.probe("m", provider=p), "the sentence does not echo it"
        _with_cli(monkeypatch)
    assert Endpoint.calls == []


def test_with_the_cli_a_token_in_the_key_variable_still_goes_only_to_the_cli(box, monkeypatch):
    runs = _with_cli(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-the-token")
    _no_http(monkeypatch)
    assert llm.complete("hi", "main")[0] == "from the cli" and len(runs) == 1


def test_with_the_cli_health_reads_nothing_extra(box, monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(providers, "target", lambda *a: pytest.fail("no control.db read on this path"))
    assert llm.blocked() == ""


def test_a_base_url_that_is_no_url_is_systemic(box, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "llm-gateway.example.com")
    with pytest.raises(RuntimeError, match="the model endpoint failed"):
        llm.complete("hi", "main")


def test_a_registered_endpoint_that_stops_resolving_is_systemic(box, monkeypatch):
    def gone(host, port):
        raise OSError
    monkeypatch.setattr(netguard, "resolve", gone)
    p = providers.Provider("corp", "Corp", "anthropic", "https://gateway.example.com", "", "", "", "k", ("m",))
    with pytest.raises(RuntimeError, match="did not resolve"):
        llm._run(providers.Target(p, "m"), "hi", 5)


def test_a_redirect_does_not_carry_the_key_away(box, monkeypatch, endpoint):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    monkeypatch.setattr(Endpoint, "reply", (302, {"Location": endpoint + "/elsewhere"}, b""))
    with pytest.raises(RuntimeError, match="Anthropic said 302"):
        llm.complete("hi", "main")
    assert [c["path"] for c in Endpoint.calls] == ["/v1/messages"]


def _queued(sid="s1"):
    queue.queue_pending({"session_id": sid, "transcript": "user: hello\nassistant: hi\n", "project": "p"},
                        org_id=store.DEFAULT_ORG)


def test_the_queue_parks_instead_of_poisoning_and_says_why(box, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    _no_http(monkeypatch)
    _queued()
    done, failed, poisoned, _, _ = csa.drain_org(store.DEFAULT_ORG, "main", 5, compact=False)
    assert done == [] and poisoned == [] and "needs Claude Code" in failed[0][1]
    assert queue.stage("s1", store.DEFAULT_ORG) == "queued", "still waiting, nothing quarantined"
    drain_loop._drain_one(store.DEFAULT_ORG, "main", 5)
    assert "needs Claude Code" in orgs.extraction_stalls()[store.DEFAULT_ORG]["reason"]


def test_health_reports_a_model_this_image_cannot_run_before_any_session_waits(box, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert queue.depth(store.DEFAULT_ORG)[0] == 0
    org = apiserver.extraction_status(store.DEFAULT_ORG)
    anonymous = apiserver.extraction_status()
    assert org["stalled"] is True and "a login token needs Claude Code" in org["reason"]
    assert anonymous["stalled"] is True and "needs Claude Code" in anonymous["reason"]
    assert "login token" not in anonymous["reason"], "the anonymous route does not say what the box runs on"


def test_health_is_unchanged_with_the_cli(box, monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert apiserver.extraction_status() == {"stalled": False, "reason": ""}


@pytest.mark.parametrize("test", [True, False])
@pytest.mark.parametrize("body, label", [(BEDROCK, "AWS Bedrock"), (VERTEX, "Google Vertex")])
def test_the_page_says_why_it_cannot_save_a_kind_this_image_cannot_run(box, body, label, test):
    code, out = _post("/api/org/provider/save", {**body, "test": test}, _admin())
    assert code == 400 and out["error"].startswith(f"{label} needs Claude Code") and providers.list_all() == []


def test_the_view_names_what_this_image_cannot_run(box, monkeypatch):
    v = _view()
    assert v["unavailable_kinds"] == ["bedrock", "vertex"] and v["blocked"] == "" and v["installed"] == ""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    v = _view()
    assert "a login token needs Claude Code" in v["blocked"] and v["default"].startswith("NONE: ")
    assert v["installed"] == "", "the role pickers do not offer a credential that cannot run"


def test_a_stored_provider_this_image_cannot_run_is_flagged_once_a_role_uses_it(box):
    providers.save(providers.checked(BEDROCK), "t")
    assert llm.blocked() == "", "no role uses it yet"
    tenant.set_deployment(orgs.control_db(), small_model=f"corp-bedrock/{BEDROCK['models'][0]}")
    assert "AWS Bedrock needs Claude Code" in llm.blocked()
    assert llm.model_ready() is False, "the small role is blocked; main has no credential either"
    tenant.set_deployment(orgs.control_db(), main_model=f"corp-bedrock/{BEDROCK['models'][0]}")
    assert llm.model_ready() is False and llm.provider_summary().startswith("NONE: AWS Bedrock needs")


def test_a_key_runs_and_the_view_offers_it(box, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    v = _view()
    assert v["installed"] == "an Anthropic API key" and v["blocked"] == "" and "from the environment" in v["default"]
    assert llm.model_ready() is True
