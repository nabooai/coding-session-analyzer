"""Org control plane (data/control.db): orgs, invites, per-user tokens, the audit log. The one source of
verified identity, and it never holds corpus content (each org's corpus is its own file, store.org_db).

Tokens are high-entropy random secrets, stored and looked up by their sha256; a slow KDF would buy
nothing. Statements run through db.run on the raw connection, never db.engine_for: its persistent
`journal_mode=WAL` would change control.db's on-disk format (tests/test_orgs_models.py pins 'delete').

Logs to stderr only: MCP stdout is the JSON-RPC channel.
"""
import calendar, hashlib, hmac, logging, os, re, secrets, sqlite3, threading, time, urllib.parse

from sqlalchemy import bindparam, column, delete, func, insert, literal, select, update

from analyzer import db, migrations, store
from analyzer.tenant import ExtractionStall, Invite, Org, Sso, UserToken, WorkspaceRequest, admin_log

_ORG, _UT, _AL, _WR = Org.__table__, UserToken.__table__, admin_log, WorkspaceRequest.__table__
_INV, _SSO, _STALL = Invite.__table__, Sso.__table__, ExtractionStall.__table__

log = logging.getLogger("csa.orgs")

TOKEN_PREFIX = "csa_"                                    # user token   — greppable in logs/leak scans
INVITE_PREFIX = "inv_"                                   # org invite   — what the cli-auth page accepts
JOIN_PREFIX = "jn_"                                      # per-person invite link — what /join carries
INVITE_TTL_DAYS = 7
# Live unredeemed links one workspace may hold: bounds the standing surface an XSS could open.
MAX_PENDING_INVITES = 50
_LAST_USED_EVERY = 3600                                  # refresh last_used_at at most hourly (see _touch)


def _now(plus=0.0):
    """UTC ISO-8601, optionally `plus` seconds ahead. Fixed-width, so string comparison is time order."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + plus))


def _epoch(iso):
    """ISO-8601 Z -> epoch seconds. calendar.timegm, not time.mktime, which reads local time."""
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def _ttl_seconds(org_id=None):
    """Idle lifetime of a per-user token in seconds, from the org's token_ttl_days (the default without
    an org); 0 disables expiry. Every use slides it forward (see _touch)."""
    from analyzer import tenant                          # late: orgs is imported before schemas exist
    if org_id is not None:
        try:
            days = tenant.get_config(store.org_db(org_id)).token_ttl_days
        except Exception:                                # unreadable org db must not block token flows
            days = tenant.defaults().token_ttl_days
    else:
        days = tenant.defaults().token_ttl_days
    return int(days * 86400) if days > 0 else 0


_legacy_imported = False
_legacy_lock = threading.Lock()


def _import_legacy_tokens(con):
    """Copy tokens from a pre-control-plane `user_tokens` table in the default corpus db, idempotently.
    True once done (or nothing to do); False on a transient failure, so the caller retries."""
    try:
        corpus = store.org_db()
        if not os.path.exists(corpus):
            return True
        old = sqlite3.connect(f"file:{corpus}?mode=ro", uri=True)
        try:
            # CARVE-OUT: raw reads of a foreign schema the models do not describe
            if not old.execute("SELECT 1 FROM sqlite_master WHERE name='user_tokens'").fetchone():
                return True
            cur = old.execute("SELECT token_hash,email,org_id,hostname,created,revoked_at "
                              "FROM user_tokens")
            cols = tuple(d[0] for d in cur.description) + ("last_used_at",)
            rows = [dict(zip(cols, r + (None,))) for r in cur.fetchall()]
        finally:
            old.close()
        if rows:
            db.run_many(con, insert(_UT).prefix_with("OR IGNORE")
                        .values({c: bindparam(c) for c in cols}), rows)
            date_undated(con)                           # imported rows arrive with no expires_at
            _audit(con, "import_legacy_tokens", store.DEFAULT_ORG, f"n={len(rows)}")
            con.commit()
            log.info("imported %d pre-org-layer token(s) from %s", len(rows), corpus)
        return True
    except Exception as e:
        log.warning("legacy token import deferred (will retry): %s", str(e)[:120])
        return False


def date_undated(con):
    """Give every live token without an expiry one, a TTL past its last use: a machine still shipping
    keeps working, a quiet one is already cold. Returns the count."""
    ttl = _ttl_seconds()
    if not ttl:
        return 0
    n = db.run(con, update(_UT).values(                  # sqlite computes the date; strftime keeps the Z shape
        expires_at=func.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 func.coalesce(_UT.c.last_used_at, _UT.c.created, _now()),
                                 f"+{ttl} seconds"))
        .where(_UT.c.revoked_at.is_(None), _UT.c.expires_at.is_(None))).rowcount
    if n:
        log.info("token expiry: dated %d previously non-expiring token(s) from their last use", n)
    return n


def control_path():
    """The control plane's file: `control.db` beside the corpus in this box's data dir, absolute and
    resolved on every call (store.data_root)."""
    return store.data_root() / "control.db"


def control_db(path=None):
    """A new connection per call: sqlite3 connections are not thread-safe."""
    global _legacy_imported
    is_box_db = path is None                             # an explicit path is a test/tool db, never the box's
    path = control_path() if path is None else path
    if path != ":memory:" and os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)                          # no pragmas: control.db stays in rollback-journal mode
    migrations.ensure(con, "control", None if path == ":memory:" else os.path.abspath(path))
    if not _legacy_imported and is_box_db:
        with _legacy_lock:
            if not _legacy_imported:
                _legacy_imported = _import_legacy_tokens(con)
    return con


def _hash(secret):
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _audit(con, action, org_id, detail=""):
    """NEVER commits: each mutating caller owns mutation+audit+commit as ONE transaction unit."""
    db.run(con, insert(_AL).values(at=_now(), action=action, org_id=org_id, detail=detail))


def slug(name):
    """Human org name -> a valid org id (which is also a directory name — store.valid_org_id is the gate)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")[:63]
    return store.valid_org_id(s or "org")                # raises on anything that still isn't path-safe


