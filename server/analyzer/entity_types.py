"""Per-org entity taxonomy: fixed DEFAULT types (ticket/pr/...) + team-defined CUSTOM types.

A tenant that tracks Releases, Features or Clients defines each as key + description; the description
IS the extraction prompt for that type (prompt_block). Custom types live in the ORG's own db
(custom_entity_types), so one tenant's taxonomy never touches another's extraction prompt.
`entities.type` is plain TEXT everywhere (store/FTS/UI), so storing/searching a custom type needs no
schema change. Custom types apply to sessions extracted AFTER they're added — deliberately no
re-extract of the back catalog (that's a paid `claude -p` per session; use `csa reextract` per session
if one matters). Imports only analyzer.db/models (neither imports store) — store.py imports DEFAULTS
from here, so that stays cycle-free.
"""
import re, time

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from analyzer import db
from analyzer.models import CustomEntityType

_CET = CustomEntityType.__table__

DEFAULTS = ("ticket", "pr", "release", "service", "repo", "file", "person", "data_source", "env", "url")
# Display-only docs for the built-ins (NOT fed to the extractor — its rules live in extract.PROMPT).
# Exists so a user can check whether their candidate type overlaps a default BEFORE hitting the 400
# (all three user-sims flagged the bare-string defaults as unjudgeable).
DEFAULT_INFO = {
    "ticket": "an issue-tracker ticket, e.g. KLG-200",
    "pr": "a pull request",
    "release": "a shipped or planned release/version",
    "service": "a deployed service or application",
    "repo": "a code repository",
    "file": "a source file important to the work",
    "person": "a teammate or stakeholder",
    "data_source": "a company data system, e.g. BigQuery or analytics.db",
    "env": "an environment: prod, staging, lab",
    "url": "a link referenced in the session",
}
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def valid_key(key):
    key = (key or "").strip().lower()
    if not key:
        raise ValueError("missing 'key': the type's short name, e.g. 'client'")
    if not _KEY.fullmatch(key):
        raise ValueError("key must start with a lowercase letter and use only lowercase letters, "
                         "digits and underscores, max 32 chars, e.g. 'client', 'release_train'")
    if key in DEFAULTS:
        raise ValueError(f"'{key}' is a built-in type, already extracted for every team: "
                         f"pick a different key (e.g. '{key}_train' or 'my_{key}') to add your own definition")
    return key


def custom(con):
    return [{"key": k, "description": d or "", "updated_at": ua or "", "updated_by": ub or ""}
            for k, d, ua, ub in db.run(con, select(
                _CET.c.key, _CET.c.description, _CET.c.updated_at, _CET.c.updated_by)
                .order_by(_CET.c.key))]


def upsert(con, key, description, by=None):
    """`by` = the verified identity that wrote/edited the rule (shown as attribution in the UI). None is
    fine — a CLI edit or a credential the gate could not name just shows no author."""
    key = valid_key(key)
    description = (description or "").strip()
    if not description:
        raise ValueError("a custom entity type needs a description — it IS the extraction prompt")
    if len(description) > 2000:                 # it's injected into EVERY extraction prompt — bound the spend
        raise ValueError("description too long (max 2000 chars)")
    if db.run(con, select(func.count()).select_from(_CET)
              .where(_CET.c.key != key)).fetchone()[0] >= 50:
        raise ValueError("too many custom entity types (max 50)")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    by = (by or "").strip()
    existed = db.run(con, select(_CET.c.key).where(_CET.c.key == key)).fetchone() is not None
    ins = sqlite_insert(_CET).values(key=key, description=description,
                                     created_at=now, updated_at=now, updated_by=by or None)
    db.run(con, ins.on_conflict_do_update(
        index_elements=[_CET.c.key],
        set_={"description": ins.excluded.description, "updated_at": ins.excluded.updated_at,
              "updated_by": ins.excluded.updated_by}))
    con.commit()                                          # caller-owned txn: the bridge never commits
    # `created` makes a typo'd-key-meant-as-edit visible; `applies_to` answers every user's first
    # question (retroactivity) in the response itself instead of tribal knowledge.
    return {"key": key, "description": description, "updated_at": now, "updated_by": by,
            "created": not existed, "applies_to": "sessions extracted from now on"}


def remove(con, key):
    n = db.run(con, delete(_CET).where(_CET.c.key == (key or "").strip().lower())).rowcount
    con.commit()
    return bool(n)


def prompt_block(types):
    """Block injected into the extraction prompt right before TRANSCRIPT:. Empty string when no customs,
    so a tenant with none gets the byte-identical default prompt (asserted in extract.py's self-check)."""
    if not types:
        return ""
    lines = "\n".join(f'- "{t["key"]}" = {t["description"]}' for t in types)
    return ('\nCUSTOM ENTITY TYPES (team-defined): in "entities", "type" may ALSO be one of these keys. '
            "Extract every mention — same shape, same stable-id rules as the built-in types:\n"
            + lines + "\n\n")
