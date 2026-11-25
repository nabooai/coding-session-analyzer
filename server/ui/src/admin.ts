// Pure helpers for the Admin pages: chart scales and paths, the delta sentence under each tile, the
// Analytics definitions, and relative time. No DOM here (main.ts draws), so vitest can pin the
// numbers and the copy.
import { SESSION_COST } from "./sessions";

export const CSA_SPEND = "CSA extraction spend";

/** Axis ticks from 0 to a round ceiling at or above `max`: steps of 1, 2, 2.5 or 5 times a power of
 *  ten, chosen so the axis carries four to six labels; `integer` keeps a count axis on whole numbers.
 *  `max` of 0 still yields a visible axis. */
export function ticks(max: number, want = 5, integer = false): number[] {
  if (!(max > 0)) return [0, 1];
  const rough = max / want;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  let step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= rough) ?? 10 * mag;
  if (integer) step = Math.max(1, Math.round(step));   // a count axis never reads "1, 1, 2, 2"
  const out: number[] = [];
  for (let v = 0; v < max + step; v += step) out.push(Number(v.toFixed(6)));
  return out;
}

/** The pixel y of a value on a plot of height `h` whose top tick is `top`. */
export function yOf(v: number, top: number, h: number): number {
  return top > 0 ? h - (v / top) * h : h;
}

/** SVG path for a polyline through evenly spaced points, "M x,y L x,y…" (empty for no points). */
export function linePath(values: number[], w: number, h: number, top: number): string {
  if (values.length === 0) return "";
  const dx = values.length > 1 ? w / (values.length - 1) : 0;
  return values.map((v, i) => `${i ? "L" : "M"}${(i * dx).toFixed(1)},${yOf(v, top, h).toFixed(1)}`).join(" ");
}

/** The line closed down to the baseline, for the area wash under it. */
export function areaPath(values: number[], w: number, h: number, top: number): string {
  const line = linePath(values, w, h, top);
  if (!line) return "";
  const dx = values.length > 1 ? w / (values.length - 1) : 0;
  return `${line} L${((values.length - 1) * dx).toFixed(1)},${h} L0,${h} Z`;
}

export type Delta = { text: string; dir: "up" | "down" | "flat" | "none" };

/** The sentence under a tile: the absolute change against the previous window, with a percentage
 *  only when the previous window held enough (20 or more) for one to mean something. A previous
 *  window that is unknown (null) says so; one that held nothing gets the plain change, never an
 *  infinite rise. */
export function deltaLine(cur: number, prev: number | null, weeks: number, fmt: (n: number) => string = fmtInt): Delta {
  const window = `the previous ${weeks} weeks`;
  if (prev === null) return { text: `no data for ${window}`, dir: "none" };
  const diff = cur - prev;
  if (Math.abs(diff) < 1e-9) return { text: `no change vs ${window}`, dir: "flat" };
  const sign = diff > 0 ? "+" : "-";
  const pct = prev >= 20 ? ` (${sign}${Math.round((Math.abs(diff) / prev) * 100)}%)` : "";
  return { text: `${sign}${fmt(Math.abs(diff))} vs ${window}${pct}`, dir: diff > 0 ? "up" : "down" };
}

export function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

/** Dollars with cents below 1,000, whole dollars above (a tile value, not an invoice). */
export function fmtUsd(n: number): string {
  if (n >= 1000) return `$${fmtInt(n)}`;
  return `$${n.toFixed(2)}`;
}

/** "1 session", "12 sessions", "1 person", "3 people". */
export function count(n: number, one: string, many = `${one}s`): string {
  return `${fmtInt(n)} ${n === 1 ? one : many}`;
}

/** A share as a whole percent, "·" when there is nothing to divide by. */
export function pct(n: number, of: number): string {
  return of > 0 ? `${Math.round((n / of) * 100)}%` : "·";
}

/** The Analytics date range: whole weeks ending today. */
export const WINDOWS: { weeks: 4 | 12 | 26; label: string }[] = [
  { weeks: 4, label: "4 weeks" },
  { weeks: 12, label: "12 weeks" },
  { weeks: 26, label: "6 months" },
];

/** What each Analytics number counts, shown on hover and focus. One sentence each, in the words of
 *  the server's own definition (analyzer/analytics.py), so a buyer can check a number against it. */
