// App logic + rendering. DOM ONLY here; all network lives in api.ts. See CONVENTIONS.md.
// View switching via the URL hash (#/ = list, #/session/<id> = detail) — no router lib.
import "./style.css";
import "./release.css";
import { ROUTE_ALIASES, bareArrivalHash, corpusSessionRoute, decodeId, frontDoor, sessionHref, signedOutView, ssoHref } from "./routes";
import { splitServed } from "./entities";
import { LABEL_TEXT, RECALL_FILTERS, RECALL_LINE, RECALL_SOURCE, REUSED_TILE, foldSameClaim, labelText, recallLogLine, reuseClaim, seenLine } from "./memory";
import { initialOf, teamCard } from "./taxonomy";
import { INVITE, SERVICE, inviteError, inviteLine, machineLabel, serviceTokenError, servicePaste, teamCountLine, teamCounts, teamPeople, waiting, type TeamPerson } from "./team";
import { ASSETS, CONNECT, FIRST_RUN, firstRunSteps, installLine, invitePaste, macSteps, machineLine, modelReady, parseChecksums, setupLine, versionLine, winSetupLine } from "./connect";
import { DEMO, DEMO_METRICS, DEMO_RULE, HERO, META, NAV, PILLARS, type Pillar, REQUEST, SIGNIN, STORED, SUCCESS, validRequest } from "./landing";
import { GraphView, cssColors, type GNode } from "./graph";
import { fmtCost, fmtCount, fmtDur, GRAPH_WINDOWS, graphWindow, PROD_TIP, SESSION_COST, SESSION_COST_DEF, shortDate, windowLabel } from "./sessions";
import {
  AuthError,
  authConfig,
  flag,
  getCorpusSession,
  getSessionTranscript,
  type TranscriptPage,
  type TranscriptBlock,
  getCorpusSessions,
  getCorpusStats,
  getFacts,
  getFlags,
  getGraph,
  type GraphData,
  type GraphResponse,
  type SessionMetrics,
  getLabels,
  getActivity,
  getOnboarding,
  getImprovements,
  getMe,
  getPipeline,
  getQuestions,
  getSession,
  getSessions,
  getTeam,
  getOrgMembers,
  getEntityTypes,
  saveEntityType,
  removeEntityType,
  getEntities,
  getEntity,
  getInviteLinks,
  createInviteLink,
  createServiceToken,
  revokeInviteLink,
  type InviteLink,
  type RecallFilter,
  type NewInvite,
  getChecksums,
  getMachines,
  type Machine,
  setMemberRole,
  revokeMember,
  type EntitiesPage,
  type EntityRow,
  type EntityDetail,
  login,
  logout,
  postWorkspaceRequest,
  type OrgMember,
  type CorpusAction,
  type CorpusEntity,
  type CorpusHiddenFact,
  type CorpusLink,
  type CorpusRelation,
  type CorpusSessionDetail,
  type CorpusSessionSummary,
  type CorpusStats,
  type ActivityData,
  type OnboardCard,
  type Fact,
  type Finding,
  type ImproveRow,
  type ProjectRollup,
  type FactRow,
  type FlagRow,
  type Label,
  type Labels,
  type Me,
  type PipelineData,
  type PipelineItem,
  type PipelineStage,
  type QuestionEntry,
  type QuestionsData,
  type EntityTypesData,
  type TeamRow,
} from "./api";
import { ForbiddenError, getWorkspaceAnalytics, track, type AnalyticsWeeks, type WorkspaceAnalytics } from "./api";
import { getProviders, removeProvider, saveModels, saveProvider, type ProviderForm, type ProviderInfo, type ProviderKind, type ProvidersView } from "./api";
import { KIND_FIELDS, MODELS_HINT, PROVIDER_META, WIZARD_STEPS, kindLabel, kindNote, modelOptions, otherVendor, parseModels, providerLine, type WizardStep } from "./models";
import { CSA_SPEND, DEFS, MACHINE_WAITING, UNPRICED, WINDOWS, actedTile, ago, areaPath, asOf, contributorsNote, count, deltaLine, fmtInt, fmtUsd, fmtWhen, labelIndexes, linePath, membersLine, pageName, pct, peopleLine, reuseNote, shortDay, stackTotals, stallNotice, ticks, webUse, weekLabels, yOf } from "./admin";

const LABELS: Label[] = ["gold", "wrong", "unimportant", "clear"]; // clear = unflag, back to neutral
const app = document.getElementById("app")!;
let labelLegend: Labels | null = null; // fetched once, reused across views
let me: Me | null = null; // signed-in identity, fetched once at boot

// --- tiny DOM helpers (our whole "framework") ------------------------------
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
  ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

// el() cannot make SVG: createElement builds HTML-namespace nodes, and an <svg> made that way
// renders as nothing. Same shape as el(), different namespace; the landing hero is its only caller.
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string> = {},
  ...children: (Node | string)[]
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const c of children) node.append(c);
  return node;
}

let activeGraph: GraphView | null = null; // the one canvas graph on screen; every view swap destroys it

function setView(...nodes: Node[]): void {
  activeGraph?.destroy();
  activeGraph = null;
  app.replaceChildren(...nodes);
  app.removeAttribute("aria-busy");
}

function showError(where: string, err: unknown): void {
  // A 401 is not an error to report, it's a state to recover from: every view funnels its failures
  // through here, so this one branch turns "your session ended" into the login screen everywhere
  // instead of showing a dev hint ("run apiserver.py") to a teammate on the team dashboard.
  // no identity on record = never signed in, or signed out on purpose: the notice would be wrong
  if (err instanceof AuthError) return renderLogin(me ? err.message : "");
  const msg = err instanceof Error ? err.message : String(err);
  view(el("main", {},
    el("p", { class: "error", role: "alert" }, `Failed to ${where}: ${msg}`),
    // Not a dev hint. This renders on a HOSTED server too, where "run apiserver.py" tells a teammate
    // to do something impossible and reads as "the product is broken" — a fresh-install tester hit
    // exactly this and concluded their install had failed.
    el("p", { class: "hint" }, location.hostname === "localhost" || location.hostname === "127.0.0.1"
      ? "Is the API running? `uv run python apiserver.py` serves it."
      : "That request did not reach the server. Try again in a moment, if it keeps failing, whoever runs this CSA can check the server log."),
  ));
}

// --- sign in ----------------------------------------------------------------

// A MODE, not a route: no #/login hash, so the back button can never land on a logged-out view and
// signing in resumes exactly where the user was. The shell (index.html/js/css) is served without a
// cookie — it holds no corpus data — and everything it renders arrives from gated /api/* calls.
function renderLogin(notice = ""): void {
  const email = el("input", {
    id: "email", type: "email", required: "", placeholder: SIGNIN.emailPlaceholder,
    "aria-describedby": "email-help", autocomplete: "email",
  }) as HTMLInputElement;
  const invite = el("input", {
    id: "invite", type: "password", required: "", "aria-describedby": "invite-help", autocomplete: "off",
  }) as HTMLInputElement;
  const go = el("button", { class: "btn-primary", type: "submit" }, SIGNIN.submit) as HTMLButtonElement;
  const msg = el("p", { class: "error", role: "alert" });
  msg.hidden = !notice;
  if (notice) msg.textContent = notice;

  const form = el(
    "form", { class: "login-form" },
    el("label", { for: "email" }, SIGNIN.emailLabel),
    email,
    // The honest bit: on the invite path the code is shared and the email is unverified, so say
    // plainly that the email is a label, not a login. An identity provider is what replaces this caveat.
    el("small", { id: "email-help", class: "hint" }, SIGNIN.emailHelp.text),
    el("label", { for: "invite" }, SIGNIN.inviteLabel),
    invite,
    el("small", { id: "invite-help", class: "hint" }, SIGNIN.inviteHelp),
    go,
  ) as HTMLFormElement;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    go.disabled = true;
    const r = await login(invite.value, email.value);
    if (r.ok) {
      // replaceState, not location.hash=: the hashchange listener is already wired (boot() below wires
      // it on the very first call), so a real hash change here would fire route() BEFORE the identity
      // this boot() fetches has landed, then again when boot() calls route() itself — two renders.
      history.replaceState(null, "", "#/");
      return void boot();                  // re-fetch chrome as the new identity, then route the hash
    }
    msg.textContent = r.error;             // never clears the typed invite: retyping a secret is the friction
    msg.hidden = false;
    go.disabled = false;
  });

  // Both sign-in surfaces start hidden and exactly one is revealed once the server says which it
  // offers: a flash of an invite form that does not exist on an SSO-only server is the wrong first
  // impression for a trust screen. The SSO button is a plain link: the round trip leaves this page
  // and the callback's own page hops back to the hash we were on.
  const ssoGo = el("a", { class: "btn-primary sso-go" }) as HTMLAnchorElement;
  // What this server keeps is READ from the server, never asserted: the sentence has to stay true on
  // the one screen where a stranger decides whether to trust the product.
  const sub = el("p", { class: "login-sub" }, SIGNIN.keepsNone.text);
  const foot = el("p", { class: "hint login-hint" }, "");
  const request = el("p", { class: "login-foot" }, SIGNIN.noWorkspace + " ");
  const reqBtn = el("button", { class: "linklike", type: "button" }, SIGNIN.request);
  reqBtn.addEventListener("click", () => renderLanding("request"));
  request.append(reqBtn);
  for (const n of [ssoGo, form, foot, request]) n.hidden = true;

  // An invited person arrives carrying a link, not a password. The server names it (it reads the
  // httpOnly join cookie), so the card can offer that one identity instead of a form whose fields
  // the join path ignores.
  const invited = el("p", { class: "login-sub" });
  const joinGo = el("button", { class: "btn-primary", type: "button" }) as HTMLButtonElement;
  for (const n of [invited, joinGo]) n.hidden = true;
  joinGo.addEventListener("click", async () => {
    joinGo.disabled = true;
    const r = await login("", "");            // the join cookie is the credential; nothing is typed
    if (r.ok) {
      history.replaceState(null, "", "#/");   // see the invite handler above for why not location.hash=
      return void boot();
    }
    msg.textContent = r.error;
    msg.hidden = false;
    joinGo.disabled = false;
  });

  const bg = el("canvas", { class: "login-bg", "aria-hidden": "true" }) as HTMLCanvasElement;
  setView(
    el("div", { class: "login-page" },
      el("header", { class: "login-head" }, brandStatic()),
      el("div", { class: "login-stage" },
        bg,
        el("div", { class: "login-card" },
          el("h1", { class: "login-title" }, SIGNIN.title),
          el("p", { class: "login-pitch" }, META.description.text),
          sub, invited, ssoGo, joinGo, form, msg, foot,
        ),
        request,
      ),
    ),
  );
  // the demo story, faint and still, behind the card (the same drawing as the landing hero)
  activeGraph = new GraphView(bg, { data: DEMO, colors: () => cssColors(bg), reducedMotion: true, labels: false });

  void (async () => {
    const cfg = await authConfig();
    sub.textContent = cfg.archive_days === null ? SIGNIN.keepsUnknown.text
      : cfg.archive_days < 0 ? SIGNIN.keepsForever.text
      : cfg.archive_days > 0 ? SIGNIN.keepsDays.text.replace("{n}", String(cfg.archive_days))
      : SIGNIN.keepsNone.text;
    if (cfg.join_email) {
      invited.hidden = false;
      invited.textContent = (cfg.join_org ? SIGNIN.invitedTo.replace("{org}", cfg.join_org)
        : SIGNIN.invitedAs).replace("{email}", cfg.join_email);
      // Carrying a link, on a server with no issuer to verify anyone: the link IS the credential and
      // the address is the one it names, so there is nothing to type. It is OFFERED, never the only
      // way out of this screen: a session that expired in a browser that once opened someone else's
      // link would otherwise have one button, and it would sign you in as them. With an issuer the
      // link rides the round trip instead (the server reads the cookie at /api/sso/start).
      if (!cfg.sso_required) {
        joinGo.hidden = false;
        joinGo.textContent = SIGNIN.continueAs.replace("{email}", cfg.join_email);
      }
    }
    if (cfg.join_stale) {
      // They followed a link that no longer resolves. The same one sentence every unusable link
      // gets, rather than a form asking for an invite code nobody ever gave them.
      msg.textContent = SIGNIN.joinStale;
      msg.hidden = false;
    }
    const invitePath = !cfg.sso_required && !cfg.join_email;   // a link is not an invite code
    if (invitePath) {
      form.hidden = false;
      foot.hidden = false;
      foot.textContent = SIGNIN.noInvite;
      email.focus();   // `autofocus` only fires on first document parse, not on this re-render (e.g. after Sign out)
    } else if (cfg.join_email && !cfg.sso) {
      joinGo.focus();
    } else {
      sub.textContent += " " + SIGNIN.ssoOnly.text;
    }
    request.hidden = !cfg.workspace_requests;
    if (!cfg.sso) return;
    ssoGo.href = ssoHref(cfg.sso.start_url);
    ssoGo.textContent = SIGNIN.ssoButton.replace("{name}", cfg.sso.name);
    ssoGo.hidden = false;
    ssoGo.focus();
  })();
}

// --- pre-auth landing ------------------------------------------------------------------------------
// The signed-out front door: one long page. A MODE like renderLogin (no hash, no app chrome): a hash
// change here fires route(), whose first authenticated fetch 401s into renderLogin, so every in-page
// jump is a scrollIntoView and Sign in / Request swap views in place. The request form is the only
// network call. Copy and the demo story live in landing.ts, where landing.test.ts holds them to the
// word budgets, the claims audit and the no-real-names rule. The pillar "screenshots" are the app's
// own components fed with the demo story (fictional names, both themes), not pictures.

function jumpTo(id: string): void {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.getElementById(id)?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
}

function renderLanding(jump = ""): void {
  const graphHost = el("div", { class: "lp-graph" });
  const panel = el("div", { class: "graph-panel" });
  panel.hidden = true;
  setView(
    el("div", { class: "lp" },
      landingHead(),
      landingHero(graphHost, panel),
      ...PILLARS.map((p, i) => pillar(p, i)),
      landingStored(),
      landingRequest(),
      el("footer", { class: "lp-foot" }, brandStatic(), signInLink()),
    ),
  );
  activeGraph = demoGraph(graphHost, panel);
  if (jump) jumpTo(jump);
}

function signInLink(cls = "linklike"): HTMLElement {
  const b = el("button", { class: cls, type: "button" }, NAV.signIn);
  b.addEventListener("click", () => renderLogin());
  return b;
}

function landingHead(): HTMLElement {
  const jump = (text: string, id: string, cls = "lp-navlink lp-navhide") => {
    const b = el("button", { class: cls, type: "button" }, text);
    b.addEventListener("click", () => jumpTo(id));
    return b;
  };
  return el("header", { class: "lp-head" },
    brandStatic(),
    el("nav", { class: "lp-nav", "aria-label": "landing" },
      jump(NAV.how, "observe"), jump(NAV.stored, "stored"), jump(NAV.request, "request", "lp-navlink"), signInLink("lp-navlink"),
    ),
  );
}

function landingHero(graphHost: HTMLElement, panel: HTMLElement): HTMLElement {
  const cta = el("button", { class: "cta", type: "button" }, HERO.cta);
  cta.addEventListener("click", () => jumpTo("request"));
  const legend = el("div", { class: "lp-legend", "aria-hidden": "true" });
  for (const [t, label] of HERO.legend) legend.append(el("span", { class: `t-${t}` }, el("i", { class: "dot" }), label));
  legend.append(el("span", {}, el("i", { class: "edge-glyph" }), HERO.legendEdge));
  const works = el("div", { class: "lp-harness", role: "list", "aria-label": "harnesses" });
  for (const h of HERO.harnesses)
    works.append(el("span", { class: h.next ? "lp-harness-item next" : "lp-harness-item", role: "listitem" },
      el("img", { src: `/logos/${h.id}.png`, alt: "", class: h.mono ? "mono" : "", width: "40", height: "40" }), h.name,
      ...(h.next ? [el("em", { class: "lp-harness-next" }, HERO.nextLabel)] : [])));
  return el("section", { class: "lp-hero" },
    el("h1", { class: "lp-name" }, HERO.name),
    el("p", { class: "lp-line" }, HERO.line.text),
    cta,
    works,
    graphHost,
    legend,
    panel,
  );
}

// The hero graph: the fixed demo story, hover card pinned by the pointer, a fact panel (below the
// graph) on click. A tap on touch (no hover) shows the session card. The same list, visually hidden,
// gives a screen reader the whole story as text.
function demoGraph(host: HTMLElement, panel: HTMLElement): GraphView {
  const canvas = el("canvas", { class: "lp-canvas", role: "img", "aria-label": HERO.alt }) as HTMLCanvasElement;
  const card = el("div", { class: "graph-card" });
  card.hidden = true;
  host.append(canvas, card, demoList());
  const place = (x: number, y: number) => {
    const b = host.getBoundingClientRect();
    card.style.left = `${Math.min(Math.max(8, x - b.left + 16), Math.max(8, b.width - card.offsetWidth - 8))}px`;
    card.style.top = `${Math.min(Math.max(8, y - b.top - 24), Math.max(8, b.height - card.offsetHeight - 8))}px`;
  };
  let shown = "";
  const show = (n: GNode, x: number, y: number) => {
    card.replaceChildren(...hoverCard(n, view));
    card.hidden = false;
    shown = n.id;
    place(x, y);
  };
  const hide = () => { card.hidden = true; shown = ""; };
  const view = new GraphView(canvas, {
    data: DEMO,
    colors: () => cssColors(canvas),
    ambient: 0.9,
    onHover: (n, x, y) => (n ? show(n, x, y) : hide()),
    // a tap has no hover: tapping a session shows its card, tapping it again or the empty canvas hides it
    onClick: (n, x, y) => {
      if (!n) return hide();
      if (n.kind === "session") return shown === n.id && !card.hidden ? hide() : show(n, x, y);
      panel.replaceChildren(...factPanel(n.id, view, () => { panel.hidden = true; }));
      panel.hidden = false;
      panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    },
  });
  return view;
}

function hoverCard(n: GNode, view: GraphView): Node[] {
  if (n.kind === "fact") {
    const f = DEMO.facts.find((x) => x.ref === n.id)!;
    return [el("div", { class: "k" }, f.type), el("div", { class: "t" }, f.claim)];
  }
  const s = view.session(n.id)!;
  const out: Node[] = [
    el("div", { class: "k" }, HERO.cardSession),
    el("div", { class: "t" }, s.title),
    el("div", { class: "k" }, `${s.started_at} · ${s.n_facts} facts`),
  ];
  const from = view.recalledFacts(s.session_id);
  if (from.length) {
    const ul = el("ul", {});
    for (const f of from) ul.append(el("li", { class: `t-${f.type}` }, el("i", { class: "dot" }), f.claim));
    out.push(el("div", { class: "k", style: "margin-top:8px" }, HERO.cardRecalled), ul);
  }
  return out;
}

function factPanel(ref: string, view: GraphView, close: () => void): Node[] {
  const f = DEMO.facts.find((x) => x.ref === ref)!;
  const s = view.session(f.session_id)!;
  const x = el("button", { class: "close", type: "button" }, HERO.close);
  x.addEventListener("click", close);
  const out: Node[] = [
    el("div", { class: `graph-panel-head t-${f.type}` }, el("i", { class: "dot", "aria-hidden": "true" }), el("span", { class: "tchip" }, f.type), x),
    el("h3", {}, f.claim),
    el("blockquote", {}, f.evidence ?? ""),
    el("p", { class: "byline" }, `${HERO.panelShipped} ${s.user} · ${s.project} · ${s.started_at}`),
  ];
  const into = view.recalledInto(ref).map((id) => view.session(id)!);
  if (into.length) {
    const ul = el("ul", {});
    for (const t of into) ul.append(el("li", {}, el("span", {}, t.started_at), el("span", {}, t.title), el("span", { class: "hint" }, t.project)));
    out.push(el("div", { class: "k" }, `${HERO.panelRecalled} ${into.length} later ${into.length === 1 ? "session" : "sessions"}`), ul);
  }
  return out;
}

function demoList(): HTMLElement {
  const ol = el("ol", { class: "sr-only" });
  for (const s of DEMO.sessions) {
    const ul = el("ul", {});
    for (const f of DEMO.facts) if (f.session_id === s.session_id) ul.append(el("li", {}, `${f.type}: ${f.claim}`));
    ol.append(el("li", {}, `${s.title}, ${s.project}, ${s.user}, ${s.started_at}. `, ul));
  }
  return ol;
}

function pillar(p: Pillar, i: number): HTMLElement {
  const shot = i === 0 ? shotObserve() : i === 1 ? shotDiscover() : shotImprove();
  return el("section", { class: i % 2 ? "pillar flip" : "pillar", id: p.id },
    el("div", { class: "pillar-text" },
      el("p", { class: "eyebrow" }, p.eyebrow),
      el("h2", {}, p.head.plain + " ", el("em", {}, p.head.em)),
      el("p", { class: "pillar-body" }, p.body.text),
    ),
    el("div", { class: "shot", role: "img", "aria-label": p.shotAlt, inert: "" }, shotBar(p.shotTab), el("div", { class: "shot-body" }, ...shot)),
  );
}

// The facsimile's chrome: the real top bar, small, with the tabs the live app has (NAV_ITEMS), so a
// visitor who signs in finds exactly what the landing showed.
function shotBar(active: string): HTMLElement {
  const tabs = el("div", { class: "shot-tabs" });
  for (const [t] of NAV_ITEMS) tabs.append(el("span", { class: t === active ? "on" : "" }, t));
  return el("div", { class: "shot-bar" }, el("span", { class: "brand" }, ringMark(), "CSA"), tabs, el("span", { class: "avatar" }, "M"));
}

function demoFact(ref: string, extras: FactExtras = {}): HTMLElement {
  const f = DEMO.facts.find((x) => x.ref === ref)!;
  const s = DEMO.sessions.find((x) => x.session_id === f.session_id)!;
  return factCard({ ref: f.ref, kind: "hidden_context", project: s.project, type: f.type, claim: f.claim, evidence: f.evidence ?? null, label: null, flaggable: false },
    { user: s.user, when: s.started_at, ...extras });
}

function shotObserve(): Node[] {
  const s = DEMO.sessions.find((x) => x.session_id === DEMO_METRICS.session_id)!;
  const m = el("div", { class: "metrics" });
  for (const [k, v] of [["Cost", DEMO_METRICS.cost], ["Tool calls", DEMO_METRICS.toolCalls], ["Wasted", DEMO_METRICS.wasted], ["Duration", DEMO_METRICS.duration]])
    m.append(el("div", {}, el("span", {}, k), el("b", {}, v)));
  const list = el("ul", { class: "shot-facts" });
  for (const f of DEMO.facts.filter((x) => x.session_id === s.session_id).slice(0, 5))
    list.append(el("li", {}, el("div", { class: `fact-type t-${f.type}` }, el("i", { class: "dot" }), el("span", { class: "tchip" }, f.type)),
      el("div", {}, el("b", {}, f.claim), el("q", {}, f.evidence ?? ""))));
  return [
    el("h3", { class: "shot-title" }, s.title),
    el("p", { class: "shot-meta" }, `${s.project} · ${s.user} · ${s.started_at}`),
    m,
    el("p", { class: "shot-sub" }, "Facts from this session"),
    list,
  ];
}

function shotDiscover(): Node[] {
  const scope = el("div", { class: "scope" },
    el("span", { class: "scope-btn active" }, "Everyone"), el("span", { class: "scope-btn" }, "Just mine"), el("span", { class: "scope-btn" }, "One teammate"));
  const search = el("div", { class: "search" }, "rate limit");
  const list = el("div", { class: "facts" });
  for (const [ref, used] of [["f1", 3], ["f2", 1], ["f28", 0]] as [string, number][]) list.append(demoFact(ref, { used }));
  return [el("div", { class: "shot-toolbar" }, scope, search), el("p", { class: "hint" }, "5 facts match"), list];
}

function shotImprove(): Node[] {
  const f = DEMO.facts.find((x) => x.ref === "f1")!;
  const s = DEMO.sessions.find((x) => x.session_id === f.session_id)!;
  const into = DEMO.recalls.filter((r) => r.ref === f.ref).map((r) => DEMO.sessions.find((x) => x.session_id === r.session_id)!);
  const table = el("table", { class: "recalled" }, el("thead", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "Session"), el("th", {}, "Project"))));
  const tb = el("tbody", {});
  for (const t of into) tb.append(el("tr", {}, el("td", {}, t.started_at), el("td", {}, t.title), el("td", {}, t.project)));
  table.append(tb);
  return [
    el("div", { class: `fact-detail t-${f.type}` },
      el("div", { class: "fact-detail-head" }, el("i", { class: "dot" }), el("span", { class: "tchip" }, f.type),
        el("span", { class: "pill" }, `${HERO.panelRecalled} ${into.length} later sessions`)),
      el("h4", {}, f.claim),
      el("blockquote", {}, f.evidence ?? ""),
      el("p", { class: "byline" }, `${s.user} · ${s.project} · ${s.started_at}`)),
    el("p", { class: "shot-sub" }, "Recalled in"),
    table,
    el("div", { class: "rule-card" },
      el("p", { class: "eyebrow" }, el("span", {}, DEMO_RULE.eyebrow), el("span", {}, DEMO_RULE.project)),
      el("b", {}, DEMO_RULE.rule), el("i", {}, DEMO_RULE.do), el("p", {}, DEMO_RULE.why)),
  ];
}

// Backtick spans render as <code>; both halves land as text nodes (XSS-inert).
function codeSpans(s: string): (Node | string)[] {
  return s.split("`").flatMap((part, i) => (part ? [i % 2 ? el("code", {}, part) : part] : []));
}

// Line icons for the What-is-stored card: one path each, 24-unit grid, drawn in currentColor.
const ICON: Record<string, string> = {
  doc: "M6 3h8l4 4v14H6z M14 3v4h4 M9 12h6 M9 16h6",
  quote: "M6 8h4v4H6v2a2 2 0 0 0 2 2 M14 8h4v4h-4v2a2 2 0 0 0 2 2",
  person: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 21c0-4 3.6-6 8-6s8 2 8 6",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  calendar: "M4 6h16v14H4z M4 10h16 M8 3v5 M16 3v5",
  lines: "M5 4h14v16H5z M8 9h8 M8 13h8 M8 17h5",
  prompt: "M4 5h16v14H4z M8 10l3 2-3 2 M13 14h4",
  laptop: "M4 5h16v11H4z M2 19h20 M16 13v-2a2 2 0 0 0-4 0v2",
  cloud: "M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9.5 3.5 3.5 0 0 1 18 18z",
  server: "M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01",
  loop: "M20 12a8 8 0 1 1-2.3-5.7 M20 4v5h-5",
  lock: "M6 11h12v10H6z M9 11V7a3 3 0 0 1 6 0v4",
  memory: "M12 3a6 6 0 0 0-6 6c0 2.2 1.2 3.6 2.5 4.6V17h7v-3.4C16.8 12.6 18 11.2 18 9a6 6 0 0 0-6-6z M9.5 20h5",
  graph: "M6 6a2 2 0 1 0 0 .01 M18 6a2 2 0 1 0 0 .01 M12 18a2 2 0 1 0 0 .01 M7.5 7.5l3.5 9 M16.5 7.5l-3.5 9 M8 6h8",
  team: "M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z M2 20c0-3.3 3.1-5 7-5s7 1.7 7 5 M16 4a3.5 3.5 0 0 1 0 7 M18 15c2.4.5 4 2 4 5",
  chart: "M4 20V4 M4 20h16 M8 16v-5 M12 16V8 M16 16v-8",
  model: "M12 3l8 4.5v9L12 21l-8-4.5v-9z M12 12l8-4.5 M12 12v9 M12 12L4 7.5",
  key: "M15 9a4 4 0 1 1-8 0 4 4 0 0 1 8 0z M14 12l7 7 M18 16l2-2 M16 18l2-2",
  menu: "M4 7h16 M4 12h16 M4 17h16",
};

