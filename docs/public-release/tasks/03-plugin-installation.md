# Task 03: Plugin installation and release

**Status:** open · **Owner:** plugin/client · **Priority:** P0

## Objective

A Claude Code user can inspect, install, connect and verify CSA from the public marketplace without a
compiled binary, manual settings edits, or routine slash-command sharing.

## Confirmed blocker

README and `csa-plugin/README.md` reference `plugin-v0.2.0`, but no such remote tag exists. The documented
installation cannot work as written.

## Deliverables

- Finalize the readable Python plugin and its consent/redaction behavior.
- Create a repeatable plugin release process and cut `plugin-v0.2.0` from the reviewed master commit.
- Keep `defaultEnabled: false`; nothing runs or ships before enable + browser Authorize.
- Ensure normal completed sessions ship deterministically through hooks; `/csa:send-session` is only an
  exception/manual path.
- Keep old history and unselected projects off by default.
- Provide `/csa:status`, `/csa:preview`, `/csa:scope`, `/csa:disconnect` and clear failure recovery.
- Document the Go binary separately for Windows/bots/backfill where required; do not present three equal
  choices to a new Claude Code user.
- Update marketplace metadata, plugin repository URL and version references for the company organization.
- Add a CI clean-install validation and plugin manifest validation.

## Acceptance

Run on clean macOS and Ubuntu with no existing `~/.csa` or CSA hooks:

- [ ] Marketplace add/install/enable commands in README work from the public tag.
- [ ] Plugin is disabled at install and its installed source matches the reviewed tag.
- [ ] Browser pairing creates a per-user token; client-supplied identity cannot override it.
- [ ] Before consent and for an unselected project, no session or prompt leaves the machine.
- [ ] After consent, a new completed session arrives without any slash command and becomes searchable.
- [ ] A missed/crashed session is caught by the deterministic catch-up path without duplicates.
- [ ] Historical sessions do not ship unless the user explicitly opts into backfill.
- [ ] Preview shows the exact redacted payload; planted fake secrets never reach the server.
- [ ] Disconnect revokes the credential and removes plugin-created local integration without touching
      unrelated Claude hooks.
- [ ] Wrong server URL, expired token, missing Python and untrusted TLS each give one actionable fix.
- [ ] The exact tagged plugin passes existing plugin tests, redaction fixtures and clean E2E.
- [ ] The Windows instruction leads to a working alternative or is explicitly marked as a later step.

## Merge/release requirement

Merge plugin changes and release workflow to `origin/master` before tagging. Tag only the merged commit.
Repeat installation anonymously after publication and attach the successful validation output to the release.
