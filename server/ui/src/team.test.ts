import { describe, expect, it } from "vitest";
import type { OrgMember, TeamRow } from "./api";
import { INVITE, SERVICE, inviteError, inviteLine, machineLabel, servicePaste, serviceTokenError, teamCountLine, teamCounts, teamPeople, waiting } from "./team";
import { membersLine } from "./admin";

const rollup: TeamRow[] = [
  { user: "mara@demo.test", sessions: 5, facts: 20, entities: 9, first_seen: "2026-08-20", last_active: "2026-09-01", agent: false },
  { user: "jules@demo.test", sessions: 4, facts: 12, entities: 6, first_seen: "2026-08-21", last_active: "2026-08-30", agent: false },
];

function tok(email: string, hostname: string, role: "admin" | "member" = "member", active = true, service = false): OrgMember {
  return { token_hash: `${email}:${hostname}`, email, hostname, created: "2026-08-20", last_used_at: "2026-09-01",
    revoked_at: active ? null : "2026-08-25", role, active, service };
}

describe("teamPeople", () => {
  it("keeps the rollup order and attaches each person's live machines", () => {
    const people = teamPeople(rollup, [tok("mara@demo.test", "mara-mac", "admin"), tok("Mara@demo.test", "web-sso", "admin"),
      tok("jules@demo.test", "jules-laptop"), tok("jules@demo.test", "old-box", "member", false)]);
    expect(people.map((p) => p.user)).toEqual(["mara@demo.test", "jules@demo.test"]);
    expect(people[0].role).toBe("admin");
    expect(people[0].machines.map((m) => m.hostname)).toEqual(["mara-mac", "web-sso"]);
    expect(people[1].role).toBe("member");
    expect(people[1].machines.map((m) => m.hostname)).toEqual(["jules-laptop"]);
  });
  it("a service token never decides a role: alone it reads as service, beside a person it is ignored", () => {
    const people = teamPeople(rollup, [tok("atlas@demo.test", "atlas", "member", true, true),
      tok("jules@demo.test", "jules-bot", "member", true, true), tok("jules@demo.test", "jules-laptop", "admin")]);
    expect(people.find((p) => p.user === "atlas@demo.test")?.role).toBe("service");
    expect(people.find((p) => p.user === "jules@demo.test")?.role).toBe("admin");
  });
  it("adds a token holder who has shipped nothing, with zeros", () => {
    const people = teamPeople(rollup, [tok("tomasz@demo.test", "tomasz-pc")]);
    expect(people[2]).toMatchObject({ user: "tomasz@demo.test", sessions: 0, facts: 0, role: "member" });
    expect(people[2].machines[0].hash).toBe("tomasz@demo.test:tomasz-pc");
  });
  it("counts its rows the way Analytics' Members card does", () => {
    const shipped: TeamRow[] = [...rollup,
      { user: "gone@demo.test", sessions: 2, facts: 1, entities: 0, first_seen: "2026-08-01", last_active: "2026-08-02", agent: false },
      { user: "", sessions: 1, facts: 0, entities: 0, first_seen: "2026-08-01", last_active: "2026-08-01", agent: false }];
    const people = teamPeople(shipped, [tok("mara@demo.test", "mara-mac", "admin"), tok("jules@demo.test", "jules-laptop"),
      tok("tomasz@demo.test", "web-sso"), tok("gone@demo.test", "old-box", "member", false)]);
    expect(teamCountLine(people, true)).toBe("4 people · 3 with a CSA credential · 1 shipped without one");
    expect(membersLine({ members: 3, shipped_without_credential: 1 })).toBe("3 people with a CSA credential. Team also lists 1 who shipped without one");
    expect(teamCountLine(people, false)).toBe("4 people");
    const withAgent = teamPeople(shipped, [tok("mara@demo.test", "mara-mac", "admin"), tok("jules@demo.test", "jules-laptop"),
      tok("tomasz@demo.test", "web-sso"), tok("bot@demo.test", "ci", "member", true, true)]);
    expect(teamCountLine(withAgent, true)).toBe("4 people · 3 with a CSA credential · 1 shipped without one");
  });
  it("counts an agent that shipped apart from people, in an admin's view and a member's", () => {
    const shipped: TeamRow[] = [...rollup,
      { user: "atlas@demo.test", sessions: 3, facts: 2, entities: 1, first_seen: "2026-08-01", last_active: "2026-08-02", agent: true }];
    const admin = teamPeople(shipped, [tok("mara@demo.test", "mara-mac", "admin"), tok("jules@demo.test", "jules-laptop"),
      tok("atlas@demo.test", "atlas", "member", true, true)]);
    expect(teamCounts(admin)).toEqual({ people: 2, agents: 1 });
    expect(teamCountLine(admin, true)).toBe("2 people · 2 with a CSA credential · 1 agent");
    expect(teamCounts(teamPeople(shipped, null))).toEqual({ people: 2, agents: 1 });
    expect(teamCountLine(teamPeople(shipped, null), false)).toBe("2 people · 1 agent");
  });
  it("a member's view (no members payload) carries no role and no machines", () => {
    const people = teamPeople(rollup, null);
    expect(people).toHaveLength(2);
    expect(people.every((p) => p.role === null && p.machines.length === 0)).toBe(true);
  });
  it("a person whose only token is revoked has no live machine and no role", () => {
    const people = teamPeople(rollup, [tok("jules@demo.test", "jules-laptop", "admin", false)]);
    expect(people[1].role).toBeNull();
    expect(people[1].machines).toEqual([]);
  });
});

