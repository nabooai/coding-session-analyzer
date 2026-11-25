"""apiserver's _self_check, ported verbatim into the suite (P3). Same fixtures, same assertions —
these used to run via `CSA_SELF_CHECK=1 python apiserver.py`; the env-mutating blocks keep their
original save/restore shape so the suite stays order-independent."""
import json
import os
import threading
import time
import urllib.request

import pytest
from http.server import ThreadingHTTPServer

from sqlalchemy import select

import apiserver
from analyzer import analytics, auth, cliauth, db, gql, improve, llm, models, orgs, route, store, tenant


def test_forget_removes_derived_rows_and_enforces_owner():
    con = store.sqlite_db(":memory:")
    sid = "forgotten"
    con.execute("INSERT INTO sessions(session_id,source) VALUES (?,'session')", (sid,))
    con.execute("INSERT INTO harvest_actions VALUES (?,?,?,?)", (sid, "create", "PR-1", "ctx"))
    con.execute("INSERT INTO harvest_mentions VALUES (?,?)", (sid, "PR-1"))
    con.execute("INSERT INTO harvest_links VALUES (?,?,?)", (sid, "https://github.com/o/r/pull/1", "o/r#1"))
    con.execute("INSERT INTO actions VALUES (?,?,?,?)", (sid, "create", "PR-1", "ctx"))
    con.execute("INSERT INTO mentions(session_id,entity_id) VALUES (?,?)", (sid, "PR-1"))
    con.execute("INSERT INTO links VALUES (?,?,?)", (sid, "https://github.com/o/r/pull/1", "o/r#1"))
    con.commit()
    # ownership gate over the HTTP route (require_owner=True): you can only forget your OWN sessions
    con.execute("INSERT INTO sessions(session_id,source,user) VALUES ('owned-by-alice','session','alice@x')")
    con.commit()
    assert not apiserver.forget(con, "no-such-session", require_owner=True)["ok"]                    # unknown id -> ok:False
    assert not apiserver.forget(con, "owned-by-alice", require_owner=True)["ok"]                      # MISSING identity -> DENY (not admin)
    assert not apiserver.forget(con, "owned-by-alice", requester="", require_owner=True)["ok"]        # empty identity -> DENY
    assert not apiserver.forget(con, "owned-by-alice", requester="bob@x", require_owner=True)["ok"]   # wrong owner -> deny
    assert con.execute("SELECT 1 FROM sessions WHERE session_id='owned-by-alice'").fetchone(), "refused forget must not delete"
    assert apiserver.forget(con, "owned-by-alice", requester="Alice@X", require_owner=True)["ok"]     # right owner (case-insensitive) -> allowed
    # RELAY: `user` is derived from transcript CONTENT, so it must never be the only key to delete. The
    # account that actually shipped the session keeps its right (else self-asserted text revokes it), and a
    # third party still gets nothing — transcript text is not an authz grant.
    con.execute("INSERT INTO sessions(session_id,source,user,shipped_by) "
                "VALUES ('relayed','session','jules@x','bot@x')")
    con.commit()
    assert not apiserver.forget(con, "relayed", requester="mallory@x", require_owner=True)["ok"]      # stranger -> deny
    assert apiserver.forget(con, "relayed", requester="bot@x", require_owner=True)["ok"]              # shipper -> allowed
    assert apiserver.forget(con, sid)["ok"]                                      # local/admin path (no require_owner) -> allowed
    tables = store._PERSESSION + store._HARVEST_MARKERS + ("pr_links", "session_workspace", "signals")
    left = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE session_id=?", (sid,)).fetchone()[0] for t in tables}
    assert not any(left.values()), left


def test_gate_wiring_public_surface_and_per_user_tokens():
    # --- gate wiring: a corpus route is denied without a credential; /api/health is open, in each mode ---
    _saved = {k: os.environ.get(k) for k in ("CSA_AUTH", "CSA_AUTH_TOKEN")}
    try:
        os.environ["CSA_AUTH"] = "org"; os.environ["CSA_AUTH_TOKEN"] = "sekret"
        assert "/api/facts" not in auth.OPEN_PATHS and "/api/pipeline" not in auth.OPEN_PATHS, "corpus routes not open"
        assert "/api/health" in auth.OPEN_PATHS, "health is open"
        assert auth.identify(auth._CIHeaders())[0] is False, "no token -> deny corpus route"
        assert auth.identify(auth._CIHeaders({"Authorization": "Bearer sekret"}))[0] is True, "right token -> allow"
        os.environ["CSA_AUTH"] = "off"; os.environ.pop("CSA_AUTH_TOKEN", None)
        assert auth.identify(auth._CIHeaders())[0] is True, "off mode -> allow (loopback-only by boot invariant)"

        # --- onboarding surface: pre-auth by design, and CLOSED to everything else ---------------
        for p in ("/install", "/cli-auth", "/latest/csa-client-darwin-arm64", "/latest/checksums.txt",
                  "/api/cli-auth/new", "/api/cli-auth/approve", "/api/cli-auth/abc123",
                  "/api/sso/start", "/api/sso/callback",   # the OpenID round trip runs before any credential exists
                  "/join/jn_abc123"):                     # the invited person holds no credential yet
            assert apiserver._public(p), p
        assert apiserver._public("/api/policy"), "policy is public (pre-consent redaction floor)"
        assert apiserver._public("/api/ui-login") and apiserver._public("/api/ui-logout"), "the dashboard sign-in is pre-auth"
        for p in ("/api/facts", "/api/onboarding", "/api/improve", "/api/activity", "/api/ask", "/api/team", "/api/ingest", "/api/me",
                  "/api/pipeline", "/", "/index.html", "/api/forget", "/api/graph", "/api/corpus/sessions"):
            assert not apiserver._public(p), "%s must stay behind the gate" % p
        # ^ "/" and "/index.html" STAY here on purpose: the shell is reached by falling back on gate
        #   FAILURE (do_GET), not by joining the pre-auth allowlist. The doc's Deliverable 4 wanted
        #   them public, which would both break this assertion and 404 (_onboarding_get serves no UI).
        assert not apiserver._public("/latest"), "the prefix alone is not public"
        assert not apiserver._public("/join"), "...nor is /join without a token"
        assert not apiserver._public("/joinery"), "prefix matching is anchored, not a substring"
        assert not apiserver._public("/installer"), "_public is exact/prefix, not a substring match"
        assert apiserver._ASSET_RE.match("csa-client-linux-amd64") and apiserver._ASSET_RE.match("csa-client-windows-amd64.exe")
        assert apiserver._ASSET_RE.match("checksums.txt")
        for bad in ("../../etc/passwd", "csa-client-linux-amd64/../../x", ".env", "csa-client-linux-mips",
                    "install.sh", "analyzer/orgs.py"):
            assert not apiserver._ASSET_RE.match(bad), bad

        # --- per-user tokens: identity + ORG ride the credential, and a cross-org read is impossible ---
        os.environ["CSA_AUTH"] = "org"; os.environ["CSA_AUTH_TOKEN"] = "sekret"
        cc = orgs.control_db(":memory:")
        _real_ctl, orgs.control_db = orgs.control_db, lambda path=None: cc
        try:
            # --- browser session (org mode, no tenants: the env invite is CSA_AUTH_TOKEN='sekret') ----------
            # The cookie authenticates HTTP, and only HTTP, and ONLY as a per-user token — via identify's
            # allow_cookie. It runs in token mode FIRST, before the org block below flips to org mode
            # (where the env invite is disabled).
            tok = orgs.mint_token("user@x.com", store.DEFAULT_ORG, "laptop", con=cc)
            ok, principal, err = auth.identify(auth._CIHeaders({"Authorization": "Bearer " + tok}))
            assert ok and principal["email"] == "user@x.com" and err is None, \
                "per-user token authenticates a corpus route AND carries verified identity"
            assert auth.identify(auth._CIHeaders({"Authorization": "Bearer " + tok,
                                                  "X-CSA-User": "someone.else@x.com"}))[1]["email"] == "user@x.com", \
                "spoofed X-CSA-User does not change the attributed identity"
            ok, principal, err = auth.identify(auth._CIHeaders({"Authorization": "Bearer sekret"}))
            assert ok and principal["email"] is None and err is None, \
                "the admin/compose token still works, unchanged"
            assert auth.identify(auth._CIHeaders())[0] is False, "still no credential -> deny"

            ok_web, web_tok = cliauth.web_login("web@x.com", "sekret")   # invite defaults to CSA_AUTH_TOKEN
            assert ok_web, web_tok
            jar = auth._CIHeaders({"Cookie": cliauth.session_cookie(web_tok).split(";")[0]})
            okc, prc, _ = auth.identify(jar, allow_cookie=True)
            assert okc and prc["email"] == "web@x.com", "the Set-Cookie this server emits authenticates via the cookie"
            assert auth.identify(jar)[0] is False, "...and ONLY where allow_cookie is opted into (never the MCP gate)"

            jar_cookie = cliauth.session_cookie(web_tok).split(";")[0]
            assert apiserver._browser_session(jar) is True, "cookie alone -> browser session"
            assert apiserver._browser_session(auth._CIHeaders({"Authorization": "Bearer " + tok})) is False, "real header client"
            assert apiserver._browser_session(auth._CIHeaders({"Cookie": jar_cookie,
                                                               "Authorization": "Bearer " + tok})) is False, \
                "cookie + a VALID bearer -> the header authenticates it, unrestricted"
            assert apiserver._browser_session(auth._CIHeaders({"Cookie": jar_cookie,
                                                               "Authorization": "Bearer totally-bogus"})) is True, \
                "cookie + a JUNK bearer STILL authenticates off the cookie -> restricted (the bypass)"
            assert apiserver._browser_session(auth._CIHeaders({"Cookie": jar_cookie,
                                                               "X-CSA-Token": "not-a-real-token"})) is True, \
                "the junk-proxy-header variant of the same bypass"
            assert apiserver._browser_session(auth._CIHeaders({"Cookie": jar_cookie,
                                                               "Authorization": "Bearer sekret"})) is False, \
                "cookie + the ADMIN secret (CSA_AUTH_TOKEN='sekret' here) -> a real admin client, unrestricted"
            assert apiserver._browser_session(auth._CIHeaders()) is False

            # blast radius: a browser session may read + rate/curate the corpus, and nothing else
            assert apiserver._COOKIE_POST_OK == {"/api/flag", "/api/hctype", "/api/entity-types",
                                                 "/api/graphql", "/api/track",
                                                 "/api/org/member/role", "/api/org/member/revoke",
                                                 "/api/org/invite/new", "/api/org/invite/revoke",
                                                 "/api/org/service-token/new",
                                                 "/api/org/provider/save", "/api/org/provider/remove",
                                                 "/api/org/models",
                                                 "/api/config"}, \
                "the SPA POSTs exactly these (api.ts)"
            # The invite LINK routes are in and rotate is out, and the line between them is what the
            # response is worth to an XSS holding the cookie. rotate returns the workspace's shared
            # secret: permanent, admits anybody, outlives the session. A link names one address,
            # expires, is listed while pending, is revocable from the same panel, and mints a MEMBER
            # (orgs.create_invite takes no role, and /api/org/member/role can only raise someone who
            # already holds a token). What an XSS can still buy is one outsider seat, which is why
            # orgs.MAX_PENDING_INVITES bounds how many may stand at once.
            assert orgs.MAX_PENDING_INVITES > 0
            for danger in ("/api/forget", "/api/ingest", "/api/ask", "/api/org/invite/rotate"):
                assert danger not in apiserver._COOKIE_POST_OK, "%s must never be reachable from a cookie" % danger
            # /api/graphql is in that set ONLY because it cannot write. A POST is just how the
            # protocol carries a query; the schema defines no mutation type, so the capability a
            # cookie gains is exactly "read the corpus" — the same as the GETs it already has. Add a
            # mutation and this assertion fails, which is the point: it must leave the allowlist.
            from analyzer import gql
            assert gql.schema.mutation is None, \
                "GraphQL gained a mutation: take /api/graphql OUT of _COOKIE_POST_OK (an XSS with a " \
                "session cookie would inherit every mutation it defines)"
            assert "type Mutation" not in gql.sdl()

            # _same_origin: the same-SITE case is the one SameSite=Strict lets through
            H = auth._CIHeaders
            assert apiserver._same_origin(H()) is True, "absent (curl / Go client) -> allowed; CSRF is a browser-only attack"
            assert apiserver._same_origin(H({"Sec-Fetch-Site": "same-origin"})) is True, "our own SPA"
            assert apiserver._same_origin(H({"Sec-Fetch-Site": "none"})) is True, "typed URL / bookmark"
            assert apiserver._same_origin(H({"Sec-Fetch-Site": "Same-Origin"})) is True, "case-insensitive"
            assert apiserver._same_origin(H({"Sec-Fetch-Site": "cross-site"})) is False
            assert apiserver._same_origin(H({"Sec-Fetch-Site": "same-site"})) is False, \
                "a sibling subdomain is same-SITE: Strict still sends the cookie, this is what refuses it"

            orgs.revoke(web_tok, con=cc)
            assert auth.identify(jar, allow_cookie=True)[0] is False, "logout revokes -> the cookie is dead"
            orgs.revoke(tok, con=cc)
            assert auth.identify(auth._CIHeaders({"Authorization": "Bearer " + tok}))[0] is False, "revoked -> deny"

            # --- per-user tokens: identity + ORG ride the credential, and a cross-org read is impossible ---
            a_id, _a_inv = orgs._create_org("Org A", con=cc)   # fixture: bypass the single-tenant guard
            b_id, _b_inv = orgs._create_org("Org B", con=cc)   # fixture: bypass the single-tenant guard
            atok = orgs.mint_token("dev@a.com", a_id, "laptop-a", con=cc)
            btok = orgs.mint_token("dev@b.com", b_id, "laptop-b", con=cc)
            ok, pa, _ = auth.identify(auth._CIHeaders({"Authorization": "Bearer " + atok}))
            assert ok and pa == {"email": "dev@a.com", "org_id": a_id, "role": "member", "service": False}, pa
            # the whole point: two orgs' credentials resolve to two DIFFERENT FILES
            ok, pb, _ = auth.identify(auth._CIHeaders({"Authorization": "Bearer " + btok}))
            assert store.org_db(pa["org_id"]) != store.org_db(pb["org_id"]), "org A and B must not share a corpus file"
            # spoofing: neither header nor payload can move you — org_id comes only from the credential
            _, sp, _ = auth.identify(auth._CIHeaders({"Authorization": "Bearer " + btok,
                                                      "X-CSA-User": "dev@a.com", "X-CSA-Org": a_id}))
            assert sp == {"email": "dev@b.com", "org_id": b_id, "role": "member", "service": False}, \
                "spoofed X-CSA-User/X-CSA-Org must not change the verified identity or org"
            # the shared secret is not a credential on a box that hosts tenants: a 401 naming the fix
            ok, ad, err = auth.identify(auth._CIHeaders({"Authorization": "Bearer sekret"}))
            assert ok is False and ad is None and "csa-client setup" in err, (ad, err)
            assert auth.identify(auth._CIHeaders())[0] is False, "still no credential -> deny"
            # revocation denies, and must NOT fall through to the shared secret
            orgs.revoke(atok, con=cc)
            assert auth.identify(auth._CIHeaders({"Authorization": "Bearer " + atok}))[0] is False, "revoked -> deny"
            assert auth.identify(auth._CIHeaders({"Authorization": "Bearer " + atok,
                                                  "X-CSA-Token": "sekret"}))[0] is False, \
                "a revoked token must deny outright, never fall through to the shared secret"
            assert auth.boot_error("0.0.0.0") is None, "org mode boots multi-tenant"
            ok, pa2, _ = auth.identify(auth._CIHeaders({"Authorization": "Bearer " + btok}))
            assert ok and pa2["org_id"] == b_id, "per-user tokens work in org mode"
        finally:
            orgs.control_db = _real_ctl
    finally:
        for k, v in _saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _fact(con_, sid, day, claim, scope="organizational", ty="gotcha"):
    con_.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
                 "VALUES(?,?,?,'session','a@b.c')", (sid, "p", day))
    con_.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref) "
                 "VALUES(?,?,?,?,'e',?)", (sid, ty, scope, claim, "ref-" + sid))


