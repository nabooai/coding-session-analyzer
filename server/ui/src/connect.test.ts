import { describe, expect, it } from "vitest";
import { ASSETS, CONNECT, FIRST_RUN, firstRunSteps, installLine, invitePaste, macSteps, machineLine, modelReady, parseChecksums, setupLine, winSetupLine } from "./connect";

const BASE = "https://csa.example.com";

// The real prod body, byte for byte (two spaces, sha256sum format), so the parser is pinned against
// what /latest/checksums.txt actually serves rather than a hand-tidied sample.
const REAL = `cb2ad2d3d19197d4d5ede230863c31fcfc4df169fab9fae5b740a7ee26a7dc0e  csa-client-darwin-amd64
51af114db9fcf164d32549ee0894960350c7cea31045733fc7748d662973c859  csa-client-darwin-arm64
f8c4e4888f4ab97ecbfa29ea0b934c859daa94d5bdf0274710ad009d5a9f823e  csa-client-linux-amd64
71745550e29986008d0a804afcd964d03009d7993c2b65e7ddf5849fa48198aa  csa-client-linux-arm64
e27a1af7a217c8fb9ccf16d2cb0947d33f3d3cc5f2f6d34c1732cac488504e66  csa-client-windows-amd64.exe
`;

describe("parseChecksums", () => {
  it("reads every asset the page lists out of the published file", () => {
    const sums = parseChecksums(REAL);
    for (const a of ASSETS) expect(sums[a.name], a.name).toMatch(/^[0-9a-f]{64}$/);
    expect(sums["csa-client-darwin-arm64"]).toBe("51af114db9fcf164d32549ee0894960350c7cea31045733fc7748d662973c859");
  });
  it("accepts sha256sum's binary-mode asterisk", () => {
    expect(parseChecksums(`${"a".repeat(64)} *csa-client-linux-amd64`)["csa-client-linux-amd64"]).toBe("a".repeat(64));
  });
  it("renders nothing rather than a fake hash when the body is not a checksums file", () => {
    // A 404 JSON body or an HTML error page must leave every row on "not published", never on a
    // truncated string that reads like a hash a reviewer would then compare against.
    expect(parseChecksums('{"ok": false, "error": "not found"}')).toEqual({});
    expect(parseChecksums("<!doctype html><title>oops</title>")).toEqual({});
    expect(parseChecksums("deadbeef  csa-client-linux-amd64")).toEqual({});
    expect(parseChecksums("")).toEqual({});
  });
});

describe("the command lines carry this server's own base", () => {
  it("never hardcodes a host", () => {
    expect(installLine(BASE)).toBe(`curl -sSL ${BASE}/install | sh`);
    expect(setupLine(BASE)).toBe(`csa-client setup --server ${BASE}`);
    expect(winSetupLine(BASE)).toBe(`csa-client.exe setup --server ${BASE}`);
    for (const line of [installLine(BASE), setupLine(BASE), winSetupLine(BASE), ...macSteps("x", BASE)])
      expect(line).not.toMatch(/127\.0\.0\.1|localhost/);
  });
  it("verifies first, then clears the quarantine, then runs", () => {
    // install.sh verifies the sha256 and dies on a mismatch. The hand path is the one a reviewer
    // takes BECAUSE they will not run the script, so it must not be the only path with no check.
    // The quarantine matters because a browser download is quarantined and the binary is unsigned,
    // so Gatekeeper refuses the first run outright. Order is load-bearing: run comes last.
    const steps = macSteps("csa-client-darwin-arm64", BASE);
    expect(steps[0]).toBe("cd ~/Downloads");
    expect(steps[1]).toBe("shasum -a 256 csa-client-darwin-arm64");
    expect(steps[2]).toBe("chmod +x csa-client-darwin-arm64");
    expect(steps[3]).toBe("xattr -d com.apple.quarantine csa-client-darwin-arm64");
    expect(steps[4]).toContain("setup --server");
    expect(steps.findIndex((x) => x.startsWith("shasum")))
      .toBeLessThan(steps.findIndex((x) => x.includes("setup --server")));
  });
  it("pastes everything the admin would otherwise gather from four screens", () => {
    // Measured by a user sim: the link and the install line came from the panel, but the expiry,
    // the workspace name and the WORD-WORD step did not, and the last of those is where the
    // invitee stops and asks.
    const paste = invitePaste(`${BASE}/join/tok`, BASE, "Demo Co", "Sep 13, 2026");
    expect(paste).toContain(`${BASE}/join/tok`);
    expect(paste).toContain(installLine(BASE));
    expect(paste).toContain("Demo Co");
    expect(paste).toContain("Sep 13, 2026");
    expect(paste).toMatch(/two word code/);
    expect(invitePaste(`${BASE}/join/tok`, BASE, "", "")).not.toMatch(/stops working/);
  });
});

describe("machineLine", () => {
  const d = (iso: string) => iso.slice(0, 10);
  it("says a machine that just connected has not reported in, without reading as a failure", () => {
    // orgs._touch throttles last_used_at to hourly, so a machine that connected a minute ago
    // legitimately has none.
    expect(machineLine({ hostname: "mbp", created: "2026-09-05T10:00:00Z", last_used_at: null }, d))
      .toBe("connected 2026-09-05 · no contact yet");
  });
  it("shows the last contact when there is one", () => {
    expect(machineLine({ hostname: "mbp", created: "2026-09-05T10:00:00Z", last_used_at: "2026-09-06T09:00:00Z" }, d))
      .toBe("connected 2026-09-05 · last contact 2026-09-06");
  });
});

