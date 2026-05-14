// Shared client state. The server is the source of truth: every WS frame
// rewrites `state.snapshot`, so the rest of the modules just re-render off it.
//
// `state` is a singleton object so that imports across modules see the same
// reference — re-assigning a primitive `let` would be invisible to other
// modules. Per-device expand/collapse is persisted in localStorage so a
// reload doesn't fold cards the user just opened.

// Whitelist the persisted sortKey so users carrying retired values
// ("type", "last_seen") in localStorage fall back cleanly to "id" instead
// of getting a no-op select.
const VALID_SORT_KEYS = new Set(["id", "name", "status", "category"]);
const savedSortKey = localStorage.getItem("sortKey");

// Filter is a facet-style multi-toggle: each real sync class is independently
// on/off. The "all" button is a master toggle — flips between "all on" and
// "all off"; when every category is on it reads as active. "ungrouped" is
// the no-cloud state and always shows — it isn't a togglable category.
// An empty filter set is a legitimate "show nothing" state (the empty-state
// pane explains how to recover), so we persist it as-is rather than
// snapping back to defaults.
export const ALL_CATEGORIES = ["missing", "orphan", "mismatch", "synced"];
const savedFilters = (() => {
  try {
    const arr = JSON.parse(localStorage.getItem("filters") || "null");
    if (!Array.isArray(arr)) return null;
    return arr.filter((f) => ALL_CATEGORIES.includes(f));
  } catch {
    return null;
  }
})();

export const state = {
  snapshot: null,
  filters: new Set(savedFilters || ALL_CATEGORIES),
  query: "",
  sortKey: VALID_SORT_KEYS.has(savedSortKey) ? savedSortKey : "id",
};

export function saveFilters() {
  localStorage.setItem("filters", JSON.stringify([...state.filters]));
}

export const expandedIds = new Set(
  JSON.parse(localStorage.getItem("expandedIds") || "[]")
);

export function saveExpanded() {
  localStorage.setItem("expandedIds", JSON.stringify([...expandedIds]));
}
