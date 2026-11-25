# Task 06: Move to company organization and finish GitHub/package metadata

**Status:** open · **Owner:** company org admin + release lead · **Priority:** P0

## Objective

Move CSA to durable company ownership and make repository, package and security metadata correct without
breaking installation, signing, releases or public links.

## Does the company move help?

Yes for ownership, team administration, security response, package permissions and long-term trust. It does
not sanitize commits, rotate secrets, create a license, make GHCR public, or simplify setup by itself.
Transfer includes history, so task 01 remains a hard prerequisite.

## Deliverables

### Organization and URLs

- Transfer to the selected company GitHub organization after the history remediation path is approved.
- Update all `nabooai/coding-session-analyzer` and `ghcr.io/nabooai` references (42 and 28 occurrences
  respectively at audit time), including:
  - README/INSTALL/plugin/client docs;
  - marketplace and plugin manifests;
  - GHCR image names in compose/Helm/release workflows;
  - Homebrew formula/tap;
  - CI badges, release URLs, source links and installer raw URLs;
  - cosign `IDENTITY_RE` and other signing provenance;
  - tests that assert old URLs.
- Verify transferred redirects, then intentionally use the final canonical URL everywhere public.

### Legal and repository metadata

- Add the Apache-2.0 `LICENSE` and copyright/NOTICE information approved by the company; verify GitHub
  detects it. The license currently exists only on `origin/apache-2-license`, not `origin/master`.
- Set description: “Turn your team's AI coding sessions into shared, usable knowledge.”
- Set homepage, topics (`claude-code`, `agent-memory`, `knowledge-graph`, `mcp`, `developer-tools`,
  `self-hosted`) and social preview using the clean UI.
- Keep Issues enabled; decide Discussions intentionally. Add a small bug template and PR template.
- Update `CONTRIBUTING.md` to target `origin/master`, not `onprem-main`.
- Keep `SECURITY.md` accurate and enable private vulnerability reporting.

### Packages and security settings

- Make `csa-server-slim` public under the company GHCR namespace; leave subscription/proprietary images
  private where required. Confirm anonymous pull.
- Configure org/repo Actions secrets, variables and GHCR read/write permissions after transfer.
- Enable secret scanning, push protection, Dependabot security updates and CodeQL where supported.
- Require CI on master and restrict direct pushes/force pushes.
- Re-cut versioned server/plugin releases from the final org and verify SBOM/signature identity.

## Acceptance

- [ ] GitHub repository displays company owner, Apache-2.0, description, homepage and topics.
- [ ] No public documentation or release config uses the old personal org as its canonical URL.
- [ ] Anonymous `git clone`, GHCR pull and marketplace add work without `gh auth`.
- [ ] Release workflow produces signed artifacts whose identity names the company repo/workflow/tag.
- [ ] Secret scanning/push protection/private vulnerability reporting are enabled; CI is required on master.
- [ ] License, SECURITY and CONTRIBUTING links resolve and instructions match master development.
- [ ] Repository transfer caused no loss of issues, releases, tags, package versions or action permissions.

## Merge requirement

Merge URL/workflow/metadata changes to `origin/master` before cutting releases. Perform visibility changes
only after tasks 01–05 and the pre-public checklist pass.
