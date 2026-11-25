// Pure logic and copy for #/connect, kept out of main.ts so vitest can pin it: the asset table, the
// checksums.txt parser, and every string the page says. The page's job is to get a machine shipping,
// so the copy has to stay true about what setup does and when anything actually appears.
import type { Machine } from "./api";

/** The five builds CI publishes, in the order the page lists them. `os` groups the notes below. */
export const ASSETS: { name: string; label: string; os: "darwin" | "linux" | "windows" }[] = [
  { name: "csa-client-darwin-arm64", label: "macOS · Apple silicon", os: "darwin" },
  { name: "csa-client-darwin-amd64", label: "macOS · Intel", os: "darwin" },
  { name: "csa-client-linux-amd64", label: "Linux · x86-64", os: "linux" },
  { name: "csa-client-linux-arm64", label: "Linux · arm64", os: "linux" },
  { name: "csa-client-windows-amd64.exe", label: "Windows · x86-64", os: "windows" },
];

/** checksums.txt ("<sha256>  <name>", the sha256sum format install.sh reads) -> name to hash.
 *  A "*name" second field is the binary-mode marker sha256sum writes; strip it so the page and the
 *  installer agree on the same line. Anything that is not a 64-hex-digit pair is skipped rather than
 *  rendered, so a truncated or HTML error body shows as "not published", never as a fake hash. */
export function parseChecksums(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of (text || "").split("\n")) {
    const m = line.trim().match(/^([0-9a-f]{64})\s+\*?(\S+)$/i);
    if (m) out[m[2]] = m[1].toLowerCase();
  }
  return out;
}

/** What a machine row reads as. `created` is when setup finished; `last_used_at` is throttled to
 *  hourly server-side (orgs._touch), so a machine that just connected legitimately shows no contact
 *  yet and the copy must not read as a failure. */
export function machineLine(m: Machine, fmtDate: (iso: string) => string): string {
  const seen = m.last_used_at ? `last contact ${fmtDate(m.last_used_at)}` : "no contact yet";
  return `connected ${m.created ? fmtDate(m.created) : "recently"} · ${seen}`;
}

export const CONNECT = {
  nav: "Connect a machine",
  title: "Connect a machine",
  explain: "This browser session reads the memory. It ships nothing. Connect the machine you code on.",

  installHead: "Paste this in a terminal",
  installNote: "It shows what it would collect and waits for your yes. Then it prints a two word code and opens this browser: type the code, click Authorize.",
  installedAlready: "Already installed? Run",
  copy: "Copy",
  copied: "Copied",
  noBase: "This server has not been told its own public URL, so it cannot hand out an installer. The operator sets CSA_PUBLIC_URL.",

  machinesHead: "Your machines",
  machinesNone: "No machine connected yet. Run the line above.",
  machinesFoot: "Sessions ship when they end. Nothing reaches the memory until your next Claude Code session finishes.",
  machinesFailed: "Could not read your machines just now.",
  adminNote: "Admins: Who is silent on Admin lists machines that hold a credential but have not reached this server in 7 days. A plugin in manual mode sends only what its owner chooses, so it can look silent.",

  collectHead: "What it sends",
  collect: [
    "Full transcripts of your Claude Code sessions, from ~/.claude/projects. Teammates see the facts and quotes drawn from them, under your name. You and workspace admins can read a transcript the workspace keeps.",
    "Secrets are stripped on your machine first. Prompts, code, file paths, emails and names are kept on purpose, because that is what makes recall work.",
    "Every project folder by default. Narrow it with csa-client only pick. Existing sessions stay put unless you pass --backfill.",
    "csa-client uninstall removes the hooks and the binary and revokes this machine's credential. Add --purge to wipe the local config.",
  ],
  collectFoot: "setup shows you this same notice in your terminal and waits for your yes before anything ships.",

  downloadHead: "Download instead",
  downloadNote: "Five builds, each with its SHA-256.",
  trust: "These hashes are served by this same server, so they catch a corrupted download, not a tampered one. Whoever can hand you the binary can hand you the hash.",
  checksums: "checksums.txt",
  noChecksums: "This server publishes no checksums.txt, so nothing here can be verified against it.",
  notPublished: "not published",
  macHead: "After downloading on macOS",
  macNote: "Line 2 applies to a browser download only, which macOS quarantines. A curl download is not quarantined and that line will say there is no such xattr, which is fine.",
  unsigned: "The binary is not signed by Apple.",
  mismatch: "If the hash does not match the row above, stop. Do not run it.",
  versionCheck: "Check what you installed:",
  winHead: "Windows",
  winNote: "The installer covers macOS and Linux only. On Windows, download the .exe and run setup yourself. SmartScreen warns on it, because the binary is unsigned.",

  since: "On this server since",
} as const;

