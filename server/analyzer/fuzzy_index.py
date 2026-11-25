"""The fuzzy identifier index behind cross-axis "search everywhere".

Ported from an upstream project's fuzzy_index. The two-stage RECALL→PRECISION architecture, the Tantivy FST,
the surface tiers, cross-axis fold, FAMILY collapse, unique-containment demotion, and the prefix arm
are upstream's, verbatim. Only candidate GENERATION from the store is re-implemented for CSA's SQLite
schema (upstream pulls from DuckDB axes + declared ``table_sources``; CSA has no declared source
systems, so ``axis_sources`` is empty and the ``sources`` display field stays blank — which the
downstream fold already handles gracefully).

* **Recall (this module)** — every auto-selected identifier value's SURFACES
  (:mod:`analyzer.fuzzy_axes` picks the axes) live as raw terms in a Tantivy FST, intersected at
  query time with a Levenshtein automaton (transpositions cost 1). Retrieval returns every surface
  within distance ``MAX_EDIT``, verified against true Damerau — no n-gram fragility
  (``cluade`` finds ``claude``), no recall holes to tune.
* **Precision (:mod:`analyzer.fuzzy_resolve`)** — the retrieved candidates are reranked per axis
  with Damerau + accept/margin/abstain; the decision and anti-hallucination guarantee live there.

Hits then collapse per ``(kind, normalized tail)`` so one entity isn't surfaced on N columns, with
same-axis populations above ``FAMILY_MIN`` collapsed into one ``n_members`` family.
"""

from __future__ import annotations

import functools
import pickle
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tantivy
from rapidfuzz.distance import DamerauLevenshtein
from sqlalchemy import select
from sqlalchemy import column as sa_column, table as sa_table

from analyzer import db
from analyzer.fuzzy_axes import select_axes
from analyzer.fuzzy_resolve import (
    DEFAULT_MARGIN,
    Resolution,
    Status,
    _surfaces,
    normalize,
    resolve,
)

#: max edit distance the retrieval guarantees. Also the Levenshtein-automaton ceiling — tantivy ≤ 2.
MAX_EDIT = 2
#: safety cap on candidates handed to the rerank; FAIR-SHARED across axes (see :func:`search`).
DEFAULT_TOPK = 500
#: a mention shorter than this does NOT get the prefix arm — a 1-3 char prefix floods.
MIN_PREFIX_LEN = 4
#: cap on surfaces a single prefix lookup returns — bounds a pathological prefix.
PREFIX_SURFACE_CAP = 500
#: more same-axis hits than this is a FAMILY, reported as ONE entity with ``n_members``.
FAMILY_MIN = 5
#: prefix hits map their length-ratio into this score band — strictly below an EXACT hit (1.0).
_PREFIX_BAND = (0.80, 0.95)


def _prefix_score(query: str, surface: str) -> float:
    """Length-ratio of a prefix match, mapped into ``_PREFIX_BAND`` — a closer-length prefix scores
    higher (``mokafor`` of ``mokafor42`` beats ``mok`` of a long branch name)."""
    lo, hi = _PREFIX_BAND
    return lo + (hi - lo) * (len(query) / len(surface))


@dataclass(frozen=True, slots=True)
class Candidate:
    """One recall-stage hit: a real indexed value plus the axis/kind it came from."""

    value: str
    axis: str  # "Table.column"
    kind: str


@dataclass(slots=True)
class ResolvedEntity:
    """A rerank+collapse result: one entity, its best score/status, and the axes that matched it."""

    kind: str
    value: str  # the highest-scoring surface form
    best_score: float = 0.0
    resolved: bool = False
    axes: list[dict] = field(default_factory=list)
    #: >1 marks a FAMILY: this many same-axis values match the mention.
    n_members: int = 1
    #: the source systems the matched axes live in (display metadata; empty on CSA — no declared
    #: sources). Aggregated by :func:`annotate_sources`. NOT part of the grouping key.
    sources: list[str] = field(default_factory=list)


_FUZZY_SUFFIX = ".fuzzy"
_SURFACES_FILE = "surfaces.pkl"
_AXIS_SOURCES_FILE = "axis_sources.pkl"
_FST_DIR = "fst"
_SURFACE_FIELD = "surface"


def fuzzy_sibling(db_path: Path) -> Path:
    """The ``<db>.fuzzy`` sibling dir that belongs to a store db path."""
    return db_path.with_suffix(_FUZZY_SUFFIX)


