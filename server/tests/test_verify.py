"""registries-verify acceptance suite: fixture registry, no
network — gh/git live behind stubable seams (`sync_github(fetch=)`, `_gh_members` never called
here). The idempotency bar is `con.total_changes` deltas, not byte-identity (WAL/freelist churn
makes byte-identity a false bar for SQLite)."""
import sqlite3

import pytest

from analyzer import store, verify

AS_OF = "2026-08-01T00:00:00Z"


@pytest.fixture()
def con(tmp_path, monkeypatch):
    for env in ("CSA_REGISTRY_SOURCES", "CSA_REGISTRY_REFUTE"):
        monkeypatch.delenv(env, raising=False)
    c = store.sqlite_db(str(tmp_path / "analyzer.db"))
    yield c
    c.close()


def _fact(con, ref, claim, sid="s1", user="mara@example.com", type_="constraint",
          at="2026-07-15T09:00:00Z", scope="organizational"):
    con.execute("INSERT OR IGNORE INTO sessions(session_id,project,started_at,ended_at,user) "
                "VALUES(?,?,?,?,?)", (sid, "proj", at, at, user))
    con.execute("INSERT INTO hidden_context(session_id,type,scope,claim,evidence,ref) "
                "VALUES(?,?,?,?,?,?)", (sid, type_, scope, claim, "test-evidence", ref))
    store.refresh_facts(con, refs={ref})


def _cfg(con, sources, refute=""):
    con.execute("INSERT OR REPLACE INTO tenant_config(id,registry_sources,registry_refute) "
                "VALUES(1,?,?)", (sources, refute))


def _reg(con, kind, key, value, source, as_of=AS_OF):
    con.execute("INSERT OR REPLACE INTO registry(kind,key,value,as_of,source) VALUES(?,?,?,?,?)",
                (kind, key, value, as_of, source))


def _person(con, name):
    con.execute("INSERT OR IGNORE INTO entities(id,type,name) VALUES(?,?,?)",
                (name.lower(), "person", name))


def _receipt(con, ref):
    return con.execute("SELECT status, note FROM reviews WHERE kind='registry' AND ref=?",
                       (ref,)).fetchone()


# --- acceptance box 1: member + roster containing X -> verified, floor, receipt ---------------

def test_member_verified_floor_receipt(con):
    _cfg(con, "sso")
    _reg(con, "member", "jules@example.com", "default", "sso")
    _person(con, "Jules")
    _fact(con, "refjule00001", "Jules is a member of the org")
    touched = verify.verify_all(con)
    state, conf = con.execute("SELECT state, confidence FROM facts WHERE ref='refjule00001'").fetchone()
    assert state == "verified" and conf >= verify.VERIFIED_FLOOR
    status, note = _receipt(con, "refjule00001")
    assert status == "verified" and "registry sso" in note and AS_OF in note
    assert "refjule00001" in touched


# --- acceptance box 2: the Priya case --------------------------------------------------------

def _priya(con, refute="sso"):
    _cfg(con, "sso", refute)
    _reg(con, "member", "mara@example.com", "default", "sso")
    _person(con, "Priya")
    _fact(con, "seedpriya001", "Priya is the on-call DevOps lead and must approve lab deploys")


def test_priya_refuted_with_receipt_edge_and_window(con):
    _priya(con)
    verify.verify_all(con)
    state, invalid_at, expired_at, claim, conf = con.execute(
        "SELECT state, invalid_at, expired_at, claim, confidence FROM facts "
        "WHERE ref='seedpriya001'").fetchone()
    assert state == "superseded" and invalid_at == AS_OF and expired_at
    assert conf == 0.0                                         # refuted -> floor zero (design rule 1)
    assert claim.startswith("Priya is the on-call")            # loser kept, window closed
    src, note, at = con.execute("SELECT src_ref, note, at FROM fact_edges WHERE rel='supersedes' "
                                "AND dst_ref='seedpriya001'").fetchone()
    assert "registry" in note and "sso" in note and at == AS_OF
    sstate, sconf = con.execute("SELECT state, confidence FROM facts WHERE ref=?", (src,)).fetchone()
    assert sstate == "verified" and sconf >= verify.VERIFIED_FLOOR   # winner is registry-backed
    # the synthetic winner survives a FULL refresh (mention-backed, never culled)
    store.refresh_facts(con)
    assert con.execute("SELECT COUNT(*) FROM fact_edges WHERE dst_ref='seedpriya001'").fetchone()[0] == 1


