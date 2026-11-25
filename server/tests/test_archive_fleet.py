"""Retained transcripts + the fleet plane, at the seams the modules' own self-checks cannot reach.

analyzer/archive.py and analyzer/fleet.py each carry an assert-based `__main__` self-check for their
own behaviour. What is left, and what lives here, is the WIRING: that ingest actually archives, that
forget actually deletes, that the read route enforces owner-or-admin, and that a directive survives
the round trip from an admin POST to the machine that has to act on it.
"""
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import apiserver
from analyzer import archive, auth, cliauth, fleet, gql, orgs, store, tenant


def _set_days(value, org_id=None):
    """Set one org's transcript retention the way `csa.py org config --set archive_days=` does."""
    store.sqlite_db(store.org_db(org_id)).close()
    tenant.set_config(store.org_db(org_id), archive_days=value)


@pytest.fixture()
def wd(tmp_path, monkeypatch):
    """A cwd of its own: store/orgs/archive paths are all relative to it. The default org keeps
    transcripts for 30 days; every other org is at the default (none) unless a test says otherwise."""
    monkeypatch.chdir(tmp_path)
    _set_days(30)
    yield tmp_path


def _post(session_id, user, text="hello there", org_id=None):
    """One ingest, through the real function the HTTP route calls."""
    content = "\n".join(json.dumps({"message": {"role": r, "content": c}})
                        for r, c in (("user", text), ("assistant", "and the answer"))) + "\n"
    return apiserver.ingest_session(session_id, content, user, org_id=org_id)


def test_ingest_archives_and_forget_deletes(wd):
    r = _post("s-keep", "mara@example.com")
    assert r["ok"] and r["queued"]
    rec = archive.get("s-keep")
    assert rec and rec["turns"], "an ingest must retain the redacted transcript while archiving is on"
    assert rec["user"] == "mara@example.com"

    # The delete has to reach it. Nothing else in forget() touches the archive — it is the one piece
    # of a session that is not derived from the corpus.
    con = store.sqlite_db(store.org_db(None))
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-keep','session','mara@example.com')")
    con.commit()
    assert apiserver.forget(con, "s-keep", requester="mara@example.com", require_owner=True)["ok"]
    assert archive.get("s-keep") is None, "forget left the full transcript on disk"


def test_archiving_off_stores_nothing(wd):
    _set_days(0)
    assert _post("s-off", "mara@example.com")["ok"]
    assert archive.get("s-off") is None
    assert not os.path.exists(os.path.join(store.org_dir(None), "archive")), \
        "the default (retention off) must not even create the directory"


def test_a_secret_never_reaches_the_archive(wd):
    """The archive stores the queue's bytes, which ingest already redacted. If that ever stops being
    true, this is the test that fails — the checklist's redaction gate for a new storage surface."""
    _post("s-secret", "mara@example.com", text="my key is sk-abcdEFGH0123456789abcdEFGH0123456789")
    rec = archive.get("s-secret")
    assert rec, "sanity: it was archived"
    assert "sk-abcdEFGH0123456789" not in "\n".join(rec["turns"]), "a live secret reached long-term storage"


