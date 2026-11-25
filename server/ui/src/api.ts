// Typed fetch client — the SINGLE SOURCE for API types (mirror apiserver.py exactly).
// NETWORK ONLY: no DOM, no rendering, no app logic here. See CONVENTIONS.md.
// All URLs are relative so the same code works behind the Vite dev proxy and the
// prod Python server (which serves ui/dist and answers /api itself).

export type Label = "gold" | "wrong" | "unimportant" | "clear"; // clear = remove your flag (back to neutral)

// GET /api/labels -> { gold:"boost", wrong:"hide", unimportant:"down-rank", clear:"back to neutral" }
export type Labels = Record<Label, string>;

// GET /api/sessions: the newest recall requests, the same rows Analytics counts as recall requests.
export interface RecallRow {
  at: string;
  session: string;          // "" when the request carried no session id
  mine: boolean;            // the reader asked it
  source: "hook" | "api" | "mcp"; // the channel, shown where the row has no session or hides it
  automatic: boolean;       // the prompt hook, not a deliberate ask
  question: string | null;  // null: a teammate's request that found nothing stays private
  results: number;          // 0 = an honest miss
}

export interface RecallLog {
  total: number;     // every recall request, all time
  answered: number;  // of those, the ones that returned at least one result
  as_of: string;     // the server's clock when it counted, ISO UTC
  rows: RecallRow[]; // the newest, at most a server cap
}

export type RecallFilter = "" | "answered" | "missed";

// A single served fact within a question.
export interface Fact {
  ref: string;
  kind: string;
  project: string;
  type: string | null;
  claim: string | null;
  evidence: string | null;
  label: Label | null;
  flaggable: boolean;
}

// GET /api/sessions/{session} -> QuestionEntry[]
export interface QuestionEntry {
  asked_at: string;
  question: string;
  facts: Fact[];
}

// POST /api/flag -> ok | error
export type FlagResult =
  | { ok: true; ref: string; label: Label }
  | { ok: false; error: string };

// --- auth ------------------------------------------------------------------
// The session lives in an httpOnly cookie (`__Host-csa_session`), so JS cannot read it and there is
// no "am I logged in?" to check locally — the only honest answer comes from the server. Every call
// below therefore turns a 401 into an AuthError, and main.ts renders the login view on one. Deliberate:
// no localStorage mirror of the auth state to drift out of sync with the cookie the browser holds.
export class AuthError extends Error {
  constructor() {
    super("Your session expired. Sign in again.");
    this.name = "AuthError";
  }
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return (await res.json()) as T;
}

export type LoginResult = { ok: true } | { ok: false; error: string };

// `workspace_requests` = the server announces workspace requests somewhere (its request webhook is
// set); the signed-out front door shows the landing and its request form only when true. Absent on
// an older server -> false -> the sign-in is the front door.
// `archive_days` is what the workspace keeps of transcripts (0 none, N days, negative until deleted);
// null when the server hosts several workspaces and cannot say which is yours before sign-in.
// `sso` is the identity provider this server signs in with (its display name and the route that
// starts the round trip), null when sign-in is by invite; `sso_required` says the invite paths are
// closed, which is the case whenever an issuer is configured.
export type AuthConfig = { sso: { name: string; start_url: string } | null; sso_required: boolean; workspace_requests: boolean;
                           archive_days: number | null;
                           // The base URL the installer bakes into itself; null when the server has
                           // not been told its public URL, in which case /install answers 421 and
                           // #/connect must not print a one-liner at all.
                           base_url: string | null;
                           // The date this server's client binaries landed on it (checksums.txt
                           // mtime), not an upstream publish date. null when it publishes none.
                           client_published: string | null;
                           // Who the invite link this browser is carrying names, and which workspace
                           // invited them, from the server's own read of the httpOnly join cookie.
                           // null when there is no live link.
                           join_email: string | null; join_org: string | null;
                           // This browser followed a link that no longer resolves.
                           join_stale: boolean };

// GET /api/auth-config -> which sign-in methods this server offers. Public by necessity: the login
// screen has to render before any credential exists. Falls back to the invite form if it can't be
// reached, so a blip shows a form that works rather than a blank screen — and to
// workspace_requests:false, so a server we can't read shows the sign-in, never a marketing page it
// may not want.
export async function authConfig(): Promise<AuthConfig> {
  try {
    const res = await fetch("/api/auth-config");
    const d = (await res.json()) as Partial<AuthConfig>;
    const sso = d.sso && d.sso.name && d.sso.start_url ? { name: String(d.sso.name), start_url: String(d.sso.start_url) } : null;
    return { sso, sso_required: !!d.sso_required,
             workspace_requests: !!d.workspace_requests,
             archive_days: d.archive_days == null ? null : Number(d.archive_days) || 0,
             base_url: d.base_url || null, client_published: d.client_published || null,
             join_email: d.join_email || null, join_org: d.join_org || null,
             join_stale: !!d.join_stale };
  } catch {
    return { sso: null, sso_required: false, workspace_requests: false, archive_days: null,
             base_url: null, client_published: null, join_email: null, join_org: null,
             join_stale: false };
  }
}

// POST /api/ui-login {invite, email} -> sets the httpOnly session cookie. The token is never in
// the body. NOT routed through getJSON on purpose: a wrong invite answers 401, and the AuthError
// path would re-render the login view and swallow the message — the user would see the form
// silently reset. An identity-provider sign-in never comes through here: it is a navigation to
// /api/sso/start and the callback sets the same cookie.
export async function login(invite: string, email: string): Promise<LoginResult> {
  let res: Response;
  try {
    res = await fetch("/api/ui-login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ invite, email }),
    });
  } catch {
    return { ok: false, error: "Can't reach the CSA server. Check your connection and try again." };
  }
  const d = (await res.json().catch(() => null)) as LoginResult | null;
  if (d && typeof d.ok === "boolean") return d;
  return { ok: false, error: "Something went wrong signing you in. Try again in a moment." };
}

