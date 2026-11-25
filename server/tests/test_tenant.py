"""Tenant model + config resolution (analyzer/tenant.py). Model-versus-file parity is
tests/test_migrations.py (`alembic check`), not a hand-written column list."""
import sqlite3

import pytest

from analyzer import llm, orgs, route, store, tenant


@pytest.fixture()
def org_db(tmp_path):
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()                        # create the full schema like a real org db
    return path


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """A cwd with data/: the default org's corpus and control.db, as a one-workspace server."""
    monkeypatch.chdir(tmp_path)
    store.sqlite_db(store.org_db(store.DEFAULT_ORG)).close()
    orgs.control_db().close()
    monkeypatch.setattr(llm, "probe", lambda ref, **k: None)
    return tmp_path


def test_defaults_without_a_row():
    cfg = tenant.defaults()
    assert cfg.entity_floor is True and cfg.entity_gc_days == 45 and cfg.autojudge is True
    assert cfg.daily_usd == 0.0 and cfg.relay_accounts == frozenset(), "no spend cap unless one is set"
    assert cfg.min_redaction == "secrets" and cfg.settle_min == 30 and cfg.token_ttl_days == 30
    assert cfg.archive_days == 0 and cfg.daily_tokens == 0 and cfg.trace_days == 30
    dep = tenant.DeploymentConfig(**tenant._resolve(tenant.DEPLOYMENT, {}))
    assert (dep.main_model, dep.small_model, dep.drain_workers, dep.request_webhook) == ("", "", 3, "")


def test_no_environment_variable_is_consulted(org_db, monkeypatch):
    """The env era is over: a pod env that still holds an old knob changes nothing, and clearing a
    field lands on the built-in default, not on whatever the process happened to inherit."""
    for env in ("CSA_ORG_DAILY_USD", "CSA_SETTLE_MIN", "CSA_ENTITY_FLOOR", "CSA_MIN_REDACTION",
                "CSA_RELAY_ACCOUNTS", "CSA_TOKEN_TTL_DAYS", "CSA_AUTOJUDGE", "CSA_DRAIN_MODEL"):
        monkeypatch.setenv(env, "0")
    cfg = tenant.get_config(org_db)
    assert (cfg.daily_usd, cfg.settle_min, cfg.entity_floor, cfg.autojudge) == (0.0, 30, True, True)
    tenant.set_config(org_db, daily_usd=5)
    assert tenant.set_config(org_db, daily_usd=None).daily_usd == 0.0


def test_archive_days_values(org_db):
    """Days, capped at a decade, or the literal `forever`; anything else is refused at write time."""
    for raw, want in ((30, 30), ("30", 30), ("forever", tenant.KEEP_FOREVER), (" Forever ", tenant.KEEP_FOREVER),
                      ("", 0), (None, 0), (3650, 3650)):
        assert tenant.set_config(org_db, archive_days=raw).archive_days == want, raw
    for junk in ("-1", -5, "banana", 99999):
        with pytest.raises(ValueError, match="archive_days"):
            tenant.set_config(org_db, archive_days=junk)
    tenant.set_config(org_db, archive_days="forever")
    assert tenant.load_row(org_db).archive_days == "forever", "the row keeps what the operator typed"


def test_daily_tokens_values(org_db):
    for raw, want in ((250000, 250000), ("250000", 250000), ("", 0), (None, 0)):
        assert tenant.set_config(org_db, daily_tokens=raw).daily_tokens == want, raw
    tenant.set_config(org_db, daily_tokens="250000")
    assert tenant.load_row(org_db).daily_tokens == 250000, "stored as an integer, not the CLI's string"
    for junk in ("250,000", "250k", "1e6", "250000.0", "banana", -1):
        with pytest.raises(ValueError, match="daily_tokens"):
            tenant.set_config(org_db, daily_tokens=junk)
    assert tenant.load_row(org_db).daily_tokens == 250000, "a refused write leaves the cap where it was"