def test_second_pass_changes_zero_rows(con):
    _priya(con)
    verify.verify_all(con)
    before = con.total_changes
    assert verify.verify_all(con) == set()
    assert con.total_changes == before


# --- acceptance negatives ---------------------------------------------------------------------

def test_unconfigured_tenant_zero_writes(con):
    _reg(con, "member", "mara@example.com", "default", "sso")     # rows exist, but no token
    _person(con, "Priya")
    _fact(con, "seedpriya001", "Priya is the on-call DevOps lead and must approve lab deploys")
    before = con.total_changes
    assert verify.verify_all(con) == set()
    assert con.total_changes == before
    assert con.execute("SELECT state FROM facts WHERE ref='seedpriya001'").fetchone()[0] == "extracted"


def test_membership_without_github_source_stays_unverifiable(con):
    _cfg(con, "sso", "sso")                                    # sso armed; github NOT configured
    _reg(con, "member", "jules@example.com", "default", "sso")
    _person(con, "Jules")
    _fact(con, "refghjule001", "Jules is in the GitHub org")
    verify.verify_all(con)
    state, invalid_at, conf = con.execute(
        "SELECT state, invalid_at, confidence FROM facts WHERE ref='refghjule001'").fetchone()
    assert state == "unverifiable" and invalid_at is None and conf is None   # zero penalty
    assert con.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0] == 0  # zero edges


def test_absent_without_refute_optin_is_dry_run(con):
    _priya(con, refute="")                                     # connected, NOT refute-armed
    verify.verify_all(con)
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='seedpriya001'").fetchone()
    assert state == "extracted" and invalid_at is None
    assert con.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0] == 0
    status, note = _receipt(con, "seedpriya001")
    assert status == "would-refute" and "dry-run" in note


# --- refutation safety rails -------------------------------------------------------------------

def test_no_person_evidence_never_touched(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "mara@example.com", "default", "sso")
    con.execute("INSERT INTO entities(id,type,name) VALUES('postgres','technology','Postgres')")
    _fact(con, "refpg0000001", "Postgres is the session store manager")
    _fact(con, "refacl000001", "There is no ACL on corpus sessions so any teammate can read all")
    before = con.total_changes
    assert verify.verify_all(con) == set()
    assert con.total_changes == before                          # both invisible to verify


def test_past_tense_never_refuted(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "mara@example.com", "default", "sso")
    _person(con, "Priya")
    _fact(con, "refwas000001", "Priya was the on-call DevOps lead")
    verify.verify_all(con)
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='refwas000001'").fetchone()
    assert invalid_at is None and state in ("extracted", "unverifiable")


def test_past_tense_never_verified_either(con):
    """Review #2: a current snapshot can't CONFIRM a past claim any more than falsify it —
    'was a member' + present-today must not earn the 0.9 floor."""
    _cfg(con, "sso")
    _reg(con, "member", "jules@example.com", "default", "sso")
    _person(con, "Jules")
    _fact(con, "refwasmem001", "Jules was a member of the org")
    verify.verify_all(con)
    state, conf = con.execute(
        "SELECT state, confidence FROM facts WHERE ref='refwasmem001'").fetchone()
    assert state in ("extracted", "unverifiable") and conf is None
    assert con.execute("SELECT COUNT(*) FROM reviews WHERE kind='registry' "
                       "AND ref='refwasmem001' AND status='verified'").fetchone()[0] == 0


def test_unrefute_never_reopens_foreign_supersessions(con):
    """Review #1: a fact superseded by S5/a human (non-registry actor) must be HELD even when the
    registry answers its subject positively — precedence belongs to the resolution engine."""
    _cfg(con, "sso", "sso")
    _reg(con, "member", "sam@example.com", "default", "sso")
    _person(con, "Sam")
    _fact(con, "refsamheld01", "Sam is a member of the org")
    con.execute("UPDATE facts SET state='superseded', invalid_at='2026-07-01T00:00:00Z', "
                "expired_at='2026-07-01T00:00:00Z' WHERE ref='refsamheld01'")
    con.execute("INSERT INTO fact_edges(src_ref,rel,dst_ref,at,actor,note) "
                "VALUES('humanwinner1','supersedes','refsamheld01','2026-07-01T00:00:00Z',"
                "'mara@example.com','human resolution')")
    verify.verify_all(con)
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='refsamheld01'").fetchone()
    assert state == "superseded" and invalid_at == "2026-07-01T00:00:00Z"   # held, untouched


