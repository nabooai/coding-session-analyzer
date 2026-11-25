"""Auto-select the fuzzy-searchable identifier columns ("axes") from the CSA SQLite store.

Ported from an upstream project's fuzzy_axes. The SELECTION POLICY (denylists, value-shape thresholds, the
entity-kind derivation) is upstream's, verbatim; only the store introspection is re-implemented for
SQLite (upstream reads DuckDB ``information_schema`` + DuckDB SQL aggregates; here we read
``sqlite_master`` / ``PRAGMA table_info`` and compute the value-shape stats in Python, which is
trivial at CSA's ~thousands-of-values scale).

One CSA-specific adaptation: the ``entities`` table is POLYMORPHIC (one ``type`` column holds
person / repo / file / service / …), which breaks upstream's "one column = one kind" assumption. So the
``entities`` id/name columns are expanded into ONE AXIS PER ``type`` value, each carrying that type
as its kind — so ``jules`` (person) never collides with ``build_all.py`` (file), and the owner axis
(``sessions.user`` → person) and the person entities collapse into one person, exactly as upstream's
cross-axis fold intends.

A good axis holds SHORT, human-typed identifiers/names. A bad one holds opaque ids / SHAs, urls,
datetimes, enums, or free-text bodies. Selection is:

* **structural hard drops** — a name denylist of plumbing / derived-url / opaque-id columns; and
  VALUE-shape guards (url-prefixed, mostly-numeric, ISO-datetime, long-hex/SHA, email).
* **cardinality / text drops** — no variety (<2 distinct); enum (few distinct on a table with
  enough rows to trust that as an enum); bodies (p95 length / word-count caps).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy import column as sa_column, table as sa_table

from analyzer import db, models

# ── selection policy (upstream verbatim) ───────────────────────────────────────────────────────────
#: Plumbing, review, cost, signal and derived tables: never a user-typed identifier. Named through
#: their models, so renaming a table in models.py moves this set with it instead of silently
#: re-admitting the table as an axis. `queries` and `served` are here for the LEGACY in-corpus
#: copies the telemetry split left behind; the live ones are on the `tel` file, which this scan
#: does not read.
_SKIP_MODELLED = (
    models.costs, models.queries, models.served,
    models.ClaimCluster.__table__, models.EntityStats.__table__, models.EntityTypePin.__table__,
    models.Fact.__table__, models.FactEdge.__table__, models.HarvestAction.__table__,
    models.HarvestLink.__table__, models.HarvestMention.__table__, models.HcType.__table__,
    models.Review.__table__, models.Signal.__table__,
)

#: The rest have no model to ask: the FTS5 `docs` table and the five shadow tables SQLite builds
#: beside it, the two session_workspace views, and Alembic's own version table. They reach disk as
#: raw statements in the baseline. test_the_tables_fuzzy_axes_skips_are_real fails if one stops
#: existing, or if one gains a model and should have moved up.
_SKIP_UNMODELLED = frozenset(
    {
        "docs", "docs_config", "docs_content", "docs_data", "docs_docsize", "docs_idx",
        "session_branches", "session_dirs",
        "alembic_version",
    }
)

SKIP_TABLES = frozenset(t.name for t in _SKIP_MODELLED) | _SKIP_UNMODELLED

#: Column names that never hold a human-typed identifier: plumbing, opaque ids, obvious enums.
DENY_EXACT = frozenset(
    {
        "source_endpoint", "source_native_id", "_doc", "entityId", "id", "self", "iconUrl",
        "avatar_url", "apiKey", "color", "permissions", "object", "state", "status", "kind",
        "type", "accountType", "board_kind", "signOnMode", "role", "style", "projectTypeKey",
        "timeZone", "locale", "visibility", "language", "author_association", "content_type",
        "default_branch", "target_commitish", "accountStatus", "parentType",
        # CSA plumbing that survives the substring/type guards:
        "session_id", "doc_id", "ref", "rowid", "attributes", "context", "note", "scope",
        "corroboration", "verb", "rel", "evidence", "claim",
    }
)
#: Substrings that mark a column as plumbing / url / opaque-id / datetime / enum-suffix.
DENY_SUBSTR = (
    "url", "href", "_links", "avatar", "icon", "sortkey", "created", "updated", "date", "time",
    "_id", "Id", "description", "body", "profileUrl", "___type", "_at", "sha", "summary",
)

P95_LEN_MAX = 48  # bodies / long titles blow past this (p95, not max → one outlier can't kill an axis)
#: a column whose values are mostly slash-separated is PATH-like (file paths, owner/name refs) —
#: the length cap is then judged on the p95 TAIL length (the searchable basename), not the whole.
PATH_FRAC_MIN = 0.5
AVG_WORDS_MAX = 5  # keys/names/usernames are few words; sentences are not
URL_FRAC_MAX = 0.30  # a url column
NUM_FRAC_MAX = 0.90  # purely-numeric = an id, not a typed token
DT_FRAC_MAX = 0.50  # ISO-datetime strings stored as VARCHAR (closed_at, lastLogin)
HEX_FRAC_MAX = 0.50  # opaque hex ids / SHAs
EMAIL_FRAC_MAX = 0.50  # email columns are copy-paste identifiers + bot/noreply noise
#: Treat "few distinct values" as an ENUM only when the table has enough rows to trust that — on a
#: tiny table few-distinct is DATA POVERTY, not an enum, and the column may be a real key.
ENUM_MAX_DISTINCT = 3
ENUM_ROW_FLOOR = 20

#: The polymorphic table whose kind lives in a sibling column, not the table name (CSA-specific).
#: Its ``id`` is the canonical key (``jules@example.com``, ``build_all.py``, ``ledger``) and its ``name``
#: is the human label; both become one axis per ``type`` value.
_ENTITIES = models.Entity.__table__
_POLY_TABLE = _ENTITIES.name
_POLY_KIND_COL = _ENTITIES.c.type.name
_POLY_COLS = (_ENTITIES.c.id.name, _ENTITIES.c.name.name)

#: The session-owner column: the one identity axis the stat scan cannot discover (see select_axes).
_SESSIONS = models.SessionRow.__table__


@dataclass(frozen=True, slots=True)
class Axis:
    """One selected searchable column, with the entity kind its values name. ``sel_col``/``sel_val``
    (CSA extension) carry a per-row-kind predicate for the polymorphic ``entities`` table — a
    parametrized ``WHERE sel_col = sel_val`` the candidate loader applies, so one physical column
    yields one axis per kind without a SQL-injection surface."""

    table: str
    column: str
    #: best-effort entity KIND the values name (person / repo / project / …); the fuzzy index groups
    #: cross-column hits by ``(kind, normalized_value)`` so one entity is not surfaced N times.
    kind: str
    sel_col: str = ""
    sel_val: str = ""

    @property
    def key(self) -> str:
        base = f"{self.table}.{self.column}"
        return f"{self.table}[{self.sel_val}].{self.column}" if self.sel_col else base


# ── value-shape stats (SQLite: computed in Python) ──────────────────────────────────────────────
_HEX12 = re.compile(r"^[0-9a-fA-F]{12,}$")
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}.*")
_NUM = re.compile(r"^[0-9]+$")


def _p95(lengths: list[int]) -> float:
    """95th-percentile of a length list (linear on the sorted values). Empty → 0."""
    if not lengths:
        return 0.0
    s = sorted(lengths)
    idx = min(len(s) - 1, int(0.95 * (len(s) - 1) + 0.5))
    return float(s[idx])


def _column_stats(con: sqlite3.Connection, tbl: str, col: str,
                  sel_col: str = "", sel_val: str = "") -> dict | None:
    """Non-null count, distinct, p95 length (whole + path tail), avg words, path fraction, and
    value-shape fractions for one column — the SQLite/Python analogue of upstream's DuckDB aggregate."""
    stmt = select(sa_column(col)).select_from(sa_table(tbl)).where(sa_column(col).is_not(None))
    if sel_col:
        stmt = stmt.where(sa_column(sel_col) == sel_val)
    try:
        vals = [str(r[0]) for r in db.run(con, stmt) if r[0] is not None and str(r[0]) != ""]
    except sqlite3.OperationalError:                     # the bridge preserves DBAPI error types
        return None
    if not vals:
        return None
    nn = len(vals)
    distinct = set(vals)
    lengths = [len(v) for v in vals]
    tail_lengths = [len(v.rsplit("/", 1)[-1]) for v in vals]
    return {
        "nn": nn,
        "ndistinct": len(distinct),
        "p95_len": _p95(lengths),
        "p95_tail_len": _p95(tail_lengths),
        "slash_f": sum("/" in v for v in vals) / nn,
        "avg_words": sum(v.count(" ") + 1 for v in vals) / nn,
        "url_f": sum(v.startswith("http") for v in vals) / nn,
        "num_f": sum(bool(_NUM.match(v)) for v in vals) / nn,
        "dt_f": sum(bool(_ISO_DT.match(v)) for v in vals) / nn,
        "hex_f": sum(bool(_HEX12.match(v)) for v in vals) / nn,
        "email_f": sum("@" in v for v in vals) / nn,
    }


