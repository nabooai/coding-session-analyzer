// Every visible string on the signed-out front door (hero, three pillars, what is stored, request,
// sign-in) and the fictional demo story behind the hero graph. Pure module: no DOM, so landing.test.ts
// can hold it to its rules without a browser: a word budget per section, a `source` (file or route in
// this repo) on every sentence that makes a claim, no dashes, no real people or projects, and none of
// the sentences the security audit rejected. main.ts renders these; the test file audits them.

import type { GraphData } from "./api";

/** A sentence the page asserts about the product, with the file or route that makes it true. */
export interface Claim {
  text: string;
  source: string;
}

export const NAV = {
  how: "How it works",
  stored: "What is stored",
  request: "Request a workspace",
  signIn: "Sign in",
} as const;

/** The browser tab and the link preview; index.html carries the same two strings (pinned by the test). */
export const META = {
  title: "CSA · Team memory for Claude Code",
  description: {
    text: "What your team learned in past Claude Code sessions, recalled into the next one automatically, with the quote that proves it.",
    source: "client/hook.go UserPromptSubmit; hidden_context.evidence server/analyzer/store.py",
  },
} as const;

export const HERO = {
  name: "CSA",
  line: { text: "Turn your team's Harness sessions into shared, usable knowledge.",
          source: "/api/ingest client/main.go; server/analyzer/harness.py; /api/facts server/apiserver.py; client/hook.go UserPromptSubmit" },
  // The strip of harness marks above the graph. `mono` marks are one colour and invert in dark mode.
  // `next` marks render greyed with the `nextLabel`: harness.py parses claude-code and pi today, the
  // other three have no parser yet (docs/harnesses.md step 7 flips one when it ships).
  harnesses: [
    { id: "pi", name: "Pi", mono: true, next: false },
    { id: "codex", name: "Codex", mono: false, next: true },
    { id: "claude-code", name: "Claude Code", mono: false, next: false },
    { id: "cursor", name: "Cursor", mono: true, next: true },
    { id: "copilot", name: "GitHub Copilot", mono: true, next: true },
  ] as const,
  nextLabel: "next",
  cta: "Request a workspace",
  alt: "Five sessions of an invented team as a graph: each session with the facts it produced, and arrows where a fact was recalled into a later session.",
  legend: [["session", "Session"], ["decision", "Decision"], ["gotcha", "Gotcha"], ["constraint", "Constraint"], ["todo", "Todo"], ["pref", "Preference"]] as const,
  legendEdge: "Recalled into",
  cardSession: "Session",
  cardRecalled: "Recalled from",
  panelRecalled: "Recalled into",
  panelShipped: "Shipped by",
  close: "Close",
} as const;

export interface Pillar {
  id: string;
  eyebrow: string;
  head: { plain: string; em: string };
  body: Claim;
  shotTab: string;
  shotAlt: string;
}

export const PILLARS: readonly Pillar[] = [
  {
    id: "observe",
    eyebrow: "Observe",
    head: { plain: "See what", em: "every session did." },
    body: {
      text: "Every finished session on the team, with the project it touched and the facts it left behind, each with the quote that proves it. Your own sessions also show what they cost, how many tool calls they made and how many were repeats; a workspace admin sees that for everyone.",
      source: "session_metrics server/analyzer/store.py; /api/improve org_admin scoping server/apiserver.py; hidden_context.evidence server/analyzer/store.py",
    },
    shotTab: "Sessions",
    shotAlt: "The Sessions page: one session with its cost, tool calls, wasted calls and duration, and the facts it produced.",
  },
  {
    id: "discover",
    eyebrow: "Discover",
    head: { plain: "Search what your team", em: "already worked out." },
    body: {
      text: "Every fact is searchable and scoped to everyone, just you, or one teammate. Each result carries the quote from the session that proved it, who shipped it, the project, and how many later sessions reused it.",
      source: "/api/facts q, owner, used server/apiserver.py; tel.served server/analyzer/store.py",
    },
    shotTab: "Memory",
    shotAlt: "The Memory page: a search for rate limit, scoped to everyone, with three facts and their quotes.",
  },
  {
    id: "improve",
    eyebrow: "Improve",
    head: { plain: "Each session", em: "teaches the next." },
    body: {
      text: "When a teammate starts a session, the facts that match their prompt are recalled into it, whoever shipped them. When the same finding repeats across a project's sessions, it becomes a rule the project keeps: counted, not scored.",
      source: "client/hook.go UserPromptSubmit; tel.served server/analyzer/store.py; project_lessons server/analyzer/improve.py",
    },
    shotTab: "Memory",
    shotAlt: "One fact opened: its quote, who shipped it, the three later sessions it was recalled into, and a project rule counted from repeated findings.",
  },
];

