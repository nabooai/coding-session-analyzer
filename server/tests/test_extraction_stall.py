"""The etl records why extraction is not draining; the api reports it on /api/health and /api/pipeline.
The two are separate processes, so the answer lives in control.db."""
import pytest

import apiserver
import csa
from analyzer import drain_loop, orgs, queue, store

KEY_ERROR = "claude failed: Invalid API key sk-ant-api03-AbCdEf0123456789AbCdEf0123456789 · Please run /login"


@pytest.fixture()
def box(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store.sqlite_db(store.org_db("acme")).close()
    return "acme"


def _queue(org, sid="s1"):
    queue.queue_pending({"session_id": sid, "transcript": "hello", "project": "p"}, org_id=org)


def _drain_returns(monkeypatch, done=(), failed=()):
    monkeypatch.setattr(csa, "drain_org", lambda *a, **k: (list(done), list(failed), [], [], False))


def test_a_systemic_failure_is_recorded_redacted_and_reported(box, monkeypatch):
    _queue(box)
    _drain_returns(monkeypatch, failed=[("s1", KEY_ERROR)])
    drain_loop._drain_one(box, "sonnet", 5)
    stall = orgs.extraction_stalls()[box]
    assert "Invalid API key" in stall["reason"] and "sk-ant-api03" not in stall["reason"]
    assert apiserver.extraction_status(box) == {"stalled": True, "reason": stall["reason"]}
    anonymous = apiserver.extraction_status()
    assert anonymous["stalled"] is True and "Invalid API key" not in anonymous["reason"]


def test_the_same_reason_keeps_the_first_failure_time(box):
    orgs.set_extraction_stall(box, KEY_ERROR)
    first = orgs.extraction_stalls()[box]["since"]
    orgs.set_extraction_stall(box, KEY_ERROR)
    assert orgs.extraction_stalls()[box]["since"] == first


def test_a_batch_that_extracts_clears_the_stall(box, monkeypatch):
    _queue(box)
    orgs.set_extraction_stall(box, KEY_ERROR)
    _drain_returns(monkeypatch, done=[("s1", {}, {})])
    drain_loop._drain_one(box, "sonnet", 5)
    assert orgs.extraction_stalls() == {}
    assert apiserver.extraction_status(box) == {"stalled": False, "reason": ""}


def test_a_batch_that_extracts_some_and_stalls_stays_stalled(box, monkeypatch):
    _queue(box)
    _drain_returns(monkeypatch, done=[("s1", {}, {})], failed=[("s2", KEY_ERROR)])
    drain_loop._drain_one(box, "sonnet", 5)
    assert box in orgs.extraction_stalls()


def test_deleting_a_workspace_deletes_its_stall(box):
    orgs._create_org("Acme", box)
    orgs.set_extraction_stall(box, KEY_ERROR)
    orgs.delete_org(box)
    assert orgs.extraction_stalls() == {}


def test_a_stall_with_nothing_waiting_is_not_reported(box):
    orgs.set_extraction_stall(box, KEY_ERROR)
    assert apiserver.extraction_status(box)["stalled"] is False


def test_another_workspaces_stall_is_not_reported_to_this_one(box):
    _queue(box)
    orgs.set_extraction_stall(box, KEY_ERROR)
    assert apiserver.extraction_status("other") == {"stalled": False, "reason": ""}
