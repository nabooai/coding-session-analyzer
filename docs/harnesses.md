# Harnesses: how a coding agent's sessions reach CSA

Claude Code is the live source. Every other harness is a parser plus a discovery rule: the server
normalises its transcript into Claude Code's event shape and everything downstream stays untouched.

## What exists today

| Harness | Server parser (`server/analyzer/harness.py`) | Client discovery | Landing strip |
|---|---|---|---|
| Claude Code | `_claude_code` (identity adapter, the canonical shape) | `~/.claude/projects/**/*.jsonl`, hooks wired by `csa-client setup` | yes |
| Pi (the Slack relay bot) | `_pi` (session v3 header; toolCall and toolResult mapped; project from the Slack channel; requester from `Initiated by:`) | the relay ships under its own credentials; the workspace's `relay_accounts` setting re-attributes | yes |
| Codex, Cursor, GitHub Copilot | none yet | none yet | shown as a forward claim (founder decision, 2026-09-05) |

The landing strip lists all five. Until a parser lands, that is a promise, recorded in
`server/ui/src/landing.ts` (the `harnesses` claim carries its source) and in [`development/landing-copy.md`](development/landing-copy.md).

## The canonical shape

One JSON event per line: `{"timestamp", "cwd", "gitBranch", "message": {"role": "user" | "assistant",
"content": [blocks]}}` where blocks are `text`, `tool_use` (`name`, `input`) and `tool_result`.
`ingest._session_from_lines` counts turns, tokens and tool calls from this shape; `harvest` reads
Bash tool inputs for deterministic evidence; `extract` sends the redacted text to the LLM.

## Adding a harness

1. **Sniff.** One clause in `harness.detect(first_event)`: a header field, a version, a file name
   convention. The default is claude-code.
2. **Adapt.** One function in `_ADAPTERS` that maps each raw event to the canonical shape. Map the
   harness's tool call and tool result to `tool_use` and `tool_result`; keep the tool names the
   harvest recognises (`Bash` with `{"command": ...}`). If the harness is a relay (one account ships
   everyone's work), derive the requester like `pi_requester` does and let `sessions.user` carry the
   person; `shipped_by` carries the credential.
3. **Project.** If `cwd` is meaningless for the harness (Pi's is one path for every session), derive
   the project from what the harness knows (a channel, a workspace name) in the adapter.
4. **Self-check and tests.** Extend the `__main__` self-check in `harness.py` with a real minimal
   transcript (three events: a user turn, a tool call, a tool result) and add a case to
   `tests/test_transcript_shapes.py`. Redaction runs on the normalised text, so include a secret in
   the fixture and assert it is stripped by ingest.
5. **Discovery on the laptop.** The Go client watches `~/.claude/projects/**/*.jsonl` only. A new
   harness needs its transcript path and session-id rule in `client/` (watch, sweep, the `.owner`
   sidecar convention) and a consent notice that names it; the client is stdlib Go and ships raw
   redacted text, nothing more.
6. **Recall.** The UserPromptSubmit hook is Claude Code's; a harness with its own hook mechanism
   needs the equivalent of `client/hook.go` (POST the prompt with its `session` id to `/api/ask`,
   which keeps the hook's strict gold-only bar, and print the served facts).
   Without it, the harness ships sessions but does not receive memory.
7. **The landing.** A planned harness may sit in `HERO.harnesses` with `next: true`: its mark renders
   greyed with the "next" label. Flip it to `next: false` only once the parser ships. The mark is a
   transparent PNG under `server/ui/public/logos/` (crop to the mark, key out any checkerboard,
   `mono: true` if it is one colour so dark mode inverts it).
8. **Docs.** This table, and the harness list in the root `README.md`.

## Lessons

- Pi's `Initiated by:` line is matched only inside the metadata block, anchored, so a later message
  quoting it cannot re-attribute a session (`_PI_REQUESTER`).
- A relay's cwd is the relay's, not the person's; never let it become the project.
- `sessions.user` is an authorisation field downstream (Ask-X scoping); a parser that guesses it
  wrong leaks one person's facts into another's scope. Prefer no requester to a guessed one.
