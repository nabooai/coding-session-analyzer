"""Ported verbatim from entity_types.py's __main__ self-check (deleted with the P1 migration):
defaults immutable, key/description validation, upsert/remove semantics, prompt block."""
import pytest

from analyzer import entity_types, store


@pytest.fixture()
def con():
    return store.sqlite_db(":memory:")


def test_no_customs_empty_block(con):
    assert entity_types.custom(con) == []
    assert entity_types.prompt_block(entity_types.custom(con)) == ""


def test_every_default_documented():
    assert set(entity_types.DEFAULT_INFO) == set(entity_types.DEFAULTS)


def test_upsert_normalizes_sorts_and_attributes(con):
    r1 = entity_types.upsert(con, "Client ", "a paying customer account, e.g. Acme; id = the company name as-is")
    assert r1["created"] is True and r1["applies_to"], "fresh key -> created:true + retroactivity stated"
    entity_types.upsert(con, "feature", "a named product capability tracked by the team")
    assert [t["key"] for t in entity_types.custom(con)] == ["client", "feature"], "keys normalized + sorted"
    r2 = entity_types.upsert(con, "client", "UPDATED description", by="mara@example.com")
    assert r2["created"] is False, "existing key -> created:false (typo'd edits become visible)"
    rows = entity_types.custom(con)
    assert rows[0]["description"] == "UPDATED description", "upsert overwrites"
    assert rows[0]["updated_by"] == "mara@example.com" and rows[0]["updated_at"], "attribution stored"
    assert rows[1]["updated_by"] == "", "no identity -> no author, not a crash"


def test_key_validation_messages(con):
    for bad, want in ((" ", "missing 'key'"), ("Bad Key!", "lowercase"), ("pr", "built-in")):
        with pytest.raises(ValueError, match=want.replace("'", ".")):
            entity_types.upsert(con, bad, "d")


def test_prompt_block_carries_customs(con):
    entity_types.upsert(con, "client", "UPDATED description")
    entity_types.upsert(con, "feature", "a named product capability")
    blk = entity_types.prompt_block(entity_types.custom(con))
    assert '"client" = UPDATED description' in blk and '"feature"' in blk


def test_bad_keys_rejected(con):
    for bad in ("pr", "Client-X", "", "9lives", "x" * 40):
        with pytest.raises(ValueError):
            entity_types.upsert(con, bad, "d")


def test_bad_descriptions_rejected(con):
    for bad_desc in ("  ", "x" * 2001):
        with pytest.raises(ValueError):
            entity_types.upsert(con, "empty_desc", bad_desc)


def test_remove_case_normalized_and_honest(con):
    entity_types.upsert(con, "feature", "a named product capability")
    assert entity_types.remove(con, "FEATURE") and not entity_types.remove(con, "feature")
