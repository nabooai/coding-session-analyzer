"""Fleet directives: the server telling a tenant's csa-clients what to do. A directive is one admin
instruction, `{who, since}` (ship everything you have from <date>), served to a matching machine on its
next SessionStart and acked back with what happened.

1. The credential names the machine: `email` and `hostname` come from the presented token's row
   (orgs.machine), never from the request.
2. One-shot per machine: `for_client` hides a directive the token has already acked.
3. An ack can say no: a client whose owner declined backfill records `declined`.

Control plane: statements run through db.run on orgs.control_db(), never db.engine_for.
"""
import logging, re, secrets
from datetime import datetime, timezone

from sqlalchemy import and_, delete, exists, func, insert, literal, or_, select, update

from analyzer import db, orgs, store
from analyzer.tenant import ClientDirective, ClientDirectiveAck

_CD, _CA = ClientDirective.__table__, ClientDirectiveAck.__table__

log = logging.getLogger("csa.fleet")

STATUSES = ("applied", "declined", "error")
_MAX_DETAIL = 200                                # an ack detail is a log line, not a payload channel


def _now():
    return orgs._now()                           # one clock for the whole control plane


def new_id():
    """Short, unguessable, and safe in a URL/JSON — the id the client echoes back in its ack."""
    return secrets.token_hex(8)


def norm_since(since):
    """'' (all history) or a normalized RFC3339 UTC instant. Raises ValueError on anything else.

    Validated HERE, at creation, rather than trusted at serve time: the client parses this with
    Go's time.Parse(time.RFC3339) and a value it cannot read means it ships NOTHING (client
    ship.go treats an unreadable floor as "sweep nothing" — deliberately, so a corrupt stamp is
    never read as 'send everything'). A typo in the console must fail in the console."""
    s = (since or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):            # a plain date means the start of that day
        s += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"since: expected a date (2026-08-19) or an ISO instant, got {since!r}")
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if dt > datetime.now(timezone.utc):
        raise ValueError("since is in the future — that directive would fetch nothing")
    return dt.astimezone(timezone.utc).isoformat()


def _norm(s):
    """Targets and identities are compared case-insensitively: an email is case-insensitive by
    RFC, and a hostname the OS reports as 'Sams-MacBook' must match a target typed in lowercase."""
    return (s or "").strip().lower()


def create_directive(org_id, since="", target_email="", target_hostname="", created_by="", con=None):
    """Issue one directive. Returns the stored row as a dict.

    A hostname without an email is refused: hostnames are not unique across people (two 'MacBook-Pro's
    in one company is the normal case), so that shape would silently fan out to machines the operator
    did not mean to name."""
    org_id = store.valid_org_id(org_id)
    email, host = _norm(target_email), _norm(target_hostname)
    if host and not email:
        raise ValueError("a hostname needs an email too — hostnames are not unique across people; "
                         "target the person, optionally narrowed to one of their machines")
    row = {"id": new_id(), "org_id": org_id, "target_email": email, "target_hostname": host,
           "since": norm_since(since), "created": _now(), "created_by": (created_by or "")[:120],
           "revoked_at": None}
    con = con or orgs.control_db()
    db.run(con, insert(_CD).values(**row))
    orgs._audit(con, "fleet_request", org_id,                       # same audit trail as every admin action
                f"{email or '*'}/{host or '*'} since={row['since'] or 'all'}")
    con.commit()                                                  # mutation + audit as ONE unit (orgs._audit contract)
    return row


def revoke_directive(directive_id, con=None):
    """Cancel a directive that has not been acted on yet. Idempotent; returns True if it was live.

    Keeps the original `revoked_at` on a second call, and leaves acks alone: a machine that already
    complied did really ship those sessions, and erasing that record would make the console lie."""
    con = con or orgs.control_db()
    row = db.run(con, select(_CD.c.org_id, _CD.c.revoked_at).where(_CD.c.id == directive_id)).fetchone()
    if not row:
        return False
    if row[1]:
        return False                                              # already revoked — no second audit row
    db.run(con, update(_CD).values(revoked_at=_now()).where(_CD.c.id == directive_id))
    orgs._audit(con, "fleet_cancel", row[0], directive_id)
    con.commit()
    return True


