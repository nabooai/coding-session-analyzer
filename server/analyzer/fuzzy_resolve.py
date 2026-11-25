"""Connector-agnostic fuzzy resolution of a free-text mention to a canonical value.

Ported verbatim from an upstream project's fuzzy_resolve (it is pure and store-free, so it copies unchanged).

A user types a loose token for an entity — ``orbitai`` for the repo ``orbit-ai``, ``jules`` for the
owner ``jules@example.com`` — but the data stores the canonical form. This module maps the mention to a
value drawn ONLY from a caller-supplied, enumerable candidate pool (session owners, entity names,
project keys, …). It is pure and store-free: the caller fetches the pool, this module decides.

The contract is anti-hallucination by construction — every returned value is a member of the
pool, never a fabricated string. The matcher is deliberately conservative, because string
distance ALONE cannot tell a typo of a real value (``orbitai`` → ``orbit-ai``) from the name of
an absent one (``orbit-cli`` is also one edit from ``orbit-ai``). So it only ``RESOLVED``s on an
exact normalized hit or a confident, unambiguous lead; a short token requires an exact hit
(a single edit on a 1–4 char key lands within distance 1 of almost everything); otherwise it
reports ``AMBIGUOUS`` (ranked "did you mean" candidates) or ``ABSTAIN`` rather than guess.

Exact hits come in two TIERS: an IDENTITY hit (the mention IS the candidate — its whole value or
path tail) and, under ``word_tokens``, a TOKEN hit (the candidate merely CONTAINS the mention as a
word/delimiter component — ``jules`` in ``jules@example.com``). Identity outranks containment, and a
containment-based decision is flagged ``via_token`` so the cross-axis layer can refuse to let "the
only value in THIS pool carrying the token" masquerade as "THE entity the mention names".
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz.distance import DamerauLevenshtein

#: Tokens whose normalized length is at or below this require an EXACT normalized hit — fuzzy
#: matching a short token is unsafe (one edit on a 3–4 char key matches almost anything).
SHORT_EXACT_LEN = 4
#: Default minimum similarity in [0, 1] for a fuzzy (non-exact) ``RESOLVED``. Tuned so a SINGLE edit
#: on a short token clears it (``hermse``→``hermes`` ≈ 0.83, ``orbit-ia``→``orbit-ai`` ≈ 0.86) while a
#: two-edit miss does not (``orbitmeshhh``→``orbitmesh`` ≈ 0.82-, ``orbitcli``→``orbitai`` ≈ 0.75).
DEFAULT_ACCEPT = 0.82
#: Default minimum lead of the top candidate over the runner-up to ``RESOLVED`` (else AMBIGUOUS).
DEFAULT_MARGIN = 0.08
#: How many ranked candidates to carry back as the "did you mean" payload.
_TOP_K = 5
#: A delimiter token shorter than this is never a match surface — a 1-char fragment matches nothing
#: a user meant.
_MIN_DELIM_TOKEN_LEN = 2


class Status(StrEnum):
    """The outcome of a resolution attempt."""

    #: a single confident match — :attr:`Resolution.value` is the canonical value to use.
    RESOLVED = "RESOLVED"
    #: several plausible matches and no clear winner — surface :attr:`Resolution.candidates`.
    AMBIGUOUS = "AMBIGUOUS"
    #: nothing close enough — do not guess.
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class Scored:
    """A candidate paired with its similarity to the mention."""

    #: a real member of the candidate pool (never a fabricated string).
    value: str
    #: similarity in [0, 1] — 1.0 means an exact match after normalization.
    score: float


@dataclass(frozen=True, slots=True)
class Resolution:
    """The decision: a status, the chosen value (only when ``RESOLVED``), and ranked candidates."""

    status: Status
    #: the canonical value when ``status is Status.RESOLVED``, else ``None``.
    value: str | None
    #: candidates ranked best-first (the "did you mean" list); may be empty on ABSTAIN.
    candidates: tuple[Scored, ...]
    #: True when the exact hit(s) behind this decision landed on a TOKEN surface (the candidate
    #: CONTAINS the mention as a word/delimiter component) rather than an IDENTITY surface (the
    #: candidate IS the mention — whole value / path tail). A containment RESOLVED is a weaker
    #: claim: within one pool it is the only value carrying the token, but the cross-axis layer
    #: (:func:`analyzer.fuzzy_index.enforce_unique_containment`) demotes it when other entities
    #: elsewhere carry the same token — a lone-in-its-axis hit is an axis-cardinality accident,
    #: not confidence.
    via_token: bool = False


def normalize(text: str) -> str:
    """Casefold + NFKC + drop every non-alphanumeric character.

    Collapses the case/separator/compound noise that dominates real input — ``orbit-ai``,
    ``Orbit_AI`` and ``orbitai`` all become ``orbitai``; ``HERMES`` becomes ``hermes`` — so an
    exact lookup on the normalized form catches the bulk of variation before any edit-distance.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _alnum_runs(form: str) -> Iterator[str]:
    """Maximal alphanumeric runs of ``form`` — the components of a compound identifier
    (``production-acme`` → ``production``, ``acme``). Same character class as :func:`normalize`."""
    run: list[str] = []
    for ch in form:
        if ch.isalnum():
            run.append(ch)
        elif run:
            yield "".join(run)
            run = []
    if run:
        yield "".join(run)


