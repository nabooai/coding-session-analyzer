# Pre-finish checklist (agents must pass every item)

Run this before declaring any change done.

- [ ] **Build is green:** `cd ui && npm run build` succeeds — both `tsc --noEmit` (typecheck)
      and `vite build`. `ui/dist/index.html` + assets exist afterward.
- [ ] **No new runtime deps:** `package.json` still has only `vite` + `typescript`, both under
      `devDependencies`. `dependencies` is empty/absent. Any exception needs written justification.
- [ ] **All fetches relative:** grep confirms no hardcoded host/port —
      `grep -rn "127.0.0.1\|localhost\|:8766\|http://" ui/src ui/index.html` returns nothing.
      Every call goes to `/api/...`.
- [ ] **No secrets / no config sprawl:** no API keys, tokens, or env-specific hosts committed.
- [ ] **Types match the contract:** `src/api.ts` types still mirror `apiserver.py`
      (labels, sessions, session detail Fact shape, flag request/response). Contract unchanged.
- [ ] **Layering intact:** network only in `api.ts`; DOM/rendering only in `main.ts`.
- [ ] **Manual smoke of the 3 flows** (API running via `uv run python apiserver.py`, then
      `npm run dev`):
    1. Sessions list loads; a row links into a session.
    2. Session detail shows questions with their served facts (claim, evidence, type badge,
       project, current label).
    3. Flagging a `flaggable` fact `gold`/`wrong`/`unimportant` calls `POST /api/flag`, updates
       the shown label immediately on `{ok:true}`, and shows the error on `{ok:false}`.
- [ ] **No framework/lib creep:** no React/Vue/Svelte, router, state lib, CSS framework, HTTP
      lib, or test/CI/Docker added.
- [ ] **Touched only `ui/`:** no Python or other repo files changed.