def test_empty_synced_slice_never_refutes(con):
    """Review Q6: a synced-but-EMPTY roster (wiped control db) must refute nothing — 'nobody
    exists' is a broken sync, not evidence."""
    _cfg(con, "sso", "sso")
    ctl = sqlite3.connect(":memory:")
    ctl.execute("CREATE TABLE user_tokens(token_hash TEXT, email TEXT, org_id TEXT, "
                "revoked_at TEXT, expires_at TEXT)")
    ctl.execute("INSERT INTO user_tokens VALUES('h','a@x.com','default',NULL,NULL)")
    verify.sync_roster(con, "default", control_con=ctl)         # non-empty sync...
    ctl.execute("DELETE FROM user_tokens")
    verify.sync_roster(con, "default", control_con=ctl)         # ...then wiped (meta row keeps as_of)
    _person(con, "Priya")
    _fact(con, "seedpriya001", "Priya is the on-call DevOps lead and must approve lab deploys")
    verify.verify_all(con)
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='seedpriya001'").fetchone()
    assert invalid_at is None and state in ("extracted", "unverifiable")
    assert con.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0] == 0


def test_role_value_mismatch_abstains(con):
    _cfg(con, "github:acme", "github:acme")
    _reg(con, "member", "octocat", "github:acme", "github:acme")
    _reg(con, "role", "octocat", "admin", "github:acme")
    _person(con, "Octocat")
    _fact(con, "refocto00001", "Octocat is the CTO")
    verify.verify_all(con)
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='refocto00001'").fetchone()
    assert state == "unverifiable" and invalid_at is None       # GitHub doesn't model "CTO"
    assert con.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0] == 0


def test_role_exact_match_verifies(con):
    _cfg(con, "github:acme")
    _reg(con, "member", "octocat", "github:acme", "github:acme")
    _reg(con, "role", "octocat", "admin", "github:acme")
    _person(con, "Octocat")
    _fact(con, "refocto00002", "Octocat is an admin")
    verify.verify_all(con)
    assert con.execute("SELECT state FROM facts WHERE ref='refocto00002'").fetchone()[0] == "verified"


def test_repo_touch_never_refutes(con):
    _cfg(con, "git:/tmp/ledger", "git:/tmp/ledger")                # even armed, repo class won't refute
    _reg(con, "committer", "jules@example.com", "ledger", "git:/tmp/ledger")
    _person(con, "Jules")
    _person(con, "Priya")
    _fact(con, "refrepo00001", "Jules maintains ledger")
    _fact(con, "refrepo00002", "Priya maintains ledger")
    verify.verify_all(con)
    assert con.execute("SELECT state FROM facts WHERE ref='refrepo00001'").fetchone()[0] == "verified"
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='refrepo00002'").fetchone()
    assert state == "unverifiable" and invalid_at is None
    assert con.execute("SELECT COUNT(*) FROM fact_edges").fetchone()[0] == 0


def test_ambiguous_subject_abstains(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "dan.a@example.com", "default", "sso")
    _reg(con, "member", "dan.b@example.com", "default", "sso")
    _person(con, "Dan")
    _fact(con, "refdan000001", "Dan is a member of the org")
    verify.verify_all(con)
    state, invalid_at = con.execute(
        "SELECT state, invalid_at FROM facts WHERE ref='refdan000001'").fetchone()
    assert state == "unverifiable" and invalid_at is None       # two Dans: never guess


def test_blast_radius_cap_parks_the_sweep(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "mara@example.com", "default", "sso")
    for i, name in enumerate(("Alice", "Bob", "Carol", "Dave", "Erin")):
        _person(con, name)
        _fact(con, f"refblast000{i}", f"{name} is a member of the org", sid=f"sb{i}")
    verify.verify_all(con)
    assert con.execute("SELECT COUNT(*) FROM facts WHERE state='superseded'").fetchone()[0] == 0
    parked = con.execute("SELECT COUNT(*) FROM reviews WHERE kind='registry' "
                         "AND status='would-refute' AND note LIKE '%parked%'").fetchone()[0]
    assert parked == 5                                          # receipts instead of a mass refutation