export type WorkspaceRequestResult = { ok: true } | { ok: false; error: string };

// POST /api/workspace-request {company,email} — the landing page's only write, pre-auth by design
// (a stranger has no credential yet, so no cookie and no Bearer). Mirrors login(): returns, never
// throws, so the form renders whatever error string comes back instead of tripping AuthError
// machinery that would swallow it. A parsed {ok:boolean} body passes through; everything else —
// network failure, 413, a body that isn't JSON — collapses to one generic retry message.
export async function postWorkspaceRequest(company: string, email: string): Promise<WorkspaceRequestResult> {
  const generic = "Couldn't send the request. Try again in a moment.";
  let res: Response;
  try {
    res = await fetch("/api/workspace-request", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ company, email }),
    });
  } catch {
    return { ok: false, error: generic };
  }
  if (res.status === 429) {
    return { ok: false, error: "Too many requests from this network. Try again in ten minutes." };
  }
  if (res.status === 404) {   // a single-tenant server takes no workspace requests
    return { ok: false, error: "This server does not take workspace requests. Ask whoever runs it for an invite." };
  }
  const d = (await res.json().catch(() => null)) as { ok?: unknown; error?: unknown } | null;
  if (d && typeof d.ok === "boolean") {
    if (d.ok) return { ok: true };
    return { ok: false, error: typeof d.error === "string" && d.error ? d.error : generic };
  }
  return { ok: false, error: generic };
}

// POST /api/ui-logout -> revokes the token server-side AND clears the cookie. Never throws: the UI
// returns to the login screen regardless, and a failure here must not strand the user signed-in.
export async function logout(): Promise<void> {
  try {
    await fetch("/api/ui-logout", { method: "POST" });
  } catch {
    /* offline: the cookie may survive, but the login screen is still the right next view */
  }
}

export function getLabels(): Promise<Labels> {
  return getJSON<Labels>("/api/labels");
}

export function getSessions(result: RecallFilter = ""): Promise<RecallLog> {
  return getJSON<RecallLog>(result ? `/api/sessions?result=${result}` : "/api/sessions");
}

export function getSession(session: string): Promise<QuestionEntry[]> {
  return getJSON<QuestionEntry[]>(`/api/sessions/${encodeURIComponent(session)}`);
}