@pytest.mark.parametrize("field,bad,says", [
    ("daily_usd", -1, "can't be negative"),
    ("daily_usd", "fifty", "amount in dollars"),
    ("min_redaction", "paranoid", "secrets, privacy or strict"),
    ("settle_min", 0, "between 1 and 1,440"),
    ("token_ttl_days", -3, "between 0 and 3,650"),
    ("relay_accounts", "bot@x.com, not-an-email", "Not an email address: not-an-email"),
    ("person_aliases", "slackname=mara, oops", "Not a handle=local-part pair: oops."),
    ("autojudge", "maybe", "on or off"),
    ("registry_sources", "sso,svn:x", "Unknown source svn:x"),
    ("request_webhook", "hooks.slack.com/x", "https://"),
    ("request_webhook", "ftp://x.test/y", "https://"),
    ("otlp_endpoint", "not a url", "https://"),
    ("drain_workers", 17, "between 1 and 16"),
    ("main_model", "so net", "registered provider"),
    ("small_model", "sonnet", "registered provider"),
])
def test_a_bad_value_gets_a_sentence(field, bad, says):
    values, errors = tenant.checked({field: bad})
    assert values == {} and says in errors[field], errors


def test_a_refused_batch_stores_nothing(org_db):
    tenant.set_config(org_db, daily_usd=20, settle_min=45)
    with pytest.raises(ValueError, match="daily_usd"):
        tenant.set_config(org_db, settle_min=90, daily_usd=-1)
    row = tenant.load_row(org_db)
    assert (row.daily_usd, row.settle_min) == (20, 45), "the valid field in the batch was not written either"


def test_set_config_upserts_and_none_clears(org_db):
    cfg = tenant.set_config(org_db, updated_by="test", settle_min=90)
    assert cfg.settle_min == 90
    assert tenant.load_row(org_db).updated_by == "test"
    cfg = tenant.set_config(org_db, daily_usd=7.5)       # second field must not disturb the first
    assert cfg.settle_min == 90 and cfg.daily_usd == 7.5
    cfg = tenant.set_config(org_db, settle_min=None)     # None clears back to the default
    assert cfg.settle_min == 30


def test_set_config_rejects_unknown_field_and_the_wrong_scope(org_db):
    with pytest.raises(ValueError, match="no setting called entity_flor"):
        tenant.set_config(org_db, entity_flor="off")
    with pytest.raises(ValueError, match="deployment setting"):
        tenant.set_config(org_db, drain_workers=4)


def test_bridge_read_agrees_with_the_repository(org_db):
    tenant.set_config(org_db, settle_min=90, min_redaction="privacy")
    con = sqlite3.connect(org_db)
    assert tenant.config(con).settle_min == 90 and tenant.config(con).min_redaction == "privacy"


def test_bridge_falls_back_to_defaults_on_bare_connection():
    bare = sqlite3.connect(":memory:")                   # no tenant_config table at all
    assert tenant.config(bare).settle_min == 30


