import { describe, expect, it } from "vitest";
import { fmtCost, fmtCount, fmtDur, graphWindow, shortDate, windowLabel } from "./sessions";

describe("fmtCost", () => {
  it("is a dot for an unmeasured or hidden cost, and never $0.00 for unknown", () => {
    expect(fmtCost(null)).toBe("·");
    expect(fmtCost(undefined)).toBe("·");
    expect(fmtCost(0)).toBe("$0.00");
    expect(fmtCost(0.0045)).toBe("<$0.01");
    expect(fmtCost(0.005)).toBe("$0.01");
    expect(fmtCost(4.8199)).toBe("$4.82");
  });
});

describe("fmtCount / fmtDur", () => {
  it("dot for unknown, plain numbers otherwise", () => {
    expect(fmtCount(null)).toBe("·");
    expect(fmtCount(0)).toBe("0");
    expect(fmtCount(1234)).toBe("1,234");
    expect(fmtDur(null)).toBe("·");
    expect(fmtDur(0)).toBe("0s");
    expect(fmtDur(2880)).toBe("48m");
    expect(fmtDur(4320)).toBe("1h 12m");
  });
});

describe("graphWindow / windowLabel", () => {
  it("falls back to 30 days for anything unknown", () => {
    expect(graphWindow(null).days).toBe(30);
    expect(graphWindow("zz").days).toBe(30);
    expect(graphWindow("90")).toEqual({ days: 90, limit: 120, label: "Last 90 days" });
    expect(graphWindow("3650").label).toBe("All time"); // the option and the window label say the same thing
  });
  it("says when a cap cut the list", () => {
    const w = { days: 30, limit: 60, facts_budget: 600, since: "", total: 143 };
    expect(windowLabel(w, 60)).toBe("Last 30 days · newest 60 of 143 sessions");
    expect(windowLabel({ ...w, total: 12 }, 12)).toBe("Last 30 days · 12 sessions");
    expect(windowLabel({ ...w, total: 1 }, 1)).toBe("Last 30 days · 1 session");
    expect(windowLabel({ ...w, days: 3650, total: 500 }, 120)).toBe("All time · newest 120 of 500 sessions");
  });
});

describe("shortDate", () => {
  it("is month and day, and leaves junk alone", () => {
    expect(shortDate("2026-08-20")).toBe("Aug 20");
    expect(shortDate("not a date")).toBe("not a date");
  });
});
