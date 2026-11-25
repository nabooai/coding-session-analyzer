from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "security" / "audit_history.py"


class AuditHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "source"
        subprocess.run(["git", "init", "-q", "-b", "main", source], check=True)
        subprocess.run(["git", "-C", source, "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", source, "config", "user.name", "Fixture"], check=True)
        (source / "one.txt").write_text("ordinary\n", encoding="utf-8")
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(["git", "-C", source, "commit", "-qm", "first"], check=True)
        subprocess.run(["git", "-C", source, "checkout", "-qb", "other"], check=True)
        (source / "private.txt").write_text("PRIVATE_MARKER\n", encoding="utf-8")
        subprocess.run(["git", "-C", source, "add", "."], check=True)
        subprocess.run(["git", "-C", source, "commit", "-qm", "other"], check=True)
        self.mirror = self.root / "mirror.git"
        subprocess.run(["git", "clone", "-q", "--mirror", source, self.mirror], check=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._tool("gitleaks", """#!/bin/sh
if [ "$1" = "--version" ]; then echo 'gitleaks fixture'; exit 0; fi
for arg in "$@"; do case "$arg" in --report-path=*) report=${arg#*=};; esac; done
printf '[]' > "$report"
exit 0
""")
        self._tool("trufflehog", """#!/bin/sh
if [ "$1" = "--version" ]; then echo 'trufflehog fixture'; exit 0; fi
exit "${TRUFFLE_EXIT:-0}"
""")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _tool(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def test_scans_all_refs_and_keeps_dictionary_term_out_of_safe_report(self) -> None:
        dictionary = self.root / "dictionary.txt"
        dictionary.write_text("private_marker\n", encoding="utf-8")
        private = self.root / "private"
        safe = self.root / "safe.md"
        env = os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}"}
        result = subprocess.run(
            ["python3", SCRIPT, "--mirror", self.mirror, "--private-output", private,
             "--safe-report", safe, "--dictionary", dictionary], env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        report = safe.read_text(encoding="utf-8")
        self.assertIn("- Refs: 2", report)
        self.assertIn("- Commits: 2", report)
        self.assertIn("| Private dictionary matches | 1 |", report)
        self.assertNotIn("private_marker", report)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual((private / "dictionary-findings.json").stat().st_mode & 0o777, 0o600)

    def test_current_trufflehog_findings_exit_is_accepted(self) -> None:
        private = self.root / "private-findings"
        safe = self.root / "safe-findings.md"
        env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TRUFFLE_EXIT": "1",
        }
        result = subprocess.run(
            ["python3", SCRIPT, "--mirror", self.mirror, "--private-output", private,
             "--safe-report", safe], env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_scanner_fails_before_creating_output(self) -> None:
        env = os.environ | {"PATH": "/usr/bin:/bin"}
        private = self.root / "private"
        result = subprocess.run(
            ["python3", SCRIPT, "--mirror", self.mirror, "--private-output", private,
             "--safe-report", self.root / "safe.md"], env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("required tools missing", result.stderr)
        self.assertFalse(private.exists())


if __name__ == "__main__":
    unittest.main()
