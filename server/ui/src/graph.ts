// The session / fact / recall graph. Two halves: a pure, seeded force layout (unit-tested, no DOM)
// and a canvas view that draws it, hit-tests hover and click, nudges nodes near the pointer, and
// freezes under prefers-reduced-motion. The landing feeds it a fixed demo story; Memory's Graph view
// feeds it GET /api/graph, whose shape lives in api.ts with every other contract type.
import type { GraphData, GraphFact, GraphSession } from "./api";

export interface GNode {
  id: string;
  kind: "session" | "fact" | "grey"; // grey = ambient scenery: no data, no label, no hit
  type: string;
  x: number;
  y: number;
  r: number;
  home: number; // index of the owning session node; -1 for a session
}
export interface GEdge {
  a: number; // fact (or a grey node)
  b: number; // session (or the grey node's neighbour)
  kind: "owns" | "recall" | "ambient";
}
export interface Layout {
  nodes: GNode[];
  edges: GEdge[];
  w: number;
  h: number;
}

// FNV-1a over the sorted session ids: the same corpus lays out the same way on every reload.
export function seedOf(ids: string[]): number {
  let h = 0x811c9dc5;
  for (const id of [...ids].sort()) {
    for (let i = 0; i < id.length; i++) {
      h ^= id.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
  }
  return h >>> 0;
}

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const SESSION_R = 11;
const FACT_R = 5;
const GREY_R = 4.5;
const PAD = 48;

/** Deterministic force layout. Sessions are hubs, each fact springs to its session, a recall pulls a
 *  fact toward the session that reused it. Runs to rest synchronously so the view never animates
 *  the settle (and reduced motion needs no special case). */
export function layout(data: GraphData, w: number, h: number, steps = 260, ambient = 0): Layout {
  const rnd = mulberry32(seedOf(data.sessions.map((s) => s.session_id)));
  const nodes: GNode[] = [];
  const edges: GEdge[] = [];
  const sIdx = new Map<string, number>();
  const n = data.sessions.length;
  data.sessions.forEach((s, i) => {
    // sessions start spread along a shallow arc so labels rarely collide; facts orbit their hub
    const t = n === 1 ? 0.5 : i / (n - 1);
    const x = PAD + t * (w - 2 * PAD) + (rnd() - 0.5) * 40;
    const y = h * (0.42 + 0.22 * Math.sin(t * Math.PI * 1.7 + rnd())) + (rnd() - 0.5) * 30;
    sIdx.set(s.session_id, nodes.length);
    nodes.push({ id: s.session_id, kind: "session", type: "session", x, y, r: SESSION_R, home: -1 });
  });
  const fIdx = new Map<string, number>();
  for (const f of data.facts) {
    const home = sIdx.get(f.session_id);
    if (home === undefined) continue;
    const hub = nodes[home];
    const ang = rnd() * Math.PI * 2;
    const d = 40 + rnd() * 30;
    fIdx.set(f.ref, nodes.length);
    nodes.push({ id: f.ref, kind: "fact", type: f.type, x: hub.x + Math.cos(ang) * d, y: hub.y + Math.sin(ang) * d, r: FACT_R, home });
    edges.push({ a: nodes.length - 1, b: home, kind: "owns" });
  }
  for (const r of data.recalls) {
    const a = fIdx.get(r.ref);
    const b = sIdx.get(r.session_id);
    if (a === undefined || b === undefined || nodes[a].home === b) continue;
    edges.push({ a, b, kind: "recall" });
  }

  const vx = new Float64Array(nodes.length);
  const vy = new Float64Array(nodes.length);
  for (let step = 0; step < steps; step++) {
    const cool = 1 - step / steps;
    // repulsion, every pair (n is small: at most ~120 nodes)
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = rnd() - 0.5; dy = rnd() - 0.5; d2 = 1; }
        const both = a.kind === "session" && b.kind === "session";
        // hubs repel harder on a small canvas (a phone): the same five sessions need the same room
        const k = both ? 42000 * Math.max(1, 450000 / (w * h)) : a.kind === "fact" && b.kind === "fact" ? 900 : 2600;
        const f = Math.min(k / d2, 12);
        const d = Math.sqrt(d2);
        vx[i] += (dx / d) * f; vy[i] += (dy / d) * f;
        vx[j] -= (dx / d) * f; vy[j] -= (dy / d) * f;
      }
    }
    // springs
    for (const e of edges) {
      const a = nodes[e.a], b = nodes[e.b];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const rest = e.kind === "owns" ? 62 : 200;
      const k = e.kind === "owns" ? 0.06 : 0.012;
      const f = (d - rest) * k;
      vx[e.a] += (dx / d) * f; vy[e.a] += (dy / d) * f;
      // the hub moves less: it is the heavier node
      vx[e.b] -= (dx / d) * f * 0.3; vy[e.b] -= (dy / d) * f * 0.3;
    }
    // gentle gravity toward the centre line, so nothing drifts into a corner
    for (let i = 0; i < nodes.length; i++) {
      const nd = nodes[i];
      const g = nd.kind === "session" ? 0.0006 : 0.002;
      vx[i] += (w / 2 - nd.x) * g;
      vy[i] += (h / 2 - nd.y) * g * 2;
      vx[i] *= 0.82; vy[i] *= 0.82;
      const lim = 6 * cool + 0.4;
      nd.x += Math.max(-lim, Math.min(lim, vx[i]));
      nd.y += Math.max(-lim, Math.min(lim, vy[i]));
      const p = nd.kind === "session" ? PAD : PAD * 0.4;
      nd.x = Math.max(p, Math.min(w - p, nd.x));
      nd.y = Math.max(p, Math.min(h - p, nd.y));
    }
  }
  if (ambient > 0) scenery(nodes, edges, rnd, w, h, ambient);
  return { nodes, edges, w, h };
}

