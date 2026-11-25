import { describe, expect, it } from "vitest";
import { ROUTE_ALIASES, bareArrivalHash } from "./routes";

describe("release search routes", () => {
  it("opens a bare signed-in arrival on search", () => {
    expect(bareArrivalHash("")).toBe("#/");
    expect(bareArrivalHash("#")).toBe("#/");
  });

  it("redirects retired product tabs to search or settings", () => {
    expect(ROUTE_ALIASES["/sessions"]).toBe("/");
    expect(ROUTE_ALIASES["/entities"]).toBe("/admin/entity-types");
    expect(ROUTE_ALIASES["/analytics"]).toBe("/admin");
    expect(ROUTE_ALIASES["/settings"]).toBe("/admin/models");
  });
});