def test_onboarding_regimes_age_floor_and_scope():
    # --- onboarding: the two ranking regimes, the age floor, and the scope filter ---------------
    # This is the screen a first-time user is judged by, and every one of these was a real bug risk:
    # a usage sort on a cold corpus degenerates to newest-first (showing what they wrote yesterday),
    # an equality test on `scope` silently drops legacy rows, and a receipt that claims a recall that
    # never happened discredits every other receipt on the page.
    _ob = store.sqlite_db(":memory:")      # not a bare sqlite3.connect: the telemetry db (`tel`) is
    #                                        attached by sqlite_db, and onboarding reads tel.served
    _d = apiserver.onboarding(_ob)
    assert _d["cards"] == [] and _d["solo"] is True, "empty corpus must yield no cards, not a crash"
    assert _d["metrics"]["facts"] == 0

    _long = " that is comfortably longer than the forty character floor"
    _fact(_ob, "s-old", "2026-01-01", "oldest claim" + _long)
    _fact(_ob, "s-mid", "2026-03-01", "middle claim" + _long)
    _fact(_ob, "s-new", time.strftime("%Y-%m-%d"), "written today" + _long)
    _c = apiserver.onboarding(_ob)
    assert _c["frame"] == "cold", "nothing served yet -> cold regime"
    assert _c["cards"][0]["learned_at"] == "2026-01-01", "cold must rank OLDEST first, not newest"
    assert all(x["reused"] == 0 for x in _c["cards"]), "cold cards must make no reuse claim"
    assert time.strftime("%Y-%m-%d") not in [x["learned_at"] for x in _c["cards"]], \
        "a fact written today is not forgotten -> excluded by the age floor"

    _fact(_ob, "s-per", "2026-01-02", "a machine-local quirk" + _long, scope="personal")
    _fact(_ob, "s-leg", "2026-01-03", "a legacy row with no scope" + _long, scope=None)
    _txt = " ".join(x["claim"] for x in apiserver.onboarding(_ob, limit=10)["cards"])
    assert "machine-local quirk" not in _txt, "personal scope must never surface to a teammate"
    assert "legacy row with no scope" in _txt, "scope IS NULL is legacy, not personal — must be kept"

    # warm regime: reuse is a fact served to someone other than its author (analytics.reuses). A
    # serve the corpus cannot open still counts, but only a session it holds becomes a receipt link.
    store.refresh_facts(_ob)
    _served = "INSERT INTO served(session,asked_at,question,ref,kind,project,person,asked_by) VALUES(?,?,'q',?,'hidden_context','p','',?)"
    _ob.execute(_served, ("ghost-never-shipped", "2026-04-01", "ref-s-mid", "a@b.c"))
    assert apiserver.onboarding(_ob)["frame"] == "cold", "the author asking again is not a reuse"
    _ob.execute(_served, ("teammate-live", "2026-04-01", "ref-s-mid", "b@b.c"))
    _w = apiserver.onboarding(_ob)
    assert _w["frame"] == "warm", "a serve to a teammate flips the regime"
    assert (_w["cards"][0]["reused"], _w["cards"][0]["people"], _w["cards"][0]["reused_in"]) == (1, 1, []), \
        "counted, but with no receipt: the corpus does not hold that session"
    _ob.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) "
                "VALUES('other-session','p','2026-04-01','2026-04-01T00:00:00Z','session','b@b.c')")
    _ob.execute(_served, ("other-session", "2026-04-02", "ref-s-mid", ""))
    _w = apiserver.onboarding(_ob)
    assert (_w["cards"][0]["reused"], _w["cards"][0]["people"], _w["cards"][0]["reused_in"]) == (2, 1, ["other-session"]), \
        "no asking credential: the receiving session's owner is the receiver"
    _ob.execute(_served, ("s-mid", "2026-04-02", "ref-s-mid", "c@b.c"))
    assert apiserver.onboarding(_ob)["cards"][0]["reused"] == 2, \
        "a fact's OWN session must not count as reuse: that is 'was there', not 'helped later'"
    _ob.close()


def test_onboarding_reused_tile_is_the_analytics_definition():
    con = store.sqlite_db(":memory:")
    _fact(con, "s-old", "2026-01-01", "a gotcha that is comfortably longer than the forty character floor")
    store.refresh_facts(con)
    for sid, who in (("ghost-1", "b@b.c"), ("ghost-2", "c@b.c"), ("ghost-3", "a@b.c"), ("ghost-4", "")):
        con.execute("INSERT INTO served(session,asked_at,question,ref,kind,project,person,asked_by) "
                    "VALUES(?,'2026-04-01','q','ref-s-old','hidden_context','p','',?)", (sid, who))
    d = apiserver.onboarding(con)
    assert d["metrics"]["reused"] == analytics.reused_facts(con) == 1, "one fact, reused: the Welcome tile and Analytics agree"
    assert [(c["reused"], c["people"]) for c in d["cards"]] == [(2, 2)], \
        "its author asking again and a receiver nobody can name are not reuse"
    con.close()


