// Client-side plugin host. Boots from GET /api/plugins; with an empty manifest
// it touches nothing — no tab bar, no DOM changes — so a plugin-less UI is
// identical to before. Only when one or more plugins are present does it build
// a top tab bar ("Devices" + one tab per plugin) and lazily mount plugin pages.
//
// A plugin page module exports `mount(rootEl, ctx)`; `ctx` is the small,
// host-agnostic surface defined in pluginCtx() below.

import { state } from "./state.js";
import { render } from "./render.js";
import { toast } from "./dom.js";
import { confirm } from "./modal-confirm.js";
import { registerHeaderAction, renderActionsMenu, setHeaderAttention } from "./header-actions.js";
import { t, getLang, onLangChange } from "./i18n.js";

let manifest = [];
const stateSubs = new Set();
// id -> { rootEl, mounted } for each plugin page; "devices" is implicit.
const pages = new Map();
// Init-script URLs already imported, so a rescan only runs *new* ones (ES
// module imports are cached by URL anyway; this also skips the await + init()).
const importedInit = new Set();
let $tabs = null;
let $pluginRoot = null;
let deviceSections = [];

// Fan WS frames out to plugin onState() subscribers. Empty set ⇒ no-op, so this
// is free when no plugin is installed.
export function notifyPluginState(snapshot) {
  for (const cb of stateSubs) {
    try {
      cb(snapshot);
    } catch (e) {
      console.error("plugin onState handler threw", e);
    }
  }
}

// Map a plugin asset URL (/plugins/<id>/…) back to its plugin id, so a header
// action can default to scoping itself to that plugin's tab.
function pluginIdFromUrl(url) {
  const m = /^\/plugins\/([^/]+)\//.exec(url);
  return m ? m[1] : null;
}

// The context handed to a plugin's init(ctx) / page mount(rootEl, ctx).
// `defaultScope` is the scope an addHeaderAction gets when it doesn't set its
// own: a plugin's tab id (so its items live on its tab) for plugins that have a
// tab, or "global" for header-only plugins (no tab to live on).
function pluginCtx(opts = {}) {
  const defaultScope = opts.defaultScope;
  return {
    getState: () => state.snapshot,
    onState: (cb) => {
      stateSubs.add(cb);
      return () => stateSubs.delete(cb);
    },
    // Auth/JSON fetch wrapper. Browser Basic-auth creds (if any) ride along
    // automatically. JSON body is encoded; JSON responses are parsed; non-2xx
    // throws with the response text so plugins can try/catch.
    api: async (path, opts = {}) => {
      const init = { ...opts };
      if (init.body !== undefined && typeof init.body !== "string") {
        init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
        init.body = JSON.stringify(init.body);
      }
      const res = await fetch(path, init);
      const text = await res.text();
      if (!res.ok) throw new Error(text || `HTTP ${res.status}`);
      if (!text) return null;
      try {
        return JSON.parse(text);
      } catch {
        return text;
      }
    },
    toast,
    confirm,
    // Translate a key through the manager's i18n layer (for shared/built-in
    // keys). A plugin that ships its own strings typically keeps its own
    // dictionary and re-renders on onLangChange — see getLang below.
    t,
    // The active language code (e.g. "en", "ko"), so a plugin can pick its own
    // dictionary at mount time.
    getLang,
    // Subscribe to language switches (returns an unsubscribe fn). Fires after the
    // shell has switched, so a plugin can re-render its own JS-built UI in the
    // new language — applyDom() only re-localizes [data-i18n] nodes, not
    // imperatively built markup.
    onLangChange,
    // Contribute a hamburger-menu item through the same registry the built-in
    // actions use. Plugins default into the 200+ order band (after the app's
    // own items) and should namespace their `id` (e.g. "myplugin-thing") to
    // avoid clobbering a built-in. Unless the action sets its own `scope`, it
    // defaults to this plugin's tab (or "global" for header-only plugins) — pass
    // `scope: "global"` to show it on every tab. Re-renders the menu.
    addHeaderAction: (action) => {
      const scope = action.scope ?? defaultScope;
      registerHeaderAction({ order: 200, ...action, scope });
      renderActionsMenu();
    },
    // Flag (or clear) one of this plugin's own header items as needing notice:
    // an amber dot on the item and on the collapsed hamburger. Pass the same
    // `id` used in addHeaderAction. Re-renders the menu.
    setHeaderAttention: (id, on) => setHeaderAttention(id, on),
  };
}

function tabClass(active) {
  const base = "px-3 py-1.5 text-sm rounded-t border-b-2 -mb-px";
  return active
    ? `${base} border-slate-700 dark:border-slate-200 text-slate-900 dark:text-slate-100 font-medium`
    : `${base} border-transparent text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200`;
}

function updateTabStyles() {
  if (!$tabs) return;
  for (const btn of $tabs.querySelectorAll("button[data-page]")) {
    btn.className = tabClass(btn.dataset.page === state.currentPage);
  }
}