export async function flag(ref: string, label: Label, note = ""): Promise<FlagResult> {
  const res = await fetch("/api/flag", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ref, label, note }),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for /api/flag`);
  return (await res.json()) as FlagResult;
}

// --- entity types (team taxonomy: fixed defaults + this org's custom types) --
export interface EntityTypesData {
  default: string[];
  default_info?: Record<string, string>; // what each built-in covers — shown as chip tooltips
  custom: { key: string; description: string; updated_at?: string; updated_by?: string }[];
}

export function getEntityTypes(): Promise<EntityTypesData> {
  return getJSON<EntityTypesData>("/api/entity-types");
}

export type EntityTypeResult = {
  ok: boolean;
  key?: string;
  description?: string;
  created?: boolean; // false = an existing type was overwritten (typo'd-key edits become visible)
  applies_to?: string;
  error?: string;
};

// NOT routed through a throw-on-!ok helper: a bad key/description answers 400 with the reason in the
// body, and the form shows that reason inline instead of a generic error.
async function postEntityTypes(body: Record<string, unknown>): Promise<EntityTypeResult> {
  const res = await fetch("/api/entity-types", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new AuthError();
  return (await res.json()) as EntityTypeResult;
}

export function saveEntityType(key: string, description: string): Promise<EntityTypeResult> {
  return postEntityTypes({ key, description });
}

export function removeEntityType(key: string): Promise<EntityTypeResult> {
  return postEntityTypes({ key, delete: true });
}

// --- Corpus explorer (indexed sessions + their extracted content) ----------
// GET /api/corpus/stats -> CorpusStats
export interface CorpusStats {
  stages: {
    on_disk: number;
    ingested: number;
    extracted: number;
    covered: number;
    stale: number;
    missing: number;
    indexed: number;
    memories: number;
  };
  totals: { hidden_context: number; entities: number; relations: number; docs: number };
  feedback: {
    served_sessions: number;
    served_facts: number;
    flags: number;
    judge_labels: number; // machine labels (flagger='judge')
    judge_gold: number; // machine-gold signals
  };
  costs: {
    calls: number;
    usd: number;
    in_tokens: number;
    out_tokens: number;
    usd_24h: number;
    by_step: Record<string, number>;
  };
  stale_ids: string[]; // session_ids that are extracted-but-stale
}

// --- Live pipeline (in-flight ingests + last few finished) -------------------
// GET /api/pipeline -> PipelineData. Server-side in-memory only: a restart forgets
// history — the live view is about NOW. Polled by Home while it is the active view.
// "queued" is the normal first state now that ingest always queues and a separate ETL extracts.
export type PipelineStage = "received" | "queued" | "extracting" | "indexing" | "done" | "failed";

export interface PipelineItem {
  session_id: string;
  stage: PipelineStage;
  owner: string | null;
  project: string | null;
  elapsed_s: number; // live for in-flight; frozen at finish for recent
  error?: string; // only on stage === "failed"
  facts?: number; // only on stage === "done": what the session yielded
  entities?: number;
}

export interface PipelineData {
  inflight: PipelineItem[]; // newest first
  recent: PipelineItem[]; // last ~20 finished (done/failed), newest first
  extraction: { stalled: boolean; reason: string }; // the etl's last batch failed and sessions are waiting
}

export function getPipeline(): Promise<PipelineData> {
  return getJSON<PipelineData>("/api/pipeline");
}

// GET /api/corpus/sessions -> CorpusSessionSummary[]
export interface CorpusSessionSummary {
  session_id: string;
  project: string;
  git_branch: string | null;
  started_at: string | null;
  n_user: number;
  n_asst: number;
  deployed_to_prod: 0 | 1;
  hidden_context: number;
  entities: number;
  summary: string | null;
  user: string; // owner: Claude account email the session was ingested under
  cost_usd: number | null; // null = never measured, or not yours to see (the owner, an admin); never 0 for "unknown"
}

// A single extracted hidden-context fact (adapts to Fact for factCard/flagging).
export interface CorpusHiddenFact {
  claim: string;
  evidence: string;
  scope: string | null;
  ref: string;
  label: Label | null;
  used: number; // recalled into N later sessions
  dominant: 0 | 1; // about the entity this session was about
}

export interface CorpusEntity {
  id: string;
  type: string;
  name: string;
  attributes: string | null;
  label: string | null;
  served?: 0 | 1; // 0 = under the entity noise floor (one-off, not served to recall).
  // Absent (older server / CSA_ENTITY_FLOOR=off) = served — render exactly as before.
}

export interface CorpusRelation {
  source_id: string;
  rel: string;
  target_id: string;
  context: string;
}

export interface CorpusAction {
  verb: string;
  target_id: string;
  context: string;
}

export interface CorpusLink {
  url: string;
  title: string;
}

export interface CorpusSessionMeta {
  session_id: string;
  project: string;
  git_branch: string | null;
  started_at: string | null;
  ended_at: string | null;
  n_user: number;
  n_asst: number;
  summary: string;
  deployed_to_prod: 0 | 1;
  source: string;
  user: string; // owner: Claude account email the session was ingested under
}

export interface CorpusPipeline {
  extracted: boolean;
  stale: boolean;
  indexed: boolean;
  entities: number;
}

// null fields = never measured. The whole object is null when these numbers are not the caller's to
// see, which is the server's call, not the UI's.
export interface SessionMetrics {
  cost_usd: number | null;
  tool_calls: number | null;
  wasted: number | null; // repeated + re-read + failed calls
  wall_s: number | null;
  idle_s: number | null;
}

// GET /api/corpus/sessions/{id} -> CorpusSessionDetail
export interface CorpusSessionDetail {
  session: CorpusSessionMeta;
  pipeline: CorpusPipeline;
  worked_on: string[];
  hidden_context: Record<string, CorpusHiddenFact[]>;
  entities: CorpusEntity[];
  relations: CorpusRelation[];
  actions: CorpusAction[];
  links: CorpusLink[];
  data_sources: string[];
  metrics: SessionMetrics | null;
}

export function getCorpusStats(): Promise<CorpusStats> {
  return getJSON<CorpusStats>("/api/corpus/stats");
}

// `owner` scopes to one person's sessions (the dashboard's Mine/Everyone switch); "" = the whole org.
// `search` is the filter box, applied SERVER-side: it matches the owner, project, summary, branch or
// id, so it reaches sessions this page has not loaded rather than only the ones already in memory.
const CORPUS_Q = `query($owner: String, $q: String, $limit: Int!) {
  sessions(user: $owner, q: $q, limit: $limit) {
    items { sessionId project gitBranch startedAt nUser nAsst deployedToProd
            factCount entityCount summary user costUsd }
    totalCount hasMore
  }
}`;

export async function getCorpusSessions(owner = "", search = ""): Promise<CorpusSessionSummary[]> {
  const d = await graphql<{
    sessions: {
      items: {
        sessionId: string; project: string; gitBranch: string; startedAt: string;
        nUser: number; nAsst: number; deployedToProd: number; factCount: number;
        entityCount: number; summary: string; user: string; costUsd: number | null;
      }[];
    };
  }>(CORPUS_Q, { owner: owner || null, q: search || null, limit: 500 });
  return d.sessions.items.map((r) => ({
    session_id: r.sessionId, project: r.project, git_branch: r.gitBranch || null,
    started_at: r.startedAt || null, n_user: r.nUser, n_asst: r.nAsst,
    deployed_to_prod: (r.deployedToProd ? 1 : 0) as 0 | 1,
    hidden_context: r.factCount, entities: r.entityCount,
    summary: r.summary || null, user: r.user,
    cost_usd: r.costUsd ?? null, // ?? not ||: a measured $0.00 is a number, not "unknown"
  }));
}

// Session ids can contain "/" or ":" — encodeURIComponent keeps the path intact.
export function getCorpusSession(id: string): Promise<CorpusSessionDetail> {
  return getJSON<CorpusSessionDetail>(`/api/corpus/sessions/${encodeURIComponent(id)}`);
}

// --- GraphQL (analyzer/gql.py) ---------------------------------------------
// One POST route, query-only, behind the same gate as everything else. It exists for the two things
// REST was awkward at: a transcript is unbounded so it must page, and session search grows filters.
export async function graphql<T>(query: string, variables?: Record<string, unknown>): Promise<T> {
  const res = await fetch("/api/graphql", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query, variables: variables ?? {} }),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for /api/graphql`);
  const body = (await res.json()) as { data?: T; errors?: { message: string }[] };
  // GraphQL reports failure in a 200 body. Surfacing it as a thrown Error keeps every caller's
  // try/catch meaningful instead of handing them a `data: null` they have to re-check.
  if (body.errors?.length) throw new Error(body.errors.map((e) => e.message).join("; "));
  return body.data as T;
}

// One renderable piece of a session. Flat, because this is the unit that pages: a long assistant
// turn is many blocks. `messageIndex` is what regroups them for display.
export interface TranscriptBlock {
  index: number;
  messageIndex: number;
  role: "user" | "assistant";
  type: "text" | "thinking" | "tool_use" | "tool_result" | "image";
  text: string;
  toolName: string;
  toolId: string;
  isError: boolean;
  truncated: boolean;
  at: string;
  sidechain: boolean;
  meta: boolean;
}
export interface TranscriptPage {
  items: TranscriptBlock[];
  totalCount: number;
  hasMore: boolean;
  nextCursor: string | null;
  retentionDays: number;
  redactionMode: string;
  storedAt: string;
}