def test_onboarding_people_are_case_blind_and_named():
    con = store.sqlite_db(":memory:")
    for sid, user in (("s1", "Ana@x"), ("s2", "ana@x"), ("s3", "ben@x"), ("s4", None), ("s5", "")):
        con.execute("INSERT INTO sessions(session_id,started_at,source,user) VALUES(?,'2026-01-01','session',?)",
                    (sid, user))
    con.execute("INSERT INTO sessions(session_id,started_at,source,user) VALUES('m1','2026-01-01','memory','cy@x')")
    d = apiserver.onboarding(con)
    assert (d["metrics"]["people"], d["team"], d["metrics"]["sessions"]) == (2, 2, 5), \
        "people counts the way Analytics counts contributors; sessions is every shipped session"
    con.close()


def test_team_rollup_names_an_unattributed_session_as_empty_never_null():
    con = store.sqlite_db(":memory:")
    for sid, user in (("s1", "ana@x"), ("s2", None), ("s3", "")):
        con.execute("INSERT INTO sessions(session_id,started_at,source,user) VALUES(?,'2026-01-01','session',?)",
                    (sid, user))
    rows = apiserver.team(con)
    assert sorted((r["user"], r["sessions"]) for r in rows) == [("", 2), ("ana@x", 1)], \
        "the Team page lower-cases every user; a null one blanked the page"
    con.close()


def test_hook_ask_names_the_missing_field():
    con = store.sqlite_db(":memory:")
    for q in ("", "   ", None):
        assert apiserver.ask(con, q) == {"ok": False, "error": 'missing "query"'}
    con.close()


def test_improve_is_owner_private_server_side():
    # --- Improve is per-owner private, and the rule is enforced HERE, not in the UI --------------
    # A person picker shipped on that page, so any teammate could read any other teammate's cost,
    # failed-call count, idle time and touched file paths by name. `?owner=<someone else>` is one curl
    # away, so a UI-only fix would have been theatre.
    # CSA_AUTH is pinned to the self-check's ambient mode ("off", the loopback dev default) so the
    # nameless-caller branch is deterministic no matter what env the suite runs under.
    _auth_saved = os.environ.get("CSA_AUTH")
    os.environ["CSA_AUTH"] = "off"
    try:
        _pv = store.sqlite_db(":memory:")  # sqlite_db, for `tel` — see the note above
        for _u, _sid in (("me@x.com", "s-me"), ("them@x.com", "s-them")):
            _pv.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) "
                        "VALUES(?,?,'2026-01-01','2026-01-01T00:00:00Z','session',?)", (_sid, "p", _u))
            _pv.execute("INSERT INTO session_metrics(session_id,cost_usd,tool_calls) VALUES(?,?,?)",
                        (_sid, 9.0, 10))
        _mine = apiserver.improvements(_pv, owner="them@x.com", caller="me@x.com", org_admin=False)
        assert [r["user"] for r in _mine["rows"]] == ["me@x.com"], \
            "a named caller asking for someone else's rows must get their OWN: " + repr(_mine["rows"])
        assert _mine["can_widen"] is False
        _all = apiserver.improvements(_pv, caller="", org_admin=True)
        assert len({r["user"] for r in _all["rows"]}) == 2, "a workspace admin may see their whole org"
        assert _all["can_widen"] is True
        _self = apiserver.improvements(_pv, caller="", org_admin=False)
        assert len(_self["rows"]) == 2, \
            "in CSA_AUTH=off there is one user of that box — an unfiltered view is still their own data"
        # ...but in org mode a caller the gate could not name must get NOTHING: falling through to
        # owner="" (unfiltered) would hand it every person's cost, idle time and file paths.
        _mode, auth.current_mode = auth.current_mode, lambda: "org"
        try:
            _anon = apiserver.improvements(_pv, caller="", org_admin=False)
            assert _anon["rows"] == [] and _anon["unnamed"] is True, \
                "an unnamed non-admin caller in a named auth mode must see nothing: " + repr(_anon)
            _anon2 = apiserver.improvements(_pv, owner="them@x.com", caller="", org_admin=False)
            assert _anon2["rows"] == [], "...and must not be able to ask for a named person either"
        finally:
            auth.current_mode = _mode
        _pv.close()
    finally:
        os.environ.pop("CSA_AUTH", None) if _auth_saved is None \
            else os.environ.__setitem__("CSA_AUTH", _auth_saved)


def test_fresh_empty_tenant_does_not_502():
    # A BRAND-NEW tenant must not 502. `docs` (fts5) is created by build_fts, which never runs when
    # there are zero sessions — so /api/corpus/stats (it counts docs) and /api/facts?q= (it joins docs)
    # both raised OperationalError, i.e. the Sessions tab was broken on arrival and the first search
    # anyone ran on a fresh install returned a Bad Gateway. store.sqlite_db now creates it empty.
    _fresh = store.sqlite_db(":memory:")          # explicit ":memory:" — sqlite_db() defaults to the box corpus
    apiserver.corpus_stats(_fresh)                # must not raise
    assert apiserver.facts(_fresh, q="anything")["total"] == 0, "a fresh tenant searches to empty, not to a 502"
    assert apiserver.onboarding(_fresh, limit=3)["cards"] == [], "a fresh tenant has no onboarding cards"
    assert apiserver.graph(_fresh)["sessions"] == [] and apiserver.graph(_fresh)["window"]["total"] == 0
    assert apiserver.facts(_fresh)["rules"] == []
    store.build_fts(_fresh)                       # and build_fts must still work on the pre-created table
    assert apiserver.facts(_fresh, q="anything")["total"] == 0
    _fresh.close()


def test_forget_recomputes_and_removes_facts():
    """schema v2 E2E over the forget route (ported from #70's _self_check additions): forgetting a
    fact's earliest asserting session recomputes its dates from the remaining mentions; forgetting
    the last one removes the fact row with its edges (both directions)."""
    con = store.sqlite_db(":memory:")
    for s, u, day in (("fgA", "early@x", "2026-06-01"), ("fgB", "late@x", "2026-07-01")):
        store.load_sqlite(con, dict(session_id=s, project="p", git_branch=None, started_at=f"{day}T09:00:00Z",
                                    ended_at=f"{day}T10:00:00Z", n_user=1, n_asst=1, user=u, source="session"),
                          {"hidden_context": [{"type": "decision", "claim": "forget recompute fixture",
                                               "evidence": "e"}]})
    _fr = store.hc_ref("forget recompute fixture")
    con.execute("INSERT INTO fact_edges VALUES ('winner-ref','supersedes',?, '2026-07-20','t','receipt')", (_fr,))
    con.commit()
    assert apiserver.forget(con, "fgA")["ok"]
    assert con.execute("SELECT author, created_at FROM facts WHERE ref=?", (_fr,)).fetchone() == \
        ("late@x", "2026-07-01T10:00:00Z"), "forget must recompute fact author/dates from remaining mentions"
    assert apiserver.forget(con, "fgB")["ok"]
    assert not con.execute("SELECT 1 FROM facts WHERE ref=?", (_fr,)).fetchone(), \
        "a fact with zero remaining mentions must be removed by forget"
    assert not con.execute("SELECT 1 FROM fact_edges WHERE src_ref=? OR dst_ref=?", (_fr, _fr)).fetchone(), \
        "…with its edges (both directions)"


def test_workspace_request_public_and_rate_limited(monkeypatch):
    # PRE-AUTH by design: the route joins _PUBLIC_EXACT — never do_POST's tuple or _COOKIE_POST_OK
    # (either of those would 401 it before it runs; the task doc's recorded trap).
    assert apiserver._public("/api/workspace-request")
    assert "/api/workspace-request" not in apiserver._COOKIE_POST_OK
    # the limiter is process state — replace it wholesale so this test is order-independent
    monkeypatch.setattr(apiserver, "_WSREQ_HITS", {})
    monkeypatch.setattr(apiserver, "_WSREQ_GLOBAL", apiserver.deque())
    monkeypatch.setattr(apiserver, "WSREQ_PER_IP", 3)
    monkeypatch.setattr(apiserver, "WSREQ_GLOBAL", 5)
    assert [apiserver._wsreq_limited("a") for _ in range(4)] == [False, False, False, True], \
        "the 4th hit from one key inside the window is refused"
    assert apiserver._wsreq_limited("b") is False, "another key has its own bucket"
    assert apiserver._wsreq_limited("c") is False
    assert apiserver._wsreq_limited("d") is True, "the GLOBAL window bounds spoofed/rotating keys"
    assert "d" not in apiserver._WSREQ_HITS, "a globally-refused first hit must not leave an empty bucket"


def test_workspace_request_webhook_notice(monkeypatch, tmp_path):
    # the admin notice text carries what an operator needs: the id (to approve), company, and email
    txt = apiserver._webhook_text("a1b2c3d4", "Acme Corp", "dev@acme.com")
    assert "a1b2c3d4" in txt and "Acme Corp" in txt and "dev@acme.com" in txt
    assert "org approve a1b2c3d4" in txt, "the notice hands the operator the exact approve command"
    # the webhook is read in ONE place, per request, never cached: unset is "", set is the URL
    monkeypatch.chdir(tmp_path)
    assert apiserver._request_webhook() == "", "no control.db yet reads as unset, and creates nothing"
    assert not (tmp_path / "data" / "control.db").exists()
    tenant.set_deployment(orgs.control_db(), request_webhook="https://hooks.example.com/x")
    assert apiserver._request_webhook() == "https://hooks.example.com/x"
    # fail-open: a dead webhook is logged in its thread, never raised to the request
    assert apiserver._notify_new_request("http://127.0.0.1:9/hook", "a1b2c3d4", "Acme Corp", "dev@acme.com") is None


def test_content_length_never_raises():
    # a junk Content-Length used to escape as a traceback that dropped the connection (prober finding)
    from email.message import Message
    for raw, want in (("12", 12), ("abc", 0), ("", 0), ("-5", 0), (None, 0)):
        h = Message()
        if raw is not None:
            h["Content-Length"] = raw
        assert apiserver._content_length(h) == want


