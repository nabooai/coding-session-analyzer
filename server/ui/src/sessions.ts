// Pure helpers for the Sessions page and Memory's Graph view, kept out of main.ts so vitest can import
// them with no DOM.
import type { GraphWindow } from "./api";

// A developer's own Claude Code session, priced from its usage. Analytics' CSA_SPEND is a different bill.
export const SESSION_COST = "Claude Code cost";
export const SESSION_COST_DEF =
  "What this Claude Code session's own model use would cost at list prices. On a subscription nothing was charged. Not CSA extraction spend, which is on Admin > Analytics.";

export const PROD_TIP = "This session released code or config to production.";

/** "·" is unknown, never free: a measured zero is "$0.00", a measured fraction of a cent "<$0.01". */
export function fmtCost(n: number | null | undefined): string {
  if (typeof n !== "number") return "·";
  return n > 0 && n < 0.005 ? "<$0.01" : `$${n.toFixed(2)}`;
}

export function fmtCount(n: number | null | undefined): string {
  return typeof n === "number" ? n.toLocaleString("en-US") : "·";
}

/** Seconds as "48m" or "1h 12m" ("40s" under a minute); "·" when unknown. */
export function fmtDur(s: number | null | undefined): string {
  if (typeof s !== "number") return "·";
  const n = Math.max(0, Math.round(s));
  if (n >= 3600) return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
  return n >= 60 ? `${Math.floor(n / 60)}m` : `${n}s`;
}

// Each span carries the session cap the server applies to it.
export const GRAPH_WINDOWS = [
  { days: 30, limit: 60, label: "Last 30 days" },
  { days: 90, limit: 120, label: "Last 90 days" },
  { days: 3650, limit: 120, label: "All time" },
] as const;
export type GraphWindowChoice = (typeof GRAPH_WINDOWS)[number];

/** The window for a `?w=<days>` value; anything else is the default. */
export function graphWindow(days: string | null | undefined): GraphWindowChoice {
  return GRAPH_WINDOWS.find((w) => String(w.days) === days) ?? GRAPH_WINDOWS[0];
}

/** "Last 30 days · 12 sessions", or "Last 30 days · newest 60 of 143 sessions" when a cap cut the list. */
export function windowLabel(w: GraphWindow, shown: number): string {
  const span = w.days >= 3650 ? "All time" : `Last ${w.days} days`;
  const n = (k: number) => `${k.toLocaleString("en-US")} ${k === 1 ? "session" : "sessions"}`;
  return shown < w.total ? `${span} · newest ${shown.toLocaleString("en-US")} of ${n(w.total)}` : `${span} · ${n(shown)}`;
}

const SHORT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

/** "Aug 20" for a canvas label; the text as it came when it does not parse. */
export function shortDate(iso: string): string {
  const t = Date.parse(iso.length === 10 ? iso + "T00:00:00" : iso);
  return Number.isNaN(t) ? iso : SHORT.format(t);
}
