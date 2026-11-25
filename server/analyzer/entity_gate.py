"""Entity noise gate — the serving floor over one-off entities.

ONE predicate decides "served", used identically by build_fts (entity docs rows), rerank's
graph-overlap neighbors, the session-detail entity list, and the
resolution engine's S4 candidate retrieval. An entity is NOT served iff the tenant's floor is on
and:

    type != 'person'                     -- people are what the product promises to remember; the
                                         -- entity card is the only structured answer for anyone
                                         -- off the Ask-X roster (route.roster = CSA accounts only)
    AND sessions <= 1                    -- alias-aware, AND the lowercased-name FAMILY is
    AND name_sessions <= 1               -- single-session (dedup hasn't merged case-variants)
    AND (   (dominant_n = 0 AND acted_n = 0 AND headline_n = 0 AND age > GRACE_DAYS)   -- floor
         OR age > entity_gc_days)                    -- GC: stale singleton, even a dominant one
    where age = days since last_seen (last activity of the entity's newest session)

GRACE_DAYS: a singleton hasn't had the CHANCE to recur — without it a demo-fresh tenant's
overlap boost and entity rows are disabled corpus-wide during exactly the week they evaluate.
FAIL-OPEN everywhere: no entity_stats row, unknown ids, unparseable timestamps, partial schemas
=> served. Fail-closed would silently un-serve entities that are served today, violating the
floor-off parity contract. Serving-side only: nothing here deletes or mutates
`entities`/`mentions` — the gate is reversible per tenant. NOT gated on purpose: resolve/ask
cards and rerank's q_ents (a typed identifier IS the anti-noise signal), HITL curation surfaces
(you can't merge what you can't see), aggregate counts (extraction ledger, not retrieval).

Configuration comes from the tenant, not the process: `analyzer.tenant.config(con)` resolves the
per-org `tenant_config` row (entity_floor, entity_gc_days).

STABLE API for the resolution engine: `analyzer.entity_gate.is_served_entity(con, entity_id)`.
"""
import sqlite3

from sqlalchemy import and_, func, literal, or_, select

from analyzer import db, tenant
from analyzer.models import Alias, Entity, EntityStats, EntityTypePin

GRACE_DAYS = 7           # floor only applies to singletons older than this
_ALIAS_CHAIN_MAX = 16    # merge chains are human-made and short; bound the walk regardless

_ALIASES = Alias.__table__


def gate_parts(cfg, e):
    """The Core face of the gate, for callers building SQLAlchemy statements: given the caller's
    `entities` alias/table `e`, returns (from_obj, served_expr) — the three LEFT JOINs applied to
    `e` (same order as gate_joins) and the served predicate as a ColumnElement usable both as a
    WHERE conjunct and as a labeled SELECT column. Same semantics as served_cond, including the
    age expression evaluated twice and gc_days from the typed config."""
    ga = _ALIASES.alias("ga")
    gs = EntityStats.__table__.alias("gs")
    gt = EntityTypePin.__table__.alias("gt")
    frm = (e.outerjoin(ga, ga.c.alias_id == e.c.id)
            .outerjoin(gs, gs.c.entity_id == func.ifnull(ga.c.canonical_id, e.c.id))
            .outerjoin(gt, gt.c.id == e.c.id))
    age = func.ifnull(func.julianday("now") - func.julianday(gs.c.last_seen), 0)
    expr = or_(
        gs.c.entity_id.is_(None),
        literal("person").in_([e.c.type, func.ifnull(gt.c.type, "")]),
        ~and_(gs.c.sessions <= 1,
              func.ifnull(gs.c.name_sessions, 2) <= 1,
              or_(and_(gs.c.dominant_n == 0, gs.c.acted_n == 0,
                       func.ifnull(gs.c.headline_n, 1) == 0, age > GRACE_DAYS),
                  age > int(cfg.entity_gc_days))))
    return frm, expr


