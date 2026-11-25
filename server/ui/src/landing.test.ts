import { describe, expect, it } from "vitest";
import {
  BUDGETS,
  DEMO,
  DEMO_METRICS,
  DEMO_RULE,
  FORM_ERRORS,
  HERO,
  NAV,
  META,
  PILLARS,
  REQUEST,
  SIGNIN,
  STORED,
  SUCCESS,
  claims,
  sections,
  validRequest,
  words,
} from "./landing";

// Every exported string, wherever it nests, so the audit never depends on remembering a new field.
// `source` fields are pointers into the repo, not copy: they are audited by the traceability test.
function allStrings(v: unknown): string[] {
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return v.flatMap(allStrings);
  if (v && typeof v === "object") return Object.entries(v).filter(([k]) => k !== "source" && k !== "rowsSource").flatMap(([, x]) => allStrings(x));
  return [];
}
const COPY = allStrings([NAV, HERO, PILLARS, STORED, REQUEST, FORM_ERRORS, SUCCESS, SIGNIN]);
const DEMO_TEXT = allStrings([DEMO, DEMO_METRICS, DEMO_RULE]);
const EVERYTHING = [...COPY, ...DEMO_TEXT];

describe("word budgets", () => {
  const s = sections();
  for (const [id, budget] of Object.entries(BUDGETS)) {
    it(`${id} stays within ${budget} words`, () => {
      expect(s[id], id).toBeDefined();
      expect(words(s[id])).toBeLessThanOrEqual(budget);
    });
  }
  it("counts a token only when it carries a letter or digit", () => {
    expect(words(["a · b", "→", "3 of 9"])).toBe(5);
  });
});

describe("the sign-in", () => {
  it("says what CSA is before asking anyone to sign in", () => {
    expect(sections().signin[1]).toBe(META.description.text);
    expect(claims()).toContain(META.description);
  });
});

