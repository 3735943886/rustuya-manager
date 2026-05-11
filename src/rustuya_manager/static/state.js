// Shared client state. The server is the source of truth: every WS frame
// rewrites `state.snapshot`, so the rest of the modules just re-render off it.
//
// `state` is a singleton object so that imports across modules see the same
// reference — re-assigning a primitive `let` would be invisible to other
// modules. Per-device expand/collapse is persisted in localStorage so a
// reload doesn't fold cards the user just opened.

export const state = {
  snapshot: null,
  filter: "all",
  query: "",
  sortKey: localStorage.getItem("sortKey") || "id",
};

export const expandedIds = new Set(
  JSON.parse(localStorage.getItem("expandedIds") || "[]")
);

export function saveExpanded() {
  localStorage.setItem("expandedIds", JSON.stringify([...expandedIds]));
}
