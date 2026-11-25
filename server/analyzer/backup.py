"""Consistent backup and restore of every plane a CSA box holds: the control plane and one corpus per
tenant.

Every database is copied through SQLite's online backup API, never `cp`: a WAL database's recent
commits live in its `-wal` sidecar, so a plain copy is torn. The result is one self-contained file per
database, consistent under a concurrent writer. Consistency is per file, not across files: stop the
writers for a true point in time. control.db stays in `journal_mode=delete` on both sides (the backup
API runs no PRAGMA on the source); never route it through `db.engine_for`.

The snapshot mirrors data/ exactly (store.org_dir, store.telemetry_path, orgs.control_path):

    data/control.db                     control plane
    data/analyzer.db, telemetry.db      the default org's corpus and telemetry
    data/orgs/<org>/analyzer.db, ...    one corpus per hosted tenant

plus each org's _COPIED_DIRS, and a `manifest.json` with a sha256 per file.

CLI (run from `server/`):
    uv run python -m analyzer.backup snapshot <data_dir> <dest>   # consistent copy of every plane
    uv run python -m analyzer.backup verify   <dir>               # checksums + integrity_check + inventory
    uv run python -m analyzer.backup restore  <snap> <data_dir>   # snapshot -> a data/ dir

Imports only the stdlib, so the host's bare python3 can run it. Logs to stderr; the CLI prints to stdout.
"""
import argparse
import glob
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time

log = logging.getLogger("csa.backup")

MANIFEST = "manifest.json"
_SIDECARS = ("-wal", "-shm")                             # never copied: the backup API folds them in

# Plain-file directories copied per org: the ingest queue (accepted sessions the client will never
# resend), the extraction cache (without it a rebuild re-bills every extraction), retained transcripts.
_COPIED_DIRS = ("pending", "extracted", "archive")


def _connect_ro(path):
    """Open a source db read-only, without touching its journal mode or creating sidecars, and waiting
    out a writer rather than failing."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def db_files(data_dir="data"):
    """Every SQLite file this box holds, as (source_path, path_relative_to_data_dir), in restore order.
    From the filesystem, not the orgs table, so a corrupt control plane cannot hide a tenant."""
    data_dir = os.path.abspath(data_dir)
    out = []
    for rel in ("control.db", "analyzer.db", "telemetry.db"):
        p = os.path.join(data_dir, rel)
        if os.path.exists(p):
            out.append((p, rel))
    for org_db in sorted(glob.glob(os.path.join(data_dir, "orgs", "*", "analyzer.db"))):
        org_dir = os.path.dirname(org_db)
        for name in ("analyzer.db", "telemetry.db"):
            p = os.path.join(org_dir, name)
            if os.path.exists(p):
                out.append((p, os.path.relpath(p, data_dir)))
    return out


def org_roots(data_dir="data"):
    """Every per-org directory, the default org's `data/` included. From the directories, not the db
    files: a tenant's first queued session exists before its corpus does."""
    data_dir = os.path.abspath(data_dir)
    return [data_dir] + sorted(p.rstrip(os.sep)
                               for p in glob.glob(os.path.join(data_dir, "orgs", "*", ""))
                               if os.path.isdir(p))