def _surfaces(value: str, *, word_tokens: bool = False) -> tuple[tuple[str, str, bool], ...]:
    """``(raw, normalized, is_token)`` surface forms of a candidate: the whole value, its path tail,
    and (opt-in) its component tokens.

    The path tail (after the last ``/``) lets a bare ``orbit-ai`` resolve a value stored as
    ``orbitai/orbit-ai`` — a structural ``owner/name`` split, not connector-specific knowledge. The
    RAW surface is carried alongside the normalized one so a caller can tell a genuine match from a
    normalization ARTIFACT (e.g. a numeric mention exact-matching a version tail only because ``.``
    was stripped — see :func:`resolve`). Deduped by normalized form.

    With ``word_tokens`` the individual COMPONENTS become surfaces too: whitespace-separated words
    (``jules`` → ``Jules Renner``, ``ana`` → ``Ana Reyes``) and delimiter-separated alphanumeric
    runs of a compound identifier (``jules`` → ``jules@example.com``), so one token of a compound
    name resolves it. Pure-DIGIT runs are never emitted (``2.0.9`` must not gain a ``2`` surface —
    a digit fragment of a version/id is the adjacent-id hazard, see the numeric guard in
    :func:`resolve`); whole-value digit WORDS keep their surface (``Sprint 42``). Off by default:
    the scope-autocorrect path keeps its tuned whole-string behaviour; only cross-axis
    "search everywhere" opts in.

    ``is_token`` splits the surfaces into two TIERS: an IDENTITY surface (the whole value, its path
    tail, a path suffix — matching it means the candidate IS the mention) vs a TOKEN surface (a word
    or delimiter run — matching it means the candidate merely CONTAINS the mention as a component).
    :func:`resolve` uses the tier to keep containment from masquerading as identity. Identity forms
    are emitted first, so a token that collapses to the same normalized form dedups onto the identity
    surface (a single-word value's own token is the value).
    """
    forms = [value]
    if "/" in value:
        forms.append(value.rsplit("/", 1)[-1])
    raws: list[tuple[str, bool]] = [(r, False) for r in forms]
    if word_tokens:
        # progressive path SUFFIXES: a user recalls the last segments of a deep path
        # (``bitbucket/prs.py`` for ``etl/src/integrations/bitbucket/prs.py``), so the 2- and
        # 3-segment tails are surfaces too (the 1-segment tail is already a form above).
        segments = value.split("/")
        raws.extend(("/".join(segments[-k:]), False) for k in (2, 3) if len(segments) > k)
        for form in forms:
            raws.extend((word, True) for word in form.split())
            raws.extend(
                (tok, True)
                for tok in _alnum_runs(form)
                if len(tok) >= _MIN_DELIM_TOKEN_LEN and not tok.isdigit()
            )
    out: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for raw, is_token in raws:
        norm = normalize(raw)
        if norm and norm not in seen:
            seen.add(norm)
            out.append((raw, norm, is_token))
    return tuple(out)


def _similarity(query_norm: str, surface: str) -> float:
    """Damerau-Levenshtein similarity in [0, 1] (1.0 == identical), 0 when either side is empty."""
    if not query_norm or not surface:
        return 0.0
    distance = DamerauLevenshtein.distance(query_norm, surface)
    return 1.0 - distance / max(len(query_norm), len(surface))