def test_transcript_route_is_owner_or_admin_only(wd, monkeypatch):
    _post("s-read", "mara@example.com")
    con = store.sqlite_db(store.org_db(None))
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-read','session','mara@example.com')")
    con.commit()

    owner = {"email": "mara@example.com", "org_id": store.DEFAULT_ORG, "role": "member"}
    mate = {**owner, "email": "someone@example.com"}
    admin = {**owner, "email": None, "role": "admin"}
    boss = {**owner, "email": "boss@example.com", "role": "admin"}
    # Loopback dev and a NAMED workspace admin bypass ownership; the nameless admin role (the
    # self-host shared secret, held by every client) never does.
    monkeypatch.setenv("CSA_AUTH", "org")

    code, body = apiserver._transcript(con, "s-read", owner)
    assert code == 200 and body["turns"], "the owner reads their own transcript"
    assert body["retention_days"] == 30

    assert apiserver._transcript(con, "s-read", admin)[0] == 404, "the shared secret never bypasses ownership"
    assert apiserver._transcript(con, "s-read", boss)[0] == 200, "a workspace admin reads any transcript in the org"
    monkeypatch.setenv("CSA_AUTH", "off")
    assert apiserver._transcript(con, "s-read", admin)[0] == 200, "loopback dev is open"
    monkeypatch.setenv("CSA_AUTH", "org")

    # An admin of ANOTHER org: their credential opens their own org's corpus and archive dir, where
    # this session does not exist. Rival retains too, so the miss is the file's, not archive_off.
    _set_days(30, "rival")
    rival_con = store.sqlite_db(store.org_db("rival"))
    rival_boss = {"email": "zed@rival.com", "org_id": "rival", "role": "admin"}
    code, body = apiserver._transcript(rival_con, "s-read", rival_boss, org_id="rival")
    assert code == 404 and body.get("code") != "archive_off"

    # The GraphQL reader applies the SAME rule (gql.may_read_transcript is the one function both call).
    def total(principal, c=con, org_id=None):
        r = gql.run_query('{ transcript(sessionId: "s-read") { totalCount } }', None, con=c,
                          principal=principal, org_id=org_id, auth_mode="org")
        return r["data"]["transcript"]["totalCount"]
    assert total(owner) > 0 and total(boss) == total(owner)
    assert total(mate) == 0 and total(admin) == 0
    assert total(rival_boss, rival_con, "rival") == 0
    listed = gql.run_query('{ sessions { items { sessionId hasTranscript } } }', None, con=con,
                           principal=boss, org_id=None, auth_mode="org")
    assert {i["sessionId"]: i["hasTranscript"] for i in listed["data"]["sessions"]["items"]}["s-read"] is True, \
        "the listing tells an admin the transcript is there for them"

    # A teammate is NOT enough. Everything else in this app is org-readable; a transcript is not.
    code, body = apiserver._transcript(con, "s-read", mate)
    assert code == 404 and not body["ok"]
    # ...and the refusal is byte-identical to a miss, so the route is not an existence oracle for
    # session ids (which recall hands out as provenance).
    miss = apiserver._transcript(con, "no-such-session", mate)
    assert (code, body) == miss, "'not yours' must be indistinguishable from 'no such session'"


def test_transcript_route_refuses_past_the_window_even_if_the_purge_never_ran(wd):
    _post("s-old", "mara@example.com")
    con = store.sqlite_db(store.org_db(None))
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-old','session','mara@example.com')")
    con.commit()
    owner = {"email": "mara@example.com", "org_id": store.DEFAULT_ORG, "admin": False}
    assert apiserver._transcript(con, "s-old", owner)[0] == 200

    # Shrink the window rather than moving the clock: same effect, no monkeypatched datetime. The
    # ETL owns the purge, and a down ETL must not silently extend a retention promise.
    _set_days(0)
    assert apiserver._transcript(con, "s-old", owner)[0] == 404
    assert os.path.exists(archive._path("s-old")), "read-refusal is not a delete — purge owns that"


def test_directive_round_trip(wd):
    con = orgs.control_db(":memory:")
    orgs._create_org("Acme", org_id="acme", con=con)
    con.commit()
    token = orgs.mint_token("sam@acme.com", "acme", hostname="Sams-MacBook", con=con)
    m = orgs.machine(token, con=con)

    d = fleet.create_directive("acme", since="2026-08-19", created_by="admin", con=con)
    served = fleet.for_client(m, con=con)
    assert [x["id"] for x in served] == [d["id"]]
    assert served[0]["since"].startswith("2026-08-19T00:00:00"), \
        "the client parses this with RFC3339 — a shape it cannot read means it ships NOTHING"

    fleet.ack(d["id"], m, "declined", detail="backfill declined at setup", con=con)
    assert fleet.for_client(m, con=con) == [], "an acked directive is not served again to that machine"
    rolled = fleet.list_directives("acme", con=con)[0]
    assert rolled["state"] == "answered" and rolled["acks"][0]["status"] == "declined", \
        "a refusal must be visible in the console, not read as still-pending"


