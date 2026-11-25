import { describe, expect, it } from "vitest";
import { SESSION_COST, SESSION_COST_DEF } from "./sessions";
import { CSA_SPEND, DEFS, UNPRICED, WINDOWS, actedTile, ago, areaPath, asOf, contributorsNote, peopleLine, count, deltaLine, fmtUsd, fmtWhen, labelIndexes, linePath, membersLine, pageName, pct, reuseNote, shortDay, stackTotals, stallNotice, ticks, webUse, weekLabels, yOf, zone } from "./admin";

describe("one definition per number", () => {
  it("shows no acted-on rate until an answered request came from a shipped session, and never a bound of zero", () => {
    expect(actedTile({ acted: 0, measured: 0 })).toEqual({ value: "Not measured", note: "no answered request came from a session shipped to CSA yet" });
    expect(actedTile({ acted: 0, measured: 4 })).toEqual({ value: "None seen", note: "in 4 answered requests from shipped sessions" });
    expect(actedTile({ acted: 1, measured: 4 })).toEqual({ value: "≥ 25%", note: "of 4 answered requests from shipped sessions, a lower bound" });
  });
  it("names the people Team lists beside the members, so the card and the table add up", () => {
    expect(membersLine({ members: 4, shipped_without_credential: 1 })).toBe("4 people with a CSA credential. Team also lists 1 who shipped without one");
    expect(membersLine({ members: 4, shipped_without_credential: 0 })).toBe("4 people with a CSA credential");
    expect(membersLine({ members: 0, shipped_without_credential: 0 })).toBe("Nobody holds a CSA credential yet");
  });
  it("names the two costs apart on every screen, and each definition points at the other", () => {
    expect(SESSION_COST).toBe("Claude Code cost");
    expect(CSA_SPEND).toBe("CSA extraction spend");
    expect(DEFS.spend).toContain("workspace AI provider");
    expect(SESSION_COST_DEF).toContain(CSA_SPEND);
    expect(SESSION_COST_DEF).toMatch(/Not CSA extraction spend, which is on Admin > Analytics/);
    expect(SESSION_COST_DEF).not.toMatch(/[–—]/);
  });
  it("says in plain words how many contributors the Members card leaves out", () => {
    expect(contributorsNote({ contributors: 3, members_shipping: 2, agents: 0 })).toBe("including 1 who is not a member");
    expect(contributorsNote({ contributors: 5, members_shipping: 2, agents: 0 })).toBe("including 3 who are not members");
    expect(contributorsNote({ contributors: 2, members_shipping: 2, agents: 0 })).toBe("");
    expect(contributorsNote({ contributors: 2, members_shipping: 2, agents: 1 })).toBe("plus 1 agent");
    expect(DEFS.contributors).toMatch(/without a member credential/);
    expect(DEFS.contributors).toMatch(/Agents.*counted apart/);
  });
  it("counts people and agents apart in one phrase every screen shares", () => {
    expect(peopleLine(3, 0)).toBe("3 people");
    expect(peopleLine(1, 2)).toBe("1 person and 2 agents");
    expect(peopleLine(2, 1)).toBe("2 people and 1 agent");
  });
  it("stamps a snapshot with the server's time in the reader's clock", () => {
    const iso = "2026-10-19T09:05:00Z";
    const d = new Date(iso);
    expect(asOf(iso)).toBe(`As of ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} ${zone(d)}`);
    expect(asOf("")).toBe("");
  });
  it("names the reader's zone on every stamp", () => {
    expect(zone(new Date("2026-10-19T09:05:00Z"))).toMatch(/^(UTC|GMT([+-]\d{1,2}(:\d\d)?)?|[A-Z]{2,5})$/);
    expect(asOf("2026-10-19T09:05:00Z")).toMatch(/^As of \d\d:\d\d \S+$/);
    expect(fmtWhen("2026-10-19T09:05:00Z")).toMatch(/^\d{4}-\d\d-\d\d \d\d:\d\d \S+$/);
  });
  it("shows a row's time in the same clock as the stamp above it", () => {
    const stamp = "2026-10-19T12:47:44Z", row = "2026-10-19T12:47:32Z";
    const d = new Date(row);
    const local = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} ${zone(d)}`;
    expect(fmtWhen(row)).toBe(local);
    expect(fmtWhen(row).slice(11)).toBe(asOf(stamp).slice(6));
    expect(fmtWhen("2026-10-19 12:47:32")).toBe(local);
    expect(asOf("2026-10-19 12:47:32")).toBe(asOf(row));
    expect(fmtWhen("2026-10-19")).toBe("2026-10-19");
  });
  it("shows Web app use's figures under its title, and says so when nobody used it", () => {
    expect(webUse({ people: 4, searches: 2, session_opens: 0, flags: 0, pages: [{}] })).toEqual({ title: "Web app use · 4 people", empty: "" });
    expect(webUse({ people: 0, searches: 0, session_opens: 0, flags: 0, pages: [] })).toEqual({ title: "Web app use", empty: "Nobody used the web app in this window." });
  });
  it("puts Welcome's all-time reuse figure beside the window's", () => {
    expect(reuseNote({ facts_all_time: 5, deliveries: 17, receivers: 2, receiving_agents: 0 })).toBe("5 all time · 17 handoffs to 2 people");
    expect(reuseNote({ facts_all_time: 5, deliveries: 31, receivers: 2, receiving_agents: 1 })).toBe("5 all time · 31 handoffs to 2 people and 1 agent");
  });
  it("says a cost is not priced in the same words Sessions uses, and tells the two costs apart", () => {
    expect(UNPRICED).toMatch(/^not priced/);
    expect(DEFS.spend).toContain(`${SESSION_COST} column on Sessions`);
    expect(DEFS.transfer).toMatch(/same count as the Welcome page/);
    expect(DEFS.asks).toMatch(/Recalled into sessions lists these same requests/);
  });
});

describe("stallNotice", () => {
  it("says nothing unless the server reports a stall", () => {
    expect(stallNotice(undefined)).toBeNull();
    expect(stallNotice({ stalled: false, reason: "" })).toBeNull();
  });
  it("leads with the fact and carries the server's reason verbatim", () => {
    const text = stallNotice({ stalled: true, reason: "claude failed: Invalid API key" });
    expect(text).toMatch(/^Extraction is stalled/);
    expect(text).toContain("claude failed: Invalid API key");
    expect(text).not.toMatch(/[–—]/);
  });
  it("still names the stall when the reason is empty", () => {
    expect(stallNotice({ stalled: true, reason: "" })).toMatch(/^Extraction is stalled, so new sessions are not being read\. It resumes/);
  });
});

describe("ticks", () => {
  it("rounds the axis ceiling to a clean step and keeps four to six labels", () => {
    expect(ticks(412)).toEqual([0, 100, 200, 300, 400, 500]);
    expect(ticks(3.4)).toEqual([0, 1, 2, 3, 4]);
    expect(ticks(61.2)).toEqual([0, 20, 40, 60, 80]);
    expect(ticks(7)).toEqual([0, 2, 4, 6, 8]);
    expect(ticks(1)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
  });
  it("keeps a count axis on whole numbers", () => {
    expect(ticks(2, 5, true)).toEqual([0, 1, 2]);
    expect(ticks(1, 5, true)).toEqual([0, 1]);
    expect(ticks(7, 5, true)).toEqual([0, 2, 4, 6, 8]);
    expect(ticks(412, 5, true)).toEqual([0, 100, 200, 300, 400, 500]);
  });
  it("still draws an axis for an empty window", () => {
    expect(ticks(0)).toEqual([0, 1]);
    expect(ticks(Number.NaN)).toEqual([0, 1]);
  });
});

describe("paths", () => {
  it("maps values onto the plot with the top tick at y=0 and zero at the baseline", () => {
    expect(yOf(0, 10, 100)).toBe(100);
    expect(yOf(10, 10, 100)).toBe(0);
    expect(yOf(5, 10, 100)).toBe(50);
    expect(yOf(3, 0, 100)).toBe(100);
  });
  it("spaces points evenly and closes the area down to the baseline", () => {
    expect(linePath([0, 5, 10], 200, 100, 10)).toBe("M0.0,100.0 L100.0,50.0 L200.0,0.0");
    expect(areaPath([0, 5, 10], 200, 100, 10)).toBe("M0.0,100.0 L100.0,50.0 L200.0,0.0 L200.0,100 L0,100 Z");
    expect(linePath([4], 200, 100, 10)).toBe("M0.0,60.0");
    expect(linePath([], 200, 100, 10)).toBe("");
    expect(areaPath([], 200, 100, 10)).toBe("");
  });
});

describe("deltaLine", () => {
  it("leads with the absolute change and names the real window", () => {
    expect(deltaLine(412, 368, 12)).toEqual({ text: "+44 vs the previous 12 weeks (+12%)", dir: "up" });
    expect(deltaLine(12, 20, 4)).toEqual({ text: "-8 vs the previous 4 weeks (-40%)", dir: "down" });
  });
  it("gives no percentage off a small previous window", () => {
    expect(deltaLine(3, 1, 4)).toEqual({ text: "+2 vs the previous 4 weeks", dir: "up" });
    expect(deltaLine(0, 19, 26)).toEqual({ text: "-19 vs the previous 26 weeks", dir: "down" });
  });
  it("says when there is nothing to compare against, never an infinite rise", () => {
    expect(deltaLine(5, 0, 12)).toEqual({ text: "+5 vs the previous 12 weeks", dir: "up" });
    expect(deltaLine(0, 0, 12)).toEqual({ text: "no change vs the previous 12 weeks", dir: "flat" });
    expect(deltaLine(5, null, 12)).toEqual({ text: "no data for the previous 12 weeks", dir: "none" });
    expect(deltaLine(7, 7, 12)).toEqual({ text: "no change vs the previous 12 weeks", dir: "flat" });
  });
  it("formats money with the caller's formatter", () => {
    expect(deltaLine(61.2, 66.5, 12, fmtUsd)).toEqual({ text: "-$5.30 vs the previous 12 weeks (-8%)", dir: "down" });
  });
});

describe("formatting", () => {
  it("shows cents below a thousand dollars and whole dollars above", () => {
    expect(fmtUsd(61.2)).toBe("$61.20");
    expect(fmtUsd(0)).toBe("$0.00");
    expect(fmtUsd(1204.5)).toBe("$1,205");
  });
  it("counts in the singular for one", () => {
    expect(count(1, "session")).toBe("1 session");
    expect(count(1200, "session")).toBe("1,200 sessions");
    expect(count(1, "person", "people")).toBe("1 person");
    expect(count(0, "person", "people")).toBe("0 people");
  });
  it("shows a share as a whole percent and nothing over an empty base", () => {
    expect(pct(1, 3)).toBe("33%");
    expect(pct(0, 5)).toBe("0%");
    expect(pct(3, 0)).toBe("·");
  });
  it("prints a day as month and day", () => {
    expect(shortDay("2026-09-01")).toBe("Sep 1");
    expect(shortDay("2026-12-25T10:00:00Z")).toBe("Dec 25");
    expect(shortDay("junk")).toBe("junk");
  });
  it("says how long ago in the unit a person would use", () => {
    const now = Date.parse("2026-09-05T12:00:00Z");
    expect(ago("2026-09-05T11:59:30Z", now)).toBe("just now");
    expect(ago("2026-09-05T11:35:00Z", now)).toBe("25 minutes ago");
    expect(ago("2026-09-05T10:00:00Z", now)).toBe("2 hours ago");
    expect(ago("2026-09-05T11:00:00Z", now)).toBe("1 hour ago");
    expect(ago("2026-08-22T12:00:00Z", now)).toBe("14 days ago");
    expect(ago("2026-09-04T11:00:00Z", now)).toBe("1 day ago");
    expect(ago("nope", now)).toBe("nope");
  });
});

describe("labelIndexes", () => {
  it("labels every day of a short window and thins a long one to first, last and evenly between", () => {
    expect(labelIndexes(7)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    const ninety = labelIndexes(90);
    expect(ninety[0]).toBe(0);
    expect(ninety[ninety.length - 1]).toBe(89);
    expect(ninety.length).toBeLessThanOrEqual(9);
    expect(ninety.length).toBeGreaterThanOrEqual(7);
    expect(labelIndexes(30)).toEqual([0, 5, 10, 15, 20, 25, 29]);
    expect(labelIndexes(0)).toEqual([]);
  });
});

describe("pageName", () => {
  it("names the pages that render and nothing for redirects or a session open", () => {
    expect(pageName("/")).toBe("memory");
    expect(pageName("")).toBe("memory");
    expect(pageName("/corpus")).toBe("sessions");
    expect(pageName("/team")).toBe("team");
    expect(pageName("/admin")).toBe("admin");
    expect(pageName("/today/2026-09-01")).toBe("today");
    expect(pageName("/improve")).toBe("improve");
    expect(pageName("/session/abc")).toBe("recall");
    expect(pageName("/corpus/session/abc")).toBe("");
    expect(pageName("/entities")).toBe("");
    expect(pageName("/facts")).toBe("");
    expect(pageName("/operator")).toBe("");
    expect(pageName("/nonsense")).toBe("");
  });
});

describe("Analytics copy and series", () => {
  it("offers the three windows the server accepts", () => {
    expect(WINDOWS.map((w) => w.weeks)).toEqual([4, 12, 26]);
  });
  it("defines every metric in one plain sentence with no dashes", () => {
    for (const [key, text] of Object.entries(DEFS)) {
      expect(text, key).toMatch(/^[A-Z].*\.$/);
      expect(text, key).not.toMatch(/[–—]/);
    }
  });
  it("never sells CSA's own bill as the team's, nor a lower bound as the rate", () => {
    expect(DEFS.spend).toMatch(/Not your team's Claude Code bill/);
    expect(DEFS.acted).toMatch(/real share is higher/);
    expect(DEFS.transfer).toMatch(/its author's own sessions/);
  });
  it("labels weeks by their first day and totals each stack", () => {
    expect(weekLabels(["2026-08-09", "2026-08-16"])).toEqual(["Aug 9", "Aug 16"]);
    expect(stackTotals([[1, 2, 3], [], [0]])).toEqual([6, 0, 0]);
  });
});
