// Pure logic for the Team page, kept out of main.ts so vitest can pin it: the per-person rollup
// (GET /api/team) merged with the workspace's live tokens (GET /api/org/members, admins only) into
// one row per person, busiest first, with the machines an admin may revoke.
import type { InviteLink, OrgMember, TeamRow } from "./api";

export interface Machine {
  hash: string; // the revoke handle
  hostname: string;
  lastUsed: string | null;
  role: "admin" | "member";
  service: boolean;
}

export interface TeamPerson {
  user: string;
  sessions: number;
  facts: number;
  entities: number;
  first_seen: string | null;
  last_active: string | null;
  role: "admin" | "member" | "service" | null; // null = no live token (admin view), or unknown (member view)
  machines: Machine[]; // live tokens only; empty for a member's view
}

/** How a token's hostname reads on the page: the two browser sign-ins get words, a machine keeps its name. */
export function machineLabel(hostname: string): string {
  if (hostname === "web") return "browser, invite sign-in";
  if (hostname === "web-sso") return "browser, SSO";
  return hostname || "unnamed machine";
}

/** One row per person: the rollup's order, then anyone holding a live token who has shipped nothing
 *  yet (zeros). A person's role is admin when any live token of theirs is. Revoked and expired tokens
 *  are not machines. */
export function teamPeople(rollup: TeamRow[], members: OrgMember[] | null): TeamPerson[] {
  const byEmail = new Map<string, Machine[]>();
  for (const m of members ?? []) {
    if (!m.active || !m.email) continue;
    const key = m.email.toLowerCase();
    const list = byEmail.get(key) ?? [];
    list.push({ hash: m.token_hash, hostname: m.hostname ?? "", lastUsed: m.last_used_at, role: m.role, service: m.service });
    byEmail.set(key, list);
  }
  const roleOf = (machines: Machine[]): TeamPerson["role"] => {
    const people = machines.filter((x) => !x.service);
    if (people.length === 0) return machines.length ? "service" : null;
    return people.some((x) => x.role === "admin") ? "admin" : "member";
  };
  const out: TeamPerson[] = rollup.map(({ agent, ...r }) => {
    const machines = byEmail.get(r.user.toLowerCase()) ?? [];
    byEmail.delete(r.user.toLowerCase());
    return { ...r, role: agent ? "service" : roleOf(machines), machines };
  });
  for (const [email, machines] of byEmail)
    out.push({ user: email, sessions: 0, facts: 0, entities: 0, first_seen: null, last_active: null, role: roleOf(machines), machines });
  return out;
}

/** People and agents on the page, counted apart: humans, then agents (service tokens) that shipped. */
export function teamCounts(people: TeamPerson[]): { people: number; agents: number } {
  const named = people.filter((p) => p.user);
  const agents = named.filter((p) => p.role === "service" && p.sessions > 0).length;   // an agent that shipped nothing is no one
  return { people: named.filter((p) => p.role !== "service").length, agents };
}

/** "4 people · 1 agent", and for an admin the split Analytics' Members card counts: "4 people · 3 with a
 *  CSA credential · 1 shipped without one · 1 agent". */
export function teamCountLine(people: TeamPerson[], admin: boolean): string {
  const n = teamCounts(people);
  const fmt = (x: number) => x.toLocaleString("en-US");
  const parts = [`${fmt(n.people)} ${n.people === 1 ? "person" : "people"}`];
  if (admin) {
    const humans = people.filter((p) => p.user && p.role !== "service");
    const holding = humans.filter((p) => p.role === "admin" || p.role === "member").length;
    const without = humans.filter((p) => p.role !== "admin" && p.role !== "member" && p.sessions > 0).length;
    parts.push(`${fmt(holding)} with a CSA credential`);
    if (without) parts.push(`${fmt(without)} shipped without one`);
  }
  if (n.agents) parts.push(`${fmt(n.agents)} ${n.agents === 1 ? "agent" : "agents"}`);
  return parts.join(" · ");
}


export const INVITE = {
  open: "Invite",
  head: "Invite a teammate",
  emailLabel: "Their work email",
  expiryLabel: "Expires in",
  submit: "Create link",
  sendThis: "Send them this",
  once: "Shown once. Copy it now, it cannot be shown again.",
  // Two different truths, and the wrong one is dangerous. With SSO, the identity provider decides who
  // the person is and the link really does admit only them. Without it, the link IS the credential: whoever
  // opens it signs in as that address. Saying "only {email}" on a server with no SSO invites an
  // admin to paste it in a channel.
  worksVerified: "Only {email} can use it, and only until {date}.",
  worksBearer: "Anyone who opens this link signs in as {email}, until {date}. Send it to them privately.",
  discardWarn: "You have not copied the link. It cannot be shown again.",
  pendingHead: "Waiting to join",
  pendingNone: "Nobody is waiting to join.",
  newLink: "Replace link",
  revoke: "Revoke",
  noBase: "This server has not been told its own public URL, so it cannot make a link. The operator sets CSA_PUBLIC_URL.",
  cap: "At most {n} links can be waiting at once.",
} as const;

/** How one waiting row reads. `used_at` is stamped on a completed SIGN-IN, not on opening the page,
 *  so it cannot honestly be called "opened": a person who clicked and backed out looks the same as
 *  one who never clicked. This list holds only people who have not joined; once they have, they are
 *  in the members table below and the row is gone. */
export function inviteLine(inv: InviteLink, fmtDate: (iso: string) => string): string {
  const by = inv.created_by ? ` · invited by ${inv.created_by}` : "";
  return `expires ${inv.expires_at ? fmtDate(inv.expires_at) : "soon"}${by}`;
}

/** Only the people who have not joined. A row that stayed under "Waiting to join" after the person
 *  was already listed as a member read as a second, unfinished thing. */
export function waiting(list: InviteLink[]): InviteLink[] {
  return list.filter((i) => !i.used_at);
}

/** Non-empty when the address cannot be sent to the server. Mirrors orgs.create_invite's own limits,
 *  so the form refuses what the route would refuse. */
export function inviteError(email: string): string {
  const e = email.trim();
  if (!e) return "Enter their email address.";
  if (!e.includes("@")) return "That is not an email address.";
  if (e.length > 254) return "That email address is too long.";
  return "";
}

export const SERVICE = {
  open: "+ Create token",
  head: "A token for a machine",
  nameLabel: "Machine name",
  emailLabel: "It ships and asks as",
  submit: "Create token",
  once: "Shown once. Copy it now, it cannot be shown again.",
  discardWarn: "You have not copied the token. It cannot be shown again.",
  works: "{name} ships and asks as {email}, always as a member. Find it in the Service Tokens table to revoke it.",
} as const;

/** Non-empty when the form cannot be sent. Mirrors orgs.mint_service_token's own checks. */
export function serviceTokenError(name: string, email: string): string {
  const n = name.trim(), e = email.trim();
  if (!n) return "Name the machine, for example atlas.";
  if (["web", "web-sso"].includes(n.toLowerCase()) || n.length > 80) return "That is not a machine name.";
  return inviteError(e);
}

/** What the admin pastes into the machine: the MCP entry for recall, the header for shipping. */
export function servicePaste(token: string, base: string): string {
  return [
    `Token: ${token}`,
    "",
    `Recall (MCP, http): ${base}/mcp`,
    `Ship:   POST ${base}/api/ingest`,
    `Header: Authorization: Bearer ${token}`,
  ].join("\n");
}