# --- orgs -------------------------------------------------------------------------------------------

_warned = {}                                             # key -> last logged epoch (see _warn_once)
_WARN_EVERY = 300                                        # seconds


def _warn_once(key, fmt, *args):
    """Log a degraded-state warning at most every _WARN_EVERY seconds per key: health polling would
    otherwise flood the log with one fault."""
    now = time.time()
    if now - _warned.get(key, 0) < _WARN_EVERY:
        return
    _warned[key] = now
    log.warning(fmt, *args)


def default_is_adopted(con=None):
    """Has the built-in `default` corpus been adopted as a named tenant (an org row exists)? Fail-closed
    on an unreadable control plane: that must not re-open the env-invite path."""
    try:
        con = con or control_db()
        return db.run(con, select(literal(1)).select_from(_ORG)
                      .where(_ORG.c.id == store.DEFAULT_ORG)).fetchone() is not None
    except Exception as e:
        _warn_once("default_is_adopted", "cannot read %s (%s) — assuming adopted (fail-closed)",
                   control_path(), str(e)[:80])
        return True


def is_multi_tenant(con=None, on_error=True):
    """Does this deployment host tenants: any row in `orgs`, an adopted `default` included?

    `on_error` is the answer when the control plane is unreadable; the auth and invite callers pass
    True (never hand a shared credential a corpus)."""
    try:
        con = con or control_db()
        row = db.run(con, select(func.count()).select_from(_ORG)).fetchone()
        return (row[0] or 0) > 0
    except Exception as e:
        _warn_once("is_multi_tenant", "cannot read %s (%s) — answering %s for this caller; see "
                   "/api/health", control_path(), str(e)[:80], on_error)
        return on_error


def control_plane_ok(con=None):
    """Can the control plane be read at all? /api/health reports this."""
    try:
        con = con or control_db()
        db.run(con, select(func.count()).select_from(_ORG)).fetchone()
        return True
    except Exception as e:
        _warn_once("control_plane", "control plane unreadable: %s (%s). Every credential is validated "
                   "against this file; usually ownership on the bind mount after an image pull.",
                   control_path(), str(e)[:80])
        return False


def set_extraction_stall(org_id, reason, con=None):
    """Record why `org_id`'s extraction is not draining; the reason is redacted and cut to 300 chars.
    A repeat of the same reason writes nothing, so `since` keeps the first failure's time."""
    from analyzer import redact
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    text = redact.redact(" ".join((reason or "").split()), "privacy")[0][:300]
    row = db.run(con, select(_STALL.c.reason).where(_STALL.c.org_id == oid)).fetchone()
    if row and row[0] == text:
        return
    db.run(con, delete(_STALL).where(_STALL.c.org_id == oid))
    db.run(con, insert(_STALL).values(org_id=oid, reason=text, since=_now()))
    con.commit()


def clear_extraction_stall(org_id, con=None):
    con = con or control_db()
    if db.run(con, delete(_STALL).where(_STALL.c.org_id == store.valid_org_id(org_id))).rowcount:
        con.commit()


def extraction_stalls(con=None):
    """{org_id: {"reason", "since"}} for every workspace whose extraction is failing."""
    con = con or control_db()
    return {o: {"reason": r, "since": s}
            for o, r, s in db.run(con, select(_STALL.c.org_id, _STALL.c.reason, _STALL.c.since))}


def corpus_orgs(con=None):
    """Every org id that has a corpus file: `default` first and listed once whether or not it has an
    org row. Falls back to `default` alone when the control plane is unreadable."""
    try:
        con = con or control_db()
        return [store.DEFAULT_ORG] + [o["id"] for o in list_orgs(con)
                                      if o["id"] != store.DEFAULT_ORG]
    except Exception as e:
        log.warning("corpus_orgs failed, falling back to the default org only: %s", str(e)[:120])
        return [store.DEFAULT_ORG]


def sole_org(con=None):
    """The one workspace this server hosts, or None when it hosts several or the control plane cannot
    say. `default` counts without an org row while its corpus file exists."""
    try:
        con = con or control_db()
        ids = {r[0] for r in db.run(con, select(_ORG.c.id))}
    except Exception:
        return None
    if not ids or os.path.exists(store.org_db()):
        ids.add(store.DEFAULT_ORG)
    return ids.pop() if len(ids) == 1 else None


def adopt_default_org(name, domain=None, invite=None, con=None):
    """Give the built-in `default` corpus an org row. Moves no data, and the id stays `default` so
    already-minted tokens and sign-in routing keep pointing at the same file.

    `invite` defaults to None on purpose: verify_invite matches a stored invite_hash before anything
    else, so seeding it from CSA_AUTH_TOKEN would make that secret a permanent mint credential.
    Adopting twice raises rather than rewriting a live row."""
    con = con or control_db()
    if not (name or "").strip():
        raise ValueError("adopt_default_org requires a name — it becomes the workspace's display name")
    if default_is_adopted(con):
        raise ValueError(f"the {store.DEFAULT_ORG!r} org already has a row (use set_domain/rotate_invite "
                         f"to change it)")
    if domain:                                           # validate BEFORE the insert: adopting twice is refused
        _validate_domain(domain, store.DEFAULT_ORG, con)
    db.run(con, insert(_ORG).values(id=store.DEFAULT_ORG, name=name.strip(),
                                    invite_hash=_hash(invite) if invite else None, created=_now()))
    _audit(con, "adopt_default_org", store.DEFAULT_ORG, name.strip())
    con.commit()
    if domain:
        set_domain(store.DEFAULT_ORG, domain, con=con)
    return store.DEFAULT_ORG