export const STORED = {
  eyebrow: "What is stored",
  head: { plain: "What is stored,", em: "for how long, and who can read it." },
  cardTitle: "What is stored",
  // Five short points. `em` is the one phrase set in ink weight.
  items: [
    {
      text: "Facts and their quotes, who shipped them, the project, the date, the prompts sessions send for recall, and the transcript if your workspace keeps it.",
      em: "",
      source: "hidden_context, sessions, tel.queries server/analyzer/store.py; retention_days server/analyzer/archive.py; archive_days server/analyzer/tenant.py",
    },
    {
      text: "Facts stay in your workspace until the person who shipped them or the workspace admin deletes them.",
      em: "No automatic expiry.",
      source: "/api/forget server/apiserver.py; archive.drop server/analyzer/archive.py; keep-forever and admin forget: PR #103",
    },
    {
      text: "Secrets are stripped on the developer's machine before anything is sent, at the level that developer chose. The server strips again at the workspace floor.",
      em: "",
      source: "redactWith client/main.go; client/init.go modes; server/analyzer/ingest.py floor; /api/policy",
    },
    {
      text: "The AI provider you configure gets the redacted transcript for extraction, the facts for labelling, and a question to the ask tool to plan its search. Sign-in goes to the identity provider your workspace configured, and no script loads from it. A Slack webhook gets workspace requests.",
      em: "No other third party.",
      source: "server/analyzer/llm.py complete(); server/analyzer/providers.py (AI Models); server/analyzer/judge.py; server/analyzer/route.py classify; /api/sso/start server/analyzer/sso.py; CSA_REQUEST_WEBHOOK server/apiserver.py; client/update.go (this server only)",
    },
    {
      text: "The people who run the hosted server can read any workspace and can ask its clients to re-ship history. Self-host to remove that.",
      em: "",
      source: "csa.py org overview, org audit, org fetch; client/ship.go directives; docs/public-release/install-server.md",
    },
  ] as readonly (Claim & { em: string })[],
  rows: [
    { icon: "doc", tint: "decision", name: "Facts", sub: "Typed findings", what: "Decisions, gotchas, constraints, todos and preferences.", example: "Retry 429s with exponential backoff and jitter" },
    { icon: "quote", tint: "constraint", name: "Quotes", sub: "Proof from the session", what: "The exact sentence that proves the fact.", example: "\"Let's back off exponentially with jitter on 429s.\"" },
    { icon: "person", tint: "todo", name: "Shipper", sub: "Who shipped it", what: "The teammate whose session produced the fact.", example: "Mara Okafor", when: "Jun 3, 2024" },
    { icon: "folder", tint: "session", name: "Project", sub: "Where it happened", what: "The project folder the session ran in.", example: "api-gateway" },
    { icon: "calendar", tint: "pref", name: "Date", sub: "When it shipped", what: "When the session ended.", example: "Jun 3, 2024" },
    { icon: "lines", tint: "grey", name: "Transcript", sub: "Full session text", what: "The whole session, redacted before sending.", example: "none by default; your admin sets the days" },
    { icon: "prompt", tint: "rule", name: "Prompts", sub: "What sessions asked", what: "Each recall prompt, logged with what was served.", example: "how do we retry 429s" },
  ] as const,
  rowsSource: "hidden_context server/analyzer/store.py; sessions.user, project, ended_at server/analyzer/store.py; archive_days default 0 server/analyzer/tenant.py; tel.queries and tel.served server/analyzer/store.py; client/hook.go",
  flow: [
    { icon: "laptop", head: "Redacted", body: "on the developer's machine" },
    { icon: "cloud", head: "Extracted", body: "by the AI model you configure" },
    { icon: "server", head: "Stored", body: "in your workspace" },
    { icon: "loop", head: "Recalled", body: "into later sessions" },
  ] as const,
  foot: {
    text: "Delete your own sessions any time with `csa-client forget`. A deleted session takes its facts, quotes and transcript with it.",
    source: "/api/forget server/apiserver.py; archive.drop server/analyzer/archive.py; incremental.drop server/analyzer/incremental.py",
  },
} as const;

export const REQUEST = {
  eyebrow: "Request a workspace",
  head: { plain: "Request a workspace", em: "for your team." },
  intro: {
    text: "A person reads every request and, if it is approved, sends you the invite. Nothing here sends email.",
    source: "request_workspace server/analyzer/orgs.py; csa.py org requests, org approve",
  },
  title: "Request a workspace",
  companyLabel: "Company",
  companyPlaceholder: "Your company name",
  emailLabel: "Work email",
  emailPlaceholder: "you@company.com",
  emailHelp: "Only used to get back to you about this request.",
  submit: "Send request",
  note: "A person reviews every request and sends the invite.",
} as const;