def resolve(
    mention: str,
    candidates: Iterable[str],
    *,
    accept: float = DEFAULT_ACCEPT,
    margin: float = DEFAULT_MARGIN,
    word_tokens: bool = False,
) -> Resolution:
    """Resolve ``mention`` to one of ``candidates``, or report AMBIGUOUS / ABSTAIN.

    Order of decision: exact IDENTITY hit — the mention IS a candidate's whole value / path tail
    (unique → RESOLVED; several values collapsing to the same normalized form → AMBIGUOUS) → exact
    TOKEN hit — the mention is a word/delimiter COMPONENT of a candidate, ``word_tokens`` only
    (unique → RESOLVED flagged ``via_token``; several → AMBIGUOUS: the verdict says which candidates
    CONTAIN the mention, not which one IS it) → short-token guard (no exact hit and
    ``len <= SHORT_EXACT_LEN`` → ABSTAIN; an exact hit of either tier bypasses it — ``foo`` must
    still hit the token of ``prod_foo``) → fuzzy: the top candidate ``RESOLVED``s only if it clears
    ``accept`` AND leads the runner-up by ``margin`` (or is the lone candidate); a close pack is
    AMBIGUOUS; nothing close is ABSTAIN. The returned value is always a member of ``candidates``.

    ``word_tokens`` also matches a mention against each WHITESPACE-separated word of a candidate, so a
    single word resolves a multi-word name (``jules`` → ``Jules Renner``). Off by default — see
    :func:`_surfaces`.
    """
    query_norm = normalize(mention)
    pool = [c for c in dict.fromkeys(candidates) if c]
    if not query_norm or not pool:
        return Resolution(Status.ABSTAIN, None, ())

    # numeric means the user typed a BARE number ("209") — the adjacent-id / version-artifact hazard.
    # Judged on the RAW mention, not the normalized form: "2.25.3" normalizes to digits too, but a
    # user pasting the exact version string is a genuine hit, not an artifact (it used to ABSTAIN).
    numeric_query = mention.strip().isdigit()
    scored: list[Scored] = []
    exact_identity: list[str] = []
    exact_token: list[str] = []
    for candidate in pool:
        best = 0.0
        exact_is_token: bool | None = None  # None = no exact hit; else the matched surface's tier
        for raw, surface, is_token in _surfaces(candidate, word_tokens=word_tokens):
            if surface == query_norm:
                best = 1.0
                # A numeric mention only counts an exact hit when the RAW surface is numeric too —
                # otherwise the match is a separator-stripping ARTIFACT: a version tail ``2.0.9``
                # normalizes to the digits ``209`` and spuriously equals the number ``209``. A genuine
                # numeric candidate (``100234``) still resolves; the fabricated version collision does
                # not (it falls through to the numeric guard below and ABSTAINs).
                if not numeric_query or raw.isdigit():
                    exact_is_token = is_token
                break
            best = max(best, _similarity(query_norm, surface))
        if exact_is_token is False:
            exact_identity.append(candidate)
        elif exact_is_token is True:
            exact_token.append(candidate)
        scored.append(Scored(candidate, best))
    scored.sort(key=lambda s: s.score, reverse=True)
    top = tuple(scored[:_TOP_K])

    if exact_identity:
        # the mention IS one of these values — an identity hit outranks any containment hit (a value
        # that merely CONTAINS the mention as a token never blocks or ties the value that IS it).
        if len(exact_identity) == 1:
            return Resolution(Status.RESOLVED, exact_identity[0], top)
        # several DISTINCT values share one normalized form — genuinely ambiguous, never pick one.
        return Resolution(
            Status.AMBIGUOUS, None, tuple(s for s in scored if s.value in set(exact_identity))
        )
    if exact_token:
        # containment tier: the mention is a COMPONENT of these values, not the whole of any. A lone
        # carrier resolves within this pool but the Resolution says so (``via_token``) — the caller's
        # cross-axis layer demotes it when the token turns out to name other entities elsewhere.
        if len(exact_token) == 1:
            return Resolution(Status.RESOLVED, exact_token[0], top, via_token=True)
        return Resolution(
            Status.AMBIGUOUS,
            None,
            tuple(s for s in scored if s.value in set(exact_token)),
            via_token=True,
        )

    if len(query_norm) <= SHORT_EXACT_LEN or numeric_query:
        # No exact hit and EITHER a short token (one edit on a 3–4 char key matches almost anything)
        # OR a purely-numeric token (an adjacent id like 100235→100234 is a DIFFERENT entity, never a
        # recoverable "typo") → abstain. Still hand back the ranked pack for a "did you mean".
        return Resolution(Status.ABSTAIN, None, top)

    best = scored[0]
    # Length-aware accept: ONE edit must always be acceptable on a fuzz-eligible token, but a single
    # edit on a 5-char token scores 0.80 < the global 0.82 ("usres"→"users" abstained while
    # "hermse"→"hermes", 6 chars, resolved). Never RAISES the bar — only relaxes it to exactly the
    # one-edit similarity of a query this short.
    accept_eff = min(accept, (len(query_norm) - 1) / len(query_norm))
    if best.score < accept_eff:
        return Resolution(Status.ABSTAIN, None, top)
    runner_up = scored[1].score if len(scored) > 1 else 0.0
    if best.score - runner_up >= margin:
        return Resolution(Status.RESOLVED, best.value, top)
    # A close pack larger than the display top-K is returned WHOLE: the caller's family collapse
    # keys off the contender COUNT (50 paths sharing a typo'd token are one population, not 5
    # arbitrary members), and truncating here silently hid the rest.
    contenders = tuple(s for s in scored if best.score - s.score < margin)
    return Resolution(Status.AMBIGUOUS, None, contenders if len(contenders) > _TOP_K else top)