def create_org(name, org_id=None, domain=None, con=None):
    """Provision an org: (org_id, invite). The first one makes the deployment multi-tenant, which
    demotes the shared secret to the control plane and closes the env invite (see verify_invite)."""
    return _create_org(name, org_id=org_id, domain=domain, con=con)


def _create_org(name, org_id=None, domain=None, con=None):
    """Create an org and mint its first invite -> (org_id, invite); the invite is shown once. An existing
    id raises. `domain` binds the sign-in domain (set_domain)."""
    con = con or control_db()
    oid = store.valid_org_id(org_id) if org_id else slug(name)
    if oid == store.DEFAULT_ORG:
        raise ValueError("'default' is the built-in self-host org and is never created via this path")
    if db.run(con, select(literal(1)).select_from(_ORG).where(_ORG.c.id == oid)).fetchone():
        raise ValueError(f"org {oid!r} already exists (use rotate_invite to issue a new invite)")
    invite = INVITE_PREFIX + secrets.token_urlsafe(32)
    db.run(con, insert(_ORG).values(id=oid, name=name or oid, invite_hash=_hash(invite), created=_now()))
    _audit(con, "create_org", oid, name or "")
    con.commit()
    if domain:
        set_domain(oid, domain, con=con)                 # validates + refuses a domain another org owns
    return oid, invite


def rotate_invite(org_id, con=None):
    """New invite for an org; the old one stops working at once. Minted user tokens are unaffected."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    if not db.run(con, select(literal(1)).select_from(_ORG).where(_ORG.c.id == oid)).fetchone():
        raise ValueError(f"no such org {oid!r}")
    invite = INVITE_PREFIX + secrets.token_urlsafe(32)
    db.run(con, update(_ORG).values(invite_hash=_hash(invite)).where(_ORG.c.id == oid))
    _audit(con, "rotate_invite", oid)
    con.commit()
    return invite


def verify_invite(invite, con=None):
    """The org invite presented at /cli-auth -> org_id, or None to deny; the gate on minting, so
    fail-closed and never raising:

      1. a provisioned org's invite                     -> that org
      2. CSA_ORG_INVITE / CSA_AUTH_TOKEN, while the box hosts no tenants -> `default`
      3. nothing configured and auth mode "off" (loopback only) -> `default`
      4. else deny."""
    try:
        presented = (invite or "").strip()
        con = con or control_db()
        if presented:                                    # (1) a real org invite always wins
            row = db.run(con, select(_ORG.c.id).where(_ORG.c.invite_hash == _hash(presented))).fetchone()
            if row:
                return row[0]
        from analyzer import auth                        # late: auth imports orgs (would be a cycle at module level)
        mode = auth.current_mode()
        expected = (os.environ.get("CSA_ORG_INVITE", "") or os.environ.get("CSA_AUTH_TOKEN", "")).strip()
        if not expected:                                 # (3) nothing configured
            if mode == "off":
                return store.DEFAULT_ORG
            log.warning("cli-auth: no org invite and no CSA_ORG_INVITE/CSA_AUTH_TOKEN in mode=%s — denying", mode)
            return None
        if is_multi_tenant(con):                         # (2, disabled) this box hosts tenants
            log.warning("cli-auth: the env invite is disabled on a multi-tenant deployment")
            return None
        if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
            return store.DEFAULT_ORG                     # (2) self-host env invite
        return None                                      # (4)
    except Exception as e:                               # a thrown exception must read as deny
        log.warning("verify_invite raised: %s", str(e)[:120])
        return None


def sso_allowlist_configured(con=None):
    """Does any org claim a domain? auth.boot_error refuses to start with an sso row and none, since
    every sign-in would then be denied."""
    try:
        con = con or control_db()
        return db.run(con, select(literal(1)).select_from(_ORG)
                      .where(_ORG.c.domain.is_not(None), _ORG.c.domain != "").limit(1)).fetchone() is not None
    except Exception:
        return False


# --- the OpenID issuer (control.db `sso`; analyzer/auth.py reads it, analyzer/sso.py runs the flow) --


def valid_issuer(url):
    """The issuer URL as the provider names it, raising on anything the discovery fetch must not be
    pointed at. https, except a loopback host, which may be http for a stub on the box itself. Kept
    byte for byte, trailing slash included: `iss` is an exact string compare."""
    from analyzer import auth                            # late: auth imports orgs
    url = (url or "").strip()
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    secure = u.scheme == "https" or (u.scheme == "http" and auth.is_loopback(host))
    if not host or not secure or u.username or u.password or u.query or u.fragment:
        raise ValueError(f"{url!r} is not an issuer URL (https://host[/path], no query or fragment)")
    return url


def set_sso(issuer, client_id, client_secret, display_name, updated_by="", con=None):
    """Configure the issuer; the row is replaced whole. The secret is stored as given: the token
    endpoint needs it in the clear (see tenant.Sso)."""
    con = con or control_db()
    issuer = valid_issuer(issuer)
    if not (client_id or "").strip():
        raise ValueError("sso set requires the client id the identity provider issued")
    if not (display_name or "").strip():
        raise ValueError("sso set requires a display name (what the sign-in button says)")
    db.run(con, delete(_SSO))
    db.run(con, insert(_SSO).values(id=1, issuer=issuer, client_id=client_id.strip(),
                                    client_secret=(client_secret or "").strip() or None,
                                    display_name=display_name.strip(), updated_at=_now(),
                                    updated_by=(updated_by or "").strip() or None))
    _audit(con, "sso_set", None, f"{issuer} client_id={client_id.strip()} "
                                 f"secret={'stored' if (client_secret or '').strip() else 'none'}")
    con.commit()
    return issuer


def clear_sso(con=None):
    """Remove the issuer -> whether there was one. Sign-in falls back to the invite paths."""
    con = con or control_db()
    n = db.run(con, delete(_SSO)).rowcount
    if n:
        _audit(con, "sso_clear", None)
        con.commit()
    return n > 0


# --- per-person invite links ------------------------------------------------------------------------
# Narrower than the workspace invite above: a link names one address, expires, and is revocable.


def _live_invite(now=None):
    """The WHERE that matches a link that may still be redeemed."""
    now = now or _now()
    return (_INV.c.revoked_at.is_(None), _INV.c.expires_at > now)


def create_invite(org_id, email, created_by, ttl_days=INVITE_TTL_DAYS, con=None):
    """Issue a link for one person -> (invite_id, token); only the token's hash is stored. Replaces that
    address's live link. Raises ValueError on a bad address, a current member, or MAX_PENDING_INVITES."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    who = (email or "").strip().lower()
    if "@" not in who or len(who) > 254:
        raise ValueError("an invite names one email address")
    try:
        days = max(1, min(int(ttl_days), 90))
    except (TypeError, ValueError):
        days = INVITE_TTL_DAYS
    if any(t["active"] and (t["email"] or "").lower() == who for t in list_tokens(oid, con=con)):
        raise ValueError(f"{who} is already in this workspace.")
    token = JOIN_PREFIX + secrets.token_urlsafe(32)
    invite_id = secrets.token_hex(8)
    # One transaction with the revoke first: the writer lock serializes two creates for one address.
    revoked = db.run(con, update(_INV).values(revoked_at=_now())
                     .where(_INV.c.org_id == oid, func.lower(_INV.c.email) == who,
                            _INV.c.used_at.is_(None), *_live_invite())).rowcount
    pending = db.run(con, select(func.count()).select_from(_INV)
                     .where(_INV.c.org_id == oid, _INV.c.used_at.is_(None), *_live_invite())).fetchone()[0]
    if pending >= MAX_PENDING_INVITES:
        con.rollback()                                   # the revoke above goes with it
        raise ValueError(f"this workspace already has {pending} unused invite links. "
                         f"Revoke some before making another.")
    db.run(con, insert(_INV).values(id=invite_id, org_id=oid, email=who, token_hash=_hash(token),
                                    created_by=(created_by or "").strip().lower() or None,
                                    created_at=_now(), expires_at=_now(days * 86400),
                                    used_at=None, revoked_at=None))
    if revoked:
        _audit(con, "invite_revoke", oid, f"{created_by or '-'} -> {who} (replaced)")
    _audit(con, "invite_new", oid, f"{created_by or '-'} -> {who} ({days}d)")
    con.commit()
    return invite_id, token


