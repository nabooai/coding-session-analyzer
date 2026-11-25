import { describe, expect, it } from "vitest";
import { themeAttr } from "./theme";

describe("themeAttr", () => {
  it("stamps light/dark and lets system fall through to the OS", () => {
    expect(themeAttr("light")).toBe("light");
    expect(themeAttr("dark")).toBe("dark");
    expect(themeAttr("system")).toBeNull(); // no attribute -> prefers-color-scheme decides
  });
});
