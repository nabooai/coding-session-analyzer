"""GET/POST /api/config over real HTTP, and the no-restart property for each process that reads a
setting: the api, the mcp server and the etl loop read the row per use, as SSO does
(test_auth.py::test_the_issuer_is_read_from_control_db_on_every_call)."""
import contextlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import apiserver
import csa
from analyzer import cliauth, drain_loop, llm, orgs, store, tenant


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """org mode, `default` adopted as Acme (one workspace), an admin and a member, over real HTTP."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "probe", lambda provider, model: None)
    orgs.adopt_default_org("Acme", domain="acme.test")
    store.sqlite_db(store.org_db(store.DEFAULT_ORG)).close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": "http://127.0.0.1:%d" % srv.server_address[1],
           "admin": orgs.mint_token("dana@acme.test", store.DEFAULT_ORG, role="admin"),
           "member": orgs.mint_token("sam@acme.test", store.DEFAULT_ORG)}
    srv.shutdown()
    srv.server_close()


def call(box, who, body=None, headers=None):
    h = {"Authorization": "Bearer " + box[who]} if who else {}
    h.update(headers or {})
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(box["base"] + "/api/config", headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ConnectionResetError:                           # the status arrived, the body raced the reset
            return e.code, {}
    except (ConnectionResetError, urllib.error.URLError):     # refused at the gate before the body was read
        return "reset", {}


def fields(body):
    return {f["name"]: f for f in body["fields"]}


def test_a_member_gets_403_on_both_verbs_and_nothing_changes(box):
    assert call(box, "member")[0] == 403
    st, body = call(box, "member", {"daily_usd": 1})
    assert st == 403 and "admin" in body["error"]
    assert tenant.get_config(store.org_db()).daily_usd == 0.0
    assert call(box, None)[0] == 401


def test_the_admin_reads_every_field_with_its_source(box):
    st, body = call(box, "admin")
    assert st == 200 and body["deployment_editable"] is True, "one workspace: its admin is the operator"
    f = fields(body)
    assert set(f) == set(tenant._FIELDS)
    assert (f["daily_usd"]["value"], f["daily_usd"]["source"], f["daily_usd"]["scope"]) == (0.0, "default", "workspace")
    assert f["drain_workers"]["scope"] == "deployment" and f["drain_workers"]["editable"] is True


def test_a_budget_change_is_stored_shown_and_visible_to_the_cli(box):
    st, body = call(box, "admin", {"daily_usd": "12.5", "min_redaction": "privacy"})
    assert st == 200 and (fields(body)["daily_usd"]["value"], fields(body)["daily_usd"]["source"]) == (12.5, "set")
    assert body["workspace_updated"]["by"] == "dana@acme.test"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        csa.cmd_org_config(type("A", (), {"org_id": store.DEFAULT_ORG, "set": None})())
    line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("daily_usd "))
    assert line.split()[1:] == ["workspace", "12.5", "12.5"], line


def test_bad_values_are_refused_with_a_sentence_and_store_nothing(box):
    call(box, "admin", {"daily_usd": 20})
    before = tenant.load_row(store.org_db())
    st, body = call(box, "admin", {"daily_usd": -1, "request_webhook": "hooks.slack.com/x", "settle_min": 90})
    assert st == 400 and body["ok"] is False and body["code"] == "invalid"
    assert set(body["errors"]) == {"daily_usd", "request_webhook"}
    assert "negative" in body["errors"]["daily_usd"] and "https://" in body["errors"]["request_webhook"]
    after = tenant.load_row(store.org_db())
    assert (after.daily_usd, after.settle_min, after.updated_at) == (before.daily_usd, before.settle_min, before.updated_at)
    assert tenant.deployment().request_webhook == ""


def test_clearing_a_field_is_the_built_in_default_not_the_pod_env(box, monkeypatch):
    monkeypatch.setenv("CSA_ORG_DAILY_USD", "999")
    call(box, "admin", {"daily_usd": 7})
    st, body = call(box, "admin", {"daily_usd": None})
    assert st == 200 and (fields(body)["daily_usd"]["value"], fields(body)["daily_usd"]["source"]) == (0.0, "default")


def test_a_second_workspace_makes_deployment_settings_the_operators(box):
    orgs._create_org("Globex", "globex")
    st, body = call(box, "admin")
    assert body["deployment_editable"] is False and fields(body)["drain_workers"]["editable"] is False
    st, body = call(box, "admin", {"drain_workers": 8})
    assert st == 400 and "operator" in body["errors"]["drain_workers"]
    assert tenant.deployment().drain_workers == 3


def test_the_dashboard_cookie_may_post_it_same_origin_only(box):
    assert "/api/config" in apiserver._COOKIE_POST_OK
    ck = {"Cookie": cliauth.session_cookie(box["admin"]).split(";")[0]}
    assert call(box, None, {"settle_min": 45}, {**ck, "Sec-Fetch-Site": "same-origin"})[0] == 200
    assert call(box, None, {"settle_min": 46}, {**ck, "Sec-Fetch-Site": "same-site"})[0] in (401, "reset")
    assert tenant.get_config(store.org_db()).settle_min == 45
    st, body = call(box, None, {"otlp_endpoint": "https://collector.evil.test"}, {**ck, "Sec-Fetch-Site": "same-origin"})
    assert st == 400 and "set on the server" in body["errors"]["otlp_endpoint"], \
        "an XSS riding the admin's cookie must not point the traces somewhere else"
    assert fields(call(box, None, headers={**ck, "Sec-Fetch-Site": "same-origin"})[1])["otlp_endpoint"]["editable"] is False
    assert call(box, "admin", {"otlp_endpoint": "https://collector.acme.test"})[0] == 200, "a bearer may"


# --- no restart: each process reads the row on its next use ---------------------------------------

def test_api_reads_the_workspace_row_on_every_request(box):
    """The api's autojudge gate and its settle window follow a write made by another process."""
    assert apiserver._autojudge_on(store.DEFAULT_ORG) is True
    tenant.set_config(store.org_db(), autojudge="off", settle_min=5)      # what `csa.py org config` does
    assert apiserver._autojudge_on(store.DEFAULT_ORG) is False
    assert fields(call(box, "admin")[1])["settle_min"]["value"] == 5