def redeem_invite(token, email, con=None):
    """A link plus the address it names -> the org to mint into, or None to deny. The plaintext face
    of redeem_invite_hash, for the cookie a sign-in POST carries."""
    return redeem_invite_hash(_hash(token), email, con=con) if token else None


def redeem_invite_hash(token_hash, email, con=None):
    """A link's sha256 plus the address it names -> the org to mint into, or None to deny; never raises.
    `email` must already be verified by the caller. Every failure is the same None. Stamps `used_at` on
    the first redemption, without gating later ones (the same person's second machine)."""
    try:
        who = (email or "").strip().lower()
        if not who or not token_hash:
            return None
        con = con or control_db()
        row = db.run(con, select(_INV.c.id, _INV.c.org_id, _INV.c.email, _INV.c.used_at)
                     .where(_INV.c.token_hash == token_hash, *_live_invite())).fetchone()
        if not row or (row[2] or "") != who:
            return None
        invite_id, oid, _who, used_at = row
        if not used_at:
            db.run(con, update(_INV).values(used_at=_now())
                   .where(_INV.c.id == invite_id, _INV.c.used_at.is_(None)))
            _audit(con, "invite_used", oid, who)
            con.commit()
        return oid
    except Exception as e:                               # a thrown exception must read as deny
        log.warning("redeem_invite raised: %s", str(e)[:120])
        return None


def claim_invite(token_hash, con=None):
    """Spend a live link that nobody has redeemed yet -> True for the one caller that did. Atomic, so a
    link can confer a one-time privilege only once."""
    con = con or control_db()
    won = db.run(con, update(_INV).values(used_at=_now())
                 .where(_INV.c.token_hash == (token_hash or ""), _INV.c.used_at.is_(None), *_live_invite())).rowcount
    con.commit()
    return bool(won)


def invite_for(token, con=None):
    """{"email", "org_id"} for a live link, or None; never raises."""
    return invite_for_hash(_hash(token), con=con) if token else None


def invite_for_hash(token_hash, con=None):
    try:
        con = con or control_db()
        row = db.run(con, select(_INV.c.email, _INV.c.org_id)
                     .where(_INV.c.token_hash == (token_hash or ""), *_live_invite())).fetchone()
        return {"email": row[0], "org_id": row[1]} if row else None
    except Exception as e:
        log.warning("invite_for raised: %s", str(e)[:120])
        return None