const TRANSCRIPT_Q = `query($id: String!, $limit: Int!, $cursor: String) {
  transcript(sessionId: $id, limit: $limit, cursor: $cursor) {
    items { index messageIndex role type text toolName toolId isError truncated sidechain meta }
    totalCount hasMore nextCursor retentionDays redactionMode storedAt
  }
}`;

// An empty page is the normal answer for: never retained, past its window, or not yours. The server
// answers all three identically on purpose, and so does this.
export async function getSessionTranscript(
  id: string,
  limit = 200,
  cursor: string | null = null,
): Promise<TranscriptPage | null> {
  try {
    const d = await graphql<{ transcript: TranscriptPage }>(TRANSCRIPT_Q, { id, limit, cursor });
    return d.transcript;
  } catch {
    return null; // an older server without /api/graphql, or a transport failure: render nothing
  }
}

// --- Onboarding: the first-run screen --------------------------------------
// GET /api/onboarding?project=&limit= -> OnboardingData. Behind the auth gate like every other
// corpus route: an unauthenticated version would have no credential, therefore no org, therefore
// would serve the operator's corpus to every visitor of every tenant.
export interface OnboardCard {
  ref: string;
  type: string;
  claim: string;
  evidence: string | null;
  project: string | null;
  user: string | null;
  learned_at: string;
  reused: number;      // sessions of someone other than its author this fact was served into; 0 = make NO reuse claim
  people: number;      // how many people those sessions belong to
  reused_in: string[]; // the ones the corpus holds (at most 5), so the claim is checkable rather than asserted
  gold: boolean;
  session_id: string;
}

export interface OnboardingData {
  frame: "warm" | "cold";  // cold = nothing reused yet, ranked oldest-first instead
  solo: boolean;           // 1 person in the corpus -> personal framing, not "this team"
  team: number;
  metrics: { facts: number; gotchas: number; sessions: number; people: number; agents: number; weeks: number; reused: number };
  mine: number;            // sessions the READER has shipped; 0 => every number here is someone else's
  scoped_to_you: boolean;  // true when the cards are teammates' lessons in THIS person's projects
  projects: string[];      // which projects those are, for the lede
  cards: OnboardCard[];
}

export function getOnboarding(project = "", limit = 3): Promise<OnboardingData> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (project) q.set("project", project);
  return getJSON<OnboardingData>(`/api/onboarding?${q}`);
}

// --- Activity: the standup view — one day, everyone, what landed ------------
// GET /api/activity?date=&owner= -> ActivityData. Carries WHAT was worked on only: projects,
// summaries, PRs, prod touches. Durations/costs deliberately live in Improve, private to their owner.
export interface DidRow {
  session_id: string;
  project: string | null;
  summary: string | null;
  branch: string | null;
  prod: number;
  at: string;    // HH:MM the session ended
  facts: number; // 0 = produced no knowledge; folded out of the standup by default
}

export interface PersonDay {
  user: string;
  sessions: number;
  trivial: number; // of `sessions`, how many produced zero facts
  projects: [string, number][];
  prod: number;
  prs: { tag: string; url: string }[];
  did: DidRow[];
}

export interface ActivityData {
  date: string | null;
  days: string[]; // every day that has sessions, newest first — drives prev/next
  people: PersonDay[];
  quiet: { user: string; last: string }[];   // in the roster but no session that day, + last seen
  filters: { project: string; branch: string; owner: string };   // echoed back, so pills cannot lie
  page: { limit: number; offset: number; total: number; returned: number };
  totals: { people: number; team: number; sessions: number; prod: number; prs: number; pr_credits: number };
}

// Filters and paging are real query params, so any client (curl, a script, a future mobile view) gets
// the same narrowing the UI does — and a big day is never shipped whole.
export function getActivity(
  date = "", owner = "",
  opts: { project?: string; branch?: string; limit?: string; offset?: string } = {},
): Promise<ActivityData> {
  const q = new URLSearchParams();
  if (date) q.set("date", date);
  if (owner) q.set("owner", owner);
  for (const k of ["project", "branch", "limit", "offset"] as const) {
    const v = opts[k];
    if (v) q.set(k, v);
  }
  const qs = q.toString();
  return getJSON<ActivityData>(`/api/activity${qs ? `?${qs}` : ""}`);
}

// --- Self improvement (per-session efficiency + findings) --------------------
// GET /api/improve?owner=&limit= -> ImproveData. Metrics are counted at ingest from the raw
// transcript, so rows exist only for sessions ingested since that shipped.
// A finding carries its own short chain of thought: the evidence, the inference, the instruction.
// `rule` is the stable id the project rollup groups on (headlines carry per-session numbers).
export interface Finding {
  kind: "cost" | "turns" | "time";
  rule: string;
  headline: string;
  saw: string;   // what was counted — checkable against the session
  why: string;   // the one-line inference from it
  do: string;    // the instruction, addressed to whoever works on this next
}

export interface ProjectLesson {
  rule: string;
  kind: Finding["kind"];
  do: string;
  why: string;
  sessions: number;      // how many sessions in this project hit it
  reviewed: number;      // ...out of how many reviewed — a bare "3x" is not a weighable claim
  share: number;
  session_ids: string[];
}

export interface ProjectRollup {
  project: string;
  sessions: number;
  lessons: ProjectLesson[];
}

export interface ImproveRow {
  session_id: string;
  project: string | null;
  user: string | null;
  summary: string | null;
  ended_at: string | null;
  model: string | null;
  tool_calls: number;
  tool_errors: number;
  dup_calls: number;
  reread_calls: number;
  top_read: string | null;
  top_read_n: number;
  top_tool: string | null;
  in_tokens: number;
  out_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  wall_s: number;
  idle_s: number;
  max_gap_s: number;
  n_user: number;
  n_asst: number;
  settled: boolean; // false = still open; findings are withheld until it has been quiet
  findings: Finding[];
}

