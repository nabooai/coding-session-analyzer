# Claude Code plugin

The CSA plugin is readable Python with no compiled binary. It installs disabled and sends nothing until you
enable it, run `/csa:connect`, and authorize the two-word code in your browser.

## Everyday commands

| Command | Purpose |
|---|---|
| `/csa:connect <server-url>` | Pair this machine and choose automatic or manual mode |
| `/csa:status` | Show server, consent, project scope, and recovery action |
| `/csa:preview` | Write the exact locally redacted payload without sending it |
| `/csa:scope` | Include or ignore projects |
| `/csa:sent` | Review the local send ledger and payload hashes |
| `/csa:send-session` | Explicitly send one session |
| `/csa:forget <id>` | Delete one shipped session and derived facts |
| `/csa:disconnect` | Revoke the credential and remove plugin-created integration |

## Defaults

- New completed sessions ship automatically only after consent in automatic mode.
- Historical sessions and unselected projects stay local.
- A deterministic sweep catches a completed session missed by a hook without creating duplicates.
- Common key/token formats are redacted before network access.
- Manual mode sends no prompt or session unless you explicitly request it.

Use `/csa:preview` with planted synthetic secrets before trusting a new redaction policy. Names, code, paths,
and prose secrets are not guaranteed to be secret formats; scope and deny rules remain important.

Windows, bots, CI, Codex, and bulk backfill use the separate Go client. New Claude Code users should start
with the plugin, not choose among several equivalent installers.