def list_invites(org_id, con=None):
    """This workspace's live links, newest first. Never the token — only its hash was stored."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    rows = db.run(con, select(_INV.c.id, _INV.c.email, _INV.c.created_by, _INV.c.created_at,
                              _INV.c.expires_at, _INV.c.used_at)
                  .where(_INV.c.org_id == oid, *_live_invite())
                  .order_by(_INV.c.created_at.desc()))
    return [{"id": i, "email": e, "created_by": cb, "created_at": ca, "expires_at": x, "used_at": u}
            for i, e, cb, ca, x, u in rows]


def revoke_invite(invite_id, org_id, actor="", con=None):
    """Kill one link -> was there one to kill. Org-scoped, so another tenant's id answers exactly like
    a miss, and so does a second revoke of the same link."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    row = db.run(con, select(_INV.c.email).where(_INV.c.id == (invite_id or ""),
                                                 _INV.c.org_id == oid,
                                                 _INV.c.revoked_at.is_(None))).fetchone()
    if not row:
        return False
    db.run(con, update(_INV).values(revoked_at=_now())
           .where(_INV.c.id == invite_id, _INV.c.revoked_at.is_(None)))
    _audit(con, "invite_revoke", oid, f"{actor or '-'} -> {row[0]}")
    con.commit()
    return True


def org_for_identity(email, domain_claim, con=None):
    """A verified identity -> the org whose `domain` equals its domain claim, or None to deny; never
    raises. The claim comes from the ID token (Google's `hd`), never the address's domain: a consumer
    account can hold a verified address at any domain."""
    try:
        email = (email or "").strip().lower()
        claim = (domain_claim or "").strip().lower()
        if not email or not claim:
            return None
        con = con or control_db()
        row = db.run(con, select(_ORG.c.id).where(func.lower(_ORG.c.domain) == claim)).fetchone()
        if row:
            return row[0]
        log.info("sign-in denied for %s (domain=%s): no org claims that domain", email, claim)
        return None
    except Exception as e:                               # a thrown exception must read as deny
        log.warning("org_for_identity raised: %s", str(e)[:120])
        return None


def domain_of(org_id, con=None):
    """The email domain an org admits at the typed sign-in, or None when it names none."""
    con = con or control_db()
    row = db.run(con, select(_ORG.c.domain).where(_ORG.c.id == store.valid_org_id(org_id))).fetchone()
    return (row[0] or "").lower() or None if row else None


def _validate_domain(domain, oid, con, require_org=False):
    """Normalized domain (or None to unbind), raising on anything unusable."""
    d = (domain or "").strip().lower().lstrip("@") or None
    if d and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d):
        raise ValueError(f"{domain!r} is not a domain name (expected e.g. acme.com)")
    if require_org and not db.run(con, select(literal(1)).select_from(_ORG)
                                  .where(_ORG.c.id == oid)).fetchone():
        raise ValueError(f"no such org {oid!r}")
    if d:
        other = db.run(con, select(_ORG.c.id)
                       .where(func.lower(_ORG.c.domain) == d, _ORG.c.id != oid)).fetchone()
        if other:
            raise ValueError(f"domain {d!r} is already bound to org {other[0]!r}")
    return d


def set_domain(org_id, domain, con=None):
    """Bind an org's sign-in domain (org_for_identity); blank unbinds. One org per domain."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    d = _validate_domain(domain, oid, con, require_org=True)
    db.run(con, update(_ORG).values(domain=d).where(_ORG.c.id == oid))
    _audit(con, "set_domain", oid, d or "(cleared)")
    con.commit()
    return d


def set_name(org_id, name, con=None):
    """The workspace's display name."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    if not (name or "").strip():
        raise ValueError("a workspace needs a name")
    db.run(con, update(_ORG).values(name=name.strip()).where(_ORG.c.id == oid))
    _audit(con, "set_name", oid, name.strip())
    con.commit()


def list_orgs(con=None):
    con = con or control_db()
    return [{"id": i, "name": n, "created": c, "domain": d,
             "tokens": db.run(con, select(func.count()).select_from(_UT)
                              .where(_UT.c.org_id == i, _UT.c.revoked_at.is_(None))).fetchone()[0]}
            for i, n, c, d in db.run(con, select(_ORG.c.id, _ORG.c.name, _ORG.c.created, _ORG.c.domain)
                                     .order_by(_ORG.c.created))]


def audit_log(org_id=None, limit=100, con=None):
    """Admin action history, newest first, for one org or (None) all. Reads are not logged."""
    con = con or control_db()
    limit = max(1, min(int(limit), 1000))
    stmt = select(_AL.c.at, _AL.c.action, _AL.c.org_id, _AL.c.detail)
    if org_id is not None:
        stmt = stmt.where(_AL.c.org_id == store.valid_org_id(org_id))
    # rowid, not `at`: same-second actions still list newest first
    stmt = stmt.order_by(column("rowid").desc()).limit(limit)
    return [{"at": a, "action": act, "org_id": o, "detail": d}
            for a, act, o, d in db.run(con, stmt)]


