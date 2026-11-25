"""Classify a question by scope (self|person|team) and shape (lookup|relational|locate|miss) so `ask` can
route it. The deterministic tier (regexes, the owner roster) runs everywhere; the LLM tier only on the
interactive MCP path, and only when deterministic signals conflict. Scope reorders and labels, never
excludes, so a wrong verdict costs ordering, not recall."""
import json, re
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from analyzer import db, llm, search, tenant, tracer
from analyzer.models import SessionRow

Route = namedtuple("Route", "scope person shape missed escalated")
# scope: 'self'|'person'|'team'   person: resolved owner email (or the explicit fragment) | None
# shape: 'lookup'|'relational'|'locate'|'miss'   missed: unresolved ticket ids   escalated: LLM tier decided

_SELF = re.compile(r"\b(i|i'm|i've|i'd|me|my|mine|myself)\b", re.I)
# "how do I deploy" is team knowledge in first-person clothing: modal phrasings never self-scope
_MODAL = re.compile(r"\b(can|could|should|how|do|does|may|shall|where|am)\s+i\b", re.I)
_TEAM = re.compile(r"\b(we|our|us|ours|the team|anyone|everyone|somebody)\b", re.I)
_RELATIONAL = re.compile(r"depend|relat|connect|how many|count|who owns|\bpath\b|\bhop\b|\bmost\b|\btop\b|between",
                         re.I)  # aggregate and multi-hop questions go to the planner
# "find me the session that ran the ETL" asks for a session, not a fact. The locator word must own the
# noun: "what did we decide in the session about X" is an ordinary fact question.
_LOCATE = re.compile(r"\b(which|what|whose|find|locate|list|show)\s+(me\s+)?(?:the|a|my|our|all|any)?\s*sessions?\b",
                     re.I)
_WORD = re.compile(r"[a-z0-9]+")


# The locator's own vocabulary, beyond the topic: never evidence of a match. A document-frequency cap
# cannot find these, since a filler word is often the rarer one.
LOCATE_WORDS = {"which", "what", "whose", "where", "when", "who", "find", "locate", "list", "show", "me", "my",
                "our", "all", "any", "did", "session", "sessions", "the", "a",
                "talked", "talk", "said", "say", "mentioned", "mention", "discussed", "discuss", "covered",
                "ran", "touched", "involved", "regarding", "about", "new", "old", "remember", "recall"}

# Words that never make an ambient prompt specific. Question and discourse words are low-df in a corpus
# of terse claims, so without this they would rank as "rare" and evict the actual topic.
FILLER = {"again", "now", "right", "just", "also", "really", "maybe", "actually", "please", "then",
          "here", "there", "your", "our", "can", "will", "would", "could", "should", "some", "any",
          "all", "more", "most", "very", "only", "update", "change", "run", "fix", "add", "make",
          "get", "use", "want", "need", "code", "test", "file", "function",
          "did", "does", "what", "why", "how", "when", "who", "which", "was", "were", "are",
          "still", "already", "about", "think", "thought", "remember", "sure", "guess", "thing",
          "things", "work", "works", "working", "worked", "settle", "settled", "support",
          "supports", "anymore", "thanks", "appreciate", "help", "explain", "simply", "keep",
          "keeps", "tell", "know", "see", "look", "give", "take", "wanted", "going", "gonna",
          "let", "lets", "say", "said", "told", "idea", "ideas", "question", "happen", "happened",
          "happening"}
# Auxiliaries `query_words` keeps, which no fact says either.
_AUXILIARY = {"has", "have", "had", "been", "being"}
# How a recap asks about a stretch of time: "what was decided yesterday" names no topic.
_RECAP = {"change", "changed", "changes", "decide", "decided", "decision", "decisions", "ship", "shipped",
          "summarize", "summary", "recap", "team", "progress", "status", "learn", "learned", "discuss",
          "discussed"}
_ASKS = re.compile(r"\?|\b(what|which|who|did|recap|summarize|show|list)\b", re.I)


def topical(query):
    """The query minus the locator's own vocabulary, for retrieval on the locate path."""
    return " ".join(w for w in re.findall(r"[A-Za-z0-9_.\-]+", query or "") if w.lower() not in LOCATE_WORDS)


def is_locate(query):
    """Does this ask which session, rather than what was decided?"""
    return bool(_LOCATE.search(query or ""))


def roster(con):
    """Sorted distinct session owners (the 'Ask X' namespace)."""
    return sorted({u for (u,) in db.run(con, select(SessionRow.user).distinct()
                   .where(SessionRow.source == "session")) if u})


