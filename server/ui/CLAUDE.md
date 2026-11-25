# ui/ — CSA Sessions Feedback UI (for AI agents)

Single-page dashboard over a team's mined coding sessions.

**One top bar, and only pages that exist are in it.** The people this serves are developers reading
their team's memory, not the person running the analyzer. The redesign lands in five PRs; the bar lists Memory, Sessions and Team
today, and Entities (PR 3) and Admin (PR 4, admins only) join with their routes (`NAV_ITEMS` in
main.ts). A tab that lands on Memory reads as a broken tab, so nothing is listed ahead of its page.

- **Memory** (`#/`, `#/?q=<query>`): the front door once signed in. Every mined fact, newest-useful
  first, with the scope control and type chips. The top bar holds the ONE search box; Enter writes the
  query into the hash and Memory's list answers it (no second box on the page). Rows stating the same
  claim fold into one card ("Seen in 3 sessions · webapp, infra", `memory.ts` `foldSameClaim`), and a
  rating on that card is sent for every folded ref. Each card: type, claim, the proving quote (folded
  to two lines), who shipped it, project, reuse, and one **Rate** control that opens the three verdicts.
  The verdicts read **Useful / Wrong / Noise** everywhere (`LABEL_TEXT` in `memory.ts`; Review, the
  session pane and the entity drawer show the full row); the pressed one clicked again is the undo,
  wire value `clear`.
- **Sessions** (`#/corpus`): mined sessions at the same scope, list on the left (project, owner,
  when, summary, facts, cost) and the chosen session on the right (`?s=<id>` in the hash, pushed by
  hand so a click does not refetch; Back and reload go through `route()`). The pane is a reading view
  (title, owner, date, metrics strip, facts by type, entities, worked on); `#/corpus/session/<id>` stays
  the full page (transcript, relations, links, `?fact=` scroll) and every link from Memory lands there.
  Cost is "·" unless the server sent a number: the owner and a workspace admin see it, nobody else.
- **Memory Graph** (`#/?view=graph&w=<days>`): the List / Graph switch sits beside the scope control
  (two controls, not one). The canvas is `graph.ts` fed by `GET /api/graph`; the window picker rewrites
  the hash; hover card, side panel (a fact: claim, quote, shipper, project, recalled-in links; a session:
  what it reused and produced). Project rules (`type: "rule"`) sit at the top of the List with no
  verdicts and no session link, and hang off their project's newest session in the Graph.
- **Entities** (`#/entities`, `#/entities/<id>` opens the drawer): the browser over
  `GET /api/entities`, full width for everyone (one row per canonical entity; a search matches aliases
  too; `facts` counts the facts from the sessions that mention it, which the page says out loud). The
  taxonomy (ten built-ins read-only, custom types add / edit / remove through `POST /api/entity-types`)
  is **Admin > Entity types** (`#/admin/entity-types`).
- **Team** (`#/team`): per-person rollup; **each name is a link that scopes Memory to that person**.
  A **workspace admin** (`me.role === "admin"`) also gets the live machines per person with a Revoke
  each, a role select, and the invite's status: there is no separate Members page (`#/admin/members`
  redirects here). Under 640px the table stacks into one card per person. Role and revoke POST from the cookie session (they are in
  `_COOKIE_POST_OK`); rotating the invite is bearer-only, the panel says how. A session signed in through
  the invite form (`me.hostname === "web"`) is always a member and the page says so. Its link row offers
  "Recalled into sessions" (`#/recall`) to everyone and Pipeline, Review and Ratings (`#/labels`) to
  admins. Welcome, Today and Improve still render but are not in the bar. **Connect a machine** is a
  sidebar item for everyone.
  There is no Operator page: cross-workspace administration is `csa.py org …` on the server, and no
  credential a browser holds reaches more than one workspace.

**Signed out**, a multi-tenant server shows the landing (`renderLanding`, copy and the fictional demo
story in `landing.ts`, audited by `landing.test.ts`); a single-tenant one shows the sign-in. The hero
graph and Memory's coming Graph view share `graph.ts` (seeded force layout + canvas view; the layout
is pure and unit-tested). The design tokens live at the top of `style.css`; fonts are vendored under
`public/fonts/` (no Google Fonts request); the Ring mark is `public/favicon.svg` and `ringMark()`.

