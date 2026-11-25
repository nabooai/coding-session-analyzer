"""Load extracted JSON into SQLite (structured + FTS). HITL merges + reviews."""
import sqlite3, os, json, re, collections, hashlib, logging, time, functools, difflib, threading
from pathlib import Path

from sqlalchemy import bindparam, delete, func, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from analyzer import db
from analyzer import migrations
from analyzer import models
from analyzer import harness
from analyzer import entity_types
from analyzer import entity_gate
from analyzer import tenant
from analyzer import tracer

DOCS_TOKENIZER = "porter unicode61"
# The FTS columns, defined once: the base schema creates `docs` (so a fresh tenant has one, empty) and
# build_fts recreates it to reapply the tokenizer.
DOCS_COLS = ("doc_id UNINDEXED, kind UNINDEXED, session_id UNINDEXED, project UNINDEXED, started_at UNINDEXED, "
             "etype UNINDEXED, source UNINDEXED, status UNINDEXED, corroboration UNINDEXED, "
             "text, "
             # New columns go AFTER `text`: snippet(docs,9,...) callers are positional. A fact's
             # first_at/started_at are its first/last mention; invalid_at IS NULL means current.
             # Other kinds: first_at = started_at, validity columns NULL.
             "first_at UNINDEXED, valid_at UNINDEXED, invalid_at UNINDEXED, state UNINDEXED, "
             f"tokenize='{DOCS_TOKENIZER}'")

TELEMETRY_DB = "telemetry.db"


def data_root():
    """This box's data directory as an absolute Path, resolved per call: callers chdir into a box
    directory and expect `data/` to follow them."""
    return Path.cwd() / "data"


def telemetry_path(corpus_path):
    """The telemetry file for a corpus file: its sibling, so one per org directory. A str, since it is
    bound as a SQL parameter. tracer._telemetry_path repeats this rule (importing it would cycle)."""
    if corpus_path == ":memory:":
        return ":memory:"
    return os.path.join(os.path.dirname(os.path.abspath(corpus_path)), TELEMETRY_DB)


# --- org isolation ------------------------------------------------------------
# The tenant boundary is the FILE: one SQLite db per org, never a `WHERE org_id=?`. ask.py runs
# model-written SQL, and `ref` is a content hash that collides across companies, so no predicate could
# hold the line. See docs/public-release/org-admin.md.
DEFAULT_ORG = "default"
_ORG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")     # \Z not $: `$` matches before a trailing "\n"


def valid_org_id(org_id):
    """Normalize and validate an org id, which becomes a filesystem path. A bad id raises; it never
    degrades to the default org."""
    if org_id is None or org_id == DEFAULT_ORG:
        return DEFAULT_ORG
    o = str(org_id)
    if not _ORG_RE.match(o):                             # blocks '..', '/', absolute paths, unicode, trailing \n
        raise ValueError(f"invalid org id {org_id!r}")
    return o


def valid_session_id(sid):
    """A session id is also a filename (the queue, the extraction cache), so a '../' would climb out of
    the org's directory. Rejects only path-dangerous shapes, and raises rather than coerce."""
    s = str(sid or "")
    if (not s or len(s) > 128 or s in (".", "..") or s.startswith(".")
            or s != os.path.basename(s) or "\\" in s or any(ord(c) < 32 for c in s)):
        raise ValueError(f"invalid session id {sid!r}")
    return s


def org_dir(org_id=None):
    """Per-org data root (db, extracted/, pending/), absolute. The default org is `data/` itself."""
    o = valid_org_id(org_id)
    return data_root() if o == DEFAULT_ORG else data_root() / "orgs" / o


def _no_attach(action, _a1, _a2, _db, _src):
    """SQLite authorizer: deny ATTACH/DETACH, allow everything else."""
    return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK


def ro_connect(path):
    """Read-only connection for model-authored SQL (analyzer.ask): the isolation-critical seam.
    `mode=ro` does not stop `ATTACH DATABASE '<another org's file>'`, so the authorizer refuses it.
    This org's `tel` is attached first, from this path, never from model input."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=15000")
    tel = telemetry_path(path)
    have_tel = os.path.exists(tel)               # checked once, so the schema step below agrees with the attach
    con.execute("ATTACH DATABASE ? AS tel",      # missing file -> an empty :memory: stand-in, so a query
                (f"file:{tel}?mode=ro" if have_tel else ":memory:",))   # naming tel.* still parses
    if not have_tel:
        migrations.ensure(con, "telemetry")       # the stand-in needs the tables to exist (empty)
    con.set_authorizer(_no_attach)
    return con


def org_db(org_id=None):
    """Absolute path to one org's corpus. sqlite_db() creates the parent, so a new org needs no
    provisioning step."""
    return org_dir(org_id) / "analyzer.db"


_PERSESSION = ("worked_on", "hidden_context", "mentions", "actions", "relations", "links", "data_sources")
_HARVEST_MARKERS = ("harvest_actions", "harvest_mentions", "harvest_links")
ENTITY_TYPES = set(entity_types.DEFAULTS)   # defaults only; a tenant's full set = this | entity_types.custom(con)

# _PERSESSION/_HARVEST_MARKERS are name tuples apiserver iterates; _TBL maps a name to its Core Table.
_ALIASES, _ENTITIES = models.Alias.__table__, models.Entity.__table__
_REVIEWS, _SIGNALS = models.Review.__table__, models.Signal.__table__
_HC_TYPES, _ENTITY_PINS = models.HcType.__table__, models.EntityTypePin.__table__
_CLUSTERS, _STATS = models.ClaimCluster.__table__, models.EntityStats.__table__
_SESSIONS, _SMETRICS = models.SessionRow.__table__, models.SessionMetrics.__table__
_TBL = {t.name: t for t in (
    models.worked_on, models.hidden_context, models.mentions, models.actions, models.relations,
    models.links, models.data_sources, models.HarvestAction.__table__,
    models.HarvestMention.__table__, models.HarvestLink.__table__)}


_SCHEMA_LOCKS = {}                        # one lock per db path
_SCHEMA_LOCKS_GUARD = threading.Lock()


def _schema_lock(key):
    """In-process single-flight for connect plus cold-open DDL: two threads opening one file must not
    interleave the WAL switch and a schema upgrade. Across processes it does nothing; busy_timeout
    covers that."""
    with _SCHEMA_LOCKS_GUARD:
        return _SCHEMA_LOCKS.setdefault(key, threading.Lock())


def sqlite_db(path=None):
    path = org_db() if path is None else path            # resolved here: a default arg would bind one cwd
    if path != ":memory:" and os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with _schema_lock(path if path == ":memory:" else os.path.abspath(path)):
        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA busy_timeout=15000")   # wait for the lock, don't 500 "database is locked"
            if path != ":memory:":
                _wal(con)
            _cold_open(con, path)
            _attach_telemetry(con, path)             # AFTER the corpus schema: the migration reads main.*
        except BaseException:
            con.close()                              # a failed open must not leak the handle: the ETL
            raise                                    # retries this org every tick
        return con


def _wal(con, schema="main"):
    """Switch a file to WAL, retrying: the mode switch takes an exclusive lock without honoring
    busy_timeout, so processes opening a new file at once collide. A stubborn file is left for the
    next open rather than raising."""
    for _ in range(50):
        try:
            if con.execute(f"PRAGMA {schema}.journal_mode=WAL").fetchone()[0] == "wal":
                return True
        except sqlite3.OperationalError:
            pass
        time.sleep(0.05)
    logging.warning("%s is not in WAL mode: readers will block its writer", schema)
    return False


def _attach_telemetry(con, path):
    """Attach this corpus's telemetry file as `tel` and make sure its schema exists."""
    tel = telemetry_path(path)
    con.execute("ATTACH DATABASE ? AS tel", (tel,))
    if tel != ":memory:":
        _wal(con, "tel")
        con.execute("PRAGMA tel.synchronous=NORMAL")  # replaceable data, written per prompt: no fsync per row
    migrations.ensure(con, "telemetry", None if tel == ":memory:" else tel)
    _migrate_telemetry(con, tel)