function icon(name: string): SVGSVGElement {
  const svg = svgEl("svg", { class: "ico", viewBox: "0 0 24 24", "aria-hidden": "true" });
  svg.append(svgEl("path", { d: ICON[name], fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round" }));
  return svg;
}

// A flat illustrated portrait for the demo shipper: nobody real, drawn here so nothing is fetched.
function demoAvatar(): SVGSVGElement {
  const svg = svgEl("svg", { class: "portrait", viewBox: "0 0 40 40", "aria-hidden": "true" });
  svg.append(
    svgEl("circle", { cx: "20", cy: "20", r: "20", fill: "#e9e6de" }),
    svgEl("path", { d: "M6 40c1-8 6-12 14-12s13 4 14 12z", fill: "#8f84d8" }),
    svgEl("circle", { cx: "20", cy: "17", r: "8", fill: "#d9a98a" }),
    svgEl("path", { d: "M12 16c0-6 4-9 8-9s8 3 8 9c-2-2-4-3-8-3s-6 1-8 3z", fill: "#3b2f2a" }),
  );
  return svg;
}

function landingStored(): HTMLElement {
  const list = el("ul", { class: "stored-list" });
  for (const it of STORED.items) {
    const li = el("li", {}, el("i", { class: "dot", "aria-hidden": "true" }), el("span", {}, it.text + " "));
    if (it.em) li.lastElementChild!.append(el("b", {}, it.em));
    list.append(li);
  }
  const table = el("div", { class: "stored-table" });
  for (const r of STORED.rows) {
    const example = "when" in r
      ? el("div", { class: "stored-person" }, demoAvatar(), el("div", {}, el("b", {}, r.example), el("span", { class: "k" }, r.when)))
      : el("div", {}, el("code", {}, r.example));
    table.append(el("div", { class: "stored-row" },
      el("div", { class: `tile t-${r.tint}` }, icon(r.icon)),
      el("div", {}, el("b", {}, r.name), el("span", { class: "k" }, r.sub)),
      el("div", { class: "what" }, r.what),
      example));
  }
  const flow = el("div", { class: "flow" });
  STORED.flow.forEach((st, i) => {
    if (i) flow.append(el("span", { class: "flow-arrow", "aria-hidden": "true" }, "\u2192"));
    flow.append(el("div", { class: "flow-step" }, icon(st.icon), el("b", {}, st.head), st.body));
  });
  return el("section", { class: "stored", id: "stored" },
    el("div", { class: "pillar-text" },
      el("p", { class: "eyebrow" }, STORED.eyebrow),
      el("h2", {}, STORED.head.plain + " ", el("em", {}, STORED.head.em)),
      list),
    el("div", { class: "stored-card" },
      el("h3", {}, STORED.cardTitle),
      table,
      flow,
      el("p", { class: "stored-foot" }, icon("lock"), el("span", {}, ...codeSpans(STORED.foot.text)))),
  );
}

function landingRequest(): HTMLElement {
  const card = el("div", { class: "request-card" });
  card.append(...requestForm(card));
  return el("section", { class: "request", id: "request" },
    el("div", { class: "pillar-text" },
      el("p", { class: "eyebrow" }, REQUEST.eyebrow),
      el("h2", {}, REQUEST.head.plain + " ", el("em", {}, REQUEST.head.em)),
      el("p", { class: "request-intro" }, REQUEST.intro.text)),
    card,
  );
}

function requestForm(card: HTMLElement): Node[] {
  const company = el("input", { id: "wr-company", type: "text", required: "", maxlength: "120", placeholder: REQUEST.companyPlaceholder, autocomplete: "organization" }) as HTMLInputElement;
  const email = el("input", { id: "wr-email", type: "email", required: "", placeholder: REQUEST.emailPlaceholder, "aria-describedby": "wr-email-help", autocomplete: "email" }) as HTMLInputElement;
  const send = el("button", { class: "btn-primary", type: "submit" }, REQUEST.submit) as HTMLButtonElement;
  const msg = el("p", { class: "error", role: "alert" });
  msg.hidden = true;
  const form = el("form", { class: "login-form" },
    el("label", { for: "wr-company" }, REQUEST.companyLabel), company,
    el("label", { for: "wr-email" }, REQUEST.emailLabel), email,
    el("small", { id: "wr-email-help", class: "hint" }, REQUEST.emailHelp),
    send,
  ) as HTMLFormElement;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const bad = validRequest(company.value, email.value);
    if (bad) {
      msg.textContent = bad;
      msg.hidden = false;
      return;
    }
    send.disabled = true;
    msg.hidden = true;
    const r = await postWorkspaceRequest(company.value.trim(), email.value.trim());
    if (r.ok) return card.replaceChildren(...requestSuccess(email.value.trim()));
    msg.textContent = r.error; // never clears what was typed: retyping is the friction
    msg.hidden = false;
    send.disabled = false;
  });
  return [el("h3", {}, REQUEST.title), form, msg, el("p", { class: "request-note" }, REQUEST.note)];
}

// Renders purely from {ok:true} + the email just typed. No stored state: a duplicate submit paints this
// pixel-identical to the first, so the page holds no memory a prober could read back.
function requestSuccess(email: string): Node[] {
  return [
    el("h3", {}, SUCCESS.title),
    el("p", { class: "request-sub" }, SUCCESS.bodyBefore + " ", el("strong", {}, email), ". " + SUCCESS.bodyAfter),
    el("p", { class: "request-alt" }, SUCCESS.haveInvite + " ", signInLink()),
  ];
}

// --- shared chrome: the sidebar shell ------------------------------------------
// --- shared chrome: the top bar ---------------------------------------------------------------
// The Ring: a filled circle inside a thin ring, one colour (the accent). Also the favicon.
function ringMark(): SVGSVGElement {
  const svg = svgEl("svg", { class: "mark", viewBox: "0 0 26 26", "aria-hidden": "true" });
  svg.append(
    svgEl("circle", { cx: "13", cy: "13", r: "11", fill: "none", stroke: "currentColor", "stroke-width": "1.8" }),
    svgEl("circle", { cx: "13", cy: "13", r: "6", fill: "currentColor" }),
  );
  return svg;
}

function brand(href: string): HTMLElement {
  return el("a", { href, class: "brand" }, ringMark(), "CSA");
}

// The landing has no routes to link to (a hash change would fire route()): its wordmark scrolls up.
function brandStatic(): HTMLElement {
  const b = el("button", { class: "brand", type: "button", "aria-label": "top of page" }, ringMark(), "CSA");
  b.addEventListener("click", () => window.scrollTo({ top: 0 }));
  return b;
}

function searchGlyph(): SVGSVGElement {
  const svg = svgEl("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" });
  svg.append(
    svgEl("circle", { cx: "7", cy: "7", r: "5", fill: "none", stroke: "currentColor", "stroke-width": "1.6" }),
    svgEl("path", { d: "M11 11 L15 15", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round" }),
  );
  return svg;
}

// Retired pages (Today, Improve) and the Team-page tools light the tab they belong to.
const NAV_ITEMS: [string, string, (h: string) => boolean][] = [];

// Admin's pages, each its own route, in the sidebar's Admin group. People and roles live on Team.
const ADMIN_ITEMS: [string, string, string, (h: string) => boolean][] = [
  ["Analytics", "#/admin", "chart", (h) => h === "/admin"],
  ["AI Models", "#/admin/models", "model", (h) => h === "/admin/models"],
  ["Service Tokens", "#/admin/tokens", "key", (h) => h === "/admin/tokens"],
  ["Entity types", "#/admin/entity-types", "graph", (h) => h === "/admin/entity-types"],
];
const NAV_ICON: Record<string, string> = { Memory: "memory", Sessions: "lines", Entities: "graph", Team: "team", [CONNECT.nav]: "laptop" };

function navLink(text: string, href: string, iconName: string, on: boolean): HTMLElement {
  return el("a", on ? { href, class: "side-link active", "aria-current": "page" } : { href, class: "side-link" },
    icon(iconName), el("span", {}, text));
}

// One sidebar for the whole app, in CSA's palette: the workspace's pages for everyone,
// then an Admin group for admins, and who is signed in at the foot.
function sidebar(): HTMLElement {
  const hash = routePath() || "/";
  const nav = el("nav", { class: "side-nav", "aria-label": "main" });
  for (const [text, href, isOn] of NAV_ITEMS) nav.append(navLink(text, href, NAV_ICON[text], isOn(hash)));
  if (me?.role === "admin") {
    nav.append(el("p", { class: "side-group" }, "Admin"));
    for (const [text, href, ico, isOn] of ADMIN_ITEMS) nav.append(navLink(text, href, ico, isOn(hash)));
  }
  const side = el("aside", { class: "sidebar" });
  nav.addEventListener("click", (e) => { if ((e.target as HTMLElement).closest(".side-link")) side.classList.remove("open"); });
  const toggle = el("button", { class: "side-toggle", type: "button", "aria-label": "menu", "aria-expanded": "false" }, icon("menu"));
  toggle.addEventListener("click", () => {
    const open = side.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });
  const top = el("div", { class: "side-top" }, brand("#/"), toggle);
  if (me) top.append(el("span", { class: "side-org" }, teamCard(me.org_id, me.org_name).name));
  side.append(top, nav);
  if (me) side.append(el("div", { class: "side-foot" }, accountMenu()));
  return side;
}

/** Memory's list for a query; the query rides in the hash so a reload and Back keep it. */
function memoryHash(q: string): string {
  return q ? `#/?q=${encodeURIComponent(q)}` : "#/";
}

// Search and the theme, above every page. The one search box: it answers on Memory's list.
function topstrip(): HTMLElement {
  return el("header", { class: "release-head" }, brand("#/"), accountMenu());
}

function shell(...nodes: Node[]): HTMLElement {
  return el("div", { class: "release-shell" }, topstrip(), el("div", { class: "release-content" }, ...nodes));
}

// The identity chip opens the account menu.
function accountMenu(): HTMLElement {
  const who = me?.name && me.name !== me.email ? me.name : (me?.email ?? "signed in with a shared token");
  const btn = el("button", { class: "nav-me", type: "button", "aria-haspopup": "menu",
                             "aria-expanded": "false", "aria-label": "Account and settings", title: "Account and settings" },
    el("span", { class: "avatar", "aria-hidden": "true" }, initialOf(me?.name, me?.email)),
    el("span", { class: "nav-who" }, who));
  const connect = el("a", { href: "#/connect", class: "menu-item", role: "menuitem" }, CONNECT.nav);
  const models = el("a", { href: "#/admin/models", class: "menu-item", role: "menuitem" }, "AI models");
  const members = el("a", { href: "#/team", class: "menu-item", role: "menuitem" }, "Members and access");
  const admin = el("a", { href: "#/admin", class: "menu-item", role: "menuitem" }, "Administration");
  admin.hidden = me?.role !== "admin";
  const out = el("button", { class: "menu-item", type: "button", role: "menuitem" }, "Sign out");
  const menu = el("div", { class: "account-menu", role: "menu", "aria-label": "Account and settings" }, connect, models, members, admin, out);
  menu.hidden = true;
  const close = () => {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onAway, true);
    document.removeEventListener("keydown", onEsc);
  };
  const onAway = (e: Event) => {
    if (!wrap.contains(e.target as Node)) close();
  };
  const onEsc = (e: KeyboardEvent) => {
    if (e.key === "Escape") { close(); btn.focus(); }
  };
  btn.addEventListener("click", () => {
    if (!menu.hidden) return close();
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    document.addEventListener("click", onAway, true);
    document.addEventListener("keydown", onEsc);
    connect.focus();
  });
  for (const link of [connect, models, members, admin]) link.addEventListener("click", close);
  out.addEventListener("click", async () => {
    (out as HTMLButtonElement).disabled = true;
    stopHomePoll();   // this bypasses route(), so stop the Pipeline poll by hand or it clobbers the login on its next tick
    await logout();
    me = null;
    labelLegend = null;
    renderLogin("");
  });
  const wrap = el("div", { class: "account-wrap" }, btn, menu);
  return wrap;
}

// Every page renders as: top bar, then the content column.
function view(...nodes: Node[]): void {
  setView(shell(...nodes));
}

// Every page opens with its name and one sentence saying what you're looking at.
function pageHead(title: string, explain: string): HTMLElement {
  return el(
    "div",
    { class: "page-head" },
    el("h1", { class: "page-title" }, title),
    el("p", { class: "explain" }, explain),
  );
}

function pager(offset: number, limit: number, total: number, go: (o: number) => void): HTMLElement {
  const prev = el("button", { class: "btn", type: "button" }, "← Prev") as HTMLButtonElement;
  const next = el("button", { class: "btn", type: "button" }, "Next →") as HTMLButtonElement;
  prev.disabled = offset <= 0;
  next.disabled = offset + limit >= total;
  prev.addEventListener("click", () => go(Math.max(0, offset - limit)));
  next.addEventListener("click", () => go(offset + limit));
  const info = el(
    "span",
    { class: "pager-info" },
    total ? `${offset + 1}-${Math.min(offset + limit, total)} of ${total}` : "0 of 0",
  );
  return el("div", { class: "pager" }, prev, info, next);
}

// --- recall log: every recall request, the same rows Analytics counts ---------
let recallFilter: RecallFilter = "";

function recallFilterControl(): HTMLElement {
  const bar = el("div", { class: "scope", role: "group", "aria-label": "show" });
  for (const { value, label } of RECALL_FILTERS) {
    const on = value === recallFilter;
    const b = el("button", { class: `scope-btn${on ? " active" : ""}`, type: "button", "aria-pressed": String(on) }, label);
    b.addEventListener("click", () => {
      if (recallFilter === value) return;
      recallFilter = value;
      void renderRecall();
    });
    bar.append(b);
  }
  return bar;
}

async function renderRecall(): Promise<void> {
  const filter = recallFilter;
  let log;
  try {
    log = await getSessions(filter);
  } catch (err) {
    return showError("load recall log", err);
  }
  if (routePath() !== "/recall" || filter !== recallFilter) return;
  const table = el("table", { class: "sessions" });
  table.append(el("thead", {}, el("tr", {},
    el("th", {}, "When"), el("th", {}, "Asked from"), el("th", {}, "Question"),
    el("th", {}, "How"), el("th", { class: "num" }, "Results"))));
  const body = el("tbody", {});
  if (log.rows.length === 0)
    body.append(el("tr", {}, el("td", { colspan: "5", class: "hint" },
      !log.total ? "Nothing yet. This fills up once a connected Claude Code session asks for past context."
        : filter === "missed" ? "No misses. Every request found something." : "No request has found anything yet.")));
  for (const r of log.rows) {
    const href = `#/session/${encodeURIComponent(r.session)}`;
    const question = r.question === null
      ? el("span", { class: "hint" }, "a teammate's request that found nothing")
      : r.results > 0 && r.session ? el("a", { href, title: r.question }, truncate(r.question, 90))
      : el("span", { title: r.question }, truncate(r.question, 90));
    body.append(el("tr", {},
      el("td", { class: "when" }, r.at ? fmtWhen(r.at) : ""),
      el("td", {}, r.session ? el("a", { href, title: r.session }, shortId(r.session))
        : el("span", { class: "hint", title: RECALL_SOURCE[r.source].title }, RECALL_SOURCE[r.source].text)),
      el("td", { class: "summary-cell" }, question),
      el("td", {}, r.automatic ? "automatic" : "asked"),
      el("td", { class: "num" }, r.results > 0 ? String(r.results) : el("span", { class: "hint" }, "miss"))));
  }
  table.append(body);
  const line = [recallLogLine(log, filter), log.total ? `${asOf(log.as_of)}.` : ""].filter(Boolean).join(" ");
  view(
    el(
      "main",
      {},
      pageHead(
        "Recalled into sessions",
        "Every recall request a Claude Code session made, and how many results CSA handed back.",
      ),
      line ? el("p", { class: "explain" }, line) : "",
      log.total ? el("div", { class: "toolbar" }, recallFilterControl()) : "",
      table,
    ),
  );
}

// --- session detail view ---------------------------------------------------
async function renderDetail(sid: string): Promise<void> {
  let questions;
  try {
    questions = await getSession(sid);
  } catch (err) {
    return showError("load session", err);
  }
  const main = el(
    "main",
    {},
    el("h1", { class: "sid" }, sid),
    el("p", { class: "explain" }, "The questions this session asked, and the facts recalled for each. Rate a fact to tune what is recalled next time."),
  );
  if (questions.length === 0) {
    main.append(el("p", { class: "hint" }, "No questions for this session."));
  }
  for (const q of questions) main.append(questionBlock(q));
  view(main);
}

// --- home: the memory itself -------------------------------------------------
const PAGE = 50;

// Scope is shared by Memory and Sessions so switching pages keeps the lens you chose.
// "everyone" | "mine" | a teammate's email. Binary was the bug: you could read your own work or all
// of it, but never one person's — so "show me Jules's work" had to be attempted as a text search for
// the word "jules", which returns claims that MENTION him, written by someone else.
type Scope = "everyone" | "mine" | string;
// Default follows the two-product split: the SHARED views (Today, Memory) open on the team, because
// "what did the team do" and "what does the team know" are the questions they answer. The PRIVATE one
// (Improve) opens on you — its numbers are diagnostics about how someone worked and are nobody else's
// default view. `scopeChosen` makes that a default, not a rule: once the user picks, we never override.
let scope: Scope = "everyone";
let scopeChosen = false;
let teamCache: TeamRow[] | null = null; // owners for the picker; fetched once, reused across pages
let memQ = "";
let memType = "";
let memProject = "";
let memOffset = 0;

// The signed-in email, or "" when the gate has no verified identity (shared-token self-host).
// Without it there is no "mine" to scope TO, so the switch hides and everything reads as one corpus.
function myEmail(): string {
  return me?.email && me.email.includes("@") ? me.email : "";
}

// Your own email IS "mine". Team's name links set the scope to a raw address, and when that address is
// yours the switch used to light nothing at all: "Everyone" inactive, "Just mine" inactive, dropdown
// reading "Someone else…", over a page showing only your work. Normalise on the way in instead.
function normaliseScope(): void {
  const me = myEmail();
  if (me && scope === me) scope = "mine";
}

function scopedOwner(): string {
  if (scope === "everyone") return "";
  return scope === "mine" ? myEmail() : scope;
}

// Label for whoever is being shown right now — drives the page's one-line explanation.
function scopeName(): string {
  if (scope === "everyone") return "the team";
  if (scope === "mine") return "you";
  return scope.split("@")[0];
}

// Owners other than you, busiest first. Empty until /api/team has been fetched at least once.
function otherOwners(): TeamRow[] {
  const mine = myEmail();
  return (teamCache ?? []).filter((r) => r.user && r.user !== mine && r.user.includes("@"));
}

// The signature element: the whole pipeline as live, clickable counts.
function pipelineStrip(s: CorpusStats): HTMLElement {
  // href "" renders an inert stage — the on-disk count is the SERVER's own filesystem and has no page
  // to link to now that the disk browser is gone (it only ever listed the server box's own chats).
  const stage = (n: number, label: string, sub: string, href: string, tip: string) =>
    el(
      href ? "a" : "div",
      href ? { class: "stage", href, title: tip } : { class: "stage stage-inert", title: tip },
      el("span", { class: "stage-n" }, n.toLocaleString("en-US")),
      el("span", { class: "stage-label" }, label),
      el("span", { class: "stage-sub" }, sub),
    );
  const arrow = () => el("span", { class: "stage-arrow", "aria-hidden": "true" }, "→");
  return el(
    "section",
    { class: "pipeline-wrap", "aria-label": "pipeline overview" },
    el(
      "div",
      { class: "pipeline" },
      stage(s.stages.on_disk, "on disk", "raw chats", "", "session files under the server's own ~/.claude/projects"),
      arrow(),
      stage(
        s.stages.indexed,
        "extracted",
        s.stages.stale ? `${s.stages.stale} stale` : "mined for facts",
        "#/corpus",
        "sessions the analyzer has ingested and mined",
      ),
      arrow(),
      stage(
        s.totals.hidden_context,
        "facts",
        `${s.totals.entities} entities`,
        "#/corpus",
        "decisions, gotchas and constraints pulled out of transcripts",
      ),
      arrow(),
      stage(
        s.feedback.served_facts,
        "served",
        `to ${s.feedback.served_sessions} sessions`,
        "#/recall",
        "facts handed back to Claude Code when it asked for past context",
      ),
      arrow(),
      stage(
        s.feedback.flags,
        "flags",
        s.feedback.judge_labels > 0 ? `${s.feedback.judge_labels} 🤖 auto` : "your feedback",
        "#/labels",
        "gold / wrong / unimportant ratings that tune future recall, 🤖 = auto-labeled by the judge (haiku); your flags override",
      ),
    ),
  );
}

// --- live pipeline (home): sessions flowing through ingest, right now ---------
// PM may refine copy later — keep these strings findable.
const PROCESSING_EMPTY =
  "Nothing processing right now, sessions appear here live as teammates' clients ship them.";
const PROCESSING_HEAD = "Processing now";
const PROCESSING_FINISHED_HEAD = "Just finished";
// "queued" sits before "extracting": ingest queues instantly, the ETL picks it up. Without it here a
// queued row's stage indexOf() is -1 and every step renders inert (the "permanently idle pipeline" bug).
const PIPE_STEPS: PipelineStage[] = ["queued", "extracting", "indexing", "done"];
const PIPE_POLL_MS = 3000;
const PIPE_IDLE_EVERY = 5; // with nothing in flight, ask every 15s, not every 3s
const PIPE_RECENT_SHOWN = 6;
let homeTimer: number | undefined; // the Pipeline poll — cleared on every route change (no leaked intervals)

function stopHomePoll(): void {
  if (homeTimer !== undefined) window.clearInterval(homeTimer);
  homeTimer = undefined;
}

// Every recurring timer registers here. route() calls this on EVERY navigation: an interval that
// outlives its view keeps mutating a detached node forever, which is the bug stopHomePoll exists for.
let rotateTimer: number | undefined;

function stopTimers(): void {
  stopHomePoll();
  if (rotateTimer !== undefined) window.clearInterval(rotateTimer);
  rotateTimer = undefined;
}

const ROTATE_MS = 8000; // interviews said 3.2s was too fast; user-sims still couldn't finish at 6s
const ROTATE_FADE_MS = 200; // matches the opacity transition behind .is-out in style.css

// The one rotation primitive, shared by both heroes (Memory's standing card, Welcome's hero line):
// every ROTATE_MS fade `fades` out, hand the next index to `paint`, fade back in. `paint` is also what
// re-points the click target, so the target changes in the same step as the words on screen and cannot
// lag the visible phrase. Reduced motion never starts the timer at all — a CSS media query cannot see
// a setInterval that swaps text.
function rotateEvery(fades: HTMLElement, count: number, paint: (i: number) => void): void {
  if (count < 2 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let i = 0;
  // Own the handle locally. Sign-in paints Welcome TWICE (hash change, then boot's /api/me resolving)
  // and Memory re-renders on every keystroke in its search, so a shared module global let the second
  // rotator overwrite the first's id — and the first's next tick then cleared the SECOND, live, timer
  // and leaked itself. Clear whatever is running, then only ever clear OUR id.
  if (rotateTimer) window.clearInterval(rotateTimer);
  const mine = window.setInterval(() => {
    if (!document.body.contains(fades)) return window.clearInterval(mine); // belt: route() clears it too
    // Reading or aiming = hovering. A tick mid-hover either yanks the words away or swaps the click
    // target under the pointer (a sim clicked claim A and landed on claim B's session). Skip the tick.
    if (fades.matches(":hover")) return;
    i = (i + 1) % count;
    fades.classList.add("is-out");
    window.setTimeout(() => {
      paint(i);
      fades.classList.remove("is-out");
    }, ROTATE_FADE_MS);
  }, ROTATE_MS);
  rotateTimer = mine;
}

function onHome(): boolean {
  const h = routePath();                 // NOT the raw hash: "#/?owner=x" must still be Home
  return h === "" || h === "/";
}

function onPipeline(): boolean {
  return routePath() === "/pipeline";
}

function fmtElapsed(s: number): string {
  return s >= 90 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
}

function pipeRow(it: PipelineItem): HTMLElement {
  const row = el("div", { class: "pipe-row" });
  row.append(el("span", { class: "mono", title: it.session_id }, shortId(it.session_id)));
  row.append(ownerCell(it.owner));
  if (it.project) row.append(el("span", { class: "project" }, it.project));
  if (it.stage === "failed") {
    const fail = el("span", { class: "pipe-fail" }, "✗ failed");
    if (it.error) fail.append(el("span", { class: "pipe-err", title: it.error }, truncate(it.error, 90)));
    row.append(fail);
  } else {
    // 4 fixed steps; the current one pulses via CSS only — no per-row JS timers.
    // "received" is the sub-millisecond state before the queue write in the same POST; collapse it onto
    // "queued" so a rare in-between poll doesn't render an all-inert row (indexOf would be -1).
    const stage = it.stage === "received" ? "queued" : it.stage;
    const cur = PIPE_STEPS.indexOf(stage);
    const steps = el("span", { class: "pipe-steps", "aria-label": `stage: ${it.stage}` });
    PIPE_STEPS.forEach((s, i) => {
      if (i > 0) steps.append(el("span", { class: "pipe-sep", "aria-hidden": "true" }, "→"));
      const cls =
        it.stage === "done" || i < cur ? "pipe-step isdone" : i === cur ? "pipe-step cur" : "pipe-step";
      steps.append(el("span", { class: cls }, s));
    });
    row.append(steps);
    if (it.stage === "done") {
      row.append(el("span", { class: "pipe-ok", "aria-hidden": "true" }, "✓"));
      if (it.facts != null)
        row.append(
          el(
            "a",
            { class: "pipe-yield", href: `#/corpus/session/${encodeURIComponent(it.session_id)}` },
            `${it.facts} fact${it.facts === 1 ? "" : "s"} · ${it.entities ?? 0} entities`,
          ),
        );
    }
  }
  row.append(el("span", { class: "pipe-elapsed", title: "time since the request landed" }, fmtElapsed(it.elapsed_s)));
  return row;
}

function processingSection(p: PipelineData, showFinished: boolean): HTMLElement {
  const sec = el("section", { class: "pipe-live" }, el("h2", { class: "sec-head" }, PROCESSING_HEAD));
  if (p.inflight.length === 0) {
    sec.append(el("p", { class: "hint" }, PROCESSING_EMPTY));
  } else {
    const list = el("div", { class: "pipe-rows" });
    for (const it of p.inflight) list.append(pipeRow(it));
    sec.append(list);
  }
  if (showFinished && p.recent.length > 0) {
    sec.append(el("p", { class: "pipe-sub hint" }, PROCESSING_FINISHED_HEAD));
    const done = el("div", { class: "pipe-rows" });
    for (const it of p.recent.slice(0, PIPE_RECENT_SHOWN)) {
      const r = pipeRow(it);
      r.classList.add("pipe-finished");
      done.append(r);
    }
    sec.append(done);
  }
  return sec;
}

async function renderPipeline(): Promise<void> {
  stopHomePoll();
  let stats: CorpusStats | null;
  let recent: CorpusSessionSummary[];
  let pipe: PipelineData;
  try {
    [stats, recent, pipe] = await Promise.all([getCorpusStats(), getCorpusSessions(), getPipeline()]);
  } catch (err) {
    return showError("load the pipeline", err);
  }
  if (!onPipeline()) return; // navigated away while fetching — don't stomp the new view or start a poll
  const main = el("main", {});
  main.append(pageHead("Pipeline", "The pipeline at a glance: from every chat on disk to team knowledge."));
  main.append(el("p", { class: "hint" }, `${asOf(new Date().toISOString())}.`));
  main.append(pipelineStrip(stats));
  if (stats.costs && stats.costs.calls > 0) {
    const c = stats.costs;
    const steps = Object.entries(c.by_step)
      .map(([k, v]) => `${k} $${v.toFixed(2)}`)
      .join(" · ");
    main.append(
      el(
        "p",
        { class: "hint", title: `${c.in_tokens.toLocaleString()} tokens in · ${c.out_tokens.toLocaleString()} tokens out` },
        `${CSA_SPEND}: $${c.usd.toFixed(2)} across ${c.calls.toLocaleString()} calls (${steps}) · $${c.usd_24h.toFixed(2)} in the last 24h`,
      ),
    );
  }
  const banner = me?.role === "admin" ? stallBanner(pipe.extraction) : null;
  if (banner) main.prepend(banner);
  const showFinished = stats.stages.indexed > 0;
  const procWrap = el("div", {}, processingSection(pipe, showFinished)); // poll target — replaced in place every tick
  main.append(procWrap);
  if (stats.stages.indexed === 0) {
    main.append(
      el(
        "section",
        { class: "welcome" },
        el("h2", {}, "Welcome to CSA"),
        el(
          "p",
          {},
          "Nothing has been mined yet. Connect a machine and new Claude Code sessions will start flowing in.",
        ),
      ),
    );
  } else {
    main.append(el("h2", { class: "sec-head" }, "Recently mined"));
    const feed = el("div", { class: "feed" });
    for (const r of recent.slice(0, 8)) {
      feed.append(
        el(
          "div",
          { class: "feed-row" },
          el("span", { class: "feed-when" }, r.started_at ? fmtWhen(r.started_at) : "·"),
          el(
            "span",
            { class: "feed-text" },
            el(
              "a",
              { href: `#/corpus/session/${encodeURIComponent(r.session_id)}`, title: r.session_id },
              r.summary || shortId(r.session_id),
            ),
          ),
          el("span", { class: "feed-who" }, ownerCell(r.user).textContent ?? ""),
        ),
      );
    }
    main.append(feed);
    main.append(
      el("p", { class: "hint" }, "", el("a", { href: "#/corpus" }, "See everything extracted →")),
    );
  }
  view(main);
  // Poll while Home is active: redraw "Processing now" in place each tick (elapsed ticks with it —
  // ponytail: 3s granularity, no per-second timers). A transition out of in-flight (done/failed)
  // re-renders the whole Home, refreshing the strip numbers + Recently Mined via the same fetchers.
  let inflightIds = new Set(pipe.inflight.map((i) => i.session_id));
  let idleTicks = 0;
  homeTimer = window.setInterval(async () => {
    if (!onPipeline()) return stopHomePoll(); // belt: route() also clears this on navigation
    if (document.hidden) return;
    if (inflightIds.size === 0 && idleTicks++ % PIPE_IDLE_EVERY !== 0) return;
    let p: PipelineData;
    try {
      p = await getPipeline();
    } catch (err) {
      // A 401 is NOT transient: retrying it forever leaves a frozen page that never asks you to
      // sign in (and keeps hammering /api/pipeline every 3s). Stop the poll and show the login.
      if (err instanceof AuthError) {
        stopHomePoll();
        return renderLogin(err.message);
      }
      return; // genuinely transient (server restart, blip) — try again next tick
    }
    const ids = new Set(p.inflight.map((i) => i.session_id));
    const finished = [...inflightIds].some((id) => !ids.has(id));
    inflightIds = ids;
    if (finished) return void renderPipeline();
    procWrap.replaceChildren(processingSection(p, showFinished));
  }, PIPE_POLL_MS);
}

// --- memory (the only page) --------------------------------------------------
// The claims themselves, newest-useful first. Everything a design partner needs on day one is here:
// what was worked out, why, and by whom. The operator surfaces (pipeline, review queue, flag ledger,
// recall log) are still reachable — under Operator, where the person running CSA looks for them.

// Everyone / Just mine. Hidden when the gate has no verified email — there is no "mine" to mean.
function scopeSwitch(onPick: () => void): HTMLElement | null {
  const mine = myEmail();
  const others = otherOwners();
  if (!mine && others.length < 2) return null;   // nobody to choose between
  const pick = (): void => {
    track("scope", pageName(routePath()) || "memory", { scope: scope === "everyone" || scope === "mine" ? scope : "person" });
    onPick();
  };
  const wrap = el("div", { class: "scope-wrap" });

  const bar = el("div", { class: "scope", role: "group", "aria-label": "whose work to show" });
  const buttons: [Scope, string][] = mine ? [["everyone", "Everyone"], ["mine", "Just mine"]] : [["everyone", "Everyone"]];
  for (const [id, label] of buttons) {
    const active = scope === id;
    const b = el(
      "button",
      { class: `scope-btn${active ? " active" : ""}`, type: "button", "aria-pressed": String(active) },
      label,
    );
    b.addEventListener("click", () => {
      if (scope === id) return;
      scope = id;
      scopeChosen = true;
      pick();
    });
    bar.append(b);
  }
  wrap.append(bar);

  // ponytail: a native <select>. A teammate list is a list of options — that is what the element is for,
  // and it gets keyboard, search-as-you-type and mobile pickers with no code.
  if (others.length) {
    const sel = el("select", { class: "scope-pick", id: "scope-pick", name: "scope", "aria-label": "show one teammate's work" }) as HTMLSelectElement;
    const placeholder = el("option", { value: "" }, "One teammate…");
    sel.append(placeholder);
    for (const r of others) {
      const o = el("option", { value: r.user }, `${r.user.split("@")[0]} · ${r.facts.toLocaleString("en-US")} ${r.facts === 1 ? "fact" : "facts"}`);
      if (scope === r.user) o.setAttribute("selected", "selected");
      sel.append(o);
    }
    sel.value = others.some((r) => r.user === scope) ? (scope as string) : "";
    if (sel.value) sel.classList.add("active");
    sel.addEventListener("change", () => {
      scope = sel.value || "everyone";             // back to the placeholder = back to Everyone
      normaliseScope();                            // picking yourself is "Just mine", not a nameless third state
      scopeChosen = true;
      pick();
    });
    wrap.append(sel);
  }
  return wrap;
}

// The picker needs /api/team, which the fact/session fetch does not carry. Fetch it once, never block
// the page on it: a failure just means the picker is absent this render, not a broken screen.
async function ensureTeam(): Promise<void> {
  if (teamCache) return;
  try {
    teamCache = await getTeam();
  } catch {
    teamCache = [];
  }
}


async function renderSearchHome(): Promise<void> {
  const owner = scopedOwner();
  // Read the hash query NOW: route() overwrites hashQuery, and the first await below yields to it.
  if (hashQuery.get("view") === "graph") {
    // the Graph view carries its scope in the hash, so a bookmark reopens the same people
    scope = hashQuery.get("owner") || "everyone";   // no owner in the hash IS Everyone
    normaliseScope();
    scopeChosen = true;
    return renderMemoryGraph(scopedOwner(), hashQuery.get("w"));
  }
  let page;
  try {
    [page] = await Promise.all([
      getFacts({ q: memQ, type: memType, owner, offset: memOffset, limit: PAGE }),
      ensureTeam(),
    ]);
  } catch (err) {
    return showError("load memory", err);
  }
  // navigated away mid-fetch: the Graph view shares this route, so `view` is part of "am I still here"
  if (!onHome() || hashQuery.get("view") === "graph") return;
  const main = el("main", {});
  main.append(
    pageHead(
      "Memory",
      scope === "everyone"
        ? "What your team worked out, and why."
        : `What ${scopeName()} worked out, and why.`,
    ),
  );

  const redraw = (): void => {
    memOffset = 0;
    void renderHome();
  };
  const sw = scopeSwitch(redraw);

  const rows = page.rows;
  const fresh = page.total === 0 && !page.rules.length && !memQ && !memType;
  if (fresh) main.append(memoryEmpty());
  void stalledBanner().then((b) => b && main.prepend(b));

  const bar = el("div", { class: "toolbar toolbar-mem" });
  const controls = el("div", { class: "scope-wrap" }, memoryViewSwitch("list"));
  if (sw) controls.append(sw);
  bar.append(controls);
  main.append(bar);

  const chips = el("div", { class: "filterbar" });
  for (const t of FACT_TYPES) {
    if (t === "rule" && scopedOwner() && scope !== "mine") continue; // a rule belongs to a project, never to a teammate
    const active = memType === t;
    const chip = el(
      "button",
      { class: `chip chip-toggle${active ? " active chip-type" : ""}`, type: "button", "aria-pressed": String(active), title: TYPE_TIP[t] ?? "" },
      t,
    );
    chip.addEventListener("click", () => {
      memType = active ? "" : t;
      redraw();
    });
    chips.append(chip);
  }
  if (!fresh) main.append(chips);

  // Say which of the reader's words the corpus has never seen. The FTS ORs the terms and drops the
  // ones that match nothing, so a question containing an unknown noun still returns thousands of hits
  // ranked on its COMMON words — "how did we shard the Cassandra ring" answered with a CSS focus-ring
  // fact. Ranking cannot fix that; only naming the gap can. Three testers were each fooled by it.
  const miss = page.coverage?.missing || [];
  if (memQ && miss.length) {
    main.append(el("p", { class: "coverage-warn", role: "status" },
      el("b", {}, miss.length === 1 ? `Nothing here mentions “${miss[0]}”.` : `Nothing here mentions ${miss.map((m) => `“${m}”`).join(" or ")}.`),
      " " + (page.total
        ? `The ${page.total.toLocaleString("en-US")} results below match only your other words, so they may not answer the question.`
        : "")));
  }
  // The count belongs ABOVE the results, not after fifty cards — scale is the most useful honesty
  // signal on this page and it was the last thing anyone saw.
  if (page.total) {
    // page.rows, not `rows`: the claims lifted into the hero are still ON this page, and the pager
    // steps by `limit` regardless — counting the shortened list skipped 3 facts per page silently.
    const from = page.offset + 1, to = Math.min(page.offset + page.rows.length, page.total);
    const count = el("p", { class: "hint result-count" },
      `${from.toLocaleString("en-US")}-${to.toLocaleString("en-US")} of ${page.total.toLocaleString("en-US")}`
      + (page.total === 1 ? " fact" : " facts"));
    if (memQ) count.append(` matching “${memQ}” · `, clearSearch());
    main.append(count);
  }

  if (page.rules.length) main.append(rulesBlock(page.rules));
  if (page.total === 0) {
    if (!page.rules.length && !fresh) main.append(memoryEmpty());
  } else {
    const list = el("div", { class: "facts" });
    for (const { row, same } of foldSameClaim(rows)) list.append(factRowCard(row, { same, menu: true }));
    main.append(list);
    main.append(
      pager(page.offset, page.limit, page.total, (o) => {
        memOffset = o;
        void renderHome();
        window.scrollTo(0, 0);
      }),
    );
  }
  view(main);
}

async function renderHome(): Promise<void> {
  const form = el("form", { class: "search-form", role: "search" });
  const input = el("input", {
    class: "search-input", type: "search", name: "q", value: memQ,
    placeholder: "Search your team's knowledge", "aria-label": "Search team knowledge", autocomplete: "off",
  }) as HTMLInputElement;
  const submit = el("button", { class: "search-submit", type: "submit", "aria-label": "Search" }, searchGlyph());
  form.append(input, submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const q = input.value.trim();
    if (q) track("search", "memory");
    memOffset = 0;
    location.hash = memoryHash(q);
    if (q === memQ) void renderHome();
  });

  const main = el("main", { class: `search-page${memQ ? " has-query" : ""}` },
    el("h1", { class: "sr-only" }, "Search team knowledge"), form);
  if (!memQ) {
    main.append(el("p", { class: "search-prompt" }, "Search decisions, constraints, gotchas, and preferences from past work."));
    view(main);
    window.setTimeout(() => input.focus(), 0);
    return;
  }

  const status = el("p", { class: "search-status", role: "status" }, "Searching…");
  main.append(status);
  view(main);

  let page;
  try {
    [page] = await Promise.all([
      getFacts({ q: memQ, owner: scopedOwner(), project: memProject, offset: memOffset, limit: PAGE }),
      ensureTeam(),
    ]);
  } catch (err) {
    if (err instanceof AuthError) return renderLogin(err.message);
    status.className = "search-state error";
    status.textContent = "We couldn't reach the CSA server. Check your connection and try again.";
    return;
  }
  if (!onHome() || (hashQuery.get("q") ?? "") !== memQ) return;

  status.remove();
  const owners = [...new Set(page.rows.map((row) => row.user).filter((x): x is string => !!x))].sort();
  const projects = [...new Set(page.rows.map((row) => row.project).filter((x): x is string => !!x))].sort();
  if (owners.length > 1 || projects.length > 1) {
    const filters = el("div", { class: "search-filters", "aria-label": "Search filters" });
    if (owners.length > 1) {
      const person = el("select", { "aria-label": "Filter by person" }) as HTMLSelectElement;
      person.append(el("option", { value: "" }, "Any person"));
      for (const owner of owners) person.append(el("option", { value: owner }, owner));
      person.value = scopedOwner();
      person.addEventListener("change", () => { scope = person.value || "everyone"; memOffset = 0; void renderHome(); });
      filters.append(person);
    }
    if (projects.length > 1) {
      const project = el("select", { "aria-label": "Filter by project" }) as HTMLSelectElement;
      project.append(el("option", { value: "" }, "Any project"));
      for (const name of projects) project.append(el("option", { value: name }, name));
      project.value = memProject;
      project.addEventListener("change", () => { memProject = project.value; memOffset = 0; void renderHome(); });
      filters.append(project);
    }
    main.append(filters);
  }

  const missing = page.coverage?.missing ?? [];
  if (missing.length) main.append(el("p", { class: "search-state", role: "status" },
    `Nothing here mentions ${missing.map((term) => `“${term}”`).join(" or ")}. Results may match only the other words.`));
  if (!page.total) {
    main.append(el("section", { class: "search-empty" }, el("h2", {}, "No results"),
      el("p", {}, `CSA found no knowledge matching “${memQ}”. Try fewer or different words.`)));
    return;
  }

  main.append(el("p", { class: "result-count" }, `${page.total.toLocaleString("en-US")} ${page.total === 1 ? "result" : "results"}`));
  const list = el("div", { class: "facts search-results" });
  for (const { row, same } of foldSameClaim(page.rows)) list.append(factRowCard(row, { same }));
  main.append(list, pager(page.offset, page.limit, page.total, (offset) => {
    memOffset = offset;
    void renderHome();
    window.scrollTo(0, 0);
  }));
}

// An empty screen is an invitation to act — say which emptiness this is and what fixes it.
function memoryEmpty(): HTMLElement {
  if (memQ || memType) {
    const p = el("p", { class: "hint" }, scope === "everyone"
      ? "No facts match: "
      : `No facts from ${scopeName()} match: switch to Everyone, or `);
    if (memQ) p.append(clearSearch());
    if (memQ && memType) p.append(" or ");
    if (memType) {
      const all = el("button", { class: "linklike", type: "button" }, "show every type");
      all.addEventListener("click", () => { memType = ""; memOffset = 0; void renderHome(); });
      p.append(all);
    }
    p.append(".");
    return p;
  }
  if (scope === "mine")
    return el(
      "section",
      { class: "welcome" },
      el("h2", {}, "Nothing from you yet"),
      noModelNote() || machineAware(el("p", {}, "This browser session ships nothing. Connect the machine you code on and your sessions are mined a few minutes after each one ends.")),
      connectCta(),
      el("p", {}, "Or switch to Everyone to read what the rest of the team has already worked out."),
    );
  if (scope !== "everyone")
    return el(
      "section",
      { class: "welcome" },
      el("h2", {}, `Nothing from ${scopeName()} yet`),
      el("p", {}, "No facts have come out of their sessions yet. Switch to Everyone to read the rest of the team's."),
    );
  return el(
    "section",
    { class: "welcome" },
    el("h2", {}, "The memory is empty"),
    noModelNote() || machineAware(el("p", {}, "No machine is connected yet. One line on each teammate's laptop starts it, and decisions, gotchas and constraints appear here within minutes of a session ending.")),
    el("p", {}, RECALL_LINE),
    connectCta(),
  );
}

/** With no AI model set up nothing is read, so an empty Memory says that instead of "within minutes". */
function noModelNote(): HTMLElement | null {
  if (me?.model_ready !== false) return null;
  return el("p", { class: "stall-banner", role: "alert" }, el("b", {}, "No AI model is set up. "),
    "Sessions are not read until one is, so no facts appear here yet. ",
    me.role === "admin" ? el("a", { href: "#/admin/models" }, "Add one on AI Models.") : "Ask a workspace admin to add one on AI Models.");
}

/** An admin's empty workspace: the three steps to a first recall, each checked against live state. */
async function firstRunPanel(): Promise<HTMLElement> {
  const [prov, machines, org, cfg] = await Promise.all([
    getProviders().catch(() => null),
    getMachines().catch(() => []),
    getOrgMembers().catch(() => null),
    authConfig(),
  ]);
  const steps = firstRunSteps({
    model: prov?.ok === true && !prov.blocked && modelReady(prov.providers.length, prov.default),
    machine: machines.length > 0,
    teammate: !!org?.members.some((m) => m.active && !m.service && m.email && m.email !== myEmail()),
  });
  const list = el("ol", { class: "first-run" });
  for (const s of steps) {
    const li = el("li", { class: s.done ? "done" : "" },
      el("a", { href: s.href, class: "first-run-title" }, s.title), el("p", {}, s.text));
    if (s.href === "#/connect" && !s.done && cfg.base_url) li.append(cmdLine(installLine(cfg.base_url)));
    list.append(li);
  }
  return el("section", { class: "welcome first-run-card" },
    el("h2", {}, FIRST_RUN.title), el("p", { class: "hint" }, FIRST_RUN.explain),
    list, el("p", { class: "hint" }, FIRST_RUN.foot));
}

function clearSearch(): HTMLElement {
  return el("a", { href: "#/", class: "clear-q" }, "clear the search");
}

/** Swaps a "nothing is connected" paragraph for the truth once the caller's own machines load. */
function machineAware(p: HTMLElement): HTMLElement {
  void getMachines().then((ms) => {
    if (ms.length) p.textContent = MACHINE_WAITING;
  }).catch(() => undefined);
  return p;
}

/** Shown only once the reader's own machines load and there are none: the call to connect is for
 *  readers who ship nothing. */
function machineGate(node: HTMLElement): HTMLElement {
  node.hidden = true;
  void getMachines().then((ms) => { node.hidden = ms.length > 0; }).catch(() => undefined);
  return node;
}

/** Admins only: why extraction is not running, from the pipeline status; null for everyone else. */
async function stalledBanner(): Promise<HTMLElement | null> {
  if (!me || me.role !== "admin") return null;
  const p = await getPipeline().catch(() => null);
  return p ? stallBanner(p.extraction) : null;
}

function stallBanner(x: PipelineData["extraction"]): HTMLElement | null {
  const text = stallNotice(x);
  return text ? el("p", { class: "stall-banner", role: "alert" }, text) : null;
}

/** The install line and the way to the page that explains it. Rendered wherever an empty screen's
 *  honest answer is "nothing is connected": the one-liner needs this server's own base, so it is
 *  fetched, and the link stands on its own until it arrives. */
function connectCta(): HTMLElement {
  const btn = el("a", { href: "#/connect", class: "btn-primary" }, CONNECT.nav);
  const code = el("code", { class: "connect-line" });
  code.hidden = true;
  const card = el("section", { class: "connect-cta card" },
    el("h3", { class: "sec-head" }, "Send sessions to CSA"),
    el("p", { class: "set-help" }, "This browser reads the memory; it ships nothing. Connect the machine you code on. For an agent or a pipeline, an admin mints a token on Service Tokens."),
    code,
    el("div", { class: "connect-cta-actions" }, btn));
  void authConfig().then((cfg) => {
    if (cfg.base_url) { code.textContent = installLine(cfg.base_url); code.hidden = false; }
  });
  return card;
}

// ===================================================================================================
// --- Connect a machine: the one page that turns a reader into a sender. Reached from the account
// menu and from Memory's empty state. Copy and the asset table live in connect.ts (tested).
// ===================================================================================================

/** One shell line with a Copy button. The clipboard call can reject (denied permission, no secure
 *  context), so the button says so rather than lying about having copied. */
function cmdLine(text: string): HTMLElement {
  const btn = el("button", { class: "btn btn-small copy-btn", type: "button",
                             "aria-label": `Copy: ${text}` }, CONNECT.copy);
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = CONNECT.copied;
    } catch {
      btn.textContent = "Select and copy";
    }
    setTimeout(() => (btn.textContent = CONNECT.copy), 2000);
  });
  return el("div", { class: "cmd" }, el("code", {}, text), btn);
}