def delete_org(org_id, con=None):
    """Right-to-delete for a whole org: its data directory, tokens and org row. The admin_log survives,
    recording the deletion. Raises on an id with nothing to delete, so a typo never reports success."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    if oid == store.DEFAULT_ORG:                        # data/ is the whole self-host install
        raise ValueError("refusing to delete the 'default' org (that is the self-host corpus itself)")
    d = store.org_dir(oid)
    has_dir = os.path.isdir(d)
    has_row = db.run(con, select(literal(1)).select_from(_ORG)
                     .where(_ORG.c.id == oid)).fetchone() is not None
    has_tokens = db.run(con, select(literal(1)).select_from(_UT)
                        .where(_UT.c.org_id == oid).limit(1)).fetchone() is not None
    if not (has_dir or has_row or has_tokens):
        raise ValueError(f"no such org {oid!r} — nothing deleted (check the id; deletion is not idempotent-on-absent)")
    removed = has_dir
    if removed:
        import shutil
        shutil.rmtree(d)
    n = db.run(con, select(func.count()).select_from(_UT).where(_UT.c.org_id == oid)).fetchone()[0]
    db.run(con, delete(_UT).where(_UT.c.org_id == oid))
    db.run(con, delete(_STALL).where(_STALL.c.org_id == oid))
    db.run(con, delete(_ORG).where(_ORG.c.id == oid))
    _audit(con, "delete_org", oid, f"tokens={n} corpus={'removed' if removed else 'absent'}")
    con.commit()
    return {"org_id": oid, "corpus_removed": removed, "tokens_deleted": n}


# --- workspace requests (landing page "Request a workspace" -> operator approval) -------------------

# C0, DEL, C1, bidi overrides and isolates, line separators, zero-width and BOM: what a terminal mishandles.
_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def _clean_text(s, cap):
    """An anonymous string made safe to print: _CTRL_RE stripped, whitespace collapsed, capped."""
    s = _CTRL_RE.sub(" ", s or "")
    return " ".join(s.split())[:cap]


def request_workspace(company, email, con=None):
    """Store a landing-page request; ValueError on an invalid field. A repeat is a silent no-op, and no
    audit row is written (anonymous POSTs must not grow the audit log).

    Returns the stored {'id', 'company', 'email'} for a new request, None for a duplicate. Server-side
    only: the HTTP answer must not differ between the two."""
    if company is None:
        company = ""
    if email is None:
        email = ""
    if not isinstance(company, str) or not isinstance(email, str):
        raise ValueError("company and email must be text")
    company = _clean_text(company, 200)
    email = _clean_text(email, 4096)
    if not company:
        raise ValueError("company is required")
    if "@" not in email or len(email) > 254:             # the mint gate's rule; never truncate an address
        raise ValueError("a valid email is required")
    con = con or control_db()
    rid = secrets.token_hex(4)                           # typeable in `org approve <id>`
    n = db.run(con, insert(_WR).prefix_with("OR IGNORE").values(
        id=rid, company=company, email=email,
        company_norm=" ".join(company.split()).lower(), email_norm=email.strip().lower(),
        created=_now(), status="pending", handled_at=None, org_id=None)).rowcount
    con.commit()
    return {"id": rid, "company": company, "email": email} if n == 1 else None


def list_requests(status="pending", con=None, limit=None):
    """Workspace requests, newest first, matching `status` (None = all), at most `limit`
    (count_requests says how many more)."""
    con = con or control_db()
    stmt = select(_WR.c.id, _WR.c.company, _WR.c.email, _WR.c.created, _WR.c.status,
                  _WR.c.handled_at, _WR.c.org_id)
    if status is not None:
        stmt = stmt.where(_WR.c.status == status)
    stmt = stmt.order_by(_WR.c.created.desc(), _WR.c.id)   # newest first; id breaks same-second ties
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return [{"id": i, "company": c, "email": e, "created": cr, "status": s,
             "handled_at": h, "org_id": o}
            for i, c, e, cr, s, h, o in db.run(con, stmt)]


def count_requests(status="pending", con=None):
    """How many requests match `status` (None = all)."""
    con = con or control_db()
    stmt = select(func.count()).select_from(_WR)
    if status is not None:
        stmt = stmt.where(_WR.c.status == status)
    return db.run(con, stmt).fetchone()[0]


def approve_request(request_id, domain=None, org_id=None, con=None):
    """A pending request -> an org and its one-time invite:
    {"request", "org_id", "invite", "domain", "domain_error"}.

    The approved mark, audit row and org row commit together; a failed create leaves the request
    pending. Approving twice raises. A refused domain is reported in `domain_error`, not raised,
    since raising would lose the invite."""
    con = con or control_db()
    rid = (request_id or "").strip()
    row = db.run(con, select(_WR.c.company, _WR.c.email, _WR.c.status, _WR.c.handled_at,
                             _WR.c.org_id).where(_WR.c.id == rid)).fetchone()
    if not row:
        raise ValueError(f"no such request {rid!r} (see `csa.py org requests`)")
    company, email, status, handled_at, existing_org = row
    if status == "approved":
        raise ValueError(f"request {rid} was already approved into org {existing_org!r} at {handled_at}. "
                         f"The invite was shown once then; issue a fresh one with "
                         f"`csa.py org invite {existing_org}`.")
    oid = store.valid_org_id(org_id) if org_id else slug(company)
    db.run(con, update(_WR).values(status="approved", handled_at=_now(), org_id=oid)
           .where(_WR.c.id == rid))
    _audit(con, "approve_request", oid, f"req={rid} {company} <{email}>")
    try:
        oid, invite = create_org(company, org_id=oid, con=con)   # commits mark+audit+org atomically
    except ValueError as e:
        con.rollback()                                   # the request stays pending
        if "already exists" in str(e):
            raise ValueError(f"org id {oid!r} already exists — another company may slug to the same id. "
                             f"Re-run with an explicit id: `csa.py org approve {rid} --id <unique-id>`") from e
        raise
    bound_domain, domain_err = None, None
    if domain:
        try:
            bound_domain = set_domain(oid, domain, con=con)
        except ValueError as e:
            domain_err = str(e)
    return {"request": {"id": rid, "company": company, "email": email},
            "org_id": oid, "invite": invite,
            "domain": bound_domain, "domain_error": domain_err}


# --- per-user tokens --------------------------------------------------------------------------------

ROLES = ("admin", "member")


class LastAdminError(ValueError):
    """The change would leave the workspace with nobody who can administer it."""


def _role_of(email, org_id, con):
    """This person's role in this org from their ACTIVE tokens ('admin' wins), or None with none."""
    who = (email or "").strip().lower()
    roles = {t["role"] for t in list_tokens(org_id, con=con)
             if (t["email"] or "").lower() == who and t["active"]}
    return "admin" if "admin" in roles else ("member" if roles else None)