def aliases(con):
    """Handle or nickname -> email local-part, per workspace: a nickname is semantic, out of fuzzy_resolve's reach."""
    return tenant.config(con).person_aliases


def resolve_person(con, fragment):
    """A person fragment (email, local-part, nickname, or a typo) -> the one owner email it names, or None
    when unknown or ambiguous. Most precise first: alias, exact, fuzzy, then a unique substring."""
    if not fragment:
        return None
    owners = roster(con)
    f = aliases(con).get(fragment.strip().lower(), fragment.strip().lower())   # alias -> local-part, pre-match
    exact = [o for o in owners if o.lower() == f or o.split("@")[0].lower() == f]
    if len(exact) == 1:
        return exact[0]
    try:                                                 # fuzzy: a prefix or a one-edit typo
        from analyzer import fuzzy_resolve
        r = fuzzy_resolve.resolve(f, owners, word_tokens=True)
        if r.status is fuzzy_resolve.Status.RESOLVED:
            return r.value
    except Exception:
        pass
    sub = [o for o in owners if f in o.lower()]
    return sub[0] if len(sub) == 1 else None


def named_owner(owners, word, alias_map):
    """The one owner a lowercased query word names: the whole local part or one of its parts first, else
    a prefix of 4+ letters. None when it names no one or several."""
    w = alias_map.get(word, word)
    locals_ = [(o, o.split("@")[0].lower()) for o in owners]
    for hits in ([o for o, lp in locals_ if w in {lp, *re.split(r"[._+\-]", lp)}],
                 [o for o, lp in locals_ if len(w) >= 4 and lp.startswith(w)]):
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def named_scope(con, query):
    """(owner, topic) when the query's words name exactly one teammate, else (None, query). `topic` keeps
    the query's other words minus activity phrasing ("been working on") and filler, "" when none is left."""
    owners = roster(con)
    alias_map = aliases(con)
    named = {w: o for w in search.query_words(query) if len(w) > 2 and (o := named_owner(owners, w, alias_map))}
    if len(set(named.values())) != 1:
        return None, query
    rest = _ACTIVITY.sub(" ", query)
    keep = {w for w in search.query_words(rest) if w not in named and w not in FILLER and w not in _AUXILIARY}
    topic = " ".join(w for w in re.findall(r"[A-Za-z0-9_.\-]+", rest) if w.lower() in keep)
    return next(iter(named.values())), topic


def topic_words(con, q):
    """(unseen, known): the typed topic words of `q` the corpus has under no synonym, sorted, and how many
    it has. Not a topic word: two letters or fewer, FILLER, an auxiliary, a recap word, a teammate's name,
    or part of a date or activity phrase."""
    owners = roster(con)
    alias_map = aliases(con)
    q = _ACTIVITY.sub(" ", undated(q))
    words = {w: alts for w, alts in search.query_words(q).items()
             if len(w) > 2 and w not in FILLER | _AUXILIARY | _RECAP and not named_owner(owners, w, alias_map)}
    df = search.term_df(con, set().union(*words.values()))
    unseen = sorted(w for w, alts in words.items() if not any(df[a] for a in alts))
    return unseen, len(words) - len(unseen)


def asks(query):
    """Is the prompt a question, rather than an instruction that happens to name a day?"""
    return bool(_ASKS.search(query or ""))


def outweighed(unseen, known):
    """The honest-miss rule: never-seen topic words at least as many as the known ones, and at least one."""
    return len(unseen) >= max(known, 1)


def resolve_owner(con, name):
    """The write-side twin of resolve_person: a name a relay harness reported -> the roster identity it
    names exactly, or None. Never fuzzy and never the raw name: this becomes the permanent credit in
    `sessions.user`, and a near miss names a different teammate."""
    n = (name or "").strip()
    if not n:
        return None
    f = aliases(con).get(n.lower(), n.lower())
    exact = [o for o in roster(con) if o.lower() == f or o.split("@")[0].lower() == f]
    return exact[0] if len(exact) == 1 else None


def _owner_matches(owners, query):
    """Owners a query token matches: an exact whole token (local-part or full email, never a substring),
    else a fuzzy match per name-ish token against the roster, whose abstain guards keep 'work' or 'week'
    from matching anyone."""
    toks = set(_WORD.findall(query.lower()))
    exact = [o for o in owners if o.lower() in toks or o.split("@")[0].lower() in toks]
    if exact:
        return exact
    try:                                                 # a resolver error degrades to team scope
        from analyzer import fuzzy_resolve
        hits = []
        for t in toks:
            if len(t) < 4:                               # the resolver abstains on these anyway
                continue
            r = fuzzy_resolve.resolve(t, owners, word_tokens=True)
            if r.status is fuzzy_resolve.Status.RESOLVED and r.value not in hits:
                hits.append(r.value)
        return hits
    except Exception:
        return []