def _fst_schema() -> tantivy.Schema:
    """One raw-tokenized stored text field: each SURFACE is one term in the FST."""
    sb = tantivy.SchemaBuilder()
    sb.add_text_field(_SURFACE_FIELD, stored=True, tokenizer_name="raw")
    return sb.build()


def _build_fst(surfaces: Iterable[str], path: Path | None) -> tantivy.Index:
    """Index ``surfaces`` as raw terms — in-RAM when ``path`` is None, on disk when persisted."""
    schema = _fst_schema()
    if path is None:
        index = tantivy.Index(schema)
    else:
        path.mkdir(parents=True, exist_ok=True)
        index = tantivy.Index(schema, path=str(path))
    writer = index.writer(heap_size=50_000_000)
    for surface in sorted(surfaces):
        writer.add_document(tantivy.Document(**{_SURFACE_FIELD: surface}))
    writer.commit()
    writer.wait_merging_threads()
    index.reload()
    return index


class FuzzyIndex:
    """The recall index: a Tantivy FST whose terms are the normalized surfaces, plus ONE plain dict
    ``by_surface`` mapping a surface → the candidates carrying it. Built in-process; persistable."""

    __slots__ = ("_fst", "_searcher", "_schema", "_by_surface", "axis_sources")

    def __init__(
        self,
        fst: tantivy.Index,
        by_surface: dict[str, list[tuple[str, str, str]]],
        axis_sources: dict[str, str] | None = None,
    ) -> None:
        self._fst = fst
        self._schema = _fst_schema()
        self._searcher = fst.searcher()
        self._by_surface = by_surface
        self.axis_sources: dict[str, str] = axis_sources or {}

    def surfaces_within(self, query: str, distance: int, cap: int) -> list[str]:
        """Surfaces within Levenshtein distance ``distance`` of ``query`` (transpositions cost 1)."""
        q = tantivy.Query.fuzzy_term_query(
            self._schema, _SURFACE_FIELD, query, distance=distance, transposition_cost_one=True
        )
        hits = self._searcher.search(q, cap).hits
        return [self._searcher.doc(d)[_SURFACE_FIELD][0] for _, d in hits]

    def surfaces_with_prefix(self, query: str, cap: int) -> list[str]:
        """Surfaces that STRICTLY start with ``query``, up to ``cap``, lexicographic."""
        q = tantivy.Query.fuzzy_term_query(
            self._schema, _SURFACE_FIELD, query, distance=0, prefix=True
        )
        hits = self._searcher.search(q, cap + 1).hits  # +1: the exact surface may be among them
        out = sorted(s for _, d in hits if (s := self._searcher.doc(d)[_SURFACE_FIELD][0]) != query)
        return out[:cap]

    @property
    def n_surfaces(self) -> int:
        return len(self._by_surface)

    def save(self, out_dir: str | Path) -> None:
        """Persist the FST + ``by_surface`` (+ axis→source map) via ``.tmp``-then-``replace``."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        _build_fst(self._by_surface, out / _FST_DIR)
        for name, payload in (
            (_SURFACES_FILE, self._by_surface),
            (_AXIS_SOURCES_FILE, self.axis_sources),
        ):
            tmp = out / (name + ".tmp")
            with tmp.open("wb") as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(out / name)

    @classmethod
    def load(cls, out_dir: str | Path) -> FuzzyIndex:
        """Load a persisted index (raises if absent or a pre-FST artifact — caller falls back)."""
        out = Path(out_dir)
        with (out / _SURFACES_FILE).open("rb") as fh:
            by_surface = pickle.load(fh)  # noqa: S301 — our own artifact, not untrusted input
        axis_sources: dict[str, str] = {}
        sources_file = out / _AXIS_SOURCES_FILE
        if sources_file.exists():
            with sources_file.open("rb") as fh:
                axis_sources = pickle.load(fh)  # noqa: S301 — our own artifact
        return cls(tantivy.Index(_fst_schema(), path=str(out / _FST_DIR)), by_surface, axis_sources)


def _tail(value: str) -> str:
    """The path tail (after the last ``/``) — the owner/name split the resolver + collapse use."""
    return value.rsplit("/", 1)[-1]


def _surfaces_of(value: str) -> set[str]:
    """Normalized match surfaces of a value (whole, path tail, words, delimiter tokens). DELEGATES to
    :func:`analyzer.fuzzy_resolve._surfaces` with ``word_tokens=True`` so recall + rerank agree."""
    return {norm for _, norm, _ in _surfaces(value, word_tokens=True)}


# ── candidate generation (CSA SQLite) ───────────────────────────────────────────────────────────
def candidates_from_store(con: sqlite3.Connection) -> Iterator[Candidate]:
    """Every distinct identifier value across every auto-selected axis, tagged with its axis + kind.
    The polymorphic ``entities`` axes carry a parametrized ``WHERE sel_col = sel_val`` predicate."""
    for ax in select_axes(con):  # ax.table / ax.column come from introspection, never user input —
        col = sa_column(ax.column)               # hence the lightweight sa.table()/sa.column() build
        stmt = (select(col.label("v")).distinct()   # (the fuzzy_axes._column_stats precedent)
                .select_from(sa_table(ax.table))
                .where(col.is_not(None)))
        if ax.sel_col:
            stmt = stmt.where(sa_column(ax.sel_col) == ax.sel_val)
        for (value,) in db.run(con, stmt).fetchall():
            if value:
                yield Candidate(str(value), ax.key, ax.kind)


def build_full(
    candidates: Iterable[Candidate], axis_sources: dict[str, str] | None = None
) -> FuzzyIndex:
    """Build the index from ``candidates``: the surface → candidates dict plus an in-RAM FST."""
    by_surface: dict[str, list[tuple[str, str, str]]] = {}
    for c in candidates:
        entry = (c.value, c.axis, c.kind)
        for surface in _surfaces_of(c.value):
            by_surface.setdefault(surface, []).append(entry)
    return FuzzyIndex(_build_fst(by_surface, None), by_surface, axis_sources)


def build_index_from_store(con: sqlite3.Connection) -> FuzzyIndex:
    """Build the in-memory index from every auto-selected identifier value in ``con``. CSA has no
    declared per-axis source systems, so ``axis_sources`` is empty (sources display stays blank)."""
    return build_full(candidates_from_store(con), axis_sources={})


@functools.lru_cache(maxsize=4)
def _cached_load(sibling: str, _mtime: float) -> FuzzyIndex:
    """mtime in the key busts the cache when the offline build rewrites the artifact (a new generation)."""
    return FuzzyIndex.load(sibling)


def load_index(db_path: str | Path) -> FuzzyIndex | None:
    """Load the persisted ``<db>.fuzzy`` index for ``db_path`` (the serve path: mmap once per generation,
    cached by artifact mtime). Returns None when the sibling is absent — the caller can build in-RAM
    from the store (:func:`build_index_from_store`) or skip fuzzy resolution entirely."""
    sib = fuzzy_sibling(Path(db_path))
    marker = sib / _SURFACES_FILE
    if not marker.exists():
        return None
    try:
        return _cached_load(str(sib), marker.stat().st_mtime)
    except Exception:                                    # a half-written / pre-FST artifact -> caller rebuilds
        return None


def search(index: FuzzyIndex, mention: str, *, limit: int = DEFAULT_TOPK) -> list[Candidate]:
    """Recall stage: every candidate within Damerau distance ``MAX_EDIT`` of ``mention``, nearest
    first. Automaton∩FST lookup, then VERIFY true Damerau ≤ ``MAX_EDIT`` per surface."""
    query = normalize(mention)
    if not query:
        return []
    surfaces: set[str] = {query} if query in index._by_surface else set()
    for distance in range(1, MAX_EDIT + 1):
        surfaces.update(index.surfaces_within(query, distance, cap=4 * limit))

    # Iterate surfaces nearest-first (ties lexicographic) BEFORE the (value, axis) dedup, so each
    # candidate is kept at its BEST distance and retrieval order is deterministic.
    ranked = sorted(
        (d, s) for s in surfaces if (d := DamerauLevenshtein.distance(query, s)) <= MAX_EDIT
    )
    per_axis: dict[str, list[Candidate]] = {}
    seen: set[tuple[str, str]] = set()
    for _, surface in ranked:
        for value, axis, kind in index._by_surface.get(surface, ()):
            key = (value, axis)
            if key not in seen:
                seen.add(key)
                per_axis.setdefault(axis, []).append(Candidate(value, axis, kind))

    # FAIR-SHARE the cap across axes (round-robin, nearest-first per axis) — a flat fill starves whole
    # axes; the rerank judges each axis's pool independently, so every axis's nearest must arrive.
    out: list[Candidate] = []
    queues = list(per_axis.values())
    rank = 0
    while len(out) < limit:
        progressed = False
        for queue in queues:
            if rank < len(queue):
                out.append(queue[rank])
                progressed = True
                if len(out) >= limit:
                    return out
        if not progressed:
            return out
        rank += 1
    return out


def _fold_family(
    groups: dict[tuple[str, str], ResolvedEntity],
    *,
    axis: str,
    kind: str,
    value: str,
    score: float,
    n_members: int,
    via_token: bool = False,
    source: str = "",
) -> None:
    """Fold a same-axis FAMILY into ``groups`` as ONE entity keyed per axis."""
    fkey = (kind, f"__family__:{axis}")
    entity = groups.get(fkey)
    if entity is None:
        entity = groups[fkey] = ResolvedEntity(kind=kind, value=value)
    entity.n_members = max(entity.n_members, n_members)
    entity.axes.append(
        {
            "axis": axis,
            "value": value,
            "score": score,
            "status": "AMBIGUOUS",
            "via_token": via_token,
            "source": source,
            "n_members": n_members,
        }
    )
    if score > entity.best_score:
        entity.best_score = score
        entity.value = value


def fold_axis_resolution(
    groups: dict[tuple[str, str], ResolvedEntity],
    axis: str,
    kind: str,
    r: Resolution,
    *,
    min_score: float,
    source: str = "",
) -> None:
    """Fold one axis's rerank Resolution into the cross-axis entity ``groups``. ABSTAIN never
    contributes; RESOLVED contributes its single winner; AMBIGUOUS contributes its contenders (within
    ``DEFAULT_MARGIN``); >``FAMILY_MIN`` contenders in one axis is a FAMILY."""
    if r.status is Status.ABSTAIN or not r.candidates:
        return
    if r.status is Status.RESOLVED:
        tops = [next((c for c in r.candidates if c.value == r.value), r.candidates[0])]
    else:
        lead = r.candidates[0].score
        tops = [c for c in r.candidates if lead - c.score < DEFAULT_MARGIN]
    tops = [c for c in tops if c.score >= min_score]
    if len(tops) > FAMILY_MIN:
        _fold_family(
            groups,
            axis=axis,
            kind=kind,
            value=tops[0].value,
            score=tops[0].score,
            n_members=len(tops),
            via_token=r.via_token,
            source=source,
        )
        return
    for top in tops:
        gkey = (kind, normalize(_tail(top.value)))
        entity = groups.get(gkey)
        if entity is None:
            entity = groups[gkey] = ResolvedEntity(kind=kind, value=top.value)
        entity.axes.append(
            {
                "axis": axis,
                "value": top.value,
                "score": top.score,
                "status": str(r.status),
                "via_token": r.via_token,
                "source": source,
            }
        )
        if top.score > entity.best_score:
            entity.best_score = top.score
            entity.value = top.value
        if r.status is Status.RESOLVED:
            entity.resolved = True


def enforce_unique_containment(entities: Iterable[ResolvedEntity]) -> None:
    """Demote a token-containment RESOLVED that is not the mention's UNIQUE containment entity across
    all axes (the same-name-many-axes incoherence). Identity entities neither demote nor get demoted; prefix
    resolves (no ``via_token``) are untouched. Mutates in place."""
    ents = list(entities)
    containment: list[ResolvedEntity] = []
    for e in ents:
        has_token = any(a["score"] >= 1.0 and a.get("via_token") for a in e.axes)
        has_identity = any(a["score"] >= 1.0 and not a.get("via_token") for a in e.axes)
        if has_token and not has_identity:
            containment.append(e)
    if len(containment) <= 1:
        return
    resolved_str = str(Status.RESOLVED)
    for e in containment:
        resolved_axes = [a for a in e.axes if a["status"] == resolved_str]
        if (
            not e.resolved
            or not resolved_axes
            or not all(a.get("via_token") for a in resolved_axes)
        ):
            continue
        e.resolved = False
        for a in resolved_axes:
            a["status"] = str(Status.AMBIGUOUS)


def annotate_sources(entities: Iterable[ResolvedEntity]) -> None:
    """Aggregate each entity's ``sources`` from its matched axes' declared ``source`` stamps (empty on
    CSA). Purely additive display metadata."""
    for e in entities:
        e.sources = sorted({src for a in e.axes if (src := a.get("source", ""))})


def resolve_everywhere(
    index: FuzzyIndex, mention: str, *, min_score: float = 0.66, limit: int = DEFAULT_TOPK
) -> list[ResolvedEntity]:
    """Two-stage cross-axis resolve: recall candidates, rerank per axis with
    :func:`analyzer.fuzzy_resolve.resolve` (``word_tokens``), collapse per ``(kind, normalized
    tail)``, families above ``FAMILY_MIN``, containment gated by :func:`enforce_unique_containment`,
    plus the prefix arm. Confident entities first, then unresolved singles, then families."""
    per_axis: dict[str, tuple[str, list[str]]] = {}
    for c in search(index, mention, limit=limit):
        per_axis.setdefault(c.axis, (c.kind, []))[1].append(c.value)

    groups: dict[tuple[str, str], ResolvedEntity] = {}
    for axis, (kind, values) in per_axis.items():
        r = resolve(mention, values, word_tokens=True)
        fold_axis_resolution(
            groups, axis, kind, r, min_score=min_score, source=index.axis_sources.get(axis, "")
        )

    # PREFIX ARM: a mention that is a strict PREFIX of a longer id (``mokafor`` → ``mokafor42``) is
    # intentional narrowing, not a typo — bounded-edit retrieval + Damerau both miss it.
    query = normalize(mention)
    if len(query) >= MIN_PREFIX_LEN and not query.isdigit():
        prefix_surfaces = index.surfaces_with_prefix(query, PREFIX_SURFACE_CAP)
        per_axis_hits: dict[str, dict[str, tuple[str, float]]] = {}
        for surface in prefix_surfaces:
            score = _prefix_score(query, surface)
            for value, axis, kind in index._by_surface.get(surface, ()):
                cur = per_axis_hits.setdefault(axis, {}).get(value)
                if cur is None or score > cur[1]:
                    per_axis_hits[axis][value] = (kind, score)

        singles: dict[tuple[str, str], list[tuple[str, str, float]]] = {}
        families: list[tuple[str, str, str, float, int]] = []
        for axis, hits in per_axis_hits.items():
            if len(hits) > FAMILY_MIN:
                value, (kind, score) = max(hits.items(), key=lambda kv: kv[1][1])
                families.append((axis, kind, value, score, len(hits)))
            else:
                for value, (kind, score) in hits.items():
                    singles.setdefault((kind, normalize(_tail(value))), []).append(
                        (axis, value, score)
                    )
        # unique narrowing → confident; never when a family matched, never when the surface scan hit
        # its cap, never when the mention already EXACT-matched other entities OF THE SAME KIND.
        single_kinds = {kind for kind, _ in singles}
        exact_elsewhere = any(
            g.best_score >= 1.0 and gkey not in singles and gkey[0] in single_kinds
            for gkey, g in groups.items()
        )
        prefix_resolved = (
            len(singles) == 1
            and not families
            and len(prefix_surfaces) < PREFIX_SURFACE_CAP
            and not exact_elsewhere
        )
        for gkey, members in singles.items():
            kind = gkey[0]
            _, best_value, best_score = max(members, key=lambda m: m[2])
            entity = groups.get(gkey)
            if entity is None:
                entity = groups[gkey] = ResolvedEntity(kind=kind, value=best_value)
            for axis, value, score in members:
                entity.axes.append(
                    {
                        "axis": axis,
                        "value": value,
                        "score": score,
                        "status": "RESOLVED" if prefix_resolved else "AMBIGUOUS",
                        "source": index.axis_sources.get(axis, ""),
                    }
                )
            if best_score > entity.best_score:
                entity.best_score = best_score
                entity.value = best_value
            if prefix_resolved:
                entity.resolved = True
        for axis, kind, value, score, n in families:
            _fold_family(
                groups,
                axis=axis,
                kind=kind,
                value=value,
                score=score,
                n_members=n,
                source=index.axis_sources.get(axis, ""),
            )

    enforce_unique_containment(groups.values())
    annotate_sources(groups.values())

    entities = sorted(
        groups.values(), key=lambda e: (not e.resolved, e.n_members > 1, -e.best_score)
    )
    for e in entities:
        e.axes.sort(key=lambda a: a["score"], reverse=True)
    return entities