describe("claims are traceable", () => {
  it("every claim names a file or route in this repo", () => {
    for (const c of claims()) {
      expect(c.text.length, c.text).toBeGreaterThan(10);
      expect(c.source, c.text).toMatch(/(server\/|client\/|docs\/|\/api\/|csa\.py)/);
    }
  });
  it("every sentence that talks about storage, sending or providers is a registered claim", () => {
    const registered = claims().map((c) => c.text);
    const talks = /(stored|stores|kept|keeps|deleted|deletes|redact|Anthropic|Google|leaves|sent|provider)/i;
    for (const s of COPY) {
      if (!talks.test(s)) continue;
      // chrome words that mention the noun without asserting anything about it
      if (/^(What is stored|Request a workspace|Sign in|Stored|Redacted|Extracted|Recalled|Transcript|Full session text|Proof from the session|Whose machine sent it|Kept until deleted\.|Redacted before it leaves the laptop\.|The people who run the hosted server can read it\.|What else leaves\.|What is stored,|for how long, and who can read it\.|The whole session, redacted on the developer's machine before sending\.|Request sent\.)$/.test(s)) continue;
      expect(registered.some((r) => r.includes(s) || s.includes(r)), `unregistered claim: ${s}`).toBe(true);
    }
  });
  it("names the LLM provider and loads no third-party script", () => {
    const all = COPY.join(" ");
    expect(all).toMatch(/The AI provider you configure gets the redacted transcript/);
    expect(all).toMatch(/Extracted by the AI model you configure/);
    expect(all).not.toMatch(/Anthropic/);
    expect(all).toMatch(/identity provider/);
    expect(all).not.toMatch(/accounts\.google\.com|Google/);
  });
});

describe("what the page may never say", () => {
  // Each of these was falsified against the repo (the redesign research and
  // the PM audit of 2026-09-05): transcripts are kept, the hosted operator CAN read data, more than the
  // transcript leaves the server, and extraction is not a single call.
  const forbidden = [
    /transcripts? (are|is|get|gets) deleted/i,
    /deleted after/i,
    /don'?t access your data/i,
    /nothing else leaves/i,
    /once, for extraction/i,
    /Asia\/Jerusalem/,
    /(helm|encrypt|automatic backup|sparkline|subdomain)/i,
  ];
  it("no exported string makes a rejected claim", () => {
    for (const s of EVERYTHING) for (const f of forbidden) expect(s, s).not.toMatch(f);
  });
  it("no exported string carries an em or en dash", () => {
    for (const s of EVERYTHING) expect(s, s).not.toMatch(/[—–]/);
  });
  // The public page shows an invented team. Real people, customers and repos must never reach it, in
  // the copy OR the demo story: server/tests/test_no_private_names.py keeps them out of the whole tree,
  // and this pins what the page itself may name.
  const TEAM = new Set(["Mara Okafor", "Jules Renner", "Tomasz Wiek", "Priya Natarajan", "Sam Achterberg"]);
  it("names only the invented team and no real repo", () => {
    for (const s of DEMO.sessions) expect(TEAM.has(s.user), s.user).toBe(true);
    for (const s of EVERYTHING) expect(s, s).not.toMatch(/claude-session-analyzer|coding-session-analyzer|prefect/i);
  });
  it("ranks or compares no one", () => {
    const ranking = /\b(rank(ing|ed|s)?|leaderboard|scoreboard|top performer|most efficient|least efficient|who is (most|least))\b/i;
    for (const s of EVERYTHING) expect(s, s).not.toMatch(ranking);
    expect(DEMO_RULE.rule).toMatch(/\bof\b/); // a denominator, never a bare Nx
    for (const s of EVERYTHING) expect(s, s).not.toMatch(/\b\d+\s*x\b/i);
  });
});

describe("demo story", () => {
  it("is five sessions with three to twelve facts each and consistent recall edges", () => {
    expect(DEMO.sessions).toHaveLength(5);
    for (const s of DEMO.sessions) {
      expect(s.n_facts).toBeGreaterThanOrEqual(3);
      expect(s.n_facts).toBeLessThanOrEqual(12);
    }
    const refs = new Set(DEMO.facts.map((f) => f.ref));
    const ids = new Set(DEMO.sessions.map((s) => s.session_id));
    for (const r of DEMO.recalls) {
      expect(refs.has(r.ref), r.ref).toBe(true);
      expect(ids.has(r.session_id), r.session_id).toBe(true);
      expect(DEMO.facts.find((f) => f.ref === r.ref)!.session_id).not.toBe(r.session_id);
    }
    // the story: the first decision is recalled into three later sessions
    expect(DEMO.recalls.filter((r) => r.ref === "f1")).toHaveLength(3);
  });
  it("uses only the five fact types the extractor knows", () => {
    for (const f of DEMO.facts) expect(["decision", "gotcha", "constraint", "todo", "pref"]).toContain(f.type);
  });
});

describe("validRequest", () => {
  it("accepts a plain company + work email", () => {
    expect(validRequest("Acme Corp", "a@acme.com")).toBeNull();
  });
  it("requires a company after trimming", () => {
    expect(validRequest("", "a@acme.com")).toBe(FORM_ERRORS.company);
    expect(validRequest("   ", "a@acme.com")).toBe(FORM_ERRORS.company);
  });
  it("caps the company name at 120 chars", () => {
    expect(validRequest("x".repeat(120), "a@acme.com")).toBeNull();
    expect(validRequest("x".repeat(121), "a@acme.com")).toBe(FORM_ERRORS.companyLong);
  });
  it("requires an @ in the email", () => {
    expect(validRequest("Acme", "not-an-email")).toBe(FORM_ERRORS.email);
    expect(validRequest("Acme", "")).toBe(FORM_ERRORS.email);
  });
  it("caps the email at 254 chars", () => {
    expect(validRequest("Acme", "a".repeat(249) + "@x.io")).toBeNull(); // 254 exactly
    expect(validRequest("Acme", "a".repeat(250) + "@x.io")).toBe(FORM_ERRORS.emailLong); // 255
  });
  it("trims whitespace before judging either field", () => {
    expect(validRequest("  Acme  ", "  a@acme.com  ")).toBeNull();
  });
});
