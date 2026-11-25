// Theme preference: Light / Dark / System. A per-viewer convenience, so it lives in localStorage
// (the auth-never-in-localStorage rule is about the SESSION; a colour preference is fine here) and
// is applied by stamping `data-theme` on the root element. "system" stamps nothing and lets the CSS
// `prefers-color-scheme` media query decide. The DEFAULT is light: the cream paper is the product's
// identity, dark is a choice. Pure mapping (themeAttr) is unit-tested; the DOM/storage wrappers are
// thin. An inline script in index.html applies the saved choice before first paint (no flash); this
// module keeps it in sync as the user toggles.

export type Theme = "light" | "dark" | "system";
// A new key: the old boot wrote its implicit "system" default on first visit, so a stored "system"
// under the old key says nothing about what the viewer chose. A stored light or dark there WAS a
// click, and is honoured until the toggle writes the new key.
const KEY = "csa-appearance";
const OLD_KEY = "csa-theme";

/** The value for the root's `data-theme` attribute, or null when the OS should decide (system). */
export function themeAttr(t: Theme): "light" | "dark" | null {
  return t === "system" ? null : t;
}

/** The saved preference, defaulting to "light". Never throws (private windows / blocked storage). */
export function getTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
    const old = localStorage.getItem(OLD_KEY);
    if (old === "light" || old === "dark") return old;
  } catch {
    /* storage blocked: fall through to the default */
  }
  return "light";
}

/** Apply a theme to the document and, when `persist`, remember it. Both operations are best-effort. */
export function applyTheme(t: Theme, persist = true): void {
  const attr = themeAttr(t);
  const root = document.documentElement;
  if (attr) root.setAttribute("data-theme", attr);
  else root.removeAttribute("data-theme");
  if (!persist) return;
  try {
    localStorage.setItem(KEY, t);
  } catch {
    /* storage blocked: the choice still applies for this page */
  }
}

/** Re-assert the saved choice on boot without writing it back: a default is not a choice. */
export function initTheme(): void {
  applyTheme(getTheme(), false);
}