def test_run_query_refuses_an_empty_document_as_a_graphql_error():
    # an empty or missing query used to raise strawberry's MissingQueryError out of do_POST (prober finding)
    for q in ("", "   ", None, 7):
        out = gql.run_query(q, None, con=None, principal={}, org_id="x")
        assert out["data"] is None
        assert out["errors"][0]["message"] == "query must be a non-empty string"


def _ago(days, hour=9):
    """An ISO stamp `days` days before now. The graph window is relative to the server's clock, so a
    fixture with hardcoded dates would go red on an unchanged tree once the calendar passed it."""
    return time.strftime(f"%Y-%m-%dT{hour:02d}:%M:%SZ", time.gmtime(time.time() - days * 86400))


def _pr2_corpus():
    """Five sessions: three infra sessions (two mine, one theirs) that each re-read one file so the project
    earns a rule; a billing session with a $0.00 metrics row; an old billing session outside 30 days with
    no metrics. One fact is corroborated across a1 and a2 and recalled into a3; the old fact is recalled
    into b1."""
    con = store.sqlite_db(":memory:")
    for sid, user, proj, ago in (("a1", "me@x.com", "infra", 8), ("a2", "me@x.com", "infra", 6),
                                 ("a3", "them@x.com", "infra", 4), ("old", "them@x.com", "billing", 120),
                                 ("b1", "them@x.com", "billing", 2)):
        con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,n_user,n_asst,summary,source,user) "
                    "VALUES(?,?,?,?,4,8,?,'session',?)", (sid, proj, _ago(ago), _ago(ago, 10), sid + " summary", user))
    for sid in ("a1", "a2", "a3"):
        con.execute("INSERT INTO session_metrics(session_id,model,tool_calls,reread_calls,top_read,top_read_n,cost_usd,wall_s,idle_s) "
                    "VALUES(?,'claude-opus-5',40,3,'/x/deploy.py',5,1.5,3000,100)", (sid,))
    con.execute("INSERT INTO session_metrics(session_id,model,cost_usd,tool_calls) VALUES('b1','claude-opus-5',0.0,3)")
    for sid, k in (("a1", 0), ("a1", 1), ("a2", 0), ("a3", 0), ("old", 0), ("b1", 0)):
        con.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref,confidence) VALUES(?,?,?,?,?,?,?)",
                    (sid, "gotcha", "shared", f"claim {sid} {k}", "quote", f"r:{sid}:{k}", "high"))
    con.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref,confidence) "
                "VALUES('a2','gotcha','shared','claim a1 0','quote','r:a1:0','high')")
    for sess, at, ref in (("a3", _ago(4, 11), "r:a1:0"), ("a1", _ago(8, 11), "r:a1:0"),
                          ("b1", _ago(2, 11), "r:old:0")):
        con.execute("INSERT INTO tel.served(session,asked_at,question,ref,kind) VALUES(?,?,'q',?,'hidden_context')", (sess, at, ref))
    con.commit()
    store.refresh_facts(con)
    store.build_fts(con)
    con.commit()
    return con


def test_row_dicts_are_keyed_by_the_statement_not_a_parallel_list():
    con = store.sqlite_db(":memory:")
    con.execute("INSERT INTO sessions(session_id,project,user,source) VALUES('s1','p','u@x','session')")
    con.commit()
    s = apiserver._S
    want = {"session_id": "s1", "project": "p", "user": "u@x"}
    assert apiserver._dicts(con, select(s.c.session_id, s.c.project, s.c.user)) == [want]
    assert apiserver._dicts(con, select(s.c.user, s.c.session_id, s.c.project)) == [want], \
        "reordering the select must move the values, never the labels"
    assert apiserver._dicts(con, select(s.c.project.label("proj"))) == [{"proj": "p"}], "a label is the key"
    con.close()


def test_corpus_sessions_cost_is_joined_and_gated(monkeypatch):
    con = _pr2_corpus()
    monkeypatch.setattr(auth, "current_mode", lambda: "org")
    me = {"email": "me@x.com", "role": "member"}
    rows = {r["session_id"]: r["cost_usd"] for r in apiserver.corpus_sessions(con, principal=me)}
    assert rows["a1"] == 1.5 and rows["a2"] == 1.5, "my own measured sessions carry their cost"
    assert rows["a3"] is None and rows["b1"] is None, "a teammate's cost is not mine to see"
    assert rows["old"] is None, "no metrics row -> null, never 0"
    admin = {"email": "boss@x.com", "role": "admin"}
    rows = {r["session_id"]: r["cost_usd"] for r in apiserver.corpus_sessions(con, principal=admin)}
    assert rows["a3"] == 1.5 and rows["b1"] == 0.0 and rows["old"] is None, "an admin sees every measured cost, and a real $0.00 stays 0.0"
    assert apiserver.corpus_session(con, "a3", me)["metrics"] is None, "the strip is absent on another person's session"
    assert apiserver.corpus_session(con, "a1", me)["metrics"] == {"cost_usd": 1.5, "tool_calls": 40, "wasted": 3, "wall_s": 3000, "idle_s": 100}
    assert apiserver.corpus_session(con, "old", admin)["metrics"] == {"cost_usd": None, "tool_calls": None, "wasted": None, "wall_s": None, "idle_s": None}, \
        "allowed but never measured: an object of nulls, so the UI can draw the strip with dots"
    con.execute("INSERT INTO session_metrics(session_id,model,cost_usd) VALUES('old','claude-opus-5',2.0)")   # a row whose waste columns are NULL
    con.commit()
    assert apiserver.corpus_session(con, "old", admin)["metrics"]["wasted"] is None, \
        "nothing counted is unknown, not zero"
    q = gql.run_query("{ sessions(limit: 10) { items { sessionId costUsd } } }", con=con, principal=me, org_id="default", auth_mode="org")
    got = {i["sessionId"]: i["costUsd"] for i in q["data"]["sessions"]["items"]}
    assert got["a1"] == 1.5 and got["a3"] is None and got["old"] is None, "the GraphQL list the UI reads carries the same gated figure"
    con.close()


def test_a_session_cost_is_shown_only_when_its_model_has_a_list_price(monkeypatch):
    con = _pr2_corpus()
    monkeypatch.setattr(auth, "current_mode", lambda: "org")
    con.execute("UPDATE session_metrics SET model='llama-4-maverick' WHERE session_id='a2'")
    con.commit()
    me = {"email": "me@x.com", "role": "member"}
    assert improve.price("llama-4-maverick") is None and improve.price("claude-sonnet-4-5-20250929") == (3.0, 15.0)
    rows = {r["session_id"]: r["cost_usd"] for r in apiserver.corpus_sessions(con, principal=me)}
    assert (rows["a1"], rows["a2"]) == (1.5, None), "a stored estimate for an unpriced model is not shown"
    assert apiserver.corpus_session(con, "a2", me)["metrics"]["cost_usd"] is None
    assert {r["session_id"]: r["cost_usd"] for r in apiserver.improvements(con, caller="me@x.com")["rows"]}["a2"] is None
    q = gql.run_query("{ sessions(limit: 10) { items { sessionId costUsd } } }", con=con, principal=me, org_id="default", auth_mode="org")
    assert {i["sessionId"]: i["costUsd"] for i in q["data"]["sessions"]["items"]}["a2"] is None
    con.close()


def test_csa_spend_through_a_chat_provider_is_priced_by_the_same_table(monkeypatch):
    class _Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}).encode()

    monkeypatch.setattr(llm, "_open", lambda req, timeout: _Resp(req))
    assert llm._chat("https://x/v1/chat/completions", {}, {"model": "llama-4-maverick", "messages": []}, 5)[1] is None, \
        "no list price: the ledger records tokens and no cost"
    assert llm._chat("https://x/v1/chat/completions", {}, {"model": "claude-sonnet-4-5", "messages": []}, 5)[1] == 3.0
    assert llm._chat("https://x/v1/chat/completions", {}, {"model": "gemini-2.5-flash", "messages": []}, 5)[1] == 0.3


@pytest.mark.parametrize("model, rates", [
    ("gemini-2.5-pro", (1.25, 10.0)),
    ("gemini-2.5-flash", (0.30, 2.50)),
    ("gemini-2.5-flash-lite", (0.10, 0.40)),
    ("gemini-2.5-flash-preview-09-2025", (0.30, 2.50)),
    ("gemini-3.1-pro-preview", (2.0, 12.0)),
    ("gpt-5", (1.25, 10.0)),
    ("gpt-5-2025-08-07", (1.25, 10.0)),
    ("gpt-5-mini", (0.25, 2.0)),
    ("gpt-5-mini-2025-08-07", (0.25, 2.0)),
    ("gpt-5-pro", (15.0, 120.0)),
    ("gpt-4o-mini-2024-07-18", (0.15, 0.60)),
    ("o3-mini", (1.10, 4.40)),
    ("claude-sonnet-4-5@20250929", (3.0, 15.0)),
    ("gpt-5.3", None),
    ("gpt-50", None),
    ("o3x", None),
    ("llama-4-maverick", None),
    ("", None),
])
def test_a_model_is_priced_by_its_longest_family(model, rates):
    assert improve.price(model) == rates


def test_known_cost_in_sql_agrees_with_price():
    con = _pr2_corpus()
    models_ = ["gemini-2.5-flash-lite", "gpt-5-2025-08-07", "gpt-5.3", "o3x", "claude-opus-4-8", "llama-4-maverick"]
    for i, m in enumerate(models_):
        con.execute("INSERT INTO session_metrics(session_id,model,cost_usd) VALUES(?,?,1.0)", (f"k{i}", m))
    m = models.SessionMetrics.__table__
    got = dict(db.run(con, select(m.c.model, improve.known_cost(m)).where(m.c.session_id.like("k%"))).fetchall())
    assert got == {x: (1.0 if improve.price(x) else None) for x in models_}


def test_an_azure_deployment_is_priced_by_the_model_it_reports(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"model": "gpt-4o-2024-08-06", "choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}).encode()

    monkeypatch.setattr(llm, "_open", lambda req, timeout: _Resp())
    assert llm._chat("https://x/openai/deployments/prod/chat/completions", {}, {"messages": []}, 5)[1] == 2.5


def test_csa_spend_through_a_direct_anthropic_key_is_priced_by_the_same_table(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "ok"}],
                               "usage": {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
                                         "cache_creation_input_tokens": 0, "output_tokens": 0}}).encode()

    monkeypatch.setattr(llm, "_open", lambda req, timeout: _Resp())
    assert llm._anthropic("hi", "claude-sonnet-4-5", 5, "k", "https://x")[1] == 3.3, \
        "fresh input at list, cache reads at 0.1x, never unpriced for a model the table knows"


