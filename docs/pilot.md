# Run a CSA pilot

A 2 to 4 week pilot for a VP Eng or a DevEx lead whose team already uses Claude Code. It says who
does what, what CSA measures today and where you read it, what it costs to run, and when to call it.

## Who

| Role | Usually | Owns |
|---|---|---|
| Buyer | VP Eng, Head of Platform or DevEx | the go or no-go at the end |
| Approver | Security or IT | the answers in [Security and data](#security-and-data) and [Who sees our session text](#who-sees-our-session-text) |
| Champion | a DevEx lead or staff engineer | the install, inviting the pilot team, the weekly check |
| Pilot team | 5 to 20 engineers who share a codebase | using Claude Code as they already do |

Pick one team that works on the same repositories. Recall pays off when one person's session answers
another person's question, so a team spread over unrelated code shows little.

## Setup (day 1)

| Step | Where |
|---|---|
| Run the server on a box or cluster you control | [Quickstart](quickstart.md), [Docker Compose](public-release/install-server.md), [Kubernetes](public-release/install-kubernetes.md) |
| Add a model provider (Anthropic, Bedrock, Vertex, OpenAI, Azure OpenAI, or an OpenAI-compatible endpoint) | **Admin > AI Models**, [install-kubernetes.md §4](public-release/install-kubernetes.md#4-ai-models-the-model-providers-in-the-app) |
| Sign in through your identity provider, or invite by link | [sso.md](sso.md), [Okta](public-release/sso-okta.md) |
| Connect each laptop: `curl -sSL <server>/install \| sh`, then `csa-client setup` | **Connect a machine** in the app, [install-client.md](public-release/install-client.md) |
| Optional: cap extraction spend per day | `csa.py org config <org> --set daily_usd=<dollars>` on the server ([on Kubernetes](public-release/install-kubernetes.md#3-the-command-line-when-you-need-it)); see [Budget](#budget) |

`csa-client setup` is the command that wires all three Claude Code hooks. On **Admin > Analytics**, the
**Members** card should count each pilot engineer as Shipping within a day of their first session.

## What CSA measures, and where

Every number below is on **Admin > Analytics** (window: 4 weeks, 12 weeks or 6 months) unless the
row says otherwise. Hover a dotted label in the app for the exact definition.

| Question | Read it on | What it means | What it does not mean |
|---|---|---|---|
| Is the team using it? | **Active contributors**, **Sessions shipped**, **Members** (Shipping, Quiet, Not connected), **Silent machines** | people whose Claude Code sessions reached CSA | that anyone read what CSA kept |
| Is it learning anything? | **Facts in memory**, **New facts**, **New facts per week, by type** | decisions, gotchas and constraints extracted, each with its quote | that every fact is right; people rate facts Useful, Wrong or Noise on **Memory** |
| Does knowledge move between people? | **Facts reused**, **Handoffs to a teammate per week**, **Most reused facts**, **Top contributors** (Teammates reached) | a fact recalled into a session of someone other than its author | that the teammate needed it; a recall into the author's own session is not counted |
| Is recall used? | **Recall requests** (automatic and asked) | every prompt the hook checked, plus questions through MCP or the `csa-recall` skill | that the person wanted help; most requests are automatic |
| Does recall find something? | **Answered** | the share of requests that returned at least one fact; the rest are honest misses | that the facts helped |
| Did the session use it? | **Acted on** | at least this share of answered requests, from shipped sessions, were followed by words from the recalled fact. A lower bound: silent use is not seen | the true rate, which is at least this. Shows "Not measured" until an answered request comes from a shipped session |
| What did people ask? | **Team > Recalled into sessions** | every recall request, answered or missed, and the session it came from, without the asker's name; a teammate's miss hides its question | who asked |

**Not measured: time or money saved.** CSA does not know what a session would have cost without the
fact it was given, and a guess at that counterfactual is not a number. Use the reuse and acted-on
rows above as the evidence, and ask the pilot team at the end.

## Cost to run

| Item | What it is | Where you see it |
|---|---|---|
| Extraction | one model call per finished session to read it, plus a cheaper call to judge the new facts | **CSA extraction spend** tile and per-day chart on **Admin > Analytics**, with per session read and per contributor |
| Recall on a prompt | no model call | |
| Questions through MCP | a small call to route or plan the search, on some questions | your provider's bill only; not in the spend tile |
| Daily cap | optional, off by default; past it, sessions wait in the queue and are never dropped | **Daily cap** tile, once set |

The spend tile is what CSA paid your provider, not your team's Claude Code bill (that is the
**Claude Code cost** column on **Sessions**). A model without a list price in CSA shows tokens instead
of dollars.

### Budget

Extraction is the cost that grows with the pilot: one read per finished session. Engineers x sessions
per engineer per week x weeks x cost per session:

| Pilot | Engineers | Sessions per engineer per week | Weeks | Sessions | At $0.07 each |
|---|---|---|---|---|---|
| Small | 5 | 10 | 2 | 100 | about $7 |
| Typical | 10 | 15 | 4 | 600 | about $42 |
| Large | 20 | 25 | 4 | 2,000 | about $140 |

**The assumption.** $0.07 is one measurement: backfilling 73 sessions with Claude cost $5.07. Cost
per session scales with your model's price and your sessions' length, so a cheaper model scales the
table down and a pricier one up. After week one, read **per session read** under **CSA extraction
spend** on **Admin > Analytics** and redo the sum with your own figure.

**The bound.** Set a daily cap on the server: `csa.py org config <org> --set daily_usd=<dollars>`
(there is no screen for it). **Admin > Analytics** then shows a **Daily cap** tile with the days it was
reached, and the cap as a line on the spend chart. The cap counts extraction and judging, checked
before each batch, so a day can end slightly over it. Past it, sessions wait in the queue for the next
day and are never dropped, so the pilot's extraction spend stays near cap x days. A model without a
list price is not counted in dollars: cap it with `daily_tokens=<tokens>` instead. Questions through
MCP are not counted against the cap.

## Security and data

| Question | Answer |
|---|---|
| What leaves a laptop? | each finished session's transcript, redacted at the mode the person chose, and each prompt, redacted in `secrets` mode, to your CSA server only. [SECURITY.md](../SECURITY.md#what-leaves-a-laptop) |
| What does the server call? | your model endpoint, your identity provider, nothing else unless you turn on trace export or a webhook. Measured in [egress.md](public-release/egress.md) |
| What is redacted? | secrets by default; `privacy` and `strict` add more. Pattern matching, not a guarantee. [redaction.md](redaction.md) |
| What is stored, and for how long? | facts and quotes until deleted; transcripts only when the workspace turns on retention. Every file, who reads it, what deletes it: [data-at-rest.md](public-release/data-at-rest.md) |
| Who sees what? | every member reads the workspace's facts; transcripts only their owner and an admin. [data-at-rest.md](public-release/data-at-rest.md#what-a-workspace-admin-can-see-about-a-person) |
| Can we delete? | the owner or the machine that shipped it (`csa-client forget`), or an admin (`POST /api/forget`) deletes a session and everything mined from it. Backups and the question log are not reached. [data-at-rest.md](public-release/data-at-rest.md#deletion-paths) |

## Who sees our session text

Session text leaves your CSA server for one place: the model provider the workspace runs on. Laptops
send only to your CSA server. Recall on a prompt makes no model call.

| Step | Text sent | Sent to |
|---|---|---|
| A laptop ships a finished session | the transcript, redacted on the laptop at the person's mode | your CSA server only |
| A laptop asks for recall on a prompt | the prompt, redacted in `secrets` mode | your CSA server only; recall there is lexical, no model call |
| The server queues the session | the transcript, redacted again at the workspace floor; the stricter mode wins | stays on the server |
| Extraction, once per finished session | the redacted transcript | the **main model** |
| Judging facts | each new fact with its quote; groups of near-duplicate facts (claim, date), to mark the older ones stale | the **small model** |
| A question through MCP `ask` | the question, redacted at the workspace floor. When it is unclear whether it asks about the asker, a teammate or the team: the asker's and the session owners' emails. When it is relational: entity ids from your corpus (tickets, repos, services) and the person it is about | the small model to route, the main model to plan a query. The query runs on the server; its rows are not sent |

Each role runs on the provider and model an admin picks for it on **Admin > AI Models**. Adding a
provider alone moves nothing:

| Provider | Where the text is processed |
|---|---|
| AWS Bedrock | your AWS account (the key you enter), in the region you enter |
| Google Vertex | your GCP project, in the region you enter, with that project's service account key |
| Azure OpenAI | your Azure OpenAI resource, at the endpoint and deployment you enter |
| Anthropic | Anthropic's API (`api.anthropic.com`), or the https endpoint you enter |
| OpenAI | OpenAI's API (`api.openai.com/v1`), or the OpenAI-compatible https endpoint you enter (Gemini's, a gateway): that vendor's cloud |
| No model picked for the role | the Claude credential in the `csa-llm` Secret or the compose `.env`: Anthropic's API with `ANTHROPIC_API_KEY`, your gateway with `ANTHROPIC_BASE_URL`, or Bedrock or Vertex with `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX` and that cloud's settings |

- The provider's settings on **AI Models** decide the endpoint, account and region. How the provider
  processes and keeps the text is your agreement with it. A Bedrock model id with a cross-region
  prefix, such as `us.` or `global.`, lets AWS serve the call from another region; that choice is in
  the model ids you list.
- An endpoint typed on **AI Models** must be a public https address. A private gateway goes through
  `ANTHROPIC_BASE_URL` in the Secret, which applies only to a role with no model picked.
- Every connection the server and laptops open, measured: [egress.md](public-release/egress.md).

| Stored on | What |
|---|---|
| Your CSA server's data volume | facts with their quotes, session metadata, the question log, redacted transcripts while queued, the extraction cache, transcripts only when retention is on, provider keys in plaintext. Every file: [data-at-rest.md](public-release/data-at-rest.md) |
| Laptops | in `~/.csa`: the client's config and token, its logs, a cached teammate roster, redaction term lists, a marker per shipped session. No session text beyond what Claude Code already keeps |
| The model provider | nothing CSA asks it to keep: each call sends a prompt and reads the answer. The provider's own retention terms apply |

## Known ceilings

Say these before the pilot starts. The full list: [org-admin.md, known ceilings](public-release/org-admin.md#known-ceilings-in-v1-say-these-out-loud-to-customers).

- One server process of each kind and SQLite on one volume. Backups are snapshots on the same disk, and restore is manual.
- The audit log records admin actions, not which person ran them, and does not log reads.
- Redaction is best effort.
- Claude Code is the supported source.

## Exit criteria

Agree on these on day 1, read them on the last day from **Admin > Analytics** at the pilot window.

| Signal | Healthy pilot |
|---|---|
| Adoption | most of the pilot team Shipping, no Silent machines left unexplained |
| Reuse | **Handoffs to a teammate per week** above zero every week after the first; **Most reused facts** lists more than one author |
| Recall | **Answered** steady or rising; **Acted on** shows a lower bound, not "Not measured" |
| Quality | few facts rated Wrong or Noise on **Memory** |
| Cost | **CSA extraction spend** per session read inside what you agreed |
| People | the pilot team can name a time a recalled fact saved them a search or a repeat mistake |

If adoption is low, check the laptops before the product: `csa-client doctor` on each machine.