// Grey nodes with no data behind them, spread over the whole canvas and threaded to their nearest
// neighbour: the corpus is bigger than the five sessions the story tells. They repel the real nodes
// lightly so they never sit on a label, and hit() never returns them.
function scenery(nodes: GNode[], edges: GEdge[], rnd: () => number, w: number, h: number, count: number): void {
  const first = nodes.length;
  for (let i = 0; i < count; i++)
    nodes.push({ id: `~${i}`, kind: "grey", type: "grey", x: 12 + rnd() * (w - 24), y: 12 + rnd() * (h - 24), r: GREY_R, home: -1 });
  for (let step = 0; step < 40; step++) {
    for (let i = first; i < nodes.length; i++) {
      const g = nodes[i];
      let fx = 0, fy = 0;
      for (let j = 0; j < nodes.length; j++) {
        if (j === i) continue;
        const o = nodes[j];
        const dx = g.x - o.x, dy = g.y - o.y;
        const d2 = dx * dx + dy * dy || 1;
        const k = o.kind === "grey" ? 900 : 4000;
        const f = Math.min(k / d2, 8);
        const d = Math.sqrt(d2);
        fx += (dx / d) * f; fy += (dy / d) * f;
      }
      g.x = Math.max(8, Math.min(w - 8, g.x + fx));
      g.y = Math.max(8, Math.min(h - 8, g.y + fy));
    }
  }
  for (let i = first; i < nodes.length; i++) {
    let best = -1, bestD = 170 * 170;
    for (let j = 0; j < nodes.length; j++) {
      if (j === i || nodes[j].kind === "session") continue;
      const d2 = (nodes[i].x - nodes[j].x) ** 2 + (nodes[i].y - nodes[j].y) ** 2;
      if (d2 < bestD) { best = j; bestD = d2; }
    }
    if (best >= 0) edges.push({ a: i, b: best, kind: "ambient" });
  }
}

/** The node under world point (x, y), sessions first because they are the bigger targets. Facts get a
 *  wider hit radius than they draw: a 10px dot is not a touch target. `scale` keeps that target the
 *  same size ON SCREEN at every zoom, so a zoomed-out node is not harder to hit than a zoomed-in one. */