function connectInstall(base: string): HTMLElement {
  const sec = el("section", { class: "admin-section" }, el("h3", {}, CONNECT.installHead));
  if (!base) {
    sec.append(el("p", { class: "hint" }, CONNECT.noBase));
    return sec;
  }
  sec.append(
    cmdLine(installLine(base)),
    el("p", { class: "hint" }, CONNECT.installNote),
    el("p", { class: "hint" }, CONNECT.installedAlready + " "),
    cmdLine(setupLine(base)),
  );
  return sec;
}

// What the client sends, before anyone downloads it. The authorize page carries the same notice, but
// it needs a nonce from `setup` to reach, so a reviewer would have had to run the binary to find out
// what running the binary does.
function connectCollects(): HTMLElement {
  const sec = el("section", { class: "admin-section" }, el("h3", {}, CONNECT.collectHead));
  const list = el("ul", {});
  for (const line of CONNECT.collect) list.append(el("li", {}, line));
  sec.append(list, el("p", { class: "hint" }, CONNECT.collectFoot));
  return sec;
}

function connectMachines(machines: Machine[] | null): HTMLElement {
  const sec = el("section", { class: "admin-section" }, el("h3", {}, CONNECT.machinesHead));
  if (machines === null) {
    sec.append(el("p", { class: "hint" }, CONNECT.machinesFailed));
    return sec;
  }
  if (!machines.length) {
    sec.append(el("p", { class: "hint" }, CONNECT.machinesNone));
  } else {
    const list = el("ul", { class: "machines" });
    for (const m of machines)
      list.append(el("li", {},
        el("span", { class: "machine-host" }, m.hostname || "unnamed machine"),
        el("span", { class: "when" }, machineLine(m, fmtDate))));
    sec.append(list);
  }
  sec.append(el("p", { class: "hint" }, CONNECT.machinesFoot));
  if (me?.role === "admin") sec.append(el("p", { class: "hint" }, CONNECT.adminNote));
  return sec;
}

function connectDownloads(base: string, sums: Record<string, string>): HTMLElement {
  const sec = el("section", { class: "admin-section" },
    el("h3", {}, CONNECT.downloadHead),
    el("p", { class: "hint" }, CONNECT.downloadNote),
    // install.sh says this in its own comments, where only the people who pipe it into a shell ever
    // read it. The reviewer who refuses to do that is exactly who needs to know.
    el("p", { class: "hint" }, CONNECT.trust));
  const table = el("table", { class: "sessions downloads" },
    el("thead", {}, el("tr", {}, el("th", {}, "Build"), el("th", {}, "SHA-256"))));
  const body = el("tbody", {});
  for (const a of ASSETS)
    body.append(el("tr", {},
      el("td", {}, el("a", { href: `/latest/${a.name}` }, a.label),
        el("span", { class: "hint asset-name" }, a.name)),
      el("td", { class: "mono sha" }, sums[a.name] ?? CONNECT.notPublished)));
  table.append(body);
  sec.append(table);
  sec.append(Object.keys(sums).length
    ? el("p", { class: "hint" }, el("a", { href: "/latest/checksums.txt" }, CONNECT.checksums))
    : el("p", { class: "hint" }, CONNECT.noChecksums));
  if (base) {
    sec.append(el("h4", {}, CONNECT.macHead),
      el("p", { class: "hint" }, `${CONNECT.unsigned} ${CONNECT.macNote}`));
    for (const line of macSteps(ASSETS[0].name, base)) sec.append(cmdLine(line));
    sec.append(el("p", { class: "hint" }, CONNECT.mismatch),
      el("p", { class: "hint" }, CONNECT.versionCheck), cmdLine(versionLine(ASSETS[0].name)));
    sec.append(el("h4", {}, CONNECT.winHead));
    sec.append(cmdLine(winSetupLine(base)), el("p", { class: "hint" }, CONNECT.winNote));
  }
  return sec;
}

async function renderConnect(): Promise<void> {
  const [cfg, machines, sums] = await Promise.all([
    authConfig(),
    getMachines().catch((err) => {
      if (err instanceof AuthError) throw err;
      return null;
    }),
    getChecksums(),
  ]).catch((err) => {
    showError("load this page", err);
    return null as never;
  });
  if (!cfg || routePath() !== "/connect") return;   // navigated away mid-fetch
  const base = cfg.base_url ?? "";
  const head = pageHead(CONNECT.title, CONNECT.explain);
  if (cfg.client_published)
    head.append(el("p", { class: "hint" }, `${CONNECT.since} ${fmtDate(cfg.client_published)}.`));
  view(el("main", { class: "connect" },
    head,
    connectInstall(base),
    connectMachines(machines),
    connectCollects(),
    connectDownloads(base, parseChecksums(sums)),
  ));
}

// Reading one person's memory is the whole point of the Team page — make the name the way in.
// (Scope lives in module state, so this sets it and navigates; no query-param router needed.)
function personLink(user: string): HTMLElement {
  const a = el("a", { href: "#/", title: `read what ${ownerName(user)} learned` }, user);
  a.addEventListener("click", () => {
    scope = user;
    normaliseScope();                              // clicking your OWN name lit no control at all
    scopeChosen = true;
    memOffset = 0;
    memQ = "";
    memType = "";
    // href="#/" only navigates from ANOTHER page: assigning the hash it already has fires no
    // hashchange, so from Memory itself this link would set the scope and repaint nothing — the same
    // "the click did nothing" failure the id-less session href used to cause.
    if (onHome()) void renderHome();
  });
  return a;
}

// --- welcome: the first-run screen -------------------------------------------------
// Its whole job is the reaction "how did it know that?". That needs the fact to be true, specific,
// and FORGOTTEN — which is why the cold-start regime ranks oldest-first (see apiserver.onboarding).
// Lives behind the auth gate like every other corpus view.

function receiptLine(c: OnboardCard): HTMLElement {
  const bits: (Node | string)[] = [];
  if (c.user) bits.push(ownerCell(c.user));
  if (c.learned_at) bits.push(el("span", { class: "when" }, c.learned_at));
  // Only claim reuse when a `served` row backs it: the server's one definition, a fact served to
  // someone other than its author. Each id below opens a receiving session the corpus holds.
  if (c.reused > 0) {
    const label = reuseClaim(c.reused, c.people);
    if (!c.reused_in.length) {
      bits.push(el("span", { class: "receipt-reuse" }, label));
    } else {
      // The phrase itself opens the receipts. A separate "check" button next to it read as UI debris
      // and broke the byline mid-sentence; the underline on the claim says "this is checkable" without
      // adding a control, and the thing you click is the thing you are verifying.
      const show = el("button", { class: "receipt-reuse as-link", type: "button",
        "aria-expanded": "false", title: "show the sessions that recalled this" }, label);
      const list = el("span", { class: "receipt-list", hidden: "hidden" });
      for (const sid of c.reused_in)
        list.append(el("a", { href: `#/corpus/session/${encodeURIComponent(sid)}`, title: sid },
          shortId(sid)));
      if (c.reused > c.reused_in.length)
        list.append(el("span", { class: "hint", title: "older, or never shipped to CSA, so there is nothing to open" },
          `+${c.reused - c.reused_in.length} more`));
      show.addEventListener("click", () => {
        const open = !list.hasAttribute("hidden");
        list.toggleAttribute("hidden", open);
        show.setAttribute("aria-expanded", String(!open));
      });
      bits.push(show, list);
    }
  }
  else
    bits.push(el("span", { class: "hint" }, "not recalled yet, you are the first to see it again"));
  if (c.project) bits.push(el("span", { class: "project" }, c.project));
  return el("div", { class: "receipt" }, ...bits);
}

function welcomeCard(c: OnboardCard, lead: boolean): HTMLElement {
  const card = el("article", { class: lead ? "wcard wcard-lead" : "wcard" });
  card.append(el("p", { class: lead ? "wclaim wclaim-lead" : "wclaim" }, c.claim));
  if (c.evidence && lead) card.append(el("p", { class: "wevidence" }, c.evidence));
  card.append(receiptLine(c));
  card.append(el("a", { class: "hint", href: sessionHref(c.session_id, c.ref) },
    "open the session →"));
  return card;
}

// The rotating half of the hero. Real claims, cycling in place (see rotateEvery for the timer).
// `targets` are ready-made hrefs (session + fact ref), so the click lands focused on the claim.
function rotatingHero(fixed: string, phrases: string[], targets: string[] = []): HTMLElement {
  const wrap = el("section", { class: "hero" });
  wrap.append(el("h1", { class: "hero-fixed" }, fixed));
  const slot = el("div", { class: "hero-slot" });
  // Clickable: the rotating line was a truncated copy of a card just below it — the biggest thing on
  // the page and the only one that led nowhere. It now opens the session the claim came from, which is
  // both the obvious affordance and the quickest way to show what a single session leaves behind.
  const line = el("span", { class: "hero-active" }, phrases[0] ?? "");
  let at = 0;
  const go = () => {
    const href = targets[at];
    if (href) location.hash = href;
  };
  if (targets.length) {
    line.classList.add("is-link");
    line.setAttribute("role", "link");
    line.setAttribute("tabindex", "0");
    line.setAttribute("title", "open the session this came from");
    line.addEventListener("click", go);
    line.addEventListener("keydown", (e) => {
      if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") { e.preventDefault(); go(); }
    });
  }
  slot.append(line, el("span", { class: "hero-caret", "aria-hidden": "true" }, ""));
  wrap.append(slot);
  rotateEvery(line, phrases.length, (i) => {
    line.textContent = phrases[i];
    at = i;                         // the click target must follow the visible phrase, not lag it
  });
  return wrap;
}