def _denied_name(col: str) -> bool:
    return col in DENY_EXACT or any(s in col for s in DENY_SUBSTR)


def _rejected_reason(s: dict) -> str | None:
    """The first policy reason this column is NOT a good axis, or None if it passes (upstream verbatim)."""
    if s["ndistinct"] < 2:
        return "no-variety"
    if s["ndistinct"] <= ENUM_MAX_DISTINCT and s["nn"] >= ENUM_ROW_FLOOR:
        return f"enum({s['ndistinct']})"
    effective_len = s["p95_tail_len"] if s["slash_f"] > PATH_FRAC_MIN else s["p95_len"]
    if effective_len > P95_LEN_MAX:
        return f"p95_len={effective_len:.0f}"
    if s["avg_words"] > AVG_WORDS_MAX:
        return f"avg_words={s['avg_words']:.1f}"
    if s["url_f"] > URL_FRAC_MAX:
        return f"url={s['url_f']:.0%}"
    if s["num_f"] > NUM_FRAC_MAX:
        return f"numeric={s['num_f']:.0%}"
    if s["dt_f"] > DT_FRAC_MAX:
        return f"datetime={s['dt_f']:.0%}"
    if s["hex_f"] > HEX_FRAC_MAX:
        return f"hexid={s['hex_f']:.0%}"
    if s["email_f"] > EMAIL_FRAC_MAX:
        return f"email={s['email_f']:.0%}"
    return None


