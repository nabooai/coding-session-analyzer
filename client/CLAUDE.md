# client/

The Go client. It redacts Claude Code transcripts locally and POSTs them to the server; every
command exists to set up, scope, inspect or undo that. User docs: [README.md](README.md).

## Layout

| file | what |
|---|---|
| `main.go` | dispatch and `usage()`, redaction patterns, `send()`, the watcher loop, `forget`, `redact` |
| `setup.go` | `setup`: consent, browser authorize, hooks, MCP |
| `init.go` | `init` (shared-token wizard, then setup's hooks), `install`/`uninstall`, `only`/`ignore`, `team`, `demo` |
| `ship.go` | `ship` (SessionEnd) and `ship --sweep` (SessionStart), fleet directives |
| `hook.go` | `hook` (ambient recall) and the settings.json hook installer |
| `config.go` | `~/.csa/config.json` and the consent predicates |
| `doctor.go`, `update.go` | `doctor`, `version`/`update` |
| `authstatus.go`, `certerr.go` | the 401 and TLS markers a detached ship leaves for the next prompt |

## Rules

- **Light client, heavy server.** No extraction, no database, no LLM here. Go stdlib only.
- **The ingest contract:** `POST <server>/api/ingest` with `{session_id, content, owner,
  redaction_mode}`; `session_id` is the transcript's basename without `.jsonl`. The client branches
  only on HTTP status and `ok`: non-2xx retries, `{ok:false}` is logged and not retried until the
  file grows.
- **A hook never makes the user wait on the server.** `ship` and `ship --sweep` re-exec into a
  detached child (`shipDetach`) and return in milliseconds. Only `hook` (recall) waits, inside its
  3s budget.
- **Redaction:** `secretPatterns` and `privacyPatterns` in `main.go` mirror
  `../server/analyzer/redact.py` label for label, regex for regex, in order. Never `(?i)` in a
  `secrets` pattern: Go folds Unicode, Python's `re.A` folds ASCII, and the server would fail open.
- **Consent:** nothing ships without a current record (`requiredPolicyVersion`). Here only `setup` and
  `init` write one, and the only ways past the notice are typing `yes` or `--yes`. The plugin
  (`../csa-plugin/`) writes the same record from the browser path: its `/csa:connect` consent is the
  Authorize click on `/cli-auth` (`method: browser`). Bump the policy version when WHAT ships changes.
- **The plugin is the other client.** `shipping` in the config names it; a config with `shipping` set
  never ships from here (`consentOK`), and `setup`, `init` and `install` refuse while it is enabled.
- **Two shipping paths, never both.** `setup` and `init` wire three hooks; `init --install` runs
  the watcher instead (bot/CI hosts). Wiring hooks retires a login watcher; `install` refuses where
  hooks ship. A command that connects a machine ends with it shipping, never with "one step left".
- **Config is `~/.csa/config.json`, mode 0600.** The `CSA_*` variables in README.md are dev
  overrides. There is no default server and no consent-free path.

## Releases

`.github/workflows/release.yml` builds `csa-client-<os>-<arch>[.exe]` for darwin/linux amd64+arm64
and windows/amd64 with `checksums.txt` on every push to `master` that touches `client/`, tagged
`v0.1.<run>`. The server does not serve those: its image builds the same five binaries from its
own commit (`../server/Dockerfile`, stamped with the image's version) and serves them at
`/latest/`, which is where `install` and `update` fetch from. After a release, copy the version and the four sha256 lines from
`checksums.txt` into `../Formula/csa-client.rb` (the job summary prints the stanza).

## Lessons and gates

- Setup versus hooks, the sweep floor, the dead-collector CPU burn, the update check:
  `../docs/development/operations.md`. Read before touching hooks, shipping or retries.
- Adding a harness's transcripts: `../docs/harnesses.md`.
- Gates: `gofmt -l .` prints nothing, `go vet ./...` and `go test ./...` pass. After editing
  `../docs/redaction_samples.json` (shared with the server), run `go test -count=1`.
  Every change clears `../docs/development/checklist.md`.
