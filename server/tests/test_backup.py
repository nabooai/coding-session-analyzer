"""backup._self_check ported into the suite (the P4 convention — see tests/test_orgs.py), plus the
multi-tenant cases that are the whole point of the module: the control plane and every hosted org
must survive a backup/restore round trip, taken while writers hold the databases open.

The fixtures build the tree with the REAL store/orgs code rather than hand-rolled SQLite files, so
a layout change in store.org_dir cannot quietly pass here and fail on the box."""
import json
import os
import shutil
import sqlite3
import sys

import pytest

import apiserver
import csa
from analyzer import archive, backup, orgs, queue, store, tenant


@pytest.fixture()
def box(monkeypatch, tmp_path):
    """A prod-shaped scratch box: control plane + default corpus + two hosted tenants, with the
    connections LEFT OPEN — a backup that only works against idle files is not the case we ship."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")                # create_org refuses single-tenant modes
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    cons = []

    def seed(org_id, marker, n):
        con = store.sqlite_db(store.org_db(org_id))
        con.execute("INSERT INTO sessions(session_id, started_at, summary) VALUES(?,?,?)",
                    (f"{marker}-sess", "2026-08-31T00:00:00Z", f"{marker} summary"))
        con.executemany("INSERT INTO hidden_context(session_id, type, claim, ref) VALUES(?,?,?,?)",
                        [(f"{marker}-sess", "decision", f"{marker} {i}", f"{marker}{i}")
                         for i in range(n)])
        con.commit()
        cons.append(con)
        return con

    seed(None, "default", 50)
    for oid, name in (("acme", "Acme Corp"), ("globex", "Globex")):
        orgs.create_org(name, oid, f"{oid}.example")
        orgs.mint_token(f"admin@{oid}.example", oid, hostname="laptop")
        seed(oid, oid, 30)
    yield tmp_path
    for c in cons:
        c.close()


def test_discovers_every_plane(box):
    rels = [rel for _, rel in backup.db_files("data")]
    assert "control.db" in rels                          # the control plane `cp` never captured
    assert "analyzer.db" in rels and "telemetry.db" in rels
    assert os.path.join("orgs", "acme", "analyzer.db") in rels
    assert os.path.join("orgs", "globex", "analyzer.db") in rels
    assert not any(r.endswith((".migrate.lock", "-wal", "-shm")) for r in rels), \
        "lock files and WAL sidecars are not databases; the backup API folds -wal in by itself"


def test_round_trip_restores_control_plane_and_every_tenant(box, tmp_path):
    snap, scratch = str(tmp_path / "snap"), str(tmp_path / "scratch")
    manifest = backup.snapshot("data", snap)
    # `default` is listed too: its corpus is in the snapshot either way, and omitting it made the
    # summary disagree with the contents on an adopted deployment.
    assert manifest["orgs"] == ["default", "acme", "globex"]

    os.makedirs(scratch)
    keep = os.path.join(scratch, "extracted-cache.txt")
    open(keep, "w").close()
    restored = backup.restore(snap, scratch)

    ok, problems = backup.verify(scratch)
    assert ok, problems
    assert "control.db" in restored
    assert os.path.join("orgs", "acme", "analyzer.db") in restored
    assert os.path.exists(keep), "a restore is not a wipe — non-database files stay put"

    con = sqlite3.connect(os.path.join(scratch, "control.db"))
    assert sorted(r[0] for r in con.execute("SELECT id FROM orgs")) == ["acme", "globex"]
    assert con.execute("SELECT count(*) FROM user_tokens WHERE revoked_at IS NULL").fetchone()[0] == 2
    con.close()

    con = sqlite3.connect(os.path.join(scratch, "orgs", "acme", "analyzer.db"))
    assert con.execute("SELECT count(*) FROM hidden_context").fetchone()[0] == 30
    con.close()


def test_control_db_journal_mode_never_flips(box, tmp_path):
    """control.db is journal_mode=delete on purpose (analyzer/orgs.py) and tests/test_orgs_models.py
    fails if that changes. Backing it up must not be the thing that changes it."""
    snap = str(tmp_path / "snap")
    backup.snapshot("data", snap)
    for p in ("data/control.db", os.path.join(snap, "control.db")):
        con = sqlite3.connect(p)
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete", p
        con.close()
    assert not os.path.exists(os.path.join(snap, "control.db-wal"))


def test_uncheckpointed_commit_survives(box, tmp_path):
    """THE bug: `cp` of a WAL db copies the main file only, so a durable-but-uncheckpointed commit is
    lost. Reproduce the loss, then show the backup API keeps it."""
    src = os.path.join("data", "orgs", "acme", "analyzer.db")
    con = store.sqlite_db(src)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")       # long-lived db: history already in the main file
    con.execute("INSERT INTO hidden_context(session_id, type, claim, ref) VALUES(?,?,?,?)",
                ("acme-sess", "decision", "ONLY-IN-WAL", "wal-marker"))
    con.commit()

    torn = str(tmp_path / "torn.db")
    shutil.copyfile(src, torn)                           # exactly what the runbook used to do
    tc = sqlite3.connect(torn)
    assert tc.execute("SELECT count(*) FROM hidden_context WHERE claim='ONLY-IN-WAL'").fetchone()[0] == 0
    tc.close()

    snap = str(tmp_path / "snap")
    backup.snapshot("data", snap)
    sc = sqlite3.connect(os.path.join(snap, "orgs", "acme", "analyzer.db"))
    assert sc.execute("SELECT count(*) FROM hidden_context WHERE claim='ONLY-IN-WAL'").fetchone()[0] == 1
    sc.close()
    con.close()


def test_verify_catches_a_missing_tenant(box, tmp_path):
    snap = str(tmp_path / "snap")
    backup.snapshot("data", snap)
    os.remove(os.path.join(snap, "orgs", "globex", "analyzer.db"))
    ok, problems = backup.verify(snap)
    assert not ok and any("globex" in p for p in problems), problems


def test_verify_catches_a_torn_file_with_a_matching_checksum(box, tmp_path):
    """A checksum proves the bytes did not change in transit. It says nothing about whether those
    bytes are a valid database — which is exactly the `cp`-a-live-db failure. integrity_check does."""
    snap = str(tmp_path / "snap")
    backup.snapshot("data", snap)
    victim = os.path.join(snap, "orgs", "acme", "analyzer.db")
    with open(victim, "r+b") as f:
        f.truncate(os.path.getsize(victim) // 2)
    mpath = os.path.join(snap, backup.MANIFEST)
    with open(mpath) as f:
        m = json.load(f)
    for e in m["files"]:                                 # re-hash so ONLY integrity_check can object
        e["sha256"] = backup.sha256(os.path.join(snap, e["path"]))
    with open(mpath, "w") as f:
        json.dump(m, f)
    ok, problems = backup.verify(snap)
    assert not ok and any("acme" in p for p in problems), problems


def test_empty_data_dir_refuses(tmp_path):
    """"Backed up 0 databases" exiting 0 is how a broken backup survives for months."""
    with pytest.raises(SystemExit):
        backup.snapshot(str(tmp_path / "nothing"), str(tmp_path / "snap"))


def test_the_ingest_queue_is_captured_and_restored(box, tmp_path):
    """From the operator simulation: the snapshot dropped `pending/`, so a restore silently destroyed
    every session a client had already shipped and had accepted but that was not yet extracted. The
    client dedups on its own marker and never resends, so those sessions are gone for good — the exact
    failure this module exists to prevent, in the one directory it was not looking at."""
    os.makedirs(os.path.join("data", "orgs", "acme", "pending"), exist_ok=True)
    with open(os.path.join("data", "orgs", "acme", "pending", "q1.json"), "w") as f:
        f.write('{"session_id": "q1"}')
    os.makedirs(os.path.join("data", "pending"), exist_ok=True)
    with open(os.path.join("data", "pending", "q0.json"), "w") as f:
        f.write('{"session_id": "q0"}')

    snap, scratch = str(tmp_path / "snap"), str(tmp_path / "scratch")
    man = backup.snapshot("data", snap)
    assert man["queued_files"] == 2, man
    os.makedirs(scratch)
    backup.restore(snap, scratch)

    assert os.path.exists(os.path.join(scratch, "orgs", "acme", "pending", "q1.json"))
    assert os.path.exists(os.path.join(scratch, "pending", "q0.json"))
    ok, problems = backup.verify(scratch)
    assert ok, problems                                  # JSON files are checksummed, not integrity_checked


def test_restore_removes_stale_wal_sidecars(box, tmp_path):
    snap, scratch = str(tmp_path / "snap"), str(tmp_path / "scratch")
    backup.snapshot("data", snap)
    stale = os.path.join(scratch, "orgs", "acme", "analyzer.db-wal")
    os.makedirs(os.path.dirname(stale), exist_ok=True)
    with open(stale, "w") as f:
        f.write("OLD WAL")

    backup.restore(snap, scratch)

    assert not os.path.exists(stale), "an old WAL must not shadow or corrupt the restored database"


def test_cli_requires_a_real_subcommand():
    with pytest.raises(SystemExit) as exc:
        backup.main([])
    assert exc.value.code == 2


def test_restore_refuses_a_damaged_snapshot_before_touching_anything(box, tmp_path):
    """It used to validate file-by-file and SystemExit on the first bad entry, leaving data/ half old
    and half new — a restored control plane pointing at tenants that had not been restored. A restore
    that can stop in the middle is not a rollback."""
    snap, scratch = str(tmp_path / "snap"), str(tmp_path / "scratch")
    backup.snapshot("data", snap)
    os.makedirs(scratch)
    sentinel = os.path.join(scratch, "control.db")
    with open(sentinel, "w") as f:
        f.write("ORIGINAL")
    os.remove(os.path.join(snap, "orgs", "globex", "analyzer.db"))    # damage a LATE entry

    with pytest.raises(SystemExit):
        backup.restore(snap, scratch)
    with open(sentinel) as f:
        assert f.read() == "ORIGINAL", "control.db was overwritten before the damage was noticed"


def test_restore_refuses_on_a_checksum_mismatch(box, tmp_path):
    snap, scratch = str(tmp_path / "snap"), str(tmp_path / "scratch")
    backup.snapshot("data", snap)
    os.makedirs(scratch)
    with open(os.path.join(snap, "orgs", "acme", "analyzer.db"), "r+b") as f:
        f.seek(0)
        f.write(b"xxxx")
    with pytest.raises(SystemExit):
        backup.restore(snap, scratch)


def test_an_org_with_a_queue_but_no_corpus_is_still_captured(box, tmp_path):
    """Reachable for a new tenant between its first accepted ingest (which writes the queue file
    without opening a corpus) and the next ETL tick. Keying the walk on orgs/*/analyzer.db dropped
    that org from the snapshot entirely — the same silent omission this module exists to end."""
    os.makedirs(os.path.join("data", "orgs", "fresh", "pending"), exist_ok=True)
    with open(os.path.join("data", "orgs", "fresh", "pending", "s1.json"), "w") as f:
        f.write('{"session_id": "s1"}')
    snap = str(tmp_path / "snap")
    man = backup.snapshot("data", snap)
    assert "fresh" in man["orgs"], man["orgs"]
    assert os.path.exists(os.path.join(snap, "orgs", "fresh", "pending", "s1.json"))


def test_the_extraction_cache_is_captured(box, tmp_path):
    """Losing it loses no data, but a missing file reads as "never extracted" — so a
    rebuild-from-snapshot re-extracts the whole corpus through the LLM. A real bill, no warning."""
    os.makedirs(os.path.join("data", "orgs", "acme", "extracted"), exist_ok=True)
    with open(os.path.join("data", "orgs", "acme", "extracted", "acme-sess.json"), "w") as f:
        f.write("{}")
    snap = str(tmp_path / "snap")
    backup.snapshot("data", snap)
    assert os.path.exists(os.path.join(snap, "orgs", "acme", "extracted", "acme-sess.json"))


def test_archived_transcripts_are_captured(box, tmp_path):
    """Sessions kept under `tenant_config.archive_days` (analyzer/archive.py) live in `archive/`,
    which `_COPIED_DIRS` did not cover. A restore silently dropped every retained transcript."""
    tenant.set_config(store.org_db("acme"), archive_days=30)
    archive.put({"session_id": "acme-sess", "user": "admin@acme.example",
                "turns": ["USER: hi", "ASSISTANT: hello"]}, org_id="acme")
    snap = str(tmp_path / "snap")
    man = backup.snapshot("data", snap)
    assert os.path.exists(os.path.join(snap, "orgs", "acme", "archive", "acme-sess.json"))
    assert man["queued_files"] == 1


def _cli_backup(monkeypatch, capsys, keep, dest=None):
    argv = ["csa", "backup", "--keep", str(keep)]
    if dest is not None:
        argv += ["--dest", dest]
    monkeypatch.setattr(sys, "argv", argv)
    csa.main()
    return capsys.readouterr()


def test_cli_backup_keeps_only_the_newest_n_by_manifest_created(box, monkeypatch, capsys):
    for _ in range(3):
        out = _cli_backup(monkeypatch, capsys, keep=2)
    assert "snapshot:" in out.out and "verified:" in out.out
    remaining = sorted(d for d in os.listdir("data") if d.startswith("backup-"))
    assert len(remaining) == 2, remaining
    createds = []
    for d in remaining:
        with open(os.path.join("data", d, backup.MANIFEST)) as f:
            createds.append(json.load(f)["created"])
    assert createds == sorted(createds), "the two SURVIVING snapshots are the newest by manifest created"


def test_cli_backup_removes_a_snapshot_that_fails_verify(box, monkeypatch, capsys):
    monkeypatch.setattr(backup, "verify", lambda path: (False, [f"CORRUPT {path}"]))
    monkeypatch.setattr(sys, "argv", ["csa", "backup", "--keep", "7"])
    with pytest.raises(SystemExit) as exc:
        csa.main()
    assert exc.value.code == 1
    assert "CORRUPT" in capsys.readouterr().err
    assert [d for d in os.listdir("data") if d.startswith("backup-")] == [], \
        "a snapshot that fails verify must not survive"


def test_cli_backup_rejects_a_keep_below_one(box, monkeypatch):
    """`--keep 0` (or negative) would prune every snapshot on the box, including the one this very
    run just verified: a typo must not silently empty the backup directory."""
    for keep in ("0", "-1"):
        monkeypatch.setattr(sys, "argv", ["csa", "backup", "--keep", keep])
        with pytest.raises(SystemExit) as exc:
            csa.main()
        assert exc.value.code != 0
    assert [d for d in os.listdir("data") if d.startswith("backup-")] == []


def test_cli_backup_cleans_up_a_partial_snapshot_on_failure(box, monkeypatch):
    """A disk-full or permission error partway through `backup.snapshot` must not leave a manifest-less
    directory behind forever: it has no manifest, so neither pruning nor the next run's name-collision
    check will ever touch it again."""
    def boom(data_dir, dest):
        os.makedirs(dest)                                 # what snapshot() does before it starts copying
        raise OSError("disk full")
    monkeypatch.setattr(backup, "snapshot", boom)
    monkeypatch.setattr(sys, "argv", ["csa", "backup", "--keep", "7"])
    with pytest.raises(OSError):
        csa.main()
    assert [d for d in os.listdir("data") if d.startswith("backup-")] == [], \
        "a snapshot that never finished must not survive the failure"


def test_restore_onto_a_second_box_matches_the_source(box, tmp_path, monkeypatch):
    """The acceptance case: a restore on a SECOND box directory answers the same /api/facts, the same
    token still validates against the restored control.db, the pending/ queue count matches, and an
    archived transcript reads back, not only that the files exist."""
    tenant.set_config(store.org_db("acme"), archive_days=30)
    sess = {"session_id": "acme-arch", "user": "admin@acme.example",
            "turns": ["USER: hi", "ASSISTANT: hello"]}
    archive.put(sess, org_id="acme")

    os.makedirs(os.path.join("data", "orgs", "acme", "pending"), exist_ok=True)
    with open(os.path.join("data", "orgs", "acme", "pending", "q1.json"), "w") as f:
        f.write('{"session_id": "q1"}')

    tok = orgs.mint_token("owner@acme.example", "acme", hostname="laptop2")

    admin = {"email": "owner@acme.example", "role": "admin"}
    con = store.sqlite_db(store.org_db("acme"))
    facts_before = apiserver.facts(con, principal=admin)
    con.close()

    snap = str(tmp_path / "snap")
    backup.snapshot("data", snap)
    ok, problems = backup.verify(snap)
    assert ok, problems

    box2 = tmp_path / "box2"
    (box2 / "data").mkdir(parents=True)
    backup.restore(snap, str(box2 / "data"))

    monkeypatch.chdir(box2)
    con2 = store.sqlite_db(store.org_db("acme"))
    facts_after = apiserver.facts(con2, principal=admin)
    con2.close()
    assert facts_after == facts_before, "the restored corpus must answer /api/facts identically"

    assert orgs.verify(tok) == {"email": "owner@acme.example", "org_id": "acme"}, \
        "a token minted before the backup still validates against the restored control.db"

    assert queue.depth("acme")[0] == 1, "the pending/ count must match the source"

    got = archive.get("acme-arch", org_id="acme")
    assert got is not None and got["turns"] == sess["turns"], "the archived transcript must read back"
