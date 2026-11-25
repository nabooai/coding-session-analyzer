# Conventions

Small, boring rules that keep this app small and boring (ponytail).

## Types & the API contract
- **`src/api.ts` is the single source of truth for API types.** They mirror `apiserver.py`
  exactly. Import types from `api.ts`; never redefine a contract type elsewhere.
- Changing a type means the API contract changed — confirm against `apiserver.py` first.

## Layering
- **`api.ts` = network only.** Typed `fetch` wrappers, JSON in/out. No DOM, no rendering,
  no app/business logic.
- **`main.ts` = the app.** DOM construction, rendering, the hash router, event handlers.
- **`style.css` = presentation.** No CSS-in-JS, no framework.

## Code style
- Keep functions small and single-purpose (one view / one card / one control each).
- Build DOM with the `el()` helper. Avoid `innerHTML`; put user-provided text (`claim`,
  `evidence`, `question`, session ids) through `textContent` — it stays XSS-safe by default.
- `naming`: camelCase for vars/functions, PascalCase for types/interfaces, kebab-case for
  CSS classes and file names.
- Prefer editing an existing file over creating a new one. New files need a reason.

## Networking
- **Relative URLs only** (`/api/...`). Never hardcode a host or port — dev uses the Vite proxy,
  prod is same-origin behind `apiserver.py`.

## Dependencies
- Runtime deps: **none.** Tooling: `vite` + `typescript` only, as devDependencies.
- No framework, router, state lib, HTTP lib, CSS framework, test framework, CI, or Docker.
  Adding any dependency requires a written justification in the PR/commit.
