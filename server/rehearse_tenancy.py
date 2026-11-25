"""MIGRATION REHEARSAL against a scratch copy of prod-SHAPED data. Touches no production system.

The question this answers, in the founder's words: do CURRENT GLOBEX USERS still report sessions and
get 200 with the tokens they already hold, after the mode collapse and after `default` is adopted?

Shape of the run:
  1. Build a box the way prod is TODAY: CSA_AUTH=org, a shared secret, per-user tokens already
     minted into `default`, a browser cookie session already issued.
  2. Boot the real apiserver against it and record what those credentials do BEFORE anything changes.
  3. Run the migration: adopt `default` as "Globex".
  4. Boot again and assert the SAME credentials still behave the same.

Everything runs over real HTTP against a real server process, because the whole class of bug this
task exists to catch (a route that opens the wrong file, a gate that stops stamping a header) is
invisible to an in-process call.
"""
import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

if __name__ != "__main__":
    raise SystemExit("rehearse_tenancy.py is a script, not a test module")

SERVER = os.path.dirname(os.path.abspath(__file__))          # this file lives in server/
PY = os.path.join(SERVER, ".venv/bin/python")

_ap = argparse.ArgumentParser(
    description="Rehearse the tenancy migration against a scratch copy of prod-shaped data. Boots "
                "real apiserver processes against a throwaway box; touches no production system.")
_ap.add_argument("box", nargs="?", default=None,
                 help="directory to build the scratch box in (default: a fresh temp dir). Must not "
                      "already exist: this script never deletes an existing directory.")
_args = _ap.parse_args()
if _args.box:
    BOX = os.path.abspath(_args.box)                        # absolute: the fixture chdirs around
    if os.path.exists(BOX):
        raise SystemExit(f"refusing: {BOX} already exists. Remove it, or pass a fresh path.")
    os.makedirs(BOX)
else:
    BOX = tempfile.mkdtemp(prefix="csa-rehearsal-")          # already created, already fresh
if os.path.realpath(SERVER).startswith(os.path.realpath(BOX)):
    raise SystemExit(f"refusing: {BOX} contains the server tree")
SECRET = "prod-shared-secret-abc123"