# --- un-refute: refutation must not be a one-way door -------------------------------------------

def test_unrefute_when_registry_answers_again(con):
    _priya(con)
    verify.verify_all(con)
    assert con.execute("SELECT state FROM facts WHERE ref='seedpriya001'").fetchone()[0] == "superseded"
    _reg(con, "member", "priya@example.com", "default", "sso")     # Priya (re)joins
    verify.verify_all(con)
    state, invalid_at, expired_at = con.execute(
        "SELECT state, invalid_at, expired_at FROM facts WHERE ref='seedpriya001'").fetchone()
    # reopened (absence-refutation basis gone) but the ROLE itself is still unanswerable
    assert state == "unverifiable" and invalid_at is None and expired_at is None
    conf = con.execute("SELECT confidence FROM facts WHERE ref='seedpriya001'").fetchone()[0]
    assert conf is None                                         # not the refuted-era 0.0 (review #3)
    assert con.execute("SELECT COUNT(*) FROM fact_edges WHERE dst_ref='seedpriya001'").fetchone()[0] == 0
    assert con.execute(                                         # synthetic winner culled with its mention
        "SELECT COUNT(*) FROM facts WHERE author LIKE 'registry:%'").fetchone()[0] == 0


def test_unrefute_member_claim_reopens_verified(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "mara@example.com", "default", "sso")
    _person(con, "Sam")
    _fact(con, "refsam000001", "Sam is a member of the org")
    verify.verify_all(con)
    assert con.execute("SELECT state FROM facts WHERE ref='refsam000001'").fetchone()[0] == "superseded"
    _reg(con, "member", "sam@example.com", "default", "sso")
    verify.verify_all(con)
    state, conf = con.execute(
        "SELECT state, confidence FROM facts WHERE ref='refsam000001'").fetchone()
    assert state == "verified" and conf >= verify.VERIFIED_FLOOR


# --- sim-round friction fixes ---------------------------------------------------------------------

def test_offboard_refutation_dated_to_the_departure_sync(con):
    """Sim F2: a shrink-only re-sync must date the absence to the sync that SAW the departure —
    not to whenever some surviving row last changed (the kind='sync' meta row carries it)."""
    _cfg(con, "sso", "sso")
    ctl = sqlite3.connect(":memory:")
    ctl.execute("CREATE TABLE user_tokens(token_hash TEXT, email TEXT, org_id TEXT, "
                "revoked_at TEXT, expires_at TEXT)")
    ctl.executemany("INSERT INTO user_tokens VALUES(?,?,'default',NULL,NULL)",
                    [("h1", "dana@example.com"), ("h2", "mara@example.com")])
    verify.sync_roster(con, "default", control_con=ctl)
    t0 = con.execute("SELECT as_of FROM registry WHERE kind='sync' AND source='sso'").fetchone()[0]
    _person(con, "Dana")
    _fact(con, "refdana00001", "Dana is a member of the org")
    verify.verify_all(con)
    assert con.execute("SELECT state FROM facts WHERE ref='refdana00001'").fetchone()[0] == "verified"
    ctl.execute("UPDATE user_tokens SET revoked_at='x' WHERE email='dana@example.com'")   # offboard
    verify.sync_roster(con, "default", control_con=ctl)
    t1 = con.execute("SELECT as_of FROM registry WHERE kind='sync' AND source='sso'").fetchone()[0]
    assert t1 >= t0                                             # meta row moved with the deletion
    verify.verify_all(con)
    invalid_at = con.execute(
        "SELECT invalid_at FROM facts WHERE ref='refdana00001'").fetchone()[0]
    assert invalid_at == t1                                     # dated to the departure sync, not
    #                                                             a stale surviving row's as_of