**Scope is a person, not a boolean.** `scope` is `"everyone" | "mine" | <teammate email>`, and
`scopedOwner()` turns it into the `owner=` query param both pages send. The control is a segmented
`[Everyone | Just mine]` plus a native `<select>` of everyone else (from `/api/team`, cached in
`teamCache`, fetched alongside the page's own data and never blocking it). A binary switch was the
original bug: "show me Jules's work" could only be attempted as a text search for `jules`, which
returns claims that *mention* him, written by someone else. Any single-person scope also drops the
redundant Owner column from the Sessions table. `"mine"` comes from `/api/me`'s server-verified
email; with no verified identity the Just-mine button hides and only the picker remains. `hidden_context` facts everywhere reuse the same `factCard`
+ flag channel, so they are flaggable inline — the buttons READ as "Useful / Wrong / Noise /
Undo" (`LABEL_TEXT` in `memory.ts`) while the wire values stay `gold`/`wrong`/`unimportant`/`clear`.

**Do not reintroduce a disk browser.** `#/sessions` and `/api/local/sessions*` are gone: they
listed the SERVER's own `~/.claude/projects`, so every visitor to the hosted dashboard saw the
same one container-local chat and none of their own. Clients ship extracted facts, never
transcript files — there is nothing on that disk to show a tenant. `#/sessions`, `#/facts` and
`#/local/session/<id>` now redirect to Memory.

## Stack (and why)

**Vanilla TypeScript + Vite. No framework, no libraries.** (ponytail: lazy = efficient.)
This UI is a few hundred lines of DOM manipulation over four fetch calls — a framework, a
router, a state lib, or a CSS kit would each be more code and more deps than the app itself.
So: **do not add** React/Vue/Svelte/Solid, Tailwind/Bootstrap, a router, a state manager, an
HTTP/fetch library, a component or test framework. The only runtime tooling is `vite` +
`typescript`, both devDependencies. **No new deps without a written justification.**

## Run it

Dev (two processes):

```
uv run python apiserver.py        # from repo root → API on http://127.0.0.1:8766
cd ui && npm install && npm run dev   # Vite dev server; open the URL it prints
```

Vite proxies `/api` → `127.0.0.1:8766` (see `vite.config.ts`), so the app always calls
same-origin `/api/...`.

Prod:

```
cd ui && npm run build            # tsc --noEmit typecheck, then vite build → ui/dist/
```

`apiserver.py` serves `ui/dist/` directly and answers `/api` itself. `npm run preview`
serves the built bundle locally for a smoke test.

## API contract (mirrors apiserver.py — DO NOT change it; types live in src/api.ts)

- `GET  /api/labels` → `{ "gold":"boost", "wrong":"hide", "unimportant":"down-rank", "clear":"back to neutral" }`
- `GET  /api/sessions?result=|answered|missed` → `{ total:int, answered:int, as_of:str, rows:[{ at, session, mine:bool,
  source:"hook"|"api"|"mcp", automatic:bool, question:str|null, results:int }] }`: the newest recall requests
  (`tel.queries`, the rows Analytics counts as recall requests), `total` and `answered` all time whatever the
  filter, `as_of` the server's clock. A teammate's miss carries `question: null`.
- `GET  /api/sessions/{session}` →
  `[{ "asked_at":str, "question":str, "facts":[Fact] }]`
  where `Fact = { ref:str, kind:str, project:str, type:str|null, claim:str|null,
  evidence:str|null, label:"gold"|"wrong"|"unimportant"|null, flaggable:bool }`
- `POST /api/flag` body `{ "ref":str, "label":"gold"|"wrong"|"unimportant"|"clear", "note":str }` →
  `{ "ok":true, "ref":str, "label":str|null }` or `{ "ok":false, "error":str }` — `clear` deletes
  the review row (back to neutral; error if there is no flag to clear)

Only facts with `flaggable === true` (i.e. `kind === "hidden_context"`) get flag buttons.

Corpus explorer (read-only; types in src/api.ts):

- `GET  /api/corpus/stats` → `{ stages:{ingested,extracted,covered,stale,missing,indexed,memories:int},
  totals:{hidden_context,entities,relations,docs:int}, graph:{status:"connected"|"off", nodes?:int},
  stale_ids:str[] }` — `stale_ids` = session_ids that are extracted-but-stale (used to badge rows).
- `GET  /api/corpus/sessions` → `[{ session_id, project, git_branch:str|null, started_at:str|null,
  n_user:int, n_asst:int, deployed_to_prod:0|1, hidden_context:int, entities:int, summary:str|null,
  cost_usd:number|null }]` — `cost_usd` is the `session_metrics` figure when the caller may see it
  (the owner, a workspace admin, or `CSA_AUTH=off`; `gql.may_see_metrics`), else null. The UI renders
  "·" for null, never $0.00; a measured zero is a number.
- `GET  /api/corpus/sessions/{id}` → `{ session:{…meta…}, pipeline:{extracted,stale,indexed:bool, entities:int},
  worked_on:str[], hidden_context:{[type]:{claim,evidence,scope,ref,label}[]}, entities[], relations[],
  actions[], links[], data_sources:str[], metrics:{cost_usd,tool_calls,wasted,wall_s,idle_s}|null }`.
  `metrics` is an object (fields null when never measured) under the same gate as `cost_usd` above,
  and null outright when the numbers are not the caller's to see. Ids may contain `/` or `:` →
  `encodeURIComponent`.
- `GET /api/graph?owner=&days=30&limit=60` → `{ window:{days,limit,facts_budget,since,total},
  sessions:[{session_id,title,project,user,started_at,n_facts}], facts:[{ref,type,claim,evidence,
  session_id}], recalls:[{ref,session_id,at}] }` — the newest `limit` (max 120) sessions active in the
  last `days` days, at most `facts_budget` (600) facts between them, `total` matching before the caps;
  a fact's home is the earliest window session that mentions it; recalls come from `tel.served` with
  both ends in the window; rules hang off their project's newest window session. `GraphData` and
  `GraphResponse` (which adds `window`) both live in `api.ts`; `graph.ts` imports them.

Corpus `hidden_context` facts are adapted to the `Fact` shape and rendered with the same
`factCard`/flag controls, so they use the `POST /api/flag` channel above.

- `GET /api/corpus/sessions?owner=<email>` → the same list scoped to one person (the Mine/Everyone
  switch). Omit `owner` for the whole org.
- `GET /api/improve?owner=&limit=` → `{ settle_minutes:int, totals:{sessions,cost_usd,tool_calls,wasted_calls},
  rows:[{ session_id, project, user, summary, ended_at, model, tool_calls, tool_errors, dup_calls,
  reread_calls, top_read, top_read_n, top_tool, in_tokens, out_tokens, cache_read_tokens,
  cache_write_tokens, cost_usd, wall_s, idle_s, max_gap_s, n_user, n_asst, settled:bool,
  findings:[{kind:"cost"|"turns"|"time", headline, detail}] }] }` — cost-desc. `findings` is empty
  whenever `settled` is false.
- An unknown `/api/...` path is a **404 JSON**, not the SPA shell — only non-`/api` paths fall
  through to `index.html`.
- `/api/corpus/stats` also carries `stages.on_disk` and `feedback:{served_sessions,served_facts,flags,judge_labels,judge_gold}`
  (powers the home pipeline strip); it is stat-only — never parses transcripts.

Flags ledger:

- `GET /api/flags` → `{ total:int, counts:{[label]:int}, rows:[{ kind, ref, label, note:str|null,
  flagger:str|null, claim:str|null, type:str|null, session_id:str|null, project:str|null,
  user:str|null }] }` — every human flag, newest first (`flagger` = who flagged: an email via MCP's
  X-CSA-User header, "mcp", or "web-ui"); rendered on `#/labels` under the label definitions.
- Corpus rows and session meta now carry `user` (owner: the Claude account email the session was
  ingested under; remote clients send it in `POST /api/ingest` as `owner`).

Facts browser (server-side FTS + filters, paginated; types in src/api.ts):

- `GET /api/facts?q=&type=&label=&owner=&project=&offset=0&limit=50` → `{ total:int, offset, limit,
  rows:[{ ref, type, claim, evidence:str|null, project:str|null, session_id, user:str|null,
  started_at:str|null, label:str|null, flagger:str|null, corroboration:int, used:int, dominant:0|1,
  judge_gold:0|1 }] }` — `flagger:"judge"` = the label was set by the haiku auto-labeler (humans override
  via flag); `judge_gold` = the judge rated it gold (capped rerank boost until a human confirms);
  `used` = recalled into N later sessions; `dominant` = about the entity the session was about;
  `corroboration` = extracted independently from N sessions. Rows adapt to `Fact` for `factCard`.
  The first unsearched page (no `q`, no `label`, `type` "" or "rule") also carries `rules:[FactRow]`:
  `improve.project_lessons` rows as facts with `type:"rule"`, `claim:"<Rule>. <do>"`,
  `evidence:"<n> of <reviewed> sessions"`, `project`, empty `session_id`, no user; counted apart from
  `total`, never flaggable. Under an owner scope they exist only where that person's numbers do
  (their own, or an admin), so "One teammate" shows none.
- Corpus session detail `hidden_context` facts also carry `used:int` and `dominant:0|1`.

Questions — HITL review queue (types in src/api.ts):

- `GET  /api/questions` → `{ types:str[], labels:{gold,wrong,unimportant:str},
  counts:{uncertain_extraction,uncertain_labels,certain:int}, uncertain_extraction:QFact[],
  uncertain_labels:QFact[], certain:QFact[], today:FactRow[],
  receipts:{your_labels,labeled_served,used_this_corpus,judge_labels,judge_gold:int} }` where
  `QFact = { ref, type, scope:str|null, claim, evidence, confidence:str|null, project, session_id,
  label:Label|null, flagger:str|null, judge_gold:0|1, type_confirmed:bool }`.
  `today` = top-5 leverage-sorted unlabeled facts (same row shape as /api/facts) — the Review
  page renders ONLY these plus the receipts; the old type-confirmation UI is gone.
- `POST /api/hctype` body `{ ref:str, type:str }` → `{ ok:true, ref, type }` or `{ ok:false, error }`.
  Confirms a hidden_context fact's type; the review card leaves the queue on success.

`uncertain_labels` facts adapt to `Fact` and reuse `factCard`/flag (`POST /api/flag`); the
"not a fact" reject button also uses `POST /api/flag` with `label:"wrong"`.

## File map

- `index.html` — shell; mounts `#app`, loads `src/main.ts`.
- `src/api.ts` — typed fetch client. **Network only.** TS types here are the single source
  of truth for the contract above.
- `src/main.ts` — all DOM building, rendering, hash router, event handlers.
  Routes (matched in this order): `#/corpus/session/<id>` → session detail; `#/corpus` →
  Sessions; `#/facts`, `#/sessions`, `#/local/...` → redirect to `#/` (retired);
  `#/pipeline` → pipeline + live ingest + spend (operator); `#/questions` → HITL review queue;
  `#/labels` → what flagging does; `#/recall` → recall log; `#/team` → per-person rollup;
  `#/operator[/<org>]` → control plane; `#/session/<id>` → recall detail;
  `#/` (default) → **Memory**, the fact feed.
- `src/style.css` — small dark-friendly styles, no framework.
- `vite.config.ts` — dev `/api` proxy + `build.outDir=dist`.
- `tsconfig.json` — `strict: true`, checks `src/` only.
- `CONVENTIONS.md` — naming + layering rules. `AGENT_CHECKLIST.md` — pre-finish gate.

## Lessons and skills that apply here

- The design system, tokens, fonts and the Ring: `../../docs/design/palette.md` (`tokens.json` beside it).
- Views outside the router, `[hidden]`, canvas sizing, theme persistence, sticky offsets, judge
  labels, the SPA fallback, browser tooling: `../../docs/development/web-ui.md`. Read before any change here.
- What the landing may claim: `../../docs/development/landing-copy.md` and the audit in `landing.test.ts`.
- Skills: `ui-visual-walk` (build, screenshot, compare to the mock, both themes, 390px),
  `scratch-server` (a seeded box to look at), `user-sims` (the five personas), `code-tests`.
- `npm run check` = tsc, eslint, vitest. eslint is dev tooling only; runtime dependencies stay none.

## Conventions (full list in CONVENTIONS.md)

- Types mirror the API contract and live in `src/api.ts` — the single source. Import from there.
- Layering: **DOM/rendering in `main.ts`, network in `api.ts`.** No app logic or DOM in `api.ts`.
- Keep functions small and single-purpose. Build DOM with the `el()` helper, not `innerHTML`
  (user text like `claim`/`evidence`/`question` goes through `textContent`, so it is XSS-safe —
  keep it that way).
- Prefer editing existing files over adding new ones.

## Gotchas

- **All fetches must be relative (`/api/...`).** Never hardcode `http://127.0.0.1:8766` or any
  port. The dev proxy handles dev; prod is same-origin. Hardcoding breaks prod.
- The proxy in `vite.config.ts` affects **dev only**. It has no effect on the built bundle.
- Vite typechecks nothing at build unless `tsc` runs — `npm run build` runs `tsc --noEmit`
  first on purpose. Keep it green.
- `tsconfig.json` includes `src` only, so `vite.config.ts` is not tsc-checked (Vite loads it via
  esbuild). That's why no `@types/node` is needed.
- **Auth:** the app runs on a real team server, not just 127.0.0.1. The
  session is an **httpOnly** cookie — JS cannot read it, so there is no local "am I signed in?" flag:
  the server is the only source of truth. `api.ts` turns every `/api/*` 401 into an `AuthError`, and
  `showError`/`boot` render the login view on one. Keep it that way — no localStorage mirror of auth
  state (it drifts from the cookie), and no new POST route reachable from the browser without adding
  it to `_COOKIE_POST_OK` in `apiserver.py` (a browser session may read and flag; it may not forget,
  ingest, or ask). Still no secrets/env config in this app: the cookie never reaches JS.