// The hero is a teaser, not the claim — the full text is in the card directly below it. Cut at the
// first natural clause boundary so it reads as a phrase rather than as a sentence chopped mid-word,
// and fall back to a word boundary. CSS keeps it to one line so a long one cannot shift the layout.
function shorten(claim: string, n = 58): string {
  // Take the first sentence ONLY if it is a phrase rather than a fragment: real claims start "No." and
  // "PR #322." often enough that an unguarded split turns the hero into the single word "No.".
  // (split on [.;] rather than a lookbehind: this was the bundle's only lookbehind, a parse-time
  // SyntaxError on older Safari, which blanks every page rather than just this one.)
  const first = (claim || "").split(/[.;]\s/)[0] || "";
  let t = first.length >= 25 ? first : (claim || "");
  const clause = t.search(/[,(—:]|\s-\s/);
  if (clause > 24) t = t.slice(0, clause);
  t = t.trim();
  if (t.length <= n) return t;
  const cut = t.slice(0, n);
  const sp = cut.lastIndexOf(" ");
  return `${(sp > 30 ? cut.slice(0, sp) : cut).trimEnd()}…`;
}

async function renderWelcome(): Promise<void> {
  let d;
  try {
    d = await getOnboarding("", 3);
  } catch (err) {
    return showError("load your first run", err);
  }
  if (routePath() !== "/welcome") return;   // navigated away mid-fetch
  const main = el("main", { class: "welcome-page" });
  const m = d.metrics;
  void stalledBanner().then((b) => b && main.prepend(b));

  if (d.cards.length === 0) {
    // Empty corpus: say what will happen and when, and never pretend to know something.
    main.append(pageHead("Nothing to show you yet",
      "CSA reads your finished Claude Code sessions and keeps the decisions and gotchas in them. It has not seen any yet."));
    if (me?.role === "admin") {
      const steps = await firstRunPanel();
      if (routePath() !== "/welcome") return;
      main.append(steps);
      return view(main);
    }
    main.append(el("section", { class: "welcome" },
      el("h2", {}, "Finish a session and come back"),
      el("p", {}, "A session is read a few minutes after it ends. Once two or three have landed, this page shows you the things worth remembering, starting with the ones you have already forgotten."),
      el("p", { class: "hint" }, "Already have months of sessions on this machine? Ask whoever set CSA up to backfill them. It is one command on the server, and it costs real money, so it is deliberately not automatic.")));
    main.append(connectCta());
    return view(main);
  }

  // Solo users get a personal frame. "This team already knows why" advertises an experience someone
  // working alone cannot have, turning their first impression into an ad for a missing feature.
  const fixed = d.solo ? "You already worked this out." : "This team already knows why";
  const cards = foldSameClaim(d.cards).map((g) => g.row);   // one claim learned twice is one card
  main.append(rotatingHero(fixed, cards.map((c) => shorten(c.claim)),
                           cards.map((c) => sessionHref(c.session_id, c.ref))));

  // Say WHOSE these are. A newcomer who has shipped nothing still gets the team's corpus here (the
  // personal ranking falls back when they have no projects yet), so unlabelled numerals read as the
  // reader's own history — 13,805 things "you" learned on day one. The line only appears when the
  // corpus genuinely is someone else's work.
  if (!d.solo && d.mine === 0 && m.sessions > 0) {
    main.append(el("p", { class: "explain hero-whose" },
      `Nothing of yours yet. Everything below is your ${m.people === 1 ? "teammate's" : `${m.people} teammates'`} work.`));
  }

  const strip = el("section", { class: "wmetrics" });
  const cell = (n: number, label: string, title: string, href = "") => {
    const kids = [el("b", {}, n.toLocaleString("en-US")), el("span", {}, label)];
    return href ? el("a", { class: "wmetric", title, href }, ...kids) : el("div", { class: "wmetric", title }, ...kids);
  };
  strip.append(cell(m.facts, m.facts === 1 ? "fact" : "facts",
    `decisions, gotchas and constraints from ${m.sessions.toLocaleString("en-US")} finished sessions`, "#/"));
  strip.append(cell(m.reused, REUSED_TILE.label, REUSED_TILE.title));
  strip.append(cell(m.people, m.people === 1 ? "contributor" : "contributors", "people who shipped a session, all time", "#/team"));
  if (m.agents) strip.append(cell(m.agents, m.agents === 1 ? "agent" : "agents", "service tokens that shipped a session, all time, counted apart from people", "#/team"));
  main.append(strip);
  main.append(machineGate(el("section", { class: "wconnect" }, el("p", { class: "explain" }, RECALL_LINE), connectCta())));

  main.append(el("p", { class: "wlede" },
    el("b", {}, String(cards.length)),
    d.solo
      ? " facts you already worked out and probably forgot"
      : " facts this team learned that you have not seen yet"));
  for (const [i, c] of cards.entries()) main.append(welcomeCard(c, i === 0));

  main.append(el("p", { class: "hint wfoot" },
    d.frame === "cold"
      ? "Oldest first, the ones most likely forgotten."
      : "Most reused first."));
  view(main);
}

// --- today: the standup view ------------------------------------------------------
// One day, everyone, what landed. Names people on purpose — that is the question it answers
// ("what did Jules work on yesterday") — but carries no durations, costs, or retry counts. The
// moment those appear here it stops being a standup board and becomes a performance dashboard.

function dayLabel(d: string, days: string[]): string {
  const newest = days[0];
  if (d === newest) return "Today";
  if (days[1] === d) return "Yesterday";
  return new Date(d + "T12:00:00Z").toLocaleDateString(undefined,
    { weekday: "long", month: "short", day: "numeric" });
}

let showTrivial = false;   // ping/pong sessions: counted, folded, one click away
let currentDay = "";       // so the trivial toggle re-renders the day you are actually on
let hashQuery = new URLSearchParams();   // the current route's query string, set by route()

// Today's filters and page size are URL state. `todayHash` rebuilds the canonical hash so that every
// chip, pill and "load more" is an ordinary link-style navigation rather than hidden local state.
function todayHash(date: string, over: Record<string, string> = {}): string {
  const q = new URLSearchParams(hashQuery);
  for (const [k, v] of Object.entries(over)) {
    if (v) q.set(k, v);
    else q.delete(k);
  }
  q.delete("date");
  const qs = q.toString();
  return `#/today${date ? `/${date}` : ""}${qs ? `?${qs}` : ""}`;
}

function personBlock(p: ActivityData["people"][number]): HTMLElement {
  const card = el("article", { class: "day-person" });
  const head = el("div", { class: "day-person-head" });
  const name = p.user.includes("@") ? p.user.split("@")[0] : p.user;
  head.append(el("span", { class: "day-who", title: p.user }, name));
  head.append(el("span", { class: "hint" }, `${p.sessions} ${p.sessions === 1 ? "session" : "sessions"}`));
  for (const [proj, n] of p.projects.slice(0, 3)) {
    // The chip IS the filter — clicking a project narrows the day to it. Filtering happens on the
    // server so the header totals and the rows can never disagree.
    const on = hashQuery.get("project") === proj;
    const pc = el("button", { class: "chip as-btn", type: "button",
      title: on ? `showing only ${proj}, click to clear` : `show only ${proj} (${n} today)`,
      "aria-pressed": String(on) }, proj);
    // second click on the active chip clears it; `offset` resets or you land on an empty page
    pc.addEventListener("click", () => {
      location.hash = todayHash(currentDay, { project: on ? "" : proj, offset: "" });
    });
    head.append(pc);
  }
  for (const pr of p.prs.slice(0, 4))
    head.append(el("a", { class: "chip chip-pr", href: pr.url, target: "_blank", rel: "noreferrer" }, pr.tag));
  if (p.prs.length > 4)          // the cap used to silently drop the rest: header said "5 PRs", row showed 4
    head.append(el("span", { class: "chip", title: p.prs.slice(4).map((x) => x.tag).join(", ") },
      `+${p.prs.length - 4} more`));
  if (p.prod > 0)
    head.append(el("span", { class: "chip chip-prod", title: "a session in this day reached production" },
      `⚠ ${p.prod} touched production`));
  card.append(head);

  const list = el("div", { class: "day-did" });
  // A session that produced no knowledge is real activity but not standup material ("user sent ping,
  // assistant replied pong"). Fold, never drop — the count stays visible and one click restores them.
  const worth = p.did.filter((d) => d.facts > 0);
  const shown = showTrivial ? p.did : worth;
  for (const d of shown) {
    const row = el("div", { class: "day-row" });
    row.append(el("span", { class: "when" }, d.at));
    const txt = el("span", { class: "day-text" });
    txt.append(el("a", { href: `#/corpus/session/${encodeURIComponent(d.session_id)}` },
      d.summary || shortId(d.session_id)));
    row.append(txt);
    if (d.prod) row.append(el("span", { class: "chip chip-prod", title: PROD_TIP }, "prod"));
    if (d.branch) {
      const br = d.branch;                 // capture: TS cannot narrow d.branch inside the callback
      const bon = hashQuery.get("branch") === br;
      const bc = el("button", { class: "chip chip-branch as-btn", type: "button",
        "aria-pressed": String(bon),
        title: bon ? `showing only ${br}, click to clear` : `git branch ${br}, click to filter` },
        br);
      bc.addEventListener("click", () => {
        location.hash = todayHash(currentDay, { branch: bon ? "" : br, offset: "" });
      });
      row.append(bc);
    }
    list.append(row);
  }
  card.append(list);
  if (p.trivial > 0) {
    const b = el("button", { class: "day-fold", type: "button" },
      showTrivial ? `hide ${p.trivial} trivial` : `+ ${p.trivial} trivial ${p.trivial === 1 ? "session" : "sessions"}`);
    b.addEventListener("click", () => {
      showTrivial = !showTrivial;
      void renderToday(currentDay);
    });
    card.append(b);
  }
  return card;
}

async function renderToday(date: string): Promise<void> {
  currentDay = date;
  let data;
  try {
    data = await getActivity(date, scopedOwner(), {
      project: hashQuery.get("project") || "",
      branch: hashQuery.get("branch") || "",
      limit: hashQuery.get("limit") || "",
      offset: hashQuery.get("offset") || "",
    });
  } catch (err) {
    return showError("load the day", err);
  }
  if (!routePath().startsWith("/today")) return; // navigated away mid-fetch
  const main = el("main", {});
  if (!data.date) {
    main.append(pageHead("Today", "What the team worked on."));
    main.append(el("section", { class: "welcome" }, el("h2", {}, "Nothing here yet"),
      el("p", {}, "Sessions appear here as teammates finish them.")));
    return view(main);
  }

  const i = data.days.indexOf(data.date);
  const nav = el("div", { class: "day-nav" });
  const jump = (d: string | undefined, label: string) => {
    const b = el("button", { class: "btn", type: "button" }, label) as HTMLButtonElement;
    b.disabled = !d;
    if (d) b.addEventListener("click", () => { location.hash = todayHash(d, { offset: "" }); });
    return b;
  };
  nav.append(jump(data.days[i + 1], "← Earlier"));   // days[] is newest-first, so +1 is further back
  nav.append(el("span", { class: "day-date" }, `${dayLabel(data.date, data.days)} · ${data.date}`));
  nav.append(jump(i > 0 ? data.days[i - 1] : undefined, "Later →"));
  const t = data.totals;
  // "3 of 5" not "3 people" — the day's headcount alone reads as the size of the team, so a quiet
  // day looks like attrition. The denominator is whoever shipped in the last 14 days.
  nav.append(el("span", { class: "hint day-tot" },
    `${t.people} of ${t.team} ${t.team === 1 ? "person" : "people"} · ${t.sessions} sessions` +
    (t.prs ? ` · ${t.prs} ${t.prs === 1 ? "PR" : "PRs"}` : "")
    + (t.prod ? ` · ${t.prod} touched production` : "")));
  // ...and name the difference, on the page. A bare "3 of 5" cannot distinguish "two people are on
  // leave" from "two people's machines stopped shipping", and a standup lead should not have to page
  // back a week and visit another tab to find out which. Last-active is the tell.
  if (data.quiet?.length)
    nav.append(el("span", { class: "hint day-quiet" },
      "no sessions: " + data.quiet.map((q) => `${q.user.split("@")[0]} (last ${q.last})`).join(", ")));
  // One pill per active filter, each removable. Rendered from what the SERVER echoed back, so a pill
  // can never claim a filter that was not actually applied.
  for (const [key, label] of [["project", "project"], ["branch", "branch"]] as const) {
    const v = data.filters?.[key] || "";
    if (!v) continue;
    const pill = el("button", { class: "btn day-clear", type: "button",
      title: `stop filtering by ${label}` }, `${label}: ${v} ✕`);
    pill.addEventListener("click", () => {
      location.hash = todayHash(data.date || "", { [key]: "", offset: "" });
    });
    nav.append(pill);
  }

  main.append(pageHead("Today", "Who worked on what, and what landed."));
  const sw = scopeSwitch(() => void renderToday(date));
  if (sw) main.append(el("div", { class: "toolbar toolbar-mem" }, sw));
  main.append(nav);
  for (const p of data.people) main.append(personBlock(p));
  // A busy day is paged, and says so. Silently rendering the first 200 of 340 while the header reads
  // "340 sessions" would be the same class of lie as the PR chip cap that claimed 5 and drew 4.
  const pg = data.page;
  if (pg && pg.returned < pg.total) {
    const shown = pg.offset + pg.returned;
    const foot = el("div", { class: "day-nav day-page" },
      el("span", { class: "hint" }, `showing ${pg.offset + 1}-${shown} of ${pg.total} sessions`));
    const more = el("button", { class: "btn", type: "button" }, "Show more");
    more.addEventListener("click", () => {
      // Raise the window rather than append a page: re-rendering from one request keeps the
      // person grouping and the day totals consistent, and the URL stays the whole truth.
      location.hash = todayHash(data.date || "", { limit: String(Math.min(pg.limit + 200, 1000)) });
    });
    foot.append(more);
    main.append(foot);
  }
  view(main);
}

// --- self improvement -------------------------------------------------------------
// The third observer: what each FINISHED session could have done cheaper, in fewer turns, or with
// less waiting. Every number is counted server-side from the transcript (analyzer/improve.py) —
// nothing here is a model's opinion, so a finding can be checked against the session it came from.

const KIND_LABEL: Record<Finding["kind"], string> = {
  cost: "cost",
  turns: "turns",
  time: "time",
};

function metricChips(r: ImproveRow): HTMLElement {
  const bar = el("div", { class: "fact-meta" });
  // Labelled "API" on purpose: on a Claude subscription these tokens are covered by the plan, so
  // this is the pay-as-you-go equivalent — a size signal, not money anyone was charged.
  bar.append(el("span", {
    class: "chip chip-cost",
    title: r.cost_usd === null ? `Not priced: ${r.model || "this model"} has no list price in CSA.`
      : `${SESSION_COST}: what this session's tokens would cost at ${r.model} pay-as-you-go rates. On a subscription they are covered by the plan.`,
  }, `${fmtCost(r.cost_usd)} API`));
  // NOT n_asst: that counts transcript LINES, and each tool_use block is its own line, so it reads
  // ~2x the number of real replies. Tokens are the checkable quantity behind the cost figure.
  bar.append(el("span", { class: "chip",
    title: `${r.n_user} messages from you · ${fmtTok(r.in_tokens)} in · ${fmtTok(r.out_tokens)} out · `
      + `${fmtTok(r.cache_read_tokens)} cached reads · ${fmtTok(r.cache_write_tokens)} cache writes` },
    `${r.tool_calls} tool calls`));
  if (r.wall_s) {
    const active = Math.max(0, r.wall_s - r.idle_s);
    // Show the split, don't hide it in a tooltip. "4h 44m" as the card's most prominent number reads as
    // five hours of work when 4h 1m of it was waiting — and on a session that big no time finding fires,
    // so the tooltip was the only place the truth appeared. Say it inline when most of the span was idle.
    bar.append(el("span", { class: "chip", title: `${fmtDur(active)} active, ${fmtDur(r.idle_s)} idle` },
      r.idle_s > active ? `${fmtDur(active)} active of ${fmtDur(r.wall_s)}` : fmtDur(r.wall_s)));
  }
  const waste = (r.dup_calls || 0) + (r.reread_calls || 0) + (r.tool_errors || 0);
  if (waste > 0)
    bar.append(el("span", { class: "chip chip-waste", title: `${r.dup_calls} repeated · ${r.reread_calls} re-read · ${r.tool_errors} failed` },
      `${waste} avoidable`));
  return bar;
}

function improveCard(r: ImproveRow, settleMinutes: number): HTMLElement {
  const card = el("article", { class: "fact improve-card" });
  const head = el("div", { class: "fact-meta" });
  if (r.project) head.append(el("span", { class: "project" }, r.project));
  head.append(ownerCell(r.user));
  if (r.ended_at) head.append(el("span", { class: "when" }, fmtWhen(r.ended_at)));
  card.append(head);
  card.append(el("p", { class: "claim" }, r.summary || shortId(r.session_id)));
  card.append(metricChips(r));

  if (!r.settled) {
    // Deliberately no advice: the session is still being added to, so any finding would be about a
    // partial transcript and would change under the reader.
    card.append(el("p", { class: "hint" },
      `Still open, reviewed once it has been quiet for ${settleMinutes} minutes.`));
  } else if (r.findings.length === 0) {
    card.append(el("p", { class: "hint" }, "Nothing stood out. No repeated calls, no failed tools, no long idle stretches."));
  } else {
    const list = el("div", { class: "findings" });
    for (const f of r.findings) {
      const item = el("div", { class: `finding finding-${f.kind}` });
      item.append(el("div", { class: "finding-head" },
        el("span", { class: `chip chip-${f.kind}` }, KIND_LABEL[f.kind]),
        el("span", { class: "finding-title" }, f.headline)));
      // The reasoning, in the order a reader needs it: evidence, inference, instruction. Labelled so
      // the "why" is never mistaken for the "what" — it is the step you would argue with.
      item.append(el("p", { class: "finding-step" }, el("span", { class: "step-k" }, "Saw"), f.saw));
      item.append(el("p", { class: "finding-step" }, el("span", { class: "step-k" }, "Why"), f.why));
      item.append(el("p", { class: "finding-step finding-do" }, el("span", { class: "step-k" }, "Do"), f.do));
      list.append(item);
    }
    card.append(list);
  }
  card.append(el("a", { class: "hint", href: `#/corpus/session/${encodeURIComponent(r.session_id)}` }, "open the session →"));
  return card;
}

// What a future session on this project should be told. This is the compounding part: one session's
// finding is an anecdote, the same finding across sessions is a property of the project itself.
// Tokens run to tens of millions; a raw integer there is unreadable and unmemorable.
function fmtTok(n: number): string {
  if (!n) return "0";
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}

function projectRollup(projects: ProjectRollup[], reviewed: number): HTMLElement {
  const wrap = el("section", { class: "rollup" });
  wrap.append(el("h2", { class: "sec-head" }, "What these projects should do differently"));
  wrap.append(el("p", { class: "explain" },
    "Patterns that showed up in more than one session. Written as instructions so the next person, or the next coding session, can act on them."));
  // A lesson has to recur (3+ sessions AND 30% of the project's reviewed sessions) before it is printed
  // as "what this project should do differently" — a 1-off is not a pattern. Say so when nothing clears
  // the bar yet, because the alternative shipped a heading with an empty body under it and read as broken.
  const withLessons = projects.filter((p) => p.lessons.length);
  if (!withLessons.length) {
    wrap.append(el("p", { class: "empty" },
      reviewed
        ? `Nothing has recurred often enough yet, ${reviewed} ${reviewed === 1 ? "session has" : "sessions have"} been reviewed. A pattern has to show up in 3 sessions, and in at least 30% of a project's reviewed sessions, before it is worth telling the next session about.`
        : "No sessions have been reviewed yet. A session is reviewed once it has been quiet for a while, so today's work shows up a little later."));
    return wrap;
  }
  for (const p of withLessons) {
    const card = el("article", { class: "rollup-card" });
    card.append(el("div", { class: "fact-meta" },
      el("span", { class: "project" }, p.project),
      el("span", { class: "hint" }, `${p.sessions} ${p.sessions === 1 ? "session" : "sessions"} reviewed`)));
    for (const l of p.lessons) {
      const row = el("div", { class: `lesson lesson-${l.kind}` });
      row.append(el("div", { class: "finding-head" },
        el("span", { class: `chip chip-${l.kind}` }, KIND_LABEL[l.kind]),
        el("span", { class: "finding-title" }, l.do),
        el("span", { class: "chip", title: l.session_ids.join(", ") },
          l.reviewed ? `${l.sessions} of ${l.reviewed}` : `${l.sessions}×`)));
      row.append(el("p", { class: "finding-detail" }, l.why));
      card.append(row);
    }
    wrap.append(card);
  }
  return wrap;
}

async function renderImprove(): Promise<void> {
  // Private by default, WITHOUT touching the shared `scope` global: this page used to assign to it and
  // leave scopeChosen false, so opening Improve and going back left Today/Memory pinned to your own
  // work with the switch still reading "everyone". The server pins non-admins to the caller anyway.
  const mineOnly = !scopeChosen || scope === "mine";
  let data;
  try {
    [data] = await Promise.all([getImprovements(mineOnly ? myEmail() : scopedOwner()), ensureTeam()]);
  } catch (err) {
    return showError("load session reviews", err);
  }
  if (routePath() !== "/improve") return; // navigated away mid-fetch
  const main = el("main", {});
  main.append(
    pageHead(
      "Improve",
      "Where each session's tokens, turns and waiting went, counted, never guessed.",
    ),
  );
  // No person picker unless the caller holds the admin credential. These are how-well-did-someone-work
  // numbers; the server pins them to the caller regardless, so showing a control that cannot widen
  // would just be a lie about what the page does.
  if (data.can_widen) {
    const sw = scopeSwitch(() => void renderImprove());
    if (sw) main.append(el("div", { class: "toolbar toolbar-mem" }, sw));
  } else {
    main.append(el("p", { class: "hint" },
      "Private, you see only your own sessions."));
  }

  if (data.rows.length === 0) {
    main.append(
      el(
        "section",
        { class: "welcome" },
        el("h2", {}, "No sessions reviewed yet"),
        el("p", {}, "Reviews are counted while a session is being mined, so they cover sessions that arrived after this feature shipped. Finish a session and it appears here a few minutes later."),
      ),
    );
    return view(main);
  }

  const t = data.totals;
  main.append(
    el(
      "section",
      { class: "improve-totals" },
      el("div", { class: "improve-total" },
        el("b", {}, `$${t.cost_usd.toFixed(2)}`), el("span", {}, `${SESSION_COST} at API rates`),
        // Visible, not a tooltip: a stack of bold dollar figures reads as a bill, and the one thing
        // that makes it not a bill must not require knowing to hover.
        el("span", { class: "improve-caveat" }, "not billed, covered by your plan")),
      el("div", { class: "improve-total" }, el("b", {}, t.tool_calls.toLocaleString("en-US")), el("span", {}, "tool calls")),
      el("div", { class: "improve-total" }, el("b", {}, t.wasted_calls.toLocaleString("en-US")), el("span", {}, "avoidable calls")),
      el("div", { class: "improve-total" }, el("b", {}, String(t.sessions)), el("span", {}, "sessions")),
    ),
  );
  // the reviewed count comes from the ROWS, not from `projects` — project_lessons() drops projects
  // with no lessons, so deriving it from `projects` could only ever produce 0 (see projectRollup).
  main.append(projectRollup(data.projects, data.rows.filter((r) => r.settled).length));
  main.append(el("h2", { class: "sec-head" }, "Session by session"));
  const list = el("div", { class: "facts" });
  for (const r of data.rows) list.append(improveCard(r, data.settle_minutes));
  main.append(list);
  view(main);
}

// --- team view --------------------------------------------------------------------
// Everyone reads who shipped what; an admin also gets each person's role and machines here.
async function renderTeam(): Promise<void> {
  const admin = me?.role === "admin";
  let rows: TeamRow[];
  let members: OrgMember[] | null;
  let oktaGroup = "";
  let ssoRequired = false;
  try {
    [rows, members] = await Promise.all([
      getTeam(),
      admin ? getOrgMembers().then((d) => { oktaGroup = d.okta_admin_group ?? ""; return d.members; }).catch((err) => {
        if (err instanceof ForbiddenError) return null;   // the role changed since sign-in: read-only
        throw err;
      }) : Promise.resolve(null),
    ]);
    if (members) ssoRequired = (await authConfig()).sso_required;
  } catch (err) {
    return showError("load team", err);
  }
  if (routePath() !== "/team") return;
  teamCache = rows;                                   // the freshest owner list, feeds the scope picker
  const manage = members !== null;
  const people = teamPeople(rows, members).filter((p) => p.role !== "service" || p.sessions > 0);
  const main = el("main", {});
  main.append(pageHead("Team", manage && oktaGroup
    ? `Who has shipped sessions and what they learned. Admins are the members of the Okta group ${oktaGroup}; change it in Okta.`
    : "Who has shipped sessions. Pick a name to read what they learned."));
  const counted = teamCountLine(people, manage);
  if (counted) main.append(el("p", { class: "explain", title: DEFS.members }, counted));
  const note = invitePathNote();
  if (note) main.append(note);
  if (manage && !ssoRequired) main.append(el("div", { class: "toolbar team-tools" }, inviteButton()));
  const table = el("table", { class: "sessions people-table" });
  const head = el("tr", {},
    el("th", {}, "Person"), el("th", { class: "num" }, "Sessions"), el("th", { class: "num" }, "Facts"),
    el("th", { class: "num" }, "Entities"), el("th", {}, "First seen"), el("th", {}, "Last active"));
  if (manage) head.append(el("th", {}, "Role"), el("th", {}, "Machines"));
  table.append(el("thead", {}, head));
  const body = el("tbody", {});
  if (people.length === 0)
    body.append(el("tr", {}, el("td", { colspan: manage ? "8" : "6", class: "hint" },
      "Nobody has shipped a session yet. Connect a machine and yours is first.")));
  const cell = (label: string, cls: string, ...kids: (Node | string)[]) =>
    el("td", cls ? { class: cls, "data-label": label } : { "data-label": label }, ...kids);
  for (const p of people) {
    const tr = el("tr", {},
      !p.user ? el("td", { class: "person-cell hint", title: "sessions shipped with no owner" }, "unattributed")
        : el("td", { class: "person-cell", title: `read what ${ownerName(p.user)} learned` }, p.sessions ? personLink(p.user) : p.user),
      cell("Sessions", "num", p.sessions.toLocaleString("en-US")),
      cell("Facts", "num", p.facts.toLocaleString("en-US")),
      cell("Entities", "num", p.entities.toLocaleString("en-US")),
      cell("First seen", "when", p.first_seen ? fmtWhen(p.first_seen) : ""),
      cell("Last active", "when", p.last_active ? fmtWhen(p.last_active) : ""));
    if (manage && !p.user) tr.append(el("td", { class: "empty-cell" }, ""), el("td", { class: "empty-cell" }, ""));
    else if (manage) {
      const own = p.user.toLowerCase() === myEmail().toLowerCase();
      const status = el("span", { class: "flag-status", role: "status" });
      const otherAdmins = people.filter((q) => q.role === "admin" && q.user !== p.user).length;
      tr.append(
        cell("Role", "", !p.machines.length ? el("span", { class: "hint" }, "signed out")
          : p.role === "service" ? el("span", { class: "hint" }, "service token")
          : oktaGroup ? el("span", {}, p.role ?? "member")
          : el("span", { class: "flagger" }, roleControl(p, status, otherAdmins), status)),
        cell("Machines", "", machinesCell(p, own)));
    }
    body.append(tr);
  }
  if (rows.length > 1) {
    const sum = (f: (r: TeamRow) => number) => rows.reduce((a, r) => a + f(r), 0);
    const n = teamCounts(people);
    const totals = el("tr", { class: "totals" },
      el("td", { class: "person-cell" }, "Totals ", el("span", { class: "hint" }, peopleLine(n.people, n.agents))),
      cell("Sessions", "num", sum((r) => r.sessions).toLocaleString("en-US")),
      cell("Facts", "num", sum((r) => r.facts).toLocaleString("en-US")),
      cell("Entities", "num", sum((r) => r.entities).toLocaleString("en-US")),
      el("td", { class: "empty-cell" }, ""), el("td", { class: "empty-cell" }, ""));
    if (manage) totals.append(el("td", { class: "empty-cell" }, ""), el("td", { class: "empty-cell" }, ""));
    body.append(totals);
  }
  table.append(body);
  main.append(el("div", { class: "table-scroll" }, table));
  if (manage)
    main.append(el("p", { class: "hint" },
      "Revoking a machine stops it shipping on its next send. The browser you are reading this in cannot revoke itself: use Sign out."));
  main.append(el("div", { class: "corpus-sec" },
    el("h3", { class: "sec-head" }, "More views"), toolsIndex(admin)));
  view(main);
}

// --- labels (what flagging does + the ledger of every flag given) -------------
let flagsOffset = 0;

function flagCard(f: FlagRow): HTMLElement {
  const known = LABELS.includes(f.label as Label);
  const card = el("article", { class: "fact" });
  if (known) reflectLabel(card, f.label as Label);
  const meta = el("div", { class: "fact-meta" });
  meta.append(el("span", { class: known ? `chip chip-${f.label}` : "chip" }, labelText(f.label)));
  if (f.type) meta.append(el("span", { class: "badge" }, f.type));
  if (f.project) meta.append(el("span", { class: "project" }, f.project));
  if (f.user) meta.append(el("span", { class: "project", title: f.user }, `by ${f.user.includes("@") ? f.user.split("@")[0] : f.user}`));
  card.append(meta);
  if (f.claim) card.append(el("p", { class: "claim" }, f.claim));
  else
    card.append(
      el("p", { class: "hint" }, `${f.ref}: this fact is gone, its session was read again after it was rated`),
    );
  if (f.note && f.note !== "ui-flag") card.append(el("p", { class: "evidence" }, `note: ${f.note}`));
  const flagger = (f as { flagger?: string | null }).flagger;
  if (flagger) card.append(el("p", { class: "hint" }, `rated by ${flagger}`));
  if (f.session_id)
    card.append(el("a", { href: sessionHref(f.session_id, f.ref), class: "hint" }, "from session →"));
  return card;
}

async function renderLabels(): Promise<void> {
  let data;
  try {
    data = await getFlags();
  } catch (err) {
    return showError("load ratings", err);
  }
  const main = el("main", {});
  main.append(
    pageHead(
      "Ratings",
      "How a rating changes recall, and every rating given.",
    ),
  );
  const defs: { l: Label; action: string; body: string }[] = [
    {
      l: "gold",
      action: labelLegend?.gold ?? "boost",
      body: "Right and worth knowing. Ranks higher the next time a similar question is asked.",
    },
    {
      l: "wrong",
      action: labelLegend?.wrong ?? "hide",
      body: "Incorrect or misleading. Never recalled again.",
    },
    {
      l: "unimportant",
      action: labelLegend?.unimportant ?? "down-rank",
      body: "True, but not worth a teammate's attention. Sinks to the bottom.",
    },
  ];
  const grid = el("div", { class: "label-grid" });
  for (const d of defs) {
    grid.append(
      el(
        "article",
        { class: `label-card is-${d.l}` },
        el("div", { class: "fact-meta" }, el("span", { class: `chip chip-${d.l}` }, LABEL_TEXT[d.l]), el("span", { class: "rel" }, `→ ${d.action}`)),
        el("p", { class: "claim" }, d.body),
      ),
    );
  }
  main.append(grid);
  // The ledger: every rating actually given, newest first.
  const parts = Object.entries(data.counts).map(([l, n]) => `${n} ${labelText(l)}`);
  main.append(
    el(
      "h2",
      { class: "sec-head" },
      `All ratings (${data.total}${parts.length ? ` · ${parts.join(", ")}` : ""})`,
    ),
  );
  if (data.rows.length === 0) {
    main.append(el("p", { class: "hint" }, "None yet. Rate any fact and it lands here."));
  } else {
    const list = el("div", { class: "facts" });
    for (const f of data.rows.slice(flagsOffset, flagsOffset + PAGE)) list.append(flagCard(f));
    main.append(list);
    main.append(
      pager(flagsOffset, PAGE, data.rows.length, (o) => {
        flagsOffset = o;
        void renderLabels();
        window.scrollTo(0, 0);
      }),
    );
  }
  view(main);
}

function questionBlock(q: QuestionEntry): HTMLElement {
  const block = el("section", { class: "question" });
  block.append(
    el("div", { class: "q-head" }, el("span", { class: "when" }, q.asked_at), el("p", { class: "q-text" }, q.question)),
  );
  const list = el("div", { class: "facts" });
  for (const f of q.facts) list.append(factCard(f));
  block.append(list);
  return block;
}

// What a card shows besides the fact itself. Every field is optional: the recall detail has only the
// fact, a session page adds reuse, Memory adds shipper, date, reuse and the link to the session.
interface FactExtras {
  user?: string | null;
  when?: string | null;
  used?: number;
  chips?: Node[];
  href?: string;
  judged?: boolean; // the label was set by the judge, not a person
  also?: string[]; // refs of folded duplicates; a verdict is sent for each
  menu?: boolean; // fold the verdicts behind one Rate control
}

function factCard(f: Fact, x: FactExtras = {}): HTMLElement {
  const card = el("article", { class: `fact t-${f.type ?? "grey"}`, "data-ref": f.ref });
  reflectLabel(card, f.label);

  const type = el("div", { class: "fact-type" }, el("i", { class: "dot", "aria-hidden": "true" }));
  if (f.type) type.append(el("span", { class: "tchip", title: TYPE_TIP[f.type] ?? "" }, f.type));
  if (f.kind && f.kind !== "hidden_context") type.append(el("span", { class: "kind" }, f.kind));

  const body = el("div", { class: "fact-body" });
  if (f.claim) body.append(el("p", { class: "claim" }, f.claim));
  // The claim is the fact; the why is detail. Two lines, then a click opens the rest.
  if (f.evidence) body.append(foldedWhy(f.evidence));
  if (!f.claim && !f.evidence) {
    body.append(el("p", { class: "hint" },
      `fact ${f.ref} is gone: its session was read again after this was recorded`));
  }
  if (x.when) body.append(el("p", { class: "byline", title: x.when }, fmtDate(x.when)));

  const side = el("div", { class: "fact-side" });
  const facts = el("dl", { class: "fact-facts" });
  const kv = (k: string, v: string) => facts.append(el("div", {}, el("dt", {}, k), el("dd", { title: v }, v)));
  if (x.user) kv("Shipped by", ownerName(x.user));
  if (f.project) kv("Project", f.project);
  if (x.used) kv("Reused in", `${x.used} ${x.used === 1 ? "session" : "sessions"}`); // 0 would be a receipt for nothing
  if (facts.childElementCount) side.append(facts);
  if (x.chips?.length) side.append(el("div", { class: "chips" }, ...x.chips));
  if (f.flaggable) side.append(x.menu ? rateMenu(f, card, x) : flagControls(f, card, !!x.judged, x.also ?? []));
  if (x.href) side.append(el("a", { class: "hint fact-open", href: x.href }, "Open session"));

  card.append(type, body, side);
  return card;
}

function foldedWhy(text: string): HTMLElement {
  const why = el("p", {
    class: "evidence", role: "button", tabindex: "0",
    "aria-expanded": "false", title: "show the full why",
  }, text);
  const toggle = (): void => {
    const open = why.classList.toggle("open");
    why.setAttribute("aria-expanded", String(open));
    why.setAttribute("title", open ? "fold the why back up" : "show the full why");
  };
  why.addEventListener("click", toggle);
  why.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return why;
}

function ownerName(user: string): string {
  return user.includes("@") ? user.split("@")[0] : user;
}

const DATE_FMT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" });

/** "2026-08-31" (or any ISO stamp) as "Aug 31, 2026"; anything unparsable is shown as it came. */
function fmtDate(iso: string): string {
  const t = Date.parse(iso.length === 10 ? iso + "T00:00:00" : iso);
  return Number.isNaN(t) ? iso : DATE_FMT.format(t);
}

// Three verdicts. The pressed one, clicked again, is the undo: it sends `clear`, so the wire
// vocabulary (gold / wrong / unimportant / clear) is untouched while the card shows three buttons.
// A label the JUDGE set shows pressed too, and clicking it is a person agreeing, not undoing: that
// click re-sends the label as a human flag (clear would delete the review and exempt the fact from
// the judge for good).
const FLAG_BTNS: Label[] = ["gold", "wrong", "unimportant"];

function flagControls(f: Fact, card: HTMLElement, judged: boolean, also: string[]): HTMLElement {
  const status = el("span", { class: "flag-status", role: "status" });
  const btns = el("div", { class: "flag-btns", role: "group", "aria-label": "rate this fact" });
  for (const l of FLAG_BTNS) {
    const b = el("button", {
      class: "flag-btn", "data-label": l, type: "button", "aria-pressed": String(f.label === l),
    }, LABEL_TEXT[l]) as HTMLButtonElement;
    b.addEventListener("click", () => {
      const undo = f.label === l && !judged;
      void submitFlag(f, undo ? "clear" : l, also, card, btns, status).then(() => { if (f.label === l) judged = false; });
    });
    btns.append(b);
  }
  return el("div", { class: "flagger" }, btns, status);
}

/** The verdicts folded behind one control, for a list where a row of three buttons per card is noise.
 *  The control names the verdict once one is set. */
function rateMenu(f: Fact, card: HTMLElement, x: FactExtras): HTMLElement {
  const summary = el("summary", { class: "btn btn-small rate-summary" }, rateText(f.label));
  return el("details", { class: "rate-menu" }, summary, flagControls(f, card, !!x.judged, x.also ?? []));
}

function rateText(label: Label | null): string {
  return label ? `${LABEL_TEXT[label]} ✓` : "Rate";
}

async function submitFlag(
  f: Fact,
  label: Label,
  also: string[],
  card: HTMLElement,
  btns: HTMLElement,
  status: HTMLElement,
): Promise<void> {
  status.textContent = "saving…";
  status.className = "flag-status";
  try {
    const res = await flag(f.ref, label);
    if (res.ok) await Promise.all(also.map((ref) => flag(ref, label).catch(() => null)));
    if (res.ok) {
      track("flag", pageName(routePath()) || "memory", { label });
      f.label = res.label;
      reflectLabel(card, res.label);
      const summary = card.querySelector(".rate-summary");
      if (summary) summary.textContent = rateText(res.label);
      for (const b of Array.from(btns.querySelectorAll("button"))) {
        const on = b.getAttribute("data-label") === res.label;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", String(on));
      }
      // res.label is null after an Undo — printing it rendered the literal string "✓ null"
      status.textContent = res.label ? `✓ ${LABEL_TEXT[res.label]}` : "Cleared";
      status.className = "flag-status ok";
    } else {
      status.textContent = res.error;
      status.className = "flag-status err";
    }
  } catch (err) {
    // Session expired mid-flag: go to the login, don't strand the user with a tiny inline "signed
    // out" note next to a button (this handler catches its own error, so showError never sees it).
    if (err instanceof AuthError) return renderLogin(err.message);
    status.textContent = err instanceof Error ? err.message : String(err);
    status.className = "flag-status err";
  }
}

function reflectLabel(card: HTMLElement, label: Label | null): void {
  for (const l of LABELS) card.classList.toggle(`is-${l}`, label === l);
}

// --- corpus explorer -------------------------------------------------------
// Human-readable headings for hidden_context types (Title Case fallback).
const HC_HEAD: Record<string, string> = {
  decision: "Decisions",
  gotcha: "Gotchas",
  constraint: "Constraints",
  todo: "Open TODOs",
  preference: "Preferences",
  rule: "Rules",
};

// One-line definition per fact type — shown as tooltips and on the Review page.
const TYPE_TIP: Record<string, string> = {
  decision: "a choice that was made and why, “we picked X over Y”",
  gotcha: "a surprising pitfall that cost time and will bite the next person",
  constraint: "a hard limit or rule the work must respect",
  todo: "something left unfinished or explicitly deferred",
  preference: "how this person likes things done, style, tools, workflow",
  rule: "a pattern that recurred across a project's sessions, counted from their own metrics, never labelled",
};

function headName(type: string): string {
  return HC_HEAD[type] ?? type.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function shortId(id: string): string {
  const tail = id.split(/[/:]/).filter(Boolean).pop() ?? id;
  return tail.length > 24 ? `${tail.slice(0, 24)}…` : tail;
}

// Owner shown as the email's local part; full address on hover. "owner" = this machine (unattributed).
function ownerCell(user: string | null): HTMLElement {
  const u = user ?? "";
  return el(
    "span",
    { class: "owner", title: u.includes("@") ? u : "sessions ingested on the server machine itself" },
    ownerName(u) || "?",
  );
}

// Sortable columns. Each declares its value `type` so sorting DEFERS by type (see cmpBy):
// strings via localeCompare, dates via Date.parse, numbers numerically.
interface CorpusCol {
  id: string;
  label: string;
  num?: boolean; // right-aligned numeric column
  type: "string" | "date" | "num";
  title?: string;
  get: (r: CorpusSessionSummary) => string | number | null;
}
const CORPUS_COLS: CorpusCol[] = [
  { id: "project", label: "Project", type: "string", get: (r) => r.project ?? "" },
  { id: "owner", label: "Owner", type: "string", get: (r) => r.user ?? "" },
  { id: "started", label: "When", type: "date", get: (r) => r.started_at },
  { id: "summary", label: "Summary", type: "string", get: (r) => r.summary ?? "" },
  { id: "facts", label: "Facts", num: true, type: "num", get: (r) => r.hidden_context },
  { id: "cost", label: SESSION_COST, num: true, type: "num", get: (r) => r.cost_usd,
    title: `${SESSION_COST_DEF} Shown for your own sessions, and for every session to a workspace admin.` },
];
const SESSIONS_PAGE = 20;

let corpusRows: CorpusSessionSummary[] = [];
let corpusStale = new Set<string>();
let corpusSort = { id: "started", dir: "desc" as "asc" | "desc" };
let corpusOffset = 0;
let corpusQ = "";
let corpusProject = "";
let corpusSelected = ""; // the session open in the right pane (`?s=` in the hash)

// The rows arrive already filtered: `sessions(user:, q:)` matches the owner, project, summary,
// branch and id server-side (analyzer/gql.py), which is what lets the box find a session this page
// never loaded. The project picker narrows what is on screen.
function filteredCorpus(): CorpusSessionSummary[] {
  return corpusProject ? corpusRows.filter((r) => r.project === corpusProject) : corpusRows;
}

async function renderCorpus(): Promise<void> {
  corpusSelected = hashQuery.get("s") ?? ""; // before the first await: route() overwrites hashQuery
  try {
    const [stats, sessions] = await Promise.all([
      getCorpusStats(),
      getCorpusSessions(scopedOwner(), corpusQ),
      ensureTeam(),
    ]);
    corpusRows = sessions;
    corpusStale = new Set(stats.stale_ids);
  } catch (err) {
    return showError("load sessions", err);
  }
  if (routePath() !== "/corpus") return; // ditto: 1600 sessions is a wide race window
  drawCorpus();
}

// The filter box re-queries the SERVER rather than narrowing what is already on screen, so it
// reaches sessions this page never loaded. Debounced because it now costs a round trip per
// keystroke, and sequenced because those round trips can land out of order — an earlier, slower
// query answering last would paint stale rows under a newer term.
let corpusSearchTimer: number | undefined;
let corpusSearchSeq = 0;

function onCorpusSearch(term: string): void {
  corpusQ = term;
  corpusOffset = 0;
  if (corpusSearchTimer !== undefined) window.clearTimeout(corpusSearchTimer);
  corpusSearchTimer = window.setTimeout(() => {
    const seq = ++corpusSearchSeq;
    void (async () => {
      let rows;
      try {
        rows = await getCorpusSessions(scopedOwner(), corpusQ);
      } catch {
        return; // leave what is on screen; the next keystroke retries
      }
      if (seq !== corpusSearchSeq || routePath() !== "/corpus") return;
      corpusRows = rows;
      drawCorpus();
      const el2 = document.querySelector<HTMLInputElement>(".content .search");
      el2?.focus();
      el2?.setSelectionRange(el2.value.length, el2.value.length);
    })();
  }, 180);
}

// The pane is a reading view: only the full session page can scroll to a fact, so Memory's links keep
// landing there rather than here.
function drawCorpus(): void {
  const search = el("input", {
    class: "search",
    type: "search",
    name: "q",
    placeholder: "Search sessions",
    "aria-label": "filter sessions by person, project, summary or id",
  }) as HTMLInputElement;
  search.value = corpusQ;
  search.addEventListener("input", () => onCorpusSearch(search.value.trim()));
  const bar = el("div", { class: "toolbar toolbar-mem" });
  const sw = scopeSwitch(() => {
    corpusOffset = 0;
    corpusSelected = "";                  // the open session belonged to the scope being left
    hashQuery.delete("s");
    history.replaceState(null, "", "#/corpus");
    void renderCorpus(); // scope is a server-side filter, so this refetches rather than re-filters
  });
  if (sw) bar.append(sw);
  bar.append(search, projectPick());
  const shown = filteredCorpus().length;
  const list = el("section", { class: "sess-list" }, bar, el("div", { class: "table-scroll" }, corpusTable()));
  if (corpusRows.length === 0 && !corpusQ && !corpusProject) list.append(connectCta());
  if (shown) list.append(pager(corpusOffset, SESSIONS_PAGE, shown, (o) => {
    corpusOffset = o;
    drawCorpus();
  }));
  const pane = el("aside", { class: "sess-pane", "aria-label": "the chosen session" });
  const main = el(
    "main",
    {},
    pageHead(
      "Sessions",
      scope === "everyone"
        ? "Browse past sessions and the facts extracted from each."
        : `${scope === "mine" ? "Your" : scopeName() + "'s"} sessions and the facts extracted from each.`,
    ),
    el("div", { class: "sess-split" }, list, pane),
  );
  view(main);
  void fillPane(pane, corpusSelected);
}

// Narrows locally: the rows already hold the whole scope. The search box is what reaches the server.
function projectPick(): HTMLElement {
  const sel = el("select", { class: "scope-pick", name: "project", "aria-label": "show one project" }) as HTMLSelectElement;
  sel.append(el("option", { value: "" }, "All projects"));
  for (const p of [...new Set(corpusRows.map((r) => r.project).filter(Boolean))].sort()) {
    const o = el("option", { value: p }, p);
    if (p === corpusProject) o.setAttribute("selected", "selected");
    sel.append(o);
  }
  if (corpusProject && !sel.value) corpusProject = "";   // the scope changed under the filter
  if (corpusProject) sel.classList.add("active");
  sel.addEventListener("change", () => {
    corpusProject = sel.value;
    corpusOffset = 0;
    drawCorpus();
  });
  return sel;
}

// Selecting a row is a history entry (`#/corpus?s=<id>`), pushed by hand so the page does not refetch
// 500 sessions for a click; Back and reload go through route(), which reads `s` and lands here again.
function selectSession(id: string): void {
  corpusSelected = id;
  hashQuery.set("s", id);
  history.pushState(null, "", `#/corpus?s=${encodeURIComponent(id)}`);
  for (const tr of Array.from(document.querySelectorAll<HTMLElement>(".sess-list tr[data-id]")))
    tr.classList.toggle("is-selected", tr.dataset.id === id);
  const pane = document.querySelector<HTMLElement>(".sess-pane");
  if (!pane) return;
  // Stacked under the list (a narrow window): bring the pane into view, or the click looks like
  // nothing. After it fills: the page under a "Loading…" pane is too short to scroll it to the top.
  void fillPane(pane, id).then(() => {
    if (corpusSelected === id && getComputedStyle(pane).position !== "sticky") pane.scrollIntoView({ block: "start" });
  });
}

// Type-aware compare; null/blank values sort last regardless of direction handling upstream.
function cmpBy(a: string | number | null, b: string | number | null, type: CorpusCol["type"]): number {
  const an = a === null || a === "";
  const bn = b === null || b === "";
  if (an && bn) return 0;
  if (an) return 1;
  if (bn) return -1;
  if (type === "num") return (a as number) - (b as number);
  if (type === "date") return Date.parse(a as string) - Date.parse(b as string);
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

function sortHeader(c: CorpusCol): HTMLElement {
  const active = corpusSort.id === c.id;
  const arrow = active ? (corpusSort.dir === "asc" ? " ▲" : " ▼") : "";
  const th = el(
    "th",
    {
      class: `sortable${c.num ? " num" : ""}${active ? " sorted" : ""}`,
      role: "button",
      tabindex: "0",
      title: c.title ? `${c.title} Click to sort.` : `Sort by ${c.label}`,
      "aria-sort": active ? (corpusSort.dir === "asc" ? "ascending" : "descending") : "none",
    },
    c.label + arrow,
  );
  const toggle = (): void => {
    if (corpusSort.id === c.id) corpusSort.dir = corpusSort.dir === "asc" ? "desc" : "asc";
    else corpusSort = { id: c.id, dir: c.type === "string" ? "asc" : "desc" }; // strings A→Z; dates/nums high→low
    corpusOffset = 0; // a new ordering starts from its first page
    drawCorpus();
  };
  th.addEventListener("click", toggle);
  th.addEventListener("keydown", (e) => {
    const k = (e as KeyboardEvent).key;
    if (k === "Enter" || k === " ") {
      e.preventDefault();
      toggle();
    }
  });
  return th;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

const NOT_PRICED = "Not priced: the session's model has no list price in CSA, or it shipped before cost tracking.";

// Why a cost cell shows "·": the owner and an admin read "not priced", anyone else "not yours".
function costTip(r: CorpusSessionSummary): string {
  if (r.cost_usd !== null) return SESSION_COST_DEF;
  const mine = !!myEmail() && (r.user ?? "").toLowerCase() === myEmail().toLowerCase();
  return mine || me?.role === "admin" || !myEmail()
    ? NOT_PRICED
    : "Only the session's owner and a workspace admin see its cost.";
}

function corpusTable(): HTMLElement {
  // Under "Just mine" every row has the same owner — the column is a wasted 90px of a table that
  // already wraps. It comes back the moment you widen the scope.
  const cols = scopedOwner() ? CORPUS_COLS.filter((c) => c.id !== "owner") : CORPUS_COLS;
  const table = el("table", { class: "sessions" });
  const headRow = el("tr", {});
  for (const c of cols) headRow.append(sortHeader(c));
  table.append(el("thead", {}, headRow));

  const col = CORPUS_COLS.find((c) => c.id === corpusSort.id) ?? CORPUS_COLS[2];
  const rows = [...filteredCorpus()]
    .sort((x, y) => {
      const av = col.get(x);
      const bv = col.get(y);
      const an = av === null || av === "";
      const bn = bv === null || bv === "";
      if (an || bn) return an && bn ? 0 : an ? 1 : -1; // blanks sink to the bottom in BOTH directions
      const r = cmpBy(av, bv, col.type);
      return corpusSort.dir === "asc" ? r : -r;
    })
    .slice(corpusOffset, corpusOffset + SESSIONS_PAGE);

  const body = el("tbody", {});
  if (rows.length === 0) {
    body.append(
      el(
        "tr",
        {},
        el(
          "td",
          { colspan: String(cols.length), class: "hint" },
          corpusQ || corpusProject
            ? "No sessions match that filter."
            : scope === "everyone"
              ? "No sessions mined yet."
              : scope === "mine"
                ? "None of your sessions have been mined yet. Switch to Everyone to see the team's."
                : `Nothing from ${scopeName()} yet. Switch to Everyone to see the team's.`,
        ),
      ),
    );
  }
  for (const r of rows) {
    const summary = el("td", { class: "summary-cell" });
    const text = r.summary ?? "";
    summary.append(el("a", { href: `#/corpus?s=${encodeURIComponent(r.session_id)}`, title: text || r.session_id },
      truncate(text, 90) || shortId(r.session_id)));
    if (r.deployed_to_prod === 1) summary.append(" ", el("span", { class: "badge badge-prod", title: PROD_TIP }, "prod"));
    if (corpusStale.has(r.session_id))
      summary.append(" ", el("span", { class: "chip chip-stale", title: "extracted but the source .jsonl changed since, re-extract" }, "stale"));
    const tr = el("tr", { "data-id": r.session_id, class: r.session_id === corpusSelected ? "is-selected" : "" });
    tr.append(el("td", {}, r.project));
    if (cols.length === CORPUS_COLS.length) tr.append(el("td", {}, ownerCell(r.user)));
    tr.append(
      el("td", { class: "when" }, r.started_at ? fmtDate(r.started_at) : ""),
      summary,
      el("td", { class: "num" }, String(r.hidden_context)),
      el("td", { class: "num", title: costTip(r) }, fmtCost(r.cost_usd)),
    );
    tr.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // a modifier click is "open in a new tab"
      if ((e.target as HTMLElement).closest("a")) e.preventDefault(); // otherwise the href is for Enter and new tabs
      selectSession(r.session_id);
    });
    body.append(tr);
  }
  table.append(body);
  return table;
}

// --- the retained transcript ------------------------------------------------
// What a session actually consists of, measured over 180 real local sessions: tool_result 8,327,
// tool_use 8,321, thinking 5,076, text 3,457. Tool calls OUTNUMBER prose two to one, so a view that
// renders only prose is showing the minority of the work and hiding the part someone came to read.

const ROLE_LABEL: Record<string, string> = { user: "You", assistant: "Assistant" };

// A tool call's arguments arrive as a JSON string. Show the ARGUMENT that identifies the call —
// the command, the path — rather than the envelope: `{"command":"pytest -q"}` reads worse than
// `pytest -q`, and it is the same information.
function toolSummary(b: TranscriptBlock): string {
  const raw = b.text || "";
  try {
    const o = JSON.parse(raw) as Record<string, unknown>;
    for (const k of ["command", "file_path", "path", "query", "pattern", "url", "prompt", "skill", "description"]) {
      const v = o[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    const first = Object.values(o).find((v) => typeof v === "string" && (v as string).trim());
    return typeof first === "string" ? first : raw;
  } catch {
    return raw; // not JSON (a bare-string input); show it as it is
  }
}

// A block whose whole content already fits on the summary line does not need to be a fold: opening
// it showed the same string twice, once as the preview and once as the body. Short results ("502",
// "277 passed") are the common case, so this is most of them.
function shortEnough(text: string): boolean {
  return !text.includes("\n") && text.length <= 96;
}

function toolLine(kind: string, kindClass: string, body: string): HTMLElement {
  return el("div", { class: `turn-line ${kindClass}` },
            el("span", { class: "tool-name" }, kind),
            el("code", { class: "tool-sum" }, body));
}

function blockBody(b: TranscriptBlock): HTMLElement {
  if (b.type === "tool_use") {
    const sum = toolSummary(b);
    // The fold exists to show the FULL arguments. When the summary already is them, there is
    // nothing behind it worth a click.
    if (!b.truncated && shortEnough(sum) && sum === b.text) {
      return toolLine(b.toolName || "tool", "turn-call", sum);
    }
    const box = el("details", { class: "turn-tool" });
    box.append(
      el("summary", {},
        el("span", { class: "tool-name" }, b.toolName || "tool"),
        el("code", { class: "tool-sum" }, toolSummary(b)),
      ),
      el("pre", { class: "turn-body tool-raw" }, b.text + (b.truncated ? "\n… truncated at ingest" : "")),
    );
    return box;
  }
  if (b.type === "tool_result") {
    const kind = b.isError ? "error" : "result";
    const cls = `turn-result${b.isError ? " is-error" : ""}`;
    const text = b.text || "";
    if (!b.truncated && shortEnough(text)) return toolLine(kind, cls, text || "(no output)");
    const box = el("details", { class: `turn-tool ${cls}` });
    box.append(
      el("summary", {},
        el("span", { class: "tool-name" }, kind),
        el("code", { class: "tool-sum" }, text.split("\n")[0].slice(0, 120) || "(no output)"),
      ),
      el("pre", { class: "turn-body tool-raw" }, text + (b.truncated ? "\n… truncated at ingest" : "")),
    );
    return box;
  }
  if (b.type === "image") return el("p", { class: "turn-body turn-image" }, "[image]");
  if (b.type === "thinking") {
    const text = b.text || "";
    if (shortEnough(text)) return toolLine("thinking", "turn-thinking", text);
    const box = el("details", { class: "turn-tool turn-thinking" });
    // The summary is a PREVIEW of the same words, so opening it would show them twice. Say what it
    // is and how much there is; the body carries the text.
    box.append(el("summary", {}, el("span", { class: "tool-name" }, "thinking"),
                  el("code", { class: "tool-sum" }, `${text.length} characters of reasoning`)),
               el("pre", { class: "turn-body" }, text));
    return box;
  }
  return el("pre", { class: "turn-body" }, b.text);
}

// One message: its role label once, then every block that belongs to it. Grouping by messageIndex is
// why the label lines up with the FIRST line of the body — the previous version put a label beside
// every block, so a multi-block assistant turn repeated "claude" down the page and nothing aligned.
function messageGroup(role: string, blocks: TranscriptBlock[]): HTMLElement {
  const row = el("div", { class: `turn turn-${role}${blocks[0]?.sidechain ? " is-sidechain" : ""}` });
  const label = el("div", { class: "turn-role" }, ROLE_LABEL[role] || role);
  if (blocks[0]?.sidechain) label.append(el("span", { class: "turn-tag" }, "subagent"));
  const body = el("div", { class: "turn-blocks" });
  for (const b of blocks) body.append(blockBody(b));
  row.append(label, body);
  return row;
}

function renderBlocks(into: HTMLElement, blocks: TranscriptBlock[]): void {
  let i = 0;
  while (i < blocks.length) {
    const mi = blocks[i].messageIndex;
    const role = blocks[i].role;
    const group: TranscriptBlock[] = [];
    while (i < blocks.length && blocks[i].messageIndex === mi && blocks[i].role === role) {
      group.push(blocks[i]);
      i += 1;
    }
    into.append(messageGroup(role, group));
  }
}

function transcriptSection(page: TranscriptPage, id: string): HTMLElement {
  const box = el("details", { class: "sec transcript" });
  box.append(el("summary", { class: "sec-head" },
                el("span", {}, `Transcript (${page.totalCount})`)));
  const stored = page.storedAt ? fmtWhen(page.storedAt) : "";
  box.append(el("p", { class: "hint" },
    `Redacted at ${page.redactionMode || "secrets"} on the machine it came from. `
    + (stored ? `Arrived ${stored}. ` : "")
    + `Deleted ${page.retentionDays} days after it arrives. Nobody else on your team can open this.`));

  const body = el("div", { class: "turns" });
  renderBlocks(body, page.items);
  box.append(body);

  // A real session runs to thousands of blocks. Page them in rather than building the whole thing
  // on load; the cursor is opaque and simply echoed back.
  let cursor = page.nextCursor;
  if (page.hasMore) {
    const more = el("button", { class: "btn transcript-more" }, "");
    const label = () => {
      const shown = body.querySelectorAll(".turn-blocks > *").length;
      more.textContent = `Show more (${shown} of ${page.totalCount})`;
    };
    label();
    more.addEventListener("click", async () => {
      more.disabled = true;
      more.textContent = "loading…";
      const next = await getSessionTranscript(id, 200, cursor);
      if (!next) {
        more.textContent = "could not load more";
        return;
      }
      renderBlocks(body, next.items);
      cursor = next.nextCursor;
      if (next.hasMore) {
        more.disabled = false;
        label();
      } else {
        more.remove();
      }
    });
    box.append(more);
  }
  return box;
}

// Fetches and appends the transcript if there is one to read. Never throws into the page: a server
// that does not retain, a session that is not yours, and one past its window all answer with an
// empty page, and all three mean the same thing here. Nothing is rendered.
async function appendTranscript(content: HTMLElement, id: string): Promise<void> {
  let page: TranscriptPage | null = null;
  try {
    page = await getSessionTranscript(id);
  } catch {
    return;
  }
  if (!page?.items?.length) return;
  content.append(transcriptSection(page, id));
}

async function renderCorpusDetail(id: string): Promise<void> {
  // Read the target fact NOW: hashQuery is module state the next route() overwrites mid-fetch.
  const targetRef = hashQuery.get("fact");
  let d;
  try {
    d = await getCorpusSession(id);
  } catch (err) {
    if (!(err instanceof AuthError) && /\b404\b/.test(String(err)))
      return view(el("main", {}, pageHead("Session not found", "It is not in your workspace, or it was deleted."),
        el("a", { href: "#/corpus" }, "All sessions")));
    return showError("load the session", err);
  }
  // The throw used to happen HERE, outside the try: a 200 carrying {"error": ...} did not reject, so
  // corpusHeader() dereferenced `.session.summary` on undefined and the unhandled rejection left the
  // page on "Loading…" forever. The server sends 404 now; this keeps a malformed payload survivable.
  if (!d?.session) {
    return showError("load the session",
      new Error(`Session ${id} is not in this workspace. It may not have been shipped yet.`));
  }
  track("session_open", "sessions");
  const content = el("div", { class: "detail-main" });
  const filters = detailFilterBar(content, d);
  if (filters) content.append(filters);
  if (d.worked_on.length) content.append(chipRow("Worked on", d.worked_on));
  const entries = hiddenSections(d);
  // Entities/relations/actions/links are the machine's ledger, not the reading. They start folded —
  // that alone took the page from many screens to about one — and the nav on the right opens them.
  const machine: [string, number, HTMLElement | null][] = [
    ["Entities", d.entities.length, entitiesSection(d.entities)],
    ["Relations", d.relations.length, relationsSection(d.relations)],
    ["Actions", d.actions.length, actionsSection(d.actions)],
    ["Links", d.links.length, linksSection(d.links)],
  ];
  for (const [name, count, sec] of machine) if (sec) entries.push({ name, count, sec });
  for (const e of entries) content.append(e.sec);
  if (d.data_sources.length) content.append(chipRow("Data sources", d.data_sources));
  // The transcript, when this server retains one and you are allowed to read it. Appended
  // asynchronously so the facts (the point of the page) never wait on it, and silently absent
  // otherwise: on most deployments nothing is retained, and an empty "Transcript" section that is
  // always empty would just be furniture.
  void appendTranscript(content, id);
  const body = el("div", { class: "detail-body" }, content);
  const nav = detailNav(entries);
  if (nav) body.append(nav);
  const main = el("main", {}, corpusHeader(d), body);
  view(main);
  // Arriving from a claim elsewhere (Welcome hero, Memory, a flag): open, mark and scroll to that
  // fact, so the session visibly IS where the claim lives instead of a page it might be buried in.
  if (targetRef) {
    const hit = main.querySelector<HTMLElement>(`.fact[data-ref="${CSS.escape(targetRef)}"]`);
    if (hit) {
      hit.closest("details")?.setAttribute("open", "");
      hit.classList.add("is-target");
      hit.scrollIntoView({ block: "center" });
    }
  }
}

// The right-hand section nav (interviews: "the page is too long, take Actions, Links… aside").
// One button per section: click opens it and jumps there. Buttons, not #anchor links — a bare
// fragment href would fire the hash router and normalise the URL back to #/.
function detailNav(entries: { name: string; count: number; sec: HTMLElement }[]): HTMLElement | null {
  if (entries.length < 2) return null;
  const nav = el("nav", { class: "detail-nav", "aria-label": "sections of this session" },
    el("div", { class: "detail-nav-head" }, "On this page"));
  for (const e of entries) {
    const b = el("button", { class: "detail-nav-item", type: "button" },
      el("span", {}, e.name), el("span", { class: "detail-nav-n" }, String(e.count)));
    b.addEventListener("click", () => {
      e.sec.setAttribute("open", "");
      e.sec.scrollIntoView({ block: "start" });
    });
    nav.append(b);
  }
  return nav;
}

// A section you can fold shut (native <details> — no state to manage). `open=false` = starts folded.
function collapsible(title: string, count: number, cat: string, body: HTMLElement, suffix = "", open = true): HTMLElement {
  const det = el("details", open ? { class: "corpus-sec", "data-cat": cat, open: "" } : { class: "corpus-sec", "data-cat": cat });
  const tip = cat.startsWith("hc:") ? (TYPE_TIP[cat.slice(3)] ?? "") : "";
  det.append(
    el("summary", { class: "sec-head", title: tip }, `${title} (${count})${suffix ? ` · ${suffix}` : ""}`),
    body,
  );
  return det;
}

// Filters work on the already-rendered DOM (show/hide) so <details> open state survives.
// Null when the session has no labeled facts: a row of all-zero chips filters nothing and read as
// QA chrome on every partner-facing session (PM text diet).
function detailFilterBar(main: HTMLElement, d: CorpusSessionDetail): HTMLElement | null {
  const allFacts = Object.values(d.hidden_context).flat();
  if (!allFacts.some((f) => f.label)) return null;
  const bar = el("div", { class: "filterbar", "aria-label": "filter facts and sections" });
  // Explicit empty state: silently vanishing sections read as "my click did nothing".
  const empty = el("p", { class: "hint filter-empty", role: "status" }) as HTMLParagraphElement;
  empty.hidden = true;
  const countFor = (l: string): number =>
    l === "unlabeled" ? allFacts.filter((f) => !f.label).length : allFacts.filter((f) => f.label === l).length;

  // Label filter: none active = show every fact; active chips = show only those labels.
  const active = new Set<string>();
  const applyLabels = (): void => {
    const filtering = active.size > 0;
    let visible = 0;
    for (const card of Array.from(main.querySelectorAll<HTMLElement>('details[data-cat^="hc:"] .fact'))) {
      const label = LABELS.find((l) => card.classList.contains(`is-${l}`)) ?? "unlabeled";
      const show = !filtering || active.has(label);
      card.style.display = show ? "" : "none";
      if (show) visible += 1;
    }
    // a section with nothing visible reads as broken — fold it away while the filter holds
    for (const sec of Array.from(main.querySelectorAll<HTMLElement>('details[data-cat^="hc:"]'))) {
      const any = Array.from(sec.querySelectorAll<HTMLElement>(".fact")).some((c) => c.style.display !== "none");
      sec.style.display = any ? "" : "none";
    }
    if (filtering) {
      empty.hidden = false;
      empty.textContent =
        visible === 0
          ? `No facts on this session are labeled ${[...active].join(" or ")}, fact sections are hidden. Click the chips again to clear the filter.`
          : `Showing ${visible} of ${allFacts.length} facts (${[...active].join(", ")}).`;
    } else {
      empty.hidden = true;
    }
  };
  bar.append(el("span", { class: "filter-note" }, "labels"));
  for (const l of [...LABELS, "unlabeled"]) {
    // grey at rest — the label color appears only when the filter is ON (color = active, not identity)
    const chip = el("button", { class: "chip chip-toggle", type: "button", "aria-pressed": "false" }, `${l} (${countFor(l)})`);
    chip.addEventListener("click", () => {
      const on = !active.has(l);
      if (on) active.add(l);
      else active.delete(l);
      chip.classList.toggle(`chip-${l}`, on);
      chip.classList.toggle("active", on);
      chip.setAttribute("aria-pressed", String(on));
      applyLabels();
    });
    bar.append(chip);
  }

  // No section chips here any more: the section nav on the right owns showing/finding sections.
  return el("div", { class: "filterwrap" }, bar, empty);
}

function pipeChip(label: string, state: "on" | "off" | "stale", tip: string): HTMLElement {
  return el("span", { class: `chip chip-${state}`, title: tip }, label);
}

function corpusHeader(d: CorpusSessionDetail): HTMLElement {
  const s = d.session;
  const head = el("section", { class: "corpus-head" });
  head.append(el("h1", { class: "sid" }, s.summary || shortId(s.session_id)));
  const meta = el("div", { class: "fact-meta" });
  if (s.user) meta.append(el("span", { class: "chip chip-owner", title: s.user }, s.user));
  if (s.project) meta.append(el("span", { class: "project" }, s.project));
  if (s.git_branch) meta.append(el("span", { class: "project" }, s.git_branch));
  if (s.started_at) meta.append(el("span", { class: "when" }, s.started_at));
  head.append(meta);
  // The extracted/fresh/indexed chip row is pipeline plumbing a reader never acts on — gone
  // (interviews: too much text). Only "stale" survives, because it changes what you're reading.
  if (d.pipeline.stale)
    head.append(el("div", { class: "chips" },
      pipeChip("stale", "stale", "extracted but the source .jsonl changed since, re-extract")));
  return head;
}

function chipRow(title: string, items: string[]): HTMLElement {
  const row = el("div", { class: "chips" });
  for (const it of items) row.append(el("span", { class: "chip" }, it));
  return el("section", { class: "corpus-sec" }, el("h2", { class: "sec-head" }, title), row);
}

function toFact(hf: CorpusHiddenFact, type: string, project: string): Fact {
  return {
    ref: hf.ref,
    kind: "hidden_context",
    project,
    type,
    claim: hf.claim,
    evidence: hf.evidence,
    label: hf.label,
    flaggable: true,
  };
}

// Fact sections in the order readers ranked them (interviews): decisions are the payload, gotchas
// next. Constraint goes last AND folded — the interviewed users saw no value in it; unknown/custom
// types slot in before it.
const HC_ORDER = ["decision", "gotcha", "todo", "preference"];
function hcRank(t: string): number {
  if (t === "constraint") return HC_ORDER.length + 1;
  const i = HC_ORDER.indexOf(t);
  return i === -1 ? HC_ORDER.length : i;
}

function hiddenSections(d: CorpusSessionDetail): { name: string; count: number; sec: HTMLElement }[] {
  const out: { name: string; count: number; sec: HTMLElement }[] = [];
  const types = Object.entries(d.hidden_context)
    .filter(([, facts]) => facts.length)
    .sort(([a], [b]) => hcRank(a) - hcRank(b));
  for (const [type, facts] of types) {
    const list = el("div", { class: "facts" });
    for (const hf of facts)
      list.append(factCard(toFact(hf, type, d.session.project), { used: hf.used, chips: coreChip(hf.dominant) }));
    // per-label breakdown in the heading, e.g. "Gotchas (6), 3 gold · 1 wrong"
    const byLabel = LABELS.map((l) => [l, facts.filter((f) => f.label === l).length] as const)
      .filter(([, n]) => n > 0)
      .map(([l, n]) => `${n} ${l}`)
      .join(" · ");
    const name = headName(type);
    out.push({ name, count: facts.length,
      sec: collapsible(name, facts.length, `hc:${type}`, list, byLabel, type !== "constraint") });
  }
  return out;
}

function entitiesSection(entities: CorpusEntity[]): HTMLElement | null {
  if (!entities.length) return null;
  const { served, oneOff } = splitServed(entities);
  const mkList = (items: CorpusEntity[]) => {
    const list = el("ul", { class: "compact" });
    for (const e of items) {
      list.append(
        el(
          "li",
          {},
          el("span", { class: "badge" }, e.type),
          el("span", { class: "ent-name" }, e.name),
          el("span", { class: "mono" }, e.id),
        ),
      );
    }
    return list;
  };
  // One-offs fold into a dim sub-group instead of vanishing: silent shrinkage next to the durable
  // header count reads as "extraction broke". Header count stays the TOTAL for the same reason.
  const body = el("div", {});
  if (served.length) body.append(mkList(served));
  if (oneOff.length) {
    body.append(
      el(
        "details",
        { class: "ent-oneoff" },
        el("summary", {}, `${oneOff.length} one-off ${oneOff.length === 1 ? "entity" : "entities"} not served to recall`),
        mkList(oneOff),
      ),
    );
  }
  return collapsible("Entities", entities.length, "entities", body, "", false);
}

function relationsSection(rels: CorpusRelation[]): HTMLElement | null {
  if (!rels.length) return null;
  const list = el("ul", { class: "compact" });
  for (const r of rels) {
    const li = el(
      "li",
      {},
      el("span", { class: "mono" }, r.source_id),
      el("span", { class: "rel" }, `${r.rel}→`),
      el("span", { class: "mono" }, r.target_id),
    );
    if (r.context) li.append(el("span", { class: "ctx" }, r.context));
    list.append(li);
  }
  return collapsible("Relations", rels.length, "relations", list, "", false);
}

function actionsSection(actions: CorpusAction[]): HTMLElement | null {
  if (!actions.length) return null;
  const list = el("ul", { class: "compact" });
  for (const a of actions) {
    const li = el(
      "li",
      {},
      el("span", { class: "rel" }, `${a.verb} →`),
      el("span", { class: "mono" }, a.target_id),
    );
    if (a.context) li.append(el("span", { class: "ctx" }, a.context));
    list.append(li);
  }
  return collapsible("Actions", actions.length, "actions", list, "", false);
}

function linksSection(links: CorpusLink[]): HTMLElement | null {
  if (!links.length) return null;
  const list = el("ul", { class: "compact" });
  for (const l of links) {
    const label = l.title || l.url;
    // Only linkify http(s); anything else renders as text (XSS-safe: no javascript: hrefs).
    const node = /^https?:\/\//i.test(l.url)
      ? el("a", { href: l.url, target: "_blank", rel: "noopener noreferrer" }, label)
      : el("span", {}, label);
    list.append(el("li", {}, node));
  }
  return collapsible("Links", links.length, "links", list, "", false);
}

// --- facts browser (every mined fact, searchable) ----------------------------
const FACT_TYPES = ["decision", "gotcha", "todo", "preference", "constraint", "rule"]; // constraint demoted to last — interviews ranked it least useful
// The "core" chip: the fact is about the entity its session was about. Reuse is a card field now.
function coreChip(dominant: 0 | 1): HTMLElement[] {
  return dominant === 1 ? [el("span", { class: "chip chip-core", title: "about the entity this session was about" }, "core")] : [];
}

/** A fact row as a card. `same` are rows stating the same claim, folded into this one: a verdict
 *  applies to all of them. */
function factRowCard(r: FactRow, x: { same?: FactRow[]; menu?: boolean } = {}): HTMLElement {
  const same = x.same ?? [];
  const chips: Node[] = coreChip(r.dominant);
  const seen = seenLine([r, ...same]);
  if (seen) chips.push(el("span", { class: "chip" }, seen));
  else if (r.corroboration > 1)
    chips.push(el("span", { class: "chip", title: `extracted independently from ${r.corroboration} sessions` }, `Seen in ${r.corroboration} sessions`));
  if (r.judge_gold === 1 && !r.label)
    chips.push(el("span", { class: "chip chip-judge", title: "The automatic reviewer rated this useful. It ranks a little higher until a person confirms." }, "Likely useful"));
  if (r.label && r.flagger === "judge")
    chips.push(el("span", { class: "chip chip-judge", title: `“${labelText(r.label)}” was set by the automatic reviewer, not a person. Rate it to override.` }, "Auto-rated"));
  const f: Fact = {
    ref: r.ref,
    kind: "hidden_context",
    project: r.project ?? "",
    type: r.type,
    claim: r.claim,
    evidence: r.evidence,
    label: LABELS.includes(r.label as Label) ? (r.label as Label) : null,
    flaggable: true,
  };
  return factCard(f, { user: r.user, when: r.started_at?.slice(0, 10) ?? null, used: r.used, chips,
    href: sessionHref(r.session_id, r.ref), judged: r.flagger === "judge", also: same.map((s) => s.ref), menu: x.menu });
}

// --- questions (HITL review queue) -----------------------------------------
async function renderQuestions(): Promise<void> {
  let data;
  try {
    data = await getQuestions();
  } catch (err) {
    return showError("load questions", err);
  }
  const main = el(
    "main",
    {},
    pageHead(
      "Review",
      "Recall used these facts recently. A five second check here improves every later answer.",
    ),
    el("details", { class: "corpus-sec" },
      el("summary", { class: "sec-head" }, "What the types mean"),
      typeDefs()),
    receiptsStrip(data.receipts),
  );

  const sec = el("section", { class: "corpus-sec" }, el("h2", { class: "sec-head" }, "Today's five"));
  if (data.today.length === 0) {
    sec.append(
      el("p", { class: "hint" },
        "Nothing needs you today: recall has not used any unrated fact. ",
        el("a", { href: "#/" }, "Browse all facts")),
    );
  } else {
    const list = el("div", { class: "facts" });
    for (const r of data.today) list.append(factRowCard(r));
    sec.append(list);
  }
  main.append(sec);

  const unlabeled = data.counts.uncertain_labels + data.counts.uncertain_extraction;
  main.append(
    el("p", { class: "hint" },
      `${unlabeled} more unrated facts are recalled as usual.`),
  );
  view(main);
}

// What your ratings have done so far.
function receiptsStrip(r: QuestionsData["receipts"]): HTMLElement {
  const chip = (text: string, tip: string) => el("span", { class: "chip", title: tip }, text);
  return el(
    "div",
    { class: "receipts" },
    chip(`You rated ${r.your_labels}`, "facts you rated Useful, Wrong or Noise"),
    chip(`Your rated facts recalled ${r.labeled_served} times`, "times a fact you rated was recalled into a later session"),
    chip(`${r.used_this_corpus} facts in use`, "facts recall has already brought into later sessions"),
  );
}

// The type vocabulary as a scannable term/definition grid (educational footer on Review).
function typeDefs(): HTMLElement {
  const defs = el("dl", { class: "type-defs" });
  for (const [t, tip] of Object.entries(TYPE_TIP)) {
    defs.append(el("dt", {}, t), el("dd", {}, tip));
  }
  defs.append(el("dt", {}, "wrong"), el("dd", {}, "the claim is not true: rate it Wrong and it is never recalled again"));
  return defs;
}

// --- tools index ------------------------------------------------------------
// Reads of this workspace beyond the people table. The pipeline, the review queue and the ratings
// ledger are for the people who run it.
function toolsIndex(admin: boolean): HTMLElement {
  const row = el("div", { class: "op-index" });
  const links: [string, string, string][] = [
    ["#/recall", "Recalled into sessions", "every question a session asked, and the facts it got back"]];
  if (admin) links.push(
    ["#/pipeline", "Pipeline", "what is being read right now, and what it costs"],
    ["#/questions", "Review", "facts waiting on a person's rating"],
    ["#/labels", "Ratings", "every rating given, and what each one does to recall"]);
  for (const [href, label, tip] of links) row.append(el("a", { class: "op-index-link", href, title: tip }, label));
  return row;
}

// --- router ----------------------------------------------------------------
// A slow fetch used to leave the PREVIOUS page on screen with the new URL already in the bar — 4-5s
// of unchanged content on the lab corpus, which reads as a dead click, not as loading. Show a
// placeholder if the render has not landed shortly; the delay keeps fast navigations from flashing.
let pendingView: number | undefined;

function showPendingView(): void {
  if (pendingView !== undefined) window.clearTimeout(pendingView);
  pendingView = window.setTimeout(() => {
    if (app.getAttribute("aria-busy") !== "true") return;   // the view already rendered
    setView(shell(el("p", { class: "hint loading" }, "Loading…")));
    app.setAttribute("aria-busy", "true");                  // setView clears it; we are still busy
  }, 250);
}

// The route PATH, without the query string. Stale-render guards must compare against this, never the
// raw hash: once routes carry params ("#/corpus?project=x"), `raw !== "/corpus"` is true and the guard
// silently aborts the render, stranding the page on "Loading…" with no error anywhere.
function routePath(): string {
  const raw = location.hash.replace(/^#/, "");
  const qi = raw.indexOf("?");
  return qi >= 0 ? raw.slice(0, qi) : raw;
}

function renderNotFound(hash: string): void {
  view(el("main", {}, pageHead("Page not found", `Nothing lives at #${hash}.`),
    el("p", {}, el("a", { href: "#/" }, "Go to Memory"))));
}

function route(): void {
  stopTimers(); // leaving any view must never leak its interval (Home poll, welcome rotator)
  const raw = location.hash.replace(/^#/, "");
  // Hash routes carry their own query string ("#/today/2026-07-29?project=acme-ai&branch=master").
  // Filter state lives in the URL, not in a module global: a filtered day has to survive a reload, be
  // pasteable into Slack, and come undone with the Back button.
  const qi = raw.indexOf("?");
  const hash = qi >= 0 ? raw.slice(0, qi) : raw;
  hashQuery = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : "");
  app.setAttribute("aria-busy", "true");
  // A navigation is a new page: start it at the top. Without this, arriving from a scrolled page
  // left the next view opened mid-scroll (sim finding). Views that aim somewhere specific (?fact)
  // scroll again after they render, so they still win.
  window.scrollTo(0, 0);
  showPendingView();
  // Match the specific routes before the recall-detail route so they aren't swallowed.
  let m;
  const sid = corpusSessionRoute(hash);
  // An id-less "#/corpus/session/" is NOT an unknown hash, and must not fall through to the normalise-
  // to-#/ branch below: that redirect is exactly why a link built from a row with no session_id looked
  // like a dead button (the URL snapped back to #/ and the page you were on re-rendered, no error
  // anywhere). Report it like renderCorpusDetail reports a session that is not in the corpus.
  if (sid !== null) {
    if (sid) void renderCorpusDetail(sid);
    else showError("open that session", new Error("That link carries no session id, so there is nothing to open."));
  } else if (ROUTE_ALIASES[hash] !== undefined) location.hash = `#${ROUTE_ALIASES[hash]}`;
  else if (hash === "/corpus") void renderCorpus();
  else if (hash === "/improve") void renderImprove();
  else if ((m = hash.match(/^\/today\/(\d{4}-\d{2}-\d{2})$/))) void renderToday(m[1]);
  else if (hash === "/today") void renderToday("");
  else if (hash === "/welcome") void renderWelcome();
  // The disk browser is gone (it only ever listed the server box's own chats).
  else if (hash.startsWith("/local/")) location.hash = "#/";
  else if (hash === "/pipeline") void renderPipeline();
  else if (hash === "/questions") void renderQuestions();
  else if (hash === "/labels") void renderLabels();
  else if (hash === "/recall") void renderRecall();
  else if (hash === "/entities") void renderEntities("");
  else if (hash.startsWith("/entities/")) void renderEntities(decodeId(hash.slice("/entities/".length)));
  else if (hash === "/team") void renderTeam();
  else if (hash === "/connect") void renderConnect();
  // The operator console is gone. Old bookmarks land on Team, where an admin manages the one
  // workspace they belong to.
  else if (hash === "/operator" || hash.startsWith("/operator/")) location.hash = "#/team";
  else if (hash === "/admin") void renderAdmin();
  else if (hash === "/admin/models") void renderModels();
  else if (hash === "/admin/tokens") void renderTokens();
  else if (hash === "/admin/entity-types") void renderEntityTypes();
  else if ((m = hash.match(/^\/session\/(.+)$/))) void renderDetail(decodeId(m[1]));
  else if (hash === "" || hash === "/") {
    const q = hashQuery.get("q") ?? "";
    if (q !== memQ) {
      memQ = q;
      memOffset = 0;
    }
    void renderHome();
  }
  // Unknown hash: say so. Falling back to Memory made a wrong link look like a working one.
  else renderNotFound(hash);
  const page = pageName(hash);
  if (me && page) track("page_view", page);
}

let routerWired = false; // boot() runs again after a sign-in: wire the hashchange listener ONCE

async function boot(): Promise<void> {
  // A bare URL is an arrival: normalise it to #/welcome (bareArrivalHash), or the landing when signed
  // out, BEFORE the hashchange listener is wired, so this never double-renders. An explicit #/ (the
  // Memory nav item) passes through unchanged.
  location.hash = bareArrivalHash(location.hash);
  app.setAttribute("aria-busy", "true");
  // /api/me doubles as the auth probe: the session cookie is httpOnly, so asking the server is the
  // only way to know whether we are signed in. Catching AuthError here (rather than letting the
  // first view 401) means a signed-out visitor goes straight to the login instead of flashing a
  // half-rendered page. Any OTHER failure keeps going — both are non-critical chrome.
  let signedOut = false;
  [labelLegend, me] = await Promise.all([
    getLabels().catch(() => null),
    getMe().catch((err) => {
      if (err instanceof AuthError) signedOut = true;
      return null;
    }),
  ]);
  if (!routerWired) {
    window.addEventListener("hashchange", route);
    routerWired = true;
  }
  // Signed-out routing (frontDoor owns the rule, so vitest can pin it): the landing shows for a bare
  // arrival, #/ and #/welcome, and only on a server that takes workspace requests (every other server
  // keeps its sign-in as the front door). Everything else goes straight to the sign-in and route()
  // resumes at that hash once signed in.
  if (signedOut) {
    // Only a landing-eligible hash needs the check; a deep link / operator path skips the fetch.
    const requests = signedOutView(location.hash) === "landing" ? (await authConfig()).workspace_requests : false;
    const target = frontDoor(location.hash, requests);
    if (target === "landing") return renderLanding();
    return renderLogin();
  }
  route();
}

// ===================================================================================================
// --- Admin > Analytics: what a head of engineering asks of this workspace, in four sections:
// adoption, memory growth, recall in practice, pipeline health. The numbers come from
// analyzer/analytics.py; definitions, chart math and the delta copy live in admin.ts (tested).
// ===================================================================================================
let analyticsWeeks: AnalyticsWeeks = 12;
const NODE_VAR: Record<string, string> = {
  decision: "--n-decision", gotcha: "--n-gotcha", constraint: "--n-constraint",
  todo: "--n-todo", preference: "--n-pref", rule: "--n-rule",
};
type Tone = "good" | "warn" | "";

// A member gets the notice without a request (the role is known); the 403 branch covers a role that
// changed since sign-in.
function adminOnlyNotice(): void {
  view(el("main", {},
    pageHead("Admin", "This page is for the admins of your workspace."),
    el("p", { class: "hint" }, "Ask a workspace admin for the admin role.")));
}

async function renderAdmin(): Promise<void> {
  if (me && me.role !== "admin") return adminOnlyNotice();
  const weeks = analyticsWeeks;
  let a: WorkspaceAnalytics;
  try {
    a = await getWorkspaceAnalytics(weeks);
  } catch (err) {
    if (err instanceof ForbiddenError) return adminOnlyNotice();
    return showError("load analytics", err);
  }
  if (routePath() !== "/admin" || weeks !== analyticsWeeks) return;   // navigated away, or the window changed again
  const last = a.pipeline.spend_per_day[a.pipeline.spend_per_day.length - 1]?.day ?? a.window.until;
  const main = el("main", { class: "admin analytics" });
  main.append(
    el("div", { class: "admin-head" }, el("h1", { class: "page-title" }, "Analytics"), windowControl()),
    el("p", { class: "explain" },
      `${me?.org_name || me?.org_id || "This workspace"}, ${shortDay(a.window.since)} to ${shortDay(last)}. ${asOf(a.as_of)}. Hover a dotted label for what it counts.`));
  const stall = stallBanner({ stalled: a.pipeline.stalled, reason: a.pipeline.reason });
  if (stall) main.append(stall);
  main.append(adoptionSection(a), memorySection(a), recallSection(a), pipelineSection(a), webSection(a),
    el("p", { class: "hint admin-foot" }, "Counted by this server from what it already stores. No third-party analytics."));
  view(main);
}

// --- Admin > AI Models: the providers this server may call, and the model each role runs on -----
const OPERATOR_SET = "Set by the server operator";
async function renderModels(wizard?: ProviderInfo | "new", flash?: string): Promise<void> {
  if (me && me.role !== "admin") return adminOnlyNotice();
  let v: ProvidersView;
  try {
    const r = await getProviders();
    if (!r.ok) return showError("load AI models", new Error(r.error));
    v = r;
  } catch (err) {
    if (err instanceof ForbiddenError) return adminOnlyNotice();
    return showError("load AI models", err);
  }
  if (routePath() !== "/admin/models") return;
  if (wizard) return view(providerWizard(v, wizard === "new" ? null : wizard));
  const main = el("main", { class: "admin models" });
  const add = el("button", { class: "btn-primary", type: "button" }, "+ Add provider");
  add.addEventListener("click", () => void renderModels("new"));
  if (!v.editable) {
    add.setAttribute("disabled", "");
    add.title = OPERATOR_SET;
  }
  if (v.blocked)
    main.append(el("p", { class: "stall-banner", role: "alert" },
      el("b", {}, "Cannot run the chosen model: "), v.blocked));
  else if (!v.providers.length && v.default.startsWith("NONE"))
    main.append(el("p", { class: "stall-banner", role: "alert" },
      el("b", {}, "No AI provider. "), "New sessions are not being read and recall cannot answer.",
      v.editable ? " Add a provider to start." : " Ask whoever runs this server to add one."));
  main.append(
    el("div", { class: "admin-head" }, el("h1", { class: "page-title" }, "AI Models"), add),
    el("p", { class: "explain" }, !v.editable
      ? "This server hosts several workspaces, so its model providers are set by whoever runs it."
      : v.installed && !v.providers.length
        ? `The model providers this server may send sessions to, and which model reads them. Until you add one, CSA runs on the credential it was installed with: ${v.installed}.`
        : "The model providers this server may send sessions to, and which model reads them."));
  const nModels = v.providers.reduce((a, p) => a + p.models.length, 0);
  main.append(el("div", { class: "tiles" },
    statTile("Providers", fmtInt(v.providers.length), { text: "", tone: "" }),
    statTile("Models", fmtInt(nModels), { text: "exposed to the two roles", tone: "" })));
  main.append(providersTable(v), rolesCard(v, flash));
  view(main);
}

// A provider's chip: its vendor logo (Simple Icons, served from /vendor), white on the brand colour.
function providerMark(kind: ProviderKind): HTMLElement {
  const m = PROVIDER_META[kind];
  const logo = el("img", { class: "prov-logo", src: `/vendor/${kind}.svg`, alt: "", loading: "lazy" });
  logo.addEventListener("error", () => logo.replaceWith(document.createTextNode(m.mark)));   // offline build: fall back to the lettermark
  return el("span", { class: "prov-mark", style: `--mk:${m.accent}`, "aria-hidden": "true" }, logo);
}

// The chip of a provider that is not the vendor its type is named for: the first letter of the admin's name for it.
function nameMark(name: string): HTMLElement {
  return el("span", { class: "prov-mark", style: "--mk:#6b6f76", "aria-hidden": "true" }, name.trim().charAt(0).toUpperCase());
}

function providersTable(v: ProvidersView): HTMLElement {
  const table = el("table", { class: "sessions" });
  table.append(el("thead", {}, el("tr", {},
    el("th", {}, "Provider"), el("th", { class: "num" }, "Models"), el("th", {}, "Last updated"), el("th", {}, ""))));
  const body = el("tbody", {});
  if (!v.providers.length)
    body.append(el("tr", {}, el("td", { colspan: "4", class: "hint" }, v.default.startsWith("NONE")
      ? "No providers yet."
      : `No providers yet. Running on the credential CSA was installed with: ${v.default}.`)));
  for (const p of v.providers) {
    const other = otherVendor(p);
    const note = kindNote(p.kind, v.unavailable_kinds);
    const actions = el("span", { class: "row-actions" });
    if (v.editable) {
      const edit = el("button", { class: "btn btn-small", type: "button" }, "Edit");
      edit.addEventListener("click", () => void renderModels(p));
      const del = el("button", { class: "btn btn-small btn-danger", type: "button" }, "Remove");
      del.addEventListener("click", async () => {
        if (!confirm(`Remove ${p.name}? A role that uses its models goes back to the default.`)) return;
        const r = await removeProvider(p.id);
        if (r.ok) void renderModels();
        else showError("remove the provider", new Error(r.error));
      });
      actions.append(edit, del);
    }
    body.append(el("tr", {},
      el("td", {}, el("div", { class: "prov-cell" }, other ? nameMark(p.name) : providerMark(p.kind),
        el("div", { class: "prov-id" }, el("div", { class: "prov-name" }, p.name),
          el("div", { class: "prov-where", ...(other ? { title: `Through the ${kindLabel(p)} API` } : {}) }, providerLine(p)),
          ...(note ? [el("div", { class: "prov-note" }, note)] : [])))),
      el("td", { class: "num" }, `${p.models.length} model${p.models.length === 1 ? "" : "s"}`),
      el("td", { class: "when" }, p.updated_at ? fmtWhen(p.updated_at) : ""),
      el("td", {}, actions)));
  }
  table.append(body);
  return el("div", { class: "table-scroll" }, table);
}

// The Add provider wizard, four steps in one card: a step rail on the left, the step's content
// in the middle, Back/Next in a footer. Editing jumps straight to Connection and locks the type.
function providerWizard(v: ProvidersView, p: ProviderInfo | null): HTMLElement {
  let kind: ProviderKind = p?.kind ?? "openai";
  let step: WizardStep = p ? "connection" : "type";
  const field: Record<string, string> = { name: p?.name ?? "", endpoint: p?.endpoint ?? "", region: p?.region ?? "",
    project: p?.project ?? "", api_version: p?.api_version ?? "", credential: "", models: (p?.models ?? []).join(", ") };
  const content = el("div", { class: "wiz-content" });
  const err = el("p", { class: "set-err", role: "alert" });
  err.hidden = true;
  const status = el("span", { class: "set-status", role: "status" });
  const back = el("button", { class: "btn", type: "button" }, "Back");
  const next = el("button", { class: "btn-primary", type: "button" }, "Next");
  const steps = p ? WIZARD_STEPS.filter((s) => s.id !== "type") : WIZARD_STEPS;
  const idx = () => steps.findIndex((s) => s.id === step);
  const typed = () => kindLabel({ kind, endpoint: field.endpoint.trim(), region: field.region, project: field.project });

  const rail = el("nav", { class: "wiz-rail", "aria-label": "Add provider steps" });
  const paintRail = () => {
    rail.replaceChildren();
    steps.forEach((s, i) => {
      const here = s.id === step, done = i < idx();
      rail.append(el("div", { class: `wiz-step${here ? " on" : ""}${done ? " done" : ""}` },
        el("span", { class: "wiz-dot" }, done ? "✓" : icon(s.icon)), el("span", {}, s.label)));
    });
  };

  const field_ = (key: string, label: string, opts: { pw?: boolean; multi?: boolean; ph?: string; optional?: boolean } = {}) => {
    const input = (opts.multi ? el("textarea", { id: `wz-${key}`, rows: "4" })
      : el("input", { id: `wz-${key}`, type: opts.pw ? "password" : "text", autocomplete: "off" })) as HTMLInputElement | HTMLTextAreaElement;
    input.value = field[key];
    if (opts.ph) input.placeholder = opts.ph;
    input.addEventListener("input", () => { field[key] = input.value; });
    return el("div", { class: "set-row" },
      el("label", { class: "set-label", for: `wz-${key}` }, label + (opts.optional ? " (optional)" : "")), input);
  };

  const paint = () => {
    paintRail();
    content.replaceChildren();
    err.hidden = true;
    status.textContent = "";
    back.textContent = idx() === 0 ? "Cancel" : "Back";
    next.textContent = step === "review" ? (p ? "Save provider" : "Create provider") : "Next";
    if (step === "type") {
      const grid = el("div", { class: "wiz-grid" });
      for (const k of Object.keys(v.kinds) as ProviderKind[]) {
        const m = PROVIDER_META[k];
        const note = kindNote(k, v.unavailable_kinds);
        const card = el("button", { class: `wiz-prov${k === kind && !note ? " on" : ""}`, type: "button", ...(note ? { disabled: "" } : {}) },
          providerMark(k), el("span", { class: "wiz-prov-name" }, m.label), el("span", { class: "wiz-prov-host" }, m.host),
          ...(note ? [el("span", { class: "wiz-prov-note" }, note)] : []));
        card.addEventListener("click", () => { kind = k; field.credential = ""; step = "connection"; paint(); });
        grid.append(card);
      }
      content.append(el("h3", { class: "wiz-head" }, "Choose a provider"),
        el("p", { class: "set-help" }, "Where CSA sends sessions to read them."), grid);
    } else if (step === "connection") {
      const rows = el("div", { class: "set-rows" }, field_("name", "Name", { ph: `${typed()} for your team` }));
      for (const f of KIND_FIELDS[kind])
        rows.append(field_(f.key, f.label, { pw: f.key === "credential", multi: f.multiline,
          ph: f.key === "credential" && p?.credential_set ? "Stored. Leave blank to keep it." : f.placeholder, optional: f.optional }));
      const head = el("h3", { class: "wiz-head" }, `${typed()} connection`);
      rows.querySelector("#wz-endpoint")?.addEventListener("input", () => {
        head.textContent = `${typed()} connection`;
        (rows.querySelector("#wz-name") as HTMLInputElement).placeholder = `${typed()} for your team`;
      });
      content.append(head, rows);
    } else if (step === "models") {
      const ta = el("textarea", { id: "wz-models", rows: "3" }) as HTMLTextAreaElement;
      ta.value = field.models;
      ta.placeholder = MODELS_HINT[kind];
      ta.addEventListener("input", () => { field.models = ta.value; });
      content.append(el("h3", { class: "wiz-head" }, "Models"),
        el("p", { class: "set-help" }, "The model ids this provider serves, separated by commas or new lines. The first one answers a short test call before the provider is saved."),
        el("div", { class: "set-row" }, ta));
    } else {
      const parsed = parseModels(field.models);
      const rows: [string, string][] = [["Type", typed()], ["Name", field.name],
        ["Models", parsed.join(", ") || "none"]];
      if (field.region) rows.push(["Region", field.region]);
      if (field.project) rows.push(["Project", field.project]);
      if (field.endpoint) rows.push(["Endpoint", field.endpoint]);
      rows.push(["Key", field.credential ? "entered" : p?.credential_set ? "kept" : "missing"]);
      const dl = el("dl", { class: "wiz-review" });
      for (const [k, val] of rows) dl.append(el("dt", {}, k), el("dd", {}, val));
      content.append(el("h3", { class: "wiz-head" }, "Review"),
        el("p", { class: "set-help" }, "Save runs one short test call to this provider. Nothing is stored if it does not answer."), dl);
    }
  };

  const submit = async () => {
    const body: ProviderForm = { kind, name: field.name.trim(), models: parseModels(field.models),
      endpoint: field.endpoint.trim(), region: field.region.trim(), project: field.project.trim(),
      api_version: field.api_version.trim(), credential: field.credential.trim() };
    if (p) body.id = p.id;
    next.setAttribute("disabled", "");
    err.hidden = true;
    status.textContent = "Testing with one short call…";
    const r = await saveProvider(body).catch((x) => { if (x instanceof AuthError) throw x; return { ok: false as const, error: "Could not reach the server." }; });
    next.removeAttribute("disabled");
    status.textContent = "";
    if (!r.ok) { err.textContent = r.error; err.hidden = false; return; }
    void renderModels(undefined, p ? "Provider updated." : "Provider added.");
  };

  back.addEventListener("click", () => { if (idx() === 0) return void renderModels(); step = steps[idx() - 1].id; paint(); });
  next.addEventListener("click", () => {
    if (step === "review") return void submit();
    if (step === "connection" && !field.name.trim()) { err.textContent = "Name the provider."; err.hidden = false; return; }
    step = steps[idx() + 1].id;
    paint();
  });
  paint();
  return el("main", { class: "admin models" },
    el("div", { class: "admin-head" }, el("h1", { class: "page-title" }, p ? "Edit provider" : "Add provider")),
    el("div", { class: "wiz card" }, rail, el("div", { class: "wiz-main" }, content,
      el("div", { class: "set-actions wiz-foot" }, status, err, back, next))));
}

function rolesCard(v: ProvidersView, flash?: string): HTMLElement {
  const envDefault: Record<string, string> = { main: "sonnet", small: "haiku" };
  const pick = (role: "main" | "small", current: string) => {
    const sel = el("select", { id: `role-${role}`, class: "role-pick" }) as HTMLSelectElement;
    for (const o of modelOptions(v.providers, current, envDefault[role], v.installed)) sel.append(el("option", { value: o.value, title: o.label }, o.label));
    sel.value = current;
    sel.disabled = !v.editable;
    return sel;
  };
  const main = pick("main", v.main_model), small = pick("small", v.small_model);
  const err = el("p", { class: "set-err", role: "alert" });
  err.hidden = true;
  const status = el("span", { class: "set-status", role: "status" }, flash ?? (v.editable ? "" : OPERATOR_SET));
  const save = el("button", { class: "btn-primary", type: "button", disabled: "" }, "Save") as HTMLButtonElement;
  const dirty = () => { save.toggleAttribute("disabled", main.value === v.main_model && small.value === v.small_model); if (flash) status.textContent = ""; };
  main.addEventListener("change", dirty);
  small.addEventListener("change", dirty);
  save.addEventListener("click", async () => {
    const changes: { main_model?: string; small_model?: string } = {};
    if (main.value !== v.main_model) changes.main_model = main.value;
    if (small.value !== v.small_model) changes.small_model = small.value;
    save.disabled = true;
    err.hidden = true;
    status.textContent = "Testing the model with one short call…";
    const r = await saveModels(changes);
    if (!r.ok) {
      status.textContent = "";
      err.textContent = r.error;
      err.hidden = false;
      save.disabled = false;
      return;
    }
    void renderModels(undefined, "Models saved.");
  });
  return el("section", { class: "card roles" },
    el("h3", { class: "sec-head" }, "Default models"),
    el("div", { class: "set-row" }, el("label", { class: "set-label", for: "role-main" }, "Main model"), main,
      el("p", { class: "set-help" }, "Reads each session and writes its facts, and answers questions. Pick your larger model here.")),
    el("div", { class: "set-row" }, el("label", { class: "set-label", for: "role-small" }, "Small model"), small,
      el("p", { class: "set-help" }, "Judges facts and routes questions. Fast and cheap: a Haiku, GPT-5 mini or Gemini Flash class model.")),
    err, el("div", { class: "set-actions" }, status, save));
}

// --- Admin > Service Tokens: machines that ship and ask without signing in -----------------------
async function renderTokens(): Promise<void> {
  if (me && me.role !== "admin") return adminOnlyNotice();
  let members: OrgMember[];
  try {
    members = (await getOrgMembers()).members;
  } catch (err) {
    if (err instanceof ForbiddenError) return adminOnlyNotice();
    return showError("load service tokens", err);
  }
  if (routePath() !== "/admin/tokens") return;
  const main = el("main", { class: "admin" });
  const slot = el("div", {});
  const tableWrap = el("div", { class: "table-scroll" }, tokensTable(members));
  // the reveal says "find it in the table", so the new row has to be there before Done
  const refresh = () => void getOrgMembers().then((d) => tableWrap.replaceChildren(tokensTable(d.members))).catch(() => undefined);
  const add = el("button", { class: "btn-primary", type: "button" }, "+ Create token");
  add.addEventListener("click", () => {
    if (slot.firstChild) return void slot.replaceChildren();
    slot.replaceChildren(el("section", { class: "card" },
      el("h3", { class: "sec-head" }, SERVICE.head), serviceTokenForm(() => void renderTokens(), refresh)));
    (slot.querySelector("#svc-name") as HTMLInputElement | null)?.focus();
  });
  main.append(
    el("div", { class: "admin-head" }, el("h1", { class: "page-title" }, "Service Tokens"), add),
    el("p", { class: "explain" }, "Tokens for agents and pipelines that cannot sign in. A token ships sessions and asks questions as the email it names, always as a member."),
    slot, tableWrap);
  view(main);
}

function tokensTable(members: OrgMember[]): HTMLElement {
  const tokens = members.filter((m) => m.service && m.active);
  const table = el("table", { class: "sessions" });
  table.append(el("thead", {}, el("tr", {},
    el("th", {}, "Machine"), el("th", {}, "Ships and asks as"), el("th", {}, "Created"), el("th", {}, "Last used"), el("th", {}, ""))));
  const body = el("tbody", {});
  if (!tokens.length) body.append(el("tr", {}, el("td", { colspan: "5", class: "hint" }, "No service tokens yet.")));
  for (const t of tokens) {
    const status = el("span", { class: "flag-status", role: "status" });
    const kill = el("button", { class: "btn btn-small", type: "button" }, "Revoke") as HTMLButtonElement;
    kill.addEventListener("click", async () => {
      if (!confirm(`Revoke ${t.hostname}? It stops shipping and asking immediately.`)) return;
      kill.disabled = true;
      const r = await revokeMember(t.token_hash);
      if (r.ok) return void renderTokens();
      kill.disabled = false;
      status.textContent = r.error;
      status.className = "flag-status err";
    });
    body.append(el("tr", {},
      el("td", {}, t.hostname ?? ""), el("td", {}, t.email ?? ""),
      el("td", { class: "when" }, t.created ? fmtWhen(t.created) : ""),
      el("td", { class: "when" }, t.last_used_at ? fmtWhen(t.last_used_at) : "never"),
      el("td", {}, kill, status)));
  }
  table.append(body);
  return table;
}

function windowControl(): HTMLElement {
  const bar = el("div", { class: "scope", role: "group", "aria-label": "date range" });
  for (const { weeks, label } of WINDOWS) {
    const on = weeks === analyticsWeeks;
    const b = el("button", { class: `scope-btn${on ? " active" : ""}`, type: "button", "aria-pressed": String(on) }, label);
    b.addEventListener("click", () => {
      if (analyticsWeeks === weeks) return;
      analyticsWeeks = weeks;
      void renderAdmin();          // the old frame stays up until the new numbers land
    });
    bar.append(b);
  }
  return bar;
}

// A label that explains itself on hover and on keyboard focus (a tap focuses it on a phone).
function defined(tag: "span" | "h3", cls: string, text: string, def: string): HTMLElement {
  if (!def) return el(tag, { class: cls }, text);
  return el(tag, { class: `${cls} has-def`, tabindex: "0", "data-def": def, "aria-description": def }, text);
}

// One tile: label, the number, one line that says how it moved (the only place good / warn appear),
// and an optional second line of context. `def` puts the metric's definition on the label.
function statTile(label: string, value: string, line: { text: string; tone: Tone }, more = "", def = ""): HTMLElement {
  const t = el("article", { class: "tile-stat" },
    defined("span", "tile-label", label, def),
    el("span", { class: "tile-value" }, value),
    el("span", { class: `tile-delta${line.tone ? " tile-" + line.tone : ""}` }, line.text));
  if (more) t.append(el("span", { class: "tile-more" }, more));
  return t;
}

// upIsGood null = a neutral number (spend): the arrow shows, the colour does not.
function moved(cur: number, prev: number, weeks: number, upIsGood: boolean | null, fmt = fmtInt): { text: string; tone: Tone } {
  const x = deltaLine(cur, prev, weeks, fmt);
  const arrow = x.dir === "up" ? "↑ " : x.dir === "down" ? "↓ " : "";
  const tone: Tone = upIsGood === null || x.dir === "flat" || x.dir === "none" ? "" : (x.dir === "up") === upIsGood ? "good" : "warn";
  return { text: arrow + x.text, tone };
}

function anSection(title: string, sub: string, ...nodes: Node[]): HTMLElement {
  return el("section", { class: "an-section", "aria-label": title },
    el("h2", { class: "an-head" }, title), el("p", { class: "an-sub" }, sub), ...nodes);
}

function chartCard(title: string, sub: string, body: Node, def = ""): HTMLElement {
  return el("section", { class: "chart-card" }, defined("h3", "", title, def), el("p", { class: "chart-sub" }, sub), body);
}

function tableCard(title: string, def: string, head: string[], rows: HTMLElement[], empty: string): HTMLElement {
  const table = el("table", { class: "sessions" });
  table.append(el("thead", {}, el("tr", {}, ...head.map((h, i) => el("th", i ? { class: "num" } : {}, h)))));
  const body = el("tbody", {});
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: String(head.length), class: "hint" }, empty)));
  body.append(...rows);
  table.append(body);
  return el("section", { class: "admin-section" }, defined("h3", "", title, def), el("div", { class: "table-scroll" }, table));
}

function swatch(cssVar: string): HTMLElement {
  return el("span", { class: "swatch", style: `background: var(${cssVar})`, "aria-hidden": "true" });
}

function legend(series: { name: string; color: string }[]): HTMLElement {
  return el("div", { class: "legend" }, ...series.map((s) => el("span", { class: "legend-item" }, swatch(s.color), s.name)));
}

function adoptionSection(a: WorkspaceAnalytics): HTMLElement {
  const d = a.adoption, w = a.window.weeks;
  const tiles = el("div", { class: "tiles" },
    statTile("Active contributors", fmtInt(d.contributors), moved(d.contributors, d.contributors_prev, w, true),
      contributorsNote(d),
      DEFS.contributors),
    statTile("Sessions shipped", fmtInt(d.sessions), moved(d.sessions, d.sessions_prev, w, true),
      [d.contributors ? `${((d.sessions - d.agent_sessions) / d.contributors).toFixed(1)} per person` : "",
        d.agent_sessions ? `${fmtInt(d.agent_sessions)} from agents` : ""].filter(Boolean).join(" · "), DEFS.sessions),
    statTile("Silent machines", fmtInt(d.silent.length),
      d.silent.length ? { text: "no contact for over 7 days", tone: "warn" } : { text: "every machine checked in this week", tone: "" },
      "", DEFS.silent));
  const people = d.per_week.map((r) => r.people);
  const charts = el("div", { class: "charts" },
    chartCard("Sessions shipped per week", d.sessions ? `${count(d.sessions, "session")} from ${peopleLine(d.contributors, d.agents)}` : "No sessions in this window",
      barChart(weekLabels(a.week_starts), [{ name: "sessions", color: "--ink" }], d.per_week.map((r) => [r.sessions]),
        (i) => count(people[i], "person", "people"))),
    membersCard(d));
  const out = anSection("Adoption", "Who is shipping Claude Code sessions to CSA.", tiles, charts);
  if (d.silent.length)
    out.append(tableCard("Silent machines", DEFS.silent, ["Machine", "Person", "Last contact"],
      d.silent.map((r) => el("tr", {},
        el("td", {}, r.hostname, r.expired ? el("span", { class: "chip chip-stale" }, "signed out") : ""),
        el("td", { class: "num" }, r.user), el("td", { class: "num when" }, ago(r.last_used_at, Date.now())))), ""));
  return out;
}

function membersCard(d: WorkspaceAnalytics["adoption"]): HTMLElement {
  const parts: [string, number, string][] = [["Shipping", d.members_shipping, "--n-decision"],
    ["Quiet", d.members_quiet, "--n-todo"], ["Not connected", d.members_unconnected, "--n-grey"]];
  const bar = el("div", { class: "split-bar", role: "img",
    "aria-label": parts.map(([l, n]) => `${l} ${n}`).join(", ") });
  for (const [label, n, color] of parts)
    if (n > 0) bar.append(el("span", { class: "split-seg", style: `flex: ${n}; background: var(${color})`, title: `${label}: ${n}` }));
  const rows = el("div", { class: "split-legend" }, ...parts.map(([label, n, color]) =>
    el("span", { class: "legend-item" }, swatch(color), `${label} `, el("b", {}, fmtInt(n)))));
  return chartCard("Members", membersLine(d), el("div", {}, bar, rows), DEFS.members);
}

function memorySection(a: WorkspaceAnalytics): HTMLElement {
  const m = a.memory, w = a.window.weeks;
  const tiles = el("div", { class: "tiles" },
    statTile("Facts in memory", fmtInt(m.total), { text: `${fmtInt(m.new)} new in this window`, tone: "" }, "", DEFS.memoryTotal),
    statTile("New facts", fmtInt(m.new), moved(m.new, m.new_prev, w, true),
      a.adoption.sessions ? `${(m.new / a.adoption.sessions).toFixed(1)} per session shipped` : "", DEFS.memoryNew));
  const series = m.types.map((t) => ({ name: t, color: NODE_VAR[t] ?? "--n-grey" }));
  const chart = chartCard("New facts per week, by type", m.new ? `${count(m.new, "fact")} first extracted in this window` : "No new facts in this window",
    el("div", {}, series.length > 1 ? legend(series) : "", barChart(weekLabels(a.week_starts), series.length ? series : [{ name: "facts", color: "--ink" }],
      m.per_week.map((r) => (r.length ? r : [0])))), DEFS.memoryNew);
  const people = tableCard("Top contributors", DEFS.contributorsTable, ["Person", "New facts", "Teammates reached"],
    m.contributors.map((c) => el("tr", {}, el("td", {}, c.user, c.agent ? el("span", { class: "hint" }, " agent") : ""),
      el("td", { class: "num" }, fmtInt(c.facts)),
      el("td", { class: "num" }, fmtInt(c.reached)))), "Nobody added facts in this window.");
  return anSection("Memory", "What the team's sessions taught CSA.", tiles, el("div", { class: "charts" }, chart, people));
}

function recallSection(a: WorkspaceAnalytics): HTMLElement {
  const r = a.recall, t = r.transfer, w = a.window.weeks, acted = actedTile(r);
  const tiles = el("div", { class: "tiles" },
    statTile("Recall requests", fmtInt(r.asks), moved(r.asks, r.asks_prev, w, true),
      `${fmtInt(r.ambient.asks)} automatic · ${fmtInt(r.asked.asks)} asked`, DEFS.asks),
    statTile("Answered", pct(r.answered, r.asks),
      { text: r.asks ? `${fmtInt(r.answered)} of ${fmtInt(r.asks)}, the rest honest misses` : "nothing asked yet", tone: "" },
      r.asks ? `automatic ${pct(r.ambient.answered, r.ambient.asks)} · asked ${pct(r.asked.answered, r.asked.asks)}` : "", DEFS.answered),
    statTile("Acted on", acted.value, { text: acted.note, tone: "" }, "", DEFS.acted),
    statTile("Facts reused", fmtInt(t.facts), moved(t.facts, t.facts_prev, w, true), reuseNote(t), DEFS.transfer));
  const labels = weekLabels(a.week_starts);
  const charts = el("div", { class: "charts" },
    chartCard("Recall requests per week", r.asks ? `${pct(r.answered, r.asks)} answered over ${fmtInt(r.asks)} requests` : "No recall requests in this window",
      el("div", {}, legend([{ name: "answered", color: "--n-constraint" }, { name: "honest miss", color: "--n-grey" }]),
        barChart(labels, [{ name: "answered", color: "--n-constraint" }, { name: "honest miss", color: "--n-grey" }],
          r.per_week.map((x) => [x.answered, x.asks - x.answered]), (i) => `${fmtInt(r.per_week[i].acted)} acted on`)), DEFS.answered),
    chartCard("Handoffs to a teammate per week", t.deliveries ? `${count(t.deliveries, "handoff")} of ${count(t.facts, "fact")} to ${peopleLine(t.receivers, t.receiving_agents)}` : "No fact reached a teammate in this window",
      barChart(labels, [{ name: "handoffs", color: "--ink" }], t.per_week.map((n) => [n])), DEFS.transfer));
  const top = tableCard("Most reused facts", DEFS.topReused, ["Fact", "Author", "People", "Sessions", "Last served"],
    r.top_reused.map((f) => el("tr", {},
      el("td", { class: "an-claim" }, swatch(NODE_VAR[f.type ?? ""] ?? "--n-grey"),
        f.session_id ? el("a", { href: sessionHref(f.session_id, f.ref) }, f.claim ?? f.ref) : (f.claim ?? f.ref)),
      el("td", { class: "num" }, f.author ?? ""), el("td", { class: "num" }, fmtInt(f.people)),
      el("td", { class: "num" }, fmtInt(f.sessions)), el("td", { class: "num when" }, ago(f.last_served, Date.now())))),
    "No fact reached a teammate in this window.");
  return anSection("Recall", "Whether what one person learned reaches the next session.", tiles, charts, top);
}

function pipelineSection(a: WorkspaceAnalytics): HTMLElement {
  const p = a.pipeline, w = a.window.weeks;
  const costs = [p.per_extracted !== null ? `${fmtUsd(p.per_extracted)} per session read` : "",
    p.per_contributor !== null ? `${fmtUsd(p.per_contributor)} per contributor` : ""].filter(Boolean).join(" · ");
  const tokens = `${fmtTok(p.tokens)} tokens`;
  const tiles = el("div", { class: "tiles" },
    statTile("Waiting to be read", fmtInt(p.backlog),
      p.poisoned ? { text: `${fmtInt(p.poisoned)} gave up`, tone: "warn" } : { text: p.stalled ? "extraction is stalled" : "nothing stuck", tone: p.stalled ? "warn" : "" },
      "", DEFS.backlog),
    p.unpriced
      ? statTile(CSA_SPEND, tokens, { text: UNPRICED, tone: "" },
        p.spend_usd ? `${fmtUsd(p.spend_usd)} of it priced` : "", DEFS.spend)
      : statTile(CSA_SPEND, fmtUsd(p.spend_usd), moved(p.spend_usd, p.spend_prev, w, null, fmtUsd), costs, DEFS.spend));
  if (p.daily_cap_usd > 0)
    tiles.append(statTile("Daily cap", fmtUsd(p.daily_cap_usd),
      p.days_over_cap ? { text: `reached on ${p.days_over_cap} ${p.days_over_cap === 1 ? "day" : "days"}`, tone: "warn" }
        : { text: "never reached", tone: "good" }, "", DEFS.cap));
  const days = p.spend_per_day.map((r) => r.day);
  const read = count(p.extracted, "session");
  const chart = p.unpriced
    ? chartCard("CSA extraction tokens per day", `${tokens} for ${read} read`,
      lineChart(days, p.spend_per_day.map((r) => r.tokens), fmtTok, "tokens", true), DEFS.spend)
    : chartCard(`${CSA_SPEND} per day`, p.spend_usd ? `${fmtUsd(p.spend_usd)} for ${read} read` : "No spend in this window",
      lineChart(days, p.spend_per_day.map((r) => r.usd), fmtUsd, "spent", false, p.daily_cap_usd), DEFS.spend);
  return anSection("Pipeline", "Whether sessions are being read, and what reading them costs.", el("div", { class: "an-pair" }, tiles, chart));
}

// Product usage of this web app, last: it says how the dashboard is used, not what CSA is worth.
function webSection(a: WorkspaceAnalytics): HTMLElement {
  const u = a.web, use = webUse(u);
  const card = el("section", { class: "admin-section an-web" }, el("h3", {}, use.title));
  if (use.empty) {
    card.append(el("p", { class: "hint" }, use.empty));
    return card;
  }
  const n = (v: number, label: string) => el("span", { class: "usage-n" }, el("b", {}, fmtInt(v)), el("span", {}, label));
  const max = Math.max(1, ...u.pages.map((p) => p.n));
  const pages = el("div", { class: "usage-pages" }, ...u.pages.map((p) => el("div", { class: "usage-page" },
    el("span", { class: "usage-page-name" }, p.page),
    el("span", { class: "usage-bar" }, el("i", { style: `width: ${(p.n / max) * 100}%` })),
    el("span", { class: "usage-page-n" }, fmtInt(p.n)))));
  card.append(
    el("div", { class: "usage-row" }, n(u.people, "people opened it"), n(u.searches, "searches"),
      n(u.session_opens, "sessions opened"), n(u.flags, "facts rated")),
    u.pages.length ? pages : el("p", { class: "hint" }, "No page views in this window."));
  return card;
}

const CW = 560, CH = 200, PL = 44, PR = 14, PT = 12, PB = 26;   // the SVG box and its plot margins

// Line + area over evenly spaced days, with an optional dashed cap line. Hover (or a touch) snaps a
// hairline to the nearest day and shows its value; the headline above carries the total.
function lineChart(days: string[], values: number[], fmt: (n: number) => string, unit: string, integer = false, cap = 0): HTMLElement {
  const w = CW - PL - PR, hgt = CH - PT - PB;
  const tk = ticks(Math.max(0, cap, ...values), 5, integer);
  const top = tk[tk.length - 1];
  const dx = values.length > 1 ? w / (values.length - 1) : 0;
  const svg = svgEl("svg", { class: "chart-svg", viewBox: `0 0 ${CW} ${CH}`, role: "img", "aria-label": `${unit} per day` });
  const g = svgEl("g", { transform: `translate(${PL},${PT})` });
  for (const v of tk) {
    const y = yOf(v, top, hgt).toFixed(1);
    g.append(svgEl("line", { class: "grid", x1: "0", x2: String(w), y1: y, y2: y }),
      svgEl("text", { class: "axis", x: "-8", y: String(+y + 3.5), "text-anchor": "end" }, fmt(v)));
  }
  for (const i of labelIndexes(values.length)) {
    const anchor = i === 0 ? "start" : i === values.length - 1 ? "end" : "middle";
    g.append(svgEl("text", { class: "axis", x: (i * dx).toFixed(1), y: String(hgt + 18), "text-anchor": anchor }, shortDay(days[i])));
  }
  if (cap > 0) {
    const y = yOf(cap, top, hgt).toFixed(1);
    g.append(svgEl("line", { class: "cap", x1: "0", x2: String(w), y1: y, y2: y }),
      svgEl("text", { class: "axis", x: String(w), y: String(+y - 5), "text-anchor": "end" }, `daily cap ${fmt(cap)}`));
  }
  g.append(svgEl("path", { class: "area", d: areaPath(values, w, hgt, top) }),
    svgEl("path", { class: "line", d: linePath(values, w, hgt, top) }));
  const dots = values.length <= 14 ? values.map((_, i) => i) : [values.length - 1];
  for (const i of dots)
    g.append(svgEl("circle", { class: "dot", cx: (i * dx).toFixed(1), cy: yOf(values[i], top, hgt).toFixed(1), r: "3.5" }));
  const hair = svgEl("line", { class: "hair", y1: "0", y2: String(hgt) });
  const hdot = svgEl("circle", { class: "hdot", r: "4.5" });
  hair.setAttribute("visibility", "hidden");
  hdot.setAttribute("visibility", "hidden");
  const hit = svgEl("rect", { x: "0", y: "0", width: String(w), height: String(hgt), fill: "transparent" });
  g.append(hair, hdot, hit);
  svg.append(g);
  const tip = el("div", { class: "chart-tip", role: "status" });
  tip.hidden = true;
  const wrap = el("div", { class: "chart" }, svg, tip);
  const show = (e: PointerEvent): void => {
    if (!values.length) return;
    const rect = svg.getBoundingClientRect();
    const scale = rect.width / CW;
    const i = Math.max(0, Math.min(values.length - 1, Math.round(((e.clientX - rect.left) / scale - PL) / (dx || 1))));
    const x = (i * dx).toFixed(1), y = yOf(values[i], top, hgt).toFixed(1);
    hair.setAttribute("x1", x); hair.setAttribute("x2", x); hair.setAttribute("visibility", "visible");
    hdot.setAttribute("cx", x); hdot.setAttribute("cy", y); hdot.setAttribute("visibility", "visible");
    tip.replaceChildren(el("b", {}, `${fmt(values[i])} ${unit}`), ` · ${shortDay(days[i])}`);
    tip.hidden = false;
    tip.style.left = `${(PL + i * dx) * scale}px`;
    tip.style.top = `${(PT + +y) * scale}px`;
  };
  const hide = (): void => {
    hair.setAttribute("visibility", "hidden");
    hdot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  };
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", show);
  hit.addEventListener("pointerleave", hide);
  return wrap;
}

// Stacked bars, one per week, each segment in its series' colour with a rounded cap on the top one.
// Hover (or a touch) on a week shows every segment's count, the total and `note(i)`; a legend above
// the chart names the series, so identity is never colour alone.
function barChart(labels: string[], series: { name: string; color: string }[], rows: number[][], note?: (i: number) => string): HTMLElement {
  const w = CW - PL - PR, hgt = CH - PT - PB;
  const totals = stackTotals(rows);
  const tk = ticks(Math.max(0, ...totals), 5, true);
  const top = tk[tk.length - 1];
  const svg = svgEl("svg", { class: "chart-svg", viewBox: `0 0 ${CW} ${CH}`, role: "img",
    "aria-label": `${series.map((s) => s.name).join(" and ")} per week` });
  const g = svgEl("g", { transform: `translate(${PL},${PT})` });
  for (const v of tk) {
    const y = yOf(v, top, hgt).toFixed(1);
    g.append(svgEl("line", { class: "grid", x1: "0", x2: String(w), y1: y, y2: y }),
      svgEl("text", { class: "axis", x: "-8", y: String(+y + 3.5), "text-anchor": "end" }, fmtInt(v)));
  }
  const band = rows.length ? w / rows.length : w;
  const bw = Math.min(28, Math.max(6, band * 0.6));
  const tip = el("div", { class: "chart-tip", role: "status" });
  tip.hidden = true;
  for (const i of labelIndexes(rows.length, 7))
    g.append(svgEl("text", { class: "axis", x: (i * band + band / 2).toFixed(1), y: String(hgt + 18), "text-anchor": "middle" }, labels[i]));
  rows.forEach((row, i) => {
    const x = i * band + (band - bw) / 2;
    const bar = svgEl("g", { class: "bar" });
    const last = row.reduce((k, v, j) => (v > 0 ? j : k), -1);
    let base = hgt;
    row.forEach((v, j) => {
      if (v <= 0) return;
      const yTop = yOf(totals[i] - row.slice(j + 1).reduce((a, b) => a + b, 0), top, hgt);
      const r = j === last ? Math.min(4, base - yTop) : 0;
      bar.append(svgEl("path", { fill: `var(${series[j].color})`,
        d: `M${x},${base} V${yTop + r} Q${x},${yTop} ${x + r},${yTop} H${x + bw - r} Q${x + bw},${yTop} ${x + bw},${yTop + r} V${base} Z` }));
      base = yTop;
    });
    const hit = svgEl("rect", { x: (i * band).toFixed(1), y: "0", width: band.toFixed(1), height: String(hgt), fill: "transparent" });
    const show = (): void => {
      const lines: (Node | string)[] = [el("b", {}, `Week of ${labels[i]}`)];
      if (series.length > 1) series.forEach((s, j) => lines.push(` · ${s.name} ${fmtInt(row[j] ?? 0)}`));
      else lines.push(` · ${fmtInt(totals[i])} ${series[0].name}`);
      if (note) lines.push(` · ${note(i)}`);
      tip.replaceChildren(...lines);
      tip.hidden = false;
      const scale = svg.getBoundingClientRect().width / CW;
      tip.style.left = `${(PL + x + bw / 2) * scale}px`;
      tip.style.top = `${(PT + base) * scale}px`;
      bar.classList.add("on");
    };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", () => { tip.hidden = true; bar.classList.remove("on"); });
    g.append(bar, hit);
  });
  svg.append(g);
  return el("div", { class: "chart" }, svg, tip);
}


void sidebar;
void renderSearchHome;
document.documentElement.removeAttribute("data-theme");
void boot();

// --- Memory's Graph view, rules in the feed, the Sessions pane -------------------------------------

// The choice rides in the hash (`#/?view=graph`) so a reload and Back keep it.
function memoryViewSwitch(current: "list" | "graph"): HTMLElement {
  const bar = el("div", { class: "scope view-switch", role: "group", "aria-label": "list or graph" });
  for (const [id, label] of [["list", "List"], ["graph", "Graph"]] as const) {
    const on = id === current;
    const b = el("button", { class: `scope-btn${on ? " active" : ""}`, type: "button", "aria-pressed": String(on) }, label);
    b.addEventListener("click", () => {
      if (on) return;
      memOffset = 0;
      location.hash = id === "graph" ? graphHash() : "#/";
    });
    bar.append(b);
  }
  return bar;
}

// A rule is counted from the sessions' own metrics, so it carries no quote, no shipper and no verdicts.
function rulesBlock(rules: FactRow[]): HTMLElement {
  const wrap = el("section", { class: "rules", "aria-label": "project rules" });
  wrap.append(el("p", { class: "hint result-count" },
    `${rules.length} project ${rules.length === 1 ? "rule" : "rules"}, counted from the sessions' own metrics`));
  const list = el("div", { class: "facts" });
  for (const r of rules) list.append(ruleCard(r));
  wrap.append(list);
  return wrap;
}

// The view, the window and the people, so a shared link reopens exactly this. Empty `owner` is Everyone.
function graphHash(days: number = graphWindow(hashQuery.get("w")).days): string {
  const who = scopedOwner();
  return `#/?view=graph&w=${days}${who ? `&owner=${encodeURIComponent(who)}` : ""}`;
}

function ruleCard(r: FactRow): HTMLElement {
  return factCard({
    ref: r.ref, kind: "hidden_context", project: r.project ?? "", type: "rule",
    claim: r.claim, evidence: r.evidence, label: null, flaggable: false,
  });
}

let graphSeq = 0;

async function renderMemoryGraph(owner: string, w: string | null): Promise<void> {
  const win = graphWindow(w);
  const seq = ++graphSeq;
  let data: GraphResponse;
  try {
    [data] = await Promise.all([getGraph(owner, win.days, win.limit), ensureTeam()]);
  } catch (err) {
    return showError("load the memory graph", err);
  }
  // a window or a scope change keeps `view=graph`, so the route is not enough: only the newest fetch paints
  if (seq !== graphSeq || !onHome() || hashQuery.get("view") !== "graph") return;
  const main = el("main", {});
  main.append(pageHead("Memory", scope === "everyone"
    ? "What your team worked out, and where it was used again."
    : `What ${scopeName()} worked out, and where it was used again.`));
  const bar = el("div", { class: "toolbar toolbar-mem" });
  const controls = el("div", { class: "scope-wrap" }, memoryViewSwitch("graph"));
  const sw = scopeSwitch(() => { memOffset = 0; location.hash = graphHash(win.days); }); // the hash owns the scope here
  if (sw) controls.append(sw);
  bar.append(controls, graphWindowPick(win.days, data));
  main.append(bar);
  if (!data.sessions.length) {
    const empty = el("p", { class: "hint" }, win.days >= 3650
      ? (scope === "everyone" ? "No sessions have been read yet." : `Nothing from ${scopeName()} has been read yet. Switch to Everyone to see the team's.`)
      : `No sessions in the last ${win.days} days${scope === "everyone" ? "" : ` from ${scopeName()}`}. `);
    if (win.days < 3650) empty.append(el("a", { href: graphHash(3650) }, "Show all time"));
    main.append(empty);
    view(main);
    return;
  }
  const host = el("div", { class: "mem-graph" });
  const side = el("aside", { class: "graph-side", "aria-live": "polite" }, graphSideHint());
  main.append(el("div", { class: "mem-graph-wrap" }, host, side), graphLegend(), graphList(data));
  view(main); // the canvas takes its size from the container, so it must be in the document first
  activeGraph = memoryGraph(host, side, data);
}

function graphSideHint(): HTMLElement {
  return el("p", { class: "hint" }, "Hover a node for its card. Click a fact to read it here, or a session to see what it reused. Drag the graph to move it, scroll or pinch to zoom.");
}

// The picker widens the window through the hash, so the choice survives a reload.
function graphWindowPick(days: number, data: GraphResponse): HTMLElement {
  const sel = el("select", { class: "scope-pick", name: "w", "aria-label": "how far back the graph reaches" }) as HTMLSelectElement;
  for (const o of GRAPH_WINDOWS) {
    const opt = el("option", { value: String(o.days) }, o.label);
    if (o.days === days) opt.setAttribute("selected", "selected");
    sel.append(opt);
  }
  sel.addEventListener("change", () => { location.hash = graphHash(Number(sel.value)); });
  return el("div", { class: "graph-window" }, el("span", { class: "hint", role: "status" }, windowLabel(data.window, data.sessions.length)), sel);
}

function graphLegend(): HTMLElement {
  const legend = el("div", { class: "lp-legend mem-legend", "aria-hidden": "true" });
  for (const [t, label] of [["session", "Session"], ["decision", "Decision"], ["gotcha", "Gotcha"], ["constraint", "Constraint"],
    ["todo", "Todo"], ["preference", "Preference"], ["rule", "Rule"]])
    legend.append(el("span", { class: `t-${t}` }, el("i", { class: "dot" }), label));
  legend.append(el("span", {}, el("i", { class: "edge-glyph" }), "Recalls"));
  return legend;
}

// The canvas content as text, for a screen reader or a keyboard.
function graphList(data: GraphResponse): HTMLElement {
  const ol = el("ol", { class: "sr-only" });
  for (const s of data.sessions) {
    const ul = el("ul", {});
    for (const f of data.facts) if (f.session_id === s.session_id) ul.append(el("li", {}, `${f.type}: ${f.claim}`));
    ol.append(el("li", {}, el("a", { href: sessionHref(s.session_id) }, s.title), ` ${s.project}, ${ownerName(s.user)}, ${fmtDate(s.started_at)}, ${s.n_facts} ${s.n_facts === 1 ? "fact" : "facts"}. `, ul));
  }
  return ol;
}

function memoryGraph(host: HTMLElement, side: HTMLElement, data: GraphResponse): GraphView {
  // the canvas draws a short title and date under each session; the card and the panel keep the full ones
  const drawn: GraphData = { ...data, sessions: data.sessions.map((s) => ({ ...s, title: truncate(s.title, 26), started_at: shortDate(s.started_at) })) };
  const canvas = el("canvas", { class: "lp-canvas", role: "img",
    "aria-label": "Sessions and the facts they produced, with arrows where a fact was recalled into a later session. The list after the graph carries the same content." }) as HTMLCanvasElement;
  const card = el("div", { class: "graph-card" });
  card.hidden = true;
  host.append(canvas, card);
  const place = (x: number, y: number) => {
    const b = host.getBoundingClientRect();
    card.style.left = `${Math.min(Math.max(8, x - b.left + 16), Math.max(8, b.width - card.offsetWidth - 8))}px`;
    card.style.top = `${Math.min(Math.max(8, y - b.top - 24), Math.max(8, b.height - card.offsetHeight - 8))}px`;
  };
  const hide = () => { card.hidden = true; };
  const zoomOut = el("button", { type: "button", class: "gz", "aria-label": "zoom out" }, "\u2212") as HTMLButtonElement;
  const zoomIn = el("button", { type: "button", class: "gz", "aria-label": "zoom in" }, "+") as HTMLButtonElement;
  const reset = el("button", { type: "button", class: "gz gz-reset" }, "Fit") as HTMLButtonElement;
  host.append(el("div", { class: "graph-zoom" }, zoomOut, reset, zoomIn));
  const view = new GraphView(canvas, {
    data: drawn,
    colors: () => cssColors(canvas),
    labels: true, // a crowded window drops the labels that do not fit rather than stacking them
    stackLabels: true, // a real window puts several hubs on a row; only the demo story fits side labels
    pannable: true, // drag to move, wheel or pinch to zoom: a 600-node window does not fit at one scale
    onView: (v, min, max) => {
      zoomOut.disabled = v.scale <= min * 1.001;
      zoomIn.disabled = v.scale >= max * 0.999;
      reset.disabled = zoomOut.disabled;
    },
    onHover: (n, x, y) => {
      if (!n) return hide();
      card.replaceChildren(...memHoverCard(n, view, data));
      card.hidden = false;
      place(x, y);
    },
    onClick: (n) => {
      if (!n) return hide();
      const close = () => side.replaceChildren(graphSideHint());
      side.replaceChildren(...(n.kind === "session" ? memSessionPanel(n.id, view, data, close) : memFactPanel(n.id, view, data, close)));
    },
  });
  zoomOut.addEventListener("click", () => view.zoomBy(1 / 1.4));
  zoomIn.addEventListener("click", () => view.zoomBy(1.4));
  reset.addEventListener("click", () => view.resetView());
  return view;
}

function graphFact(ref: string, data: GraphResponse) {
  const f = data.facts.find((x) => x.ref === ref)!;
  return { f, home: data.sessions.find((s) => s.session_id === f.session_id)! };
}

function memHoverCard(n: GNode, view: GraphView, data: GraphResponse): Node[] {
  if (n.kind === "fact") {
    const { f, home } = graphFact(n.id, data);
    return f.type === "rule"
      ? [el("div", { class: "k" }, `rule · ${home.project}`), el("div", { class: "t" }, f.claim), el("div", { class: "k" }, f.evidence ?? "")]
      : [el("div", { class: "k" }, f.type), el("div", { class: "t" }, f.claim),
        el("div", { class: "k" }, `${home.title} · ${shortDate(home.started_at)}${f.recalled ? ` · reused in ${f.recalled} ${f.recalled === 1 ? "session" : "sessions"}` : ""}`)];
  }
  const s = data.sessions.find((x) => x.session_id === n.id)!;
  const out: Node[] = [
    el("div", { class: "k" }, "Session"),
    el("div", { class: "t" }, s.title),
    el("div", { class: "k" }, `${shortDate(s.started_at)} · ${s.n_facts} ${s.n_facts === 1 ? "fact" : "facts"} · ${ownerName(s.user)}`),
  ];
  const rules = data.facts.filter((f) => f.type === "rule" && f.session_id === s.session_id);
  if (rules.length)
    out.push(el("div", { class: "k" }, `plus ${rules.length} project ${rules.length === 1 ? "rule" : "rules"} for ${s.project}`));
  const from = view.recalledFacts(s.session_id);
  if (from.length) {
    const ul = el("ul", {});
    for (const f of from) ul.append(el("li", { class: `t-${f.type}` }, el("i", { class: "dot" }), f.claim));
    out.push(el("div", { class: "k", style: "margin-top:8px" }, "Recalled from"), ul);
  }
  return out;
}

function panelClose(close: () => void): HTMLElement {
  const x = el("button", { class: "close", type: "button" }, "Close");
  x.addEventListener("click", close);
  return x;
}

// A rule has no quote and no shipper: its evidence is the count, so those rows are skipped below.
function memFactPanel(ref: string, view: GraphView, data: GraphResponse, close: () => void): Node[] {
  const { f, home } = graphFact(ref, data);
  const out: Node[] = [
    el("div", { class: `graph-panel-head t-${f.type}` }, el("i", { class: "dot", "aria-hidden": "true" }), el("span", { class: "tchip" }, f.type), panelClose(close)),
    el("h3", {}, f.claim),
  ];
  if (f.evidence) out.push(el("blockquote", { class: f.type === "rule" ? "count" : "" }, f.evidence));
  const meta = el("dl", { class: "pane-meta" });
  const kv = (k: string, v: string) => meta.append(el("div", {}, el("dt", {}, k), el("dd", { title: v }, v)));
  if (f.type !== "rule") kv("Shipped by", ownerName(home.user));
  kv("Project", home.project || "(no project)");
  if (f.type !== "rule") kv("Date", fmtDate(home.started_at));
  out.push(meta);
  // `recalled` counts sessions outside this window and scope, so say how many are not listed rather
  // than letting the short list read as "never reused".
  const into = view.recalledInto(ref).map((id) => data.sessions.find((s) => s.session_id === id)!)
    .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""));
  const total = Math.max(f.recalled ?? 0, into.length);
  if (total) {
    out.push(el("div", { class: "k" }, `Recalled in ${total} later ${total === 1 ? "session" : "sessions"}`));
    if (into.length) {
      const ul = el("ul", {});
      for (const t of into) ul.append(el("li", {}, el("span", { class: "when" }, fmtDate(t.started_at)), el("a", { href: sessionHref(t.session_id, ref) }, t.title), el("span", { class: "hint" }, t.project)));
      out.push(ul);
    }
    if (total > into.length)
      out.push(el("p", { class: "hint" }, `${total - into.length} of them ${total - into.length === 1 ? "is" : "are"} not shown here, filtered out by the scope or the window.`));
  } else if (f.type !== "rule") {
    out.push(el("p", { class: "hint" }, "Not recalled into a later session yet."));
  }
  if (f.type !== "rule") out.push(el("p", { class: "pane-foot" }, el("a", { href: sessionHref(home.session_id, ref) }, "Open the session")));
  return out;
}

function memSessionPanel(id: string, view: GraphView, data: GraphResponse, close: () => void): Node[] {
  const s = data.sessions.find((x) => x.session_id === id)!;
  const out: Node[] = [
    el("div", { class: "graph-panel-head t-session" }, el("i", { class: "dot", "aria-hidden": "true" }), el("span", { class: "tchip" }, "session"), panelClose(close)),
    el("h3", {}, el("a", { href: sessionHref(s.session_id) }, s.title)),
  ];
  const meta = el("dl", { class: "pane-meta" });
  const kv = (k: string, v: string) => meta.append(el("div", {}, el("dt", {}, k), el("dd", { title: v }, v)));
  kv("Owner", ownerName(s.user));
  kv("Date", fmtDate(s.started_at));
  kv("Project", s.project || "(no project)");
  out.push(meta);
  const from = view.recalledFacts(s.session_id);
  if (from.length) {
    const ul = el("ul", {});
    for (const f of from) ul.append(el("li", { class: `t-${f.type}` }, el("i", { class: "dot" }), el("span", {}, f.claim)));
    out.push(el("div", { class: "k" }, `Recalled ${from.length} ${from.length === 1 ? "fact" : "facts"} from earlier sessions`), ul);
  }
  const own = data.facts.filter((f) => f.session_id === s.session_id && f.type !== "rule");
  if (own.length) {
    const ul = el("ul", {});
    for (const f of own) ul.append(el("li", { class: `t-${f.type}` }, el("i", { class: "dot" }), el("span", {}, f.claim)));
    out.push(el("div", { class: "k" }, `Produced ${own.length} ${own.length === 1 ? "fact" : "facts"}`), ul);
  } else {
    out.push(el("p", { class: "hint" }, "No facts were extracted from this session."));
  }
  out.push(el("p", { class: "pane-foot" }, el("a", { href: sessionHref(s.session_id) }, "Open the session")));
  return out;
}

// --- the Sessions pane: one session, read in place ----------------------------------------------------
let paneSeq = 0;

async function fillPane(pane: HTMLElement, id: string): Promise<void> {
  const seq = ++paneSeq;
  if (!id) {
    pane.replaceChildren(el("p", { class: "hint pane-empty" }, "Pick a session to read its facts here."));
    return;
  }
  pane.replaceChildren(el("p", { class: "hint loading" }, "Loading…"));
  let d: CorpusSessionDetail;
  try {
    d = await getCorpusSession(id);
  } catch (err) {
    if (seq !== paneSeq) return;
    if (err instanceof AuthError) return renderLogin(err.message);
    pane.replaceChildren(el("p", { class: "hint" }, /\b404\b/.test(String(err))
      ? "Session not found. It is not in your workspace, or it was deleted."
      : `Could not load that session: ${err instanceof Error ? err.message : String(err)}`));
    return;
  }
  if (seq !== paneSeq) return; // a later click won
  pane.replaceChildren(...sessionPane(d));
  pane.scrollTop = 0;
}

function sessionPane(d: CorpusSessionDetail): Node[] {
  const s = d.session;
  const full = sessionHref(s.session_id);
  const meta = el("dl", { class: "pane-meta" });
  const kv = (k: string, v: string, tip = v) => meta.append(el("div", {}, el("dt", {}, k), el("dd", { title: tip }, v)));
  kv("Owner", ownerName(s.user || "") || "?", s.user || "sessions ingested on the server machine itself");
  kv("Date", s.started_at ? fmtDate(s.started_at) : "·");
  kv("Project", s.project || "(no project)");
  const out: Node[] = [
    el("p", { class: "pane-project" }, s.project || "(no project)"),
    el("h2", { class: "pane-title" }, el("a", { href: full, title: "the full session: transcript, entities, relations, links" }, s.summary || shortId(s.session_id))),
    meta,
  ];
  const m = d.metrics;
  if (m) {
    // a session shipped before cost tracking has nothing to show: four "·" tiles say less than nothing
    if ([m.cost_usd, m.tool_calls, m.wasted, m.wall_s].some((v) => v !== null)) out.push(metricsStrip(m));
  } else out.push(el("p", { class: "hint pane-private" }, `${SESSION_COST} and tool calls are shown to the session's owner and to a workspace admin.`));
  const types = Object.entries(d.hidden_context).filter(([, facts]) => facts.length).sort(([a], [b]) => hcRank(a) - hcRank(b));
  const n = types.reduce((k, [, facts]) => k + facts.length, 0);
  out.push(el("div", { class: "pane-sub" }, el("h3", {}, "Facts from this session"), el("span", { class: "hint" }, `${n} ${n === 1 ? "fact" : "facts"}`)));
  if (!n) out.push(el("p", { class: "hint" }, "No facts were extracted from this session."));
  for (const [type, facts] of types) {
    out.push(el("h4", { class: "pane-type", title: TYPE_TIP[type] ?? "" }, headName(type)));
    const list = el("div", { class: "facts" });
    // no project on the card: the pane's header already names it
    for (const hf of facts) list.append(factCard(toFact(hf, type, ""), { used: hf.used, chips: coreChip(hf.dominant) }));
    out.push(list);
  }
  const { served } = splitServed(d.entities);
  if (served.length) out.push(paneChips("Entities", served.map((e) => e.name)));
  if (d.worked_on.length) out.push(paneChips("Worked on", d.worked_on));
  out.push(el("p", { class: "pane-foot" }, el("a", { href: full }, "Open the full session"), el("span", { class: "hint" }, " · transcript, relations, links")));
  return out;
}

function paneChips(title: string, items: string[]): HTMLElement {
  const row = el("div", { class: "chips" });
  for (const it of items) row.append(el("span", { class: "chip" }, it));
  return el("section", { class: "pane-chips" }, el("h4", { class: "sec-head" }, title), row);
}

// Absent altogether when the numbers are not the reader's to see: that is the server's call, and the
// caller checks `d.metrics` for it.
function metricsStrip(m: SessionMetrics): HTMLElement {
  const strip = el("div", { class: "metrics" });
  const unmeasured = "Not measured: this session shipped before cost tracking.";
  const cell = (k: string, v: string, tip: string) => strip.append(el("div", { title: tip }, el("span", {}, k), el("b", {}, v)));
  cell(SESSION_COST, fmtCost(m.cost_usd), m.cost_usd === null ? NOT_PRICED : SESSION_COST_DEF);
  cell("Tool calls", fmtCount(m.tool_calls), m.tool_calls === null ? unmeasured : "Tool calls the assistant made.");
  cell("Wasted", fmtCount(m.wasted), m.wasted === null ? unmeasured : "Repeated, re-read and failed calls.");
  cell("Duration", fmtDur(m.wall_s), m.wall_s === null ? unmeasured
    : m.idle_s ? `${fmtDur(Math.max(0, m.wall_s - m.idle_s))} active, ${fmtDur(m.idle_s)} idle.` : "First to last message.");
  return strip;
}

// =============================================================================================
// PR 3 (redesign): the Team page's admin controls and the Entities page. Kept together at the end
// of the file because two sibling PRs edit main.ts at the same time.
// =============================================================================================

// --- Team: admin controls --------------------------------------------------------------------

// The invite form records `web` and mints a member (a typed email is a self-assertion); the
// identity provider records `web-sso`. Only the person this explains sees it.
function invitePathNote(): HTMLElement | null {
  if (!me || me.role === "admin" || me.hostname !== "web") return null;
  return el("p", { class: "hint team-note" },
    "You signed in through an invite, so you are a member. To become an admin, connect a machine with csa-client setup, then ask an admin to promote you.");
}

function inviteButton(): HTMLElement {
  const wrap = el("div", { class: "invite-wrap" });
  const btn = el("button", { class: "btn", type: "button", "aria-expanded": "false" }, INVITE.open);
  const panel = el("div", { class: "invite-panel" });
  panel.hidden = true;
  btn.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    btn.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) void paintInvite(panel);
  });
  wrap.append(btn, panel);
  return wrap;
}