def test_facts_rules_ride_the_first_page(monkeypatch):
    con = _pr2_corpus()
    monkeypatch.setattr(auth, "current_mode", lambda: "org")
    me = {"email": "me@x.com", "role": "member"}
    page = apiserver.facts(con, principal=me)
    assert page["total"] == 6, "rules are counted apart from the facts"
    assert [r["ref"] for r in page["rules"]] == ["rule:infra:rereads"]
    rule = page["rules"][0]
    assert rule["type"] == "rule" and rule["project"] == "infra" and rule["evidence"] == "3 of 3 sessions"
    assert rule["claim"].startswith("Rereads. Read a file once") and "deploy.py" not in rule["claim"], \
        "the claim is the static instruction, never one session's file or dollar figure"
    assert rule["session_id"] == "" and rule["user"] is None and rule["corroboration"] == 3
    assert apiserver.facts(con, ftype="rule", principal=me)["rows"] == [] and len(apiserver.facts(con, ftype="rule", principal=me)["rules"]) == 1
    assert apiserver.facts(con, offset=1, principal=me)["rules"] == [], "first page only"
    assert apiserver.facts(con, q="claim", principal=me)["rules"] == [] and apiserver.facts(con, label="gold", principal=me)["rules"] == []
    assert apiserver.facts(con, ftype="gotcha", principal=me)["rules"] == []
    assert apiserver.facts(con, owner="them@x.com", principal=me)["rules"] == [], "One teammate: no findings computed on a named person"
    assert apiserver.facts(con, owner="me@x.com", principal=me)["rules"] == [], "Just mine: 2 of my sessions is under the 3-session floor"
    assert apiserver.rule_facts(con, owner="them@x.com", principal=me) == [], "the gate is in rule_facts, not only in its caller"
    con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,n_user,n_asst,summary,source,user) "
                "VALUES('a4','infra',?,?,4,8,'a4 summary','session','me@x.com')", (_ago(1), _ago(1, 10)))
    con.execute("INSERT INTO session_metrics(session_id,tool_calls,reread_calls,top_read,top_read_n,cost_usd) VALUES('a4',40,3,'/x/deploy.py',5,1.5)")
    con.commit()
    assert [r["ref"] for r in apiserver.facts(con, owner="me@x.com", principal=me)["rules"]] == ["rule:infra:rereads"], \
        "Just mine, with three of my own sessions in one project: the rule is mine to see"
    for extra, ago in (("t2", 5), ("t3", 3)):     # give them@x.com three qualifying sessions of their own
        con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,n_user,n_asst,summary,source,user) "
                    "VALUES(?,'infra',?,?,4,8,'t summary','session','them@x.com')", (extra, _ago(ago), _ago(ago, 10)))
        con.execute("INSERT INTO session_metrics(session_id,tool_calls,reread_calls,top_read,top_read_n,cost_usd) "
                    "VALUES(?,40,3,'/x/deploy.py',5,1.5)", (extra,))
    con.commit()
    assert apiserver.facts(con, owner="them@x.com", principal=me)["rules"] == [], \
        "a member scoping to a teammate who HAS a rule still gets none: that is their metrics"
    assert [r["ref"] for r in apiserver.facts(con, owner="them@x.com", principal={"email": "boss@x.com", "role": "admin"})["rules"]] == \
        ["rule:infra:rereads"], "an admin may scope to a person"
    q = gql.run_query("{ facts(limit: 2) { totalCount rules { ref type claim evidence project corroboration } } }",
                      con=con, principal=me, org_id="default", auth_mode="org")
    assert q["data"]["facts"]["totalCount"] == 6 and q["data"]["facts"]["rules"][0]["ref"] == "rule:infra:rereads"
    con.close()


def test_graph_shape_window_and_scope(monkeypatch):
    con = _pr2_corpus()
    monkeypatch.setattr(auth, "current_mode", lambda: "org")
    me = {"email": "me@x.com", "role": "member"}
    g = apiserver.graph(con, principal=me)
    assert set(g) == {"window", "sessions", "facts", "recalls"}
    assert g["window"] == {"days": 30, "limit": 60, "facts_budget": 600, "since": g["window"]["since"], "total": 4}
    assert g["window"]["since"].endswith("Z") and "T" in g["window"]["since"]
    assert [x["session_id"] for x in g["sessions"]] == ["b1", "a3", "a2", "a1"], "newest first; the 06-01 session is outside 30 days"
    assert set(g["sessions"][0]) == {"session_id", "title", "project", "user", "started_at", "n_facts"}
    assert {x["session_id"]: x["n_facts"] for x in g["sessions"]} == {"b1": 1, "a3": 1, "a2": 2, "a1": 2}
    homes = {f["ref"]: f["session_id"] for f in g["facts"]}
    assert homes["r:a1:0"] == "a1", "a corroborated fact has one node, at the earliest window session"
    assert set(g["facts"][0]) == {"ref", "type", "claim", "evidence", "session_id", "recalled"}
    assert {f["ref"]: f["recalled"] for f in g["facts"]}["r:a1:0"] == 1, "a self-recall is not a reuse"
    assert homes["rule:infra:rereads"] == "a3", "a rule hangs off the project's newest window session, not the one that earned it"
    assert [(r["ref"], r["session_id"]) for r in g["recalls"]] == [("r:a1:0", "a3")], \
        "a self-recall and a recall of a fact outside the window are not edges"
    wide = apiserver.graph(con, days=200, principal=me)
    assert "old" in [x["session_id"] for x in wide["sessions"]] and len(wide["recalls"]) == 2
    assert [x["session_id"] for x in apiserver.graph(con, limit=1, principal=me)["sessions"]] == ["b1"]
    assert apiserver.graph(con, days="zz", limit=-4, principal=me)["window"]["days"] == 30
    assert apiserver.graph(con, days=99999, limit=99999, principal=me)["window"] | {"since": ""} == \
        {"days": 3650, "limit": 120, "facts_budget": 600, "since": "", "total": 5}
    budget = apiserver.graph(con, facts_budget=2, principal=me)
    assert [x["session_id"] for x in budget["sessions"]] == ["b1", "a3"], "the facts budget stops adding sessions, never trims a session's facts"
    them = apiserver.graph(con, owner="them@x.com", principal=me)
    assert [x["session_id"] for x in them["sessions"]] == ["b1", "a3"]
    assert {f["ref"] for f in them["facts"]} == {"r:a3:0", "r:b1:0"}, "a member scoped to one teammate sees no third person's fact"
    assert them["recalls"] == [], "the recall of my fact into their session is not drawn: its node is out of scope"
    mine = apiserver.graph(con, owner="me@x.com", principal=me)
    assert mine["recalls"] == [] and {f["ref"]: f["recalled"] for f in mine["facts"]}["r:a1:0"] == 1, \
        "scoped to me, the edge into their session is not drawn but the count still says it was reused"
    assert apiserver.graph(con, owner="nobody@x.com", principal=me)["sessions"] == []
    con.execute("INSERT INTO sessions(session_id,project,n_user,n_asst,summary,source,user) "
                "VALUES('undated','infra',4,8,'undated summary','session','me@x.com')")
    con.commit()
    undated = apiserver.graph(con, principal=me)
    assert "undated" in [x["session_id"] for x in undated["sessions"]], \
        "a session with no timestamp cannot be placed in time, so no window may silently drop it"
    assert undated["sessions"][-1]["session_id"] == "undated", "...and it sorts last, not first"
    con.close()


def test_ask_strict_false_falls_back_only_when_asked(monkeypatch, tmp_path):
    # strict=True is the prompt hook's floor. A deliberate caller (an agent, a skill, a human) gets
    # strict=false: strict, then lenient, then raw lexical, then an honest miss that names
    # the unknown words. Jules's real query: three content terms, no single fact carries all three, so
    # strict abstains — but the top facts together cover half the words, so lenient answers.
    monkeypatch.chdir(tmp_path)                    # recall() opens its own con via store.org_db()
    con = store.sqlite_db(store.org_db())
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES "
                "('s1','ledger','2026-08-07T00:00:00Z','session','jules@example.com')")
    for ref, claim in (
            ("ra", "CORE-Bench benchmark: Qwen3-Embedding-8B drops from 71.7 to 20.3 nDCG on issue localization"),
            ("rb", "Rejected mean-of-k query embedding: the models are bit-deterministic so the gain is zero"),
            ("rw", "graph.db opens with WAL and a single writer queue to avoid SQLITE_BUSY under readers")):
        con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES ('s1','decision',?,?,'')",
                    (ref, claim))
    con.commit()
    store.build_fts(con)
    q = "measurements about embedding models"

    hook = apiserver.ask(con, q, asked_by="jules@example.com")
    assert hook["found"] is False and hook["tier"] == "miss", hook
    assert hook["message"].startswith("No strong past-session context"), "the hook path is untouched"

    out = apiserver.ask(con, q, asked_by="jules@example.com", strict=False)
    assert out["found"] is True and out["tier"] == "lenient", (out["tier"], out.get("message"))
    assert "id:ra" in out["context"] or "id:rb" in out["context"]

    strict = apiserver.ask(con, "graph.db WAL single writer queue", strict=False)
    assert strict["found"] is True and strict["tier"] == "strict", "the fallback never runs when strict already fired"

    miss = apiserver.ask(con, "zzzqqq wwwxxx", strict=False)
    assert miss["found"] is False and miss["tier"] == "miss"
    assert miss["coverage"]["missing"] == ["wwwxxx", "zzzqqq"], miss["coverage"]
    assert "real miss, not an outage" in miss["message"]

    rows = con.execute("SELECT engine, planned_query FROM queries ORDER BY rowid").fetchall()
    assert [r[0] for r in rows] == ["hook", "fallback", "fallback", "fallback"], "explicit asks log in their own bucket"
    assert "tier=miss" in rows[0][1] and "tier=lenient" in rows[1][1] and "tier=strict" in rows[2][1]
    con.close()


