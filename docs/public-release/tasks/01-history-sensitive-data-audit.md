# Task 01: Prove Git history and release artifacts contain no sensitive information

**Status:** open · **Owner:** security/release lead · **Priority:** P0, blocks all public visibility

## Objective

Produce evidence that every public Git ref and release artifact is free of client names, credentials,
private infrastructure, personal data and real session content. A deleted file in HEAD is not proof.

## Scope

Audit:

- every branch, tag and commit reachable from all refs, not only `origin/master`;
- tracked source, docs, screenshots, JSON fixtures, notebooks, archives, SQLite/DB files and binaries;
- GitHub release assets and both current and historical container images where accessible;
- client/company names, codenames, domains, employee emails, internal hosts, GCP/project identifiers,
  session IDs and real transcript excerpts;
- API keys, OAuth/subscription tokens, cookies, private keys, TLS material, connection strings, JWTs,
  webhooks and bearer tokens;
- `.claude`, `.github`, research and historical planning material for internal-only information.

The release lead must privately provide the client/codename dictionary. **Do not commit that dictionary or
a findings report containing the sensitive strings.**

## Required procedure

### 1. Preserve and inventory

- Work from a fresh full mirror containing all refs and `packed-refs`.
- Record the audited repository URL, mirror commit set, scanner versions and timestamp.
- List every release asset and image tag/digest included in the audit.
- Keep all findings in an access-controlled local report with commit SHA, path, artifact and a safe finding
  ID. Public reports contain counts/categories only.

### 2. Automated history scanning

Run at least two independent secret scanners over full history, for example Gitleaks and TruffleHog, with
`--all`-equivalent coverage. Supplement them with repository-specific searches for the private client
dictionary and known internal domain patterns.

Also scan:

- commit messages, branch names and tag names;
- text extracted from historical archives and databases;
- screenshots manually for client/person/project data;
- image filesystem layers for `/app/data`, `.env`, credentials, tokens, logs and bundled corpus.

### 3. Manual review and false-positive disposition

- Review every high/critical match and a representative sample of medium/low matches.
- Label each as true secret, sensitive client data, intentional public example, test fixture, or false positive.
- Fake credentials in tests must be obviously synthetic and cannot be copies of real credentials.
- Document the suppression rule and reviewer for each accepted false positive; never use a broad ignore file
  to make the report green.

### 4. Remediate before publication

If any real secret or sensitive client information is found:

- **Stop the visibility change.**
- Rotate/revoke exposed credentials immediately; history deletion does not revoke them.
- Choose explicit human-approved remediation: targeted history rewrite with `git-filter-repo`, or a clean
  public repository containing a sanitized source snapshot.
- Coordinate all branch/tag/release deletions and force pushes. Ask GitHub Support about cached pull-request
  refs or other server-side copies when applicable.
- Preserve an internal incident record; do not publish the leaked values in an issue or PR.
- Re-run the entire audit on the resulting refs and artifacts.

Do not rewrite history merely to remove harmless development noise.

## Acceptance evidence

- [ ] Scanner commands, versions, ref coverage and artifact digests are recorded.
- [ ] Both scanners cover all Git refs; client-name/domain dictionary checks are complete.
- [ ] Screenshots, databases, archives, release assets and image layers receive non-text/manual checks.
- [ ] Every finding has a documented disposition; zero unresolved sensitive matches remain.
- [ ] Any exposed credential is rotated and any required history remediation is completed and re-audited.
- [ ] `.github_token`, `tls.key`, `.env`, runtime `data/`, SQLite corpora, logs and local binaries are absent
      from all public refs and artifacts.
- [ ] The security lead signs off explicitly: “approved for public visibility from this exact ref set.”
- [ ] After publication, an anonymous mirror and public image are scanned again with the same gates.

## Merge requirement

Merge the sanitized audit tooling, safe suppression rules and category-only report to `origin/master`.
Keep sensitive dictionaries and detailed findings outside the public repository.