/** The macOS recipe for a browser download, in order. Verification comes FIRST: `install.sh` checks
 *  the sha256 and dies on a mismatch, so the hand path must not be the only one that skips it. */
export function macSteps(asset: string, base: string): string[] {
  return [`cd ~/Downloads`, `shasum -a 256 ${asset}`, `chmod +x ${asset}`,
          `xattr -d com.apple.quarantine ${asset}`, `./${asset} setup --server ${base}`];
}

export const installLine = (base: string): string => `curl -sSL ${base}/install | sh`;
export const setupLine = (base: string): string => `csa-client setup --server ${base}`;
export const winSetupLine = (base: string): string => `csa-client.exe setup --server ${base}`;
export const versionLine = (asset: string): string => `./${asset} version`;

/** What the admin pastes into Slack. It carries everything they would otherwise have to gather from
 *  four other screens: the link, when it dies, the install line, and the fact that the terminal will
 *  print a code the browser then asks for. An admin who has not walked the invitee's side themselves
 *  cannot know about that step, and it is where the invitee stops and asks. */
export function invitePaste(link: string, base: string, org: string, expires: string): string {
  return [
    `Join ${org || "CSA"}: ${link}`,
    expires ? `The link is yours alone and stops working on ${expires}.` : "",
    `Then connect the machine you code on: ${installLine(base)}`,
    "It prints a two word code and opens a page. Type the code there, then Authorize.",
  ].filter(Boolean).join("\n");
}

/** What stands between a fresh workspace and its first recall. */
export interface FirstRun {
  model: boolean;     // something can read a session: a provider on AI Models or the server's own credential
  machine: boolean;   // the admin has a connected machine
  teammate: boolean;  // someone else holds a live sign-in
}

export const FIRST_RUN = {
  title: "Three steps to your first recall",
  explain: "A teammate's decision, answered in your next Claude Code session.",
  foot: "Then finish a Claude Code session on one machine. A minute later, ask Claude on the other what was decided.",
} as const;

/** The admin's welcome checklist, in the order the steps have to happen. */
export function firstRunSteps(s: FirstRun): { title: string; text: string; href: string; done: boolean }[] {
  return [
    s.model
      ? { title: "AI model", text: "Ready. Change it on AI Models.", href: "#/admin/models", done: true }
      : { title: "Add an AI model", text: "CSA reads each finished session with it. Paste a key on AI Models.", href: "#/admin/models", done: false },
    s.machine
      ? { title: "This machine", text: "Connected. Your sessions ship when they end.", href: "#/connect", done: true }
      : { title: "Connect this machine", text: "Paste this in a terminal. It asks before anything ships.", href: "#/connect", done: false },
    s.teammate
      ? { title: "Teammates", text: "Someone joined. Their decisions land here as their sessions end.", href: "#/team", done: true }
      : { title: "Invite a teammate", text: "Team makes a sign-in link for one person. They open it, then paste the same line.", href: "#/team", done: false },
  ];
}

/** Can anything read a session? `line` is the AI Models page's `default`, which starts "NONE" when nothing can. */
export const modelReady = (providers: number, line: string): boolean => providers > 0 || !line.startsWith("NONE");