function serviceTokenForm(done: () => void, created: () => void): HTMLElement {
  const name = el("input", { id: "svc-name", type: "text", placeholder: "atlas", autocomplete: "off" }) as HTMLInputElement;
  const email = el("input", { id: "svc-email", type: "email", placeholder: "atlas@company.com", autocomplete: "off" }) as HTMLInputElement;
  const err = el("p", { class: "set-err", role: "alert" });
  err.hidden = true;
  const go = el("button", { class: "btn-primary", type: "submit" }, SERVICE.submit) as HTMLButtonElement;
  const form = el("form", { class: "set-rows" },
    el("div", { class: "set-row" }, el("label", { class: "set-label", for: "svc-name" }, SERVICE.nameLabel), name),
    el("div", { class: "set-row" }, el("label", { class: "set-label", for: "svc-email" }, SERVICE.emailLabel), email),
    el("div", { class: "set-actions" }, err, go)) as HTMLFormElement;
  form.noValidate = true;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const bad = serviceTokenError(name.value, email.value);
    if (bad) {
      err.textContent = bad;
      err.hidden = false;
      return;
    }
    go.disabled = true;
    err.hidden = true;
    const r = await createServiceToken(name.value.trim(), email.value.trim()).catch((x) => {
      if (x instanceof AuthError) throw x;
      return { ok: false as const, error: "Could not reach the server." };
    });
    go.disabled = false;
    if (!r.ok) {
      err.textContent = r.error;
      err.hidden = false;
      return;
    }
    created();
    const paste = servicePaste(r.token, (await authConfig()).base_url ?? location.origin);
    let copied = false;
    const copy = el("button", { class: "btn btn-small", type: "button" }, CONNECT.copy);
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(paste);
        copied = true;
        copy.textContent = CONNECT.copied;
      } catch {
        copy.textContent = "Select and copy";
      }
    });
    const close = el("button", { class: "btn btn-small", type: "button" }, "Done");
    const warn = el("p", { class: "set-err", role: "alert" });
    warn.hidden = true;
    close.addEventListener("click", () => {
      if (!copied && warn.hidden) {
        warn.textContent = SERVICE.discardWarn;
        warn.hidden = false;
        close.textContent = "Leave without copying";
        return;
      }
      done();
    });
    form.replaceWith(el("div", { class: "invite-made" },
      el("pre", { class: "invite-paste" }, paste),
      el("div", { class: "invite-actions" }, copy, close),
      warn,
      el("p", { class: "hint" }, SERVICE.once),
      el("p", { class: "hint" }, SERVICE.works.replaceAll("{name}", r.name).replaceAll("{email}", r.email))));
  });
  return form;
}