export interface ImproveData {
  settle_minutes: number;
  scoped_to: string;   // the owner the server pinned this to ("" = the whole deployment)
  can_widen: boolean;  // only the admin credential may look at another person's numbers
  totals: { sessions: number; cost_usd: number; tool_calls: number; wasted_calls: number };
  projects: ProjectRollup[];
  rows: ImproveRow[];
}

export function getImprovements(owner = ""): Promise<ImproveData> {
  const q = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  return getJSON<ImproveData>(`/api/improve${q}`);
}

// --- Identity + team -----------------------------------------------------------
// GET /api/me -> WHO IS ASKING: the signed-in email, verified server-side from the session cookie's
// per-user token (never a value this client sent). Falls back to the server machine's own Claude
// account only when the gate has no identity — a shared admin token, or loopback dev with auth off.
// The gate's server-VERIFIED principal — who is signed in, not the server box's own Claude account.
// `email` is null when the gate authenticated a credential that names nobody (a shared admin token).
export interface Me {
  email: string | null;
  name: string | null;
  account?: string | null;
  org_id: string;
  org_name?: string;
  // Who you are inside YOUR OWN workspace: "admin" manages members/invite/taxonomy. There is no
  // second axis: no credential a browser holds reaches more than one workspace.
  role?: "admin" | "member";
  verified: boolean; // the gate named you (a per-user token carries an email); not "the email was proven"
  // Which sign-in minted this session's token: "web" = the invite form (a typed email, a
  // member unless it is the chart's adminEmail arriving by its link), "web-sso" = the identity provider, else the machine that ran `csa-client setup`.
  hostname?: string;
  // What YOUR workspace keeps of transcripts: 0 none, N days, negative until deleted.
  archive_days: number;
  model_ready?: boolean; // the main model has something to call: a provider or the install credential
}

// GET /api/team -> per-owner rollup
export interface TeamRow {
  user: string;
  sessions: number;
  first_seen: string | null;
  last_active: string | null;
  facts: number;
  entities: number;
  agent: boolean;  // the owner only ever held a service token: an agent, not a person
}

export function getMe(): Promise<Me> {
  return getJSON<Me>("/api/me");
}

export function getTeam(): Promise<TeamRow[]> {
  return getJSON<TeamRow[]>("/api/team");
}

// --- Flags ledger (every human flag ever given) ------------------------------
// GET /api/flags -> FlagsData
export interface FlagRow {
  kind: string; // "hc" | "entity"
  ref: string;
  label: string; // usually gold|wrong|unimportant; older rows may carry other statuses
  note: string | null;
  claim: string | null;
  type: string | null;
  session_id: string | null;
  project: string | null;
  user: string | null;
}

export interface FlagsData {
  total: number;
  counts: Record<string, number>;
  rows: FlagRow[];
}

export function getFlags(): Promise<FlagsData> {
  return getJSON<FlagsData>("/api/flags");
}

// --- Facts (searchable corpus-wide fact browser) -----------------------------
// Served by the `facts` GraphQL query (analyzer/gql.py). The shape below is the one the views
// have always spoken; getFacts maps the camelCase response back into it.
export interface FactRow {
  ref: string;
  type: string;
  claim: string;
  evidence: string | null;
  project: string | null;
  session_id: string;
  user: string | null;
  started_at: string | null;
  label: string | null;
  flagger: string | null; // who set the label — "judge" = the haiku auto-labeler; else human/MCP
  corroboration: number; // extracted independently from N sessions
  used: number; // recalled into N later sessions
  dominant: 0 | 1; // about the entity this session was about
  judge_gold: 0 | 1; // the haiku judge rated it gold (capped boost until a human confirms)
  // --- schema v2 (facts table) — optional so old UI bundles keep working against old servers ---
  author?: string | null; // FIRST asserter (facts.author) — distinct from `user` (a mention row's owner)
  domain?: string | null; // code-home repo when known, else the first session's project
  created_at?: string | null; // system time: first extraction
  valid_at?: string | null; // world time: when it became true (as_of can back-date it)
  invalid_at?: string | null; // world time: when it stopped being true; null = current
  state?: string | null; // extracted|verified|unverifiable|current|superseded|conflict
  cluster_id?: string | null; // paraphrase cluster — supersession must close whole clusters (sibling task)
  superseded_by?: string | null; // winning fact's ref (fact_edges rel='supersedes'), if any
  superseded_at?: string | null; // when the supersession was recorded
}

export interface FactsPage {
  // which query terms the corpus has never seen ("missing"), and which are rare enough to carry real
  // topical signal. Lets the UI admit a miss instead of ranking on the common words alone.
  coverage?: { missing: string[]; distinctive: string[]; terms: number };
  total: number;
  offset: number;
  limit: number;
  rows: FactRow[];
  // type "rule" rows: the first unsearched page only, counted apart from `total`, never flaggable.
  rules: FactRow[];
}

export interface FactsParams {
  q?: string;
  type?: string;
  label?: string;
  owner?: string;
  project?: string;
  offset: number;
  limit: number;
}

// Every field the Memory page renders. Selecting a SUBSET would look like a working search and
// quietly blank the labels, the supersession banner and the corroboration counts — the parts that
// make a fact trustworthy rather than merely true-looking.
const FACT_FIELDS = `ref type claim evidence project sessionId user startedAt label flagger
  corroboration used dominant judgeGold author domain createdAt validAt invalidAt state
  clusterId supersededBy supersededAt`;

const FACTS_Q = `query($q: String, $type: String, $label: String, $owner: String, $project: String,
                       $limit: Int!, $cursor: String) {
  facts(q: $q, type: $type, label: $label, owner: $owner, project: $project,
        limit: $limit, cursor: $cursor) {
    items { ${FACT_FIELDS} }
    rules { ${FACT_FIELDS} }
    totalCount hasMore nextCursor
    coverage { missing distinctive terms }
  }
}`;

