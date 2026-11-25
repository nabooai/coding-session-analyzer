import { describe, expect, it } from "vitest";
import type { GraphData } from "./api";
import { clampView, fit, hit, LabelSpace, layout, mulberry32, seedOf, worldScale, zoomAbout } from "./graph";

function story(): GraphData {
  const sessions = ["s1", "s2", "s3", "s4", "s5"].map((id, i) => ({
    session_id: id, title: `Session ${i}`, project: "p", user: "u", started_at: "2024-06-1" + i, n_facts: 4,
  }));
  const types = ["decision", "gotcha", "constraint", "todo", "preference"];
  const facts = sessions.flatMap((s, i) =>
    [0, 1, 2, 3].map((k) => ({ ref: `${s.session_id}:f${k}`, type: types[(i + k) % 5], claim: "c", session_id: s.session_id })),
  );
  const recalls = [
    { ref: "s1:f0", session_id: "s3" },
    { ref: "s1:f0", session_id: "s4" },
    { ref: "s2:f1", session_id: "s5" },
    { ref: "s2:f1", session_id: "s2" }, // a fact recalled into its own session is not an edge
    { ref: "nope", session_id: "s5" }, // unknown ref: dropped, never thrown
  ];
  return { sessions, facts, recalls };
}

describe("seedOf / mulberry32", () => {
  it("is order-independent over session ids and stable", () => {
    expect(seedOf(["b", "a"])).toBe(seedOf(["a", "b"]));
    expect(seedOf(["a"])).not.toBe(seedOf(["b"]));
    const r1 = mulberry32(7), r2 = mulberry32(7);
    expect([r1(), r1(), r1()]).toEqual([r2(), r2(), r2()]);
  });
});

describe("layout", () => {
  it("is deterministic for the same data and size", () => {
    const a = layout(story(), 900, 500);
    const b = layout(story(), 900, 500);
    expect(a.nodes.map((n) => [n.id, Math.round(n.x), Math.round(n.y)])).toEqual(
      b.nodes.map((n) => [n.id, Math.round(n.x), Math.round(n.y)]),
    );
  });
  it("builds one owns edge per fact and one recall edge per cross-session recall", () => {
    const l = layout(story(), 900, 500);
    expect(l.edges.filter((e) => e.kind === "owns")).toHaveLength(20);
    expect(l.edges.filter((e) => e.kind === "recall")).toHaveLength(3);
  });
  it("keeps every node inside the canvas and every fact near its session", () => {
    const l = layout(story(), 900, 500);
    for (const n of l.nodes) {
      expect(n.x).toBeGreaterThanOrEqual(0);
      expect(n.x).toBeLessThanOrEqual(900);
      expect(n.y).toBeGreaterThanOrEqual(0);
      expect(n.y).toBeLessThanOrEqual(500);
      if (n.kind === "fact") {
        const hub = l.nodes[n.home];
        expect(Math.hypot(hub.x - n.x, hub.y - n.y)).toBeLessThan(140);
      }
    }
  });
  it("keeps sessions apart from each other", () => {
    const l = layout(story(), 900, 500);
    const s = l.nodes.filter((n) => n.kind === "session");
    for (let i = 0; i < s.length; i++)
      for (let j = i + 1; j < s.length; j++)
        expect(Math.hypot(s[i].x - s[j].x, s[i].y - s[j].y)).toBeGreaterThan(60);
  });
});

describe("scenery", () => {
  it("adds grey nodes that spread over the canvas, thread to a neighbour, and are never hit", () => {
    const l = layout(story(), 900, 500, 260, 40);
    const grey = l.nodes.filter((n) => n.kind === "grey");
    expect(grey).toHaveLength(40);
    expect(l.edges.filter((e) => e.kind === "ambient").length).toBeGreaterThan(30);
    expect(grey.some((n) => n.x < 200) && grey.some((n) => n.x > 700)).toBe(true);
    for (const n of grey) expect(hit(l, n.x, n.y)?.kind).not.toBe("grey");
    // the same story without scenery is unchanged
    const a = layout(story(), 900, 500).nodes.map((n) => [n.id, Math.round(n.x)]);
    const b = l.nodes.filter((n) => n.kind !== "grey").map((n) => [n.id, Math.round(n.x)]);
    expect(b).toEqual(a);
  });
});

