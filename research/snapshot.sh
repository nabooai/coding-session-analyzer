#!/bin/sh
# Freeze an org DB (+ named extraction caches) into research/corpus/<date>/, read-only.
#
#   sh research/snapshot.sh [/path/to/analyzer.db] [--new]
#
# Idempotent: re-running on an intact snapshot reports "already frozen" and touches NOTHING (a
# backup of a LIVE db is not guaranteed byte-stable, so idempotency is manifest-gated, not
# re-backup-and-compare). --new: source changed on the same day -> a NEW sibling dir (<date>-2); an
# existing dir is never written into, MANIFEST or not. SNAP_DATE=YYYY-MM-DD overrides the dir name
# (tests). Uses SQLite's backup API — never `cp` a live WAL db. Note: even a mode=ro open of a live
# WAL source touches its -shm file (SQLite's shared-memory index) — inherent to WAL reads; the db
# itself is never written. "Transcripts": the server keeps none (facts ship, transcripts stay on
# client machines); what freezes beside the db is the extraction caches for sessions.txt entries.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/../server/data/analyzer.db"
NEW=0
for a in "$@"; do
  case "$a" in
    --new) NEW=1 ;;
    *) SRC="$a" ;;
  esac
done
[ -f "$SRC" ] || {
  echo "source db not found: $SRC" >&2
  echo "(server/data/ is untracked — on a fresh clone or worktree, pass your corpus path:" >&2
  echo "  sh research/snapshot.sh /path/to/analyzer.db)" >&2
  exit 2
}
DATE="${SNAP_DATE:-$(date +%Y-%m-%d)}"
DEST="$HERE/corpus/$DATE"
if [ "$NEW" = 1 ]; then
  n=2
  while [ -e "$DEST" ]; do DEST="$HERE/corpus/$DATE-$n"; n=$((n+1)); done
fi
if [ -e "$DEST" ] && [ ! -f "$DEST/MANIFEST" ]; then
  echo "ERROR: $DEST exists but has no MANIFEST — refusing to write into an existing directory." >&2
  echo "(a half-copied or foreign dir under corpus/ is not a freeze target; remove it or use --new)" >&2
  exit 1
fi
if [ -f "$DEST/MANIFEST" ]; then
  if python3 - "$DEST" <<'EOF'
import hashlib, os, sys
d = sys.argv[1]
for line in open(os.path.join(d, "MANIFEST")):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    sha, rel = line.split(None, 1)
    p = os.path.join(d, rel)
    if not (os.path.exists(p) and hashlib.sha256(open(p, "rb").read()).hexdigest() == sha):
        sys.exit(1)
EOF
  then
    echo "already frozen: $DEST (verified byte-identical, nothing touched)"
    exit 0
  else
    echo "ERROR: $DEST exists but fails hash verification — a frozen snapshot was modified." >&2
    echo "Never edit a snapshot. To freeze a changed source today: sh research/snapshot.sh $SRC --new" >&2
    exit 1
  fi
fi
mkdir -p "$DEST"
python3 - "$SRC" "$DEST" "$HERE" <<'EOF'
import glob, hashlib, os, shutil, sqlite3, subprocess, sys, time
src, dest, here = sys.argv[1:4]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(os.path.join(dest, "analyzer.db"))
s.backup(d)                                   # SQLite backup API: consistent against a live WAL writer
d.execute("PRAGMA journal_mode=DELETE")       # a WAL header on a chmod-444 file breaks even read-only
d.close(); s.close()                          # opens (SQLite must create the -shm); frozen = non-WAL
files, absent = ["analyzer.db"], []
sess = os.path.join(here, "sessions.txt")
cache_dir = os.path.join(os.path.dirname(os.path.abspath(src)), "extracted")
prefixes = [ln.strip() for ln in open(sess)] if os.path.exists(sess) else []
prefixes = [p for p in prefixes if p and not p.startswith("#")]
os.makedirs(os.path.join(dest, "extracted"), exist_ok=True)
for p in prefixes:
    hits = glob.glob(os.path.join(cache_dir, p + "*.json")) if os.path.isdir(cache_dir) else []
    for hp in hits:
        rel = os.path.join("extracted", os.path.basename(hp))
        shutil.copyfile(hp, os.path.join(dest, rel))
        files.append(rel)
    if not hits:
        absent.append(p)
try:
    sha = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
except Exception:
    sha = "unknown"
with open(os.path.join(dest, "MANIFEST"), "w") as f:
    f.write(f"# frozen {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"from {os.path.abspath(src)} (git {sha})\n")
    f.write("# extraction caches = the S1 output record for research/sessions.txt entries; the server\n"
            "# keeps no raw transcripts (they stay on the client machines that produced them).\n")
    for p in absent:
        f.write(f"# absent: no extraction cache for session prefix {p}\n")
    for rel in files:
        h = hashlib.sha256(open(os.path.join(dest, rel), "rb").read()).hexdigest()
        f.write(f"{h}  {rel}\n")
note = (f"; {len(absent)} named session(s) had no extraction cache — informational, their FACTS "
        "are in the db" if absent else "")
print(f"frozen: {dest} ({len(files)} file(s){note})")
EOF
chmod -R a-w "$DEST"