# --- "what did X work on [last week]": the who-and-when path, by recency, since the subject's facts
# contain none of the question's words.
_ACTIVITY = re.compile(r"\b(work(ed|ing)?\s+on|working|been (doing|working|up to)|up to|doing|"
                       r"done|latest|recent(ly)?|lately)\b", re.I)
_TEMPORAL = [(re.compile(r"\btoday\b", re.I), 0), (re.compile(r"\byesterday\b", re.I), 1),
             (re.compile(r"\bpast\s+(few|couple)\s+(of\s+)?days\b", re.I), 3),
             (re.compile(r"\b(this|last|past)\s+week\b", re.I), 7),
             (re.compile(r"\b(this|last|past)\s+month\b", re.I), 30)]


def since_window(query, now=None):
    """The earliest ISO date a temporal cue in the query implies ('last week' -> 7 days ago), or None.
    Computed off the calendar DATE (not the timestamp) so 'yesterday' is exactly yesterday's date."""
    today = (now or datetime.now(timezone.utc)).date()
    for rx, days in _TEMPORAL:
        if rx.search(query):
            return (today - timedelta(days=days)).isoformat()
    return None


def activity_window(query, now=None):
    """(is_activity, since_iso|None): does this read as 'what has <someone> been working on', and over
    what window. Only consulted once a person is already scoped, so it needn't detect the person."""
    since = since_window(query, now)
    return (bool(since) or bool(_ACTIVITY.search(query)), since)


# --- explicit date filtering: "on July 17", "from 2026-07-16", "last 3 days" ---------------------------
# A date the asker names is a hard [since, until) filter on last activity, unlike since_window's hint.
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# A month is the abbreviation or its full name, then a word boundary: never "decide" or "maybe".
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
          r"sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b", re.I)
_N_DAYS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s+days?\b|\b(\d{1,3})\s+days?\s+ago\b", re.I)
_SINCE = re.compile(r"\b(since|after)\b", re.I)   # not "from": "results from Jul 16" means that day
_UNTIL = re.compile(r"\b(before|until|up to|by)\b", re.I)


_DATE_PHRASES = [_ISO, _MONTH_DAY, _DAY_MONTH, _N_DAYS, _SINCE, _UNTIL, re.compile(r"\b(today|yesterday)\b", re.I),
                 *(rx for rx, _ in _TEMPORAL)]


def undated(query):
    """The query minus its date phrases ("since July 1", "today"), unchanged when it names no date."""
    if date_window(query) == (None, None):
        return query
    for rx in _DATE_PHRASES:
        query = rx.sub(" ", query)
    return query


def _iso(d):
    return d.isoformat()


def _resolve_date(y, mo, da, today):
    """(year or None, month, day) -> a date, or None. Without a year, the current one, unless that is
    more than 60 days ahead ('December 25' asked on Jan 2 means last December)."""
    from datetime import date
    if y:
        try:
            return date(y, mo, da)
        except ValueError:
            return None
    try:
        d = date(today.year, mo, da)
    except ValueError:
        return None
    if (d - today).days > 60:
        try:
            return date(today.year - 1, mo, da)
        except ValueError:
            return None
    return d


def date_window(query, now=None):
    """(since_iso|None, until_iso|None): the explicit date range the query names, half-open [since, until).
    None,None when the query names no date. Handles ISO dates, 'Month Day'/'Day Month', 'last N days',
    'N days ago', today/yesterday/this-week/this-month, since/after/from (open end) and before/until (open
    start), and ranges (two dates, or between/from-to). Compared against a session's last-activity date."""
    if not query:
        return (None, None)
    q = query
    today = (now or datetime.now(timezone.utc)).date()
    tomorrow = today + timedelta(days=1)

    # collect explicit calendar dates in the query, in order of appearance
    dated = []
    for m in _ISO.finditer(q):
        d = _resolve_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
        if d:
            dated.append((m.start(), d))
    for m in _MONTH_DAY.finditer(q):
        d = _resolve_date(None, _MONTHS[m.group(1).lower()[:3]], int(m.group(2)), today)
        if d:
            dated.append((m.start(), d))
    for m in _DAY_MONTH.finditer(q):
        d = _resolve_date(None, _MONTHS[m.group(2).lower()[:3]], int(m.group(1)), today)
        if d:
            dated.append((m.start(), d))
    dated.sort()
    dates = [d for _, d in dated]

    if len(dates) >= 2:                                  # a range: earliest..latest, inclusive
        lo, hi = min(dates), max(dates)
        return (_iso(lo), _iso(hi + timedelta(days=1)))
    if len(dates) == 1:
        d = dates[0]
        if _SINCE.search(q):                             # "since July 1" -> open-ended forward
            return (_iso(d), None)
        if _UNTIL.search(q):                             # "before July 10" -> open-ended backward
            return (None, _iso(d))
        return (_iso(d), _iso(d + timedelta(days=1)))    # a single named day

    m = _N_DAYS.search(q)                                # "last 3 days" / "3 days ago"
    if m:
        n = int(m.group(1) or m.group(2))
        return (_iso(today - timedelta(days=n)), _iso(tomorrow))
    if re.search(r"\btoday\b", q, re.I):
        return (_iso(today), _iso(tomorrow))
    if re.search(r"\byesterday\b", q, re.I):
        return (_iso(today - timedelta(days=1)), _iso(today))
    rel = since_window(q, now)                           # this|last|past week/month, past few days
    if rel:
        return (rel, _iso(tomorrow))
    return (None, None)


