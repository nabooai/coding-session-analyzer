# Task 07: Merge everything to master and publish

**Status:** open · **Owner:** release lead · **Priority:** final gate

## Objective

Ship one reviewed master-based release and make it public without carrying unmerged fixes, stale artifacts,
private dependencies or unverified documentation.

## Required merge checklist

- [ ] The task-definition documentation PR is merged to `origin/master`.
- [ ] Every release task PR targets and merges to `origin/master`; there is no separate `onprem-main` path.
- [ ] Master branch protection requires server, client, UI, plugin and E2E CI plus review.
- [ ] `CONTRIBUTING.md` and maintainer docs use the same master flow.
- [ ] The exact release commit contains guided install/model setup, plugin release, white UI, README/INSTALL,
      org metadata and the approved history-audit outcome.
- [ ] No release-blocking PR remains open; local uncommitted binaries, `.github_token`, TLS keys, `data/`,
      `.env` and unrelated work are not staged or shipped.

## Build and verify before visibility

1. Record `RELEASE_COMMIT` on `origin/master` and run all CI from a clean checkout.
2. Build both server image architectures; inspect image layers for secrets/corpus and run image smoke tests.
3. Cut the server release and plugin tag from `RELEASE_COMMIT`, never from a local worktree.
4. Verify SBOM, release assets, checksums and signing identities.
5. Run clean macOS and Ubuntu installation tests using only public-facing instructions in a staging/anonymous
   artifact environment.
6. Give the README prompt to Claude Code on a clean machine; require a successful install or fix the docs.
7. Exercise API-key and supported login paths, plugin consent, project exclusion, first search and MCP.
8. Obtain security sign-off on task 01 and product sign-off on UI/README.

## Publication order

1. Publish the required GHCR package(s) with the intended visibility.
2. Make the GitHub repository public.
3. Immediately test anonymous clone, raw INSTALL URL, GHCR pull, marketplace install and release downloads.
4. Re-run secret/client-name checks on the public mirror and public package.
5. Tag/confirm the release and publish release notes with install instructions and known limitations.
6. Announce only after the anonymous clean-room test passes.

## Rollback/incident response

- If sensitive data is discovered, stop announcement and restrict visibility where possible, but assume the
  data may already be copied. Rotate credentials and follow task 01's history-remediation process.
- If installation alone fails, keep the project public but mark the release broken, fix master, cut a new
  release, and remove broken install instructions/assets from the primary path.
- Never respond to a leak by deleting only the latest file or opening a public issue containing the leak.

## Final release evidence

- Merged release commit SHA and CI run URLs.
- History/security sign-off.
- Server/plugin tag names and artifact digests.
- Clean macOS/Ubuntu install logs (credentials removed).
- Claude/OpenAI setup verification matrix.
- Anonymous clone/pull/plugin-install verification.
- Screenshots of final white search UI on desktop and mobile.
- README word/line review and tested Claude installation prompt.

The project is open source only when all evidence exists and the final public installation works end to end.