def test_single_row_check_constraint(org_db):
    con = sqlite3.connect(org_db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO tenant_config(id, settle_min) VALUES (2, 10)")


def test_consumers_read_per_tenant(org_db):
    """The wired consumers resolve from the row."""
    from analyzer import budget
    tenant.set_config(org_db, daily_usd=1.25, daily_tokens=40, min_redaction="strict", relay_accounts="bot@x.com",
                      person_aliases="Slack-Nick=mara")
    con = sqlite3.connect(org_db)
    assert (budget.over_budget(con).usd_cap, budget.over_budget(con).tokens_cap) == (1.25, 40)
    assert tenant.redaction_policy(con)[0] == "strict"
    assert tenant.redaction_policy(db_path=org_db)[0] == "strict"
    assert store.relay_accounts(con) == {"bot@x.com"}
    assert route.aliases(con) == {"slack-nick": "mara"} and tenant.defaults().person_aliases == {}
    assert tenant.redaction_policy() == ("secrets", [], []), "pre-auth surfaces read the built-in default"


# --- deployment scope: control.db `settings` -----------------------------------------------------

def test_deployment_reads_nothing_and_creates_nothing_without_control_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tenant.deployment().drain_workers == 3
    assert not (tmp_path / "data").exists(), "a per-use read never creates or migrates control.db"


def test_deployment_is_read_from_control_db_on_every_call(box):
    """No module cache: a change on the Settings screen reaches the next read in every process."""
    assert tenant.deployment().main_model == ""
    tenant.set_deployment(orgs.control_db(), main_model="acme/claude-sonnet-4-5", drain_workers=5)
    assert (tenant.deployment().main_model, tenant.deployment().drain_workers) == ("acme/claude-sonnet-4-5", 5)
    tenant.set_deployment(orgs.control_db(), main_model=None)
    assert tenant.deployment().main_model == "", "cleared back to the built-in default"


def test_single_row_check_constraint_on_settings(box):
    con = orgs.control_db()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO settings(id, drain_workers) VALUES (2, 1)")


# --- apply(): both scopes, all-or-nothing, audited ------------------------------------------------

def test_apply_writes_both_scopes_and_audits_each_change(box):
    errors = tenant.apply(store.DEFAULT_ORG, {"daily_usd": 12, "min_redaction": "strict",
                                              "request_webhook": "https://hooks.slack.test/T0/B0/secret"},
                          "dana@acme.test", deployment_editable=True)
    assert errors == {}
    assert tenant.get_config(store.org_db(store.DEFAULT_ORG)).daily_usd == 12
    assert tenant.deployment().request_webhook == "https://hooks.slack.test/T0/B0/secret"
    log = {r["detail"] for r in orgs.audit_log(None, 10) if r["action"] == "config_set"}
    assert "dana@acme.test daily_usd: None -> 12.0" in log
    assert "dana@acme.test request_webhook: None -> https://hooks.slack.test/…" in log, "the secret is never logged"
    assert tenant.apply(store.DEFAULT_ORG, {"daily_usd": 12}, "dana@acme.test", True) == {}
    assert len([r for r in orgs.audit_log(None, 50) if r["action"] == "config_set"]) == 3, "no-op writes are not audited"


def test_apply_refuses_the_whole_batch(box):
    tenant.apply(store.DEFAULT_ORG, {"daily_usd": 20}, "a@x.test", True)
    errors = tenant.apply(store.DEFAULT_ORG, {"daily_usd": -1, "settle_min": 90,
                                              "request_webhook": "not a url"}, "a@x.test", True)
    assert set(errors) == {"daily_usd", "request_webhook"}
    assert tenant.get_config(store.org_db(store.DEFAULT_ORG)).settle_min == 30, "the valid field was not stored"
    assert tenant.get_config(store.org_db(store.DEFAULT_ORG)).daily_usd == 20
    assert tenant.deployment().request_webhook == ""


def test_apply_refuses_deployment_fields_on_a_multi_workspace_server(box):
    errors = tenant.apply(store.DEFAULT_ORG, {"drain_workers": 8, "daily_usd": 9}, "a@x.test", False)
    assert set(errors) == {"drain_workers"} and "operator" in errors["drain_workers"]
    assert tenant.get_config(store.org_db(store.DEFAULT_ORG)).daily_usd == 0.0, "nothing stored"


def test_apply_probes_a_changed_model_before_storing_it(box, monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "probe", lambda ref, **k: calls.append(ref)
                        or ("acme/nope did not answer: 404. Nothing was saved." if ref.endswith("nope") else None))
    errors = tenant.apply(store.DEFAULT_ORG, {"main_model": "acme/nope"}, "a@x.test", True)
    assert errors == {"main_model": "acme/nope did not answer: 404. Nothing was saved."}
    assert tenant.deployment().main_model == ""
    assert tenant.apply(store.DEFAULT_ORG, {"small_model": "acme/claude-haiku-4-5"}, "a@x.test", True) == {}
    assert tenant.deployment().small_model == "acme/claude-haiku-4-5"
    tenant.apply(store.DEFAULT_ORG, {"settle_min": 40}, "a@x.test", True)
    assert calls == ["acme/nope", "acme/claude-haiku-4-5"], "a change that does not touch a model does not call one"


def test_describe_says_where_each_value_comes_from(box):
    tenant.apply(store.DEFAULT_ORG, {"settle_min": 45, "request_webhook": "https://hooks.slack.test/T/B/s"},
                 "dana@acme.test", True)
    d = tenant.describe(store.DEFAULT_ORG, deployment_editable=False)
    by = {f["name"]: f for f in d["fields"]}
    assert by["settle_min"] == {"name": "settle_min", "scope": "workspace", "value": 45, "default": 30,
                                     "source": "set", "editable": True}
    assert (by["daily_usd"]["source"], by["daily_usd"]["value"]) == ("default", 0.0)
    assert by["request_webhook"]["value"] == "https://hooks.slack.test/…", "a secret is shown as its host"
    assert by["drain_workers"]["editable"] is False and d["deployment_editable"] is False
    assert d["workspace_updated"]["by"] == "dana@acme.test"
