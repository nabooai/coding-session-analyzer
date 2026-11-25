// Pure helpers for Memory's list, kept out of main.ts so vitest can import them with no DOM.
import type { Label, RecallFilter, RecallRow } from "./api";

/** What each verdict reads as, everywhere a fact is rated. The wire values stay gold/wrong/unimportant/clear. */
export const LABEL_TEXT: Record<Label, string> = {
  gold: "Useful",
  wrong: "Wrong",
  unimportant: "Noise",
  clear: "Undo",
};

/** A raw label from the server as the reader sees it; an unknown one passes through. */
export function labelText(label: string): string {
  return LABEL_TEXT[label as Label] ?? label;
}

/** The product in one line, shown wherever a reader has not connected a machine yet. */
export const RECALL_LINE =
  "Once your machine is connected, matching facts are recalled into your Claude Code prompts automatically.";

function claimKey(claim: string | null): string {
  return (claim ?? "").trim().toLowerCase().replace(/\s+/g, " ").replace(/[.!]+$/, "");
}

/** Rows stating the same claim, folded into the first in page order. A row with no claim is never folded. */
export function foldSameClaim<T extends { claim: string | null }>(rows: T[]): { row: T; same: T[] }[] {
  const out: { row: T; same: T[] }[] = [];
  const byKey = new Map<string, { row: T; same: T[] }>();
  for (const row of rows) {
    const key = claimKey(row.claim);
    const hit = key ? byKey.get(key) : undefined;
    if (hit) {
      hit.same.push(row);
      continue;
    }
    const group = { row, same: [] as T[] };
    if (key) byKey.set(key, group);
    out.push(group);
  }
  return out;
}

/** "Seen in 3 sessions · webapp, infra" for a folded card; "" when nothing was folded into it. */
export function seenLine(rows: { session_id: string; project: string | null }[]): string {
  if (rows.length < 2) return "";
  const sessions = new Set(rows.map((r) => r.session_id)).size;
  const projects = [...new Set(rows.map((r) => r.project).filter((p): p is string => !!p))];
  const where = sessions === 1 ? `Stated ${rows.length} times in one session` : `Seen in ${sessions} sessions`;
  return projects.length ? `${where} · ${projects.join(", ")}` : where;
}

/** The Welcome tile, in the words of the one server definition (analytics.reuses). */
export const REUSED_TILE = {
  label: "reused by teammates",
  title: "Facts served into a session of someone other than their author, all time. Admin > Analytics counts the same thing.",
} as const;

/** "reused by 2 teammates in 5 sessions"; "" when there is no reuse to claim. */
export function reuseClaim(sessions: number, people: number): string {
  if (sessions <= 0) return "";
  const who = people === 1 ? "1 teammate" : `${people.toLocaleString("en-US")} teammates`;
  const where = sessions === 1 ? "1 session" : `${sessions.toLocaleString("en-US")} sessions`;
  return `reused by ${who} in ${where}`;
}

/** The recall log's caption: what the list is out of, counted as Analytics counts recall requests. */
export function recallLogLine(log: { total: number; answered: number; rows: unknown[] }, filter: RecallFilter = ""): string {
  if (!log.total) return "";
  const n = (k: number) => k.toLocaleString("en-US");
  const head = `${n(log.total)} recall ${log.total === 1 ? "request" : "requests"} all time, ${n(log.answered)} answered.`;
  const of = filter === "answered" ? log.answered : filter === "missed" ? log.total - log.answered : log.total;
  if (log.rows.length >= of) return head;
  return `${head} Showing the newest ${n(log.rows.length)}${filter ? ` ${filter}` : ""}.`;
}

/** The recall log's filter, in the order the buttons show. */
export const RECALL_FILTERS: { value: RecallFilter; label: string }[] = [
  { value: "", label: "All" }, { value: "answered", label: "Answered" }, { value: "missed", label: "Missed" }];

/** The recall log's "Asked from" cell when it shows no session: the channel, never who asked. */
export const RECALL_SOURCE: Record<RecallRow["source"], { text: string; title: string }> = {
  hook: { text: "Claude Code hook", title: "The prompt hook in a Claude Code session. A teammate's session stays private when nothing was found." },
  api: { text: "API", title: "A direct call to /api/ask, not the prompt hook." },
  mcp: { text: "MCP", title: "The CSA MCP ask tool." },
};