def test_ask_fallback_raw_lexical_tier(monkeypatch, tmp_path):
    # When even the lenient floor abstains (one word matched out of four), the raw lexical tier still
    # returns the fact, labeled so the caller knows no relevance floor was cleared. Unknown two-letter
    # particles do not stop it; one unknown topic word does.
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES "
                "('s1','ledger','2026-08-07T00:00:00Z','session','jules@example.com')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s1','gotcha','ra','the duckdb generation pointer rolled twice mid-session','')")
    con.commit()
    store.build_fts(con)
    out = apiserver.ask(con, "duckdb ux qa hr", strict=False)
    assert out["found"] is True and out["tier"] == "facts", (out["tier"], out.get("message"))
    assert out["context"].startswith('# Recalled context for') and "## Gotchas" in out["context"]
    assert "id:ra" in out["context"] and "no relevance floor was cleared" in out["context"]
    assert out["coverage"]["missing"] == ["hr", "qa", "ux"]
    miss = apiserver.ask(con, "duckdb qqq1 qqq2 qqq3", strict=False)
    assert miss["found"] is False and miss["tier"] == "miss", miss
    assert "never seen: qqq1, qqq2, qqq3" in miss["message"], miss["message"]
    con.close()


def _ops_corpus():
    """User-sim 2026-10-19: no fact carries three of a question's words, so the strict floor abstains and
    the lenient one decides. One fact says "policy", one says "leave"; none is about people."""
    con = store.sqlite_db(store.org_db())
    for sid, user, ago, claims in (
            ("a1", "alice@trial.test", 12, [
                ("decision", "Transferring the repo to the company org keeps the branch protection policy"),
                ("gotcha", "Postgres database pool is sized to 20 per pod")]),
            ("a2", "alice@trial.test", 0, [
                ("gotcha", "Connection drops came from the load balancer reaping idle sockets"),
                ("decision", "Timeouts were fixed by TCP keepalive on the db driver"),
                ("decision", "Handle retries in the client with jittered backoff")]),
            ("b1", "bob@trial.test", 1, [
                ("decision", "Feature flags leave the old code path in place until cleanup"),
                ("gotcha", "The hooks were missing from settings.json")]),
            ("b2", "bob@trial.test", 0, [
                ("decision", "Use SQLite with WAL for the cache layer")]),
            ("g1", "agent@trial.test", 1, [
                ("gotcha", "Auth tokens expire after 30 idle days")])):
        store.load_sqlite(con, dict(session_id=sid, project="ops", git_branch="main",
                                    started_at=_ago(ago, 9), ended_at=_ago(ago, 10),
                                    n_user=3, n_asst=3, user=user),
                          {"hidden_context": [{"type": t, "claim": c, "evidence": c} for t, c in claims]})
    con.commit()
    store.build_fts(con)
    return con


def test_ask_answers_off_topic_questions_with_an_honest_miss(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    con = _ops_corpus()
    vacation = apiserver.ask(con, "what is our vacation policy?", strict=False)
    assert vacation["found"] is False and vacation["tier"] == "miss", vacation.get("context")
    assert vacation["coverage"]["missing"] == ["vacation"], vacation["coverage"]
    assert "Words the corpus has never seen: vacation." in vacation["message"], vacation["message"]

    tier, md, coverage = apiserver.below_floor(con, "what is our parental leave policy?", "", 6, None, None)
    assert tier == "miss" and coverage["missing"] == ["parental"], (tier, md)
    assert "Words the corpus has never seen: parental." in md, md
    assert apiserver.facts(con, q="what is our parental leave policy?")["total"] == 2, \
        "the words it does know still match: the miss is the tier's choice, not an empty search"

    legit = apiserver.ask(con, "how did we handle database connection timeouts?", strict=False)
    assert legit["found"] is True and legit["tier"] == "lenient", (legit["tier"], legit.get("message"))
    assert legit["coverage"]["missing"] == [], legit["coverage"]

    filler = apiserver.ask(con, "how did we fix the flaky database connection timeouts", strict=False)
    assert filler["found"] is True and filler["tier"] == "lenient", (filler["tier"], filler.get("message"))
    assert filler["coverage"]["missing"] == ["flaky"], "one unknown word among several known ones still answers"

    for q in ("auth pool", "can you explain the pool"):
        out = apiserver.ask(con, q, strict=False)
        assert out["found"] is True, ("a synonym the corpus lacks and a filler word are never unseen", q, out)
        assert out["tier"] in ("lenient", "facts"), (q, out["tier"])

    named = apiserver.ask(con, "what has bob been working on?", strict=False)
    assert named["found"] is True and "Feature flags leave" in named["context"], named.get("message")
    assert "bob" in named["coverage"]["missing"], "a teammate's name is in no fact, and is never the miss"

    hook = apiserver.ask(con, "what is our vacation policy?", session="s", asked_by="eve@trial.test")
    assert hook["tier"] == "miss" and hook["message"].startswith("No strong past-session context"), \
        "the hook's strict tier is untouched"
    con.close()


def test_ask_a_date_word_never_makes_an_off_topic_question_answerable(monkeypatch, tmp_path):
    # Live on onprem-2bf5981: "today" turned any question into today's facts on the strict tier, and the
    # hook injected them into an unrelated prompt. The words left after the date pass the unseen-word rule.
    monkeypatch.chdir(tmp_path)
    con = _ops_corpus()
    for q, unseen in (("what is the lunch menu at the office today?", ["lunch", "menu", "office"]),
                      ("what is the weather today?", ["weather"]),
                      ("what is the lunch menu today?", ["lunch", "menu"]),
                      ("what is the lunch menu at the office?", ["lunch", "menu", "office"])):
        out = apiserver.ask(con, q, strict=False)
        assert out["found"] is False and out["tier"] == "miss", (q, out.get("context"))
        assert set(unseen) <= set(out["coverage"]["missing"]), (q, out["coverage"])
        assert "today" not in out["coverage"]["missing"], "a date is a filter, never an unseen word"
        named = out["message"].split("never seen: ")[1].split(".")[0].split(", ")
        assert set(unseen) <= set(named) and "today" not in named, out["message"]

    window = apiserver.ask(con, "what did we do today?", strict=False)
    assert window["found"] is True and window["tier"] == "strict" and window["n"] == 4, window.get("message")
    assert "Use SQLite with WAL" in window["context"] and "Feature flags" not in window["context"], \
        "a pure time-window ask lists everyone's facts in the window, and only those"
    assert window["coverage"] == {"missing": [], "distinctive": [], "terms": 0,
                                  "dismissed": ["did", "do", "today", "we", "what"]}, window["coverage"]

    topical = apiserver.ask(con, "how did we fix the pool timeout today?", strict=False)
    assert topical["found"] is True and topical["tier"] == "lenient", (topical["tier"], topical.get("message"))
    assert "keepalive" in topical["context"], topical["context"]

    bob = apiserver.ask(con, "what did bob do yesterday?", strict=False)
    assert bob["found"] is True and "Feature flags leave" in bob["context"], bob.get("message")
    assert "Use SQLite with WAL" not in bob["context"], "yesterday, not today"

    for q in ("what changed today?", "what was decided today?", "summarize today",
              "what did the team ship today?"):
        out = apiserver.ask(con, q, strict=False)
        assert out["found"] is True and out["n"] == 4, ("a recap of the window names no topic", q, out.get("message"))

    dated = apiserver.ask(con, "pool sizing today", strict=False)
    assert "Postgres database pool" not in (dated.get("context") or ""), \
        "a word match is dateless: the lexical tier never answers a dated question with an older fact"
    latest = apiserver.ask(con, "what is the latest vacation from today", strict=False)
    assert latest["found"] is False and "never seen: vacation." in latest["message"], latest["message"]
    con.close()


def test_hook_stays_silent_when_a_date_is_the_only_match(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    con = _ops_corpus()
    hook = lambda q: apiserver.ask(con, q, session="s", asked_by="eve@trial.test")
    for q in ("what is the weather today?", "lets refactor the css for the login button today",
              "what is the weather this week?", "what is the lunch menu yesterday?",
              "what is the weather in the last 3 days?", "thanks, that's all for today",
              "lets keep working on this today"):
        out = hook(q)
        assert out["found"] is False and out["tier"] == "miss", (q, out.get("context"))
    for q, n in (("what did we do today?", 4), ("what did we do this week?", 6), ("what happened yesterday?", 3)):
        out = hook(q)
        assert out["found"] is True and out["tier"] == "strict" and out["n"] == n, (q, out.get("message"))
        assert not {"today", "week", "yesterday"} & set(out["coverage"]["missing"]), \
            ("the strict tier reports coverage, the date dismissed", q, out["coverage"])
    con.close()


def test_facts_search_scopes_to_the_teammate_it_names(monkeypatch, tmp_path):
    # User-sim 2026-10-19: the Memory search box keyword-searched "what has bob been working on?" ("Nothing
    # here mentions bob") and returned mostly Alice's facts, while /api/ask scoped the same words to Bob.
    monkeypatch.chdir(tmp_path)
    con = _ops_corpus()
    search = lambda q, **kw: apiserver.facts(con, q=q, scope_named=True, **kw)
    recent = search("what has bob been working on?")
    assert {r["user"] for r in recent["rows"]} == {"bob@trial.test"} and recent["total"] == 3, recent["rows"]
    assert recent["rows"][0]["claim"] == "Use SQLite with WAL for the cache layer", "most recent first"
    assert recent["coverage"]["missing"] == [], recent["coverage"]
    assert [r["claim"] for r in search("bob hooks")["rows"]] == ["The hooks were missing from settings.json"]
    assert [r["claim"] for r in search("what has bob done on the cache layer")["rows"]] == \
        ["Use SQLite with WAL for the cache layer"], "an activity word never drops the topic"
    bare = search("bob")
    assert bare["total"] == 3 and {r["user"] for r in bare["rows"]} == {"bob@trial.test"}
    assert search("handle retries")["rows"][0]["user"] == "alice@trial.test", "a query naming no one searches everyone"
    assert [r["user"] for r in search("agent retries")["rows"]] == ["alice@trial.test"], \
        "a name that is also a word falls back to everyone when the teammate's facts match nothing"
    assert search("bob hooks", owner="alice@trial.test")["total"] == 0, "an explicit owner scope wins"
    assert apiserver.facts(con, q="bob hooks")["total"] == 1 and \
        apiserver.facts(con, q="what has bob been working on?")["total"] != 3, "only the search box scopes"
    page = gql.run_query('{ facts(q: "what has bob been working on?") { totalCount items { user } } }',
                         con=con, org_id=store.DEFAULT_ORG)
    assert "errors" not in page and page["data"]["facts"]["totalCount"] == 3, page
    con.close()


def test_ask_locate_skips_the_strict_floor(monkeypatch, tmp_path):
    # "find me the session where…" wants a SESSION LIST. The strict floor can only answer with a fact page,
    # and on a long locate sentence it fires on substring collisions: the prod case was `find`->"findings",
    # `session`, `ledger` all present in one unrelated code-review fact, returned as tier "strict". Non-strict
    # locate asks skip that floor; the hook (strict) keeps its old behaviour and never returns a list.
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    for sid, day, summ in (("s-review", "2026-08-19", "Fresh-context code review of a ledger PR"),
                           ("s-embed", "2026-08-07", "Embedding non-determinism experiment on ledgerB")):
        con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user,summary) "
                    "VALUES (?,?,?,?,'session','jules@example.com',?)", (sid, "ledger", f"{day}T08:00:00Z", f"{day}T12:00:00Z", summ))
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s-review','decision','r1','The code review step for ledger PRs runs in a fresh session so findings "
                "are not biased by the implementer','')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s-embed','decision','r2','Measured embedding models on ledger: mean-of-k gives zero stability gain','')")
    # Gold on both: the ambient bar admits gold only, and this test is about locate-vs-strict, not the bar.
    for ref in ("r1", "r2"):
        con.execute("INSERT INTO signals(ref,kind,session_id,at) VALUES (?,'judge_gold','s-review','')", (ref,))
    con.commit()
    store.build_fts(con)
    q = "find me the session where jules measured embedding models on ledger"

    hook = apiserver.ask(con, q)
    assert hook["tier"] == "strict" and "id:r1" in hook["context"], \
        "the hook path is unchanged: strict still fires on the substring collision (that is the bug, kept for the hook)"
    assert not hook["context"].startswith("# Sessions matching"), "strict never returns a session list"

    out = apiserver.ask(con, q, strict=False)
    assert out["found"] is True and out["tier"] == "locate", (out["tier"], out.get("message"))
    assert out["context"].startswith("# Sessions matching")
    embed = next(ln for ln in out["context"].splitlines() if "`s-embed`" in ln)
    review = next(ln for ln in out["context"].splitlines() if "`s-review`" in ln)
    assert "matched: embedding, ledger, measured, models" in embed, embed
    assert "missed:" in review and "embedding" in review.split("missed:")[1], \
        "the collision session is shown with the topic words it did NOT match; matched: is the honest part"

    rows = con.execute("SELECT engine, planned_query FROM queries ORDER BY rowid").fetchall()
    assert rows[0][0] == "hook" and "tier=strict" in rows[0][1]
    assert rows[1][0] == "fallback" and "tier=locate" in rows[1][1]
    con.close()