describe("machineLabel", () => {
  it("names the two browser sign-ins and keeps machine names", () => {
    expect(machineLabel("web")).toBe("browser, invite sign-in");
    expect(machineLabel("web-sso")).toBe("browser, SSO");
    expect(machineLabel("mara-mac")).toBe("mara-mac");
    expect(machineLabel("")).toBe("unnamed machine");
  });
});

describe("invite links", () => {
  const d = (iso: string) => iso.slice(0, 10);
  const base = { id: "a1", email: "dave@partner.io", created_by: "boss@acme.test",
                 created_at: "2026-09-01T00:00:00Z", expires_at: "2026-09-08T00:00:00Z" };

  it("lists only the people who have not joined", () => {
    // used_at is stamped on a completed SIGN-IN, not on opening the page, so it cannot honestly be
    // called "opened": someone who clicked and backed out looks the same as someone who never
    // clicked. A row that stayed under the heading after the person was already a member below read
    // as a second, unfinished thing.
    const joined = { ...base, id: "a2", used_at: "2026-09-03T00:00:00Z" };
    expect(waiting([{ ...base, used_at: null }, joined]).map((i) => i.id)).toEqual(["a1"]);
    expect(inviteLine({ ...base, used_at: null }, d))
      .toBe("expires 2026-09-08 · invited by boss@acme.test");
    for (const s of Object.values(INVITE)) expect(s, s).not.toMatch(/opened/);
  });

  it("never claims a link admits only one person on a server that cannot verify anyone", () => {
    // Without SSO the link IS the credential: whoever opens it signs in as that address. Telling an
    // admin "works for dave only" there invites them to paste it in a channel.
    expect(INVITE.worksVerified).toMatch(/Only \{email\} can use it/);
    expect(INVITE.worksBearer).toMatch(/Anyone who opens this link/);
    expect(INVITE.worksBearer).toMatch(/privately/);
  });

  it("refuses what the route would refuse, before sending it", () => {
    expect(inviteError("")).toBeTruthy();
    expect(inviteError("   ")).toBeTruthy();
    expect(inviteError("not-an-email")).toBeTruthy();
    expect(inviteError("a@" + "x".repeat(300))).toBeTruthy();
    expect(inviteError(" dave@partner.io ")).toBe("");
  });

  it("never sends an admin to the server's shell or the repo", () => {
    // The failure this change exists to remove: the old panel told an admin to run
    // `csa.py org invite <org>` on the box, and there was no other way to get an invite.
    for (const s of Object.values(INVITE)) {
      expect(s, s).not.toMatch(/csa\.py|on the server|docs\/|README/);
      expect(s, s).not.toMatch(/[—–]/);
    }
  });

  it("says the link is shown once, because it is", () => {
    // Only the hash is stored (orgs.create_invite), so a dialog closed before Copy costs a revoke
    // and a re-issue. The panel has to say so before, not after.
    expect(INVITE.once).toMatch(/cannot be shown again/);
    expect(INVITE.discardWarn).toMatch(/not copied/);
  });
});

describe("service token form", () => {
  it("refuses what the server would refuse", () => {
    expect(serviceTokenError("", "atlas@acme.com")).toMatch(/Name the machine/);
    expect(serviceTokenError("Web-SSO", "atlas@acme.com")).toBe("That is not a machine name.");
    expect(serviceTokenError("atlas", "atlas")).toBe("That is not an email address.");
    expect(serviceTokenError("atlas", "atlas@acme.com")).toBe("");
  });
  it("pastes the token with both endpoints", () => {
    const p = servicePaste("csa_x", "https://csa.acme.com");
    expect(p).toContain("https://csa.acme.com/mcp");
    expect(p).toContain("POST https://csa.acme.com/api/ingest");
    expect(p).toContain("Authorization: Bearer csa_x");
    expect(p).not.toContain("localhost");
  });
  it("warns inline, in the token's own words", () => {
    expect(SERVICE.discardWarn).toMatch(/token/);
    expect(SERVICE.discardWarn).not.toMatch(/link/);
  });
});