def test_a_machine_cannot_collect_another_orgs_directives(wd):
    con = orgs.control_db(":memory:")
    orgs._create_org("Acme", org_id="acme", con=con)
    orgs._create_org("Rival", org_id="rival", con=con)
    con.commit()
    theirs = orgs.machine(orgs.mint_token("zed@rival.com", "rival", hostname="pc", con=con), con=con)
    d = fleet.create_directive("acme", since="2026-08-19", con=con)
    assert fleet.for_client(theirs, con=con) == []
    assert fleet.ack(d["id"], theirs, "applied", con=con) is False, "nor ack one"


def test_forget_reaches_a_session_that_is_still_in_flight(wd):
    """Ingest queues (and archives) at POST time; the ETL extracts minutes later. A delete issued in
    that window used to answer "not yours" and leave BOTH copies on disk — the queued one, which the
    next drain would then extract INTO the corpus, and the retained transcript for its whole window.
    Found by the HTTP end-to-end; pinned here."""
    from analyzer import queue

    assert _post("s-flight", "mara@example.com")["ok"]
    assert queue.stage("s-flight") == "queued" and archive.get("s-flight")
    con = store.sqlite_db(store.org_db(None))            # no corpus row: nothing has drained yet

    # A stranger gets the same uniform miss as for an id that never existed. The id itself is echoed
    # (so the message reads sensibly), so compare with it swapped out: what must not differ is
    # everything else, which is what would otherwise reveal that this id exists and is someone's.
    denied = apiserver.forget(con, "s-flight", requester="mallory@x", require_owner=True)
    absent = apiserver.forget(con, "no-such-id", requester="mallory@x", require_owner=True)
    assert not denied["ok"] and not absent["ok"]
    assert denied["error"].replace("'s-flight'", "X") == absent["error"].replace("'no-such-id'", "X"), \
        "an in-flight session that is not yours must not be distinguishable from one that never existed"
    assert queue.stage("s-flight") == "queued", "a refused forget must delete nothing"
    # ...and so does a caller who omits their identity entirely
    assert not apiserver.forget(con, "s-flight", require_owner=True)["ok"]

    r = apiserver.forget(con, "s-flight", requester="Mara@Example.com", require_owner=True)
    assert r["ok"] and r["deleted"], r
    assert archive.get("s-flight") is None, "the retained transcript survived a delete"
    assert queue.stage("s-flight") is None, "the queued copy survived — the next drain would index it"


def test_forget_drops_the_queued_copy_even_with_retention_off(wd):
    """The queue leak is not conditional on the archive: with retention off there is no archived
    record to authorize from, so ownership comes from the queued session itself."""
    from analyzer import queue

    _set_days(0)
    assert _post("s-q", "mara@example.com")["ok"]
    assert queue.stage("s-q") == "queued" and archive.get("s-q") is None
    con = store.sqlite_db(store.org_db(None))
    assert apiserver.forget(con, "s-q", requester="mara@example.com", require_owner=True)["ok"]
    assert queue.stage("s-q") is None


