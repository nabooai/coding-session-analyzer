"""budget.over_budget: two daily caps (USD, tokens) per workspace, off at 0, the default org
budgeted like any other, queued work kept and resumed the next day. Run from server/:
uv run pytest tests/test_budget.py"""
import json
import os

import pytest

import csa
from analyzer import budget, drain_loop, queue, store, tenant


@pytest.fixture()
def org(tmp_path, monkeypatch):
    """A real org corpus under a scratch data/ so the CLI's config path and the queue's pending dir
    both resolve to it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CSA_ORG_DAILY_USD", raising=False)
    path = store.org_db("acme")
    store.sqlite_db(path).close()
    return "acme"


def _con(org):
    return store.sqlite_db(store.org_db(org))


def _spend(con, usd, in_tokens, out_tokens, day="now"):
    """One ledger row dated `day` ('now' or '-1 day'), through the same writer the extractor uses."""
    store.record_cost(con, "extract", "sonnet", "s1",
                      {"input_tokens": in_tokens, "output_tokens": out_tokens, "cache_read_input_tokens": 5000}, usd)
    if day != "now":
        con.execute("UPDATE costs SET at = datetime('now', ?) WHERE rowid = last_insert_rowid()", (day,))
        con.commit()


def test_default_org_is_budgeted_like_any_other(org):
    tenant.set_config(store.org_db(store.DEFAULT_ORG), daily_usd=10)
    con = _con(store.DEFAULT_ORG)
    _spend(con, 11.0, 1, 1)
    assert budget.over_budget(con).over == "usd"
    con.close()


def test_usd_cap_alone(org):
    tenant.set_config(store.org_db(org), daily_usd=10, daily_tokens=0)
    con = _con(org)
    assert budget.over_budget(con) == budget.Spend(None, 0.0, 10.0, 0, 0), "empty ledger under budget"
    _spend(con, 4.0, 100, 10)
    _spend(con, 7.0, 100, 10)
    spend = budget.over_budget(con)
    assert spend.over == "usd" and spend.usd == 11.0 and spend.usd_cap == 10.0
    assert spend.tokens == 220 and spend.tokens_cap == 0, "tokens are still reported with their cap off"
    con.close()


def test_tokens_cap_alone_counts_input_plus_output_not_cached_reads(org):
    tenant.set_config(store.org_db(org), daily_usd=0, daily_tokens=1000)
    con = _con(org)
    _spend(con, None, 600, 300)                     # an OpenAI-format gateway reports no cost at all
    assert budget.over_budget(con) == budget.Spend(None, 0.0, 0.0, 900, 1000)
    _spend(con, None, 100, 0)
    spend = budget.over_budget(con)
    assert spend.over == "tokens" and spend.tokens == 1000, "5000 cached reads per row never counted"
    con.close()


def test_both_caps_the_first_to_trip_is_named(org):
    tenant.set_config(store.org_db(org), daily_usd=10, daily_tokens=1000)
    con = _con(org)
    _spend(con, 2.0, 500, 500)
    assert budget.over_budget(con).over == "tokens"
    _spend(con, 9.0, 0, 0)
    assert budget.over_budget(con).over == "usd", "usd is checked first when both are over"
    con.close()


def test_zero_disables_each_cap_independently(org):
    con = _con(org)
    _spend(con, 999.0, 10 ** 6, 10 ** 6)
    tenant.set_config(store.org_db(org), daily_usd=0, daily_tokens=0)
    assert budget.over_budget(con).over is None
    tenant.set_config(store.org_db(org), daily_usd=0, daily_tokens=10)
    assert budget.over_budget(con).over == "tokens"
    tenant.set_config(store.org_db(org), daily_usd=10, daily_tokens=0)
    assert budget.over_budget(con).over == "usd"
    con.close()


def test_budget_is_daily(org):
    tenant.set_config(store.org_db(org), daily_usd=10, daily_tokens=1000)
    con = _con(org)
    _spend(con, 999.0, 10 ** 6, 10 ** 6, day="-1 day")
    assert budget.over_budget(con) == budget.Spend(None, 0.0, 10.0, 0, 1000), "yesterday's burn is not today's"
    con.close()


def test_broken_ledger_never_blocks_the_queue(org):
    tenant.set_config(store.org_db(org), daily_usd=10, daily_tokens=10)
    con = _con(org)
    con.execute("DROP TABLE costs")
    assert budget.over_budget(con).over is None
    con.close()


def test_empty_ledger_sum_is_float_zero(org):
    """Serving-parity pin: raw IFNULL(SUM…,0) returns int 0 on an empty ledger; the report is a
    float either way."""
    tenant.set_config(store.org_db(org), daily_usd=10)
    con = _con(org)
    spend = budget.over_budget(con)
    assert spend.usd == 0.0 and isinstance(spend.usd, float) and isinstance(spend.tokens, int)
    con.close()


def test_the_cli_field_round_trips_through_set_config(org):
    """`csa.py org config acme --set daily_tokens=5000` hands the raw string to set_config."""
    path = store.org_db(org)
    for raw, want in (("5000", 5000), (5000, 5000), ("", 0), (None, 0)):
        assert tenant.set_config(path, daily_tokens=raw).daily_tokens == want, raw
    with pytest.raises(ValueError):
        tenant.set_config(path, daily_tokens="5k")
    tenant.set_config(path, daily_tokens="5000")
    assert tenant.load_row(path).daily_tokens == 5000
    assert tenant.config(_con(org)).daily_tokens == 5000, "the ETL's bridge read sees the CLI's write"


def test_over_the_token_cap_stops_draining_keeps_the_queue_and_resumes_next_day(org, monkeypatch):
    """The drain_loop contract: an org over its cap is skipped, its pending files untouched, and the
    next day's empty ledger drains them."""
    calls = []
    monkeypatch.setattr(csa, "drain_org", lambda *a, **k: (calls.append(a), ([], [], [], [], False))[1])
    queue.queue_pending({"session_id": "q1", "transcript": "hello", "project": "p"}, org_id=org)
    pending = os.path.join(queue._dir(org), "q1.json")
    before = open(pending, "rb").read()
    tenant.set_config(store.org_db(org), daily_usd=0, daily_tokens=1000)
    con = _con(org)
    _spend(con, None, 700, 400)
    con.close()

    assert drain_loop._drain_one(org, "sonnet", 5) == 0
    assert calls == [], "over the cap: the drain is not even attempted"
    assert open(pending, "rb").read() == before and json.loads(before)["session_id"] == "q1", "queued, never dropped"

    con = _con(org)
    con.execute("UPDATE costs SET at = datetime('now', '-1 day')")
    con.commit()
    con.close()
    drain_loop._drain_one(org, "sonnet", 5)
    assert calls == [(org, "sonnet", 5)], "the next day the same queue drains"