export function hit(l: Layout, x: number, y: number, dx: Float64Array | null = null,
                    dy: Float64Array | null = null, scale = 1): GNode | null {
  let best: GNode | null = null;
  let bestD = Infinity;
  l.nodes.forEach((n, i) => {
    if (n.kind === "grey") return;
    const nx = n.x + (dx ? dx[i] : 0), ny = n.y + (dy ? dy[i] : 0);
    const reach = (n.kind === "session" ? n.r + 8 : 11) / scale;
    const d = Math.hypot(nx - x, ny - y);
    if (d <= reach && d < bestD) { best = n; bestD = d; }
  });
  return best;
}

/** Where the world sits on the canvas: world * scale + t. Identity for a view that never pans. */
export interface Viewport {
  scale: number;
  tx: number;
  ty: number;
}

export const IDENTITY: Viewport = { scale: 1, tx: 0, ty: 0 };

/** The viewport that centres a `worldW x worldH` layout inside a `w x h` canvas. */
export function fit(worldW: number, worldH: number, w: number, h: number): Viewport {
  const scale = Math.min(w / worldW, h / worldH);
  return { scale, tx: (w - worldW * scale) / 2, ty: (h - worldH * scale) / 2 };
}

/** A drag can never strand the graph off-screen: whatever the zoom, the world keeps at least
 *  `KEEP` visible pixels of itself inside the canvas on each axis, and a world smaller than the
 *  canvas on an axis stays centred on it. */
const KEEP = 80;
export function clampView(v: Viewport, worldW: number, worldH: number, w: number, h: number): Viewport {
  const axis = (t: number, world: number, canvas: number) => {
    const size = world * v.scale;
    if (size <= canvas) return (canvas - size) / 2;
    return Math.max(canvas - size - KEEP, Math.min(KEEP, t));
  };
  return { scale: v.scale, tx: axis(v.tx, worldW, w), ty: axis(v.ty, worldH, h) };
}

/** Zoom to `scale` while holding the world point under (ax, ay) still. */
export function zoomAbout(v: Viewport, scale: number, ax: number, ay: number): Viewport {
  return { scale, tx: ax - ((ax - v.tx) / v.scale) * scale, ty: ay - ((ay - v.ty) / v.scale) * scale };
}

/** A label is drawn only where nothing has been drawn already, so a crowded window shows the hubs
 *  that fit instead of a pile of overstruck text. Zooming in spreads the boxes and reveals more. */
export class LabelSpace {
  private boxes: number[][] = [];
  free(x0: number, y0: number, x1: number, y1: number): boolean {
    return this.boxes.every((b) => x1 < b[0] || x0 > b[2] || y1 < b[1] || y0 > b[3]);
  }
  take(x0: number, y0: number, x1: number, y1: number): void {
    this.boxes.push([x0, y0, x1, y1]);
  }
}

export interface GraphColors {
  ink: string;
  muted: string;
  edge: string;
  accent: string;
  paper: string;
  sans: string;
  node: (type: string) => string;
}

export interface GraphViewOptions {
  data: GraphData;
  colors: () => GraphColors;
  onHover?: (node: GNode | null, clientX: number, clientY: number) => void;
  onClick?: (node: GNode | null, clientX: number, clientY: number) => void; // null = empty canvas
  reducedMotion?: boolean;
  labels?: boolean; // false for a decorative background: nodes and edges only
  stackLabels?: boolean; // force the narrow-canvas label placement: centred under (odd hubs: over) the node
  ambient?: number; // grey scenery nodes per 10,000 square CSS pixels (0 = none)
  pannable?: boolean; // drag to pan, wheel or pinch to zoom, and a layout bigger than the canvas
  onView?: (v: Viewport, min: number, max: number) => void; // so the caller can enable/disable its controls
}

export const MAX_SCALE = 5;
//: how much bigger than the canvas the layout gets, by node count. A window of a few sessions lays
//: out exactly as before (1); a real corpus window gets room instead of a pile against the border.
export function worldScale(nodes: number): number {
  return Math.min(2.6, Math.max(1, Math.sqrt(nodes / 130)));
}

