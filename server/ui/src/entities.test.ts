import { describe, expect, it } from "vitest";
import { splitServed } from "./entities";
import type { CorpusEntity } from "./api";

const ent = (id: string, served?: 0 | 1): CorpusEntity => ({
  id, type: "file", name: id, attributes: null, label: null,
  ...(served === undefined ? {} : { served }),
});

describe("splitServed", () => {
  it("folds served=0 into oneOff, keeps served=1 in the main list", () => {
    const { served, oneOff } = splitServed([ent("a", 1), ent("b", 0), ent("c", 1)]);
    expect(served.map((e) => e.id)).toEqual(["a", "c"]);
    expect(oneOff.map((e) => e.id)).toEqual(["b"]);
  });

  it("treats an ABSENT served flag as served (older server / CSA_ENTITY_FLOOR=off renders as before)", () => {
    const { served, oneOff } = splitServed([ent("a"), ent("b")]);
    expect(served).toHaveLength(2);
    expect(oneOff).toHaveLength(0);
  });

  it("preserves server order within each half and handles the all-gated session", () => {
    const { served, oneOff } = splitServed([ent("z", 0), ent("a", 0)]);
    expect(served).toHaveLength(0);
    expect(oneOff.map((e) => e.id)).toEqual(["z", "a"]);
  });
});