_TEL_MIGRATED = set()                                 # in-process memo; the flock below is the real guard


def _migrate_telemetry(con, tel):
    """One-time move of the pre-split in-corpus `served`/`queries`/`spans` rows into the telemetry file:
    copy, then drop the corpus copy (a frozen table under the live name answers wrongly). Not an
    Alembic revision, which runs before this copy could read the rows. Guarded by an flock, since
    BEGIN IMMEDIATE on this connection would take the corpus writer too."""
    if tel == ":memory:" or tel in _TEL_MIGRATED:
        return
    import fcntl
    with open(tel + ".migrate.lock", "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        for t in ("served", "queries", "spans"):
            legacy = con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not legacy:
                continue                             # fresh install
            # a COUNT, not "tel is non-empty": tracer.py may write a live span before the first open
            if con.execute(f"SELECT COUNT(*) FROM tel.{t}").fetchone()[0] >= \
               con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]:
                continue                             # someone already moved them
            have = {r[1] for r in con.execute(f"PRAGMA main.table_info({t})")}
            cols = [r[1] for r in con.execute(f"PRAGMA tel.table_info({t})") if r[1] in have]
            n = con.execute(f"INSERT INTO tel.{t}({','.join(cols)}) "
                            f"SELECT {','.join(cols)} FROM main.{t}").rowcount
            con.commit()
            if n:
                logging.info("telemetry: moved %d legacy %s row(s) into %s", n, t, tel)
        # a separate pass: a table copied by an earlier boot still needs dropping
        for t in ("served", "queries", "spans"):
            if not con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
                               (t,)).fetchone():
                continue
            if con.execute(f"SELECT COUNT(*) FROM tel.{t}").fetchone()[0] < \
               con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]:
                logging.warning("telemetry: keeping legacy main.%s, tel has fewer rows", t)
                continue                             # never drop what was not fully copied
            con.execute(f"DROP TABLE main.{t}")
            con.commit()
            logging.info("telemetry: dropped the copied legacy %s table from the corpus", t)
    _TEL_MIGRATED.add(tel)


def _cold_open(con, path):
    """Bring the corpus to head, then rebuild a `docs` table older than schema v2 (fts5 cannot ALTER,
    and its readers would go silently empty). Runs under _schema_lock."""
    migrations.ensure(con, "corpus", None if path == ":memory:" else os.path.abspath(path))
    con.commit()
    try:
        con.execute("SELECT first_at FROM docs LIMIT 0")
    except sqlite3.OperationalError:
        try:
            refresh_facts(con)
            build_fts(con)
        except sqlite3.OperationalError:
            con.rollback()                           # another process won the race; the next open probes again
    return con


def hc_ref(claim):
    return hashlib.sha1((claim or "").strip().lower().encode()).hexdigest()[:12]


CARRY_THRESHOLD = 0.7          # dedup.overlap gate: which pairs are even eligible
CARRY_MARGIN = 0.05            # Jaccard gap the winner must beat the runner-up by; inside it, carry nothing


def carry_refs(old, claims):
    """[ref] for `claims`, reusing this session's existing refs for rewordings, so a re-extract does not
    orphan the labels and usage keyed on the old ref.

    old: [(ref, claim)] from the same session only, and only refs no other session holds. An exact
    restatement keeps its hash. Otherwise a pair is eligible at dedup.overlap >= CARRY_THRESHOLD and
    scored by Jaccard; a claim carries a ref only when each is the other's best by CARRY_MARGIN, so the
    result ignores emission order and ambiguity mints a fresh ref (a wrong carry would move a human
    label onto a different fact)."""
    from analyzer import dedup
    refs = [hc_ref(c) for c in claims]
    old_refs = {r for r, _ in old}
    free = {r: dedup.tokens(c) for r, c in old if r not in refs}      # exact restatements already hold their ref
    new = {i: dedup.tokens(claims[i]) for i, r in enumerate(refs) if r not in old_refs}
    sim = {(i, r): len(nt & ot) / len(nt | ot)                        # eligibility: overlap; score: Jaccard
           for i, nt in new.items() for r, ot in free.items()
           if len(nt & ot) >= dedup.MIN_SHARED and dedup.overlap(nt, ot) >= CARRY_THRESHOLD}

    def clear_best(scores):
        """The one unambiguous winner, or None when the runner-up is within CARRY_MARGIN."""
        ranked = sorted(scores.values(), reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0] - ranked[1] < CARRY_MARGIN):
            return None
        return max(scores, key=scores.get)

    for i in new:
        r = clear_best({r: s for (n, r), s in sim.items() if n == i})
        if r is not None and clear_best({n: s for (n, o), s in sim.items() if o == r}) == i:
            refs[i] = r                       # each is the other's clear best -> the same fact, reworded
    return refs


_AS_OF_RE = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?Z?)?\Z")   # \Z: the whole value


def valid_as_of(v):
    """The extractor's `as_of` as an ISO date (optionally with a time), or None for anything else,
    impossible dates and times included: it feeds facts.valid_at, which is compared as text."""
    s = str(v).strip() if v is not None else ""
    if not _AS_OF_RE.match(s):
        return None
    try:
        time.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None
    if len(s) > 10 and not (int(s[11:13]) < 24 and int(s[14:16]) < 60
                            and (len(s) < 19 or s[16] != ":" or int(s[17:19]) < 60)):
        return None
    return s[:25]


