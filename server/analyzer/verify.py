"""Deterministic verdicts for facts checkable against synced systems of record, no LLM.

Registries (the `registry` table) are synced snapshots per source token: the org roster (`sso`, from
user_tokens), GitHub org membership (`github:<org>`), git history (`git:<repo path>`). Each token
reconciles its own slice.

Two tenant_config knobs, comma-separated tokens (`csa.py org config <org> --set registry_sources=...`):
`registry_sources` says which registries are complete, so absence from one is negative evidence;
`registry_refute` arms refutation for a source, which otherwise runs dry. An unknown token is logged
and ignored; a tenant with no valid token gets no writes at all.

Verdicts (every write idempotent):
  verified      state='verified', confidence floored at VERIFIED_FLOOR, a reviews(kind='registry')
                receipt (invisible to serving).
  refuted       absence from a complete, armed registry, with positive person evidence: invalid_at =
                the slice's as_of, state='superseded', superseded by a synthetic registry fact.
  would-refute  a receipt only: what an unarmed source, or a sweep parked by the blast-radius cap
                (more than max(REFUTE_CAP_MIN, REFUTE_CAP_PCT of checked)), would refute.
  unverifiable  a recognized person claim no configured registry answers, from 'extracted' only.
  un-refute     a fact the registry superseded is reopened once the registry answers its subject again.

Checkable: organizational person-role and membership claims in the present tense, and repo-touch
claims (which never refute). Recognition is a regex and a role-noun lexicon over three claim shapes.

Runs at ingest (store.load_sqlite, same transaction) and on drain_loop's registry tick
(sync_and_sweep, which also refreshes the affected docs rows).
"""
import logging
import math
import os
import re
import subprocess
import time

from sqlalchemy import delete, exists, func, insert, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from analyzer import db, models, store, tenant

log = logging.getLogger("csa.verify")

_REG, _FACTS = models.RegistryRow.__table__, models.Fact.__table__
_EDGES, _HC = models.FactEdge.__table__, models.hidden_context
_REVIEWS, _SIGNALS = models.Review.__table__, models.Signal.__table__
_SESSIONS, _ENTITIES = models.SessionRow.__table__, models.Entity.__table__
_ENTITY_PINS = models.EntityTypePin.__table__
_TOKENS = tenant.UserToken.__table__                       # control plane — bridge only, never engine_for

VERIFIED_FLOOR = 0.9      # design: "registry verified (floor high)" — S5 recomputes from the same rule
REFUTE_CAP_MIN = 3        # blast radius: park the sweep when it would refute > max(MIN, PCT*checked)
REFUTE_CAP_PCT = 0.10

# --- sync: full reconcile per source token, upsert only-on-change --------------------------------

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _reconcile(con, source, rows):
    """Reconcile one source slice to exactly `rows` ({(kind, key): value}); returns the changed keys.
    A row's as_of moves only when its value does. A kind='sync' meta row dates the slice's last change,
    removals included, so an offboarding is dated correctly. No commit: the sync_* wrappers commit,
    and must never run inside load_sqlite's transaction."""
    changed, now = set(), _now()

    def put(kind, key, value):                             # the meta row upserts exactly like a data row
        ins = sqlite_insert(_REG).values(kind=kind, key=key, value=value, as_of=now, source=source)
        db.run(con, ins.on_conflict_do_update(
            index_elements=[_REG.c.kind, _REG.c.key, _REG.c.source],
            set_={"value": ins.excluded.value, "as_of": ins.excluded.as_of}))

    old = {(k, key): v for k, key, v in db.run(con, select(_REG.c.kind, _REG.c.key, _REG.c.value)
                                               .where(_REG.c.source == source,
                                                      _REG.c.kind != "sync"))}
    for (kind, key), value in rows.items():
        if old.pop((kind, key), None) != value:
            put(kind, key, value)
            changed.add(key)
    for kind, key in old:                                  # departed keys: the roster shrank
        db.run(con, delete(_REG).where(_REG.c.kind == kind, _REG.c.key == key,
                                       _REG.c.source == source))
        changed.add(key)
    if changed:
        put("sync", "", str(len(rows)))
    return changed


