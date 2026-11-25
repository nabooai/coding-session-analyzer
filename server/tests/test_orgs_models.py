"""control.db's on-disk FORMAT invariants. Model-versus-file parity is tests/test_migrations.py.

  * journal_mode == 'delete': control_db() sets no pragmas, so control.db lives in rollback-journal
    mode. Routing it through db.engine_for would run `PRAGMA journal_mode=WAL` at connect, and WAL
    is PERSISTENT: the file header flips and -wal/-shm siblings appear on every install. That is an
    on-disk behavior change, so the control plane is BRIDGE-ONLY (db.run/run_many on the raw
    connection) and this tripwire fails the moment anyone forgets.
  * no corpus table in control.db and no control table in an org db: the two planes are separate
    MetaData objects and separate migration environments, so neither can conjure the other's tables.
"""
import os
import sqlite3

import pytest

from analyzer import auth, migrations, orgs, sso


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    """(path, con) for a real control db built by orgs.control_db() on a tmp file. The path is
    an EXPLICIT one, so the legacy-token import never fires (it is gated on the no-argument call).
    chdir into tmp_path: store/orgs paths are cwd-relative, so any stray file lands here."""
    monkeypatch.chdir(tmp_path)
    path = str(tmp_path / "control.db")
    con = orgs.control_db(path)
    yield path, con
    con.close()


def test_journal_mode_stays_delete_after_all_flows(control_db, tmp_path, monkeypatch):
    """THE WAL tripwire. Run every control-plane flow class (create/rotate/set_domain/mint/
    lookup-with-touch/revoke/list/audit/delete) and assert the file is still in rollback-journal
    mode with no -wal/-shm siblings. If any of those flows ever routes through db.engine_for,
    the connect-time WAL pragma flips the file persistently and this fails."""
    for k in ("CSA_AUTH", "CSA_AUTH_TOKEN", "CSA_ORG_INVITE", "CSA_TOKEN_TTL_DAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(auth, "discovery", lambda issuer, force=False: auth.Discovery("https://idp.example/a", "t", "j"))
    path, con = control_db
    oid, invite = orgs._create_org("Acme Corp", con=con)         # fixture: bypass the boot-mode guard
    orgs.rotate_invite(oid, con=con)
    orgs.set_domain(oid, "acme.com", con=con)
    orgs.set_sso("https://idp.example", "cid", "secret", "IdP", con=con)   # sso set / show / a login row / clear
    cfg = auth.sso_config(con)
    sso.start(con, cfg, "https://csa.example.com", "/", "")
    assert sso._claim(con, "not-a-real-state") is None
    orgs.clear_sso(con=con)
    tok = orgs.mint_token("dev@acme.com", oid, "laptop", con=con)
    assert orgs.lookup(tok, con=con)[0] == "ok"                  # lookup + _touch write path
    assert orgs.revoke(tok, con=con) is True
    orgs.list_tokens(oid, con=con)
    orgs.list_orgs(con=con)
    orgs.audit_log(oid, con=con)
    orgs.org_count(con=con)
    orgs.delete_org(oid, con=con)
    orgs.request_workspace("Flow Co", "flow@flow.co", con=con)   # workspace-request write + read +
    rid = orgs.list_requests(con=con)[0]["id"]                   # approve flows under the WAL tripwire too
    orgs.approve_request(rid, con=con)
    con.commit()
    # A SECOND org's full lifecycle on the con=None DEFAULT path — the fresh-connection flows a
    # regression routing control_db through engine_for would actually hit (review finding, PR #75:
    # con=con everywhere would never exercise that path). The fixture chdir'd into tmp_path, so the
    # no-argument default resolves to its own control.db under data/, not the fixture's file.
    default_path = str(orgs.control_path())
    assert default_path != path, "the default path must be a different file from the fixture's"
    oid2, _ = orgs._create_org("Beta LLC")                       # no con= -> control_db() default path
    assert os.path.exists(default_path), "the default path must be the file control_db() opens"
    orgs.mint_token("dev@beta.io", oid2, "box")
    orgs.list_orgs()
    # Read journal_mode on a FRESH connection for BOTH files: the persistent WAL header survives
    # even if a transient engine_for con flipped it and closed (an idle con reports its own stale
    # mode, and the -wal/-shm files are gone by then — both evasion windows the review reproduced).
    for p in (path, default_path):
        fresh = sqlite3.connect(p)
        assert fresh.execute("PRAGMA journal_mode").fetchone()[0] == "delete", \
            f"{p} must stay rollback-journal — engine_for would flip it to persistent WAL"
        assert fresh.execute("SELECT version_num FROM alembic_version").fetchone()[0] \
            == migrations.head("control")
        fresh.close()
        assert not os.path.exists(p + "-wal") and not os.path.exists(p + "-shm"), \
            f"WAL side files next to {p} mean something WAL-ized it"


def test_reopen_changes_nothing(control_db):
    """control_db() runs on every request: a second open must change neither schema nor data."""
    path, con = control_db
    con.execute("INSERT INTO orgs(id,name,invite_hash,created) VALUES ('a','A','h','2026-01-01T00:00:00Z')")
    con.commit()
    before = sqlite3.connect(path).execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    con2 = orgs.control_db(path)
    assert sqlite3.connect(path).execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
    assert con2.execute("SELECT id,name FROM orgs").fetchall() == [("a", "A")]
    con2.close()


def test_no_org_db_ever_holds_control_plane_tables(tmp_path, monkeypatch):
    """Tripwire (PR #75 auth-attacker sim): the control-plane tables once shared a DeclarativeBase
    with the corpus models, so a bare create_all on an org db would have conjured user_tokens INTO
    a corpus, and _import_legacy_tokens reads user_tokens out of a data-plane file on first
    control_db() — a data->control credential-escalation vector. They are separate MetaData objects
    and separate migration environments now, and this pins the outcome: a real org db, opened the
    normal way, must contain NONE of the control-plane tables."""
    from analyzer import store
    org_db = str(tmp_path / "org" / "analyzer.db")
    store.sqlite_db(org_db).close()                              # a corpus db the normal way
    con = sqlite3.connect(org_db)
    present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    for control_table in ("orgs", "user_tokens", "admin_log", "workspace_requests", "cli_pairings"):
        assert control_table not in present, \
            f"control-plane table {control_table!r} leaked into an org db — data->control escalation surface"
    # and the corpus's OWN control model (tenant_config) is present, proving this isn't vacuous
    assert "tenant_config" in present
