# Ask API

`POST /api/ask` is recall over plain HTTP: the same call the Claude Code prompt hook makes
(`client/hook.go`), open to any script, skill or agent that holds a per-user token. It makes no model
call. For the MCP tool instead, see the agent section of
[Install on Kubernetes](public-release/install-kubernetes.md).

```bash
curl -s https://csa.your-company.example/api/ask \
    -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
    -d '{"query": "what did the team decide about payment retries"}'
```

Two bars, chosen by `session`. The prompt hook always sends its session id, so a call with one gets
the hook's bar: only the strict floor answers, over facts marked gold, and a miss is common. A call
without one (an agent, a script, a person) is a deliberate question and gets every tier down to an
honest miss. `strict` overrides either default.

## Request

`Authorization: Bearer <token>`, a per-user token from `csa-client setup`, or a service token an admin mints on **Admin > Service Tokens**. A
browser session's cookie is refused with 403. The body must be one JSON object (anything else is a
400 `invalid JSON body`), at most 32 MiB.

| Field | Type | Default | What it does |
|---|---|---|---|
| `query` | string | required | The question. Cut to 1000 characters, then redacted at the workspace's redaction floor, before it is searched or logged. Empty or missing: `{"ok": false, "error": "missing \"query\""}` |
| `strict` | boolean | `true` with a `session`, else `false` | `true` is the prompt hook's bar: only the strict floor answers, and only with facts marked gold (a reviewer's `gold`, or the judge's), so a miss is common. `false` is an explicit question: when the strict floor does not answer, a looser floor, then a plain word match, then a miss that names the words nothing in memory contains. No tier needs gold, and no tier serves a fact recall hides (see below). Send a JSON boolean: the string `"false"` counts as true |
| `k` | integer | `6` | How many facts at most, clamped to 1..12. A value that is not a number falls back to 6 |
| `person` | string | none | A name or email fragment. That person's facts come first, then the team's. A name that matches no one, or more than one person, answers `found: false` with a `message` that lists the known people |
| `cwd` | string | none | The asker's working directory. The server derives the project from it (a worktree path maps to its repository) and ranks that project's facts higher. Never a filter |
| `project` | string | none | A project name, used in place of `cwd` when `cwd` is absent. Same boost. In the `facts` tier it is a filter: only facts whose project contains it |
| `session` | string | none | The asking session's id, recorded with the question and with the facts served to it. Its presence makes `strict` default to `true` |
| `asked_by` | string | none | Read only when the credential names no person (loopback development with auth off). Otherwise the asker is the token's address and this field is ignored |

There are no other fields: `q`, `question` and `limit` are not read. The wording of the query
also steers recall: a first-person question ("what did I decide") ranks the asker's own sessions
first, a teammate's name ("why did nothing ship from bob's machine") counts as a matched word on
the facts that teammate owns, an explicit date ("on July 17", "last 3 days") is a hard date filter, and on a non-strict call
a question about which session ("find the session where we fixed the retry bug") answers with a
list of sessions.

## Response

Every handled request answers HTTP 200. A refused credential is 401, a cookie session 403, an
oversize body 413.

A hit:

```json
{"ok": true, "found": true, "tier": "lenient",
 "query": "what did the team decide about payment retries",
 "context": "# Recalled context for \"what did the team decide about payment retries\"\n\n## Decisions\n- ...",
 "n": 3,
 "system_message": "🧠 CSA recalled 3 past-session facts:\n  • [decision] Retry 429s with exponential backoff and jitter · 2026-06-03 (fact:3f2a9c1b)\n  … and 2 more (full text sent to Claude; ask it to `fact:<id>` for any one)",
 "coverage": {"missing": [], "distinctive": ["retries"], "terms": 4, "dismissed": ["about", "did", "the", "what"]}}
```

A miss:

```json
{"ok": true, "found": false, "tier": "miss",
 "message": "Nothing on \"...\" in past sessions. Words the corpus has never seen: kafka. Not searched: did, how, we. Every retrieval tier ran; this is a real miss, not an outage.",
 "coverage": {"missing": ["kafka"], "distinctive": [], "terms": 3, "dismissed": ["did", "how", "we"]}}
```

| Field | When | What it is |
|---|---|---|
| `ok` | always | `false` only for a request the route rejects, with `error` saying why |
| `found` | `ok` | whether anything was served |
| `tier` | `ok` | which step answered, below |
| `query` | a hit | the query as searched and logged, after redaction |
| `context` | a hit | markdown, the facts grouped by type, each with its owner, date, project and `id:`. At most 7000 characters, then `_[truncated by CSA ambient memory]_` |
| `n` | a hit | how many facts `context` holds |
| `system_message` | a hit | the short banner the hook shows the person: the count, the top fact, how many more |
| `message` | a miss | why nothing was served, in one or two sentences: the honest miss naming unseen words (non-strict), the list of known people for an unknown `person`, or a short "no strong past-session context" note |
| `coverage` | `ok` | `missing`: query words no fact contains; `distinctive`: query words rare enough in the corpus to carry the match; `terms`: how many words were searched; `dismissed`: query words dropped before retrieval (function words, a date the question is filtered by, and a locate question's locator words). Filled on every answer |

| `tier` | Meaning |
|---|---|
| `strict` | the strict relevance floor answered. The only tier a strict call can hit. A date ("today", "last week", "on July 17") filters by last activity and is no topic: a question that is only a date ("what did we do today") lists everyone's facts in that window (unbidden, only when the prompt asks), and the words left beside a date pass the same never-seen rule as `lenient` first, so "what is the weather today" is a miss on every call, the hook included |
| `lenient` | non-strict only: the looser floor answered, and the question has fewer never-seen words than known ones. Only words the asker typed count, each known when it or a synonym is in memory; a teammate's name, a word of one or two letters and question filler ("can", "explain", "how") count as neither. A person's recent work ("what has bob been working on") matches no words and always answers here |
| `locate` | non-strict, and a question about which session: `context` lists sessions (`# Sessions matching ...`), not facts |
| `facts` | non-strict only: no floor was cleared, every topic word of the question (as counted for `lenient`) is known, no date filters the question, and `context` is a plain word match over facts. Its second line says so; read it before relaying it |
| `miss` | nothing served. On a non-strict call, every tier above ran, or a word the corpus has never seen ruled out the two below the strict floor: "what is our vacation policy" matches a fact about a branch protection policy, and that is not an answer |

No tier serves a fact recall hides: one a reviewer flagged `wrong` (or `noise`, `rejected`, `already-documented`), or one whose validity ended because it was superseded or refuted (`invalid_at` is set). `unimportant` only ranks a fact lower. A hidden fact is still readable by its id (the MCP `fact` tool, the web UI), shown with its label and validity.

Every call, hit or miss, writes one row to the workspace's question log (`queries` in
[Data at rest](public-release/data-at-rest.md)) with the asker and the redacted query. The MCP `ask` tool redacts its question the same way before it is
logged, traced or sent to a model. The text of a question that was answered, and the session it came
from, is shown to every member of the workspace on the recall log (`GET /api/sessions`), without the
asker's name. A question that found nothing is shown only to the person who asked it.