describe("hit", () => {
  it("finds the node under the pointer and nothing in empty space", () => {
    const l = layout(story(), 900, 500);
    const s = l.nodes.find((n) => n.kind === "session")!;
    expect(hit(l, s.x + 3, s.y - 2)?.id).toBe(s.id);
    // a fact is a 5px dot but an 11px target
    const f = l.nodes.find((n) => n.kind === "fact")!;
    expect(hit(l, f.x + 9, f.y)?.id).toBe(f.id);
    expect(hit(l, -100, -100)).toBeNull();
  });
  it("honours the nudge offsets when testing", () => {
    const l = layout(story(), 900, 500);
    const i = l.nodes.findIndex((n) => n.kind === "fact");
    const dx = new Float64Array(l.nodes.length), dy = new Float64Array(l.nodes.length);
    dx[i] = 30;
    expect(hit(l, l.nodes[i].x + 30, l.nodes[i].y, dx, dy)?.id).toBe(l.nodes[i].id);
  });
});

describe("fit", () => {
  it("centres the world in the canvas and shows all of it", () => {
    const v = fit(1200, 900, 600, 600);
    expect(v.scale).toBe(0.5); // the tighter axis wins: 600/1200
    expect(v.tx).toBe(0);
    expect(v.ty).toBe(75); // 900 * 0.5 = 450, centred in 600
  });

  it("is the identity when the world is the canvas", () => {
    expect(fit(800, 500, 800, 500)).toEqual({ scale: 1, tx: 0, ty: 0 });
  });
});

describe("zoomAbout", () => {
  it("holds the anchor point still", () => {
    const before = { scale: 1, tx: 0, ty: 0 };
    const world = (v: typeof before, x: number) => (x - v.tx) / v.scale;
    const anchor = 300;
    const w0 = world(before, anchor);
    const after = zoomAbout(before, 2.5, anchor, anchor);
    expect(world(after, anchor)).toBeCloseTo(w0, 6);
  });
});

describe("clampView", () => {
  it("centres an axis whose world is smaller than the canvas, however it was dragged", () => {
    const v = clampView({ scale: 0.5, tx: -9999, ty: 4000 }, 400, 400, 600, 600);
    expect(v.tx).toBe(200); // (600 - 200) / 2
    expect(v.ty).toBe(200);
  });

  it("keeps a zoomed-in world from being dragged off the canvas", () => {
    const w = 600, world = 3000, scale = 2;
    const far = clampView({ scale, tx: 5000, ty: 5000 }, world, world, w, w);
    expect(far.tx).toBeLessThanOrEqual(80);
    const other = clampView({ scale, tx: -99999, ty: -99999 }, world, world, w, w);
    expect(other.tx).toBeGreaterThanOrEqual(w - world * scale - 80);
  });

  it("leaves a viewport that is already inside the bounds alone", () => {
    const v = { scale: 2, tx: -100, ty: -100 };
    expect(clampView(v, 3000, 3000, 600, 600)).toEqual(v);
  });
});

describe("worldScale", () => {
  it("is 1 for a small window, so the demo lays out exactly as it did", () => {
    expect(worldScale(35)).toBe(1);
    expect(worldScale(130)).toBe(1);
  });

  it("grows with the node count and is capped", () => {
    expect(worldScale(520)).toBeCloseTo(2, 5);
    expect(worldScale(100000)).toBe(2.6);
  });
});

describe("LabelSpace", () => {
  it("refuses a box that overlaps one already taken, and allows one that clears it", () => {
    const s = new LabelSpace();
    expect(s.free(0, 0, 100, 20)).toBe(true);
    s.take(0, 0, 100, 20);
    expect(s.free(50, 10, 150, 30)).toBe(false); // overlaps
    expect(s.free(101, 0, 200, 20)).toBe(true); // clears to the right
    expect(s.free(0, 21, 100, 40)).toBe(true); // clears below
  });
});

describe("hit under zoom", () => {
  // one hub, nothing else in reach, so the assertions are about distance and not about which of
  // several crowded nodes won
  const lone = () => layout({
    sessions: [{ session_id: "only", title: "t", project: "p", user: "u", started_at: "2024-06-01", n_facts: 0 }],
    facts: [], recalls: [],
  }, 900, 600, 40);

  it("keeps the touch target the same size on screen", () => {
    const l = lone();
    const s = l.nodes[0];
    const off = 18; // inside the session's 19px world reach at scale 1
    expect(hit(l, s.x + off, s.y)).toBe(s);
    // zoomed IN, the same world offset is 4x further away on screen, so it must stop hitting
    expect(hit(l, s.x + off, s.y, null, null, 4)).toBe(null);
    // zoomed OUT, a 3x bigger world offset is still within the same screen distance
    expect(hit(l, s.x + off * 3, s.y, null, null, 0.25)).toBe(s);
  });
});