def test_verdict_history_survives_in_signals(con):
    """Sim F3: reviews is last-write-only (PK kind,ref); the flip-flop history must survive in
    signals append-only."""
    _priya(con, refute="")                                      # dry-run first
    verify.verify_all(con)
    _cfg(con, "sso", "sso")                                     # then arm and refute
    verify.verify_all(con)
    kinds = {k for (k,) in con.execute(
        "SELECT kind FROM signals WHERE ref='seedpriya001' AND kind LIKE 'registry%'")}
    assert kinds == {"registry_would_refute", "registry_refuted"}
    assert con.execute("SELECT status FROM reviews WHERE kind='registry' AND ref='seedpriya001'"
                       ).fetchone()[0] == "refuted"             # register holds only the current one


def test_garbage_tokens_behave_as_unconfigured(con):
    """Sim S3-1/2/3: a typo'd registry_sources must be indistinguishable from no config — zero
    writes (no sticky 'unverifiable'), no github dispatch for an empty org arg."""
    _cfg(con, "okta,ftp://x, github:, github:x?per_page=1")     # last one: gh-api path injection
    _person(con, "Alice")
    _fact(con, "refalice0001", "Alice is a member of the org")
    before = con.total_changes
    assert verify.verify_all(con) == set()
    assert con.total_changes == before
    assert con.execute("SELECT state FROM facts WHERE ref='refalice0001'").fetchone()[0] == "extracted"
    called = []
    verify.sync_org(con, "default", fetch=lambda *a, **k: called.append(a) or [])
    assert called == []                                         # 'github:' never dispatched


def test_sync_commits_visible_to_fresh_connection(con, tmp_path):
    """Sim F1: sync_* are public entry points and must commit — an operator's setup-script sync
    silently vanishing on close was the worst onboarding foot-gun."""
    db = str(tmp_path / "analyzer.db")
    c1 = store.sqlite_db(db)
    ctl = sqlite3.connect(":memory:")
    ctl.execute("CREATE TABLE user_tokens(token_hash TEXT, email TEXT, org_id TEXT, "
                "revoked_at TEXT, expires_at TEXT)")
    ctl.execute("INSERT INTO user_tokens VALUES('h','a@x.com','default',NULL,NULL)")
    verify.sync_roster(c1, "default", control_con=ctl)
    c1.close()                                                  # no explicit commit by the caller
    c2 = sqlite3.connect(db)
    assert c2.execute("SELECT COUNT(*) FROM registry WHERE kind='member'").fetchone()[0] == 1
    c2.close()


# --- sync: idempotent reconcile, scoped re-verify ------------------------------------------------

def test_sync_twice_identical_rows_and_changed_keys(con):
    rows = {("member", "a@x.com"): "default", ("member", "b@x.com"): "default"}
    assert verify._reconcile(con, "sso", rows) == {"a@x.com", "b@x.com"}
    snap1 = con.execute("SELECT * FROM registry ORDER BY kind,key,source").fetchall()
    assert verify._reconcile(con, "sso", rows) == set()         # idempotent: zero changed keys
    assert con.execute("SELECT * FROM registry ORDER BY kind,key,source").fetchall() == snap1
    assert verify._reconcile(con, "sso", {("member", "a@x.com"): "default"}) == {"b@x.com"}
    assert con.execute("SELECT COUNT(*) FROM registry WHERE kind='member'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM registry WHERE kind='sync'").fetchone()[0] == 1


def test_changed_value_retriggers_affected_only(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "jules@example.com", "default", "sso")
    _reg(con, "member", "tomasz@example.com", "default", "sso")
    _person(con, "Jules"), _person(con, "Tomasz")
    _fact(con, "refjule00001", "Jules is a member of the org", sid="sd")
    _fact(con, "reftoma00001", "Tomasz is a member of the org", sid="sg")
    verify.verify_all(con)
    _reg(con, "member", "tomasz@example.com", "acme", "sso", as_of="2026-08-02T00:00:00Z")
    touched = verify.verify_refs(con, only_keys={"tomasz@example.com"})
    assert touched == {"reftoma00001"}                          # jules's verdict untouched


def test_slices_are_independent(con):
    """Two github orgs / two repos never wipe each other (source = the full token)."""
    verify._reconcile(con, "github:a", {("member", "dev"): "github:a"})
    verify._reconcile(con, "github:b", {("member", "dev"): "github:b"})
    assert con.execute("SELECT COUNT(*) FROM registry WHERE key='dev'").fetchone()[0] == 2
    verify._reconcile(con, "github:a", {})                      # org a empties
    assert [r[0] for r in con.execute("SELECT source FROM registry WHERE key='dev'")] == ["github:b"]