export const DEFS = {
  contributors: "People with at least one Claude Code session shipped to CSA in this window, including anyone without a member credential. Agents, which ship with a service token, are counted apart.",
  sessions: "Claude Code sessions that started in this window and reached CSA.",
  silent: "Connected machines with no contact for over 7 days. A machine is silent only when every credential it holds is. A plugin in manual mode sends only what its owner chooses, so it can look silent.",
  members: "People holding a live, non-service CSA credential. Shipping: shipped a session in this window. Quiet: has a connected machine but shipped nothing. Not connected: signed in through the browser only. Team also lists anyone who ever shipped a session without one.",
  memoryTotal: "Every fact CSA holds for this workspace, all time.",
  memoryNew: "Facts first extracted from a session in this window, by type.",
  asks: "Every recall request: prompts the Claude Code hook checked automatically, plus questions asked through MCP or the csa-recall skill. Recalled into sessions lists these same requests.",
  answered: "Requests that returned at least one result. The rest were honest misses: nothing good enough to show.",
  acted: "Answered requests from sessions shipped to CSA whose facts the session then visibly used. Measured by word overlap, so the real share is higher.",
  transfer: "Facts served into a session of someone other than their author, the same count as the Welcome page. Excludes recalls into a session that produced the fact and into its author's own sessions.",
  topReused: "Facts that reached the most people other than their author in this window.",
  contributorsTable: "Who added facts in this window, and how many teammates their facts reached.",
  backlog: "Sessions waiting for the extraction model to read them. Gave up: sessions whose extraction failed and were set aside.",
  spend: `What CSA itself paid the workspace AI provider to read sessions and judge facts, priced only for a model with a list price. Not your team's Claude Code bill: that is the ${SESSION_COST} column on Sessions, what each developer's own session would cost at list prices.`,
  cap: "The workspace's daily extraction budget, set with csa.py org config. Extraction pauses for the day when it is reached.",
} as const;

/** The weekly x labels: each bucket's first day, "Aug 9". */
export function weekLabels(starts: string[]): string[] {
  return starts.map(shortDay);
}

/** The total of each stacked bar. */
export function stackTotals(rows: number[][]): number[] {
  return rows.map((r) => r.reduce((a, b) => a + b, 0));
}

/** "2026-09-01" (or a fuller ISO stamp) as "Sep 1". */
export function shortDay(day: string): string {
  const t = Date.parse(day.length === 10 ? day + "T00:00:00Z" : day);
  if (Number.isNaN(t)) return day;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }).format(t);
}

