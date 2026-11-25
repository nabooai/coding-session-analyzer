import { describe, expect, it } from "vitest";
import { ROUTE_ALIASES, bareArrivalHash, corpusSessionRoute, frontDoor, sessionHref, signedOutView, ssoHref } from "./routes";

describe("signedOutView", () => {
  // boot() normalises a bare arrival to #/welcome (every sign-in lands there), so a RELOAD of the
  // landing carries that hash and must still render the landing, not the login box. An explicit #/
  // (Memory) lands there too.
  it("keeps the landing on a reload of #/ and on #/welcome", () => {
    expect(signedOutView("#/")).toBe("landing");
    expect(signedOutView("#/welcome")).toBe("landing");
  });
  it("shows the landing for a bare arrival", () => {
    expect(signedOutView("")).toBe("landing");
    expect(signedOutView("#")).toBe("landing");
  });
  it("sends a deep link straight to the login (the visitor has a destination)", () => {
    expect(signedOutView("#/team")).toBe("login");
    expect(signedOutView("#/corpus/session/abc")).toBe("login");
  });
  it("sends the retired operator paths to the login like any other deep link", () => {
    // There is no page that renders its own credential prompt any more — the console is gone and with
    // it the only browser-held credential that reached more than one workspace.
    expect(signedOutView("#/operator")).toBe("login");
    expect(signedOutView("#/operator/acme")).toBe("login");
  });
});

describe("frontDoor", () => {
  // Where the server takes workspace requests the landing is the front door; everywhere else (a
  // self-host or on-prem box) it downgrades to the sign-in, so deploying the landing never replaces
  // a team's sign-in.
  it("shows the landing only on a server that takes workspace requests", () => {
    expect(frontDoor("#/", true)).toBe("landing");
    expect(frontDoor("", true)).toBe("landing");
    expect(frontDoor("#/", false)).toBe("login"); // no request webhook: keep the sign-in
    expect(frontDoor("", false)).toBe("login");
  });
  it("passes a deep link to the login regardless", () => {
    expect(frontDoor("#/team", true)).toBe("login");
    expect(frontDoor("#/team", false)).toBe("login");
  });
  it("sends the retired operator paths to the login regardless", () => {
    expect(frontDoor("#/operator", true)).toBe("login");
    expect(frontDoor("#/operator", false)).toBe("login");
  });
});

describe("sessionHref", () => {
  it("builds the corpus-session route for a real id", () => {
    expect(sessionHref("1bc24cb3-cc87")).toBe("#/corpus/session/1bc24cb3-cc87");
  });
  it("encodes ids that carry / or :", () => {
    expect(sessionHref("agent/7:2")).toBe("#/corpus/session/agent%2F7%3A2");
  });
  it("carries the fact ref as a query so the session view can focus that claim", () => {
    expect(sessionHref("abc", "hc:1/2")).toBe("#/corpus/session/abc?fact=hc%3A1%2F2");
    expect(sessionHref("abc", "")).toBe("#/corpus/session/abc");
    expect(sessionHref("", "hc:1")).toBe("");
  });
  // The regression: an empty id used to build "#/corpus/session/", which matches no route — the click
  // fell through to the normalise-to-#/ branch, so the URL snapped back and the page re-rendered.
  it("returns no href at all when there is no id (nothing to link to)", () => {
    expect(sessionHref("")).toBe("");
    expect(sessionHref("   ")).toBe("");
    expect(sessionHref(null)).toBe("");
    expect(sessionHref(undefined)).toBe("");
  });
});

describe("corpusSessionRoute", () => {
  it("returns the decoded id", () => {
    expect(corpusSessionRoute("/corpus/session/agent%2F7")).toBe("agent/7");
  });
  // "" means "this IS the session route, with no id" — an honest not-found, distinct from null, which
  // means "some other route". Collapsing the two is what made the dead link invisible.
  it("distinguishes an id-less session route from another route", () => {
    expect(corpusSessionRoute("/corpus/session/")).toBe("");
    expect(corpusSessionRoute("/corpus/session")).toBe("");
    expect(corpusSessionRoute("/corpus")).toBeNull();
    expect(corpusSessionRoute("/")).toBeNull();
    expect(corpusSessionRoute("/session/abc")).toBeNull();
  });
  it("survives a malformed escape instead of throwing out of the router", () => {
    expect(corpusSessionRoute("/corpus/session/%E0%A4%A")).toBe("%E0%A4%A");
  });
});

describe("bareArrivalHash", () => {
  it("sends a bare arrival to the search-first home", () => {
    expect(bareArrivalHash("")).toBe("#/");
    expect(bareArrivalHash("#")).toBe("#/");
  });
  it("leaves an explicit #/ alone (the Memory nav item)", () => {
    expect(bareArrivalHash("#/")).toBe("#/");
  });
  it("leaves any other hash alone", () => {
    expect(bareArrivalHash("#/team")).toBe("#/team");
  });
});

describe("ssoHref", () => {
  it("always returns to search, regardless of the hash the visitor arrived on", () => {
    expect(ssoHref("/api/sso/start")).toBe("/api/sso/start?return=%2F%23%2F");
  });
});

describe("ROUTE_ALIASES", () => {
  it("sends the admin paths people guess to the page that serves them", () => {
    expect(ROUTE_ALIASES["/admin/members"]).toBe("/team");
    expect(ROUTE_ALIASES["/admin/analytics"]).toBe("/admin");
    expect(ROUTE_ALIASES["/admin/taxonomy"]).toBe("/admin/entity-types");
  });
  it("never points at another alias, so one redirect lands", () => {
    for (const to of Object.values(ROUTE_ALIASES)) expect(ROUTE_ALIASES[to]).toBeUndefined();
  });
});
