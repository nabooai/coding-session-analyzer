---
name: ui-visual-walk
description: Walk a visible UI change in a real browser before calling it done: build, boot the scratch server, screenshot every affected page in light and dark and at 390px, compare to the design system in docs/design/palette.md, and check console, network and reduced motion. Use for any change under server/ui ("check the page", "screenshot it", "compare to the mock", "/ui-visual-walk").
---

# UI visual walk

An agent cannot judge a screen it has never opened. This skill is the loop: build, look, adjust.

## Steps

1. **Build and serve.** `cd server/ui && npm run check && npm run build`, then the `scratch-server`
   skill (the server serves `dist`; rebuild after every change and reload with `ignoreCache`).
2. **Open your own tab.** Load the Chrome DevTools MCP tools with ONE ToolSearch call, then
   `new_page(url, isolatedContext: "walk-<name>")`. Only ever pass that pageId. Set the viewport with
   `emulate(pageId, viewport: "1440x900x2")` for a retina desktop and `"390x844x2,mobile,touch"` for a
   phone (`resize_page` floors near 500px). Close the page at the end.
3. **Walk the states.** Signed out (the landing, the sign-in), then signed in through the form as a
   member and as the admin: Memory, Sessions, Team, a session page, a search with no hits, a
   cross-workspace link. For each: a viewport screenshot, a full-page screenshot (jpeg, quality 50)
   and `list_console_messages` (a pre-sign-in 401 pair is expected; nothing else is).
4. **Both themes.** Click the theme toggle (or set `localStorage["csa-appearance"]` and reload). The
   landing follows the saved choice; the default is light.
5. **Compare to the design system** in `docs/design/palette.md` (tokens, type, the Ring) and to the
   neighbouring pages already built: a new page reuses their components, spacing and states.
6. **Measure, do not eyeball.** `evaluate_script` for: `document.documentElement.scrollWidth ===
   innerWidth` (no horizontal scroll), elements whose `getBoundingClientRect().right > innerWidth`,
   the canvas bitmap versus its CSS box (`canvas.width === Math.round(rect.width * devicePixelRatio)`),
   font sizes on inputs (16px on phones), tap-target heights.
7. **Reduced motion.** Reload with an `initScript` that stubs `matchMedia` for
   `prefers-reduced-motion` and counts `requestAnimationFrame`; dispatch pointer moves over the
   canvas; the count must stay 0.
8. **Network.** `list_network_requests` on the landing: only same-origin assets and the auth probe.

## Output

Screenshots attached to the PR, a list of what was adjusted, and what could not be verified.

Related: `docs/development/agent-workflow.md`, `docs/development/web-ui.md`, `docs/design/palette.md`.