/** The link, shown once. Copy takes the whole blob (link + install line) because an admin who pastes
 *  only the link sends a teammate to a dashboard that ships nothing. */
function inviteResult(link: string, base: string, inv: InviteLink, verified: boolean, org: string, done: () => void): HTMLElement {
  const paste = invitePaste(link, base, org, inv.expires_at ? fmtDate(inv.expires_at) : "");
  let copied = false;
  const copy = el("button", { class: "btn btn-small", type: "button" }, CONNECT.copy);
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(paste);
      copied = true;
      copy.textContent = CONNECT.copied;
    } catch {
      copy.textContent = "Select and copy";
    }
  });
  const close = el("button", { class: "btn btn-small", type: "button" }, "Done");
  const warn = el("p", { class: "set-err", role: "alert" });
  warn.hidden = true;
  close.addEventListener("click", () => {
    if (!copied && warn.hidden) {
      warn.textContent = INVITE.discardWarn;
      warn.hidden = false;
      close.textContent = "Leave without copying";
      return;
    }
    done();
  });
  return el("div", { class: "invite-made" },
    el("p", {}, el("b", {}, INVITE.sendThis)),
    el("pre", { class: "invite-paste" }, paste),
    el("div", { class: "invite-actions" }, copy, close),
    warn,
    el("p", { class: "hint" }, INVITE.once),
    el("p", { class: "hint" }, (verified ? INVITE.worksVerified : INVITE.worksBearer)
      .replace("{email}", inv.email)
      .replace("{date}", inv.expires_at ? fmtDate(inv.expires_at) : "it expires")),
  );
}