def test_ask_locate_with_no_topical_match_is_a_miss(monkeypatch, tmp_path):
    # The locator's own words ("which", "session", "talked", "about") are dismissed BEFORE retrieval, on
    # every tier. A session whose facts contain only "session" cannot be retrieved, so it cannot be listed
    # with `matched: —` (prod, rank 2), and the keyword tier cannot rescue it either. Nothing topical
    # matched: honest miss, and coverage names only the topical words.
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES "
                "('s1','ledger','2026-08-07T00:00:00Z','session','jules@example.com')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s1','gotcha','ra','the duckdb generation pointer rolled twice mid-session','')")
    con.commit()
    store.build_fts(con)
    out = apiserver.ask(con, "which session talked about zzzqqq wwwxxx", strict=False)
    assert out["found"] is False and out["tier"] == "miss", (out["tier"], out.get("context"))
    assert out["coverage"]["missing"] == ["wwwxxx", "zzzqqq"], out["coverage"]
    con.close()


def test_ask_locate_never_lists_a_session_matched_only_on_locator_words(monkeypatch, tmp_path):
    # The prod case, reduced: two sessions. One's facts contain the topic ("embedding", "ledger"); the other's
    # contain only the locator's vocabulary ("session", "where", "find") plus noise, and it is NEWER, which
    # the rerank's recency boost rewards. Before: the noise session ranked ABOVE the real one with
    # `matched: —`. Now it is not retrieved at all.
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    for sid, day, summ in (("s-noise", "2026-09-02", "Fixed a formatting failure"),
                           ("s-embed", "2026-08-07", "Embedding non-determinism experiment on ledgerB")):
        con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user,summary) "
                    "VALUES (?,?,?,?,'session','jules@example.com',?)", (sid, "ledger", f"{day}T08:00:00Z", f"{day}T12:00:00Z", summ))
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s-noise','gotcha','n1','In this session the formatter found where the failure was and we ran black','')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s-noise','gotcha','n2','The session that ran the check must find the container first','')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s-embed','decision','e1','Measured embedding models on ledger: mean-of-k gives zero stability gain','')")
    con.commit()
    store.build_fts(con)
    out = apiserver.ask(con, "find me the session where jules measured embedding models on ledger", strict=False)
    assert out["tier"] == "locate", (out["tier"], out.get("message"))
    assert "`s-embed`" in out["context"]
    assert "`s-noise`" not in out["context"], "matched only on where/session/find/ran: must not be retrieved"
    assert "matched: —" not in out["context"]
    con.close()


def test_ask_locate_code_home_reach_is_shown_and_ranked_below_prose_coverage(monkeypatch, tmp_path):
    # Prod, after the locator words were dismissed: rank 2 was STILL `matched: —`. Its two facts were reached
    # through hidden_context.repo='ledger' (the code-home candidate source), which never appears in their prose,
    # and the display read prose only. Now the display reads match_text, so the row says `matched: ledger`,
    # and a session covering two of your words outranks one covering one — however many loud facts it has.
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    for sid, day in (("s-home", "2026-08-17"), ("s-embed", "2026-08-07")):
        con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) "
                    "VALUES (?,?,?,?,'session','jules@example.com')", (sid, "ledger", f"{day}T08:00:00Z", f"{day}T12:00:00Z"))
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence,repo) VALUES "
                "('s-home','gotcha','h1','A git worktree does not share the root .venv','','ledger')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence,repo,path) VALUES "
                "('s-home','gotcha','h2','Repo test rule forbids caplog except for log assertions','','ledger','backend/tests')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s-embed','decision','e1','Measured embedding models on ledger: mean-of-k gives zero gain','')")
    con.commit()
    store.build_fts(con)
    out = apiserver.ask(con, "find me the session where jules measured embedding models on ledger", strict=False)
    assert out["tier"] == "locate", (out["tier"], out.get("message"))
    rows = [ln for ln in out["context"].splitlines() if ln.startswith("- ")]
    home = next(ln for ln in rows if "`s-home`" in ln)
    assert "matched: ledger" in home and "matched: —" not in out["context"], home
    assert rows[0].find("`s-embed`") > 0, "two words covered outranks two facts covering one"
    con.close()


def _trial_corpus():
    """A trial workspace (user-sim, 2026-10-19): Bob's install session among three others, every fact
    gold, so the ambient bar is out of the way and only the strict floor decides."""
    con = store.sqlite_db(store.org_db())
    for sid, user, claims in (
            ("bob", "bob@trial.test", [
                ("gotcha", "No hooks wired on Bob's machine, so nothing shipped since Aug 30"),
                ("gotcha", "csa-client setup requires browser sign-in if re-run"),
                ("decision", "Use csa-client setup rather than hook install, since only setup wires all three hooks"),
                ("decision", "Install the CSA client with the one-line curl installer from the server's /install page"),
                ("gotcha", "The CSA token is stored in ~/.csa/config and must be present before any ingest"),
                ("gotcha", "ship.log in ~/.csa is the first place to check when a machine goes silent")]),
            ("carol", "carol@trial.test", [
                ("decision", "Run the CSA server with docker compose using docker-compose.trial.yml"),
                ("gotcha", "docker compose up needs the CSA_AUTH variable set or the API refuses to boot"),
                ("gotcha", "Port 8794 was already taken on the host, so the API was mapped to 8795"),
                ("decision", "Health check the API with /api/health before pointing clients at it")]),
            ("alice", "alice@trial.test", [
                ("decision", "Daily billing resets run at midnight UTC"),
                ("gotcha", "Stripe webhooks retry for three days, so handlers must be idempotent"),
                ("todo", "Migrate the proration logic out of the cron job")]),
            ("dave", "dave@trial.test", [
                ("decision", "The dashboard uses React Query for all server state"),
                ("todo", "Add dark mode to the settings page")])):
        store.load_sqlite(con, dict(session_id=sid, project="trial", git_branch="main",
                                    started_at="2026-10-01T09:00:00Z", ended_at="2026-10-01T10:00:00Z",
                                    n_user=3, n_asst=3, user=user),
                          {"hidden_context": [{"type": t, "claim": c, "evidence": c} for t, c in claims]})
    for ref, sid in con.execute("SELECT ref, session_id FROM hidden_context").fetchall():
        con.execute("INSERT INTO signals(ref,kind,session_id,at) VALUES (?,'judge_gold',?,'')", (ref, sid))
    con.commit()
    store.build_fts(con)
    return con


def test_ask_default_floor_reads_words_the_way_the_index_does(monkeypatch, tmp_path):
    # The index stems ("installed" finds "Install", "shipped" finds "ship"); the strict floor compared raw
    # substrings, so it vetoed the very candidates retrieval had found. It also counted "running" and
    # "getting" as topic words although "run" and "get" are filler, and required the scoped person's name
    # ("bob") in the text of facts Bob owns, which rarely say "Bob".
    monkeypatch.chdir(tmp_path)
    con = _trial_corpus()
    for q, want in (("getting CSA installed and running", "one-line curl installer"),
                    ("how did bob install the hooks", "only setup wires all three hooks"),
                    ("how do I install the CSA hooks", "only setup wires all three hooks"),
                    ("why did nothing ship from bob's machine", "nothing shipped since Aug 30"),
                    ("does csa-client setup need a browser sign-in", "requires browser sign-in"),
                    ("docker compose", "docker compose")):
        out = apiserver.ask(con, q, asked_by="eve@trial.test")
        assert out["found"] is True and out["tier"] == "strict", (q, out.get("message"))
        assert want in out["system_message"], (q, out["system_message"])
    for q in ("running the billing dashboard", "how do I get dark mode on the API"):
        out = apiserver.ask(con, q, asked_by="eve@trial.test")
        assert out["found"] is False, ("words scattered over unrelated facts still abstain", q, out.get("context"))
    con.close()


def test_ask_floor_counts_a_name_only_as_a_whole_name_and_never_as_the_topic(monkeypatch, tmp_path):
    # "car" is inside "carol" but is not her name; and a name plus one topic word is still one content word.
    monkeypatch.chdir(tmp_path)
    con = _trial_corpus()
    out = apiserver.ask(con, "fix my car docker compose", asked_by="carol@trial.test")
    assert out["found"] is False, out.get("context")
    for q in ("bob hooks", "bob installer"):
        out = apiserver.ask(con, q, asked_by="eve@trial.test")
        assert out["found"] is False, (q, out.get("context"))
    con.close()


