"""Adversarial redaction: parity fixtures + a storage E2E.

This is the PYTHON HALF of the client<->server byte-parity proof. It and client/redact_fixtures_test.go
load the SAME docs/redaction_samples.json and assert the SAME must_redact / must_keep / labels. Both
green on one shared fixture set == the Go client and Python server redactors behave identically.

Plus a storage E2E: a synthesized transcript carrying every leak class flows through the real ingest
gate (ingest.load_content, mode="strict") into a :memory: SQLite + FTS via store, proving strict
redaction reaches the DB and that redaction_mode persists.
"""
import json
import os

from analyzer import redact, ingest, store

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "..", "..", "docs", "redaction_samples.json")


def test_shared_fixtures():
    """Every fixture through redact.redact(): must_redact absent, must_keep present, labels == EXACT
    fired set. Exact-set (not subset) so over- AND under-redaction both fail — the strong parity bar."""
    cases = json.load(open(SAMPLES))["cases"]
    assert cases, "no fixture cases loaded from redaction_samples.json"
    for c in cases:
        name = c["name"]
        out, found = redact.redact(c["input"], c.get("mode", "secrets"), c.get("deny", []), c.get("allow", []))
        for bad in c.get("must_redact", []):
            assert bad not in out, f"{name}: must_redact {bad!r} LEAKED -> {out!r}"
        for good in c.get("must_keep", []):
            assert good in out, f"{name}: must_keep {good!r} MISSING -> {out!r}"
        assert set(found) == set(c.get("labels", [])), \
            f"{name}: fired labels {sorted(set(found))} != expected {sorted(set(c.get('labels', [])))} -> {out!r}"


def test_secrets_mode_strips_keys_and_keeps_emails():
    s = "use sk-abcdEFGH1234567890ijklmnop, mail mara@example.com, AKIAABCDEFGHIJKLMNOP, tok ghp_" + "x" * 25
    out, found = redact.redact(s)
    assert "sk-abcd" not in out and "AKIAABCD" not in out and "ghp_xxx" not in out, out
    assert "mara@example.com" in out
    assert set(found) >= {"openai_key", "aws_key", "github_token"}, found
    assert redact.redact("just normal text") == ("just normal text", [])


def test_a_token_glued_to_a_non_ascii_letter_is_still_caught():
    assert redact.redact("\u00e9ghp_0123456789abcdef")[0] == "\u00e9[REDACTED:github_token]"
    assert redact.redact("host db01.internal\u00e9", "privacy")[0] == "host [REDACTED:hostname]\u00e9"


def test_no_prefix_match_inside_a_word_and_no_loss_at_real_boundaries():
    for benign in ("ask-questions-as-decisions", "task-handoff-protocol-memory",
                   "risk-assessment-checklist-doc", "disk-usage-monitoring-alerts", "sk-learn"):
        assert redact.redact(f"branch {benign} ok") == (f"branch {benign} ok", []), benign
    assert redact.redact("commit 356a192b7913b04c54574d18c28d46e6395428ab")[1] == []
    assert redact.redact("id 550e8400-e29b-41d4-a716-446655440000")[1] == []
    k = "sk-abcdEFGH1234567890ijklmnop"
    for pre, post in (("", ""), ("OPENAI_API_KEY=", ""), ('"', '"'), ("(", ")"), ("Bearer ", ""), ("\n", "")):
        assert redact.redact(f"{pre}{k}{post}")[0] == f"{pre}[REDACTED:openai_key]{post}", pre


def test_privacy_is_additive_and_secrets_leaves_it_alone():
    p = ("login user@corp.example bearer abcdef12345678 "
         "postgres://u:p@db:5432/x password=hunter2 host db01.internal ok")
    out, found = redact.redact(p, "privacy")
    for gone, label in (("user@corp.example", "email"), ("bearer abcdef12345678", "bearer"),
                        ("postgres://u:p@db", "conn_string"), ("hunter2", "credential"),
                        ("db01.internal", "hostname")):
        assert gone not in out and label in found, (gone, out)
    out, found = redact.redact(p, "secrets")
    assert "user@corp.example" in out and "db01.internal" in out and "password=[REDACTED:credential]" in out, out
    assert found == ["credential"]
    public = "see docs.github.com and google.com"
    assert redact.redact(public, "privacy")[0] == public


def test_deny_is_a_case_insensitive_substring_and_allow_wins():
    d = "codename Bluefin ships; keep-me stays; ping ops@example.com"
    out, found = redact.redact(d, "strict", deny=["Bluefin", "keep-me"], allow=["keep-me", "ops@example.com"])
    assert "Bluefin" not in out and "denylisted" in found, out
    assert "keep-me" in out and "ops@example.com" in out, out
    assert "[REDACTED:denylisted]" in redact.redact("BLUEFIN-2 launch", "strict", deny=["bluefin"])[0]


def test_max_mode():
    assert redact.max_mode("secrets", "strict") == "strict" and redact.max_mode("privacy", "secrets") == "privacy"
    assert redact.max_mode("secrets", "secrets") == "secrets" and redact.max_mode("bogus", "privacy") == "privacy"


