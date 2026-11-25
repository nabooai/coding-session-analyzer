// Pure hash-route logic, kept out of main.ts so vitest can import it with no DOM (main.ts touches
// `document` at import time). One place owns the rule a whole class of dead links came from: a route
// that needs an id must never be BUILT with an empty one, and must never be MATCHED as an unknown hash
// when the id is missing — an unknown hash normalises to #/, which reads as "the link does nothing".

/** The href for a session, or "" when there is no id — the caller then renders no link at all.
 *  `#/corpus/session/` (an id-less href) matched no route, so clicking it silently redirected to #/.
 *  A fact ref rides along as `?fact=` so the session view can scroll to and mark the claim the
 *  reader clicked — without it, landing on a big session reads as "this session isn't about that". */
export function sessionHref(id: string | null | undefined, fact?: string | null): string {
  const s = (id ?? "").trim();
  if (!s) return "";
  const f = (fact ?? "").trim();
  return `#/corpus/session/${encodeURIComponent(s)}${f ? `?fact=${encodeURIComponent(f)}` : ""}`;
}

/** Decoded session id for a `#/corpus/session/...` path; "" when the path IS that route but carries no
 *  id (a real not-found, to be reported); null when the path is some other route. */
export function corpusSessionRoute(path: string): string | null {
  const m = path.match(/^\/corpus\/session(?:\/(.*))?$/);
  if (!m) return null;
  return decodeId(m[1] ?? "").trim();
}

/** Which view a SIGNED-OUT visitor gets, from the hash alone. A bare arrival, `#/` (Memory) and
 *  `#/welcome` (where every sign-in lands) all land on the landing, so it survives a reload and is
 *  shareable. A deep link into the app (a teammate's `#/corpus/session/…`, `#/team`) means the
 *  visitor has a destination: they get the sign-in and route() resumes there. */
export function signedOutView(hash: string): "landing" | "login" | "route" {
  const h = (hash || "").replace(/^#/, "");
  if (h === "" || h === "/" || h === "/welcome") return "landing";
  return "login";
}

/** The signed-out front door, combining the hash view with whether the server takes workspace
 *  requests. The landing (and its "request a workspace" flow) only makes sense where a request has
 *  somewhere to go; on every other server (a self-host or on-prem box) the sign-in stays the front
 *  door, so a "landing" view downgrades to "login". Everything else passes through unchanged.
 *  `workspaceRequests` comes from /api/auth-config, defaulting false when unreadable — so an unknown
 *  server shows the sign-in, never a marketing page it may not want. */
export function frontDoor(hash: string, workspaceRequests: boolean): "landing" | "login" | "route" {
  const view = signedOutView(hash);
  if (view === "landing" && !workspaceRequests) return "login";
  return view;
}

/** Paths people guess or kept from a retired page, each to the page that serves it now. */
export const ROUTE_ALIASES: Record<string, string> = {
  "/admin/members": "/team",
  "/admin/team": "/team",
  "/admin/analytics": "/admin",
  "/admin/taxonomy": "/admin/entity-types",
  "/admin/entities": "/admin/entity-types",
  "/admin/settings": "/admin/models",
  "/admin/providers": "/admin/models",
  "/admin/service-tokens": "/admin/tokens",
  "/sessions": "/",
  "/entities": "/admin/entity-types",
  "/analytics": "/admin",
  "/settings": "/admin/models",
  "/facts": "/",
  "/memory": "/",
  "/home": "/",
};

/** A hand-typed or truncated escape ("%E0%A4%A") makes decodeURIComponent throw. Uncaught in route()
 *  that leaves the page on "Loading…" forever, so treat the raw text as the id and let the lookup 404.
 *  Every route that carries an id decodes through here. */
export function decodeId(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/** A bare signed-in arrival opens search. Applied before boot() knows whether the visitor is signed
 * out; the signed-out front door still decides between the landing and sign-in screen. */
export function bareArrivalHash(hash: string): string {
  return !hash || hash === "#" ? "#/" : hash;
}

/** The href for "Sign in with <SSO>". Every sign-in returns to the focused search screen. */
export function ssoHref(startUrl: string): string {
  return `${startUrl}?return=${encodeURIComponent("/#/")}`;
}
