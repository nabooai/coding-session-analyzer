#!/usr/bin/env python3
"""Run a fail-closed, full-ref Git history audit without publishing findings."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def git(mirror: Path, *args: str) -> str:
    return run(["git", f"--git-dir={mirror}", *args]).stdout


def version(executable: str) -> str:
    result = run([executable, "--version"], check=False)
    return (result.stdout or result.stderr).strip().splitlines()[0]


def secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(stat.S_IRWXU)


def write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def json_count(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return len(value) if isinstance(value, list) else sum(1 for _ in path.open(encoding="utf-8"))


def dictionary_scan(mirror: Path, dictionary: Path, private_dir: Path) -> int:
    terms = [line.strip() for line in dictionary.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    findings: list[dict[str, str]] = []
    commits = git(mirror, "rev-list", "--all").splitlines()
    refs = git(mirror, "for-each-ref", "--format=%(refname)").splitlines()
    commit_messages = {commit: git(mirror, "show", "-s", "--format=%B", commit) for commit in commits}
    for commit in commits:
        for index, term in enumerate(terms, 1):
            result = run(
                ["git", f"--git-dir={mirror}", "grep", "-I", "-i", "-l", "-F", "-e", term, commit],
                check=False,
            )
            for match in result.stdout.splitlines():
                path = match.split(":", 1)[-1]
                safe_id = hashlib.sha256(f"dictionary:{commit}:{path}:{index}".encode()).hexdigest()[:16]
                findings.append({"id": safe_id, "commit": commit, "path": path, "surface": "blob",
                                 "category": "private-dictionary"})
            if term.casefold() in commit_messages.get(commit, "").casefold():
                safe_id = hashlib.sha256(f"dictionary:{commit}:message:{index}".encode()).hexdigest()[:16]
                findings.append({"id": safe_id, "commit": commit, "surface": "commit-message",
                                 "category": "private-dictionary"})
    for ref in refs:
        for index, term in enumerate(terms, 1):
            if term.casefold() in ref.casefold():
                safe_id = hashlib.sha256(f"dictionary:{ref}:{index}".encode()).hexdigest()[:16]
                findings.append({"id": safe_id, "ref": ref, "surface": "ref-name",
                                 "category": "private-dictionary"})
    write_private(private_dir / "dictionary-findings.json", json.dumps(findings, indent=2) + "\n")
    return len(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path, required=True, help="fresh bare mirror created with git clone --mirror")
    parser.add_argument("--private-output", type=Path, required=True, help="new access-controlled findings directory")
    parser.add_argument("--safe-report", type=Path, required=True, help="category-only Markdown output")
    parser.add_argument("--dictionary", type=Path, help="private newline-delimited terms; never copied")
    args = parser.parse_args(argv)

    mirror = args.mirror.resolve()
    if not (mirror / "HEAD").is_file() or not (mirror / "objects").is_dir():
        parser.error("--mirror must be a bare/full mirror")
    missing = [tool for tool in ("git", "gitleaks", "trufflehog") if shutil.which(tool) is None]
    if missing:
        parser.error("required tools missing: " + ", ".join(missing))
    if args.safe_report.exists():
        parser.error("--safe-report already exists")
    secure_directory(args.private_output)

    refs = git(mirror, "for-each-ref", "--format=%(refname)·%(objectname)")
    commits = sorted(set(git(mirror, "rev-list", "--all").splitlines()))
    inventory = {
        "timestamp_utc": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "origin": git(mirror, "config", "--get", "remote.origin.url").strip(),
        "ref_count": len(refs.splitlines()),
        "commit_count": len(commits),
        "commit_set_sha256": hashlib.sha256(("\n".join(commits) + "\n").encode()).hexdigest(),
        "versions": {tool: version(tool) for tool in ("git", "gitleaks", "trufflehog")},
    }
    write_private(args.private_output / "refs.txt", refs)
    write_private(args.private_output / "inventory.json", json.dumps(inventory, indent=2) + "\n")

    gitleaks_report = args.private_output / "gitleaks.json"
    gitleaks = run(["gitleaks", "git", "--log-opts=--all", "--redact=100", "--report-format=json",
                    f"--report-path={gitleaks_report}", str(mirror)], check=False)
    write_private(args.private_output / "gitleaks.log", gitleaks.stdout + gitleaks.stderr)
    if gitleaks.returncode not in (0, 1):
        raise SystemExit(f"gitleaks failed with status {gitleaks.returncode}")
    if gitleaks_report.exists():
        gitleaks_report.chmod(stat.S_IRUSR | stat.S_IWUSR)

    truffle_report = args.private_output / "trufflehog.jsonl"
    with truffle_report.open("w", encoding="utf-8") as output:
        truffle = subprocess.run(
            ["trufflehog", "git", f"file://{mirror}", "--json", "--no-update", "--fail"],
            text=True, stdout=output, stderr=subprocess.PIPE,
        )
    truffle_report.chmod(stat.S_IRUSR | stat.S_IWUSR)
    write_private(args.private_output / "trufflehog.log", truffle.stderr)
    # TruffleHog historically used 183 for --fail findings; current releases
    # use 1. Both mean the scan completed and emitted potential findings.
    if truffle.returncode not in (0, 1, 183):
        raise SystemExit(f"trufflehog failed with status {truffle.returncode}")

    counts = {
        "Gitleaks potential secrets": json_count(gitleaks_report),
        "TruffleHog potential secrets": sum(1 for line in truffle_report.read_text(encoding="utf-8").splitlines() if line.strip()),
        "Private dictionary matches": dictionary_scan(mirror, args.dictionary, args.private_output) if args.dictionary else 0,
    }
    report = [
        "# Full-ref sensitive-data audit: category-only report", "",
        f"Generated: {inventory['timestamp_utc']}", "",
        "This report is automated evidence only. It is not a disposition record or human approval.", "",
        "## Coverage", "",
        f"- Refs: {inventory['ref_count']}", f"- Commits: {inventory['commit_count']}",
        f"- Commit-set SHA-256: `{inventory['commit_set_sha256']}`", "- Git history scanners: Gitleaks and TruffleHog",
        f"- Private dictionary supplied: {'yes' if args.dictionary else 'no'}", "",
        "## Unreviewed findings", "",
        "| Category | Count |", "|---|---:|",
        *[f"| {category} | {count} |" for category, count in counts.items()], "",
        "Detailed findings remain in the access-controlled private output directory.",
        "Release assets, container layers, screenshots, archives, databases, and manual dispositions require separate human review.", "",
    ]
    args.safe_report.parent.mkdir(parents=True, exist_ok=True)
    args.safe_report.write_text("\n".join(report), encoding="utf-8")
    return 2 if any(counts.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