/** How long ago an ISO stamp was, in the unit a person would use. `now` is epoch milliseconds. */
export function ago(iso: string, now: number): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 90) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} minutes ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} ${h === 1 ? "hour" : "hours"} ago`;
  const d = Math.round(h / 24);
  return `${d} ${d === 1 ? "day" : "days"} ago`;
}

/** Which x labels to print under a daily series: first, last, and every `every`th in between, so
 *  90 days shows about ten labels and 7 days shows all seven. */
export function labelIndexes(n: number, want = 8): number[] {
  if (n <= 0) return [];
  if (n <= want) return Array.from({ length: n }, (_, i) => i);
  const every = Math.ceil((n - 1) / (want - 1));
  const out: number[] = [];
  for (let i = 0; i < n - 1; i += every) out.push(i);
  if (out[out.length - 1] !== n - 1) {
    if (n - 1 - out[out.length - 1] < every / 2) out.pop();
    out.push(n - 1);
  }
  return out;
}

/** The page a hash renders, for the page_view event; "" for a redirect or a session page (a session
 *  open is its own event, fired once the session has loaded). */
export function pageName(hash: string): string {
  if (hash === "" || hash === "/") return "memory";
  if (hash === "/corpus") return "sessions";
  if (hash === "/team") return "team";
  if (hash === "/connect") return "connect";
  if (hash === "/admin") return "admin";
  if (hash === "/admin/settings") return "settings";
  if (hash.startsWith("/today")) return "today";
  if (hash.startsWith("/session/")) return "recall";
  if (["/welcome", "/improve", "/pipeline", "/questions", "/labels", "/recall"].includes(hash)) return hash.slice(1);
  return "";
}

/** Shown to admins when the etl cannot extract; null when nothing is wrong or the server said nothing. */
export function stallNotice(x: { stalled: boolean; reason: string } | undefined): string | null {
  if (!x?.stalled) return null;
  return `Extraction is stalled, so new sessions are not being read.${x.reason ? ` Reason: "${x.reason}".` : ""} It resumes by itself once this is fixed.`;
}

export const MACHINE_WAITING =
  "Your machine is connected. Decisions, gotchas and constraints appear here within minutes of a session ending.";

/** The Acted on tile. Only a session shipped to CSA can show use, so the denominator is the answered
 *  requests from those; with none nothing was measured, and a lower bound of zero is not shown. */
export function actedTile(r: { acted: number; measured: number }): { value: string; note: string } {
  if (!r.measured) return { value: "Not measured", note: "no answered request came from a session shipped to CSA yet" };
  const base = count(r.measured, "answered request");
  if (!r.acted) return { value: "None seen", note: `in ${base} from shipped sessions` };
  return { value: `≥ ${pct(r.acted, r.measured)}`, note: `of ${base} from shipped sessions, a lower bound` };
}

/** The Members card headline, naming the people Team also lists because they shipped without a credential. */
export function membersLine(d: { members: number; shipped_without_credential: number }): string {
  const base = d.members ? `${count(d.members, "person", "people")} with a CSA credential` : "Nobody holds a CSA credential yet";
  return d.shipped_without_credential ? `${base}. Team also lists ${fmtInt(d.shipped_without_credential)} who shipped without one` : base;
}

/** Under the contributors tile: how many of them the Members card does not count, and the agents beside them. */
export function contributorsNote(d: { contributors: number; members_shipping: number; agents: number }): string {
  const n = d.contributors - d.members_shipping;
  const parts = n > 0 ? [`including ${fmtInt(n)} ${n === 1 ? "who is not a member" : "who are not members"}`] : [];
  if (d.agents) parts.push(`plus ${count(d.agents, "agent")}`);
  return parts.join(" · ");
}

/** "3 people and 1 agent": people are humans on every screen, an agent (a service token) is counted apart. */
export function peopleLine(people: number, agents: number): string {
  const p = count(people, "person", "people");
  return agents ? `${p} and ${count(agents, "agent")}` : p;
}

const two = (n: number) => String(n).padStart(2, "0");

/** A server timestamp as a Date. The server writes UTC, with or without the `Z`. */
function utc(iso: string): Date {
  return new Date(/(Z|[+-]\d\d:?\d\d)$/i.test(iso) ? iso : `${iso.replace(" ", "T")}Z`);
}

const ZONE_FMT = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" });

/** The reader's zone as the browser abbreviates it at `d`: "UTC", "PDT", "GMT+3". */
export function zone(d: Date): string {
  return ZONE_FMT.formatToParts(d).find((p) => p.type === "timeZoneName")?.value ?? "";
}

/** "2026-10-19 15:47 GMT+3": a server timestamp in the reader's clock, the clock `asOf` reads. */
export function fmtWhen(iso: string): string {
  const d = utc(iso);
  if (iso.length <= 10 || Number.isNaN(d.getTime())) return iso.slice(0, 16).replace("T", " ");
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())} ${zone(d)}`;
}

/** "As of 14:32 GMT+3": when the server counted, in the reader's clock, so two screens read a minute
 *  apart are seen to be two snapshots of a log that keeps growing. */
export function asOf(iso: string): string {
  const d = utc(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `As of ${two(d.getHours())}:${two(d.getMinutes())} ${zone(d)}`;
}

/** The Web app use card: its title, and the sentence it shows in place of figures when nobody used the app. */
export function webUse(u: { people: number; searches: number; session_opens: number; flags: number; pages: unknown[] }): { title: string; empty: string } {
  return !u.people && !u.searches && !u.session_opens && !u.flags && !u.pages.length
    ? { title: "Web app use", empty: "Nobody used the web app in this window." }
    : { title: `Web app use · ${count(u.people, "person", "people")}`, empty: "" };
}

/** Under the reuse tile: the all-time count Welcome shows, then the handoffs behind this window's figure. */
export function reuseNote(t: { facts_all_time: number; deliveries: number; receivers: number; receiving_agents: number }): string {
  return `${fmtInt(t.facts_all_time)} all time · ${count(t.deliveries, "handoff")} to ${peopleLine(t.receivers, t.receiving_agents)}`;
}

/** The CSA spend tile when a provider reported tokens but no price: the same "not priced" Sessions shows. */
export const UNPRICED = "not priced: this provider's model has no list price in CSA";