function inviteForm(base: string, verified: boolean, org: string, repaint: () => void): HTMLElement {
  const email = el("input", { id: "inv-email", type: "email", placeholder: "you@company.com",
                              autocomplete: "off" }) as HTMLInputElement;
  const days = el("select", { id: "inv-days", name: "days" }) as HTMLSelectElement;
  for (const d of ["1", "7", "30"]) days.append(el("option", { value: d }, d === "1" ? "1 day" : `${d} days`));
  days.value = "7";
  const err = el("p", { class: "error", role: "alert" });
  err.hidden = true;
  const go = el("button", { class: "btn", type: "submit" }, INVITE.submit) as HTMLButtonElement;
  const form = el("form", { class: "invite-form" },
    el("label", { for: "inv-email" }, INVITE.emailLabel), email,
    el("label", { for: "inv-days" }, INVITE.expiryLabel), days,
    go, err) as HTMLFormElement;
  form.noValidate = true;      // inviteError() is the contract; the browser bubble hides it
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const bad = inviteError(email.value);
    if (bad) {
      err.textContent = bad;
      err.hidden = false;
      return;
    }
    go.disabled = true;
    err.hidden = true;
    const r = await createInviteLink(email.value.trim(), Number(days.value)).catch((x) => {
      if (x instanceof AuthError) throw x;
      return { ok: false as const, error: "Could not reach the server." };
    });
    go.disabled = false;
    if (!r.ok) {
      err.textContent = r.error;
      err.hidden = false;
      return;
    }
    form.replaceWith(inviteResult(r.link, base, r.invite, verified, org, repaint));
    // The create response carries the list the server sees, so the person you just invited is in
    // the list rendered below rather than missing from it until Done.
    pendingInto(r.invites, repaint);
  });
  return form;
}

