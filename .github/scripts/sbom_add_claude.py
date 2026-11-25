"""Add the Claude Code binary to a syft SPDX SBOM when syft did not catalog it.

The image installs it as a bare native binary (/usr/local/bin/claude) with no package metadata beside it, so
no cataloger finds it. The version and per-arch sha256 come from server/Dockerfile, so the entry cannot
drift from what the image installs.

    sbom_add_claude.py <sbom.spdx.json> <server/Dockerfile> <x64|arm64>
"""
import json, re, sys

sbom_path, dockerfile, arch = sys.argv[1:4]
name = f"@anthropic-ai/claude-code-linux-{arch}"
doc = json.load(open(sbom_path))
if any(p["name"] == name for p in doc["packages"]):
    print(f"syft cataloged {name}; nothing to add")
    sys.exit(0)
text = open(dockerfile).read()
version = re.search(r"com\.csa\.claude_code=([0-9][0-9.]*)", text).group(1)
sha = re.search(rf"arch={arch};\s+sum=([0-9a-f]{{64}})", text).group(1)
spdx_id = f"SPDXRef-Package-npm-claude-code-linux-{arch}"
doc["packages"].append({
    "SPDXID": spdx_id, "name": name, "versionInfo": version,
    "supplier": "Organization: Anthropic",
    "downloadLocation": f"https://registry.npmjs.org/{name}/-/claude-code-linux-{arch}-{version}.tgz",
    "filesAnalyzed": False,
    "checksums": [{"algorithm": "SHA256", "checksumValue": sha}],
    "licenseConcluded": "NOASSERTION", "licenseDeclared": "NOASSERTION",
    "licenseComments": "npm license field: SEE LICENSE IN LICENSE.md (Anthropic's commercial terms, not an open source licence)",
    "primaryPackagePurpose": "APPLICATION",
    "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                      "referenceLocator": f"pkg:npm/%40anthropic-ai/claude-code-linux-{arch}@{version}"}],
    "comment": "installed as /usr/local/bin/claude by server/Dockerfile; entry added by .github/scripts/sbom_add_claude.py",
})
root = next(r["relatedSpdxElement"] for r in doc["relationships"]
            if r["spdxElementId"] == "SPDXRef-DOCUMENT" and r["relationshipType"] == "DESCRIBES")
doc["relationships"].append({"spdxElementId": root, "relationshipType": "CONTAINS", "relatedSpdxElement": spdx_id})
with open(sbom_path, "w") as f:
    json.dump(doc, f, indent=1)
print(f"added {name} {version} sha256={sha}")
