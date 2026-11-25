# Lesson: the web UI

`server/ui` is vanilla TypeScript, no runtime dependencies, all DOM through `el()`. These are the
traps the redesign (PR #104, 2026-09-05) hit; each is checkable in the code named.

## A view outside the router must clean up after itself

The landing and the sign-in are MODES rendered by `boot()`, not routes: a hash change on them fires
`route()`, whose first authenticated fetch 401s into the sign-in. In-page navigation there is
`scrollIntoView`, never a hash link, and the wordmark is `brandStatic()`. Anything with a timer or an
animation frame registers so `setView()` can destroy it (`activeGraph`); the graph also cancels itself
when its canvas leaves the document.

## `[hidden]` loses to an author `display`

`.gbtn { display: flex }` beat the UA `[hidden]` rule, so a hidden Google slot and its divider
rendered. `style.css` now carries `[hidden] { display: none !important; }`; keep it.

## A canvas needs an integer CSS box and device-pixel text

A `vh`-based hero height gave the canvas a fractional CSS size; the bitmap was rounded and the
browser resampled it, so every label went soft. `GraphView.relayout()` sizes the canvas from its
container in whole pixels and `draw()` snaps text to device pixels. Repaint on font load, OS scheme
change and DPR change, and re-resolve the hovered node after a relayout (fresh node objects).

## The theme default is light and only a click persists it

The old boot wrote its implicit "system" on first visit, so the stored value said nothing about the
viewer. The preference lives under `csa-appearance`, written only by the toggle; a light or dark
click saved under the old `csa-theme` still counts. `initTheme()` applies without persisting.

## Every sticky or jump offset adds `--nav-h`

The top bar is 60px sticky (104px when it wraps under 900px). `scroll-margin-top` and any
`position: sticky; top` on a page adds `var(--nav-h)`, or the heading lands under the bar.

## A pressed verdict is not always a human's

`/api/facts` rows carry judge-set labels (`flagger: "judge"`). Clicking a pressed button sends
`clear` only when a person set it; on a judge label the click re-sends the label as a human flag.
Sending `clear` on a judge label deletes the review and exempts the fact from the judge for good.

## Missing static files are 404s, never the shell

The SPA fallback used to serve `index.html` for a stale `/assets/<hash>.js`, and the cache header
keyed on the request would have pinned that HTML for a year. `_serve_ui` answers a missing FILE
with 404 and only route-like paths get the shell.

## Every browser POST is allowlisted

A cookie session may POST only what `_COOKIE_POST_OK` lists, same-origin. A route missing from the
list 401s from the browser with no other symptom. `test_apiserver.py` pins the set.

## Browser screenshots

The Chrome DevTools MCP `resize_page` floors near 500px; use `emulate` with
`"390x844x2,mobile,touch"` for a phone. Each persona or check gets its own `isolatedContext`; the
context keeps localStorage, so a theme toggled during testing persists there (and nowhere else).
Reduced motion is verified by stubbing `matchMedia` in `initScript` and counting animation frames.

## Facsimiles are pictures

A landing "screenshot" built from live components carries focusable controls. Mark the container
`inert` (and `role="img"` with an alt), or Tab lands inside the picture.