def test_forget_removes_the_extraction_cache_of_its_own_org_only(wd):
    """The extraction cache holds every claim with its verbatim evidence quote, so a forget that
    leaves it has not deleted the session's quotes. The cache is per org and keyed by session id
    alone: the same id in another org is that org's data and stays."""
    from analyzer import incremental, queue

    sess = {"session_id": "s-cache"}
    record = {"hidden_context": [{"claim": "x", "evidence": "the verbatim quote"}]}
    incremental.persist(sess, record)
    incremental.persist(sess, record, org_id="acme")
    acme_cache, default_cache = incremental._out("s-cache", "acme"), incremental._out("s-cache")
    acme = store.sqlite_db(store.org_db("acme"))
    acme.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-cache','session','alice@acme.com')")
    acme.commit()
    assert _post("s-cache", "alice@acme.com", org_id="acme")["ok"]   # a re-ship of the grown session
    assert queue.stage("s-cache", org_id="acme") == "queued"

    assert apiserver.forget(acme, "s-cache", requester="alice@acme.com", require_owner=True, org_id="acme")["ok"]
    assert not os.path.exists(acme_cache), "forget left the claims and their quotes on disk"
    assert queue.stage("s-cache", org_id="acme") is None, "the queued re-ship would index the session again"
    assert os.path.exists(default_cache), "forget in one org deleted another org's cache"


def test_self_host_token_mode_has_no_admin_transcript_bypass(wd, monkeypatch):
    """The self-host shared secret is held by every client. Its control-plane authority must not let
    every teammate read everyone else's retained transcript. Only loopback dev is open."""
    _post("s-mode", "mara@example.com")
    con = store.sqlite_db(store.org_db(None))
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-mode','session','mara@example.com')")
    con.commit()
    shared = {"email": None, "org_id": store.DEFAULT_ORG, "role": "admin"}

    monkeypatch.setenv("CSA_AUTH", "org")
    assert apiserver._transcript(con, "s-mode", shared)[0] == 404, \
        "the self-host client credential must NOT read another person's transcript"
    monkeypatch.setenv("CSA_AUTH", "off")
    assert apiserver._transcript(con, "s-mode", shared)[0] == 200, \
        "loopback dev has no credential at all; refusing there only breaks local work"
    monkeypatch.setenv("CSA_AUTH", "org")
    owner = {**shared, "email": "mara@example.com"}
    assert apiserver._transcript(con, "s-mode", owner)[0] == 200, "the owner reads their own in every mode"


def test_forever_keeps_transcripts_with_no_expiry(wd):
    """archive_days=forever is the landing's promise: kept until the shipper or a workspace admin
    forgets it, never aged out. It rides the existing knob as a named constant, not a large number."""
    from datetime import timedelta

    _set_days("forever")
    assert archive.retention_days() == tenant.KEEP_FOREVER == -1
    assert archive.enabled(), "forever is ON wherever on matters"
    assert cliauth.policy(store.DEFAULT_ORG)["archive_days"] == tenant.KEEP_FOREVER, "disclosed to csa-client as is"

    _post("s-4ever", "mara@example.com")
    rec = archive.get("s-4ever")
    assert rec and rec["turns"]
    far = archive._now() + timedelta(days=365 * 100)
    assert archive.get("s-4ever", now=far) is not None, "a century later it still reads"
    assert archive.expired(rec, tenant.KEEP_FOREVER, now=far) is False
    assert archive.expired({"stored_at": "not-a-date"}, tenant.KEEP_FOREVER) is False, \
        "no age is needed when nothing expires"
    assert archive.purge(now=far) == 0 and os.path.exists(archive._path("s-4ever")), "purge ages nothing"

    con = store.sqlite_db(store.org_db(None))
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-4ever','session','mara@example.com')")
    con.commit()
    owner = {"email": "mara@example.com", "org_id": store.DEFAULT_ORG, "role": "member"}
    code, body = apiserver._transcript(con, "s-4ever", owner)
    assert code == 200 and body["retention_days"] == tenant.KEEP_FOREVER
    r = gql.run_query('{ transcript(sessionId: "s-4ever") { retentionDays totalCount } }', None, con=con,
                      principal=owner, org_id=None, auth_mode="org")
    assert r["data"]["transcript"] == {"retentionDays": tenant.KEEP_FOREVER, "totalCount": body["total_count"]}

    # Only the literal spells forever. A negative, junk or a number past the cap is refused at write
    # time and leaves the setting where it was; clearing it is OFF. A typo never reads as unlimited.
    for raw in ("-1", -5, "banana", 99999):
        with pytest.raises(ValueError):
            _set_days(raw)
        assert archive.retention_days() == tenant.KEEP_FOREVER, raw
    for raw, want in (("", 0), (" Forever ", -1), (None, 0), ("30", 30)):
        _set_days(raw)
        assert archive.retention_days() == want, raw

    # Forget is the only way out: owner deletes the corpus row, the facts and the transcript together.
    _set_days("forever")
    assert apiserver.forget(con, "s-4ever", requester="mara@example.com", require_owner=True)["ok"]
    assert archive.get("s-4ever") is None and not os.path.exists(archive._path("s-4ever"))