# ── entity-kind derivation (upstream verbatim) ──────────────────────────────────────────────────────
#: Generic role words (NOT connector field names) a reference column's path carries, mapped to the
#: KIND of entity it names. Used only to GROUP cross-column hits; a miss falls back to the table.
_KIND_TOKENS: tuple[tuple[str, str], ...] = (
    ("reporter", "person"), ("assignee", "person"), ("creator", "person"),
    ("committer", "person"), ("author", "person"), ("member", "person"), ("lead", "person"),
    ("user", "person"), ("login", "person"), ("owner", "person"), ("repo", "repo"),
    ("project", "project"), ("board", "board"), ("space", "space"), ("sprint", "sprint"),
    ("release", "release"), ("workspace", "workspace"), ("group", "group"), ("flag", "flag"),
    ("environment", "environment"), ("branch", "branch"),
)
#: Synonyms that must collapse to ONE kind so a reference column and an identity column for the same
#: entity agree.
_KIND_SYNONYMS = {"repository": "repo", "featureflag": "flag"}


def _singularize(word: str) -> str:
    """Naive English singular for a table-derived kind (``projects`` → ``project``)."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


def _kind_from_table(table: str) -> str:
    """A readable kind for a table's OWN identifier column. People-ish tables → ``person``; otherwise
    singularize. (CSA tables are not connector-prefixed, so upstream's source-strip step is a no-op.)"""
    name = table.removeprefix("List").lower()
    if any(tok in name for tok in ("user", "member", "person", "people")):
        return "person"
    return _singularize(name)


def referent_kind(table: str, column: str) -> str:
    """Best-effort KIND of the entity a column's values name: a role word in the column path
    (``user`` → person, ``repo`` → repo) else the table's collapsed-entity kind; both routed through
    ``_KIND_SYNONYMS`` so a reference kind and an identity kind for the SAME entity land on one label."""
    lowered = column.lower()
    for token, kind in _KIND_TOKENS:
        if token in lowered:
            return _KIND_SYNONYMS.get(kind, kind)
    kind = _kind_from_table(table)
    return _KIND_SYNONYMS.get(kind, kind)


# ── the selector ────────────────────────────────────────────────────────────────────────────────
# sqlite_master / PRAGMA introspection stays text(): SQLAlchemy reflection would normalize the
# raw declared-type strings (incl. the empty string for untyped columns) that _string_columns
# filters on — a byte-identity risk with zero upside. Identifiers come from introspection, never
# user input.
def _tables(con: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in db.run(con, text(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )).fetchall()
        if r[0] not in SKIP_TABLES and not r[0].startswith("sqlite_")
    ]


def _string_columns(con: sqlite3.Connection, table: str) -> list[str]:
    # SQLite is dynamically typed; accept TEXT-declared and untyped columns (skip INTEGER/REAL/BLOB).
    return [
        r[1]
        for r in db.run(con, text(f'PRAGMA table_info("{table}")')).fetchall()
        if (r[2] or "").upper() in ("", "TEXT", "VARCHAR", "CLOB") or "CHAR" in (r[2] or "").upper()
    ]


def _poly_types(con: sqlite3.Connection) -> list[str]:
    """The distinct ``entities.type`` values that become per-type axes — simple identifier tokens only
    (our own extraction emits alnum/underscore types), so they are safe to fold into an axis label."""
    # NO order_by, matching the raw SQL exactly: DISTINCT's result order feeds the axes list,
    # which feeds the tantivy build — the A/B dump compares axes as an ORDERED list.
    return [
        t
        for (t,) in db.run(con, select(sa_column(_POLY_KIND_COL)).distinct()
                           .select_from(sa_table(_POLY_TABLE))
                           .where(sa_column(_POLY_KIND_COL).is_not(None))).fetchall()
        if t and re.fullmatch(r"[A-Za-z0-9_]+", t)
    ]


def select_axes(con: sqlite3.Connection) -> list[Axis]:
    """Select every searchable identifier column in the store, as :class:`Axis` records. Pure read.
    Deterministic — same store → same axes. The polymorphic ``entities`` table is expanded per-type."""
    axes: list[Axis] = []
    # The session-owner column is the KNOWN person-scoping identity — inject it explicitly. The generic
    # stat selector rejects it (3 distinct owners over 1000s of rows reads as an "enum"; and it's an
    # email column, which upstream guards as noisy), but in CSA the owner email IS the canonical person
    # key, so it must be a person axis. upstream knows its own identity columns too; the stat scan is only
    # for DISCOVERING unknown ones. The generic loop re-evaluates 'user' and drops it (enum), no dup.
    if db.run(con, text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t")
              .bindparams(t=_SESSIONS.name)).fetchone():
        axes.append(Axis(_SESSIONS.name, _SESSIONS.c.user.name, "person"))
    for table in _tables(con):
        if table == _POLY_TABLE:
            # per-type axes: one axis per (column, type), kind == the type value itself.
            for kind in _poly_types(con):
                for col in _POLY_COLS:
                    stats = _column_stats(con, table, col, sel_col=_POLY_KIND_COL, sel_val=kind)
                    if stats is None or _rejected_reason(stats) is not None:
                        continue
                    axes.append(Axis(table, col, _KIND_SYNONYMS.get(kind, kind),
                                     sel_col=_POLY_KIND_COL, sel_val=kind))
            continue
        for col in _string_columns(con, table):
            if _denied_name(col):
                continue
            stats = _column_stats(con, table, col)
            if stats is None or _rejected_reason(stats) is not None:
                continue
            axes.append(Axis(table, col, referent_kind(table, col)))
    return axes