def refresh_facts(con, refs=None, recompute=False):
    """(Re)derive the `facts` rows from the mentions (hidden_context joined to sessions), for every ref
    or only `refs`. No commit: it runs inside the caller's transaction.

    Per ref: author is the user of the first asserting session (by effective date, NULLs last, then
    session_id); created_at the earliest effective date; valid_at the earliest valid `as_of`, else it
    tracks created_at while equal to it; domain the first code-home repo, else the first project;
    claim/type/scope the latest mention's. created_at only moves earlier, except with `recompute`
    (forget), which re-derives everything from the surviving mentions. invalid_at, state and
    confidence belong to other writers and are never touched. A ref with no mentions left loses its
    row and its edges.

    CARVE-OUT (tests/test_no_raw_sql.py): the upsert's CASE ladder reads the pre-update row against
    `excluded.*`, and is kept raw."""
    scope, params = "", []
    if refs is not None:
        refs = {r for r in refs if r}
        if not refs:
            return 0
        scope = f" AND h.ref IN ({','.join('?' * len(refs))})"
        params = list(refs)
    rows = con.execute(
        "SELECT h.ref, h.claim, h.type, h.scope, h.repo, h.as_of, h.session_id, s.user, s.project, "
        "IFNULL(s.ended_at, s.started_at) FROM hidden_context h JOIN sessions s ON s.session_id=h.session_id "
        "WHERE h.ref IS NOT NULL" + scope, params).fetchall()
    per = {}
    for ref, claim, ty, scp, repo, as_of, sid, user, project, eff in rows:
        per.setdefault(ref, []).append((eff, sid, user, project, repo, as_of, claim, ty, scp))
    for ref, ms in per.items():
        order = sorted(ms, key=lambda m: (m[0] is None, m[0] or "", m[1]))
        first, last = order[0], order[-1]
        created = min((m[0] for m in ms if m[0] is not None), default=None)
        as_ofs = sorted(a for m in ms for a in (valid_as_of(m[5]),) if a)
        valid = as_ofs[0] if as_ofs else created
        domain = next((m[4] for m in order if m[4]), None) or first[3]
        force, has_as_of = int(bool(recompute)), int(bool(as_ofs))
        con.execute(
            "INSERT INTO facts(ref,claim,type,scope,author,domain,created_at,valid_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ref) DO UPDATE SET "
            "claim=excluded.claim, type=excluded.type, scope=excluded.scope, domain=excluded.domain, "
            # every right-hand side sees the OLD row; `IS`, not `=`, is NULL-safe
            "author=CASE WHEN ? OR (excluded.created_at IS NOT NULL AND (facts.created_at IS NULL "
            "  OR excluded.created_at < facts.created_at)) THEN excluded.author ELSE facts.author END, "
            # as_of evidence or a forget recompute re-derives valid_at outright
            "valid_at=CASE WHEN ? OR ? THEN excluded.valid_at "
            "  WHEN facts.valid_at IS facts.created_at THEN "
            "    CASE WHEN excluded.created_at IS NOT NULL AND (facts.created_at IS NULL "
            "           OR excluded.created_at < facts.created_at) THEN excluded.created_at "
            "         ELSE facts.valid_at END "
            "  ELSE facts.valid_at END, "
            "created_at=CASE WHEN ? OR (excluded.created_at IS NOT NULL AND (facts.created_at IS NULL "
            "  OR excluded.created_at < facts.created_at)) THEN excluded.created_at ELSE facts.created_at END",
            (ref, last[6], last[7], last[8], first[2], domain, created, valid,
             force, has_as_of, force, force))
    if refs is None:
        gone = [r for (r,) in con.execute(
            "SELECT ref FROM facts WHERE NOT EXISTS(SELECT 1 FROM hidden_context h WHERE h.ref=facts.ref)")]
    else:
        gone = sorted(refs - set(per))
    for ref in gone:
        con.execute("DELETE FROM facts WHERE ref=?", (ref,))
        con.execute("DELETE FROM fact_edges WHERE src_ref=? OR dst_ref=?", (ref, ref))
    return len(per)


_HOME_PREFIX = re.compile(r"^(?:[A-Za-z]:/)?(?:Users|home)/[^/]+/", re.I)
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")       # a windows client's transcript has backslashed paths
# A home directory is not a repo: its basename is a username.
_HOME_DIR = re.compile(r"^(?:[A-Za-z]:/)?(?:Users|home)/[^/]+$|^root$", re.I)


def _clean_code_home(v):
    """Trust boundary on extractor (LLM) output: strip, cap, blank -> None (never "")."""
    s = (str(v).strip() if v is not None else "").strip("/")
    return s if s and len(s) <= 200 else None            # absurd length -> the model is rambling, not naming a path


def code_home(h):
    """(repo, path) for one extracted hidden_context item: repo as a basename, path relative to the repo,
    so no `/Users/<someone>/` reaches a team-served provenance line."""
    repo, path = _clean_code_home(h.get("repo")), _clean_code_home(h.get("path"))
    if repo:
        if _WIN_ABS.match(repo):
            repo = repo.replace("\\", "/")
        repo = None if _HOME_DIR.match(repo.strip("/")) else repo.rsplit("/", 1)[-1]   # /Users/x/…/ledger -> ledger
    if path:
        if _WIN_ABS.match(path):                         # C:\Users\n\Git\ledger\a.py — else the username survives
            path = path.replace("\\", "/").strip("/")
        path = _HOME_PREFIX.sub("", path)                # Users/x/Documents/Git/ledger/a/b.py -> Documents/Git/ledger/a/b.py
        if repo and f"/{repo}/" in f"/{path}":
            path = f"/{path}".split(f"/{repo}/", 1)[1]   # -> a/b.py (repo-relative, the whole point)
        path = path.strip("/") or None
    return repo, path


# --- entity resolution (HITL merges live in `aliases`) ----------------------
def canon(con, eid):
    """Follow alias chain to the canonical id (cycle-safe)."""
    seen, cur = set(), eid
    while cur and cur not in seen:
        seen.add(cur)
        row = db.run(con, select(_ALIASES.c.canonical_id)
                     .where(_ALIASES.c.alias_id == cur)).fetchone()
        if not row:
            return cur
        cur = row[0]
    return cur


def canonicalize(con, data):
    """Resolve every entity id in `data` through the alias table before loading either store."""
    d = dict(data)
    d["entities"] = [{**e, "id": canon(con, (e.get("id") or e.get("name") or "").strip())}
                     for e in data.get("entities", [])]
    d["actions"] = [{**a, "target_id": canon(con, (a.get("target_id") or "").strip())}
                    for a in data.get("actions", [])]
    d["relations"] = [{**r, "source_id": canon(con, (r.get("source_id") or "").strip()),
                       "target_id": canon(con, (r.get("target_id") or "").strip())}
                      for r in data.get("relations", [])]
    return d


def merge_entity(con, from_id, into_id):
    """HITL: fold from_id into into_id. Repoints SQLite rows; graph updates on next `index`."""
    db.run(con, insert(_ALIASES).prefix_with("OR REPLACE")
           .values(alias_id=from_id, canonical_id=into_id))
    for tbl, col in [(models.mentions, "entity_id"), (models.actions, "target_id"),
                     (models.relations, "source_id"), (models.relations, "target_id")]:
        db.run(con, update(tbl).values({col: into_id}).where(tbl.c[col] == from_id))
    db.run(con, delete(_ENTITIES).where(_ENTITIES.c.id == from_id))
    con.commit()