def test_redacted_jsonl_still_parses():
    """The client redacts raw JSONL lines, so a fixture that is a JSON line must stay one."""
    for c in json.load(open(SAMPLES))["cases"]:
        try:
            json.loads(c["input"])
        except ValueError:
            continue
        out, _ = redact.redact(c["input"], c.get("mode", "secrets"), c.get("deny", []), c.get("allow", []))
        json.loads(out)


def test_storage_e2e():
    """A transcript with a name, email, internal hostname, ghp_ token, codename, DB URL, and prose secret
    goes through the WRITE-TIME gate (ingest.load_content, strict) then into SQLite + FTS. Assert none of
    the leak classes survive the transcript OR reach the DB, and that redaction_mode persists as 'strict'.

    deny = [name, codename, hostname]: name + codename are the STRICT-ONLY leak classes (regex can't catch
    them), so proving those classes requires them on the deny list. The hostname is also caught by the
    privacy tier (strict >= privacy); it is on deny too as belt-and-suspenders."""
    name, codename, host = "Sarah", "Bluefin", "db01.internal"
    tok = "ghp_0123456789abcdef"
    email = "sarah@acme.com"
    dburl = "postgres://app:s3cr3tPW@pg.example.com:5432/prod"
    prose = "password=hunter2Pass"
    leaks = [name, codename, host, tok, email, "s3cr3tPW", "hunter2Pass"]

    raw = "\n".join([
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "user", "content": f"{name} is deploying {codename} to {host}, token {tok}"}}),
        json.dumps({"timestamp": "2026-01-01T00:00:01Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "assistant", "content": [{"type": "text",
                                "text": f"connected via {dburl}; contact {email}; set {prose}"}]}}),
        json.dumps({"timestamp": "2026-01-01T00:00:02Z", "cwd": "/Users/u/Documents/Git/web",
                    "message": {"role": "user", "content": f"confirm {codename} on {host} is done"}}),
    ])

    sess = ingest.load_content("e2e-sid", raw, mode="strict", deny=[name, codename, host], allow=[])
    assert sess is not None and sess["n_user"] >= 2, f"e2e transcript should have >=2 user msgs: {sess}"
    transcript = sess["transcript"]
    for leak in leaks:
        assert leak not in transcript, f"E2E: leak {leak!r} survived the ingest redaction gate -> {transcript!r}"
    assert sess["redaction_mode"] == "strict", f"E2E: ingest should carry redaction_mode=strict, got {sess.get('redaction_mode')!r}"

    # Persist through the real store: sessions row + FTS docs. Minimal fake extract (NO LLM call).
    sess["redaction_mode"] = "strict"
    con = store.sqlite_db(":memory:")
    store.load_sqlite(con, sess, {"summary": "s", "hidden_context": [], "entities": []})
    store.build_fts(con)

    mode_row = con.execute("SELECT redaction_mode FROM sessions WHERE session_id='e2e-sid'").fetchone()
    assert mode_row and mode_row[0] == "strict", f"E2E: sessions.redaction_mode should persist as 'strict', got {mode_row}"

    sess_row = con.execute("SELECT * FROM sessions WHERE session_id='e2e-sid'").fetchone()
    sess_blob = " ".join("" if v is None else str(v) for v in sess_row)
    docs_blob = " ".join((t or "") for (t,) in con.execute("SELECT text FROM docs").fetchall())
    for leak in leaks:
        assert leak not in sess_blob, f"E2E: leak {leak!r} reached the sessions row -> {sess_blob!r}"
        assert leak not in docs_blob, f"E2E: leak {leak!r} reached the FTS docs table -> {docs_blob!r}"
    con.close()


def test_max_mode_takes_the_higher_of_declared_and_minimum():
    assert redact.max_mode("secrets", "strict") == "strict"
    assert redact.max_mode("privacy", "secrets") == "privacy"
    assert redact.max_mode("secrets", "secrets") == "secrets"


def test_secrets_mode_strips_a_credential_value_an_older_client_shipped():
    """The default mode keeps `password=` and drops its value at the ingest gate, so a client without the
    rule cannot get it into the corpus. A reference to where the secret lives is not a secret."""
    raw = "\n".join(json.dumps({"timestamp": f"2026-01-01T00:00:0{i}Z", "cwd": "/Users/u/Documents/Git/web",
                                "message": {"role": role, "content": text}})
                    for i, (role, text) in enumerate([
                        ("user", "the broker needs auth"),
                        ("assistant", "Connect with password=hunter2-sekrit only via env"),
                        ("user", "use password=$BROKER_PASSWORD instead"),
                    ]))
    sess = ingest.load_content("cred-sid", raw, mode="secrets", deny=[], allow=[])
    assert "hunter2-sekrit" not in sess["transcript"]
    assert "password=[REDACTED:credential]" in sess["transcript"]
    assert "password=$BROKER_PASSWORD" in sess["transcript"]


def test_credential_rule_keeps_prose_and_references():
    for text in ("password reset flow", "the secret is to cache", "token: the bearer header",
                 "password=$DB_PASSWORD", 'password="${DB_PASSWORD}"', 'password=os.environ["DB_PASSWORD"]',
                 "pwd=/Users/mara/project", "password=abc"):
        assert redact.redact(text) == (text, []), text
    assert redact.redact("password=hunter2-sekrit") == ("password=[REDACTED:credential]", ["credential"])
    assert redact.redact("password=hunter2-sekrit", allow=["hunter2-sekrit"]) == ("password=hunter2-sekrit", [])
