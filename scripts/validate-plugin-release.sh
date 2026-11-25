#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

cache_dir=$(mktemp -d)
trap 'rm -rf "$cache_dir"' EXIT
export PYTHONPYCACHEPREFIX="$cache_dir"

python3 - <<'PY'
import json
import pathlib
import re
import sys

root = pathlib.Path.cwd()
marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
manifest = json.loads((root / "csa-plugin/.claude-plugin/plugin.json").read_text())
source = (root / marketplace["plugins"][0]["source"]).resolve()
expected = (root / "csa-plugin").resolve()

assert marketplace["name"] == "csa"
assert len(marketplace["plugins"]) == 1
assert marketplace["plugins"][0]["name"] == manifest["name"] == "csa"
assert source == expected, (source, expected)
assert manifest["defaultEnabled"] is False
assert manifest["repository"] == "https://github.com/nabooai/coding-session-analyzer"
code = (root / "csa-plugin/scripts/csa.py").read_text()
version = re.search(r'^VERSION = "([^"]+)"$', code, re.M).group(1)
assert manifest["version"] == version
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)

for path in expected.rglob("*"):
    if "__pycache__" in path.parts:
        continue
    assert not path.is_symlink(), "plugin release contains a symlink: %s" % path
    if path.is_file():
        assert path.suffix.lower() not in {".exe", ".dll", ".dylib", ".so", ".a", ".o", ".pyc"}, \
            "plugin release contains a binary: %s" % path
print("plugin-v%s" % version)
PY

cmp csa-plugin/scripts/redact.py server/analyzer/redact.py
python3 -m py_compile csa-plugin/scripts/csa.py csa-plugin/scripts/csa_commands.py csa-plugin/scripts/redact.py

if [[ "${PLUGIN_RUN_TESTS:-1}" == 1 ]]; then
  python3 -m pytest --noconftest -q server/tests/test_plugin_client.py
fi

if [[ "${PLUGIN_VALIDATE_CLAUDE:-0}" == 1 ]]; then
  claude plugin validate .
fi

echo "plugin release checks passed"
