// Unified header-action registry.
//
// Both the app's own hamburger items (Add device, Scan, Refresh, …) and any
// plugin-contributed items register here; the host renders the #actions-menu
// dropdown from this one list, sorted by `order`. A plugin adds a menu item the
// exact same way the app declares its own — no second code path (DRY).
//
// An action is:
//   { id, labelHtml, iconHtml, scope, order, dividerBefore, danger, title, onClick }
//     id            stable element id (built-ins keep their original ids so the
//                   e2e suite and any external selectors stay valid)
//     labelHtml     text/HTML for the label (HTML allowed: the theme toggle uses
//                   dark:/light: spans)
//     iconHtml      text/HTML for the leading icon glyph
//     scope         which tab(s) the item shows on (symmetric for manager + plugins):
//                     "global" / undefined → every tab
//                     "devices"            → the manager's Devices view only
//                     "<pluginId>"         → that plugin's tab only
//                   Any non-global scope sets data-page-scope=<scope>; plugins.js
//                   shows the item only when the current page id matches.
//     order         sort key (built-ins 10..110; plugins default 200+)
//     dividerBefore insert a separator above this item
//     danger        amber styling (used by Reconfigure / Restart)
//     title         tooltip
//     onClick       (ev, btn) => void

import { state } from "./state.js";

const actions = [];

const ITEM_CLS =
  "w-full px-3 py-2 flex items-center gap-3 hover:bg-slate-100 dark:hover:bg-slate-700 rounded text-sm text-left";
const DANGER_CLS =
  "w-full px-3 py-2 flex items-center gap-3 hover:bg-amber-50 dark:hover:bg-amber-900/30 text-amber-700 dark:text-amber-300 rounded text-sm text-left";

// Register (or replace, by id) a header action. Idempotent on id so a plugin
// re-init can't double-insert. Does not re-render — call renderActionsMenu()
// after a batch of registrations.
export function registerHeaderAction(action) {
  if (!action || !action.id) return;
  const next = { order: 100, ...action };
  const i = actions.findIndex((a) => a.id === action.id);
  if (i >= 0) actions[i] = next;
  else actions.push(next);
}

// Drop every registered action whose id matches `predicate`. Used by dynamic
// groups (e.g. the collapsible language submenu) that need to remove their
// items, not just replace them — registerHeaderAction can only add/replace by
// id, so a collapse that registers fewer items would otherwise leave stale
// rows behind. Does not re-render — call renderActionsMenu() after.
export function unregisterHeaderActions(predicate) {
  for (let i = actions.length - 1; i >= 0; i--) {
    if (predicate(actions[i].id)) actions.splice(i, 1);
  }
}

// (Re)render the dropdown inside #actions-menu from the registry.
export function renderActionsMenu() {
  const menu = document.getElementById("actions-menu");
  if (!menu) return;
  const panel = menu.querySelector("[data-actions-panel]");
  if (!panel) return;
  panel.innerHTML = "";
  const sorted = [...actions].sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
  for (const a of sorted) {
    if (a.dividerBefore) {
      const div = document.createElement("div");
      div.className = "my-1 border-t border-slate-200 dark:border-slate-700";
      panel.appendChild(div);
    }
    const btn = document.createElement("button");
    btn.id = a.id;
    btn.type = "button";
    btn.className = a.danger ? DANGER_CLS : ITEM_CLS;
    // Non-global scope → tag with data-page-scope and hide unless we're already
    // on the matching page (showPage re-applies this on every tab switch).
    const scope = a.scope && a.scope !== "global" ? a.scope : null;
    if (scope) {
      btn.dataset.pageScope = scope;
      if (scope !== (state.currentPage || "devices")) btn.classList.add("hidden");
    }
    if (a.title) btn.title = a.title;
    // `keepOpen` items (the language submenu toggle) don't dismiss the dropdown
    // on click — the dismiss handler in app.js checks for this marker.
    if (a.keepOpen) btn.dataset.keepOpen = "";
    btn.innerHTML =
      `<span class="w-5 text-center">${a.iconHtml || ""}</span>` +
      `<span>${a.labelHtml || ""}</span>`;
    if (typeof a.onClick === "function") {
      btn.addEventListener("click", (ev) => a.onClick(ev, btn));
    }
    panel.appendChild(btn);
  }
}