/** Owns one <canvas>: lays the data out for the canvas's CSS size, redraws on resize and theme
 *  change, nudges nodes near the pointer (an offset over the fixed layout, never a re-layout), and
 *  reports hover / click. destroy() stops every observer and frame; the caller must call it when the
 *  view leaves the page, because the landing lives outside route()'s timer bookkeeping. */
export class GraphView {
  private l!: Layout; // assigned by relayout() in the constructor
  private dx = new Float64Array(0);
  private dy = new Float64Array(0);
  private hot: GNode | null = null;
  private mouse: { x: number; y: number } | null = null;
  private frame = 0;
  private ro: ResizeObserver | null = null;
  private mo: MutationObserver | null = null;
  private off: (() => void)[] = [];
  private c: GraphColors | null = null; // resolved once per palette change, not per frame
  private v: Viewport = { ...IDENTITY };
  private worldW = 0;
  private worldH = 0;
  private cssW = 0;
  private cssH = 0;
  private minScale = 1;
  private drag: { x: number; y: number; moved: number } | null = null;
  private pointers = new Map<number, { x: number; y: number }>();
  private pinch = 0; // finger distance when the two-finger gesture began
  private byId = new Map<string, GraphFact>();
  private recallsOf = new Map<string, Set<string>>(); // session -> fact refs it recalled
  private recalledBy = new Map<string, Set<string>>(); // fact -> sessions that recalled it