def test_mcp_reads_the_deployment_row_on_every_call(box, monkeypatch):
    """The MCP's ask synthesis goes through llm.complete, which resolves the role per call."""
    import mcp_server                                                       # noqa: F401 (the process under test)
    from analyzer import providers
    seen = []
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(llm, "_claude", lambda prompt, model, timeout, provider=None: (seen.append(("claude", model)), ("ok", 0, {}))[1])
    monkeypatch.setattr(llm, "_openai", lambda prompt, model, timeout, key, base: (seen.append(("openai", model)), ("ok", None, {}))[1])
    llm.complete("q", model="small")
    providers.save(providers.checked({"kind": "openai", "name": "Corp", "credential": "sk-x", "models": ["gpt-5"]}), "t")
    tenant.set_deployment(orgs.control_db(), small_model="corp/gpt-5")
    llm.complete("q", model="small")
    assert seen == [("claude", "haiku"), ("openai", "gpt-5")]


def test_etl_reads_the_deployment_row_every_tick(box, monkeypatch):
    """The drain loop hands every tick the `main` role, which llm.complete resolves per call."""
    submitted, ticks = [], []
    monkeypatch.setattr(drain_loop, "_orgs", lambda: [store.DEFAULT_ORG])
    monkeypatch.setattr(drain_loop.queue, "needs_drain", lambda org=None, *a, **k: True)
    monkeypatch.setattr(drain_loop, "_drain_one", lambda org, model, chunk: submitted.append(model))
    for name in ("_registry_tick", "_compact_tick", "_purge_tick"):
        monkeypatch.setattr(drain_loop, name, lambda *a, **k: [])

    def sleep(_s):
        while len(submitted) < len(ticks) + 1:                              # the pool thread ran this tick's drain
            time.sleep(0.01)
        ticks.append(1)
        if len(ticks) > 1:
            raise KeyboardInterrupt
    monkeypatch.setattr(drain_loop, "time", type("T", (), {"sleep": staticmethod(sleep),
                                                          "monotonic": staticmethod(time.monotonic)}))
    with pytest.raises(KeyboardInterrupt):
        drain_loop.loop()
    assert submitted == ["main", "main"]


