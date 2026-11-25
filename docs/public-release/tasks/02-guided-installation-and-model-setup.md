# Task 02: Guided installation and Claude/OpenAI model setup

**Status:** open · **Owner:** server + onboarding · **Priority:** P0

## Objective

A new user starts the server with one guided command and configures a working model without editing JSON,
reading compose internals, building an image, or visiting an Admin page before first value.

## Current gaps

- Quickstart requires manual `.env`, `config/config.json`, `COMPOSE_FILE`, compose startup and log inspection.
- Anthropic/OpenAI API providers exist, but setup is spread across env and Admin > AI Models.
- Claude subscription login requires a `with-claude-code` image rather than a normal guided choice.
- OpenAI/Codex login/OAuth is not implemented in `analyzer/providers.py`; only API-key setup is present.
  Do not advertise it until implemented and tested.

## Required UX

### Server installation

Provide one public command, for example:

```bash
curl -fsSL https://raw.githubusercontent.com/nabooai/coding-session-analyzer/master/install.sh | sh
```

The installer must:

1. Check OS, Docker and Compose and print actionable prerequisites.
2. Ask workspace name, admin email and public URL, with `http://localhost:8766` as the solo default.
3. Offer a provider/auth-method matrix before starting extraction.
4. Generate config with safe permissions and defaults; no hand-authored workspace JSON.
5. Select the correct published image automatically. Never require a user to edit a Dockerfile.
6. Start required services and wait for health.
7. Print the browser sign-in URL and the exact Claude Code connection step.
8. Be safe to re-run: preserve credentials/config, report current state, and do not duplicate services.

Offer a documented non-`curl | sh` path for security-conscious users, but keep one primary path.

### Model provider matrix

| Provider | API key | Login/subscription |
|---|---|---|
| Anthropic / Claude | Use existing HTTPS provider; test during setup | Use existing Claude Code OAuth only for a person's own local/single-user path |
| OpenAI / Codex | Use existing HTTPS provider; test during setup | Implement device/OAuth login or explicitly mark it unavailable for this release |

Rules:

- A central team server uses API/provider credentials, not one employee's subscription.
- Login setup must not print tokens, put them in URLs, commit them, or include them in logs.
- Setup picks sensible main/small model defaults and performs a minimal test call before declaring success.
- Failure says whether the problem is credential, model ID, endpoint, missing CLI, network, or terms/usage mode.
- No model credentials are needed to browse the login/setup UI, but extraction honestly shows a stalled queue
  until setup completes.
- The README lists only paths covered by this matrix and E2E tests.

## Deliverables

- Idempotent guided installer and rollback/troubleshooting output.
- Provider/auth setup shared by CLI wizard and browser welcome flow.
- OpenAI login spike with a written implement/defer decision; no false documentation.
- Published images for the supported login path; proprietary subscription images are not uploaded publicly.
- `.env.example` and example config reduced to safe bootstrap values.
- `INSTALL.md` uses the same commands as the installer and README.

## Acceptance

- [ ] Clean macOS and Ubuntu: one guided command reaches browser sign-in without manual config editing.
- [ ] Anthropic API key: successful test call and extracted session.
- [ ] OpenAI API key: successful test call and extracted session.
- [ ] Claude login: guided path selects the correct image and works without user-built images.
- [ ] OpenAI/Codex login: either passes the same flow or is clearly omitted from every public instruction.
- [ ] Wrong key/model/endpoint produces one actionable error; credentials never appear in output.
- [ ] Re-running installer preserves config and does not reset consent, corpus or credentials.
- [ ] No Admin navigation or compose-service knowledge is needed for first search.
- [ ] E2E proves the exact commands on the merged release commit.

## Merge requirement

Merge installer, setup implementation, tests and generated example config to `origin/master`; release images
are cut only from that merged commit.