// validRequest() answers from these, so the audit covers the error strings too.
export const FORM_ERRORS = {
  company: "Company is required.",
  companyLong: "That company name is too long.",
  email: "That doesn't look like an email address.",
  emailLong: "That email address is too long.",
} as const;

export const SUCCESS = {
  title: "Request sent.",
  bodyBefore: "A person will read it. If it is approved, they send an invite to",
  bodyAfter: "There is no account yet and nothing to check back on.",
  haveInvite: "Have an invite already?",
  signIn: "Sign in.",
} as const;

// The sign-in. What this server keeps is READ from /api/auth-config (archive_days): 0 stores no
// transcript, N keeps each for N days, a negative value keeps them until deleted (Deliverable 6),
// null means the server hosts several workspaces and cannot say which is yours before sign-in.
export const SIGNIN = {
  title: "Sign in",
  keepsUnknown: {
    text: "This workspace holds the facts and quotes from your team's sessions, readable by everyone who signs in. Your workspace admin sets what is kept of transcripts.",
    source: "/api/auth-config archive_days null on a multi-workspace server, /api/me archive_days server/apiserver.py; tenant_config.archive_days server/analyzer/tenant.py",
  },
  keepsNone: {
    text: "This workspace holds the facts and quotes from your team's sessions, readable by everyone who signs in. No transcripts are stored.",
    source: "/api/auth-config archive_days server/apiserver.py; retention_days server/analyzer/archive.py",
  },
  keepsDays: {
    text: "This workspace holds the facts and quotes from your team's sessions, readable by everyone who signs in. Transcripts are kept {n} days, for their owner and a workspace admin.",
    source: "/api/auth-config archive_days; may_read_transcript server/analyzer/gql.py (PR #103)",
  },
  keepsForever: {
    text: "This workspace holds the facts and quotes from your team's sessions, readable by everyone who signs in. Transcripts are kept until deleted, for their owner and a workspace admin.",
    source: "/api/auth-config archive_days; may_read_transcript and /api/forget admin path server/analyzer/gql.py, server/apiserver.py (PR #103)",
  },
  ssoOnly: {
    text: "Everyone who can read them signed in through your organisation's identity provider.",
    source: "sso_required server/analyzer/auth.py",
  },
  ssoButton: "Sign in with {name}",
  emailLabel: "Email",
  emailPlaceholder: "you@company.com",
  emailHelp: {
    text: "Labels what you do here, like the facts you flag. The invite code is what grants access.",
    source: "/api/ui-login server/apiserver.py",
  },
  inviteLabel: "Invite code",
  inviteHelp: "From whoever set up CSA for your team.",
  submit: "Continue",
  noInvite: "No invite code? Ask whoever set up CSA for your team.",
  noWorkspace: "Don't have a workspace yet?",
  request: "Request a workspace",
  invitedAs: "You have been invited as {email}.",
  invitedTo: "You have been invited to {org} as {email}.",
  joinStale: "This invite link is not usable. It may have expired, been revoked, or been meant for a different account. Ask your admin for a new one.",
  continueAs: "Continue as {email}",
} as const;

// --- the demo story: five sessions of an invented team, June 2024 ---------------------------------
// Fictional people and generic project names on purpose: this page is public. The recall edges tell
// one story: an early decision (f1) is recalled into three later sessions.

const S = (session_id: string, title: string, project: string, user: string, started_at: string): GraphData["sessions"][number] =>
  ({ session_id, title, project, user, started_at, n_facts: 0 });
const F = (ref: string, session_id: string, type: string, claim: string, evidence: string): GraphData["facts"][number] =>
  ({ ref, session_id, type, claim, evidence });

