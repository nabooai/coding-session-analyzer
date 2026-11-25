# Full-ref sensitive-data audit

`audit_history.py` provides the reproducible Git-history portion of the public-release security audit. It fails when either scanner is unavailable, scans every ref in a fresh mirror, keeps detailed output private, and emits only category counts publicly. Scanner matches are unresolved until a human reviews them.

## Run

Use a new directory on an encrypted, access-controlled volume. Do not run from an ordinary clone.

```sh
git clone --mirror <repository-url> repository.git
python3 security/audit_history.py \
  --mirror repository.git \
  --dictionary /secure/path/private-dictionary.txt \
  --private-output /secure/path/audit-$(date -u +%Y%m%dT%H%M%SZ) \
  --safe-report /tmp/history-audit-summary.md
```

Install and record current versions of `gitleaks` and `trufflehog` before running. The driver records their reported versions, the origin URL, ref count, commit count, and a hash of the exact commit set in private `inventory.json`. It invokes Gitleaks with `--log-opts=--all` and TruffleHog against the full mirror. Exit status 2 means potential findings exist or an argument/tool prerequisite failed. Detailed scanner reports, ref names, commit/path dictionary matches, and logs are mode 0600 beneath a mode 0700 directory.

The private dictionary is newline-delimited. Blank lines and lines beginning with `#` are ignored. Its values are read but never copied into output. Keep the dictionary and the entire private output outside Git. Review every high/critical result and sample lower severities. Record a reviewer and narrow rationale for each suppression. This repository intentionally has no blanket scanner allowlist.

## Artifact and manual audit

The Git driver does not prove artifacts are safe. Copy [`report-template.md`](report-template.md) into the private audit record and complete these inventories separately:

1. GitHub release assets with immutable download digest.
2. Every current and historical image tag and manifest digest, then every filesystem layer for runtime data, environment files, credentials, logs, and corpora.
3. Historical archives and databases after safe local extraction.
4. Screenshots and other images by manual visual review.
5. Commit messages, ref names, `.claude`, `.github`, research, and planning material.

Never paste a matched value into a public report, issue, commit, or CI log. If a real credential or sensitive client value is found, stop publication and privately escalate rotation and the human-approved remediation decision. This tool does not rewrite history, rotate credentials, disposition findings, or grant release approval.

After remediation, create a fresh mirror and rerun the complete audit. After publication, repeat it from an anonymous mirror and public image pulls.

## Tests

```sh
python3 -m unittest discover -s security/tests -v
```