def test_sync_github_stubbed(con):
    calls = []

    def fetch(org, role=None):
        calls.append((org, role))
        return ["boss"] if role == "admin" else ["boss", "dev"]

    changed = verify.sync_github(con, "acme", fetch=fetch)
    assert calls == [("acme", None), ("acme", "admin")]
    assert changed == {"boss", "dev"}
    rows = dict(((k, key), v) for k, key, v in con.execute("SELECT kind,key,value FROM registry"))
    assert rows[("member", "dev")] == "github:acme" and rows[("role", "boss")] == "admin"
    assert rows[("role", "dev")] == "member"


def test_sync_roster_ignores_idle_expiry(con):
    ctl = sqlite3.connect(":memory:")
    ctl.execute("CREATE TABLE user_tokens(token_hash TEXT, email TEXT, org_id TEXT, "
                "revoked_at TEXT, expires_at TEXT)")
    ctl.executemany("INSERT INTO user_tokens VALUES(?,?,?,?,?)", [
        ("h1", "on-vacation@x.com", "default", None, "2020-01-01T00:00:00Z"),  # idle-expired: KEPT
        ("h2", "offboarded@x.com", "default", "2026-07-01T00:00:00Z", None),   # revoked: dropped
        ("h3", "active@x.com", "default", None, None)])
    verify.sync_roster(con, "default", control_con=ctl)
    keys = {k for (k,) in con.execute("SELECT key FROM registry WHERE source='sso' AND kind='member'")}
    assert keys == {"on-vacation@x.com", "active@x.com"}


# --- ingest wiring -------------------------------------------------------------------------------

def test_load_sqlite_runs_verify_and_returns_synthetic_refs(con):
    _cfg(con, "sso", "sso")
    _reg(con, "member", "mara@example.com", "default", "sso")
    sess = {"session_id": "ing1", "project": "proj", "git_branch": None,
            "started_at": "2026-07-15T09:00:00Z", "ended_at": "2026-07-15T10:00:00Z",
            "n_user": 1, "n_asst": 1}
    data = {"summary": "s", "hidden_context": [
        {"type": "constraint", "scope": "organizational",
         "claim": "Priya is the on-call DevOps lead and must approve lab deploys", "evidence": "e"}],
        "entities": [{"id": "priya", "type": "person", "name": "Priya"}]}
    refs = store.load_sqlite(con, sess, data)
    target = store.hc_ref("Priya is the on-call DevOps lead and must approve lab deploys")
    assert con.execute("SELECT state FROM facts WHERE ref=?", (target,)).fetchone()[0] == "superseded"
    synth = con.execute("SELECT src_ref FROM fact_edges WHERE dst_ref=?", (target,)).fetchone()[0]
    assert synth in refs                                        # index_session will doc-index the winner


def test_sweep_reaches_docs(con):
    """Deliverable 2's 'facts age; registries change': a SWEEP verdict must hit the docs validity
    columns (what search filters on), not just `facts`."""
    _priya(con)
    store.build_fts(con)
    assert con.execute("SELECT invalid_at FROM docs WHERE doc_id LIKE 'hc%' "
                       "AND text LIKE '%on-call DevOps lead%'").fetchone()[0] is None
    ctl = sqlite3.connect(":memory:")
    ctl.execute("CREATE TABLE user_tokens(token_hash TEXT, email TEXT, org_id TEXT, "
                "revoked_at TEXT, expires_at TEXT)")
    ctl.execute("INSERT INTO user_tokens VALUES('h','mara@example.com','default',NULL,NULL)")
    n = verify.sync_and_sweep(con, "default", control_con=ctl)
    assert n >= 1
    assert con.execute("SELECT invalid_at FROM docs WHERE doc_id LIKE 'hc%' "
                       "AND text LIKE '%on-call DevOps lead%'").fetchone()[0] == AS_OF
    assert con.execute("SELECT COUNT(*) FROM docs WHERE text LIKE "
                       "'%Priya is not in the active CSA identity registry%'"
                       ).fetchone()[0] == 1                     # the synthetic winner got indexed too