def for_client(machine, con=None):
    """The directives THIS machine should act on now, oldest first.

    `machine` is orgs.machine(token) — the token's own row. Nothing here is read from the request
    body (rule 1): a client cannot name a target, only present a credential. Returns [] for an
    unknown machine rather than raising, because the caller is a hot request path that must answer
    'nothing to do' rather than 500 when a token was revoked mid-flight."""
    if not machine or not machine.get("org_id"):
        return []
    con = con or orgs.control_db()
    email, host, th = _norm(machine.get("email")), _norm(machine.get("hostname")), machine.get("token_hash") or ""
    already = exists(select(literal(1)).select_from(_CA)
                     .where(and_(_CA.c.directive_id == _CD.c.id, _CA.c.token_hash == th)))
    rows = db.run(con, select(_CD.c.id, _CD.c.since, _CD.c.created)
                  .where(and_(_CD.c.org_id == machine["org_id"],
                              _CD.c.revoked_at.is_(None),
                              # '' targets everyone; otherwise it must name this machine exactly.
                              or_(_CD.c.target_email == "", _CD.c.target_email.is_(None),
                                  func.lower(_CD.c.target_email) == email),
                              or_(_CD.c.target_hostname == "", _CD.c.target_hostname.is_(None),
                                  func.lower(_CD.c.target_hostname) == host),
                              ~already))                          # rule 2: one-shot per machine
                  .order_by(_CD.c.created)).fetchall()
    return [{"id": i, "since": s or ""} for i, s, _c in rows]