# --- fuzzy duplicate detection (stdlib edit distance, no model) ---------------
# A typo match clears both gates: an absolute edit cap (long paths sharing a prefix differ by more
# than two chars) and a similarity ratio (short ids like GOAL/SOUL are two edits apart by accident).
MAX_EDITS, MIN_RATIO = 2, 0.80


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _lev(a, b, cap):
    """Levenshtein distance, or cap+1 as soon as it provably exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur, rowmin = [i], i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            rowmin = min(rowmin, v)
        if rowmin > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _numbered_siblings(a, b):
    """True if two normalized ids are identical once digits are stripped (FLOOR1/FLOOR5,
    ENG-1794/ENG-1797, summary_harness_000/001) — enumerated siblings are DISTINCT, never dups."""
    return a != b and re.sub(r"\d+", "", a) == re.sub(r"\d+", "", b)


def _fuzzy_dup(a, b):
    """The typo predicate over two normalized ids (the caller enforces the same type)."""
    if a == b or _numbered_siblings(a, b) or abs(len(a) - len(b)) > MAX_EDITS:
        return False
    return _lev(a, b, MAX_EDITS) <= MAX_EDITS and difflib.SequenceMatcher(None, a, b).ratio() >= MIN_RATIO


def _find(parent, x):
    parent.setdefault(x, x)
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:                        # path-compress
        parent[x], x = root, parent[x]
    return root


def suggest_merges(con):
    """Candidate duplicate-entity groups for human review, [[(id, type, name), ...], ...]; never mutates.
    Two entities are candidates when they share a type and their normalized ids are equal or a fuzzy
    typo match. Grouping is transitive. O(n^2) within each type."""
    by_type = collections.defaultdict(list)
    for id_, ty, nm in db.run(con, select(_ENTITIES.c.id, _ENTITIES.c.type, _ENTITIES.c.name)):
        nrm = _norm(id_)
        if nrm:                                      # ids that normalize to '' are un-mergeable (skip)
            by_type[ty].append((id_, ty, nm, nrm))
    parent = {}
    for members in by_type.values():
        reps = {}                                    # normalized id -> its representative entity id
        for id_, _ty, _nm, nrm in members:
            _find(parent, id_)
            if nrm in reps:
                parent[_find(parent, id_)] = _find(parent, reps[nrm])   # case/sep variant -> same group
            else:
                reps[nrm] = id_
        keys = list(reps.items())                    # [(normalized, representative_id)]
        for i in range(len(keys)):                   # fuzzy-link distinct normalized reps within the type
            ni, ri = keys[i]
            for j in range(i + 1, len(keys)):
                nj, rj = keys[j]
                if _fuzzy_dup(ni, nj):
                    parent[_find(parent, ri)] = _find(parent, rj)
    groups = collections.defaultdict(list)
    for members in by_type.values():
        for id_, ty, nm, _nrm in members:
            groups[_find(parent, id_)].append((id_, ty, nm))
    return [g for g in groups.values() if len(g) > 1]


# --- HITL value review (decisions survive re-index, like aliases) ------------
def set_review(con, kind, ref, status, note=None, flagger=None):
    """Label one fact or entity, stamped with `reviewed_at` so a label can be ordered against a serve."""
    db.run(con, insert(_REVIEWS).prefix_with("OR REPLACE")
           .values(kind=kind, ref=ref, status=status, note=note, flagger=flagger,
                   reviewed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    con.commit()


def clear_review(con, kind, ref):
    """flag 'clear': delete the review row. Clearing a judge label leaves a human_cleared signal, so the
    next sweep does not re-apply it."""
    row = db.run(con, select(_REVIEWS.c.flagger)
                 .where(_REVIEWS.c.kind == kind, _REVIEWS.c.ref == ref)).fetchone()
    db.run(con, delete(_REVIEWS).where(_REVIEWS.c.kind == kind, _REVIEWS.c.ref == ref))
    if row and row[0] == "judge":
        db.run(con, insert(_SIGNALS).prefix_with("OR IGNORE")
               .values(ref=ref, kind="human_cleared", session_id="human",
                       at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    con.commit()


def record_cost(con, step, model, session_id, usage, cost_usd):
    """One row per LLM call: the spend ledger behind the dashboard's cost line. usage = claude -p envelope;
    its `model`, the id the provider served, is recorded over the role the caller asked for."""
    u = usage or {}
    db.run(con, insert(models.costs).values(
        at=func.datetime("now"),
        step=step, model=u.get("model") or model, session_id=session_id,
        input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
        cache_read_tokens=u.get("cache_read_input_tokens"), cost_usd=cost_usd,
        trace_id=tracer.current_trace_id()))
    con.commit()


def set_hc_type(con, ref, hc_type):
    """HITL: pin a hidden_context fact's type (human-confirmed from the Questions queue). Survives re-index
    (hc_types is applied in load_sqlite) AND updates the currently-loaded rows so it takes effect immediately."""
    db.run(con, insert(_HC_TYPES).prefix_with("OR REPLACE").values(ref=ref, type=hc_type))
    db.run(con, update(models.hidden_context).values(type=hc_type)
           .where(models.hidden_context.c.ref == ref))
    db.run(con, update(models.Fact.__table__).values(type=hc_type)
           .where(models.Fact.__table__.c.ref == ref))   # the durable fact row tracks the retype
    con.commit()


def log_served(con, session, question, items, person=None, asked_by=None):
    """Record which facts recall served for a (session, question). `items` = [(ref, kind, project)];
    `person` is the "Ask X" scope, if any; `asked_by` the caller's identity."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tid = tracer.current_trace_id()
    db.run_many(con, insert(models.served).values(
        session=bindparam("session"), asked_at=bindparam("asked_at"), question=bindparam("question"),
        ref=bindparam("ref"), kind=bindparam("kind"), project=bindparam("project"),
        person=bindparam("person"), asked_by=bindparam("asked_by"), trace_id=bindparam("trace_id")),
        [{"session": session, "asked_at": ts, "question": question, "ref": ref, "kind": kind,
          "project": project, "person": person, "asked_by": asked_by, "trace_id": tid}
         for ref, kind, project in items])
    con.commit()


def log_query(con, asked_by, session, engine, query_text, planned_query, row_count, latency_ms,
              acted_on=0, result_ids=None):
    """One `queries` row per ask. `planned_query` is the SQL or terms emitted; `result_ids` what the query
    reached; `acted_on` is set later by signals._mark_queries."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.run(con, insert(models.queries).values(
        at=ts, asked_by=asked_by or "", session=session, engine=engine, query_text=query_text,
        planned_query=planned_query, row_count=row_count, latency_ms=latency_ms, acted_on=acted_on,
        result_ids=result_ids, trace_id=tracer.current_trace_id()))
    con.commit()


def queries_readout(con):
    """Per-engine ask counts and the empty-vs-result-bearing split."""
    q = models.queries
    total = db.run(con, select(func.count()).select_from(q)).fetchone()[0]
    n = func.count().label("n")                          # ORDER BY n DESC: dict order IS the readout order
    by_engine = dict(db.run(con, select(q.c.engine, n).group_by(q.c.engine).order_by(n.desc())))
    empty = db.run(con, select(func.count()).select_from(q)
                   .where(or_(q.c.row_count.is_(None), q.c.row_count == 0))).fetchone()[0]
    acted = db.run(con, select(func.count()).select_from(q).where(q.c.acted_on == 1)).fetchone()[0]
    return {"total": total, "by_engine": by_engine, "empty": empty, "result_bearing": total - empty,
            "empty_ratio": round(empty / total, 3) if total else 0.0, "acted_on": acted}


def set_type(con, eid, ty):
    """HITL: pin an entity's type, overriding the majority vote. Survives re-extract (own table)."""
    db.run(con, insert(_ENTITY_PINS).prefix_with("OR REPLACE").values(id=eid, type=ty))
    con.commit()