def test_the_drain_cap_holds_back_orgs_past_the_worker_count():
    class Running:
        def done(self):
            return False
    submitted = []
    drain_loop._DRAIN_LAST.clear()
    orig = drain_loop.queue.needs_drain
    drain_loop.queue.needs_drain = lambda org=None, *a, **k: True
    try:
        drain_loop._tick(None, {}, lambda: ["a", "b", "c"], lambda org: (submitted.append(org), Running())[1], workers=2)
    finally:
        drain_loop.queue.needs_drain = orig
    assert submitted == ["a", "b"]


def test_no_setting_is_read_from_the_environment():
    """The bootstrap set is all the code reads; everything else is a setting or a constant."""
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = set()
    for d in (here, os.path.join(here, "analyzer")):
        for fn in os.listdir(d):
            if fn.endswith(".py"):
                with open(os.path.join(d, fn)) as f:
                    names |= set(re.findall(r"(?:CSA|OKTA)_[A-Z_]+", f.read()))
    assert names == {"CSA_API_PORT", "CSA_AUTH", "CSA_AUTH_TOKEN", "CSA_BIND", "CSA_CLIENT_DIST", "CSA_MCP_BIND",
                     "CSA_MCP_PORT", "CSA_MCP_URL", "CSA_ORG_INVITE", "CSA_PUBLIC_URL", "CSA_CONFIG_PATH"}


def test_registry_sources_are_never_set_over_http(box):
    """They make the server run git and gh with the operator's credentials: the box's CLI only."""
    st, body = call(box, "admin", {"registry_sources": "git:/etc"})
    assert st == 400 and "runs commands on the server" in body["errors"]["registry_sources"]
    assert fields(call(box, "admin")[1])["registry_sources"]["editable"] is False
    assert tenant.apply(store.DEFAULT_ORG, {"registry_sources": "sso"}, "cli", deployment_editable=True) == {}
    assert tenant.get_config(store.org_db()).registry_sources == ("sso",)


def test_the_otlp_endpoint_is_shown_as_its_host(box):
    assert call(box, "admin", {"otlp_endpoint": "https://user:pw@collector.acme.test/v1?key=s3cret"})[0] == 200
    shown = fields(call(box, "admin")[1])["otlp_endpoint"]["value"]
    assert shown == "https://collector.acme.test/…" and "s3cret" not in json.dumps(orgs.audit_log(None, 20))


def test_a_failed_workspace_write_leaves_control_db_untouched(box, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(tenant, "set_config", boom)
    with pytest.raises(OSError):
        tenant.apply(store.DEFAULT_ORG, {"daily_usd": 9, "drain_workers": 7}, "a@x.test", deployment_editable=True)
    assert tenant.deployment().drain_workers == 3, "the deployment half was not committed"
    assert not [r for r in orgs.audit_log(None, 20) if r["action"] == "config_set"], "no audit line for a change that did not happen"


def test_upkeep_never_runs_on_the_drain_pool(box, monkeypatch):
    seen = {}
    monkeypatch.setattr(drain_loop, "_orgs", lambda: [store.DEFAULT_ORG])
    monkeypatch.setattr(drain_loop.queue, "needs_drain", lambda org=None, *a, **k: True)
    monkeypatch.setattr(drain_loop, "_drain_one", lambda org, model, chunk: None)
    for name in ("_registry_tick", "_compact_tick", "_purge_tick"):
        monkeypatch.setattr(drain_loop, name, lambda pool, *a, n=name, **k: (seen.setdefault(n, pool._max_workers), [])[1])
    drain_loop.loop(once=True)
    assert set(seen.values()) == {drain_loop.UPKEEP_WORKERS}