def test_workspace_admin_forgets_any_session_in_the_workspace(wd):
    """The landing says a session goes when the person who shipped it OR the workspace admin deletes
    it. `admin` is what the HTTP route passes for a named admin of the caller's own org; a member
    gets today's refusal, and an admin of another org gets the same uniform miss as a stranger."""
    from analyzer import queue

    _set_days(30, "acme")
    con = store.sqlite_db(store.org_db("acme"))
    assert _post("s-alice", "alice@acme.com", org_id="acme")["ok"]
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-alice','session','alice@acme.com')")
    con.execute("INSERT INTO hidden_context(session_id,type,claim,ref) VALUES ('s-alice','decision','x','r1')")
    con.commit()

    def alive():
        return (bool(con.execute("SELECT 1 FROM sessions WHERE session_id='s-alice'").fetchone()),
                bool(con.execute("SELECT 1 FROM hidden_context WHERE session_id='s-alice'").fetchone()),
                archive.get("s-alice", org_id="acme") is not None)

    assert alive() == (True, True, True)
    member = apiserver.forget(con, "s-alice", requester="bob@acme.com", require_owner=True, org_id="acme")
    assert not member["ok"] and alive() == (True, True, True), "a teammate cannot forget another's session"

    _set_days(30, "rival")
    rival_con = store.sqlite_db(store.org_db("rival"))
    other = apiserver.forget(rival_con, "s-alice", requester="zed@rival.com", require_owner=True,
                             org_id="rival", admin=True)
    absent = apiserver.forget(rival_con, "no-such-id", requester="zed@rival.com", require_owner=True,
                              org_id="rival", admin=True)
    assert not other["ok"] and alive() == (True, True, True), "an admin of another org deletes nothing"
    assert other["error"].replace("'s-alice'", "X") == absent["error"].replace("'no-such-id'", "X"), \
        "...and learns nothing about whether the id exists"

    boss = apiserver.forget(con, "s-alice", requester="boss@acme.com", require_owner=True, org_id="acme",
                            admin=True)
    assert boss["ok"] and boss["deleted"] and alive() == (False, False, False), \
        "the workspace admin takes the row, the facts and the transcript together"

    # ...and a session still in flight (shipped, not yet extracted): queued copy and transcript both go.
    assert _post("s-flight-b", "alice@acme.com", org_id="acme")["ok"]
    assert queue.stage("s-flight-b", org_id="acme") == "queued" and archive.get("s-flight-b", org_id="acme")
    assert not apiserver.forget(con, "s-flight-b", requester="bob@acme.com", require_owner=True,
                                org_id="acme")["ok"]
    assert apiserver.forget(con, "s-flight-b", requester="boss@acme.com", require_owner=True,
                            org_id="acme", admin=True)["stage"] == "in-flight"
    assert queue.stage("s-flight-b", org_id="acme") is None and archive.get("s-flight-b", org_id="acme") is None