def screen(con):
    """Label single-session file/env entities 'local' (note 'auto'), never over a human's label."""
    n = 0
    e, mm = _ENTITIES.alias("e"), models.mentions.alias("m")
    for eid, ty, sess in db.run(con, select(                      # eager fetchall, on purpose:
            e.c.id, e.c.type, func.count(mm.c.session_id.distinct()).label("sess"))
            .select_from(e.join(mm, mm.c.entity_id == e.c.id))    # set_review below commits mid-iteration
            .group_by(e.c.id)).fetchall():
        if ty not in ("file", "env") or (sess or 0) > 1:
            continue
        cur = db.run(con, select(_REVIEWS.c.note)
                     .where(_REVIEWS.c.kind == "entity", _REVIEWS.c.ref == eid)).fetchone()
        if cur and cur[0] != "auto":         # never override a human decision
            continue
        set_review(con, "entity", eid, "local", note="auto")
        n += 1
    return n


# --- load (assumes data already canonicalized) ------------------------------
def relay_accounts(con=None):
    """Accounts whose sessions are relays (a bot shipping other people's work), from the workspace's
    settings. The gate is the verified shipping account, never the client-authored transcript."""
    return set((tenant.config(con) if con is not None else tenant.defaults()).relay_accounts)


def session_owner(con, sess):
    """-> (user, shipped_by) for one session. `user` is whose work it is; `shipped_by` is the credential that
    POSTed it, or None when they are the same.

    A service token's explicit `on_behalf_of` credits that person. A declared relay account's session
    is credited to the asker its transcript names, when the roster knows them; otherwise it stays on
    the shipping account."""
    shipper = sess.get("user") or owner_identity()["email"]
    on_behalf = (sess.get("on_behalf_of") or "").strip().lower()
    if on_behalf and on_behalf != (shipper or "").lower():
        return on_behalf, shipper
    if (shipper or "").lower() not in relay_accounts(con):
        return shipper, None
    from analyzer import route                                # local import: route imports llm, store is deeper
    who = route.resolve_owner(con, harness.pi_requester(sess.get("transcript")))
    return (who, shipper) if who and who != shipper else (shipper, None)


_METRIC_COLS = ("model", "tool_calls", "tool_errors", "dup_calls", "reread_calls", "top_read", "top_read_n",
                "top_tool", "in_tokens", "out_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd",
                "wall_s", "idle_s", "max_gap_s")


def load_sqlite(con, sess, data):
    sid = sess["session_id"]
    user, shipped_by = session_owner(con, sess)
    sess["user"], sess["shipped_by"] = user, shipped_by   # downstream readers agree with the row written here

    db.run(con, insert(_SESSIONS).prefix_with("OR REPLACE").values(
        session_id=sid, project=sess["project"], git_branch=sess["git_branch"],
        started_at=sess["started_at"], ended_at=sess["ended_at"],
        n_user=sess["n_user"], n_asst=sess["n_asst"], summary=data.get("summary"),
        deployed_to_prod=int(bool(data.get("deployed_to_prod"))),
        source=sess.get("source", "session"),
        user=user,                                       # remote client sends its owner; local = this machine
        harness=sess.get("harness", "claude-code"),
        redaction_mode=sess.get("redaction_mode", "secrets"),   # effective mode it was redacted at
        shipped_by=shipped_by))
    # carry_refs' input, read BEFORE the DELETE below and atomic with it only because the INSERT above
    # already took the write lock. Refs another session holds are excluded, keeping one claim text per
    # ref. The subquery's `ref IS NOT NULL` matters: NOT IN over a NULL yields no rows at all.
    hc, h2 = models.hidden_context, models.hidden_context.alias("h2")
    old_hc = db.run(con, select(hc.c.ref, hc.c.claim)
                    .where(hc.c.session_id == sid, hc.c.ref.isnot(None),
                           hc.c.ref.notin_(select(h2.c.ref)
                                           .where(h2.c.session_id != sid,
                                                  h2.c.ref.isnot(None))))).fetchall()
    # all of this session's refs, shared ones too: the refresh_facts scope
    old_refs = {r for (r,) in db.run(con, select(hc.c.ref)
                                     .where(hc.c.session_id == sid, hc.c.ref.isnot(None)))}
    for tbl in _PERSESSION:
        db.run(con, delete(_TBL[tbl]).where(_TBL[tbl].c.session_id == sid))   # idempotent reload
    for tbl in _HARVEST_MARKERS:
        db.run(con, delete(_TBL[tbl]).where(_TBL[tbl].c.session_id == sid))
    m = sess.get("metrics")
    if m:
        db.run(con, insert(_SMETRICS).prefix_with("OR REPLACE")
               .values(session_id=sid, **{c: m.get(c) for c in _METRIC_COLS}))
    for w in data.get("worked_on", []):
        db.run(con, insert(models.worked_on).values(session_id=sid, item=str(w)))
    # answered questions (harvest) join the extracted facts as high-confidence decisions
    ask_decisions = [{"type": "decision", "scope": "organizational", "confidence": "high", **d}
                     for d in (sess.get("harvest") or {}).get("decisions", [])]
    hcs = list(data.get("hidden_context", [])) + ask_decisions
    new_refs = carry_refs(old_hc, [h.get("claim") for h in hcs])
    for h, ref in zip(hcs, new_refs):
        override = db.run(con, select(_HC_TYPES.c.type)
                          .where(_HC_TYPES.c.ref == ref)).fetchone()   # human-confirmed type wins
        repo, path = code_home(h)
        db.run(con, insert(hc).values(
            session_id=sid, type=override[0] if override else h.get("type"), scope=h.get("scope"),
            claim=h.get("claim"), evidence=h.get("evidence"), ref=ref,
            confidence=h.get("confidence"), repo=repo, path=path,
            as_of=valid_as_of(h.get("as_of"))))
    # same transaction: a reader never sees mentions without their facts
    refresh_facts(con, refs=old_refs | set(new_refs))
    for e in data.get("entities", []):
        eid = (e.get("id") or "").strip()
        if not eid:
            continue
        attrs = json.dumps(e["attributes"]) if e.get("attributes") else None
        ins = sqlite_insert(_ENTITIES).values(id=eid, type=e.get("type"),  # type majority-voted in do_index
                                              name=e.get("name") or eid, attributes=attrs)
        db.run(con, ins.on_conflict_do_update(index_elements=[_ENTITIES.c.id], set_={
            "type": func.coalesce(ins.excluded.type, _ENTITIES.c.type),
            "name": func.coalesce(ins.excluded.name, _ENTITIES.c.name),
            "attributes": func.coalesce(ins.excluded.attributes, _ENTITIES.c.attributes)}))
        db.run(con, insert(models.mentions).values(session_id=sid, entity_id=eid))
    for a in data.get("actions", []):
        tid = (a.get("target_id") or "").strip()
        if tid:
            db.run(con, insert(models.actions).values(session_id=sid, verb=a.get("verb"),
                                                      target_id=tid, context=a.get("context")))
    for r in data.get("relations", []):
        s_, t_ = (r.get("source_id") or "").strip(), (r.get("target_id") or "").strip()
        if s_ and t_:
            db.run(con, insert(models.relations).values(session_id=sid, source_id=s_,
                                                        rel=r.get("rel"), target_id=t_,
                                                        context=r.get("context")))
    for l in data.get("links", []):
        u = (l.get("url") or "").strip()
        if u:
            db.run(con, insert(models.links).values(session_id=sid, url=u, title=l.get("title")))
    for ds in data.get("data_sources", []):
        if ds:
            db.run(con, insert(models.data_sources).values(session_id=sid, name=str(ds)))
    # Registry verdicts, after the entities (person entities are evidence) and in the same transaction.
    # Its synthetic facts join the returned refs, or index_session would never index them.
    from analyzer import verify          # function-local: verify imports store
    touched = old_refs | set(new_refs)
    touched |= verify.verify_refs(con, refs=touched)
    con.commit()
    return touched                       # every ref this reload touched: index_session's FTS scope