def gate_joins(entities_alias="e"):
    """STRING face of the gate — single remaining consumer: store.build_fts's floor-on docs
    statement, which stays raw f-string SQL by design (the FTS build is byte-identity-pinned).
    Every other consumer uses gate_parts(). Retires if build_fts ever converts (P6+).
    LEFT JOINs the served predicate needs, given the `entities` table's alias in the caller's
    query. ga: alias rollup (a post-merge live drain writes alias ids into mentions until the
    next full index); gs: the derived stats keyed on canonical ids; gt: the HITL type pin (a
    human saying 'person' wins now, not at the next majority vote)."""
    return (f" LEFT JOIN aliases ga ON ga.alias_id={entities_alias}.id"
            f" LEFT JOIN entity_stats gs ON gs.entity_id=IFNULL(ga.canonical_id,{entities_alias}.id)"
            f" LEFT JOIN entity_types gt ON gt.id={entities_alias}.id")


def served_cond(cfg, entities_alias="e"):
    """SQL boolean over the gate_joins() aliases, for `cfg` = the caller's resolved
    tenant.TenantConfig. Interpolates only ints from the typed config (no user input).
    NULL stat columns (row written by an older shape) fail open via the IFNULL defaults;
    an unparseable last_seen makes julianday() NULL, so age collapses to 0 -> grace serves it."""
    age = "IFNULL(julianday('now')-julianday(gs.last_seen),0)"
    return (f"(gs.entity_id IS NULL OR 'person' IN ({entities_alias}.type, IFNULL(gt.type,''))"
            f" OR NOT (gs.sessions<=1 AND IFNULL(gs.name_sessions,2)<=1"
            f" AND ((gs.dominant_n=0 AND gs.acted_n=0 AND IFNULL(gs.headline_n,1)=0"
            f" AND {age}>{GRACE_DAYS})"
            f" OR {age}>{int(cfg.entity_gc_days)})))")


def _canonical_id(con, entity_id):
    """Resolve `entity_id` through merge chains (a->b->c) in the aliases table. Bounded walk:
    chains are produced by human merges and are at most a few hops; the bound also caps damage
    if a cycle ever slips past merge_entity."""
    seen = {entity_id}
    current = entity_id
    for _ in range(_ALIAS_CHAIN_MAX):
        row = db.run(con, select(_ALIASES.c.canonical_id)
                     .where(_ALIASES.c.alias_id == current)).fetchone()
        if not row or not row[0] or row[0] in seen:
            return current
        current = row[0]
        seen.add(current)
    return current


def is_served_entity(con, entity_id):
    """THE stable helper (resolution engine S4 imports this — keep name/location/signature).
    True when the tenant's floor is off, the id is unknown, or no stats row exists (fail-open).
    `con` = the tenant's corpus connection; any schema/lookup failure fails open — this API takes
    arbitrary caller connections and must never raise on a partial schema."""
    try:
        cfg = tenant.config(con)
        if not cfg.entity_floor:
            return True
        canonical = _canonical_id(con, entity_id)
        e = Entity.__table__.alias("e")
        frm, expr = gate_parts(cfg, e)
        row = db.run(con, select(expr).select_from(frm)
                     .where(e.c.id == canonical)).fetchone()
    except sqlite3.OperationalError:
        return True
    return bool(row[0]) if row else True


def served_ids(con, entity_ids):
    """Subset of `entity_ids` that pass the gate, for batch callers (rerank's neighbor set).
    Ids with no `entities` row fail open (relations can reference ids the entities table lost),
    as does any schema failure."""
    entity_ids = set(entity_ids)
    if not entity_ids:
        return entity_ids
    ordered = sorted(entity_ids)
    try:
        cfg = tenant.config(con)
        if not cfg.entity_floor:
            return entity_ids
        ent = Entity.__table__
        known = {row[0] for row in db.run(con, select(ent.c.id)
                                          .where(ent.c.id.in_(ordered)))}
        e = ent.alias("e")
        frm, expr = gate_parts(cfg, e)
        kept = {row[0] for row in db.run(con, select(e.c.id).select_from(frm)
                                         .where(e.c.id.in_(ordered), expr))}
    except sqlite3.OperationalError:
        return entity_ids
    return kept | (entity_ids - known)