/** Re-render the waiting list in place from a list the server just gave us. */
function pendingInto(list: InviteLink[], repaint: (email?: string) => void): void {
  document.querySelector(".invite-pending")?.replaceWith(invitePending(list, repaint));
}

function invitePending(list: InviteLink[], repaint: (email?: string, made?: NewInvite) => void): HTMLElement {
  const sec = el("section", { class: "invite-pending" }, el("h4", {}, INVITE.pendingHead));
  if (!list.length) {
    sec.append(el("p", { class: "hint" }, INVITE.pendingNone));
    return sec;
  }
  const ul = el("ul", { class: "machines" });
  for (const inv of list) {
    const status = el("span", { class: "flag-status", role: "status" });
    const again = el("button", { class: "btn btn-small", type: "button",
                                 "aria-label": `${INVITE.newLink} for ${inv.email}` }, INVITE.newLink);
    const kill = el("button", { class: "btn btn-small", type: "button",
                                "aria-label": `Revoke the invite for ${inv.email}` }, INVITE.revoke);
    // The way back from a dialog closed before Copy. It CREATES, and orgs.create_invite revokes the
    // address's previous link as part of that, so a failure here leaves the old link alive rather
    // than destroying it and handing back nothing.
    again.addEventListener("click", async () => {
      again.disabled = true;
      const r = await createInviteLink(inv.email, 7).catch((x) => {
        if (x instanceof AuthError) throw x;
        return { ok: false as const, error: "Could not reach the server." };
      });
      again.disabled = false;
      if (!r.ok) {
        status.textContent = r.error;
        status.className = "flag-status err";
        return;
      }
      repaint("", r);
    });
    kill.addEventListener("click", async () => {
      if (!confirm(`Revoke the invite for ${inv.email}? The link stops working immediately.`)) return;
      kill.disabled = true;
      const r = await revokeInviteLink(inv.id);
      if (r.ok) return repaint();
      kill.disabled = false;
      status.textContent = r.error;
      status.className = "flag-status err";
    });
    ul.append(el("li", {},
      el("span", { class: "machine-host" }, inv.email),
      el("span", { class: "when" }, inviteLine(inv, fmtDate)),
      el("span", { class: "invite-row-actions" }, again, kill, status)));
  }
  sec.append(ul);
  return sec;
}

async function paintInvite(panel: HTMLElement, prefill = "", made?: NewInvite): Promise<void> {
  panel.replaceChildren(el("p", { class: "hint" }, "Loading…"));
  let links: InviteLink[];
  let cfg;
  try {
    [links, cfg] = await Promise.all([getInviteLinks().then((r) => r.invites), authConfig()]);
  } catch (err) {
    if (err instanceof AuthError) return renderLogin(err.message);
    panel.replaceChildren(el("p", { class: "hint" }, "Could not load the invites."));
    return;
  }
  const again = (email = "", next?: NewInvite) => void paintInvite(panel, email, next);
  panel.replaceChildren(el("h4", {}, INVITE.head));
  if (!cfg.base_url) {
    panel.append(el("p", { class: "hint" }, INVITE.noBase));
    return;
  }
  // Only an identity provider verifies who redeems a link; without one the link IS the credential,
  // and the copy beside it has to say so.
  const org = teamCard(me?.org_id ?? "", me?.org_name).name;
  const form = inviteForm(cfg.base_url, cfg.sso_required, org, again);
  panel.append(made && made.ok
    ? inviteResult(made.link, cfg.base_url, made.invite, cfg.sso_required, org, () => again())
    : form,
    invitePending(waiting(made && made.ok ? made.invites : links), again));
  if (prefill) (form.querySelector("#inv-email") as HTMLInputElement | null)?.setAttribute("value", prefill);
}

// The one role a person holds, as text, and a button to the other: a select listed both.
function roleControl(p: TeamPerson, status: HTMLElement, otherAdmins: number): HTMLElement {
  const current = p.role === "admin" ? "admin" : "member";
  const role = current === "admin" ? "member" : "admin";
  const btn = el("button", { class: "btn btn-small", type: "button", "aria-label": `Make ${p.user} ${role === "admin" ? "an admin" : "a member"}` },
    role === "admin" ? "Make admin" : "Make member") as HTMLButtonElement;
  btn.addEventListener("click", () => {
    // The server refuses to demote the LAST admin; the demotion before that one is where the
    // workspace quietly ends up with a single admin, so that is where the warning belongs.
    if (role === "member" && p.user !== myEmail() && otherAdmins <= 1
      && !confirm(`Demoting ${ownerName(p.user)} leaves you the only admin of this workspace. Continue?`)) return;
    btn.disabled = true;
    void setMemberRole(p.user, role).then(async (r) => {
      if (!r.ok) {
        btn.disabled = false;
        status.textContent = r.error;
        status.className = "flag-status err";
        return;
      }
      if (p.user === myEmail()) me = await getMe().catch(() => me);   // demoted yourself: the controls go with the role
      void renderTeam();                             // repaint from the server; stale refusals go with it
    });
  });
  return el("span", { class: "role-cell" }, el("span", { class: "role-name" }, current), btn);
}

function machinesCell(p: TeamPerson, own: boolean): HTMLElement {
  const list = el("ul", { class: "machines" });
  if (!p.machines.length) list.append(el("li", { class: "hint" }, "none connected"));
  for (const m of p.machines) {
    const li = el("li", {},
      el("span", { class: "machine-host" }, machineLabel(m.hostname)),
      el("span", { class: "when", title: m.lastUsed ?? "" }, m.lastUsed ? fmtWhen(m.lastUsed) : "never used"));
    const status = el("span", { class: "flag-status", role: "status" });
    const btn = el("button", { class: "btn btn-small", type: "button", "aria-label": `Revoke ${machineLabel(m.hostname)} of ${p.user}` }, "Revoke") as HTMLButtonElement;
    btn.addEventListener("click", () => {
      // Your own machines included: a lost laptop is exactly when you need this, and the server
      // refuses only the ONE credential behind this request (409 self_revoke), which it names.
      if (!confirm(`Revoke ${own ? "your" : `${ownerName(p.user)}'s`} ${machineLabel(m.hostname)}? It stops shipping sessions on its next send. `
        + "Facts already mined stay. It can reconnect with csa-client setup.")) return;
      btn.disabled = true;
      void revokeMember(m.hash).then((r) => {
        if (r.ok) return renderTeam();
        btn.disabled = false;
        status.textContent = r.error;
        status.className = "flag-status err";
      });
    });
    li.append(btn, status);
    list.append(li);
  }
  return list;
}

// --- Entities: the taxonomy editor and the browser -------------------------------------------
const ENT_PAGE = 25;
let entQ = "";
let entType = "";
let entSort = "sessions";
let entDir: "asc" | "desc" = "desc";
let entOffset = 0;
let entHideOneOffs = false;
let entRefocus = "";                       // the control that asked for the repaint, refocused after it
let drawerEl: HTMLElement | null = null;   // the one open entity drawer; a view swap removes it

function onEntities(): boolean {
  return routePath().startsWith("/entities");
}

function entitiesHref(id: string): string {
  return id ? `#/entities/${encodeURIComponent(id)}` : "#/entities";
}

async function renderEntities(openId: string): Promise<void> {
  let page: EntitiesPage;
  let types: EntityTypesData;
  try {
    [page, types] = await Promise.all([
      getEntities({ q: entQ, type: entType, owner: scopedOwner(), served: entHideOneOffs,
        sort: entSort, dir: entDir, offset: entOffset, limit: ENT_PAGE }),
      getEntityTypes(),
      ensureTeam(),
    ]);
  } catch (err) {
    return showError("load entities", err);
  }
  if (!onEntities()) return;
  drawerEl = null;
  view(el("main", {},
    pageHead("Entities", "The tickets, services, repos and people your sessions mention. Open one to read what the team learned about it."),
    browserPanel(page, types)));
  // A repaint replaces every node, so a keyboard user who just sorted or filtered would be thrown
  // back to the top of the page. Put them back on the control they used.
  if (entRefocus) {
    document.querySelector<HTMLElement>(entRefocus)?.focus();
    entRefocus = "";
  }
  if (openId) void openEntity(openId, null);
}

// --- Admin > Entity types: what extraction files entities under --------------------------------
async function renderEntityTypes(): Promise<void> {
  if (me && me.role !== "admin") return adminOnlyNotice();
  let types: EntityTypesData;
  try {
    types = await getEntityTypes();
  } catch (err) {
    return showError("load entity types", err);
  }
  if (routePath() !== "/admin/entity-types") return;
  view(el("main", { class: "admin" },
    pageHead("Entity types", "What extraction files entities under. A custom type's description is the instruction it follows for every session from now on."),
    typesPanel(types)));
}

function typesPanel(types: EntityTypesData): HTMLElement {
  const box = el("section", { class: "ent-card ent-types", "aria-label": "entity types" });
  const builtin = el("ul", { class: "etype-list" });
  for (const k of types.default)
    builtin.append(el("li", { class: "etype-row etype-builtin" },
      el("span", { class: "etype-term" }, k),
      el("span", { class: "etype-desc", title: types.default_info?.[k] ?? "" }, types.default_info?.[k] ?? ""),
      el("span", { class: "etype-tag" }, "Built-in")));

  const msg = el("p", { class: "etype-error", role: "alert" });
  msg.hidden = true;
  const compose = typeComposer(types, msg);
  const add = el("button", { class: "btn", type: "button" }, "+ Add type");
  add.addEventListener("click", () => compose.open("", ""));
  const custom = el("ul", { class: "etype-list" });
  if (!types.custom.length)
    custom.append(el("li", { class: "hint" }, "None yet. Anything your team tracks, clients, releases, features, can be filed automatically."));
  for (const t of types.custom) {
    const edit = el("button", { class: "btn btn-small", type: "button", "aria-label": `Edit ${t.key}` }, "Edit");
    edit.addEventListener("click", () => compose.open(t.key, t.description));
    const del = el("button", { class: "btn btn-small", type: "button", "aria-label": `Remove ${t.key}` }, "Remove");
    del.addEventListener("click", () => {
      if (!confirm(`Remove "${t.key}"? New sessions stop being filed under it. Entities already filed keep their type.`)) return;
      void removeEntityType(t.key).then((r) => {
        if (r.ok) return renderEntityTypes();
        msg.textContent = r.error ?? "Could not remove it.";
        msg.hidden = false;
      });
    });
    custom.append(el("li", { class: "etype-row etype-custom" },
      el("span", { class: "etype-term" }, t.key),
      el("span", { class: "etype-actions" }, edit, del),
      el("span", { class: "etype-desc" }, t.description)));
  }
  box.append(el("div", { class: "ent-sec-head" }, el("h3", { class: "ent-h3" }, "Custom types"), add), custom, compose.form, msg,
    el("div", { class: "ent-sec-head" }, el("h3", { class: "ent-h3" }, "Built-in types")), builtin);
  return box;
}

// One composer for both Add and Edit. The key is the primary key, so Edit locks it and only the
// description (the extraction instruction) changes; Add refuses a key that already exists.
function typeComposer(types: EntityTypesData, msg: HTMLElement): { form: HTMLElement; open: (key: string, desc: string) => void } {
  const key = el("input", { class: "etype-slot etype-slot-key", id: "etype-key", name: "key", placeholder: "client", "aria-label": "type name" }) as HTMLInputElement;
  const desc = el("input", {
    class: "etype-slot etype-slot-def", id: "etype-desc", name: "description",
    placeholder: "a paying customer account; id = the company name",
    "aria-label": "description, the extraction instruction",
  }) as HTMLInputElement;
  const save = el("button", { class: "btn", type: "button" }, "Add type") as HTMLButtonElement;
  const cancel = el("button", { class: "linklike", type: "button" }, "Cancel");
  const form = el("div", { class: "etype-compose" },
    el("span", { class: "etype-lead" }, "Also extract every"), key, el("span", { class: "etype-lead" }, "·"), desc, save, cancel);
  form.hidden = true;
  let editing = "";
  const submit = (): void => {
    const k = key.value.trim().toLowerCase();
    if (!editing && types.custom.some((t) => t.key === k)) {
      msg.textContent = `"${k}" exists. Edit it instead.`;
      msg.hidden = false;
      return;
    }
    save.disabled = true;
    void saveEntityType(k, desc.value).then((r) => {
      save.disabled = false;
      if (r.ok) return renderEntityTypes();
      msg.textContent = r.error ?? "That did not save.";
      msg.hidden = false;
    });
  };
  save.addEventListener("click", submit);
  for (const input of [key, desc])
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  cancel.addEventListener("click", () => { form.hidden = true; msg.hidden = true; });
  return {
    form,
    open: (k, d) => {
      editing = k;
      key.value = k;
      key.readOnly = !!k;
      desc.value = d;
      save.textContent = k ? "Save description" : "Add type";
      form.hidden = false;
      msg.hidden = true;
      (k ? desc : key).focus();
    },
  };
}

function browserPanel(page: EntitiesPage, types: EntityTypesData): HTMLElement {
  const box = el("section", { class: "ent-card ent-browser", "aria-label": "entities found" });
  const search = el("input", {
    class: "search", type: "search", id: "ent-q", name: "q", placeholder: "Search entities",
    "aria-label": "search entities by name or alias",
  }) as HTMLInputElement;
  search.value = entQ;
  let debounce: number | undefined;
  search.addEventListener("input", () => {
    window.clearTimeout(debounce);
    debounce = window.setTimeout(() => {
      entQ = search.value.trim();
      entOffset = 0;
      void renderEntities("").then(() => {
        const s = document.querySelector<HTMLInputElement>("#ent-q");
        s?.focus();
        s?.setSelectionRange(s.value.length, s.value.length);
      });
    }, 300);
  });
  const whose = scope === "everyone" ? "all" : scope === "mine" ? "your" : `${scopeName()}'s`;
  const count = `${page.total.toLocaleString("en-US")} ${page.total === 1 ? "entity" : "entities"}${entQ ? " matching" : ""} across ${whose} sessions.`;
  box.append(el("div", { class: "ent-card-head" },
    el("div", {}, el("h2", { class: "ent-h2" }, "Entities found"), el("p", { class: "ent-sub" }, count)), search));

  const tools = el("div", { class: "toolbar toolbar-mem ent-tools" });
  const sw = scopeSwitch(() => { entOffset = 0; void renderEntities(""); });
  if (sw) tools.append(sw);
  const typeSel = el("select", { class: "scope-pick", id: "ent-type", name: "type", "aria-label": "filter by type" }) as HTMLSelectElement;
  typeSel.append(el("option", { value: "" }, "All types"));
  for (const k of [...types.default, ...types.custom.map((t) => t.key)]) typeSel.append(el("option", { value: k }, k));
  typeSel.value = entType;
  if (typeSel.value) typeSel.classList.add("active");
  typeSel.addEventListener("change", () => { entType = typeSel.value; entOffset = 0; entRefocus = "#ent-type"; void renderEntities(""); });
  const hide = el("input", { type: "checkbox", id: "ent-hide" }) as HTMLInputElement;
  hide.checked = entHideOneOffs;
  hide.addEventListener("change", () => { entHideOneOffs = hide.checked; entOffset = 0; entRefocus = "#ent-hide"; void renderEntities(""); });
  tools.append(typeSel, el("label", { class: "ent-hide", for: "ent-hide", title: "a one-off is mentioned by a single session and is not served to recall" }, hide, "Hide one-offs"));
  box.append(tools);

  if (page.total === 0) {
    const owner = scopedOwner();
    const sessions = (teamCache ?? []).filter((r) => !owner || r.user === owner).reduce((a, r) => a + r.sessions, 0);
    box.append(el("p", { class: "hint" }, entQ || entType || entHideOneOffs
      ? `No entity matches${entQ ? ` \u201c${entQ}\u201d` : ""}. Aliases are searched too, so a nickname finds its canonical name. Clear the search or the type filter.`
      : sessions
        ? `No entities were found in ${whose === "all" ? "these" : whose} ${sessions.toLocaleString("en-US")} ${sessions === 1 ? "session" : "sessions"} yet. They are picked up as new sessions are read.`
        : "No entities yet. They appear once the first sessions are read."));
    return box;
  }
  box.append(
    el("div", { class: "table-scroll" }, entitiesTable(page.rows)),
    pager(page.offset, page.limit, page.total, (o) => { entOffset = o; void renderEntities(""); window.scrollTo(0, 0); }),
  );
  return box;
}

function entHeader(id: string, label: string, num: boolean): HTMLElement {
  const active = entSort === id;
  const th = el("th", {
    class: `sortable${num ? " num" : ""}${active ? " sorted" : ""}`, role: "button", tabindex: "0",
    "data-col": id, title: `Sort by ${label}`,
    "aria-sort": active ? (entDir === "asc" ? "ascending" : "descending") : "none",
  }, label + (active ? (entDir === "asc" ? " ▲" : " ▼") : ""));
  const toggle = (): void => {
    if (entSort === id) entDir = entDir === "asc" ? "desc" : "asc";
    else { entSort = id; entDir = id === "name" ? "asc" : "desc"; }
    entOffset = 0;
    entRefocus = `.ent-table th[data-col="${id}"]`;
    void renderEntities("");
  };
  th.addEventListener("click", toggle);
  th.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return th;
}

function entitiesTable(rows: EntityRow[]): HTMLElement {
  const table = el("table", { class: "sessions ent-table" });
  table.append(el("thead", {}, el("tr", {},
    entHeader("name", "Entity", false), el("th", {}, "Type"), entHeader("sessions", "Sessions", true),
    entHeader("facts", "Facts in those sessions", true), entHeader("last_seen", "Last seen", false))));
  const body = el("tbody", {});
  for (const r of rows) {
    const tr = el("tr", {
      class: `ent-row${r.served ? "" : " ent-oneoff"}`, tabindex: "0", role: "button",
      title: r.served ? `open ${r.name}` : `${r.name}: a one-off, mentioned by a single session and not served to recall`,
    });
    tr.append(
      el("td", {}, el("span", { class: "ent-name" }, r.name)),
      el("td", {}, r.type ? el("span", { class: "echip" }, r.type) : ""),
      el("td", { class: "num" }, String(r.sessions)),
      el("td", { class: "num" }, String(r.facts)),
      el("td", { class: "ent-when" }, r.last_seen ? fmtDate(r.last_seen) : ""),
    );
    const open = (): void => void openEntity(r.id, tr);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    body.append(tr);
  }
  table.append(body);
  return table;
}

// The drawer for one entity. Opened in-page (the list keeps its page and scroll) and mirrored into the
// URL with replaceState so the link is shareable; a deep link renders the page and opens it.
async function openEntity(id: string, row: HTMLElement | null): Promise<void> {
  history.replaceState(null, "", entitiesHref(id));
  const drawer = el("aside", { class: "drawer", role: "dialog", "aria-label": "entity" });
  const onKey = (e: KeyboardEvent): void => {
    if (!document.contains(drawer)) return document.removeEventListener("keydown", onKey);
    if (e.key === "Escape") close();
  };
  const close = (): void => {
    drawer.remove();
    document.removeEventListener("keydown", onKey);
    if (drawerEl === drawer) drawerEl = null;
    if (onEntities()) history.replaceState(null, "", entitiesHref(""));
    row?.focus();
  };
  document.addEventListener("keydown", onKey);
  const closeBtn = el("button", { class: "drawer-close", type: "button", "aria-label": "close" }, "×");
  closeBtn.addEventListener("click", close);
  drawerEl?.remove();
  drawerEl = drawer;
  drawer.append(closeBtn, el("p", { class: "hint loading" }, "Loading…"));
  app.append(drawer);
  let d: EntityDetail | null;
  try {
    d = await getEntity(id);
  } catch (err) {
    if (err instanceof AuthError) return renderLogin(err.message);
    drawer.replaceChildren(closeBtn, el("p", { class: "error", role: "alert" }, "Could not load that entity."));
    return;
  }
  if (drawerEl !== drawer) return;                // another row was opened meanwhile
  const title = el("h2", { class: "drawer-title", tabindex: "-1" }, d ? d.entity.name : "Not in this workspace");
  if (!d) {
    // A link carries an EXACT id; the search box matches substrings. A shortened or half-remembered
    // id lands here, so offer the search rather than implying the entity was deleted.
    const tryIt = el("button", { class: "btn", type: "button" }, `Search for \u201c${id}\u201d`);
    tryIt.addEventListener("click", () => {
      entQ = id;
      entOffset = 0;
      close();
      void renderEntities("");
    });
    drawer.replaceChildren(closeBtn, title, el("p", { class: "hint" },
      `Nothing in this workspace has the id \u201c${id}\u201d. A link carries the exact id, so a nickname or a `
      + `shortened name lands here.`), tryIt);
    title.focus();
    return;
  }
  const head = el("div", { class: "drawer-head" }, title,
    el("div", { class: "drawer-meta" }, d.entity.type ? el("span", { class: "echip" }, d.entity.type) : "", el("code", { class: "mono" }, d.entity.id)));
  const parts: Node[] = [closeBtn, head];
  if (d.entity.aliases.length)
    parts.push(el("p", { class: "hint" }, "Also known as: " + d.entity.aliases.map((a) => a.name ?? a.id).join(", ")));
  const n = d.sessions.length;
  const sessions = el("ul", { class: "drawer-sessions" });
  for (const s of d.sessions) {
    const meta = [s.project, s.user ? ownerName(s.user) : "", s.started_at ? fmtDate(s.started_at) : "",
      `${s.facts} ${s.facts === 1 ? "fact" : "facts"}`].filter(Boolean).join(" · ");
    sessions.append(el("li", {},
      el("a", { href: sessionHref(s.session_id) }, s.summary || shortId(s.session_id)),
      el("span", { class: "project" }, meta)));
  }
  parts.push(el("h3", {}, `Mentioned in ${n} ${n === 1 ? "session" : "sessions"}`), sessions);
  parts.push(el("h3", {}, `Facts from the ${n} ${n === 1 ? "session" : "sessions"} that mention ${d.entity.name}`),
    el("p", { class: "hint" }, `These sessions mentioned ${d.entity.name}. Not every fact is about it.`));
  if (!d.facts.length) parts.push(el("p", { class: "hint" }, "No facts came out of these sessions yet."));
  else {
    const list = el("div", { class: "facts" });
    const FOLD = 8;
    for (const f of d.facts.slice(0, FOLD)) list.append(factRowCard(f));
    parts.push(list);
    if (d.facts.length > FOLD) {
      const more = el("button", { class: "btn", type: "button" }, `Show all ${d.facts.length} facts`);
      more.addEventListener("click", () => {
        for (const f of d.facts.slice(FOLD)) list.append(factRowCard(f));
        more.remove();
      });
      parts.push(more);
    }
  }
  drawer.replaceChildren(...parts);
  title.focus();
}
