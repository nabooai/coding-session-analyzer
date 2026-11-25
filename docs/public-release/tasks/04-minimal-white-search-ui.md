# Task 04: Minimal white search UI

**Status:** open · **Owner:** UI + product · **Priority:** P0

## Objective

Make the installed product feel like one focused tool: white, clean, search-first, with no extra buttons or
screens competing with the core workflow.

## Required product shape

### Initial application route

- Pure white background; no cream theme, dark theme, theme toggle, sidebar or marketing navigation.
- CSA wordmark and one prominent search input.
- No dashboard cards, activity feed, entity explorer, team analytics, welcome tour or CTA collection.

### Search results

- Results appear only after a query.
- Each result shows the fact/knowledge, evidence, person, project and date.
- Person/project/date filters appear only when useful.
- A result opens source/session detail with evidence highlighted and a clear way back to search.
- Empty, loading, no-results and server-error states are plain and honest.

### Secondary routes

- One quiet account/settings affordance covers Connect, AI Models, members/tokens and administration.
- Sessions/Entities/Team/Analytics/Entity Types disappear from primary navigation. They may remain admin
  routes only if needed, never as additional product tabs.
- Old links either open the relevant source/settings route or redirect safely; no blank/dead routes.

## Implementation guidance

Prefer a small search shell around the existing API rather than further expanding `main.ts` (currently over
5,000 lines). Do not delete backend/admin capabilities merely to simplify navigation. Keep accessibility,
keyboard search, mobile layout and provenance intact.

Update product screenshots to use synthetic data only. Do not reuse images containing real team/client facts.

## Acceptance

- [ ] Initial signed-in screen contains only brand, search and a quiet account/settings control.
- [ ] Pure white light UI; no theme control or dark/cream variant in the release path.
- [ ] Search → result → evidence/source → back works on desktop and at 390px.
- [ ] No Memory/Sessions/Entities/Team/Connect/Admin tab row or sidebar is shown in the normal workflow.
- [ ] Model setup and connection remain reachable through the single settings path.
- [ ] Keyboard navigation, focus states, accessible labels and mobile overflow checks pass.
- [ ] No-results, failed model, offline server and unauthenticated states are clear and uncluttered.
- [ ] UI typecheck, lint, unit tests, build and browser E2E pass; screenshots are reviewed by a person.
- [ ] README shows the new UI, not the old multi-screen product.

## Merge requirement

Merge the UI and screenshot changes to `origin/master` together. Do not publish README images from an older UI.
