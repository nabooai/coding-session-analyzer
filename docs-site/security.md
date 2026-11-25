# Security

Report vulnerabilities privately using GitHub private vulnerability reporting once the repository is public.
Until then, use the private contact channel listed in the repository [`SECURITY.md`](https://github.com/nabooai/coding-session-analyzer/blob/master/SECURITY.md).

Do not open a public issue containing a credential, exploit details, private hostname, client name, transcript,
or corpus sample.

## Release security gates

A public release requires:

- Full-ref Gitleaks and TruffleHog scans.
- A private client/codename dictionary scan.
- Human disposition of every finding.
- Manual review of screenshots, archives, databases, release assets, and image layers.
- Rotation of any exposed credential before history remediation.
- Anonymous rescanning of the public Git mirror and container image.
- Signed artifacts, checksums, SBOMs, branch protection, and required CI.

The repository includes reproducible audit tooling under `security/`. Detailed findings and private dictionaries
must stay outside Git; public reports contain category counts only.