export const DEMO: GraphData = {
  sessions: [
    S("s1", "Retry policy for the payments API", "api-gateway", "Mara Okafor", "Jun 3, 2024"),
    S("s2", "Move billing resets to UTC", "billing", "Jules Renner", "Jun 6, 2024"),
    S("s3", "Stabilise the integration suite", "infra", "Tomasz Wiek", "Jun 11, 2024"),
    S("s4", "Streaming responses in the SDK", "sdk", "Priya Natarajan", "Jun 14, 2024"),
    S("s5", "Checkout page rate limits", "webapp", "Sam Achterberg", "Jun 20, 2024"),
  ],
  facts: [
    F("f1", "s1", "decision", "Retry 429s with exponential backoff and jitter.", "Let's back off exponentially with jitter on 429s instead of failing the call."),
    F("f2", "s1", "constraint", "The vendor key allows 100 requests a minute across all workers.", "The limit on this key is 100 per minute, shared by every worker."),
    F("f3", "s1", "gotcha", "The vendor returns 429 even at one request a second.", "We got 429s at roughly 1 rps, so request rate alone is not the trigger."),
    F("f4", "s1", "decision", "Cap retries at five attempts and 30 seconds total.", "Five attempts, 30 seconds max, then surface the error."),
    F("f5", "s1", "todo", "Alert when 429s stay above 1% for five minutes.", "We should alert if 429s hold above 1% for five minutes."),
    F("f6", "s1", "pref", "Keep the retry wrapper in one module.", "One wrapper, imported everywhere. No copy-pasted retry loops."),
    F("f7", "s1", "gotcha", "Log the vendor request id or a 429 cannot be traced.", "Without their request id support cannot find the call on their side."),
    F("f8", "s2", "decision", "Daily billing resets run at midnight UTC.", "Reset at 00:00 UTC, not local midnight."),
    F("f9", "s2", "gotcha", "Local midnight is ambiguous on DST change days.", "On DST switch days local midnight happens twice or not at all."),
    F("f10", "s2", "constraint", "Invoices keep the customer's local date.", "Finance needs the invoice to show the customer's own date."),
    F("f11", "s2", "todo", "Backfill June resets that ran at local time.", "Backfill the June rows that reset at local midnight."),
    F("f12", "s2", "pref", "Store UTC, format at the edge.", "UTC in the database, convert when rendering."),
    F("f13", "s3", "gotcha", "Hardcoded ports collide with services already bound in CI.", "The suite collides with whatever is already listening on 8080 in CI."),
    F("f14", "s3", "decision", "Bind test servers to port 0 and read the port back.", "Bind to 0 and read the port back. No fixed ports in tests."),
    F("f15", "s3", "constraint", "Tests must be reproducible across CI runs.", "A test that passes on retry is a failing test."),
    F("f16", "s3", "decision", "Fixed random seed per test file.", "Seed once per file so a failure can be replayed."),
    F("f17", "s3", "gotcha", "The backoff wrapper hides flaky mocks in tests.", "With the backoff wrapper on, a flaky mock looks green. Turn it off in tests."),
    F("f18", "s3", "todo", "Add coverage for streaming responses.", "We still do not assert chunk order or backpressure."),
    F("f19", "s3", "pref", "Keep fixture setup local to each test file.", "It is easier to reason about a test when its fixtures sit next to it."),
    F("f20", "s4", "decision", "Stream with a bounded 64 KB buffer.", "Bound the buffer at 64 KB and apply backpressure above it."),
    F("f21", "s4", "gotcha", "A retry after the first chunk duplicates output.", "If we retry after a chunk was yielded the caller sees it twice."),
    F("f22", "s4", "decision", "Retry only before the first byte is delivered.", "Retry is safe only before the first byte. After that, surface the error."),
    F("f23", "s4", "constraint", "The SDK must not import the gateway's retry wrapper.", "The SDK ships to customers. It cannot import gateway code."),
    F("f24", "s4", "todo", "Document the retry boundary in the SDK readme.", "Write down where retries stop, or someone will re-add them."),
    F("f25", "s4", "pref", "Tests bind to port 0 like the integration suite.", "Same trick as infra. Bind to 0."),
    F("f26", "s5", "decision", "Show a retry countdown instead of an error on 429.", "Show 'try again in 12s' rather than a red banner."),
    F("f27", "s5", "constraint", "Client retries stay inside the 100 per minute budget.", "The browser retry cannot push us over the shared 100 per minute."),
    F("f28", "s5", "gotcha", "Naive client retries stampede after a 429.", "Every open tab retried at the same second."),
    F("f29", "s5", "decision", "Client retry uses the gateway's backoff curve and jitter.", "Same curve as the gateway, jitter included, so tabs spread out."),
    F("f30", "s5", "todo", "Show the reset time in the customer's local zone.", "The reset is UTC midnight. Render it in their zone."),
  ],
  recalls: [
    { ref: "f1", session_id: "s3" }, { ref: "f1", session_id: "s4" }, { ref: "f1", session_id: "s5" },
    { ref: "f2", session_id: "s5" }, { ref: "f8", session_id: "s5" },
    { ref: "f14", session_id: "s4" }, { ref: "f15", session_id: "s4" },
  ],
};
for (const s of DEMO.sessions) s.n_facts = DEMO.facts.filter((f) => f.session_id === s.session_id).length;