describe("what this page may never say", () => {
  // Every exported string, wherever it nests, so the audit never depends on remembering a new field
  // (the same rule landing.test.ts applies to the landing copy).
  const strings = (v: unknown): string[] =>
    typeof v === "string" ? [v] : Array.isArray(v) ? v.flatMap(strings)
      : v && typeof v === "object" ? Object.values(v).flatMap(strings) : [];
  const steps = [false, true].flatMap((b) => firstRunSteps({ model: b, machine: b, teammate: b }));
  const ALL = [...strings(CONNECT), ...strings(FIRST_RUN), ...strings(steps), ...ASSETS.map((a) => a.label)];

  it("carries no em or en dash", () => {
    for (const s of ALL) expect(s, s).not.toMatch(/[—–]/);
  });
  it("never sends a tenant to the repo or to the server's shell", () => {
    // The whole point of the page: an admin or a teammate must never be told to open a file in the
    // checkout or run csa.py on the box.
    for (const s of ALL) expect(s, s).not.toMatch(/csa\.py|docs\/|README|\.md\b/);
  });
  it("never offers hook install or init as a way to connect a machine", () => {
    // Only `setup` wires all three hooks; the other two wire recall only, which is how a machine
    // ends up silently not shipping.
    for (const s of ALL) expect(s, s).not.toMatch(/hook install|csa-client init/);
    // The step where a first-timer stops and asks: the terminal prints a code the browser then wants.
    expect(CONNECT.installNote).toMatch(/two word code/);
    expect(CONNECT.installNote).toMatch(/Authorize/);
  });
  it("tells the truth about when anything appears", () => {
    // Nothing reaches the memory until the next session ENDS: setup stamps sweep_from=now and the
    // ship is on SessionEnd. Copy that implies Team updates immediately is a lie.
    expect(CONNECT.machinesFoot).toMatch(/next Claude Code session finishes/);
  });
  it("admits the binary is unsigned on both download paths", () => {
    expect(CONNECT.unsigned).toMatch(/not signed by Apple/);
    expect(CONNECT.winNote).toMatch(/unsigned/);
  });
  it("does not claim the hashes prove anything they cannot", () => {
    // The page fetches checksums.txt from the same server that serves the binary, so the two are one
    // source. install.sh says this in its own comments, where only people who pipe it into a shell
    // read it; the reviewer who refuses to do that is exactly who needs it.
    expect(CONNECT.trust).toMatch(/corrupted download, not a tampered one/);
    expect(CONNECT.downloadNote).not.toMatch(/verify/i);
    expect(CONNECT.mismatch).toMatch(/stop/i);
  });
  it("says what the client sends before anyone downloads it", () => {
    // The authorize page carries the same notice but needs a nonce from `setup` to reach, so without
    // this a reviewer would have to run the binary to find out what running the binary does.
    const collect = CONNECT.collect.join(" ");
    expect(collect).toMatch(/[Ff]ull transcripts/);
    expect(collect).toMatch(/Teammates see the facts and quotes/);
    expect(collect).toMatch(/uninstall/);
    expect(CONNECT.collectFoot).toMatch(/waits for your yes/);
  });
  it("says on this server since, never published", () => {
    // checksums.txt's mtime is when the file landed on THIS box (gh release download / cp stamp it),
    // not an upstream publish date.
    expect(CONNECT.since).toBe("On this server since");
    for (const s of ALL) expect(s, s).not.toMatch(/published <|version \d/i);
  });
});

describe("the asset table", () => {
  it("lists the five builds CI publishes, and only those", () => {
    expect(ASSETS.map((a) => a.name).sort()).toEqual([
      "csa-client-darwin-amd64", "csa-client-darwin-arm64", "csa-client-linux-amd64",
      "csa-client-linux-arm64", "csa-client-windows-amd64.exe",
    ]);
  });
  it("leads with Apple silicon, which is what the mac steps are written against", () => {
    expect(ASSETS[0].name).toBe("csa-client-darwin-arm64");
  });
});

describe("the admin's first-run steps", () => {
  it("lists model, machine, teammate in that order, each with the page that does it", () => {
    const s = firstRunSteps({ model: false, machine: false, teammate: false });
    expect(s.map((x) => x.href)).toEqual(["#/admin/models", "#/connect", "#/team"]);
    expect(s.every((x) => !x.done)).toBe(true);
  });
  it("marks each step done on its own", () => {
    expect(firstRunSteps({ model: true, machine: false, teammate: false }).map((x) => x.done)).toEqual([true, false, false]);
    expect(firstRunSteps({ model: false, machine: true, teammate: false }).map((x) => x.done)).toEqual([false, true, false]);
    expect(firstRunSteps({ model: false, machine: false, teammate: true }).map((x) => x.done)).toEqual([false, false, true]);
  });
  it("counts the server's own credential as a model, and nothing else", () => {
    expect(modelReady(0, "NONE: register a provider on AI Models (or set ANTHROPIC_API_KEY); extraction and recall fail until then")).toBe(false);
    expect(modelReady(0, "Claude via an Anthropic API key from the environment (sonnet); nothing registered on AI Models")).toBe(true);
    expect(modelReady(1, "NONE: anything")).toBe(true);
  });
});