function showPage(page) {
  state.currentPage = page;
  updateTabStyles();
  // Per-tab header actions: an item with data-page-scope shows only on the tab
  // whose page id it names. "devices" = the manager's own view (manager-only),
  // "<pluginId>" = that plugin's tab. Global items carry no data-page-scope, so
  // they're never touched here and stay visible everywhere.
  for (const el of document.querySelectorAll("[data-page-scope]")) {
    el.classList.toggle("hidden", el.dataset.pageScope !== page);
  }
  if (page === "devices") {
    for (const el of deviceSections) el.classList.remove("hidden");
    $pluginRoot.classList.add("hidden");
    render();
  } else {
    for (const el of deviceSections) el.classList.add("hidden");
    $pluginRoot.classList.remove("hidden");
    mountPlugin(page);
  }
}

async function mountPlugin(id) {
  const entry = manifest.find((p) => p.id === id);
  if (!entry) return;
  let page = pages.get(id);
  if (!page) {
    const rootEl = document.createElement("div");
    rootEl.dataset.pluginPage = id;
    $pluginRoot.appendChild(rootEl);
    page = { rootEl, mounted: false };
    pages.set(id, page);
  }
  // Hide sibling plugin roots, show this one.
  for (const [pid, p] of pages) p.rootEl.classList.toggle("hidden", pid !== id);
  if (page.mounted) return;
  page.mounted = true;
  try {
    const mod = await import(entry.js_url);
    if (typeof mod.mount === "function") {
      // A page plugin's own tab id is its default header-action scope.
      await mod.mount(page.rootEl, pluginCtx({ pluginId: id, defaultScope: id }));
    } else {
      page.rootEl.textContent = t("plugins.noMount", { id });
    }
  } catch (e) {
    page.mounted = false;
    page.rootEl.textContent = t("plugins.loadFailed", { id, error: e.message });
    console.error("plugin mount failed", id, e);
  }
}

function addTab(page, label, i18nKey) {
  if ($tabs.querySelector(`button[data-page="${page}"]`)) return; // already present
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.page = page;
  // Tag the manager's own "Devices" tab with its i18n key so a later language
  // switch (applyDom) re-localizes it; plugin tabs carry their manifest label.
  if (i18nKey) btn.dataset.i18n = i18nKey;
  btn.textContent = label;
  btn.className = tabClass(page === state.currentPage);
  btn.addEventListener("click", () => showPage(page));
  $tabs.appendChild(btn);
}

function buildTabBarShell() {
  const main = document.querySelector("main");
  if (!main) return;
  // The existing <section> children make up the device view; capture them so
  // we can show/hide them as a group when switching pages.
  deviceSections = Array.from(main.children);

  $tabs = document.createElement("nav");
  $tabs.id = "page-tabs";
  // Live as a full-width second row INSIDE the sticky header, flush to its
  // bottom border, so the tab strip stays pinned under the title/badge/menu
  // row instead of scrolling away with the device list. `w-full` makes it wrap
  // onto its own line within the header's flex row; `-mb-3` cancels the header
  // container's bottom padding so the active tab's -mb-px connects to the
  // header's existing border-b. Falls back to the top of <main> if the header
  // shell isn't the shape we expect.
  const headerRow = document.querySelector("header > div");
  if (headerRow) {
    $tabs.className =
      "flex items-end gap-1 w-full -mb-3 pt-1 border-b border-slate-200 dark:border-slate-700";
    addTab("devices", t("tabs.devices"), "tabs.devices");
    headerRow.appendChild($tabs);
  } else {
    $tabs.className = "flex items-end gap-1 border-b border-slate-200 dark:border-slate-700 mb-1";
    addTab("devices", t("tabs.devices"), "tabs.devices");
    main.insertBefore($tabs, main.firstChild);
  }

  $pluginRoot = document.createElement("div");
  $pluginRoot.id = "plugin-page-root";
  $pluginRoot.className = "hidden";
  main.appendChild($pluginRoot);
}

// Ensure the tab bar exists (once any plugin page exists) and add a tab for any
// page not already shown — safe to call repeatedly after a rescan.
function syncTabs() {
  if (manifest.length === 0) return;
  if (!$tabs) buildTabBarShell();
  for (const p of manifest) addTab(p.id, p.label);
}

// Import any not-yet-imported init modules and run their init(ctx). Each runs in
// isolation — one failing module doesn't block the others or the tab bar.
async function loadInitScripts(urls) {
  for (const url of urls) {
    if (importedInit.has(url)) continue;
    importedInit.add(url);
    // Header actions from this init default to the plugin's own tab when it has
    // one; header-only plugins (no page) default to global.
    const pluginId = pluginIdFromUrl(url);
    const hasTab = manifest.some((p) => p.id === pluginId);
    const defaultScope = hasTab ? pluginId : "global";
    try {
      const mod = await import(url);
      if (typeof mod.init === "function") await mod.init(pluginCtx({ pluginId, defaultScope }));
    } catch (e) {
      console.error("plugin init script failed", url, e);
    }
  }
}

// Apply a /api/plugins manifest incrementally: update the page list, run any new
// init scripts, and add any new tabs. Idempotent — re-applying the same manifest
// is a no-op, so this backs both the initial boot and a later rescan.
async function applyManifest(data) {
  manifest = Array.isArray(data?.pages) ? data.pages : [];
  const initScripts = Array.isArray(data?.init_scripts) ? data.init_scripts : [];
  await loadInitScripts(initScripts);
  syncTabs();
}

export async function initPluginHost() {
  let data;
  try {
    const res = await fetch("/api/plugins");
    if (!res.ok) return;
    data = await res.json();
  } catch {
    return; // host stays invisible on any boot error
  }
  await applyManifest(data);
}