def set_role(email, org_id, role, con=None):
    """Promote or demote a PERSON inside one org, across every ACTIVE token they hold (the role belongs
    to the person, not the laptop). Returns the number of tokens updated, 0 when none is live. Refuses
    to demote the last admin who can still change anything."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r} — 'admin' or 'member'")
    who = (email or "").strip().lower()
    if not who:
        raise ValueError("set_role requires the member's email")
    tokens = list_tokens(oid, con=con)
    # Never a typed-email sign-in (`web`, an unverified self-assertion) or a service token (a stolen
    # session could mint one and promote it into a lasting admin credential).
    mine = [t for t in tokens if (t["email"] or "").lower() == who and t["active"] and t["hostname"] != "web"
            and not t["service"]]
    if not mine:
        return 0
    if role == "member" and any(is_mutating_admin(t) for t in mine) \
            and not any(is_mutating_admin(t) and (t["email"] or "").lower() != who for t in tokens):
        raise LastAdminError(f"{who} is the only admin of {oid!r} who can change anything. Promote "
                             f"someone else first.")
    db.run(con, update(_UT).values(role=role)
           .where(_UT.c.token_hash.in_([t["token_hash"] for t in mine])))
    _audit(con, "set_role", oid, f"{who} -> {role}")
    con.commit()
    return len(mine)


def resolve_token(ident, org_id, con=None):
    """The row `revoke(ident)` would act on within one org (plaintext, full hash or unique prefix), or
    None. The last-admin guard resolves its target exactly as revoke will."""
    con = con or control_db()
    rows = list_tokens(org_id, con=con)
    ident = (ident or "").strip()
    if not ident:
        return None
    for th in (_hash(ident), ident.lower()):             # plaintext first, then a full hash
        for r in rows:
            if r["token_hash"] == th:
                return r
    pref = ident.lower()
    if pref and all(c in "0123456789abcdef" for c in pref):
        hits = [r for r in rows if r["token_hash"].startswith(pref)]
        if len(hits) == 1:
            return hits[0]
    return None


def has_invite(org_id, con=None):
    """Does this org have an invite? A bool, never the hash."""
    con = con or control_db()
    row = db.run(con, select(_ORG.c.invite_hash).where(_ORG.c.id == store.valid_org_id(org_id))).fetchone()
    return bool(row and row[0])


BROWSER_HOSTNAMES = ("web", "web-sso")                  # what cliauth.web_login records for a dashboard sign-in


def is_mutating_admin(t):
    """An active admin token whose holder can change the workspace: a bearer, or a verified (SSO)
    dashboard session. A typed-email dashboard sign-in (`web`) reads as whatever role it holds but
    never mutates."""
    return t["role"] == "admin" and t["active"] and (t["hostname"] or "") != "web"


def admin_count(org_id, con=None, mutating_only=False):
    """Live workspace admins in an org; `mutating_only` counts only those who can change anything."""
    return sum(1 for t in list_tokens(org_id, con=con or control_db())
               if (is_mutating_admin(t) if mutating_only else t["role"] == "admin" and t["active"]))


def mint_token(email, org_id, hostname="", con=None, role=None, verified=False, service=False):
    """Mint a per-user token and return the plaintext once; only its hash is stored. It carries an idle
    expiry from birth (_ttl_seconds)."""
    con = con or control_db()
    oid = store.valid_org_id(org_id)
    if oid != store.DEFAULT_ORG and not db.run(con, select(literal(1)).select_from(_ORG)
                                               .where(_ORG.c.id == oid)).fetchone():
        raise ValueError(f"no such org {oid!r} — create it before minting tokens into it")
    if not (email or "").strip():
        raise ValueError("mint_token requires an email — it IS the verified identity")
    # role=None inherits the person's existing role, but only for a VERIFIED identity: a typed email
    # (no SSO) could name the admin's address and mint itself admin, so unverified mints are members.
    if role is None:
        role = (_role_of(email, oid, con) or "member") if verified else "member"
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r} — 'admin' or 'member'")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    ttl = _ttl_seconds(oid)                              # cross-plane read (org db) before the control write
    # every column bound: the bridge applies no model defaults
    db.run(con, insert(_UT).values(
        token_hash=_hash(token), email=email.strip(), org_id=oid,
        hostname=(hostname or "").strip()[:80], created=_now(),
        revoked_at=None, last_used_at=None, expires_at=_now(ttl) if ttl else None, role=role,
        service=1 if service else None))
    _audit(con, "mint_token", oid, f"{email.strip()} @ {(hostname or '-')[:40]}")
    con.commit()
    return token


def mint_service_token(org_id, email, name, con=None):
    """A token for a machine that cannot sign in: an agent harness, a pipeline, a CI box. Everything
    it ships or asks is `email`'s, a teammate's included. Always a member: set_role never touches it."""
    email, name = (email or "").strip().lower(), (name or "").strip()
    if "@" not in email or len(email) > 254:
        raise ValueError(f"{email!r} is not an email address")
    if not name or name.lower() in BROWSER_HOSTNAMES or len(name) > 80:
        raise ValueError(f"{name!r} is not a machine name")
    return mint_token(email, org_id, hostname=name, con=con, role="member", service=True)


def _touch(con, token_hash, last_used_at, org_id=None):
    """Record use and slide the expiry forward, at most hourly; never fatal. The slide is the only
    refresh: if it stops firing, every token expires one TTL after it was minted."""
    try:
        now = time.time()
        if last_used_at and now - _epoch(last_used_at) < _LAST_USED_EVERY:
            return
        ttl = _ttl_seconds(org_id)
        if ttl:
            db.run(con, update(_UT).values(last_used_at=_now(), expires_at=_now(ttl))
                   .where(_UT.c.token_hash == token_hash))
        else:                                           # expiry disabled: don't resurrect a dated token
            db.run(con, update(_UT).values(last_used_at=_now()).where(_UT.c.token_hash == token_hash))
        con.commit()
    except Exception:                                   # audit metadata is never worth a 500
        pass


def lookup(token, con=None):
    """(status, principal), the gate's primitive; never raises. status: 'ok' | 'revoked' | 'expired' |
    None (not a user token). The caller denies 'revoked' and 'expired' outright, never falling through
    to the shared-secret comparison."""
    try:
        if not token or not token.startswith(TOKEN_PREFIX):
            return (None, None)                         # cheap reject; the prefix is ergonomics, not security
        con = con or control_db()
        th = _hash(token)
        row = db.run(con, select(_UT.c.email, _UT.c.org_id, _UT.c.revoked_at, _UT.c.last_used_at,
                                 _UT.c.expires_at, _UT.c.role, _UT.c.service).where(_UT.c.token_hash == th)).fetchone()
        if not row:
            return (None, None)
        email, org_id, revoked_at, last_used, expires_at, role, service = row
        if revoked_at:
            return ("revoked", None)
        if expires_at and _now() > expires_at:
            return ("expired", None)
        store.valid_org_id(org_id)                      # a bad id in the db must deny, not path-escape
        _touch(con, th, last_used, org_id=org_id)
        return ("ok", {"email": email, "org_id": org_id,     # NULL or junk reads as member
                       "role": role if role in ROLES else "member", "service": bool(service)})
    except Exception as e:                              # a thrown exception must read as deny
        log.warning("token lookup raised: %s", str(e)[:120])
        return (None, None)


def verify(token, con=None):
    """token -> {"email", "org_id"} or None to deny; never raises. The role comes from lookup()."""
    status, principal = lookup(token, con=con)
    if status != "ok":
        return None
    return {"email": principal["email"], "org_id": principal["org_id"]}


def machine(token, con=None):
    """Which machine presented this token: {"token_hash", "email", "org_id", "hostname"}, or None for
    anything lookup() denies."""
    con = con or control_db()                            # one connection for both reads: a hot path
    status, principal = lookup(token, con=con)
    if status != "ok" or not principal:
        return None
    th = _hash(token)
    row = db.run(con, select(_UT.c.hostname).where(_UT.c.token_hash == th)).fetchone()
    return {"token_hash": th, "email": principal["email"], "org_id": principal["org_id"],
            "hostname": (row[0] if row else "") or ""}


def revoke(token_or_hash, con=None, org_id=None):
    """Revoke by plaintext token, full token_hash, or the hash prefix `csa.py org tokens` prints.
    Idempotent (the first timestamp stays). Raises ValueError on an ambiguous prefix; False on no
    match. `org_id` scopes it to one tenant, where another org's hash answers like a miss."""
    con = con or control_db()
    ident = (token_or_hash or "").strip()
    scope = [_UT.c.org_id == store.valid_org_id(org_id)] if org_id is not None else []
    # exact matches first: plaintext token, or a full 64-char hash
    for th in (_hash(ident), ident.lower()):
        row = db.run(con, select(_UT.c.org_id, _UT.c.revoked_at)
                     .where(_UT.c.token_hash == th, *scope)).fetchone()
        if row:
            if not row[1]:                              # already revoked: no second timestamp or audit row
                db.run(con, update(_UT).values(revoked_at=_now())
                       .where(_UT.c.token_hash == th))
                _audit(con, "revoke", row[0], th[:12])
                con.commit()
            return True
    # else a hash prefix, never guessing between two tokens
    pref = ident.lower()
    if pref and all(c in "0123456789abcdef" for c in pref):
        rows = db.run(con, select(_UT.c.token_hash, _UT.c.org_id, _UT.c.revoked_at)
                      .where(_UT.c.token_hash.like(pref + "%"), *scope)).fetchall()
        if len(rows) > 1:
            raise ValueError(f"{pref!r} matches {len(rows)} tokens — use more characters of the hash")
        if len(rows) == 1:
            th, oid, revoked_at = rows[0]
            if not revoked_at:
                db.run(con, update(_UT).values(revoked_at=_now()).where(_UT.c.token_hash == th))
                _audit(con, "revoke", oid, th[:12])
                con.commit()
            return True
    return False


