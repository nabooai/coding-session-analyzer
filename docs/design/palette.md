# CSA design system

Decided on 2026-09-04 and 05 from the redesign mocks. The tokens below are the source of truth; `server/ui/src/style.css` defines
them at the top of the file and `tokens.json` beside this page carries them for any other consumer.

## Identity

- **Paper, not a dashboard.** Cream paper, a serif for the wordmark and page titles, a sans for
  everything else. Light is the default; dark is a toggle designed with the same care.
- **Ink accent, no hue.** Buttons, active tabs and links are the ink. The graph's node palette is the
  only colour on a page. Semantic good / warn / bad appear only in deltas and states.
- **The Ring.** A filled circle inside a thin ring, one colour (the accent). The wordmark, the
  favicon, the hovered session in the graph.
- **Copy.** No em or en dashes (a period or a middle dot), no slogans, no feature tiles, real product
  content in every screenshot, fictional names on public pages.

## Tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#f8f5ee` | `#1b1a17` | page background |
| `--card` | `#fffdf8` | `#23221e` | cards, the top bar |
| `--line` | `#e4dfd3` | `#37352f` | borders |
| `--line-soft` | `#ece8dd` | `#2e2c27` | inner dividers |
| `--muted` | `#726d62` | `#a59e90` | secondary text |
| `--ink` | `#1b1d22` | `#ece7db` | text |
| `--accent` | `#1b1d22` | `#ece7db` | buttons, active tab, focus ring (= ink) |
| `--accent-soft` | `#e9e6de` | `#33312c` | chips, hover fills |
| `--on-accent` | `#f8f5ee` | `#1b1a17` | text on the accent |
| `--edge` | `#cfc9bd` | `#4a4740` | graph edges |
| `--n-grey` | `#d5d0c5` | `#45423b` | graph scenery nodes |
| `--good` | `#3f8a55` | `#7fb98f` | positive delta or state only |
| `--warn` | `#9a6d12` | `#d3a54a` | warning state only |
| `--bad` | `#b4433c` | `#e0837a` | error state only |

Node palette (the same in both themes): session `#8f84d8`, decision `#5aa46a`, gotcha `#ef8a3f`,
constraint `#4f8fe6`, todo `#e6c235`, preference `#e87ba5`, rule `#7f9b8c`.

Type chips are the node colour mixed 18 to 28 percent into the card (`color-mix`), text in ink.

## Type

- Display: **Source Serif 4** 600 and 700 (wordmark, page titles, landing headlines, card titles).
- Body: **Inter** 400, 500, 600. Base 15px / 1.5. Tabular numerals on metrics.
- Mono: the system monospace stack (code literals, eyebrows).
- Both faces are vendored as latin woff2 under `server/ui/public/fonts/` (OFL). No Google Fonts request.

## Shapes and spacing

- Radius: 10px controls, 12px cards, 14px framed panels, pills for buttons and scope controls.
- The top bar is 60px and sticky (`--nav-h`); every sticky or jump-to offset adds to it.
- Content column max 1240px; desktop-first app (1024px and up); landing and sign-in responsive to 390px.
- Focus: a 2px accent outline, offset 2px, on every interactive element.

## Where it lives

- `server/ui/src/style.css`: tokens at the top, then the shell, then components.
- `server/ui/src/graph.ts`: reads the tokens at draw time (`cssColors`), so the canvas follows the theme.
- `server/ui/src/theme.ts`: light / dark / system, saved under `csa-appearance` only when toggled.
- `server/ui/public/favicon.svg` and `ringMark()` in `main.ts`: the Ring.