def ack(directive_id, machine, status, shipped=0, detail="", con=None):
    """Record what one machine did about one directive. Returns True when it was stored.

    Rejects an ack for a directive that does not exist, or from a machine in another org — an ack is
    written from an authenticated client request, so it is attacker-influenced input like any other.
    Replaces a previous ack from the same machine (a retried sweep reporting a better count is the
    normal case), which is also what keeps the (directive_id, token_hash) primary key honest."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    if not machine or not machine.get("token_hash"):
        return False
    con = con or orgs.control_db()
    row = db.run(con, select(_CD.c.org_id).where(_CD.c.id == directive_id)).fetchone()
    if not row or row[0] != machine.get("org_id"):
        return False                                              # unknown, or another tenant's directive
    try:
        n = max(0, int(shipped))
    except (TypeError, ValueError):
        n = 0
    db.run(con, delete(_CA).where(and_(_CA.c.directive_id == directive_id,
                                       _CA.c.token_hash == machine["token_hash"])))
    db.run(con, insert(_CA).values(directive_id=directive_id, token_hash=machine["token_hash"],
                                   email=machine.get("email") or "", hostname=machine.get("hostname") or "",
                                   at=_now(), status=status, shipped=n,
                                   detail=str(detail or "")[:_MAX_DETAIL]))
    con.commit()
    return True


def acks(directive_id, con=None):
    con = con or orgs.control_db()
    return [{"token_hash": th, "email": e, "hostname": h, "at": at, "status": st,
             "shipped": n or 0, "detail": d or ""}
            for th, e, h, at, st, n, d in db.run(con, select(
                _CA.c.token_hash, _CA.c.email, _CA.c.hostname, _CA.c.at, _CA.c.status,
                _CA.c.shipped, _CA.c.detail)
                .where(_CA.c.directive_id == directive_id).order_by(_CA.c.at.desc()))]


def matches(machine_row, target_email, target_hostname):
    """Would this directive reach that machine? The same rule `for_client` applies in SQL, in Python,
    so the console can say '3 of 12' instead of a bare 'answered'."""
    te, th_ = _norm(target_email), _norm(target_hostname)
    if te and _norm(machine_row.get("email")) != te:
        return False
    if th_ and _norm(machine_row.get("hostname")) != th_:
        return False
    return True


def list_directives(org_id, limit=50, con=None):
    """Operator view: every directive for one org, newest first, each with its acks folded in.

    `targets` is how many live machines the directive reaches, so the console can render "3 of 12
    answered" rather than a bare "answered". One machine answering out of twelve is not the same
    event as twelve answering, and a single word cannot tell an operator which one happened."""
    org_id = store.valid_org_id(org_id)
    con = con or orgs.control_db()
    fleet_now = machines(org_id, con=con)
    rows = db.run(con, select(_CD.c.id, _CD.c.target_email, _CD.c.target_hostname, _CD.c.since,
                              _CD.c.created, _CD.c.created_by, _CD.c.revoked_at)
                  .where(_CD.c.org_id == org_id)
                  .order_by(_CD.c.created.desc()).limit(limit)).fetchall()
    out = []
    for i, te, th_, s, c, cb, rv in rows:
        a = acks(i, con=con)
        n_targets = sum(1 for m in fleet_now if matches(m, te, th_))
        applied = [x for x in a if x["status"] == "applied"]
        out.append({"id": i, "target_email": te or "", "target_hostname": th_ or "", "since": s or "",
                    "created": c, "created_by": cb or "", "revoked_at": rv,
                    "acks": a, "shipped": sum(x["shipped"] for x in applied),
                    # `targets` counts machines that exist NOW: a laptop revoked since can never
                    # answer, so counting it would leave every old directive stuck at "3 of 12".
                    "targets": n_targets, "answered": len(a),
                    "state": "revoked" if rv else ("answered" if len(a) >= max(n_targets, 1)
                                                   else ("partial" if a else "waiting"))})
    return out


def machines(org_id, con=None):
    """Every machine that could receive a directive: one row per live token, newest use first.

    This is `orgs.list_tokens` folded to what the fleet panel needs — including `hostname`, which is
    the half of the target the token list already knows and the corpus never does.

    `org_id` is resolved through valid_org_id like every other function here, INCLUDING None. Passing
    None straight through reached `list_tokens(None)`, which returns every org's tokens, while
    `list_directives(None)` and `archive.stats(None)` both resolve to `default` — so a call without
    an org answered with one tenant's directives beside every tenant's machines."""
    org_id = store.valid_org_id(org_id)
    return [{"token_hash": t["token_hash"], "email": t["email"] or "", "hostname": t["hostname"] or "",
             "last_used_at": t["last_used_at"] or "", "created": t["created"], "active": t["active"]}
            for t in orgs.list_tokens(org_id, con=con) if t["active"]]


if __name__ == "__main__":
    import os, tempfile, shutil

    wd = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(wd)
    try:
        con = orgs.control_db(":memory:")
        orgs._create_org("Acme", org_id="acme", con=con)
        orgs._create_org("Other", org_id="other", con=con)
        con.commit()
        t_sam = orgs.mint_token("sam@acme.com", "acme", hostname="Sams-MacBook", con=con)
        t_sam2 = orgs.mint_token("sam@acme.com", "acme", hostname="sams-desktop", con=con)
        t_kim = orgs.mint_token("kim@acme.com", "acme", hostname="kim-laptop", con=con)
        t_out = orgs.mint_token("zed@other.com", "other", hostname="zed-pc", con=con)
        m_sam, m_sam2 = orgs.machine(t_sam, con=con), orgs.machine(t_sam2, con=con)
        m_kim, m_out = orgs.machine(t_kim, con=con), orgs.machine(t_out, con=con)
        assert m_sam["hostname"] == "Sams-MacBook" and m_sam["email"] == "sam@acme.com"
        assert m_sam["token_hash"] != m_sam2["token_hash"], "two machines, two tokens, two identities"

        # --- since normalization is a CREATE-time gate ------------------------------------------
        assert norm_since("") == "" and norm_since("2026-08-19").startswith("2026-08-19T00:00:00")
        for bad in ("yesterday", "19/08/2026", "2026-13-99"):
            try:
                norm_since(bad); raise AssertionError(f"{bad!r} must not be accepted")
            except ValueError:
                pass
        try:
            norm_since("2099-01-01"); raise AssertionError("a future floor fetches nothing — refuse it")
        except ValueError:
            pass

        # --- targeting ---------------------------------------------------------------------------
        try:
            create_directive("acme", target_hostname="MacBook-Pro", con=con)
            raise AssertionError("a bare hostname must be refused (not unique across people)")
        except ValueError:
            pass

        d_all = create_directive("acme", since="2026-08-19", created_by="admin", con=con)
        assert [x["id"] for x in for_client(m_sam, con=con)] == [d_all["id"]]
        assert [x["id"] for x in for_client(m_kim, con=con)] == [d_all["id"]], "'' target = the whole org"
        assert for_client(m_out, con=con) == [], "a directive must never cross into another org"
        assert for_client(None, con=con) == [] and for_client({}, con=con) == []

        d_sam = create_directive("acme", since="", target_email="SAM@acme.com", con=con)
        assert len(for_client(m_sam, con=con)) == 2, "case-insensitive email match"
        assert len(for_client(m_kim, con=con)) == 1, "...and it does not reach kim"

        d_host = create_directive("acme", target_email="sam@acme.com", target_hostname="sams-desktop", con=con)
        assert d_host["id"] in [x["id"] for x in for_client(m_sam2, con=con)]
        assert d_host["id"] not in [x["id"] for x in for_client(m_sam, con=con)], "host narrows to ONE machine"

        # --- one-shot per machine ------------------------------------------------------------------
        assert ack(d_all["id"], m_sam, "applied", shipped=12, con=con) is True
        assert [x["id"] for x in for_client(m_sam, con=con)] == [d_sam["id"]], "an acked directive stops being served"
        assert d_all["id"] in [x["id"] for x in for_client(m_kim, con=con)], "...to THAT machine only"

        # An ack can say no, and it is what the console reads.
        assert ack(d_all["id"], m_kim, "declined", detail="backfill declined at setup", con=con)
        rolled = {d["id"]: d for d in list_directives("acme", con=con)}
        # d_all targets the whole org (3 machines) and 2 have answered: PARTIAL, not answered. An
        # operator who reads "answered" stops waiting for the third.
        assert rolled[d_all["id"]]["state"] == "partial" and rolled[d_all["id"]]["shipped"] == 12
        assert rolled[d_all["id"]]["targets"] == 3 and rolled[d_all["id"]]["answered"] == 2
        assert {a["status"] for a in rolled[d_all["id"]]["acks"]} == {"applied", "declined"}
        assert rolled[d_sam["id"]]["state"] == "waiting", "nothing has answered this one yet"

        # A re-ack from the same machine replaces, never duplicates (the PK is (directive, token)).
        assert ack(d_all["id"], m_sam, "applied", shipped=30, con=con)
        assert len(acks(d_all["id"], con=con)) == 2 and rolled[d_all["id"]]["id"] == d_all["id"]
        assert sum(a["shipped"] for a in acks(d_all["id"], con=con)) == 30

        # --- acks are attacker-influenced input ------------------------------------------------------
        assert ack(d_all["id"], m_out, "applied", con=con) is False, "another org's machine cannot ack"
        assert ack("no-such-directive", m_sam, "applied", con=con) is False
        try:
            ack(d_sam["id"], m_sam, "done", con=con); raise AssertionError("unknown status must raise")
        except ValueError:
            pass
        ack(d_sam["id"], m_sam, "error", shipped=-5, detail="x" * 5000, con=con)
        bad = acks(d_sam["id"], con=con)[0]
        assert bad["shipped"] == 0 and len(bad["detail"]) == _MAX_DETAIL, "counts floor at 0, detail is capped"

        # --- revoke -----------------------------------------------------------------------------------
        assert revoke_directive(d_host["id"], con=con) is True
        assert revoke_directive(d_host["id"], con=con) is False, "idempotent"
        assert d_host["id"] not in [x["id"] for x in for_client(m_sam2, con=con)]
        assert revoke_directive("nope", con=con) is False

        # A revoked directive keeps the acks of machines that already complied — the console must
        # not claim work was undone when the sessions really did ship.
        assert [d for d in list_directives("acme", con=con) if d["id"] == d_all["id"]][0]["acks"]

        # --- machines feed the panel from the token store ------------------------------------------
        ms = machines("acme", con=con)
        assert {m["hostname"] for m in ms} == {"Sams-MacBook", "sams-desktop", "kim-laptop"}
        orgs.revoke(t_kim, con=con)
        assert "kim-laptop" not in {m["hostname"] for m in machines("acme", con=con)}, "revoked token, gone from the fleet"
        assert orgs.machine(t_kim, con=con) is None, "and it can no longer fetch directives at all"

        # --- the console must be able to say N of M, and to tell a partial answer from a done one ---
        d_org = create_directive("acme", con=con)
        live = len(machines("acme", con=con))
        roll = {x["id"]: x for x in list_directives("acme", con=con)}[d_org["id"]]
        assert roll["targets"] == live and roll["answered"] == 0 and roll["state"] == "waiting", roll
        ack(d_org["id"], m_sam, "applied", shipped=4, con=con)
        roll = {x["id"]: x for x in list_directives("acme", con=con)}[d_org["id"]]
        assert roll["state"] == "partial" and roll["answered"] == 1, \
            "one machine of several is NOT 'answered' — an operator reading that would stop waiting"
        ack(d_org["id"], m_sam2, "declined", con=con)
        roll = {x["id"]: x for x in list_directives("acme", con=con)}[d_org["id"]]
        assert roll["state"] == "answered" and roll["shipped"] == 4, \
            "every LIVE machine has answered; only APPLIED acks count toward shipped (a decline moved nothing)"

        print("OK fleet: since validated at creation; org + email + host targeting (bare host refused); "
              "one-shot per machine; acks can decline; cross-org ack refused; revoke idempotent + keeps acks; "
              "N-of-M targets so a partial answer never reads as a finished one")
    finally:
        os.chdir(cwd)
        shutil.rmtree(wd, ignore_errors=True)
