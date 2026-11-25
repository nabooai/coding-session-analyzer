# Lesson: landing copy

The public landing is read by security reviewers who quote it back to legal. Every sentence that
asserts something carries a `source` in `server/ui/src/landing.ts` (a file or route in this repo) and
`landing.test.ts` holds the copy to per-section word budgets, a no-dash rule, a real-name denylist,
and a list of sentences the audits rejected. Change the copy and the test tells you what you broke.

## What is true, and where

| Claim | Source |
|---|---|
| Facts with quotes, shipper, project, date | `hidden_context`, `sessions` in `store.py`; `/api/facts` |
| Recall into the next prompt, automatically, with one visible line | `client/hook.go` UserPromptSubmit (`additionalContext` + `systemMessage`); `_hook_ask` strict floor; `tel.served` |
| The hero terminal's line | `_hook_ask` builds `system_message`: `🧠 CSA recalled N past-session facts:`, the top fact with its date and id, `… and N more`. `recallBanner()` mirrors it; change both together |
| Naming a teammate puts their sessions first, then the team's | `route.classify` person scope, the person partition in `app.py` (reorders, never excludes); `/api/facts?owner=` |
| Extraction and labelling run on the AI provider the workspace configured | `llm.py complete()`, `providers.py` (AI Models), `judge.py` (initial pass and a staleness pass, so never "once") |
| Cost, tool calls, wasted calls per session | `session_metrics`; `/api/improve` pins a member to their own sessions, an admin sees all |
| Recall prompts are matched on the server, not by a model | `apiserver._hook_ask` (deterministic); each prompt is logged in `tel.queries` |
| A question to the ask tool may go to that provider to plan the search, redacted first | `route.py classify` (allow_llm), `ask.py nl_to_query`, `mcp_server.ask` redacts at the workspace floor |
| Redaction on the developer's machine, again at the workspace floor | `client/main.go redactWith`, `init.go` modes, `ingest.py` |
| Sign-in goes to the identity provider the workspace configured, and no script loads from it | `/api/sso/start` in `server/analyzer/sso.py`, only when `csa.py sso set` configured one; the page links there, nothing external is loaded |
| Workspace requests go to a Slack webhook, with company and email | the `request_webhook` setting, read by `apiserver._request_webhook` |
| The client checks for updates against this server only | `client/update.go` |
| The hosted operator can read any workspace and order clients to re-ship | `csa.py org overview / audit / fetch`, `client/ship.go` |
| Kept until the shipper or the workspace admin deletes it, no automatic expiry | `archive.py` `forever`, `/api/forget` admin path (PR #103). Founder decision 2026-09-05; the server must be true before the landing ships |
| Harness strip: Claude Code and Pi in full colour; Codex, Cursor, GitHub Copilot greyed and labelled "next" | `harness.py` parses claude-code and pi; a greyed mark is `next: true` in `HERO.harnesses` and makes no claim of support (founder decision, 2026-11-03) |

## What the page may never say

"Transcripts are deleted", "we don't access your data", "nothing else leaves", "once, for
extraction", a real timezone, and any real person, customer or repo name. The demo team is Mara
Okafor, Jules Renner, Tomasz Wiek, Priya Natarajan and Sam Achterberg on generic project names.

## Process

1. Write the sentence with its `source` in `landing.ts`; if you cannot name one, the sentence is
   not true yet.
2. Run `npm test`; fix the budget or the audit, never by loosening them silently.
3. Run the security-reviewer persona (`user-sims` skill) on the live page. Round one of the
   redesign found six false or missing statements; round two found one more.