interface GqlFact {
  ref: string; type: string; claim: string; evidence: string | null; project: string | null;
  sessionId: string; user: string | null; startedAt: string | null; label: string | null;
  flagger: string | null; corroboration: number; used: number; dominant: number; judgeGold: number;
  author: string | null; domain: string | null; createdAt: string | null; validAt: string | null;
  invalidAt: string | null; state: string | null; clusterId: string | null;
  supersededBy: string | null; supersededAt: string | null;
}

// GraphQL is camelCase; the rest of this app speaks the snake_case the REST routes returned. Mapping
// HERE rather than renaming 23 fields across the views keeps the transport change a transport change.
function toFactRow(f: GqlFact): FactRow {
  return {
    ref: f.ref, type: f.type, claim: f.claim, evidence: f.evidence, project: f.project,
    session_id: f.sessionId, user: f.user, started_at: f.startedAt, label: f.label,
    flagger: f.flagger, corroboration: f.corroboration, used: f.used,
    dominant: (f.dominant ? 1 : 0) as 0 | 1, judge_gold: (f.judgeGold ? 1 : 0) as 0 | 1,
    author: f.author, domain: f.domain, created_at: f.createdAt, valid_at: f.validAt,
    invalid_at: f.invalidAt, state: f.state, cluster_id: f.clusterId,
    superseded_by: f.supersededBy, superseded_at: f.supersededAt,
  };
}

// The global search box and the Memory page both land here. `offset` is kept in the parameter shape
// because the views page by offset; it is converted to the opaque cursor the server actually takes,
// so the day this moves to keyset the views do not change.
export async function getFacts(p: FactsParams): Promise<FactsPage> {
  const d = await graphql<{
    facts: {
      items: GqlFact[]; rules: GqlFact[]; totalCount: number; hasMore: boolean; nextCursor: string | null;
      coverage: { missing: string[]; distinctive: string[]; terms: number } | null;
    };
  }>(FACTS_Q, {
    q: p.q || null, type: p.type || null, label: p.label || null,
    owner: p.owner || null, project: p.project || null,
    limit: p.limit, cursor: p.offset ? offsetCursor(p.offset) : null,
  });
  return {
    total: d.facts.totalCount,
    offset: p.offset,
    limit: p.limit,
    rows: d.facts.items.map(toFactRow),
    rules: d.facts.rules.map(toFactRow),
    coverage: d.facts.coverage ?? undefined,
  };
}

// --- GET /api/graph?owner=&days=&limit= -> GraphResponse --------------------------
// `GraphData` is also what the landing feeds graph.ts as a fixed demo story, so it stays free of the
// `window` the server adds.
export interface GraphSession {
  session_id: string;
  title: string;
  project: string;
  user: string;
  started_at: string;
  n_facts: number;
}
export interface GraphFact {
  ref: string;
  type: string;
  claim: string;
  session_id: string;
  evidence?: string;
  recalled?: number; // later sessions that recalled it, in this window and scope or not
}
export interface GraphRecall {
  ref: string;
  session_id: string;
  at?: string;
}
export interface GraphData {
  sessions: GraphSession[];
  facts: GraphFact[];
  recalls: GraphRecall[];
}
// What came back against what matched: the newest `limit` sessions active in the last `days` days, at
// most `facts_budget` facts between them, out of `total` before the caps.
export interface GraphWindow {
  days: number;
  limit: number;
  facts_budget: number;
  since: string;
  total: number;
}
export type GraphResponse = GraphData & { window: GraphWindow };

export function getGraph(owner = "", days = 30, limit = 60): Promise<GraphResponse> {
  const q = new URLSearchParams({ days: String(days), limit: String(limit) });
  if (owner) q.set("owner", owner);
  return getJSON<GraphResponse>(`/api/graph?${q}`);
}

// The server's cursor is base64 of {"o":<offset>} (analyzer/pagination.py). Building one here is the
// single place that knows the encoding, and it exists only to bridge the offset-paging views; a
// caller that already HAS a nextCursor should pass that through instead.
function offsetCursor(offset: number): string {
  return btoa(JSON.stringify({ o: offset })).replace(/=+$/, "");
}


// --- Questions (HITL review queue) -----------------------------------------
// A fact awaiting human review. Richer than the served Fact (has scope/confidence/
// session_id/type_confirmed); adapt to Fact for factCard reuse in main.ts.
export interface QuestionFact {
  ref: string;
  type: string;
  scope: string | null;
  claim: string;
  evidence: string;
  confidence: string | null;
  project: string;
  session_id: string;
  label: Label | null;
  flagger: string | null; // "judge" = machine label; humans arbitrate/override
  judge_gold: 0 | 1;
  type_confirmed: boolean;
}

// GET /api/questions -> QuestionsData
export interface QuestionsData {
  types: string[];
  labels: Labels;
  counts: { uncertain_extraction: number; uncertain_labels: number; certain: number };
  uncertain_extraction: QuestionFact[];
  uncertain_labels: QuestionFact[];
  certain: QuestionFact[];
  today: FactRow[]; // top-5 leverage-sorted unlabeled facts recall recently used
  receipts: {
    your_labels: number;
    labeled_served: number;
    used_this_corpus: number;
    judge_labels: number; // machine labels (reviews with flagger='judge')
    judge_gold: number; // machine-gold signals awaiting human confirmation
  };
}

export function getQuestions(): Promise<QuestionsData> {
  return getJSON<QuestionsData>("/api/questions");
}

// --- the TENANT-ADMIN surface (/api/org/*) -------------------------------------
// A customer managing their OWN workspace: these ride the ordinary session cookie, carry no secret,
// and the server scopes them to the credential's own org (no org_id is sent, because none is
// accepted). Role and revoke work from a verified admin's session; rotating the invite is bearer-only
// (its response carries the new invite in plaintext), so the page only shows the invite's status.
// GET /api/machines -> the machines the CALLER's own credential names. Scoped to them by the server,
// so a member sees their own laptop without the admin-only Team columns.
export interface Machine {
  hostname: string;
  created: string | null;
  last_used_at: string | null;
}

export async function getMachines(): Promise<Machine[]> {
  return (await getJSON<{ machines: Machine[] }>("/api/machines")).machines;
}