_CLASSIFY_PROMPT = """Classify a question asked to a team-memory tool.
Asker: {asked_by}. Known session owners: {owners}.
scope=self: asks about the ASKER's own past work. scope=person: about ONE named owner's work (set person
to their email from the list). scope=team: everything else — DEFAULT WHENEVER UNSURE.
Return ONLY JSON: {{"scope":"self"|"person"|"team","person":"<owner email or empty>"}}
Question: {q}"""


def _llm_classify(query, asked_by, owners, model="small"):
    text, _, _ = llm.complete(_CLASSIFY_PROMPT.format(asked_by=asked_by or "?", owners=", ".join(owners), q=query),
                              model, timeout=20)
    m = re.search(r"\{.*\}", text, re.S)
    v = json.loads(m.group(0))
    if v.get("scope") not in ("self", "person", "team") or (v.get("person") and v["person"] not in owners):
        raise ValueError(f"bad verdict {v}")
    return v


def classify(con, query, asked_by=None, person=None, eids=None, allow_llm=False, llm=None):
    """One Route per ask. `eids` is resolve.resolve()'s output (None on the hook path: no miss shape).
    Ambiguity without `allow_llm`, or any LLM failure, resolves to team. `llm` is injectable for tests."""
    with tracer.span("route", "CHAIN") as sp:
        rt = _classify(con, query, asked_by, person, eids, allow_llm, llm)
        sp.set(**{"csa.route.scope": rt.scope, "csa.route.shape": rt.shape, "csa.route.person": rt.person,
                  "csa.route.escalated": rt.escalated, "csa.route.missed_ids": rt.missed or None})
        return rt


def _classify(con, query, asked_by, person, eids, allow_llm, llm):
    from analyzer import resolve as _resolve             # late: route<->resolve would cycle at import time
    q = query or ""
    if _LOCATE.search(q):                                # before relational: "which sessions depend on X"
        shape, missed = "locate", []
    elif _RELATIONAL.search(q):
        shape, missed = "relational", []
    else:
        # ticket-shaped ids no resolved entity covers: cards() renders the honest miss
        tickets = list(_resolve._TICKET_REF.finditer(q)) if eids is not None else []
        missed = [m.group(0) for m in tickets if not any(
            m.group(2).lower() in _WORD.findall(e.lower()) for e in eids or [])]
        shape = "miss" if missed else "lookup"

    if person:                                           # an explicit person always wins
        return Route("person", person, shape, missed, False)
    owners = roster(con)
    named = [o for o in _owner_matches(owners, q) if o != (asked_by or "")]
    self_named = asked_by and asked_by in _owner_matches(owners, q)
    is_self = bool(_SELF.search(q)) and not _MODAL.search(q) and not _TEAM.search(q)

    # the only ambiguous cases: first person plus a named teammate, or a name matching several owners
    if (is_self and named) or len(named) > 1:
        if allow_llm:
            try:
                v = (llm or _llm_classify)(q, asked_by, owners)
                return Route(v["scope"], v.get("person") or (asked_by if v["scope"] == "self" else None),
                             shape, missed, True)
            except Exception:
                pass                                     # LLM outage/garbage degrades to the conservative tier
        return Route("team", None, shape, missed, False)
    if named:
        return Route("person", named[0], shape, missed, False)
    if (is_self or self_named) and asked_by:             # naming yourself == first person
        return Route("self", asked_by, shape, missed, False)
    return Route("team", None, shape, missed, False)
