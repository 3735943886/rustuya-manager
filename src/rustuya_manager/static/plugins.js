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

let manifest = [];
const stateSubs = new Set();
// id -> { rootEl, mounted } for each plugin page; "devices" is implicit.
const pages = new Map();
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

// The context handed to every plugin page's mount(rootEl, ctx).
function pluginCtx() {
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
      await mod.mount(page.rootEl, pluginCtx());
    } else {
      page.rootEl.textContent = `plugin "${id}" has no mount() export`;
    }
  } catch (e) {
    page.mounted = false;
    page.rootEl.textContent = `failed to load plugin "${id}": ${e.message}`;
    console.error("plugin mount failed", id, e);
  }
}

function buildTabBar() {
  const main = document.querySelector("main");
  if (!main) return;
  // The existing <section> children make up the device view; capture them so
  // we can show/hide them as a group when switching pages.
  deviceSections = Array.from(main.children);

  $tabs = document.createElement("nav");
  $tabs.id = "page-tabs";
  $tabs.className =
    "flex items-end gap-1 border-b border-slate-200 dark:border-slate-700 mb-1";

  const addTab = (page, label) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.page = page;
    btn.textContent = label;
    btn.className = tabClass(page === state.currentPage);
    btn.addEventListener("click", () => showPage(page));
    $tabs.appendChild(btn);
  };

  addTab("devices", "Devices");
  for (const p of manifest) addTab(p.id, p.label);

  main.insertBefore($tabs, main.firstChild);
  // deviceSections was captured before inserting the tab bar, so it's excluded.

  $pluginRoot = document.createElement("div");
  $pluginRoot.id = "plugin-page-root";
  $pluginRoot.className = "hidden";
  main.appendChild($pluginRoot);
}

export async function initPluginHost() {
  try {
    const res = await fetch("/api/plugins");
    if (!res.ok) return;
    manifest = await res.json();
  } catch {
    return; // host stays invisible on any boot error
  }
  if (!Array.isArray(manifest) || manifest.length === 0) return; // no tab bar
  buildTabBar();
}
