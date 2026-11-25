# Public release: coordinator and merge order

**Status:** open · **Release blocker owner:** release lead · **Target:** `origin/master`

## Objective

Publish CSA as an open-source project only after the history is proven safe and a stranger can install,
connect a model, and reach useful search without reading internal documentation.

## Confirmed starting findings

Audit of `origin/master`:

- GitHub repository and `ghcr.io/nabooai/csa-server-slim` are private.
- `LICENSE` is missing from `origin/master`, although the README shows an Apache-2.0 badge.
- `plugin-v0.2.0` is referenced by installation docs but does not exist as a remote tag.
- The server quickstart uses six compose services (`migrate`, `api`, `mcp`, `etl`, `backup`, `proxy`) and
  manual `.env` and workspace JSON creation.
- The UI exposes Memory, Sessions, Entities, Team, Connect and four Admin pages; it is cream/dark-capable,
  not the requested white search-only product.
- The README is 251 lines and presents too many alternatives.
- 42 references to `nabooai/coding-session-analyzer` and 28 to `ghcr.io/nabooai` need review if the
  project moves to the company organization.
- No full-history/client-name audit has been completed. **Do not infer that history is clean.**

## Task map

| Order | Task | Can run in parallel? |
|---|---|---|
| 1 | [01 — Full-history sensitive-data audit](01-history-sensitive-data-audit.md) | Yes; starts immediately and blocks visibility |
| 2 | [06 — Company organization and GitHub metadata](06-company-org-and-metadata.md) | After history remediation path is chosen; before final URLs |
| 3 | [02 — Guided installation and model setup](02-guided-installation-and-model-setup.md) | Yes |
| 4 | [03 — Plugin installation and release](03-plugin-installation.md) | Yes, but public-tag verification follows visibility |
| 5 | [04 — Minimal white search UI](04-minimal-white-search-ui.md) | Yes |
| 6 | [05 — README, product Why, and architecture flow](05-readme-and-product-flow.md) | After org URL and actual install behavior are known |
| 7 | [07 — Merge master and publish](07-merge-master-and-publish.md) | Last |

## Company organization decision

Moving to the company's GitHub organization is recommended for durable ownership, team permissions,
releases, and security administration. It does **not** sanitize history or make GHCR public. Transfer the
repository only through a path that also covers:

- full Git history and every branch/tag;
- GHCR package namespace and visibility;
- marketplace/plugin manifests and documentation links;
- GitHub Actions secrets/variables and package permissions;
- release signing/cosign workflow identity;
- Homebrew formula/tap and download URLs;
- repository description, topics, homepage, license detection and security settings.

Do not rely on GitHub's post-transfer redirects as the permanent public contract.

## Required merge sequence

1. Merge these task definitions to `origin/master` in one documentation-only PR.
2. Complete history audit and any approved remediation before changing visibility.
3. Merge implementation PRs to `origin/master`; `onprem-main` is not a second public release trunk.
4. Update `CONTRIBUTING.md` so contributors receive the same master-based instruction.
5. Cut the server image and plugin releases from the exact merged commit.
6. Run the clean-room checklist in task 07 on that commit.
7. Change repository/package visibility and run the anonymous installation test before announcement.

## Release definition of done

- History task produces a clean, reproducible report covering all refs, client names and release artifacts,
  or every finding is remediated and re-audited.
- Apache-2.0 is present and detected by GitHub.
- Server and plugin installation work anonymously from public artifacts.
- Claude and OpenAI setup paths shown in README work; unsupported login methods are never advertised.
- The initial app is white and search-first, with no competing product screens/buttons.
- README opens with the product's Why, contains the concise architecture flow, and includes a prompt a
  user can give Claude to install CSA.
- All required CI and clean-room E2E checks pass on the merged release commit.
- Repository and GHCR are intentionally public, with security features enabled.

## Explicit non-goals for the release

- New temporal graph, ETL, Insights, Dreaming, or retrieval features.
- A Hindsight migration or independent-backend rewrite.
- More harnesses, deployment platforms, admin pages, or UI themes.
- Rewriting Git history without explicit human approval.