def test_workspace_admin_is_a_named_admin_only():
    assert auth.workspace_admin({"email": "boss@acme.com", "org_id": "acme", "role": "admin"})
    assert not auth.workspace_admin({"email": "bob@acme.com", "org_id": "acme", "role": "member"})
    assert not auth.workspace_admin({"email": None, "org_id": "default", "role": "admin"}), \
        "the shared secret and loopback dev carry the role with no email: not a workspace admin"
    assert not auth.workspace_admin({})


def _serve(tokens):
    """The real handler on a loopback port. Returns (call, server); `call(path, who, body)` sends
    `who`'s bearer token."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    def call(path, who=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
        if who:
            req.add_header("Authorization", "Bearer " + tokens.get(who, who))   # a name, or a raw secret
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    return call, srv


@pytest.fixture()
def http(wd, monkeypatch):
    """Org mode, two tenants with per-user tokens, both keeping transcripts for 30 days (so a
    cross-org miss is the file's, never the archive_off branch). Non-default orgs on purpose: the
    per-org file is the boundary forget must respect."""
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    cc = orgs.control_db()
    orgs._create_org("Acme", org_id="acme", con=cc)
    orgs._create_org("Rival", org_id="rival", con=cc)
    cc.commit()
    _set_days(30, "acme")
    _set_days(30, "rival")
    tokens = {"alice": orgs.mint_token("alice@acme.com", "acme", hostname="a", con=cc),
              "bob": orgs.mint_token("bob@acme.com", "acme", hostname="b", con=cc),
              "boss": orgs.mint_token("boss@acme.com", "acme", hostname="c", con=cc, role="admin"),
              "zed": orgs.mint_token("zed@rival.com", "rival", hostname="z", con=cc, role="admin")}
    call, srv = _serve(tokens)
    yield call
    srv.shutdown()
    srv.server_close()


def test_forget_route_takes_admin_from_the_credential_only(http, monkeypatch):
    """Over HTTP the admin grant comes from the token's role, never from the body, and an org's admin
    reaches only their own org's sessions."""
    assert _post("s-a", "alice@acme.com", org_id="acme")["ok"]
    con = store.sqlite_db(store.org_db("acme"))
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('s-a','session','alice@acme.com')")
    con.commit()

    def gone():
        return not con.execute("SELECT 1 FROM sessions WHERE session_id='s-a'").fetchone() \
            and archive.get("s-a", org_id="acme") is None

    code, r = http("/api/forget", "bob", {"session_id": "s-a", "owner": "alice@acme.com"})
    assert code == 200 and not r["ok"] and not gone(), "a member's self-asserted owner field is inert"
    code, other = http("/api/forget", "zed", {"session_id": "s-a"})
    code, absent = http("/api/forget", "zed", {"session_id": "never-was"})
    assert not other["ok"] and not gone(), "another org's admin deletes nothing"
    assert other["error"].replace("'s-a'", "X") == absent["error"].replace("'never-was'", "X")
    code, r = http("/api/forget", "boss", {"session_id": "s-a"})
    assert code == 200 and r["ok"] and r["deleted"] and gone(), "the workspace admin forgets a member's session"
    assert http("/api/forget", None, {"session_id": "s-a"})[0] == 401


def test_retention_is_disclosed_per_workspace_over_http(http, monkeypatch):
    """Every surface that tells a person what is kept answers for THEIR workspace: /api/me and
    /api/client-config from the credential, /api/policy from a presented token. The pre-auth
    sign-in page cannot know the workspace on a server that hosts two, so it gets null, and so
    does the retired shared secret."""
    _set_days("forever", "rival")
    assert http("/api/me", "boss")[1]["archive_days"] == 30
    assert http("/api/me", "zed")[1]["archive_days"] == tenant.KEEP_FOREVER
    assert http("/api/client-config", "alice")[1]["archive_days"] == 30
    assert http("/api/client-config", "zed")[1]["archive_days"] == tenant.KEEP_FOREVER
    assert http("/api/policy", "bob")[1]["archive_days"] == 30, "a re-run of setup holds a token"
    assert http("/api/policy", "zed")[1]["archive_days"] == tenant.KEEP_FOREVER
    code, cfg = http("/api/auth-config")
    assert code == 200 and cfg["archive_days"] is None, "two workspaces: the sign-in page cannot say"
    code, pol = http("/api/policy")
    assert code == 200 and pol["archive_days"] is None, "a laptop with no token yet learns it after sign-in"
    monkeypatch.setenv("CSA_AUTH_TOKEN", "the-old-shared-secret")
    assert http("/api/policy", "the-old-shared-secret")[1]["archive_days"] is None, \
        "the shared secret names no workspace once tenants exist"


def test_single_workspace_discloses_retention_before_sign_in(wd, monkeypatch):
    """A self-host box (the rowless default corpus), an adopted default, or a server with one org
    row and no default corpus IS one workspace: the sign-in page and the pre-consent policy can
    state the number. A second workspace, row or live default corpus, makes both null."""
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    _set_days("forever")                                 # the default corpus exists, no org rows
    call, srv = _serve({})
    try:
        assert call("/api/auth-config")[1]["archive_days"] == tenant.KEEP_FOREVER
        assert call("/api/policy")[1]["archive_days"] == tenant.KEEP_FOREVER
        cc = orgs.control_db()
        orgs.adopt_default_org("Home", con=cc)
        cc.commit()
        assert call("/api/auth-config")[1]["archive_days"] == tenant.KEEP_FOREVER, "adopted: still one"
        orgs._create_org("Acme", org_id="acme", con=cc)
        cc.commit()
        _set_days(7, "acme")
        assert call("/api/auth-config")[1]["archive_days"] is None, "an org row beside default: two"
        assert call("/api/policy")[1]["archive_days"] is None
    finally:
        srv.shutdown()
        srv.server_close()

    one = wd / "one-org"                                 # one org row and NO default corpus file
    one.mkdir()
    monkeypatch.chdir(one)
    cc = orgs.control_db()
    orgs._create_org("Acme", org_id="acme", con=cc)
    cc.commit()
    _set_days(7, "acme")
    assert not os.path.exists(store.org_db()), "sanity: nothing opened the default corpus"
    call, srv = _serve({})
    try:
        assert call("/api/auth-config")[1]["archive_days"] == 7, "one org row: that org is the workspace"
        orgs._create_org("Rival", org_id="rival", con=cc)
        cc.commit()
        assert call("/api/auth-config")[1]["archive_days"] is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_each_org_keeps_its_own_window(wd):
    """Two orgs on one box: the value in each org's tenant_config is the one its ingest, read and
    purge honour. There is no deployment-wide switch."""
    from datetime import timedelta
    from analyzer import drain_loop

    _set_days(30, "acme")
    _set_days("forever", "third")                        # rival stays at the default: none
    for org in ("acme", "rival", "third"):
        assert _post("s-" + org, "someone@" + org, org_id=org)["ok"]
    assert archive.retention_days("acme") == 30 and archive.retention_days("rival") == 0
    assert archive.retention_days("third") == tenant.KEEP_FOREVER
    assert archive.get("s-acme", org_id="acme") and archive.get("s-third", org_id="third")
    assert archive.get("s-rival", org_id="rival") is None, "an org at the default retains nothing"
    assert not os.path.exists(os.path.join(store.org_dir("rival"), "archive"))

    later = archive._now() + timedelta(days=31)
    assert archive.get("s-acme", org_id="acme", now=later) is None
    assert archive.get("s-third", org_id="third", now=later + timedelta(days=36500)) is not None
    assert archive.purge("acme", now=later) == 1 and archive.purge("third", now=later) == 0
    drain_loop._purge_one("third")                       # the ETL's per-org purge: never raises, ages nothing
    assert archive.get("s-third", org_id="third")