def list_tokens(org_id=None, con=None):
    """Every token of one org (None = all), newest first; hashes only."""
    con = con or control_db()
    stmt = select(_UT.c.token_hash, _UT.c.email, _UT.c.org_id, _UT.c.hostname, _UT.c.created,
                  _UT.c.revoked_at, _UT.c.last_used_at, _UT.c.expires_at, _UT.c.role, _UT.c.service)
    if org_id is not None:
        stmt = stmt.where(_UT.c.org_id == store.valid_org_id(org_id))
    now = _now()
    return [{"token_hash": th, "email": e, "org_id": o, "hostname": h, "created": c,
             "revoked_at": r, "last_used_at": lu, "expires_at": x,
             "role": ro if ro in ROLES else "member",
             "active": r is None and not (x and now > x), "service": bool(sv)}
            for th, e, o, h, c, r, lu, x, ro, sv in db.run(con, stmt.order_by(_UT.c.created.desc()))]


def agent_emails(tokens):
    """The lowercased addresses in `tokens` (list_tokens rows) that only ever held a service token: agents,
    which every screen counts apart from people."""
    service = {(t["email"] or "").lower() for t in tokens if t["service"]}
    return service - {(t["email"] or "").lower() for t in tokens if not t["service"]}


def org_count(con=None):
    """Number of non-default orgs, 0 when the control plane is unreadable."""
    try:
        con = con or control_db()
        return db.run(con, select(func.count()).select_from(_ORG)
                      .where(_ORG.c.id != store.DEFAULT_ORG)).fetchone()[0]
    except Exception:
        return 0