# Review statuses recall never serves: kept out of `docs` here, and out of every query that bypasses it
# through search.servable.
HIDE_STATUS = ("rejected", "noise", "already-documented", "wrong")

# Fact docs: one row per fact, under its most recent mention's 'hc'||rowid. first_at/started_at are the
# first/last mention; validity comes from `facts` (LEFT JOIN: mid-backfill serves unfiltered). The text
# is the claim plus every mention's distinct evidence. NULL-ref rows keep one row each.
# Shared by build_fts (scope='') and index_session (scoped to the reload's refs).
_DOCS_HC = (
    "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text,"
    "first_at,valid_at,invalid_at,state) "
    "WITH m AS (SELECT h.rowid rid, h.ref ref, h.session_id sid, s.project project, s.source source, "
    "h.type type, h.claim claim, h.evidence evidence, "
    "ROW_NUMBER() OVER (PARTITION BY COALESCE(h.ref,'row:'||h.rowid) "
    "  ORDER BY IFNULL(s.ended_at,s.started_at) DESC, h.session_id DESC) rn, "
    "MIN(IFNULL(s.ended_at,s.started_at)) OVER (PARTITION BY COALESCE(h.ref,'row:'||h.rowid)) first_at, "
    "MAX(IFNULL(s.ended_at,s.started_at)) OVER (PARTITION BY COALESCE(h.ref,'row:'||h.rowid)) last_at "
    "FROM hidden_context h JOIN sessions s ON s.session_id=h.session_id{scope}) "
    "SELECT 'hc'||m.rid,'hidden_context',m.sid,m.project,m.last_at,m.type,m.source,r.status,"
    "IFNULL(cc.sessions,1),"
    # The canonical mention's text uncapped (a cap would move bm25 for single-mention facts); other
    # mentions' evidence appended distinct, newest first, capped.
    "m.type||': '||IFNULL(m.claim,'')||' '||IFNULL(m.evidence,'')||"
    "IFNULL(' '||(SELECT substr(group_concat(e,' '),1,1200) FROM "
    "  (SELECT h2.evidence e, MAX(IFNULL(s2.ended_at,s2.started_at)) d "
    "   FROM hidden_context h2 JOIN sessions s2 ON s2.session_id=h2.session_id "
    "   WHERE h2.ref=m.ref AND h2.evidence IS NOT NULL AND h2.evidence<>IFNULL(m.evidence,'') "
    "   GROUP BY h2.evidence ORDER BY d DESC, e)),''),"
    "m.first_at,f.valid_at,f.invalid_at,f.state "
    "FROM m LEFT JOIN reviews r ON r.kind='hc' AND r.ref=m.ref "
    "LEFT JOIN claim_clusters cc ON cc.ref=m.ref "
    "LEFT JOIN facts f ON f.ref=m.ref "
    "WHERE m.rn=1 AND (r.status IS NULL OR r.status NOT IN (" + ",".join(f"'{x}'" for x in HIDE_STATUS) + "))")


# harvest docs (analyzer/harvest.py) — LEFT JOIN: write() always stubs a sessions row, but never lose a doc to it.
# Shared by build_fts (full rebuild, where='') and index_session (where='WHERE <alias>.session_id=?').
_DOCS_PR_LINKS = (
    "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
    "SELECT 'prl'||p.rowid,'pr_link',p.session_id,s.project,COALESCE(s.ended_at,s.started_at,p.first_seen),'pr_link',"
    "IFNULL(s.source,'session'),NULL,1,"
    "p.repo||' PR #'||p.pr_number||' '||IFNULL(p.url,'')||"
    "(CASE WHEN p.first_seen=(SELECT MIN(q.first_seen) FROM pr_links q WHERE q.repo=p.repo AND "
    "q.pr_number=p.pr_number) THEN ' created ' ELSE ' pr-link events from ' END)||p.first_seen "
    "FROM pr_links p LEFT JOIN sessions s ON s.session_id=p.session_id {where}")
#   ^ resumed sessions re-emit pr-link streams; only the EARLIEST row is the creation — don't label the rest 'created'
_DOCS_WORKSPACE = (
    "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
    "SELECT 'ws'||w.session_id,'workspace',w.session_id,MIN(s.project),MIN(IFNULL(s.ended_at,s.started_at)),'workspace',"
    "IFNULL(MIN(s.source),'session'),NULL,1,GROUP_CONCAT(w.kind||':'||w.value,' ') "
    "FROM session_workspace w LEFT JOIN sessions s ON s.session_id=w.session_id {where} "
    "GROUP BY w.session_id")