def queue_files(data_dir="data"):
    """Every queued-ingest and extraction-cache file, as (source, relative) pairs. See _COPIED_DIRS."""
    data_dir = os.path.abspath(data_dir)
    out = []
    for root in org_roots(data_dir):
        for q in _COPIED_DIRS:
            for src in sorted(glob.glob(os.path.join(root, q, "**", "*"), recursive=True)):
                if os.path.isfile(src):
                    out.append((src, os.path.relpath(src, data_dir)))
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_db(src, dst):
    """One database -> one consistent file via the online backup API, in a single step (stepping in
    chunks can livelock under continuous ingest). Returns the destination size."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    for s in _SIDECARS:                                  # a stale sidecar next to dst would shadow it
        if os.path.exists(dst + s):
            os.remove(dst + s)
    if os.path.exists(dst):
        os.remove(dst)
    src_con = _connect_ro(src)
    try:
        dst_con = sqlite3.connect(dst)
        try:
            src_con.backup(dst_con, pages=0)
        finally:
            dst_con.close()
    finally:
        src_con.close()
    for s in _SIDECARS:                                  # a clean close leaves none
        if os.path.exists(dst + s):                      # pragma: no cover — defensive
            log.warning("backup: %s left a %s sidecar", dst, s)
    return os.path.getsize(dst)


def snapshot(data_dir="data", dest=None):
    """Back up every plane into `dest`, mirroring the data/ layout. Returns the manifest dict. An empty
    data dir raises: "backed up 0 databases" must never exit 0."""
    data_dir = os.path.abspath(data_dir)
    dest = os.path.abspath(dest or f"csa-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    files = db_files(data_dir)
    if not files:
        raise SystemExit(f"refusing to snapshot: no databases found under {data_dir!r} — wrong "
                         f"directory, or the box is not initialized. A backup of nothing is not a backup.")
    os.makedirs(dest, exist_ok=True)
    entries = []
    for src, rel in files:
        dst = os.path.join(dest, rel)
        size = _copy_db(src, dst)
        entries.append({"path": rel, "source": src, "bytes": size, "sha256": sha256(dst)})
        log.info("backup: %s -> %s (%d bytes)", src, dst, size)
    queued = queue_files(data_dir)
    for src, rel in queued:
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copyfile(src, dst)
        entries.append({"path": rel, "source": src, "bytes": os.path.getsize(dst),
                        "sha256": sha256(dst), "queued": True})
    if queued:
        log.info("backup: %d queued ingest file(s)", len(queued))
    # `orgs`: every tenant with data on this box, `default` included
    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "data_dir": data_dir,
                "orgs": ["default"] + [os.path.basename(r) for r in org_roots(data_dir)[1:]],
                "queued_files": len(queued),
                "files": entries}
    with open(os.path.join(dest, MANIFEST), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


def verify(path):
    """Check a snapshot (or a restored data dir) against its manifest: every checksum, and SQLite's
    integrity_check (a torn copy hashes fine). Returns (ok, [problem, ...])."""
    path = os.path.abspath(path)
    mpath = os.path.join(path, MANIFEST)
    if not os.path.exists(mpath):
        return (False, [f"no {MANIFEST} in {path}"])
    with open(mpath) as f:
        manifest = json.load(f)
    problems = []
    for e in manifest["files"]:
        p = os.path.join(path, e["path"])
        if not os.path.exists(p):
            problems.append(f"MISSING {e['path']}")
            continue
        got = sha256(p)
        if got != e["sha256"]:
            problems.append(f"CHECKSUM {e['path']}: {got[:12]} != {e['sha256'][:12]}")
            continue
        if e.get("queued"):                              # queued ingest files are JSON, not databases
            continue
        try:
            con = _connect_ro(p)
            try:
                row = con.execute("PRAGMA integrity_check").fetchone()
            finally:
                con.close()
            if not row or row[0] != "ok":
                problems.append(f"CORRUPT {e['path']}: {row and row[0]}")
        except sqlite3.Error as exc:
            problems.append(f"UNREADABLE {e['path']}: {exc}")
    return (not problems, problems)


def restore(snap, data_dir):
    """Snapshot -> a data/ directory; returns the restored relative paths. Copies every manifest entry
    and leaves anything else in `data_dir` alone. Stale `-wal`/`-shm` beside a restored file belong to
    the old database and are removed. The whole snapshot is checked before a byte is copied: a restore
    that stops halfway is not a rollback."""
    snap = os.path.abspath(snap)
    data_dir = os.path.abspath(data_dir)
    mpath = os.path.join(snap, MANIFEST)
    if not os.path.exists(mpath):
        raise SystemExit(f"refusing to restore: no {MANIFEST} in {snap!r}")
    with open(mpath) as f:
        manifest = json.load(f)
    problems = []
    for e in manifest["files"]:
        src = os.path.join(snap, e["path"])
        if not os.path.exists(src):
            problems.append(f"MISSING {e['path']}")
        elif sha256(src) != e["sha256"]:
            problems.append(f"CHECKSUM {e['path']}")
    if problems:
        raise SystemExit("refusing to restore: this snapshot is incomplete or damaged, and a partial "
                         "restore is worse than none.\n  " + "\n  ".join(problems))
    done = []
    for e in manifest["files"]:
        src, dst = os.path.join(snap, e["path"]), os.path.join(data_dir, e["path"])
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        for s in _SIDECARS:
            if os.path.exists(dst + s):
                os.remove(dst + s)
        shutil.copyfile(src, dst)
        done.append(e["path"])
        log.info("restore: %s -> %s", src, dst)
    shutil.copyfile(mpath, os.path.join(data_dir, MANIFEST))   # so `verify <data_dir>` works after a restore
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(description="Back up and restore all CSA data planes")
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="create a consistent backup")
    snap.add_argument("data_dir", nargs="?", default="data")
    snap.add_argument("dest", nargs="?")
    check = commands.add_parser("verify", help="verify checksums and SQLite integrity")
    check.add_argument("path")
    load = commands.add_parser("restore", help="restore a verified snapshot")
    load.add_argument("snapshot")
    load.add_argument("data_dir")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    if args.command == "snapshot":
        manifest = snapshot(args.data_dir, args.dest)
        print(json.dumps({"ok": True, "orgs": manifest["orgs"],
                          "files": len(manifest["files"])}, indent=2))
        return 0
    if args.command == "verify":
        ok, problems = verify(args.path)
        print(json.dumps({"ok": ok, "problems": problems}, indent=2))
        return 0 if ok else 1
    restored = restore(args.snapshot, args.data_dir)
    print(json.dumps({"ok": True, "restored": restored}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
