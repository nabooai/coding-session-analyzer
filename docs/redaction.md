# Redaction

CSA redacts every transcript twice: on the laptop before it is sent, and on the server before it is
stored. The two implementations are mirrors: `client/main.go` (`redactWith`) and
`server/analyzer/redact.py` (`redact`). They run the same patterns in the same order and produce the
same labels, and a shared fixture holds them to it (see [Parity](#parity)).

> Regex is necessary, not sufficient. It catches keys, tokens and emails. It misses coworker names,
> project codenames and secrets written as prose. Redacted output is not "safe to share" on its own.

## Modes

Modes are ordered levels. Each one redacts strictly more than the one below it.

| mode | level | keeps | strips, on top of the level below | labels |
|---|---|---|---|---|
| `secrets` | 0 | emails, names, hostnames, public domains, the key of a credential assignment | high-precision keys and tokens, the value of a credential assignment (`password=...`) | `openai_key` `aws_key` `github_token` `slack_token` `private_key` `jwt` `gcp_key` `stripe_key` `docker_pat` `npm_token` `sendgrid_key` `hf_token` `gitlab_pat` `pypi_token` `telegram_bot` `slack_webhook` `azure_key` `credential` |
| `privacy` | 1 | public domains, names, prose | emails, connection strings, bearer tokens, the whole credential assignment for more keys (`token`, `pwd`, ...) and references, internal-TLD hostnames | `conn_string` `bearer` `email` `credential` `hostname` |
| `strict` | 2 | anything not on the deny list | your deny terms (names, codenames, customers, hosts, path prefixes) | `denylisted` |

`secrets` is the default. Each match becomes `[REDACTED:<label>]`, and `redact()` returns the text and
the labels it found. "The minimum of two modes" is the higher level (`redact.max_mode`).

### The credential assignment at `secrets`

The last `secrets` rule, so a token above keeps its own label. A credential key, then `=` or `:`, then
a value of four or more characters: only the value becomes `[REDACTED:credential]`, so
`password=hunter2-sekrit` ships as `password=[REDACTED:credential]`.

- **Keys:** `password`, `passwd`, `secret`, `secret_key`, `private_key`, `access_key`, `access_token`,
  `auth_token`, `refresh_token`, `client_secret`, `api_key`, in any case, with `_`, `-` or nothing
  between the words (`apiKey`, `API_KEY`, `client-secret`). Spelled as character classes, not `(?i)`.
  No leading `\b`, so `DB_PASSWORD=` and `aws_secret_access_key=` count; the trailing `\b` keeps
  `password_hash=`, `secrets:` and `Secretary:` out. Bare `token` and `pwd` are left to `privacy`:
  `token: the header` is prose and `PWD=/Users/...` is a shell variable.
- **Quotes:** the key and the value may be quoted, escaped or not, so a JSON config inside a raw JSONL
  line (`\"password\": \"hunter2\"`) is caught. A quoted key needs a quoted value, so a JSON
  `"secret": true` is left alone rather than turned into invalid JSON. The value runs to a space or a
  quote and takes escape pairs (`\\`, `\n`) but never `\"`, so a redaction never eats the escape of
  a closing quote and a redacted JSONL line still parses.
- **Kept:** a value that starts with an environment reference (`$DB_PASSWORD`, `${DB_PASSWORD}`,
  `${{ secrets.X }}`), `os.environ`, `os.getenv` or `process.env` names where the secret lives, and
  is not one. Only an upper-case `$NAME` counts, so `password=$ecretHunter2` is redacted. A value
  already redacted by a token rule keeps that label.

### The privacy patterns

Applied in this order, after the secret patterns:

| label | pattern |
|---|---|
| `conn_string` | `[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@\S+` |
| `bearer` | `(?i)\bbearer\s+[A-Za-z0-9._-]{8,}` |
| `email` | `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` |
| `credential` | `(?i)(?:secret[_-]?key\|private[_-]?key\|encryption[_-]?key\|signing[_-]?key\|password\|passwd\|pwd\|secret\|api[_-]?key\|access[_-]?key\|client[_-]?secret\|token)\b\s*[:=]\s*["']?[^\s"']{4,}` |
| `hostname` | `(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:internal\|local\|lan\|corp\|intranet\|localdomain\|home\|test)\b` |

`credential` has no leading `\b` so it catches underscore-glued keys (`aws_secret_access_key=...`); the
trailing `\b` still keeps `Secretary:`. The compound `*_key` forms come first because alternation is
leftmost-first: a bare `secret` would match inside `SECRET_KEY` and then fail its `\b`.

### Deny and allow

Both apply whenever their list is non-empty, whatever the mode. Mode only decides which pattern tiers
run, so a person or an admin can strip specific terms without turning on every privacy pattern.

- **deny**: literal terms, replaced as a case-insensitive substring with `[REDACTED:denylisted]`.
  Substring, not word boundary, because terms include paths and hosts where `\b` misbehaves. Pick
  specific terms: `cat` also hits `category`.
- **allow**: a match from any rule is kept when the matched text, or the value of a `secrets`
  credential assignment, equals an allow term exactly (case-insensitive). Exact, not substring, so `allow: a` does not keep everything.

## Known limits

- **Only internal hostnames.** `privacy` strips `*.internal`, `*.local`, `*.lan`, `*.corp`,
  `*.intranet`, `*.localdomain`, `*.home` and `*.test`. A broad hostname pattern would remove every
  public domain mention and gut recall. Put a specific public host in the deny list.
- **Credential values are greedy.** At `secrets`, `password: string` (a type annotation) loses
  `string`; at `privacy`, `token: the flow` can match. A few false positives are the accepted cost.
  A password written as prose (`the password is hunter2`) is not caught at any level.
- **Assignment shapes it misses.** `password := "x"` (Go), `'password' => 'x'` (Ruby, PHP) and
  `--password x` ship as written at every level. A password containing `"` is redacted only up to
  the quote.
- **ASCII word boundaries.** Go's RE2 is ASCII-only, so every Python pattern is compiled with `re.A`;
  otherwise a secret glued to a non-ASCII letter would pass the server while the client caught it.
  The remaining differences are in the case-insensitive privacy patterns only and are listed in
  `redact.py`.

## Who sets what

**Each person**, on their laptop. `csa-client setup` records `secrets`, or the previous install's
mode, raised to the workspace floor; `init --redact-mode <mode>` or `csa-client redact set <mode>`
picks another. The mode is stored in `~/.csa/config.json` as `redaction.mode`. Two optional files, one term per line, `#` comments allowed,
are re-read on every send:

- `~/.csa/redact.deny`: terms to strip.
- `~/.csa/redact.allow`: exact terms to keep despite a matching rule.

`csa-client redact` prints the current mode and the list sizes. `csa-client redact check [--mode M]
[--file F]` shows what a sample would look like before anything is sent.

**The workspace admin**, on the server. Three workspace settings set a floor that no laptop can go
below: `min_redaction`, `redaction_deny` and `redaction_allow` (`csa.py org config <org> --set min_redaction=privacy`,
or `POST /api/config` with a workspace admin's token). On every ingest the server stores the session
at `max(declared mode, min_redaction)` and records that mode in `sessions.redaction_mode`; the
workspace deny and allow lists always apply. A recall question is redacted at the same floor before
it is logged or traced, so the logs never hold text weaker than the stored transcripts.

The consent notice a person accepts names the mode they chose and what it keeps.

## Parity

`docs/redaction_samples.json` is a list of adversarial cases with `must_redact`, `must_keep` and the
expected labels. Both implementations load the same file: `client/redact_fixtures_test.go` and
`server/tests/test_redaction_e2e.py`. A pattern change lands in both files and in this table, or one of the
two tests fails.

The Claude Code plugin does not carry a third set of patterns: `csa-plugin/scripts/redact.py` is a byte
copy of the server's file, and `client/hook_gates_test.go` runs every fixture through it and through the
Go client and compares the output bytes. To check it yourself:
`cmp csa-plugin/scripts/redact.py server/analyzer/redact.py`. The gates in front of a recall request
(`shouldAmbientRecall`, `ambientQuery`, `looksSecret`, the `only` and `ignore` globs) share
`docs/hook_gate_samples.json` the same way.