# The stats aggregate for both paths. `{scope}` (empty, or `WHERE <canonical id> IN (...)`) sits inside
# `per` because the two correlated rescue EXISTS run once per `per` row: they are all of the cost.
_STATS_SELECT = """
        SELECT per.eid AS entity_id, per.s AS sessions, per.au AS distinct_authors, per.dom AS dominant_n,
          CASE WHEN per.s <= 1 AND per.dom = 0 THEN                 -- floor candidates only: corpus-wide
            EXISTS(SELECT 1 FROM signals g                           -- LIKE would be ~5M evaluations
                   JOIN hidden_context h ON h.ref = g.ref
                   JOIN mentions m2 ON m2.session_id = h.session_id
                   LEFT JOIN aliases a2 ON a2.alias_id = m2.entity_id
                   LEFT JOIN entities e2 ON e2.id = per.eid
                   WHERE g.kind = 'acted_on' AND IFNULL(a2.canonical_id, m2.entity_id) = per.eid
                     AND (LOWER(IFNULL(h.claim,'')||' '||IFNULL(h.evidence,'')) LIKE '%'||LOWER(per.eid)||'%'
                          OR LOWER(IFNULL(h.claim,'')||' '||IFNULL(h.evidence,''))
                             LIKE '%'||LOWER(IFNULL(e2.name, per.eid))||'%'))
          ELSE 0 END AS acted_n,
          per.last_seen AS last_seen,
          IFNULL(fam.fs, per.s) AS name_sessions,
          CASE WHEN per.s <= 1 AND per.dom = 0 THEN                 -- headline rescue (user-sim finding): a
            EXISTS(SELECT 1 FROM mentions m3                         -- deliverable named in its own session's
                   LEFT JOIN aliases a3 ON a3.alias_id = m3.entity_id
                   LEFT JOIN entities e3 ON e3.id = per.eid          -- summary/worked_on is the session's
                   LEFT JOIN sessions s3 ON s3.session_id = m3.session_id   -- subject even when the 40%%-share
                   LEFT JOIN worked_on w3 ON w3.session_id = m3.session_id  -- dominance test dilutes away
                   WHERE IFNULL(a3.canonical_id, m3.entity_id) = per.eid
                     AND (LOWER(IFNULL(s3.summary,'')) LIKE '%'||LOWER(IFNULL(e3.name, per.eid))||'%'
                          OR LOWER(IFNULL(w3.item,'')) LIKE '%'||LOWER(IFNULL(e3.name, per.eid))||'%'
                          OR LOWER(IFNULL(s3.summary,'')) LIKE '%'||LOWER(per.eid)||'%'   -- id too: worked_on
                          OR LOWER(IFNULL(w3.item,'')) LIKE '%'||LOWER(per.eid)||'%'))    -- often cites the path
          ELSE 0 END AS headline_n
        FROM (SELECT IFNULL(a.canonical_id, m.entity_id) eid,
                     COUNT(DISTINCT m.session_id) s,
                     COUNT(DISTINCT s2.user) au,
                     SUM(IFNULL(m.dominant, 0)) dom,
                     MAX(IFNULL(s2.ended_at, s2.started_at)) last_seen
              FROM mentions m
              LEFT JOIN aliases a ON a.alias_id = m.entity_id
              LEFT JOIN sessions s2 ON s2.session_id = m.session_id
              {scope}
              GROUP BY eid) per
        LEFT JOIN entities pe ON pe.id = per.eid
        LEFT JOIN (SELECT LOWER(IFNULL(e4.name, c4.eid)) fname, COUNT(DISTINCT c4.sid) fs
                   FROM (SELECT m4.session_id sid, IFNULL(a4.canonical_id, m4.entity_id) eid
                         FROM mentions m4 LEFT JOIN aliases a4 ON a4.alias_id = m4.entity_id) c4
                   LEFT JOIN entities e4 ON e4.id = c4.eid
                   GROUP BY fname) fam
               ON fam.fname = LOWER(IFNULL(pe.name, per.eid))"""


def mentioned_ids(con, session_id):
    """The canonical entity ids this session mentions now (read before and after a reload)."""
    m, al = models.mentions, models.Alias.__table__
    return {r[0] for r in db.run(con, select(func.ifnull(al.c.canonical_id, m.c.entity_id))
                                 .select_from(m.outerjoin(al, al.c.alias_id == m.c.entity_id))
                                 .where(m.c.session_id == session_id).distinct())}


def entity_scope_for_session(con, session_id, extra=()):
    """The canonical entity ids whose entity_stats row this session's ingest can move: the ids it
    mentions, `extra` (the ids it mentioned before the reload), and every entity sharing a lowercased
    name with one of those (`name_sessions` is a family rollup). Equal to a full rebuild for those rows.

    Two drifts outside the scope are known and fail-open, healed by the next corpus-wide pass:
    `acted_n` for an entity only an earlier session mentions, and a renamed entity's old family."""
    e, al = models.Entity.__table__, models.Alias.__table__
    mine = mentioned_ids(con, session_id) | set(extra)
    if not mine:
        return set()
    named = {r[0]: (r[1] or r[0]).lower()
             for r in db.run(con, select(e.c.id, e.c.name).where(e.c.id.in_(sorted(mine))))}
    names = sorted({named.get(i, i.lower()) for i in mine})   # no `entities` row: the id is the name
    a2 = al.alias("a2")
    fam = {r[0] for r in db.run(con, select(func.ifnull(a2.c.canonical_id, e.c.id))
                                .select_from(e.outerjoin(a2, a2.c.alias_id == e.c.id))
                                .where(func.lower(func.ifnull(e.c.name, e.c.id)).in_(names)))}
    return mine | fam


def _entity_stats_rows(con, scope):
    """Compute the stats rows, keyed by the SELECT's own aliases, outside any transaction. CARVE-OUT."""
    if scope is None:
        cur = con.execute(_STATS_SELECT.format(scope=""))
    else:
        ph = ",".join("?" * len(scope))
        cur = con.execute(_STATS_SELECT.format(
            scope=f"WHERE IFNULL(a.canonical_id, m.entity_id) IN ({ph})"), scope)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def write_entity_stats(con, entity_ids=None):
    """Rebuild the entity_stats table (alias-aware rollup to canonical ids), the entity gate's input:
    every row, or only `entity_ids` (entity_scope_for_session).

    Compute, then apply: the aggregate runs outside any transaction, so it never holds the file's one
    writer; readers see the old rows until the short write commits."""
    scope = None if entity_ids is None else sorted(set(entity_ids))
    if scope is not None and not scope:
        return                                           # a session with no mentions moves no stats row
    if con.in_transaction:
        con.commit()      # first: the caller's pending writes would hold the writer through the aggregate
    rows = _entity_stats_rows(con, scope)                # the entire cost, outside the writer lock
    con.execute("BEGIN")                                 # DEFERRED: IMMEDIATE would also lock the attached `tel`
    db.run(con, delete(_STATS) if scope is None else delete(_STATS).where(_STATS.c.entity_id.in_(scope)))
    if rows:
        db.run_many(con, insert(_STATS).values({c: bindparam(c) for c in rows[0]}), rows)
    con.commit()


# The entity docs rows; with entity_floor off it runs with empty joins and condition.
_DOCS_ENTITIES = (
    "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
    "SELECT 'ent'||e.id,'entity',NULL,NULL,NULL,e.type,'session',r.status,1,"
    "e.id||' '||IFNULL(e.name,'')||' '||IFNULL(e.attributes,'') "
    "FROM entities e LEFT JOIN reviews r ON r.kind='entity' AND r.ref=e.id "
    "{joins}WHERE (r.status IS NULL OR r.status NOT IN ('rejected')){cond}")


def _refresh_entity_docs(con, canonical_ids):
    """Re-emit the `entity` docs rows for `canonical_ids` and every alias of them (an alias has its own
    docs row, gated on the canonical stats row)."""
    ids = sorted(canonical_ids or ())
    if not ids:
        return
    e, al = models.Entity.__table__, models.Alias.__table__
    scope = sorted({r[0] for r in db.run(con, select(e.c.id)
                                         .select_from(e.outerjoin(al, al.c.alias_id == e.c.id))
                                         .where(func.ifnull(al.c.canonical_id, e.c.id).in_(ids)))})
    if not scope:
        return                                           # mentions with no `entities` row -> no docs row
    ph = ",".join("?" * len(scope))
    con.execute(f"DELETE FROM docs WHERE doc_id IN ({ph})", ["ent" + i for i in scope])
    cfg = tenant.config(con)
    if cfg.entity_floor:
        con.execute(_DOCS_ENTITIES.format(joins=entity_gate.gate_joins("e") + " ",
                                          cond=f" AND {entity_gate.served_cond(cfg, 'e')} AND e.id IN ({ph})"),
                    scope)
    else:
        con.execute(_DOCS_ENTITIES.format(joins="", cond=f" AND e.id IN ({ph})"), scope)


def has_docs(con):
    """Is the FTS `docs` table built yet? On a cold db, callers run build_fts instead of refreshing rows."""
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone())