  constructor(private canvas: HTMLCanvasElement, private opts: GraphViewOptions) {
    if (opts.reducedMotion === undefined && typeof matchMedia === "function")
      opts.reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
    for (const f of opts.data.facts) this.byId.set(f.ref, f);
    for (const r of opts.data.recalls) {
      (this.recallsOf.get(r.session_id) ?? this.recallsOf.set(r.session_id, new Set()).get(r.session_id)!).add(r.ref);
      (this.recalledBy.get(r.ref) ?? this.recalledBy.set(r.ref, new Set()).get(r.ref)!).add(r.session_id);
    }
    this.relayout();
    if (typeof ResizeObserver !== "undefined") {
      this.ro = new ResizeObserver(() => this.relayout());
      this.ro.observe(canvas.parentElement ?? canvas);
    }
    // the palette changes under the canvas in three ways: the theme attribute, the OS scheme while
    // on "system", and the webfont arriving after the first paint
    const repaint = () => { this.c = null; this.draw(); };
    if (typeof MutationObserver !== "undefined") {
      this.mo = new MutationObserver(repaint);
      this.mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }
    if (typeof matchMedia === "function") {
      const mq = matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener("change", repaint);
      this.off.push(() => mq.removeEventListener("change", repaint));
    }
    document.fonts?.ready.then(() => { if (this.frame !== -1) repaint(); });
    const on = <K extends keyof HTMLElementEventMap>(k: K, fn: (e: HTMLElementEventMap[K]) => void,
                                                     o?: AddEventListenerOptions) => {
      canvas.addEventListener(k, fn, o);
      this.off.push(() => canvas.removeEventListener(k, fn, o));
    };
    on("pointermove", (e) => {
      if (this.dragging(e)) return;
      if (e.pointerType === "touch") return; // a finger has no hover; tap is handled by click
      const p = this.toWorld(e);
      this.mouse = p;
      const h = hit(this.l, p.x, p.y, this.dx, this.dy, this.v.scale);
      if (h !== this.hot) {
        this.hot = h;
        canvas.style.cursor = h ? "pointer" : this.opts.pannable ? "grab" : "";
        opts.onHover?.(h, e.clientX, e.clientY);
      }
      this.tick();
    });
    on("pointerleave", (e) => {
      if (e.pointerType === "touch") return;
      this.mouse = null;
      if (this.hot) { this.hot = null; opts.onHover?.(null, e.clientX, e.clientY); }
      canvas.style.cursor = this.opts.pannable ? "grab" : "";
      this.tick();
    });
    on("click", (e) => {
      if (this.drag && this.drag.moved > 4) return; // that was a pan, not a pick
      const p = this.toWorld(e);
      opts.onClick?.(hit(this.l, p.x, p.y, this.dx, this.dy, this.v.scale), e.clientX, e.clientY);
    });
    if (!opts.pannable) return;

    canvas.style.cursor = "grab";
    canvas.tabIndex = 0;
    on("pointerdown", (e) => {
      this.pointers.set(e.pointerId, this.toLocal(e));
      if (this.pointers.size === 2) { this.pinch = this.spread(); return; }
      canvas.setPointerCapture(e.pointerId);
      this.drag = { x: e.clientX, y: e.clientY, moved: 0 };
      canvas.style.cursor = "grabbing";
    });
    on("pointermove", (e) => {
      if (!this.pointers.has(e.pointerId)) return;
      this.pointers.set(e.pointerId, this.toLocal(e));
      if (this.pointers.size >= 2) {          // pinch: the finger distance drives the zoom, its midpoint the anchor
        const d = this.spread();
        if (this.pinch > 0 && d > 0) {
          const m = this.midpoint();
          this.zoomTo(this.v.scale * (d / this.pinch), m.x, m.y);
        }
        this.pinch = d;
        return;
      }
      if (!this.drag) return;
      this.drag.moved += Math.abs(e.clientX - this.drag.x) + Math.abs(e.clientY - this.drag.y);
      this.panBy(e.clientX - this.drag.x, e.clientY - this.drag.y);
      this.drag.x = e.clientX; this.drag.y = e.clientY;
    });
    const release = (e: PointerEvent) => {
      this.pointers.delete(e.pointerId);
      if (this.pointers.size < 2) this.pinch = 0;
      if (this.drag) canvas.style.cursor = this.hot ? "pointer" : "grab";
      // the click fires after pointerup, and it reads drag.moved, so the flag clears a tick later
      setTimeout(() => { this.drag = null; }, 0);
    };
    on("pointerup", release);
    on("pointercancel", release);
    // passive:false because a graph that scrolls the page instead of zooming is the bug being fixed
    on("wheel", (e) => {
      e.preventDefault();
      const p = this.toLocal(e);
      this.zoomTo(this.v.scale * Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), p.x, p.y);
    }, { passive: false });
    on("dblclick", () => this.resetView());
    on("keydown", (e) => {
      const step = e.shiftKey ? 120 : 40;
      const k: Record<string, () => void> = {
        "+": () => this.zoomBy(1.3), "=": () => this.zoomBy(1.3), "-": () => this.zoomBy(1 / 1.3),
        "0": () => this.resetView(),
        ArrowLeft: () => this.panBy(step, 0), ArrowRight: () => this.panBy(-step, 0),
        ArrowUp: () => this.panBy(0, step), ArrowDown: () => this.panBy(0, -step),
      };
      const fn = k[e.key];
      if (!fn) return;
      e.preventDefault();
      fn();
    });
  }

  private dragging(e: PointerEvent): boolean {
    return this.pointers.size >= 2 || (!!this.drag && this.pointers.has(e.pointerId));
  }

  private spread(): number {
    const [a, b] = [...this.pointers.values()];
    return b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
  }

  private midpoint(): { x: number; y: number } {
    const [a, b] = [...this.pointers.values()];
    return b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } : a;
  }

  /** Sessions the hovered node relates to, for the hover card: a session lists the facts it recalled,
   *  a fact lists the sessions that recalled it. */
  recalledFacts(sessionId: string): GraphFact[] {
    return [...(this.recallsOf.get(sessionId) ?? [])].map((r) => this.byId.get(r)!).filter(Boolean);
  }
  recalledInto(ref: string): string[] {
    return [...(this.recalledBy.get(ref) ?? [])];
  }
  session(id: string): GraphSession | undefined {
    return this.opts.data.sessions.find((s) => s.session_id === id);
  }

  destroy(): void {
    this.ro?.disconnect();
    this.mo?.disconnect();
    for (const f of this.off) f();
    if (this.frame > 0) cancelAnimationFrame(this.frame);
    this.frame = -1; // destroyed: a late fonts.ready must not draw
  }

  private toLocal(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const b = this.canvas.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  }

  /** Canvas pixels to layout coordinates. Every hit test and the pointer nudge speak world. */
  private toWorld(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const p = this.toLocal(e);
    return { x: (p.x - this.v.tx) / this.v.scale, y: (p.y - this.v.ty) / this.v.scale };
  }

  /** Show the whole layout again. */
  resetView(): void {
    this.v = fit(this.worldW, this.worldH, this.cssW, this.cssH);
    this.minScale = this.v.scale;
    this.announce();
    this.draw();
  }

  /** Multiply the zoom about the canvas centre (what a button press means). */
  zoomBy(factor: number): void {
    this.zoomTo(this.v.scale * factor, this.cssW / 2, this.cssH / 2);
  }

  private zoomTo(scale: number, ax: number, ay: number): void {
    const s = Math.max(this.minScale, Math.min(MAX_SCALE, scale));
    if (s === this.v.scale) return;
    this.v = clampView(zoomAbout(this.v, s, ax, ay), this.worldW, this.worldH, this.cssW, this.cssH);
    this.announce();
    this.draw();
  }

  private panBy(dx: number, dy: number): void {
    this.v = clampView({ ...this.v, tx: this.v.tx + dx, ty: this.v.ty + dy }, this.worldW, this.worldH, this.cssW, this.cssH);
    this.draw();
  }

  private announce(): void {
    this.opts.onView?.({ ...this.v }, this.minScale, MAX_SCALE);
  }

  // The canvas takes an INTEGER CSS size from its container: a fractional size (a vh-based height) makes
  // the browser resample the bitmap and every label goes soft.
  private relayout(): void {
    const box = this.canvas.parentElement ?? this.canvas;
    const w = Math.floor(box.clientWidth) || 600;
    const h = Math.floor(box.clientHeight) || 420;
    this.canvas.style.width = `${w}px`;
    this.canvas.style.height = `${h}px`;
    this.cssW = w;
    this.cssH = h;
    // the settle is all-pairs per step: past ~400 nodes (a real corpus window, not the demo) fewer steps
    // keep the first paint under half a second at the cost of a looser layout
    const n = this.opts.data.sessions.length + this.opts.data.facts.length;
    const steps = n > 400 ? Math.max(90, Math.round((260 * 400) / n)) : 260;
    // A pannable view lays out into a WORLD bigger than the canvas, so the layout's own edge clamp
    // stops being the thing that decides where a node goes. Everything still fits on first paint,
    // because the initial viewport is `fit`; the extra room is what zooming in has to show.
    const k = this.opts.pannable ? worldScale(n) : 1;
    this.worldW = Math.round(w * k);
    this.worldH = Math.round(h * k);
    this.l = layout(this.opts.data, this.worldW, this.worldH, steps,
                    Math.round((this.worldW * this.worldH) / 10000 * (this.opts.ambient ?? 0)));
    this.v = fit(this.worldW, this.worldH, w, h);
    this.minScale = this.v.scale;
    this.announce();
    this.dx = new Float64Array(this.l.nodes.length);
    this.dy = new Float64Array(this.l.nodes.length);
    this.hot = this.hot ? this.l.nodes.find((n) => n.id === this.hot!.id) ?? null : null; // fresh objects
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.draw();
  }

  // One frame of the pointer nudge: nodes inside 90px lean away from the pointer, everything eases
  // back toward the layout. Stops itself once still. Under reduced motion nothing moves at all.
  private tick(): void {
    if (this.opts.reducedMotion) return void this.draw();
    if (this.frame !== 0) return;
    const step = () => {
      this.frame = 0;
      if (!document.body.contains(this.canvas)) return this.destroy(); // belt: the view left without destroy()
      let moving = false;
      const m = this.mouse;
      this.l.nodes.forEach((n, i) => {
        let tx = 0, ty = 0;
        if (m) {
          const ddx = n.x - m.x, ddy = n.y - m.y;
          const d = Math.hypot(ddx, ddy) || 1;
          // nodes under the pointer are targets, not scenery: no push inside the hit reach
          if (d < 90 && d > 16 && n !== this.hot) {
            const push = (1 - d / 90) * (n.kind === "session" ? 6 : 10);
            tx = (ddx / d) * push; ty = (ddy / d) * push;
          }
        }
        this.dx[i] += (tx - this.dx[i]) * 0.18;
        this.dy[i] += (ty - this.dy[i]) * 0.18;
        if (Math.abs(this.dx[i] - tx) > 0.05 || Math.abs(this.dy[i] - ty) > 0.05) moving = true;
      });
      this.draw();
      if (moving) this.frame = requestAnimationFrame(step);
    };
    this.frame = requestAnimationFrame(step);
  }

  draw(): void {
    if (this.frame === -1) return;
    const { nodes, edges } = this.l;
    const w = this.cssW, h = this.cssH;
    const dpr = window.devicePixelRatio || 1;
    if (this.canvas.width !== Math.round(w * dpr)) return this.relayout(); // the display's ratio changed under us
    const ctx = this.canvas.getContext("2d");
    if (!ctx) return;
    const c = (this.c ??= this.opts.colors());
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const { scale, tx, ty } = this.v;
    // World to canvas. Positions scale; radii, strokes and type do NOT, so zooming spreads the graph
    // out instead of magnifying it, and a label stays the size it is readable at.
    const px = (i: number) => (nodes[i].x + this.dx[i]) * scale + tx;
    const py = (i: number) => (nodes[i].y + this.dy[i]) * scale + ty;
    const rs = Math.min(1.5, Math.max(0.7, scale));   // dots shrink a little when zoomed out, grow a little in
    const snap = (v: number) => Math.round(v * dpr) / dpr; // text on a device-pixel boundary stays sharp
    const near = (x: number, y: number, m = 60) => x > -m && x < w + m && y > -m && y < h + m;
    const hot = this.hot;
    const hotId = hot?.id ?? "";
    const lit = (e: GEdge) => !!hot && (nodes[e.a].id === hotId || nodes[e.b].id === hotId);

    // edges under everything: owns as hairlines, recalls as curved arrows into the recalling session
    ctx.lineWidth = 1;
    for (const e of edges) {
      const x1 = px(e.a), y1 = py(e.a), x2 = px(e.b), y2 = py(e.b);
      if (!near(x1, y1) && !near(x2, y2)) continue;
      const on = lit(e);
      ctx.strokeStyle = on ? c.muted : c.edge;
      ctx.globalAlpha = on ? 1 : e.kind === "owns" ? 0.9 : e.kind === "ambient" ? 0.5 : 0.75;
      ctx.beginPath();
      if (e.kind === "owns" || e.kind === "ambient") {
        ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
        continue;
      }
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const nx = -(y2 - y1), ny = x2 - x1;
      const len = Math.hypot(nx, ny) || 1;
      const bend = Math.min(40, len * 0.18);
      const cx = mx + (nx / len) * bend, cy = my + (ny / len) * bend;
      ctx.moveTo(x1, y1); ctx.quadraticCurveTo(cx, cy, x2, y2); ctx.stroke();
      // arrowhead, stopping at the session's rim
      const ang = Math.atan2(y2 - cy, x2 - cx);
      const rim = SESSION_R * rs + 3;
      const ex = x2 - Math.cos(ang) * rim, ey = y2 - Math.sin(ang) * rim;
      ctx.fillStyle = on ? c.muted : c.edge;
      ctx.beginPath();
      ctx.moveTo(ex, ey);
      ctx.lineTo(ex - 7 * Math.cos(ang - 0.45), ey - 7 * Math.sin(ang - 0.45));
      ctx.lineTo(ex - 7 * Math.cos(ang + 0.45), ey - 7 * Math.sin(ang + 0.45));
      ctx.closePath(); ctx.fill();
    }
    ctx.globalAlpha = 1;

    // scenery, then facts
    ctx.fillStyle = c.node("grey");
    nodes.forEach((n, i) => {
      if (n.kind !== "grey") return;
      const x = px(i), y = py(i);
      if (!near(x, y, 10)) return;
      ctx.beginPath();
      ctx.arc(x, y, n.r * rs, 0, Math.PI * 2);
      ctx.fill();
    });
    nodes.forEach((n, i) => {
      if (n.kind !== "fact") return;
      const x = px(i), y = py(i);
      if (!near(x, y, 12)) return;
      const isHot = n === hot;
      ctx.beginPath();
      ctx.arc(x, y, n.r * rs + (isHot ? 2 : 0), 0, Math.PI * 2);
      ctx.fillStyle = c.node(n.type);
      ctx.fill();
      if (isHot) { ctx.lineWidth = 1.5; ctx.strokeStyle = c.accent; ctx.stroke(); }
    });

    // sessions: every hub is drawn, but a LABEL is only drawn where one fits. Biggest hubs first (and
    // the hovered one before all of them), so a crowded window names what matters instead of stacking
    // every title on top of the last. Zooming in spreads the boxes and more of them come back.
    ctx.textBaseline = "top";
    const space = new LabelSpace();
    const order = nodes
      .map((n, i) => ({ n, i }))
      .filter(({ n }) => n.kind === "session")
      .sort((a, b) => (a.n === hot ? -1 : b.n === hot ? 1 : 0)
                      || (this.session(b.n.id)?.n_facts ?? 0) - (this.session(a.n.id)?.n_facts ?? 0));
    for (const { n, i } of order) {
      const x = px(i), y = py(i);
      if (!near(x, y, 20)) continue;
      ctx.beginPath();
      ctx.arc(x, y, n.r * rs, 0, Math.PI * 2);
      ctx.fillStyle = c.node("session");
      ctx.fill();
      if (n === hot) {
        ctx.beginPath();
        ctx.arc(x, y, n.r * rs + 6, 0, Math.PI * 2);
        ctx.lineWidth = 1.6; ctx.strokeStyle = c.accent; ctx.stroke();
      }
    }
    if (this.opts.labels === false) return;
    for (const { n, i } of order) {
      const s = this.session(n.id);
      if (!s) continue;
      const x = px(i), y = py(i);
      if (!near(x, y, 20)) continue;
      const r = n.r * rs;
      const stack = w < 640 || this.opts.stackLabels;
      ctx.font = `600 ${stack ? 12 : 13}px ${c.sans}`;
      const tw = ctx.measureText(s.title).width;
      let lx: number, ly: number, x0: number;
      if (stack) {
        // above or below by where the node SITS, not by its index: a hub near the bottom edge whose
        // label went under it drew half a title off the canvas
        const over = y > h * 0.72 ? true : y < h * 0.2 ? false : i % 2 === 1;
        const half = tw / 2 + 4;
        lx = Math.min(Math.max(x, half), w - half);
        ly = over ? y - r - 30 : y + r + 6;
        x0 = lx - half;
      } else {
        const right = x > w * 0.82;
        lx = right ? x - r - 6 : x + r + 6;
        ly = y + 9;
        x0 = right ? lx - tw - 4 : lx - 4;
      }
      const box: [number, number, number, number] = [x0, ly - 3, x0 + tw + 8, ly + 32];
      if (box[1] < 0 || box[3] > h || box[0] < 0 || box[2] > w) continue; // never draw a clipped name
      if (n !== hot && !space.free(...box)) continue;   // the hovered hub always gets its name
      space.take(...box);
      ctx.textAlign = stack ? "center" : x > w * 0.82 ? "right" : "left";
      ctx.fillStyle = c.ink;
      ctx.fillText(s.title, snap(lx), snap(ly));
      ctx.fillStyle = c.muted;
      ctx.font = `400 ${stack ? 11 : 12}px ${c.sans}`;
      ctx.fillText(s.started_at, snap(lx), snap(ly + (stack ? 15 : 17)));
    }
  }
}

/** Colours resolved from the CSS custom properties at draw time, so a theme toggle redraws in the
 *  new palette without a rebuild. */
export function cssColors(elm: Element): GraphColors {
  const cs = getComputedStyle(elm);
  const v = (k: string) => cs.getPropertyValue(k).trim();
  const node = (t: string) => v(`--n-${t === "preference" ? "pref" : t}`) || v("--n-grey");
  return { ink: v("--ink"), muted: v("--muted"), edge: v("--edge"), accent: v("--accent"), paper: v("--paper"), sans: v("--sans") || "sans-serif", node };
}