/** The Observe facsimile's numbers for session s3: invented, like the rest of the story. */
export const DEMO_METRICS = { session_id: "s3", cost: "$4.82", toolCalls: "31", wasted: "6", duration: "48m" } as const;

/** The Improve facsimile's project rule: a finding repeated in 3 of 9 sessions, an instruction, never a score. */
export const DEMO_RULE = {
  project: "infra",
  rule: "settings.ts re-read in 3 of 9 sessions.",
  do: "Read it once, reuse it.",
  why: "This finding repeated across the project's sessions, so it is kept as a project rule.",
  eyebrow: "Project rule",
} as const;

/** How many words each section may spend. Counted over every visible string of the section, code
 *  literals included; a token counts only if it carries a letter or digit. */
export const BUDGETS: Record<string, number> = { hero: 55, observe: 70, discover: 60, improve: 60, stored: 310, request: 110, signin: 170 };

export function words(strings: string[]): number {
  let n = 0;
  for (const s of strings) n += s.split(/\s+/).filter((w) => /[a-z0-9]/i.test(w)).length;
  return n;
}

/** The visible strings of each section, in render order, for the budget test. */
export function sections(): Record<string, string[]> {
  const pillar = (p: Pillar) => [p.eyebrow, p.head.plain, p.head.em, p.body.text];
  return {
    hero: [HERO.name, HERO.line.text, HERO.cta, ...HERO.harnesses.map((h) => h.name), HERO.nextLabel, ...HERO.legend.map((l) => l[1]), HERO.legendEdge],
    observe: pillar(PILLARS[0]),
    discover: pillar(PILLARS[1]),
    improve: pillar(PILLARS[2]),
    stored: [STORED.eyebrow, STORED.head.plain, STORED.head.em, STORED.cardTitle,
      ...STORED.items.flatMap((i) => [i.text, i.em]),
      ...STORED.rows.flatMap((r) => [r.name, r.sub, r.what, r.example]),
      ...STORED.flow.flatMap((f) => [f.head, f.body]), STORED.foot.text],
    request: [REQUEST.eyebrow, REQUEST.head.plain, REQUEST.head.em, REQUEST.intro.text, REQUEST.title, REQUEST.companyLabel,
      REQUEST.emailLabel, REQUEST.emailHelp, REQUEST.submit],
    signin: [SIGNIN.title, META.description.text, SIGNIN.keepsNone.text, SIGNIN.ssoOnly.text, SIGNIN.ssoButton, SIGNIN.emailLabel, SIGNIN.emailHelp.text,
      SIGNIN.inviteLabel, SIGNIN.inviteHelp, SIGNIN.submit, SIGNIN.noInvite, SIGNIN.noWorkspace, SIGNIN.request],
  };
}

/** Every claim the page makes, each with its source, for the traceability test. */
export function claims(): Claim[] {
  return [
    META.description,
    HERO.line,
    { text: `Harnesses shown: ${HERO.harnesses.map((h) => h.name).join(", ")}`,
      source: "server/analyzer/harness.py parses claude-code and pi; Codex, Cursor and GitHub Copilot render greyed as next (HERO.harnesses[].next)" },
    ...PILLARS.map((p) => p.body),
    ...STORED.items,
    { text: STORED.rows.map((r) => `${r.name} ${r.sub} ${r.what} ${r.example}`).join(" "), source: STORED.rowsSource },
    ...STORED.items.filter((i) => i.em).map((i) => ({ text: i.em, source: i.source })),
    { text: STORED.flow.map((f) => `${f.head} ${f.body}`).join(". "), source: STORED.rowsSource + "; server/analyzer/llm.py; server/analyzer/providers.py; tel.served server/analyzer/store.py" },
    STORED.foot,
    REQUEST.intro,
    SIGNIN.keepsUnknown, SIGNIN.keepsNone, SIGNIN.keepsDays, SIGNIN.keepsForever, SIGNIN.ssoOnly, SIGNIN.emailHelp,
  ];
}

/** The form's pre-POST check. Returns the error to show, or null when the request may ship.
 *  Mirrors the wire contract's limits: company non-empty after trim and at most 120 chars; email must
 *  carry an "@" and stay within 254 chars. */
export function validRequest(company: string, email: string): string | null {
  const c = company.trim();
  const e = email.trim();
  if (!c) return FORM_ERRORS.company;
  if (c.length > 120) return FORM_ERRORS.companyLong;
  if (!e.includes("@")) return FORM_ERRORS.email;
  if (e.length > 254) return FORM_ERRORS.emailLong;
  return null;
}