def refresh_fact_docs(con, refs, session_id=None):
    """Re-emit the fact docs rows for `refs` without a full rebuild, plus `session_id`'s ref-less rows.
    A fact's row may live under another session's mention, so rows are deleted through the refs'
    current mention rowids. Chunked under SQLite's bound-variable cap. `docs` must exist."""
    refs = sorted({r for r in refs if r})
    if not refs:
        if session_id is not None:
            con.execute(_DOCS_HC.format(scope=" WHERE h.ref IS NULL AND h.session_id=?"), (session_id,))
        return
    for i in range(0, len(refs), 500):
        chunk = refs[i:i + 500]
        ph = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM docs WHERE doc_id IN "
                    f"(SELECT 'hc'||h.rowid FROM hidden_context h WHERE h.ref IN ({ph}))", chunk)
        # one statement shape: the ref-less clause rides every chunk, matching only on the first
        con.execute(_DOCS_HC.format(scope=f" WHERE h.ref IN ({ph}) OR (h.ref IS NULL AND h.session_id=?)"),
                    [*chunk, session_id if i == 0 else None])


def stems(texts):
    """Each text's set of tokens as `docs` indexes them, so a test on it agrees with what MATCH finds."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='{DOCS_TOKENIZER}')")
        con.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        con.executemany("INSERT INTO t(rowid, x) VALUES (?, ?)", [(i + 1, x or "") for i, x in enumerate(texts)])
        out = [set() for _ in texts]
        for doc, term in con.execute("SELECT doc, term FROM v"):
            out[doc - 1].add(term)
        return out
    finally:
        con.close()


def build_fts(con):
    """Rebuild FTS from current SQLite content; hides rejected and noise rows.

    One explicit transaction: sqlite3 autocommits DDL, and a reader on another connection would see
    no `docs` table mid-rebuild. DEFERRED, so the lock lands on `main` and not the attached `tel`."""
    write_entity_stats(con)                             # its own committed transaction, before the BEGIN
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN")
    con.execute("DROP TABLE IF EXISTS docs")            # recreated so the porter tokenizer applies
    # `started_at` holds last activity, IFNULL(ended_at, started_at), in every INSERT below
    con.execute(f"CREATE VIRTUAL TABLE docs USING fts5({DOCS_COLS})")
    con.execute(_DOCS_HC.format(scope=""))
    con.execute(
        "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
        "SELECT 'sum'||session_id,'summary',session_id,project,IFNULL(ended_at,started_at),'summary',source,NULL,1,summary "
        "FROM sessions WHERE summary IS NOT NULL")
    con.execute(
        "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
        "SELECT 'wo'||w.rowid,'worked_on',w.session_id,s.project,IFNULL(s.ended_at,s.started_at),'worked_on',s.source,NULL,1,w.item "
        "FROM worked_on w JOIN sessions s ON s.session_id=w.session_id")
    gate_cfg = tenant.config(con)
    if gate_cfg.entity_floor:                           # noise gate: one-off entities lose their docs row
        con.execute(_DOCS_ENTITIES.format(joins=entity_gate.gate_joins("e") + " ",
                                          cond=" AND " + entity_gate.served_cond(gate_cfg, "e")))
    else:
        con.execute(_DOCS_ENTITIES.format(joins="", cond=""))
    con.execute(_DOCS_PR_LINKS.format(where=""))
    con.execute(_DOCS_WORKSPACE.format(where=""))
    con.commit()


def index_session(con, sess, data):
    """Index one session incrementally: its structured load plus an FTS refresh for just its rows.
    Corroboration and claim_clusters wait for the next full `index`."""
    sid = sess["session_id"]
    dropped = mentioned_ids(con, sid)                             # before the reload: a dropped mention's counts must shrink
    refs = load_sqlite(con, sess, data)
    from analyzer import signals, harvest
    signals.harvest(con, sess, data)
    harvest.write(con, sess)                                      # after load_sqlite, which wiped its guarded rows
    stat_ids = entity_scope_for_session(con, sid, extra=dropped)
    write_entity_stats(con, stat_ids)                             # after signals.harvest: dominant flags must exist
    if has_docs(con):
        con.execute("DELETE FROM docs WHERE session_id=?", (sid,))
        refresh_fact_docs(con, refs, session_id=sid)              # a fact's row may sit under another session
        con.execute(                                             # summary
            "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
            "SELECT 'sum'||session_id,'summary',session_id,project,IFNULL(ended_at,started_at),'summary',source,NULL,1,summary "
            "FROM sessions WHERE session_id=? AND summary IS NOT NULL", (sid,))
        con.execute(                                             # worked_on
            "INSERT INTO docs(doc_id,kind,session_id,project,started_at,etype,source,status,corroboration,text) "
            "SELECT 'wo'||w.rowid,'worked_on',w.session_id,s.project,IFNULL(s.ended_at,s.started_at),'worked_on',s.source,NULL,1,w.item "
            "FROM worked_on w JOIN sessions s ON s.session_id=w.session_id WHERE w.session_id=?", (sid,))
        con.execute(_DOCS_PR_LINKS.format(where="WHERE p.session_id=?"), (sid,))
        con.execute(_DOCS_WORKSPACE.format(where="WHERE w.session_id=?"), (sid,))
        _refresh_entity_docs(con, stat_ids)              # the corpus-wide pass waits for compaction
    else:
        build_fts(con)                                           # cold db: build the whole corpus once
    con.commit()


def write_clusters(con):
    """Rebuild claim_clusters, the near-duplicate claim clusters; `sessions` is how many distinct sessions
    a cluster spans. Returns the number of multi-session clusters."""
    from analyzer import dedup
    hc = models.hidden_context                          # one read: two would race a live writer
    rows = db.run(con, select(hc.c.ref, hc.c.claim, hc.c.session_id)
                  .where(hc.c.ref.isnot(None))).fetchall()
    by_ref = {}
    for ref, claim, _sid in rows:
        by_ref.setdefault(ref, claim)                   # refs can repeat (identical claim in 2 sessions) — one rep
    cl = dedup.cluster(list(by_ref.items()))
    sess = collections.defaultdict(set)
    for ref, _claim, sid in rows:
        sess[cl[ref]].add(sid)
    db.run(con, delete(_CLUSTERS))
    db.run_many(con, insert(_CLUSTERS).prefix_with("OR REPLACE")
                .values(ref=bindparam("ref"), cluster_id=bindparam("cluster_id"),
                        sessions=bindparam("sessions")),
                [{"ref": ref, "cluster_id": cid, "sessions": len(sess[cid])} for ref, cid in cl.items()])
    con.commit()
    return sum(1 for s in sess.values() if len(s) > 1)


@functools.lru_cache(maxsize=1)
def owner_identity():
    """The Claude Code account logged in on this machine (~/.claude.json), or 'owner' without one."""
    try:
        acc = json.load(open(os.path.expanduser("~/.claude.json"))).get("oauthAccount", {})
        email = acc.get("emailAddress")
        if email:
            return {"email": email, "name": acc.get("displayName") or email, "account": acc.get("accountUuid")}
    except Exception:
        pass
    return {"email": "owner", "name": "owner", "account": None}

