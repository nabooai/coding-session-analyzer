// Pure display logic for the sidebar cards — extracted from main.ts's
// DOM code so it is unit-testable (vitest, node env, no jsdom).

/** Sidebar team card: the org's human name when the control plane has one; honest fallbacks when not.
 *  The sub line only carries the org id when it adds information the name line doesn't.
 *
 *  The discriminator is "does the control plane have a NAME for this org", not "is the id `default`".
 *  Those came apart when the default corpus could be adopted as a named tenant: a real workspace whose
 *  id happens to be `default` was being labelled "Self-hosted / single tenant" to everyone in it.
 *  An unnamed `default` is still self-host and still says so. */
export function teamCard(orgId: string, orgName?: string): { name: string; sub: string } {
  const isDefault = orgId === "default";
  if (orgName) return { name: orgName, sub: isDefault ? "" : orgId };
  return isDefault ? { name: "Self-hosted", sub: "single tenant" } : { name: orgId, sub: "" };
}

/** Avatar initial for the signed-in user. */
export function initialOf(name?: string | null, email?: string | null): string {
  return (name || email || "?").trim().charAt(0).toUpperCase() || "?";
}
