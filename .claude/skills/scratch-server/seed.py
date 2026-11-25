"""Seed the scratch box (cwd holds data/): 12 sessions and 57 facts for demo-co, one confidential
session for other-co, recall receipts on the first nine facts, an admin token for mara. Fictional
people and generic projects."""
import os

from analyzer import orgs, store

PEOPLE = ["mara@demo.test"] * 5 + ["jules@demo.test"] * 4 + ["tomasz@demo.test"] * 3
PROJECTS = ["api-gateway", "billing", "infra", "sdk", "webapp"] * 2 + ["infra", "webapp"]
TITLES = ["Retry policy for the payments API", "Move billing resets to UTC", "Stabilise the integration suite",
          "Streaming responses in the SDK", "Checkout page rate limits", "Gateway timeout budget",
          "Invoice PDF rendering", "CI cache warmup", "SDK pagination helper", "Checkout analytics events",
          "Flaky nightly build", "Webapp bundle size"]
TYPES = ["decision", "gotcha", "constraint", "todo", "preference"]
CLAIMS = {
    "decision": ["Retry 429s with exponential backoff and jitter.", "Daily billing resets run at midnight UTC.",
                 "Bind test servers to port 0 and read the port back.", "Stream with a bounded 64 KB buffer.",
                 "Show a retry countdown instead of an error on 429.", "Cap retries at five attempts and 30 seconds total.",
                 "Fixed random seed per test file."],
    "gotcha": ["The vendor returns 429 even at one request a second.", "Local midnight is ambiguous on DST change days.",
               "Hardcoded ports collide with services already bound in CI.", "A retry after the first chunk duplicates output.",
               "Naive client retries stampede after a 429.", "The backoff wrapper hides flaky mocks in tests."],
    "constraint": ["The vendor key allows 100 requests a minute across all workers.", "Invoices keep the customer's local date.",
                   "Tests must be reproducible across CI runs.", "The SDK must not import the gateway's retry wrapper.",
                   "Client retries stay inside the 100 per minute budget."],
    "todo": ["Alert when 429s stay above 1% for five minutes.", "Backfill June resets that ran at local time.",
             "Add coverage for streaming responses.", "Document the retry boundary in the SDK readme.",
             "Show the reset time in the customer's local zone."],
    "preference": ["Keep the retry wrapper in one module.", "Store UTC, format at the edge.",
                   "Keep fixture setup local to each test file.", "Tests bind to port 0 like the integration suite."],
}
QUOTES = ["Let's back off exponentially with jitter on 429s instead of failing the call.", "Reset at 00:00 UTC, not local midnight.",
          "Bind to 0 and read the port back. No fixed ports in tests.", "Bound the buffer at 64 KB and apply backpressure above it.",
          "Show 'try again in 12s' rather than a red banner.", "The limit on this key is 100 per minute, shared by every worker.",
          "A test that passes on retry is a failing test.", "Same trick as infra. Bind to 0.",
          "We still do not assert chunk order or backpressure.", "UTC in the database, convert when rendering."]


def seed_demo():
    con = store.sqlite_db(store.org_db("demo-co"))
    refs = []
    for i in range(12):
        sid = f"demo-s{i + 1}"
        day = 20 + (i % 12)
        start = f"2026-08-{day:02d}T09:{i * 4:02d}:00Z"
        con.execute("INSERT INTO sessions(session_id,project,git_branch,started_at,ended_at,n_user,n_asst,summary,deployed_to_prod,source,user) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, PROJECTS[i], "main", start, start.replace("T09", "T10"), 8 + i, 20 + 2 * i,
                     f"{TITLES[i]}: what was decided and what bit us.", 0, "session", PEOPLE[i]))
        for k in range(3 + (i % 5)):
            t = TYPES[(i + k) % 5]
            ref = f"demo:{sid}:{k}"
            refs.append((ref, sid))
            con.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref,confidence) VALUES(?,?,?,?,?,?,?)",
                        (sid, t, "shared", CLAIMS[t][(i + k) % len(CLAIMS[t])], QUOTES[(i + k) % len(QUOTES)], ref, "high"))
    con.commit()
    store.refresh_facts(con)
    store.build_fts(con)
    con.commit()
    con.close()
    tel = store.sqlite_db(store.org_db("demo-co"))        # attaches the telemetry sibling as `tel`
    for j, (ref, sid) in enumerate(refs[:9]):
        for m in range(1 + j % 3):
            tel.execute("INSERT INTO tel.served(session,asked_at,question,ref,kind,project,person,asked_by,trace_id) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"demo-s{(j + m + 2) % 12 + 1}", f"2026-09-0{1 + m}T10:00:00Z", "how do we retry 429s", ref,
                         "hidden_context", "api-gateway", None, "mara@demo.test", f"tr{j}{m}"))
    tel.commit()
    tel.close()
    return len(refs)


def seed_other():
    con = store.sqlite_db(store.org_db("other-co"))
    con.execute("INSERT INTO sessions(session_id,project,started_at,ended_at,n_user,n_asst,summary,source,user) "
                "VALUES('other-1','secret-proj','2026-09-01T09:00:00Z','2026-09-01T10:00:00Z',5,9,"
                "'OTHERCO CONFIDENTIAL roadmap session','session','zed@other.test')")
    con.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref,confidence) "
                "VALUES('other-1','decision','shared','OTHERCO CONFIDENTIAL: launch moves to October.','OTHERCO board said October.','other:1','high')")
    con.commit()
    store.refresh_facts(con)
    store.build_fts(con)
    con.commit()
    con.close()


if __name__ == "__main__":
    n = seed_demo()
    seed_other()
    orgs.mint_token("mara@demo.test", "demo-co", "mara-mac", role="admin")   # a bearer for admin-only routes
    print(f"seeded demo-co ({n} facts) and other-co; mara@demo.test is admin")