def test_ask_single_word_stays_off_the_hook_and_answers_an_explicit_ask(monkeypatch, tmp_path):
    # One content word never fires unbidden: the client does not even send it. An explicit ask answers it.
    monkeypatch.chdir(tmp_path)
    con = _trial_corpus()
    for word in ("install", "setup", "csa", "hooks", "token", "docker"):
        hook = apiserver.ask(con, word, asked_by="eve@trial.test")
        assert hook["found"] is False and hook["coverage"]["terms"] == 1, (word, hook)
        out = apiserver.ask(con, word, asked_by="eve@trial.test", strict=False)
        assert out["found"] is True, (word, out.get("message"))
    con.close()


def test_ask_miss_coverage_names_the_words_it_dropped(monkeypatch, tmp_path):
    # The default path returned coverage {"terms": 0} on every miss, and the honest miss said "never
    # seen: none" even when every word was dropped before retrieval. Coverage now counts the searched
    # words on every path and names the dismissed ones.
    monkeypatch.chdir(tmp_path)
    con = _trial_corpus()
    hook = apiserver.ask(con, "how do I set up csa on my laptop", asked_by="eve@trial.test")
    assert hook["found"] is False
    assert hook["coverage"] == {"missing": ["laptop"], "distinctive": ["set", "up"], "terms": 4,
                                "dismissed": ["do", "how", "i", "my", "on"]}, hook["coverage"]
    miss = apiserver.ask(con, "how did they do it", strict=False)
    assert miss["found"] is False and miss["coverage"]["terms"] == 0, miss
    assert "never seen" not in miss["message"], "no word was searched, so none was unseen"
    assert "Not searched: did, do, how, it, they" in miss["message"], miss["message"]
    con.close()


def _unnamed_corpus(*extra, org_id=None):
    """Teammates whose addresses are first.last and whose facts never say their name, so the router
    scopes no one and only the strict floor can tie "bob" to Bob's facts. The last of Bob's is not gold."""
    con = store.sqlite_db(store.org_db(org_id))
    for sid, user, claims in (
            ("bob", "bob.stone@trial.test", [
                ("gotcha", "No hooks wired, so nothing shipped since Aug 30"),
                ("decision", "Got CSA running on a fresh machine with csa-client setup and a browser sign-in"),
                ("decision", "Install the client with the one-line curl installer"),
                ("gotcha", "The sweep floor reset dropped older unshipped sessions")]),
            ("carol", "carol.diaz@trial.test", [
                ("decision", "Run the CSA server with docker compose using docker-compose.trial.yml"),
                ("gotcha", "Port 8794 was already taken on the host, so the API was mapped to 8795")]),
            ("alice", "alice.wong@trial.test", [
                ("decision", "Daily billing resets run at midnight UTC"),
                ("todo", "Add dark mode to the settings page")]),
            *extra):
        store.load_sqlite(con, dict(session_id=sid, project="trial", git_branch="main",
                                    started_at="2026-10-01T09:00:00Z", ended_at="2026-10-01T10:00:00Z",
                                    n_user=3, n_asst=3, user=user),
                          {"hidden_context": [{"type": t, "claim": c, "evidence": c} for t, c in claims]})
    for ref, sid in con.execute("SELECT ref, session_id FROM hidden_context WHERE claim NOT LIKE 'The sweep%'"):
        con.execute("INSERT INTO signals(ref,kind,session_id,at) VALUES (?,'judge_gold',?,'')", (ref, sid))
    con.commit()
    store.build_fts(con)
    return con


def test_ask_default_floor_counts_a_teammates_name_on_the_facts_they_own(monkeypatch, tmp_path):
    # User-sim 2026-10-19: a question naming a teammate missed on the default call although the
    # teammate's fact existed, since facts never say their author's name and no person was scoped.
    monkeypatch.chdir(tmp_path)
    con = _unnamed_corpus()
    for q, want in (("are hooks wired on bob's client", "nothing shipped since Aug 30"),
                    ("why did nothing ship from bob's machine", "nothing shipped since Aug 30"),
                    ("How did Bob get CSA running on his machine?", "csa-client setup and a browser sign-in")):
        assert route.classify(con, q, asked_by="eve@trial.test").person is None, q
        out = apiserver.ask(con, q, asked_by="eve@trial.test")
        assert out["found"] is True and out["tier"] == "strict", (q, out.get("message"))
        assert want in out["system_message"], (q, out["system_message"])
    con.close()


def test_ask_default_bar_follows_the_caller(monkeypatch, tmp_path):
    """User-sim 2026-10-19 (GKE, Gemini judge): Bob's fact was judge-accepted but never gold, and a
    default /api/ask served gold only, so a person asking got tier=miss. The hook sends its session and
    keeps that bar; a call without one is somebody asking and gets the whole ladder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    cc = orgs.control_db()
    orgs._create_org("Trial", org_id="trial", con=cc)
    cc.commit()
    token = orgs.mint_token("eve@trial.test", "trial", hostname="eve-mbp", con=cc)
    con = _unnamed_corpus(org_id="trial")
    con.execute("DELETE FROM signals WHERE kind='judge_gold'")
    con.execute("INSERT INTO signals(ref,kind,session_id,at) "
                "SELECT DISTINCT ref,'judge_accept','judge','' FROM hidden_context")
    con.commit()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def ask(body):
        req = urllib.request.Request("http://127.0.0.1:%d/api/ask" % srv.server_address[1],
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    try:
        for q, want in (("are hooks wired on bob's client", "nothing shipped since Aug 30"),
                        ("why did nothing ship from bob's machine", "nothing shipped since Aug 30"),
                        ("How did Bob get CSA running on his machine?", "csa-client setup and a browser sign-in"),
                        ("how do I set up csa on my laptop", "csa-client setup and a browser sign-in")):
            out = ask({"query": q})
            assert out["found"] is True and want in out["context"], (q, out["tier"], out.get("message"))
            hook = ask({"query": q, "session": "7c1d2e0a-5b7f-4f3e-9d1a-0c2b3a4d5e6f", "asked_by": "eve@trial.test",
                        "cwd": "/home/eve/src/trial", "k": 6})          # client/hook.go's body, field for field
            assert hook["found"] is False and hook["tier"] == "miss", (q, hook.get("context"))
            assert ask({"query": q, "strict": True})["found"] is False, q
        explicit = ask({"query": "are hooks wired on bob's client", "session": "s1", "strict": False})
        assert explicit["found"] is True, "an explicit strict wins over the session's default"
    finally:
        srv.shutdown()
        srv.server_close()
    sources = [r["source"] for r in analytics.recall_log(con)["rows"]]
    assert sources.count("hook") == 4 and sources.count("api") == 9, sources
    con.close()


def test_ask_teammates_name_relaxes_nothing_else(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    con = _unnamed_corpus()
    for q in ("bob hooks", "bob installer", "are hooks wired on carol's client"):
        out = apiserver.ask(con, q, asked_by="eve@trial.test")
        assert out["found"] is False, ("one topic word, or a name on facts its owner did not write", q, out.get("context"))
    q = "did the sweep floor reset drop bob's sessions"
    assert apiserver.ask(con, q, asked_by="eve@trial.test")["found"] is False, "the hook still serves gold only"
    assert apiserver.ask(con, q, asked_by="eve@trial.test", strict=False)["tier"] == "strict"
    con.close()


def test_ask_a_name_two_teammates_share_counts_for_neither(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    con = _unnamed_corpus(("bob2", "bob.reed@trial.test", [("decision", "Billing exports run nightly")]))
    out = apiserver.ask(con, "why did nothing ship from bob's machine", asked_by="eve@trial.test")
    assert out["found"] is False, out.get("context")
    con.close()


def test_ambient_serves_gold_only_while_an_explicit_ask_keeps_everything(monkeypatch, tmp_path):
    """THE AMBIENT BAR: the UserPromptSubmit hook injects only gold. strict=false is an ask, and keeps all."""
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES "
                "('s1','ledger','2026-08-07T00:00:00Z','session','jules@example.com')")
    claim = "The etl drain lock is per-org so two workspaces never block each other"
    for ref, text in (("g", claim), ("p", claim + " under load"), ("v", claim + " on restart")):
        con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                    "('s1','decision',?,?,'')", (ref, text))
    con.execute("INSERT INTO signals(ref,kind,session_id,at) VALUES ('g','judge_gold','s1','')")
    con.execute("INSERT INTO signals(ref,kind,session_id,at) VALUES ('v','judge_gold','s1','')")
    con.execute("INSERT INTO reviews(kind,ref,status,note,flagger) VALUES ('hc','v','unimportant','','x')")
    con.commit()
    store.build_fts(con)
    q = "etl drain lock per-org workspaces"

    hook = apiserver.ask(con, q, asked_by="jules@example.com")          # the hook: strict, unasked
    assert hook["found"] is True, hook
    assert "id:g" in hook["context"], "the gold fact is served"
    assert "id:p" not in hook["context"], "nothing vouched for it, so the hook stays quiet about it"
    assert "id:v" not in hook["context"], "a human 'unimportant' vetoes machine gold"

    ask = apiserver.ask(con, q, asked_by="jules@example.com", strict=False)   # a deliberate ask
    assert ask["found"] is True and "id:p" in ask["context"], "an explicit ask still answers with everything"


def test_ambient_abstains_as_the_floor_when_nothing_is_gold(monkeypatch, tmp_path):
    """No gold must read as 'we had matches, none vouched for', not as 'nothing matched': that distinction
    is the only signal that can tune the bar, so it has to survive in the miss message."""
    monkeypatch.chdir(tmp_path)
    con = store.sqlite_db(store.org_db())
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) VALUES "
                "('s1','ledger','2026-08-07T00:00:00Z','session','jules@example.com')")
    con.execute("INSERT INTO hidden_context(session_id,type,ref,claim,evidence) VALUES "
                "('s1','decision','u','The etl drain lock is per-org so two workspaces never block','')")
    con.commit()
    store.build_fts(con)

    miss = apiserver.ask(con, "etl drain lock per-org workspaces", asked_by="jules@example.com")
    assert miss["found"] is False and miss["tier"] == "miss"
    assert miss["message"].startswith("No strong past-session context"), \
        f"abstained as the ambient floor, not as no-candidates: {miss['message']}"