// GET /latest/checksums.txt -> the sha256 lines as published, parsed by connect.ts. Public and served
// by the same handler the installer downloads from, so what the page shows and what install.sh
// verifies against cannot drift. "" when the server publishes none.
export async function getChecksums(): Promise<string> {
  try {
    const res = await fetch("/latest/checksums.txt");
    return res.ok ? await res.text() : "";
  } catch {
    return "";
  }
}

export interface OrgMember {
  token_hash: string;
  email: string | null;
  hostname: string | null;
  created: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  role: "admin" | "member";
  active: boolean;
  service: boolean; // a Team > Service token: never takes a role
}
export async function getOrgMembers(): Promise<{ org_id: string; members: OrgMember[]; okta_admin_group?: string }> {
  return getJSON("/api/org/members");
}



// --- PR 3 (redesign): the entity browser and the Team page's admin calls ----------------------

// GET /api/entities -> one row per CANONICAL entity (aliases rolled up), counted live.
export interface EntityRow {
  id: string;
  name: string;
  type: string; // "" when the canonical id has no entities row
  mentions: number; // mention rows rolled up to the canonical id
  sessions: number; // distinct sessions among them
  facts: number; // facts extracted from those sessions, not facts about the entity
  last_seen: string | null;
  served: 0 | 1; // the noise gate's verdict (0 = a one-off not served to recall); 1 when the floor is off
}

export interface EntitiesPage {
  total: number;
  offset: number;
  limit: number;
  rows: EntityRow[];
}

export interface EntitiesParams {
  q: string;
  type: string;
  owner: string;
  served: boolean; // true = only entities the gate serves
  sort: string; // sessions | facts | last_seen | name
  dir: "asc" | "desc";
  offset: number;
  limit: number;
}

export function getEntities(p: EntitiesParams): Promise<EntitiesPage> {
  const qs = new URLSearchParams({ q: p.q, type: p.type, owner: p.owner, served: p.served ? "1" : "",
    sort: p.sort, dir: p.dir, offset: String(p.offset), limit: String(p.limit) });
  return getJSON<EntitiesPage>(`/api/entities?${qs}`);
}

// GET /api/entities/{id} -> the entity (by any alias id), the sessions that mention it, the facts
// those sessions produced. null when the id is unknown here.
export interface EntityDetail {
  entity: { id: string; name: string; type: string; attributes: string | null; aliases: { id: string; name: string | null }[] };
  sessions: { session_id: string; project: string | null; user: string | null; started_at: string | null; summary: string | null; facts: number }[];
  facts: FactRow[];
}