def sync_roster(con, org_id, control_con=None):
    """source 'sso': every email with any NON-revoked token for this org. Deliberately ignores
    expires_at — expiry is a sliding IDLE window (orgs._ttl_seconds), and someone on vacation must
    not fall off the roster; offboarding = revocation. The receipt names this basis. Operator note:
    that also means revoking someone's LAST token (auth hygiene, leaked-token rotation) reads as
    offboarding to an armed sweep — expect their membership facts to flip on the next tick.
    Commits (public entry point — an uncommitted sync silently vanishing was sim-round friction #1)."""
    if control_con is None:
        from analyzer import orgs                          # late: control plane, not the corpus
        control_con = orgs.control_db()
    emails = [r[0] for r in db.run(control_con, select(func.lower(_TOKENS.c.email)).distinct()
                                   .where(_TOKENS.c.org_id == org_id,
                                          _TOKENS.c.revoked_at.is_(None),
                                          _TOKENS.c.email.like("%@%")))]
    changed = _reconcile(con, "sso", {("member", e): org_id for e in emails})
    con.commit()
    return changed


def _gh_members(gh_org, role=None):
    """The only network call in this module, isolated so tests stub it (never called without a
    github:<org> token; `gh` is authenticated out-of-band on the box)."""
    url = f"/orgs/{gh_org}/members" + (f"?role={role}" if role else "")
    out = subprocess.run(["gh", "api", url, "--paginate", "-q", ".[].login"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise RuntimeError(f"gh api {url}: {out.stderr.strip()[:200]}")
    return [l.strip().lower() for l in out.stdout.split() if l.strip()]


def sync_github(con, gh_org, fetch=None):
    """source 'github:<org>': kind=member (value 'github:<org>') + kind=role (value admin|member).
    Commits (public entry point)."""
    fetch = fetch or _gh_members
    members, admins = fetch(gh_org), set(fetch(gh_org, role="admin"))
    rows = {}
    for login in members:
        rows[("member", login)] = f"github:{gh_org}"
        rows[("role", login)] = "admin" if login in admins else "member"
    changed = _reconcile(con, f"github:{gh_org}", rows)
    con.commit()
    return changed


def sync_git(con, repo_path):
    """source 'git:<path>': kind=committer per author email, value = repo basename. The rollup is the
    shared artifact with the authority A/B work — this slice is its input, unchanged."""
    repo = os.path.basename(os.path.normpath(repo_path))
    out = subprocess.run(["git", "-C", repo_path, "log", "--format=%ae"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise RuntimeError(f"git log {repo_path}: {out.stderr.strip()[:200]}")
    emails = {e.strip().lower() for e in out.stdout.splitlines() if "@" in e}
    changed = _reconcile(con, f"git:{repo_path}", {("committer", e): repo for e in emails})
    con.commit()
    return changed


_warned_tokens = set()                                     # warn once per process, not every tick


def _valid_tokens(tokens):
    """Drop malformed registry tokens before anything acts on them: a garbage `registry_sources`
    behaves exactly like an unconfigured tenant, with no writes and no subprocesses."""
    ok = []
    for t in tokens:
        if t == "sso" or (t.startswith("github:") and re.fullmatch(r"[A-Za-z0-9-]+", t[7:])) \
                or (t.startswith("git:") and t[4:].strip()):
            ok.append(t)                                   # the github arg becomes a `gh api` path segment
        elif t not in _warned_tokens:
            _warned_tokens.add(t)
            log.warning("unknown registry token %r — ignored (valid: sso | github:<org> | git:<path>)", t)
    return tuple(ok)


def sync_org(con, org_id, fetch=None, control_con=None):
    """Sync every configured token; per-token failures never block the others. -> {token: changed}."""
    changed = {}
    for token in _valid_tokens(tenant.config(con).registry_sources):
        try:
            if token == "sso":
                changed[token] = sync_roster(con, org_id, control_con=control_con)
            elif token.startswith("github:"):
                changed[token] = sync_github(con, token.split(":", 1)[1], fetch=fetch)
            else:
                changed[token] = sync_git(con, token.split(":", 1)[1])
        except Exception as e:                             # a dead gh credential must not kill the sweep
            log.warning("registry sync %s failed: %s", token, str(e)[:200])
    return changed


# --- recognizer: three narrow claim shapes, no LLM ------------------------------------------------

_SUBJ = r"(?P<subj>[A-Za-z][\w'+.-]*@[\w.-]+\.\w+|[A-Z][\w'.-]*(?: [A-Z][\w'.-]*)?)"
_ROLE_RE = re.compile(
    _SUBJ + r" (?P<verb>is|was) (?:the |an? |our |now )?(?P<role>[^,.;:]{2,80}?)(?= and | who | that |[,.;:]|$)")
_MEMBER_OF_RE = re.compile(
    _SUBJ + r" (?P<verb>is|was) (?:a |now )?(?:member|part) of (?P<obj>.{2,60}?)\.?$")
_IN_ORG_RE = re.compile(
    _SUBJ + r" (?P<verb>is|was) in the (?P<obj>.{2,50}?) ?[Oo]rg(?:anization)?\b")
_REPO_RE = re.compile(
    _SUBJ + r" (?P<verb>owns|maintains|is the (?:owner|maintainer) of) (?P<obj>[\w./-]{2,60})")

# Head noun of the role phrase must be one of these for the claim to read as a PERSON-role claim.
_ROLE_WORDS = frozenset((
    "lead", "manager", "engineer", "developer", "admin", "administrator", "owner", "maintainer",
    "architect", "cto", "ceo", "coo", "cfo", "founder", "cofounder", "co-founder", "director",
    "vp", "head", "president", "approver", "reviewer", "on-call", "oncall", "sre", "intern",
    "contractor", "employee"))
# Dummy subjects the capitalized-word pattern would otherwise swallow ("There is no ACL/owner-gating
# on corpus sessions…" was the one real-corpus match — a true security gotcha, never to be refuted).
_STOP_SUBJECTS = frozenset((
    "there", "it", "this", "that", "these", "those", "everyone", "everybody", "nobody", "no",
    "someone", "somebody", "anyone", "we", "you", "i", "he", "she", "they", "the", "a", "an"))
_ORG_WORDS = frozenset(("org", "organization", "company", "the org", "the organization", "the company"))


def recognize(claim):
    """-> (klass, subject, obj, past) or None. klass ∈ member|role|repo. Deterministic, narrow by
    design: anything unrecognized is simply not checkable — no guessing."""
    c = " ".join((claim or "").split())
    for klass, rx in (("member", _MEMBER_OF_RE), ("member", _IN_ORG_RE), ("repo", _REPO_RE),
                      ("role", _ROLE_RE)):
        m = rx.match(c)
        if not m:
            continue
        subj = m.group("subj").strip()
        if subj.lower() in _STOP_SUBJECTS:
            return None
        if klass == "role":
            head = re.split(r"[\s-]+", m.group("role").strip().lower())[-1]
            if head not in _ROLE_WORDS:
                continue                                   # "…is the graph store" is not a person claim
            return "role", subj, m.group("role").strip(), m.group("verb") == "was"
        return klass, subj, m.group("obj").strip(), m.group("verb") == "was"
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _local(key):
    return key.split("@", 1)[0] if "@" in key else key


def _resolve(subj, keys):
    """subj -> (key, ambiguous) against one slice's keys: exact email, else UNIQUE normalized match
    of the local-part (or its first dot/underscore segment: dan.a@ answers to "Dan") against the
    full name or its first token. Two candidates = abstain (carry_refs' asymmetry-of-harm rule: a
    wrong match moves a verdict onto the wrong person, irreversibly)."""
    s = subj.strip().rstrip(".,")
    if "@" in s:
        return (s.lower() if s.lower() in keys else None), False
    n_full, n_first = _norm(s), _norm(s.split()[0])
    cands = {k for k in keys
             if _norm(_local(k)) in (n_full, n_first)
             or _norm(re.split(r"[._+-]", _local(k))[0]) in (n_full, n_first)}
    if len(cands) == 1:
        return cands.pop(), False
    return None, len(cands) > 1


# --- the verify stage ------------------------------------------------------------------------------

def _slices(con, tokens):
    out = {}
    for token in tokens:
        rows = db.run(con, select(_REG.c.kind, _REG.c.key, _REG.c.value, _REG.c.as_of)
                      .where(_REG.c.source == token)).fetchall()
        # absence is dated by the slice's last change (the sync meta row), else its newest row
        sync_at = next((a for kind, _, _, a in rows if kind == "sync"), None)
        out[token] = {"member": {k: (v, a) for kind, k, v, a in rows if kind == "member"},
                      "role": {k: (v, a) for kind, k, v, a in rows if kind == "role"},
                      "committer": {k: (v, a) for kind, k, v, a in rows if kind == "committer"},
                      "as_of": sync_at or max((a for kind, _, _, a in rows
                                               if a and kind != "sync"), default=None)}
    return out


def _basis(token):
    return {"sso": "active CSA identity"}.get(
        token, "github org membership" if token.startswith("github:") else "commit history")


def _receipt_row(con, ref, status, note, flagger):
    """Record a verdict in reviews(kind='registry'), the current verdict, and in `signals`, the history
    (the first date per verdict kind). Only when changed, and never via store.set_review, which
    commits inside load_sqlite's transaction."""
    if db.run(con, select(literal(1)).select_from(_REVIEWS)
              .where(_REVIEWS.c.kind == "registry", _REVIEWS.c.ref == ref,
                     _REVIEWS.c.status == status, _REVIEWS.c.note == note)).fetchone():
        return False
    db.run(con, insert(_REVIEWS).prefix_with("OR REPLACE")
           .values(kind="registry", ref=ref, status=status, note=note, flagger=flagger))
    db.run(con, insert(_SIGNALS).prefix_with("OR IGNORE")
           .values(ref=ref, kind=f"registry_{status.replace('-', '_')}",
                   session_id=flagger, at=_now()))
    return True


def _mark_verified(con, ref, token, note):
    changed = db.run(con, update(_FACTS).values(
        state="verified", confidence=func.max(func.ifnull(_FACTS.c.confidence, 0), VERIFIED_FLOOR))
        .where(_FACTS.c.ref == ref,
               or_(_FACTS.c.state != "verified", _FACTS.c.confidence.is_(None),
                   _FACTS.c.confidence < VERIFIED_FLOOR))).rowcount > 0
    return _receipt_row(con, ref, "verified", note, f"registry:{token}") or changed


def _refute(con, ref, subj, token, as_of):
    """Close the loser's window and mint the mention-backed synthetic winner. Idempotent: every
    write is guarded or OR IGNORE; a second identical pass changes zero rows."""
    basis, now = _basis(token), _now()
    receipt = f"registry {token} ({basis}) as_of {as_of}: no identity matches '{subj}' in a complete registry"
    claim = f"{subj} is not in the {basis} registry ({token})"
    sref, sid = store.hc_ref(claim), f"registry:{token}"
    ins = sqlite_insert(_SESSIONS).values(
        session_id=sid, project="registry", started_at=as_of, ended_at=as_of, n_user=0, n_asst=0,
        source="registry", user=f"registry:{token}")
    db.run(con, ins.on_conflict_do_update(
        index_elements=[_SESSIONS.c.session_id], set_={"ended_at": ins.excluded.ended_at},
        where=ins.excluded.ended_at > func.ifnull(_SESSIONS.c.ended_at, "")))
    h2 = _HC.alias("h2")                                             # hidden_context has no PK — guard
    db.run(con, insert(_HC).from_select(
        ["session_id", "type", "scope", "claim", "evidence", "ref", "confidence", "as_of"],
        select(literal(sid), literal("constraint"), literal("organizational"), literal(claim),
               literal(receipt), literal(sref), literal("high"), literal(as_of))
        .where(~exists(select(literal(1)).select_from(h2)
                       .where(h2.c.session_id == sid, h2.c.ref == sref)))))
    store.refresh_facts(con, refs={sref})
    db.run(con, update(_FACTS).values(                               # registry-backed by construction
        state="verified", confidence=func.max(func.ifnull(_FACTS.c.confidence, 0), VERIFIED_FLOOR))
        .where(_FACTS.c.ref == sref,
               or_(_FACTS.c.state != "verified", _FACTS.c.confidence.is_(None),
                   _FACTS.c.confidence < VERIFIED_FLOOR)))
    db.run(con, insert(_EDGES).prefix_with("OR IGNORE")
           .values(src_ref=sref, rel="supersedes", dst_ref=ref, at=as_of,
                   actor=f"registry:{token}", note=receipt))
    db.run(con, update(_FACTS)
           .values(invalid_at=as_of, expired_at=now, state="superseded", confidence=0.0)
           .where(_FACTS.c.ref == ref, _FACTS.c.state != "superseded"))     # stamps once, at the transition
    _receipt_row(con, ref, "refuted", receipt, f"registry:{token}")
    return sref


def _unrefute(con, ref, token, note, verified):
    """Reopen a registry-superseded fact whose subject the registry answers again: 'verified' when it
    confirms the claim, else 'unverifiable'. Deleting the synthetic mention lets refresh_facts cull the
    synthetic fact and its edges."""
    srcs = [r for (r, a) in db.run(con, select(_EDGES.c.src_ref, _EDGES.c.actor)
                                   .where(_EDGES.c.rel == "supersedes", _EDGES.c.dst_ref == ref))
            if (a or "").startswith("registry:")]
    for sref in srcs:
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone():
            con.execute("DELETE FROM docs WHERE doc_id IN "        # before the mention goes — after,
                        "(SELECT 'hc'||rowid FROM hidden_context WHERE ref=?)", (sref,))  # rowids are gone
        db.run(con, delete(_HC).where(_HC.c.ref == sref, _HC.c.session_id.like("registry:%")))
    store.refresh_facts(con, refs=set(srcs))
    if verified:
        db.run(con, update(_FACTS).values(
            invalid_at=None, expired_at=None, state="verified",
            confidence=func.max(func.ifnull(_FACTS.c.confidence, 0), VERIFIED_FLOOR))
            .where(_FACTS.c.ref == ref))
    else:                                                  # not the refuted-era 0.0
        db.run(con, update(_FACTS)
               .values(invalid_at=None, expired_at=None, state="unverifiable", confidence=None)
               .where(_FACTS.c.ref == ref))
    status = "verified" if verified else "unverifiable"
    _receipt_row(con, ref, status, note + " [reopened: registry now answers]", f"registry:{token}")
    return set(srcs)


def verify_refs(con, refs=None, only_keys=None):
    """Verify every checkable fact, or only `refs`; `only_keys` narrows a re-sync to the subjects it
    changed. Returns the refs whose state changed. No commit: runs inside the caller's transaction."""
    cfg = tenant.config(con)
    tokens = _valid_tokens(cfg.registry_sources)
    if not tokens:
        return set()                                       # unconfigured: no writes of any kind
    refute_ok = set(cfg.registry_refute) & set(tokens)
    reg = _slices(con, tokens)
    git_tokens = [t for t in tokens if t.startswith("git:")]
    member_tokens = [t for t in tokens if not t.startswith("git:")]
    org_names = {_norm(v) for t in member_tokens for v, _ in reg[t]["member"].values()} | \
                {_norm(t.split(":", 1)[1]) for t in member_tokens if t.startswith("github:")}

    ent, pin = _ENTITIES.alias("e"), _ENTITY_PINS.alias("t")
    person_names = {name.lower() for (name,) in db.run(con, select(ent.c.name)
                    .select_from(ent.outerjoin(pin, pin.c.id == ent.c.id))
                    .where(func.coalesce(pin.c.type, ent.c.type) == "person",
                           ent.c.name.isnot(None)))}                 # HITL retype wins
    user_locals = {_norm(_local(u)) for (u,) in db.run(con, select(_SESSIONS.c.user).distinct()
                                                      .where(_SESSIONS.c.user.like("%@%")))}

    stmt = select(_FACTS.c.ref, _FACTS.c.claim, _FACTS.c.state).where(
        _FACTS.c.scope == "organizational",
        _FACTS.c.state.in_(("extracted", "unverifiable", "verified", "superseded")),
        or_(_FACTS.c.author.is_(None), _FACTS.c.author.notlike("registry:%")))
    if refs is not None:
        refs = {r for r in refs if r}
        if not refs:
            return set()
        stmt = stmt.where(_FACTS.c.ref.in_(sorted(refs)))
    rows = db.run(con, stmt).fetchall()

    touched, would_refute, checked = set(), [], 0
    for ref, claim, state in rows:
        rec = recognize(claim)
        if not rec:
            continue
        klass, subj, obj, past = rec
        # positive person evidence: also keeps "Postgres is the session store manager" out
        is_email = "@" in subj
        is_person = is_email or subj.lower() in person_names or _norm(subj) in user_locals or \
            _norm(subj.split()[0]) in user_locals
        if not is_person:
            continue                                       # not checkable; leave the fact alone
        checked += 1

        # which slices can answer; a current snapshot answers no past-tense claim either way
        if past:
            cands = []
        elif klass == "repo":
            cands = git_tokens
        elif klass == "member":
            obj_n = _norm(re.sub(r"^(the|our) ", "", obj.lower()))
            if "github" in obj.lower():
                cands = [t for t in member_tokens if t.startswith("github:")]
            elif obj.lower() in _ORG_WORDS or obj_n in org_names:
                cands = [t for t in member_tokens if t == "sso"] or member_tokens
            else:
                cands = []                                 # "part of the on-call rotation": not ours
        else:
            cands = member_tokens

        verdict = None                                     # (kind, token, note)
        ambiguous = False
        for token in cands:
            keys = set(reg[token]["committer"] if klass == "repo" else reg[token]["member"])
            key, amb = _resolve(subj, keys)
            ambiguous = ambiguous or amb
            if key is None:
                continue
            if klass == "repo":
                repo, as_of = reg[token]["committer"][key]
                if _norm(repo) == _norm(os.path.basename(obj.rstrip("/"))):
                    verdict = ("verified", token,
                               f"registry {token} ({_basis(token)}) as_of {as_of}: "
                               f"{key} authored commits in {repo}")
                    break
                continue
            value, as_of = reg[token]["member"][key]
            note = (f"registry {token} ({_basis(token)}) as_of {as_of}: "
                    f"{key} is a member of {value}")
            if klass == "member":
                verdict = ("verified", token, note)
            else:                                          # role: exact value match or abstain —
                role_row = reg[token]["role"].get(key)     # admin|member doesn't model "CTO"
                if role_row and _norm(role_row[0]) == _norm(obj):
                    verdict = ("verified", token,
                               f"registry {token} ({_basis(token)}) as_of {role_row[1]}: "
                               f"{key} role is {role_row[0]}")
                else:                                      # present, role unanswerable: abstain,
                    verdict = ("present", token, note)     # and presence still un-refutes below
            break

        if verdict and state == "superseded":
            # reopen only what the registry itself closed: our refuted receipt, no other actor's edge
            ours = db.run(con, select(literal(1)).select_from(_REVIEWS)
                          .where(_REVIEWS.c.kind == "registry", _REVIEWS.c.ref == ref,
                                 _REVIEWS.c.status == "refuted")).fetchone()
            foreign = [a for (a,) in db.run(con, select(_EDGES.c.actor)
                       .where(_EDGES.c.rel == "supersedes", _EDGES.c.dst_ref == ref))
                       if not (a or "").startswith("registry:")]
            if ours and not foreign:
                touched |= _unrefute(con, ref, verdict[1], verdict[2], verdict[0] == "verified")
                touched.add(ref)
            continue
        if verdict and verdict[0] == "verified":
            if _mark_verified(con, ref, verdict[1], verdict[2]):
                touched.add(ref)
            continue
        if state == "superseded":
            continue                                       # still refuted (or S5/human-owned) — hold
        # Absent everywhere it could be answered: refute only with every gate open.
        refutable = (klass in ("member", "role") and cands and not past and not ambiguous
                     and verdict is None)
        if refutable and only_keys is not None and not any(
                _norm(_local(k)) in (_norm(subj), _norm(subj.split()[0])) or k == subj.lower()
                for k in only_keys):
            refutable = False                              # a re-sync scoped to other people's keys
        if refutable:
            # an empty roster is a broken sync, never evidence that nobody exists
            armed = [t for t in cands if t in refute_ok and reg[t]["as_of"] and reg[t]["member"]]
            dry = [t for t in cands if t not in refute_ok and reg[t]["as_of"] and reg[t]["member"]]
            if armed:
                would_refute.append((ref, subj, armed[0], reg[armed[0]]["as_of"]))
                continue
            if dry:
                t = dry[0]
                note = (f"registry {t} ({_basis(t)}) as_of {reg[t]['as_of']}: no identity matches "
                        f"'{subj}' [dry-run: {t} not in registry_refute]")
                if _receipt_row(con, ref, "would-refute", note, f"registry:{t}"):
                    touched.add(ref)
                continue
        if state == "extracted":                           # recognized, no registry could answer
            db.run(con, update(_FACTS).values(state="unverifiable")
                   .where(_FACTS.c.ref == ref, _FACTS.c.state == "extracted"))
            touched.add(ref)

    cap = max(REFUTE_CAP_MIN, math.ceil(REFUTE_CAP_PCT * checked))
    if len(would_refute) > cap:                            # stale-registry blast radius, bounded
        log.warning("verify: %d refutations exceed cap %d — PARKED as would-refute receipts",
                    len(would_refute), cap)
        for ref, subj, token, as_of in would_refute:
            note = (f"registry {token} ({_basis(token)}) as_of {as_of}: no identity matches "
                    f"'{subj}' [parked: blast-radius cap {cap}]")
            if _receipt_row(con, ref, "would-refute", note, f"registry:{token}"):
                touched.add(ref)
    else:
        for ref, subj, token, as_of in would_refute:
            touched.add(_refute(con, ref, subj, token, as_of))
            touched.add(ref)
    return touched


def verify_all(con):
    return verify_refs(con, refs=None)


def reindex_refs(con, refs):
    """Per-ref docs surgery (the index_session pattern, store.py) so SWEEP verdicts reach the
    serving index — the docs validity columns are what search filters on, not `facts`."""
    refs = {r for r in refs if r}
    if not refs or not con.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone():
        return
    ph = ",".join("?" * len(refs))
    con.execute(f"DELETE FROM docs WHERE doc_id IN "
                f"(SELECT 'hc'||h.rowid FROM hidden_context h WHERE h.ref IN ({ph}))", sorted(refs))
    con.execute(store._DOCS_HC.format(scope=f" WHERE h.ref IN ({ph})"), sorted(refs))


def sync_and_sweep(con, org_id, fetch=None, control_con=None):
    """The periodic job: sync every configured token, verify the whole corpus (facts age; registries
    change), surface verdict changes in docs, commit. Returns #refs whose db state changed (a
    refutation counts 2: loser + synthetic winner). org_id is needed BESIDE con because the sso
    roster lives in the control db keyed by org — the corpus file alone doesn't know its own id."""
    if not tenant.config(con).registry_sources:
        return 0
    sync_org(con, org_id, fetch=fetch, control_con=control_con)
    touched = verify_all(con)
    reindex_refs(con, touched)
    con.commit()
    return len(touched)
