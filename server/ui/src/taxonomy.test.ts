import { describe, expect, it } from "vitest";
import { initialOf, teamCard } from "./taxonomy";

describe("teamCard", () => {
  it("hosted org with a control-plane name: name + org id as sub", () => {
    expect(teamCard("acme", "Acme Engineering")).toEqual({ name: "Acme Engineering", sub: "acme" });
  });
  it("hosted org without a name: the id stands in, no redundant sub", () => {
    expect(teamCard("acme", "")).toEqual({ name: "acme", sub: "" });
  });
  it("self-host default org: honest words, no invented plan tier", () => {
    expect(teamCard("default", "")).toEqual({ name: "Self-hosted", sub: "single tenant" });
    expect(teamCard("default", undefined)).toEqual({ name: "Self-hosted", sub: "single tenant" });
  });
  it("ADOPTED default org: a named workspace, never labelled self-hosted", () => {
    // The default corpus can be adopted as a real tenant without moving bytes, so a named workspace
    // whose id is still `default` must read as itself. It used to say "Self-hosted / single tenant"
    // to every person in it.
    expect(teamCard("default", "Acme")).toEqual({ name: "Acme", sub: "" });
  });
});

describe("initialOf", () => {
  it("prefers the display name", () => {
    expect(initialOf("mara okafor", "x@y.z")).toBe("M");
  });
  it("falls back to the email, then to ?", () => {
    expect(initialOf(null, "jules@example.com")).toBe("J");
    expect(initialOf(null, null)).toBe("?");
    expect(initialOf("  ", "")).toBe("?");
  });
});
