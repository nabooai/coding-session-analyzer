import { describe, expect, it } from "vitest";
import { LABEL_TEXT, RECALL_FILTERS, RECALL_SOURCE, REUSED_TILE, foldSameClaim, labelText, recallLogLine, reuseClaim, seenLine } from "./memory";

describe("reuse and the recall log", () => {
  it("names the channel where a row shows no session, never 'none' and never who asked", () => {
    expect(Object.values(RECALL_SOURCE).map((s) => s.text)).toEqual(["Claude Code hook", "API", "MCP"]);
    for (const s of Object.values(RECALL_SOURCE)) {
      expect(s.text).not.toMatch(/none/i);
      expect(s.title).not.toMatch(/[–—]/);
    }
  });
  it("claims reuse in the unit Analytics counts: teammates and their sessions", () => {
    expect(reuseClaim(0, 0)).toBe("");
    expect(reuseClaim(1, 1)).toBe("reused by 1 teammate in 1 session");
    expect(reuseClaim(5, 2)).toBe("reused by 2 teammates in 5 sessions");
    expect(REUSED_TILE.title).toMatch(/someone other than their author/);
  });
  it("says what the log is out of, so its rows square with the count", () => {
    expect(recallLogLine({ total: 0, answered: 0, rows: [] })).toBe("");
    expect(recallLogLine({ total: 72, answered: 2, rows: new Array(72) })).toBe("72 recall requests all time, 2 answered.");
    expect(recallLogLine({ total: 1200, answered: 30, rows: new Array(200) }))
      .toBe("1,200 recall requests all time, 30 answered. Showing the newest 200.");
    expect(recallLogLine({ total: 72, answered: 2, rows: new Array(70) }, "missed")).toBe("72 recall requests all time, 2 answered.");
    expect(recallLogLine({ total: 1200, answered: 30, rows: new Array(200) }, "missed"))
      .toBe("1,200 recall requests all time, 30 answered. Showing the newest 200 missed.");
    expect(RECALL_FILTERS.map((f) => f.label)).toEqual(["All", "Answered", "Missed"]);
  });
  it("has no dashes in the copy", () => {
    for (const t of [REUSED_TILE.label, REUSED_TILE.title, reuseClaim(3, 2), recallLogLine({ total: 3, answered: 1, rows: [] })])
      expect(t).not.toMatch(/[–—]/);
  });
});

const row = (ref: string, claim: string | null, session_id = ref, project: string | null = "webapp") =>
  ({ ref, claim, session_id, project });

describe("foldSameClaim", () => {
  it("folds rows that state the same claim into the first, in page order", () => {
    const groups = foldSameClaim([
      row("a", "Tests must be reproducible across CI runs."),
      row("b", "Store UTC."),
      row("c", "tests must be  reproducible across CI runs"),
    ]);
    expect(groups.map((g) => g.row.ref)).toEqual(["a", "b"]);
    expect(groups[0].same.map((r) => r.ref)).toEqual(["c"]);
    expect(groups[1].same).toEqual([]);
  });

  it("never folds rows without a claim", () => {
    const groups = foldSameClaim([row("a", null), row("b", null), row("c", "")]);
    expect(groups.map((g) => g.row.ref)).toEqual(["a", "b", "c"]);
  });
});

describe("seenLine", () => {
  it("is empty for a single row", () => {
    expect(seenLine([row("a", "x")])).toBe("");
  });

  it("counts distinct sessions and lists distinct projects", () => {
    expect(seenLine([row("a", "x", "s1", "webapp"), row("b", "x", "s2", "infra"), row("c", "x", "s3", "webapp")]))
      .toBe("Seen in 3 sessions · webapp, infra");
  });

  it("says when a claim repeats inside one session", () => {
    expect(seenLine([row("a", "x", "s1", null), row("b", "x", "s1", null)])).toBe("Stated 2 times in one session");
  });
});

describe("rating vocabulary", () => {
  it("names each wire label once, the same everywhere", () => {
    expect(LABEL_TEXT).toEqual({ gold: "Useful", wrong: "Wrong", unimportant: "Noise", clear: "Undo" });
    expect(labelText("gold")).toBe("Useful");
    expect(labelText("stale")).toBe("stale");
  });

  it("uses no em or en dashes in reader-facing text", () => {
    for (const t of Object.values(LABEL_TEXT)) expect(t).not.toMatch(/[–—]/);
  });
});