sys.path.insert(0, SERVER)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    """The real apiserver in a subprocess, cwd=BOX so its relative data/ paths land there."""

    def __init__(self, env, box=None):
        self.port = free_port()
        self.box = box or BOX
        e = dict(os.environ)
        e.update(env)
        e["CSA_API_PORT"] = str(self.port)
        e["PYTHONPATH"] = SERVER
        # the parent process sets these while building fixtures; they must not leak into the server
        for k in ("CSA_AUTH", "CSA_AUTH_TOKEN", "CSA_ORG_INVITE"):
            if k not in env:
                e.pop(k, None)
        self.env = e
        self.proc = None

    def __enter__(self):
        self.log = open(os.path.join(self.box, f"server-{self.port}.log"), "w")
        self.proc = subprocess.Popen([PY, os.path.join(SERVER, "apiserver.py")],
                                     cwd=self.box, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(120):
            if self.proc.poll() is not None:
                raise SystemExit(f"server exited {self.proc.returncode}; see {self.log.name}\n"
                                 + open(self.log.name).read()[-3000:])
            try:
                if self.get("/api/health")[0] == 200:
                    return self
            except OSError:
                time.sleep(0.25)
        raise SystemExit("server never came up:\n" + open(self.log.name).read()[-3000:])

    def __exit__(self, *a):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    def _req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            c.request(method, path, body=json.dumps(body) if body is not None else None,
                      headers=headers or {})
            r = c.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw[:200]
        finally:
            c.close()

    def get(self, path, headers=None):
        return self._req("GET", path, headers)

    def page(self, path):
        """(status, the whole body) for an HTML route; _req keeps only the first 200 bytes of one."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def post(self, path, body, headers=None):
        return self._req("POST", path, {**(headers or {}), "Content-Type": "application/json"}, body)


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


FAILS = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# =====================================================================================
# 1. Build a box shaped like prod TODAY
# =====================================================================================
os.environ.update({"CSA_AUTH": "org", "CSA_AUTH_TOKEN": SECRET})
os.chdir(BOX)
from analyzer import cliauth, orgs, store       # noqa: E402  (after chdir: paths are cwd-relative)
from tests import legacy_ddl                   # noqa: E402

# The box starts on the schema a July 2026 install shipped: no facts, tenant_config, entity_stats
# or session_metrics, served and queries still inside the corpus, and no expires_at, role or domain
# on the control plane. Everything below then runs against files the Alembic baselines caught up on
# their first open, which is what a real deploy does to every workspace.
for db_path, ddl in ((store.org_db(None), legacy_ddl.JUL_2026_CORPUS),
                     (str(orgs.control_path()), legacy_ddl.JUL_2026_CONTROL)):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    legacy = sqlite3.connect(db_path)
    legacy.executescript(ddl)
    legacy.commit()
    legacy.close()

con = store.sqlite_db(store.org_db(None))
check("the pre-Alembic corpus caught up on open",
      con.execute("SELECT version_num FROM alembic_version").fetchone() is not None
      and {r[1] for r in con.execute("PRAGMA table_info(sessions)")} >= {"harness", "shipped_by"})
for i in range(25):
    con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) "
                "VALUES(?,?,?,?,'session',?)",
                (f"globex-{i}", "csa", "2026-08-30T09:00:00Z", "2026-08-30T10:00:00Z", "mara@globex.com"))
    con.execute("INSERT INTO hidden_context(session_id,type,claim,ref) VALUES(?,?,?,?)",
                (f"globex-{i}", "decision", f"globex decision {i}", f"gx{i}"))
con.commit()
con.close()

# the credentials real users are already holding
MARA = orgs.mint_token("mara@globex.com", "default", "mara-mac")
JULES = orgs.mint_token("jules@globex.com", "default", "jules-mac")
MARA_ADMIN = orgs.mint_token("admin@globex.com", "default", "mara-admin", role="admin")
ok_web, WEB = cliauth.web_login("mara@globex.com", SECRET)
assert ok_web, WEB
COOKIE = {"Cookie": cliauth.session_cookie(WEB).split(";")[0]}

os.chdir("/")


def add_acme():
    """A second, hosted tenant. Created AFTER the BEFORE phase on purpose: prod today has no other
    org, and with one present the old code would have refused to boot at all (boot_error)."""
    cwd = os.getcwd()
    os.chdir(BOX)
    try:
        orgs._create_org("Acme Corp", "acme", con=orgs.control_db())
        tok = orgs.mint_token("dev@acme.com", "acme", "acme-laptop")
        c = store.sqlite_db(store.org_db("acme"))
        c.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) "
                  "VALUES('acme-1','acme','2026-08-30T09:00:00Z','2026-08-30T10:00:00Z','session','dev@acme.com')")
        c.execute("INSERT INTO hidden_context(session_id,type,claim,ref) "
                  "VALUES('acme-1','decision','ACME SECRET','ac1')")
        c.commit()
        c.close()
        return tok
    finally:
        os.chdir(cwd)

print(f"scratch box: {BOX}")
print(f"  mara token {MARA[:14]}...  jules {JULES[:14]}...\n")

# =====================================================================================
# 2. BEFORE — prod as it is today (single tenant, `default` not adopted)
# =====================================================================================
print("=== BEFORE: single tenant (prod as it is today) ===")
with Server({"CSA_AUTH": "org", "CSA_AUTH_TOKEN": SECRET}) as s:
    st, me = s.get("/api/me", bearer(MARA))
    check("mara's existing token: /api/me 200", st == 200, f"{st} {me}")
    before_me = me
    st, act = s.get("/api/activity?limit=200", bearer(MARA))
    check("mara reads their corpus (activity names them)", st == 200
          and any(p.get("user") == "mara@globex.com" for p in act.get("people", [])), f"{st} {str(act)[:200]}")
    st, cs = s.get("/api/corpus/stats", bearer(MARA))
    check("mara's corpus stats visible", st == 200, f"{st}")
    st, r = s.post("/api/ingest", {"session_id": "before-ship", "lines": []}, bearer(MARA))
    check("mara SHIPS a session (ingest 200)", st == 200, f"{st} {r}")
    before_ingest = st
    st, _ = s.get("/api/activity", COOKIE)
    check("browser cookie session reads", st == 200, f"{st}")
    st, cfg = s.get("/api/auth-config")
    check("front door is the SIGN-IN (workspace_requests false: no request_webhook here)",
          cfg.get("workspace_requests") is False, f"{cfg}")
    st, _ = s.get("/api/admin/orgs", bearer(SECRET))
    check("there is no operator console, even before the migration", st == 404, f"{st}")

# =====================================================================================
# 3. THE MIGRATION
# =====================================================================================
print("\n=== MIGRATION: adopt `default` as Globex ===")
ACME = add_acme()
ACME_HASH = orgs._hash(ACME)                    # a real hash from ANOTHER org, for the negative tests
print(f"  (onboarded a second tenant first: acme, token {ACME[:14]}...)")
r = subprocess.run([PY, os.path.join(SERVER, "csa.py"), "org", "adopt-default", "Globex",
                    "--domain", "globex.com", "--yes"],
                   cwd=BOX, capture_output=True, text=True,
                   env={**os.environ, "PYTHONPATH": SERVER, "CSA_AUTH": "org"})
print("\n".join("  " + ln for ln in (r.stdout or r.stderr).strip().splitlines()))
check("adopt-default succeeded", r.returncode == 0, r.stderr[-400:])

# =====================================================================================
# 4. AFTER — `default` adopted
# =====================================================================================
print("\n=== AFTER: `default` adopted ===")
with Server({"CSA_AUTH": "org", "CSA_AUTH_TOKEN": SECRET}) as s:
    st, me = s.get("/api/me", bearer(MARA))
    check("GLOBEX USER: existing token still 200 on /api/me", st == 200, f"{st} {me}")
    check("...same email and org as before", me.get("email") == before_me.get("email")
          and me.get("org_id") == before_me.get("org_id"), f"{me}")
    check("...workspace now reads 'Globex', not 'self-hosted'", me.get("org_name") == "Globex", f"{me}")

    st, act = s.get("/api/activity?limit=200", bearer(MARA))
    check("GLOBEX USER: still reads their corpus (activity names them)", st == 200
          and any(p.get("user") == "mara@globex.com" for p in act.get("people", [])), f"{st} {str(act)[:200]}")
    st, facts = s.get("/api/facts", bearer(MARA))
    globex_blob = json.dumps(facts)
    check("GLOBEX USER: /api/facts 200 and shows globex claims", st == 200 and "globex decision" in globex_blob,
          f"{st}")

    st, r2 = s.post("/api/ingest", {"session_id": "after-ship", "lines": []}, bearer(MARA))
    check("GLOBEX USER: still SHIPS sessions (ingest 200)", st == before_ingest == 200, f"{st} {r2}")

    st, _ = s.get("/api/activity", bearer(JULES))
    check("second globex user's token still 200", st == 200, f"{st}")

    st, _ = s.get("/api/activity", COOKIE)
    check("existing browser cookie still reads", st == 200, f"{st}")

    st, imp = s.get("/api/improve", bearer(MARA))
    check("/api/improve 200 and pinned to self for a member", st == 200 and imp.get("can_widen") is False,
          f"{st} can_widen={imp.get('can_widen')}")

    # --- isolation ---
    st, afacts = s.get("/api/facts", bearer(ACME))
    ablob = json.dumps(afacts)
    check("acme sees its OWN corpus", st == 200 and "ACME SECRET" in ablob, f"{st}")
    check("acme's corpus contains NO globex content", "globex decision" not in ablob, ablob[:200])
    st, aact = s.get("/api/activity?limit=200", bearer(ACME))
    users = {p.get("user") for p in aact.get("people", [])}
    check("acme's activity names ONLY acme people", st == 200 and users == {"dev@acme.com"},
          f"{st} {users}")
    st, d = s.get("/api/sessions/globex-1", bearer(ACME))
    leaked = json.dumps(d) if not isinstance(d, bytes) else ""
    check("CROSS-TENANT REPLAY: acme replaying a globex session id learns nothing",
          "globex decision" not in leaked, f"{st} {str(d)[:160]}")
    st, d2 = s.get("/api/sessions/globex-1", bearer(MARA))
    st3, d3 = s.get("/api/sessions/no-such-session-xyz", bearer(ACME))
    check("...and the replay is INDISTINGUISHABLE from a nonexistent id",
          (st, json.dumps(d)) == (st3, json.dumps(d3)),
          f"replay={st}/{str(d)[:80]} nonexistent={st3}/{str(d3)[:80]}")

    st, cfg = s.get("/api/auth-config")
    check("front door STAYS the sign-in after adoption: the landing needs the request_webhook setting, "
          "which this rehearsal does not set",
          cfg.get("workspace_requests") is False, f"{cfg}")

    # --- THERE IS NO CROSS-WORKSPACE ROUTE: 404 for a workspace admin and a member, and the retired
    # shared secret never gets that far (401 from the gate, naming `csa-client setup`).
    for route in ("/api/admin/orgs", "/api/admin/tokens?org_id=acme", "/api/admin/audit?org_id=acme"):
        for who, tok, want in (("workspace admin", MARA_ADMIN, 404), ("plain member", JULES, 404),
                               ("retired shared secret", SECRET, 401)):
            st, _ = s.get(route, bearer(tok))
            check(f"NO CONTROL PLANE: {route.split('?')[0]} -> {want} for a {who}", st == want, f"{st}")
    for route, body in (("/api/admin/org", {"name": "Sneaky"}),
                        ("/api/admin/org/invite", {"org_id": "acme"}),
                        ("/api/admin/org/delete", {"org_id": "acme"}),
                        ("/api/admin/token/revoke", {"token_hash": "f" * 64})):
        st, _ = s.post(route, body, bearer(MARA_ADMIN))
        check(f"NO CONTROL PLANE: POST {route} is gone", st == 404, f"{st}")

    # --- NEGATIVE: the shared secret is refused on EVERY route, with the message the client acts on ---
    for route in ("/api/facts", "/api/activity", "/api/corpus/stats", "/api/me", "/api/improve", "/api/pipeline"):
        st, body = s.get(route, bearer(SECRET))
        check(f"SHARED SECRET REFUSED on {route}", st == 401 and "csa-client setup" in str(body),
              f"{st} {str(body)[:120]}")
    st, body = s.post("/api/ask", {"query": "anything"}, bearer(SECRET))
    check("SHARED SECRET REFUSED on /api/ask", st == 401, f"{st} {str(body)[:120]}")
    st, body = s.post("/api/ingest", {"session_id": "op", "lines": []}, bearer(SECRET))
    check("SHARED SECRET REFUSED on /api/ingest", st == 401, f"{st} {str(body)[:120]}")

    # --- NEGATIVE: the tenant admin is scoped, and no input can move them ---
    st, mem = s.get("/api/org/members", bearer(MARA_ADMIN))
    emails = {m.get("email") for m in mem.get("members", [])} if st == 200 else set()
    check("TENANT ADMIN lists their OWN org's members", st == 200 and emails
          and all(e.endswith("@globex.com") for e in emails), f"{st} {emails}")
    for probe in ("/api/org/members?org_id=acme", "/api/org/members?org_id=acme&limit=5"):
        st2, m2 = s.get(probe, bearer(MARA_ADMIN))
        e2 = {m.get("email") for m in m2.get("members", [])} if st2 == 200 else set()
        check(f"...and a query param cannot move them ({probe.split('?')[1]})",
              st2 == 200 and e2 == emails, f"{st2} {e2}")

    st, other = s.post("/api/org/member/revoke", {"token_hash": ACME_HASH, "org_id": "acme"},
                       bearer(MARA_ADMIN))
    check("CROSS-TENANT WRITE: revoking another org's token is refused", st == 404, f"{st} {other}")
    st_none, nonexistent = s.post("/api/org/member/revoke", {"token_hash": "f" * 64}, bearer(MARA_ADMIN))
    check("...INDISTINGUISHABLE from a token that exists nowhere",
          (st, json.dumps(other)) == (st_none, json.dumps(nonexistent)),
          f"other-org={st}/{other}  nonexistent={st_none}/{nonexistent}")
    st, _ = s.get("/api/activity", bearer(ACME))
    check("...and the victim's token still works", st == 200, f"{st}")

    # --- NEGATIVE: the two admin meanings do not leak into each other ---
    st, body = s.get("/api/org/members", bearer(JULES))
    check("PLAIN MEMBER refused the tenant-admin surface", st == 403, f"{st} {str(body)[:120]}")
    st, body = s.get("/api/me", bearer(MARA_ADMIN))
    check("the principal has NO cross-workspace axis",
          st == 200 and "operator" not in body and "admin" not in body, f"{st} {body}")
    st, imp = s.get("/api/improve", bearer(MARA_ADMIN))
    check("workspace admin CAN widen Improve inside their own org", st == 200
          and imp.get("can_widen") is True, f"{st} {imp.get('can_widen')}")

    # --- the two footguns the tenant-admin simulation walked into ---
    # 1. revoking the ONLY admin locked the workspace out, answering {"ok": true}
    admin_hash = next((m["token_hash"] for m in mem.get("members", [])
                       if m.get("role") == "admin" and m.get("active")), None)
    st, r = s.post("/api/org/member/revoke", {"token_hash": admin_hash}, bearer(MARA_ADMIN))
    check("LAST ADMIN cannot revoke themselves into a lockout",
          st == 409 and r.get("code") == "last_admin", f"{st} {r}")
    # ...and the action that refusal names must actually exist
    st, pr = s.post("/api/org/member/role", {"email": "jules@globex.com", "role": "admin"},
                    bearer(MARA_ADMIN))
    check("the refusal's remedy is a real route (promote a colleague)",
          st == 200 and pr.get("tokens_updated", 0) >= 1, f"{st} {pr}")
    st, r2 = s.post("/api/org/member/revoke", {"token_hash": admin_hash}, bearer(MARA_ADMIN))
    check("...and with a second admin, the revoke goes through", st == 200, f"{st} {r2}")
    # MARA_ADMIN just revoked ITSELF, so it must stop working from here — that is the guard doing its
    # job, and the promoted colleague is now the one holding the workspace.
    st, _ = s.get("/api/org/members", bearer(MARA_ADMIN))
    check("...and the revoked admin token is dead", st == 401, f"{st}")
    st, mem2 = s.get("/api/org/members", bearer(JULES))
    check("the promoted colleague can now administer", st == 200, f"{st} {str(mem2)[:120]}")
    st, x = s.post("/api/org/member/role", {"email": "dev@acme.com", "role": "admin"}, bearer(JULES))
    check("CROSS-TENANT: cannot promote someone in another workspace", st == 404, f"{st} {x}")

    # 2. rotating a LEAKED invite says who already walked through it
    st, rot = s.post("/api/org/invite/rotate", {}, bearer(JULES))
    check("invite rotate returns a fresh invite", st == 200 and str(rot.get("invite", "")).startswith("inv_"),
          f"{st} {rot}")
    check("...and says how many members the OLD invite already admitted",
          isinstance(rot.get("members_admitted"), int) and rot["members_admitted"] >= 1
          and "revoke anyone you do not" in rot.get("note", ""), f"{rot}")

# =====================================================================================
# 5. THE SSO CUTOVER: `sso set` for the Google issuer with prod's client id; nobody re-authorizes
# =====================================================================================
print("\n=== SSO: `csa.py sso set` for the Google issuer, then every existing credential again ===")
GOOGLE_CLIENT_ID = "1234567890-rehearsal.apps.googleusercontent.com"
r = subprocess.run([PY, os.path.join(SERVER, "csa.py"), "sso", "set", "--issuer", "https://accounts.google.com",
                    "--client-id", GOOGLE_CLIENT_ID, "--name", "Google", "--client-secret-stdin"],
                   cwd=BOX, capture_output=True, text=True, input="rehearsal-client-secret\n",
                   env={**os.environ, "PYTHONPATH": SERVER, "CSA_AUTH": "org",
                        "CSA_PUBLIC_URL": "https://csa.example.com"})
print("\n".join("  " + ln for ln in (r.stdout or r.stderr).strip().splitlines()))
check("sso set succeeded (needs the network for Google's discovery document)", r.returncode == 0, r.stderr[-400:])
check("...and printed the redirect URI to register",
      "https://csa.example.com/api/sso/callback" in r.stdout, r.stdout[-300:])
check("...and never echoed the secret", "rehearsal-client-secret" not in r.stdout + r.stderr)

with Server({"CSA_AUTH": "org", "CSA_AUTH_TOKEN": SECRET, "CSA_PUBLIC_URL": "https://csa.example.com"}) as s:
    check("the server boots with the sso row (default has a domain)", True)
    for who, tok in (("mara", MARA), ("jules", JULES), ("acme", ACME)):
        st, _ = s.get("/api/activity", bearer(tok))
        check(f"{who}'s existing token still 200 after sso set", st == 200, f"{st}")
    st, r2 = s.post("/api/ingest", {"session_id": "after-sso", "lines": []}, bearer(MARA))
    check("mara still SHIPS sessions after sso set", st == 200, f"{st} {r2}")
    st, _ = s.get("/api/activity", COOKIE)
    check("the existing browser cookie still reads (it is not revoked by sso set)", st == 200, f"{st}")
    st, cfg = s.get("/api/auth-config")
    check("auth-config offers the issuer and closes the invite path",
          cfg.get("sso") == {"name": "Google", "start_url": "/api/sso/start"} and cfg.get("sso_required") is True,
          f"{cfg}")
    st, body = s.post("/api/ui-login", {"email": "anyone@globex.com", "invite": "whatever"},
                      {"Sec-Fetch-Site": "same-origin"})
    check("a typed-email sign-in is refused, naming the issuer",
          st == 401 and "requires signing in with Google" in str(body), f"{st} {body}")
    st, page = s.page("/cli-auth?nonce=x&hostname=y")
    check("the /cli-auth page links to /api/sso/start and loads nothing from Google",
          st == 200 and b"/api/sso/start" in page and b"accounts.google.com" not in page, f"{st}")
    st, r3 = s.get("/api/sso/start?return=/")
    check("/api/sso/start redirects to accounts.google.com with code+PKCE", st == 302, f"{st} {str(r3)[:120]}")
    con = sqlite3.connect(os.path.join(BOX, "data", "control.db"))
    check("every user_tokens row is untouched by sso set (no revoke, no re-authorization)",
          con.execute("SELECT COUNT(*) FROM user_tokens WHERE revoked_at IS NULL").fetchone()[0] >= 3)
    check("control.db stays rollback-journal after the sso writes",
          con.execute("PRAGMA journal_mode").fetchone()[0] == "delete")
    con.close()

# =====================================================================================
# 6. NEGATIVE: a self-host single-tenant install keeps its shared token
# =====================================================================================
print("\n=== SELF-HOST on the shared token, no tenants ===")
SELF = BOX + "-selfhost"
shutil.rmtree(SELF, ignore_errors=True)
os.makedirs(SELF)
_cwd = os.getcwd()
os.chdir(SELF)
_sc = store.sqlite_db(store.org_db(None))
_sc.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,source,user) "
            "VALUES('sh-1','p','2026-08-30T09:00:00Z','2026-08-30T10:00:00Z','session','me@self.host')")
_sc.commit()
_sc.close()
os.chdir(_cwd)

with Server({"CSA_AUTH": "org", "CSA_AUTH_TOKEN": "selfhost-secret"}, box=SELF) as s:
    check("self-host BOOTS on CSA_AUTH=org with a shared token", True)
    st, cfg = s.get("/api/auth-config")
    check("self-host front door stays the SIGN-IN (no public workspace form)",
          cfg.get("workspace_requests") is False, f"{cfg}")
    st, _ = s.get("/api/activity", bearer("selfhost-secret"))
    check("self-host: the shared token still SERVES its corpus (no tenants => no demotion)",
          st == 200, f"{st}")
    st, r = s.post("/api/cli-auth/new", {"hostname": "laptop"})
    check("self-host /cli-auth still mints (the env-invite path is alive)",
          st in (200, 201), f"{st} {str(r)[:140]}")

print("\n" + ("=" * 70))
print(f"REHEARSAL: {'PASS' if not FAILS else 'FAIL — ' + '; '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