export async function getEntity(id: string): Promise<EntityDetail | null> {
  const res = await fetch(`/api/entities/${encodeURIComponent(id)}`);
  if (res.status === 401) throw new AuthError();
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for /api/entities/${id}`);
  return (await res.json()) as EntityDetail;
}

// POST /api/org/member/role and /api/org/member/revoke from the Team page. A refusal comes back with
// the server's reason (last_admin, self_revoke, forbidden) for the row to show; never thrown.
export type OrgResult = { ok: true } | { ok: false; code?: string; error: string };

async function postOrg(path: string, body: Record<string, string>): Promise<OrgResult> {
  const res = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (res.status === 401) throw new AuthError();
  const data = (await res.json()) as { ok?: boolean; code?: string; error?: string };
  return data.ok ? { ok: true } : { ok: false, code: data.code, error: data.error ?? `${res.status} ${res.statusText}` };
}

export function setMemberRole(email: string, role: "admin" | "member"): Promise<OrgResult> {
  return postOrg("/api/org/member/role", { email, role });
}

export function revokeMember(tokenHash: string): Promise<OrgResult> {
  return postOrg("/api/org/member/revoke", { token_hash: tokenHash });
}

// A per-person invite LINK. `used_at` is a FIRST-use stamp and never a refusal: the same person
// redeems the link again for their second machine.
export interface InviteLink {
  id: string;
  email: string;
  created_by: string | null;
  created_at: string | null;
  expires_at: string | null;
  used_at: string | null;
}

export function getInviteLinks(): Promise<{ org_id: string; invites: InviteLink[]; max_pending: number }> {
  return getJSON("/api/org/invites");
}

// The link comes back ONCE; only its hash is stored. `link` is absent on a refusal.
export type NewInvite = { ok: true; link: string; invite: InviteLink; invites: InviteLink[] }
                      | { ok: false; error: string };

// NOT through postOrg: that helper throws away the body, and the whole point of this call is the one
// value the server will never repeat.
export async function createInviteLink(email: string, ttl_days: number): Promise<NewInvite> {
  const res = await fetch("/api/org/invite/new", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, ttl_days }),
  });
  if (res.status === 401) throw new AuthError();
  const d = (await res.json().catch(() => null)) as (NewInvite & { error?: string }) | null;
  if (d && d.ok) return d;
  return { ok: false, error: d?.error ?? `${res.status} ${res.statusText}` };
}

// A token for a machine that cannot sign in. It comes back ONCE; only its hash is stored. The machine
// then shows under Team by its name, revoked like any other.
export type NewServiceToken = { ok: true; token: string; email: string; name: string } | { ok: false; error: string };

export async function createServiceToken(name: string, email: string): Promise<NewServiceToken> {
  const res = await fetch("/api/org/service-token/new", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, email }),
  });
  if (res.status === 401) throw new AuthError();
  const d = (await res.json().catch(() => null)) as (NewServiceToken & { error?: string }) | null;
  if (d && d.ok) return d;
  return { ok: false, error: d?.error ?? `${res.status} ${res.statusText}` };
}

export function revokeInviteLink(id: string): Promise<OrgResult> {
  return postOrg("/api/org/invite/revoke", { id });
}

// --- usage events + workspace health (the Admin page) --------------------------------------------
export type TrackEvent = "page_view" | "search" | "scope" | "flag" | "session_open" | "entity_open";

// Fire and forget: a usage event never blocks a render and never surfaces a failure. The server
// credits the signed-in identity from the cookie; nothing here names a person.
export function track(event: TrackEvent, page: string, meta?: Record<string, string | number>): void {
  try {
    void fetch("/api/track", {
      method: "POST",
      keepalive: true,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(meta ? { event, page, meta } : { event, page }),
    }).catch(() => undefined);
  } catch {
    // a blocked fetch (an extension, a sandbox) is not the reader's problem
  }
}

// 403: signed in, but not this workspace's admin. Distinct from AuthError so the page can say so
// instead of bouncing to the sign-in.
export class ForbiddenError extends Error {
  constructor() {
    super("This page is for workspace admins.");
    this.name = "ForbiddenError";
  }
}

export type AnalyticsWeeks = 4 | 12 | 26;
export interface SilentMachine { user: string; hostname: string; last_used_at: string; expired: boolean }

// GET /api/workspace/analytics?weeks=4|12|26 (admin only; a member gets 403). Every `per_week` array
// has one entry per `week_starts` day, oldest first; `*_prev` is the same count for the window before.
export interface WorkspaceAnalytics {
  ok: true;
  as_of: string;   // the server's clock when it counted, ISO UTC
  window: { weeks: AnalyticsWeeks; since: string; until: string; previous_since: string };
  week_starts: string[];
  adoption: {
    members: number; members_shipping: number; members_quiet: number; members_unconnected: number;
    shipped_without_credential: number;   // shipped a session, all time, but holds no live member credential
    contributors: number; contributors_prev: number;   // people only: agents are counted apart
    agents: number; agent_sessions: number;            // service-token identities that shipped, and their sessions
    sessions: number; sessions_prev: number;
    per_week: { sessions: number; people: number }[];
    silent: SilentMachine[];
  };
  memory: {
    total: number; new: number; new_prev: number;
    types: string[];
    per_week: number[][];     // per week, one count per entry of `types`
    contributors: { user: string; facts: number; reached: number; agent: boolean }[];
  };
  recall: {
    asks: number; asks_prev: number; answered: number; answered_prev: number; acted: number;
    measured: number;                              // answered requests from sessions CSA holds: acted on's denominator
    ambient: { asks: number; answered: number };   // the prompt hook
    asked: { asks: number; answered: number };     // MCP ask, the csa-recall skill
    per_week: { asks: number; answered: number; acted: number }[];
    transfer: { facts: number; facts_prev: number; facts_all_time: number; deliveries: number;
                receivers: number; receiving_agents: number; contributors: number; per_week: number[] };
    top_reused: { ref: string; claim: string | null; type: string | null; author: string | null; people: number;
                  sessions: number; last_served: string; session_id: string | null }[];
  };
  pipeline: {
    backlog: number; poisoned: number; stalled: boolean; reason: string;
    extracted: number; spend_usd: number; spend_prev: number;
    tokens: number; unpriced: boolean;               // unpriced: a provider reported tokens but no cost
    per_extracted: number | null; per_contributor: number | null;
    daily_cap_usd: number; days_over_cap: number;   // cap 0 = no cap
    spend_per_day: { day: string; usd: number; tokens: number }[];
  };
  web: { people: number; searches: number; session_opens: number; flags: number; pages: { page: string; n: number }[] };
}

export async function getWorkspaceAnalytics(weeks: AnalyticsWeeks): Promise<WorkspaceAnalytics> {
  const res = await fetch(`/api/workspace/analytics?weeks=${weeks}`);
  if (res.status === 401) throw new AuthError();
  if (res.status === 403) throw new ForbiddenError();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for /api/workspace/analytics`);
  return (await res.json()) as WorkspaceAnalytics;
}


// --- AI Models (admin): the providers registered on this server and the model each role runs on ---
export type ProviderKind = "anthropic" | "bedrock" | "vertex" | "openai" | "azure_openai";

export interface ProviderInfo {
  id: string;
  name: string;
  kind: ProviderKind;
  kind_label: string;
  endpoint: string;
  region: string;
  project: string;
  api_version: string;
  credential_set: boolean;   // the key itself is never sent to the browser
  models: string[];
  updated_at: string | null;
}

export interface ProvidersView {
  kinds: Record<ProviderKind, string>;
  editable: boolean;         // false on a server hosting several workspaces
  providers: ProviderInfo[];
  main_model: string;        // "<provider id>/<model>", "" = the environment's Claude credential
  small_model: string;
  default: string;           // one line on what runs when nothing is picked
  installed: string;         // the Claude credential the install carries and can run, in words; "" = none
  blocked: string;           // why a role cannot run on this image, with the fix; "" = nothing blocked
  unavailable_kinds: ProviderKind[];   // provider types this image cannot run
}

export interface ProviderForm {
  id?: string;
  kind: ProviderKind;
  name: string;
  endpoint?: string;
  region?: string;
  project?: string;
  api_version?: string;
  credential?: string;       // blank on an edit keeps the stored key
  models: string[];
}

export type ProvidersResult = ({ ok: true } & ProvidersView) | { ok: false; error: string };

async function providersCall(path: string, body?: unknown): Promise<ProvidersResult> {
  const res = await fetch(path, body === undefined ? undefined
    : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (res.status === 401) throw new AuthError();
  if (res.status === 403 && body === undefined) throw new ForbiddenError();
  const d = (await res.json().catch(() => null)) as (ProvidersView & { ok?: boolean; error?: string }) | null;
  if (d && d.ok) return { ...d, ok: true };
  return { ok: false, error: d?.error ?? `${res.status} ${res.statusText}` };
}

export const getProviders = () => providersCall("/api/org/providers");
export const saveProvider = (form: ProviderForm) => providersCall("/api/org/provider/save", form);
export const removeProvider = (id: string) => providersCall("/api/org/provider/remove", { id });
export const saveModels = (models: { main_model?: string; small_model?: string }) => providersCall("/api/org/models", models);
