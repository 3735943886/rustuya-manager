// rustuya-manager web client — entry module.
//
// The server is the source of truth: every WS frame carries the full state,
// so we just re-render. With dozens of devices this is well under a
// millisecond and keeps the client logic flat — no diffing, no patching.
//
// Implementation lives in sibling modules:
//   state.js         shared mutable snapshot
//   dom.js           pure helpers (escape, format, icon factories, toast)
//   api.js           POST /api/command + /api/cloud helpers
//   ws.js            WebSocket connection + connection-state badge
//   cards.js         per-device card renderers
//   render.js        top-level renderer + tree/filter/sort
//   modal-sync.js    bulk-sync modal
//   modal-wizard.js  Tuya cloud login flow

import { state, ALL_CATEGORIES, saveFilters } from "./state.js";
import { formatAgo, toast } from "./dom.js";
import { uploadCloud, postCommand, postScan, publishCommand } from "./api.js";
import { connect } from "./ws.js";
import { render, renderDevices, renderFilterCounts } from "./render.js";
import { initSyncModal } from "./modal-sync.js";
import { initWizardModal, openWizardModal } from "./modal-wizard.js";
import { initDeviceModal, openAddModal } from "./modal-device.js";
import { initConfirmModal, confirm } from "./modal-confirm.js";
import { initPluginsModal, openPluginsModal } from "./modal-plugins.js";
import { initLogModal, openLogModal } from "./modal-log.js";
import { initPluginHost, scanForPlugins } from "./plugins.js";
import { registerHeaderAction, unregisterHeaderActions, renderActionsMenu } from "./header-actions.js";
import { initI18n, applyDom, t, getLocales, getLocaleName, getLang, setLang } from "./i18n.js";

// ── Cloud upload (drop zone + file picker) ─────────────────────────────────
const $dropzone = document.getElementById("cloud-dropzone");
const $pickBtn = document.getElementById("cloud-pick-btn");
const $fileInput = document.getElementById("cloud-file-input");

$pickBtn?.addEventListener("click", (e) => { e.preventDefault(); $fileInput.click(); });
$fileInput?.addEventListener("change", (ev) => {
  const file = ev.target.files?.[0];
  if (file) uploadCloud(file);
  ev.target.value = "";
});
if ($dropzone) {
  ["dragenter", "dragover"].forEach((evt) =>
    $dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      $dropzone.classList.add("ring-2", "ring-amber-400");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    $dropzone.addEventListener(evt, () =>
      $dropzone.classList.remove("ring-2", "ring-amber-400")
    )
  );
  $dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) uploadCloud(file);
  });
}

// ── Filter / search / sort wiring ──────────────────────────────────────────
const $filterTabs = document.getElementById("filter-tabs");
const $search = document.getElementById("search-input");
const $sort = document.getElementById("sort-select");

$filterTabs.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-filter]");
  if (!btn) return;
  const key = btn.dataset.filter;
  if (key === "all") {
    // Master toggle: all-on flips to all-off, anything else (partial or
    // all-off) flips to all-on. Snap-back-on-empty used to be here; it
    // overrode legitimate "hide everything" intent. The empty-state pane
    // now guides recovery.
    const allOn = ALL_CATEGORIES.every((c) => state.filters.has(c));
    state.filters = new Set(allOn ? [] : ALL_CATEGORIES);
  } else {
    if (state.filters.has(key)) state.filters.delete(key);
    else state.filters.add(key);
  }
  saveFilters();
  if (state.snapshot) renderFilterCounts();   // reapply active/idle styles
  renderDevices();
});

// Custom clear button. Visibility is driven from the input value so it
// shows up only when there's something to clear; the click handler also
// re-focuses the input so the user keeps typing momentum.
const $searchClear = document.getElementById("search-clear");
function syncSearchClear() {
  $searchClear?.classList.toggle("hidden", !$search.value);
}

$search.addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  syncSearchClear();
  renderDevices();
});

$searchClear?.addEventListener("click", () => {
  $search.value = "";
  state.query = "";
  syncSearchClear();
  renderDevices();
  $search.focus();
});

// `/` focuses search, ESC clears
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $search) {
    e.preventDefault();
    $search.focus();
  } else if (e.key === "Escape" && document.activeElement === $search) {
    $search.value = "";
    state.query = "";
    syncSearchClear();
    renderDevices();
  }
});

$sort.value = state.sortKey;
$sort.addEventListener("change", (e) => {
  state.sortKey = e.target.value;
  localStorage.setItem("sortKey", state.sortKey);
  renderDevices();
});

// ── Header actions (unified registry) ───────────────────────────────────────
// The hamburger dropdown is rendered from header-actions.js. The built-ins are
// registered here; plugins add their own via ctx.addHeaderAction (plugins.js)
// into the same registry — one code path for both. Handlers receive (ev, btn)
// so per-action UI (e.g. disabling during a fetch) works without a pre-fetched
// element reference (the buttons are created by renderActionsMenu).

// Refresh — pull a fresh snapshot and re-ask the bridge for its status. Keep the
// label stable (it usually completes <100ms); disabled is enough feedback.
async function doRefresh(_ev, btn) {
  btn.disabled = true;
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.snapshot = await res.json();
    render();
    await postCommand({ action: "status", id: "bridge" });
    const snap = state.snapshot;
    const bridgeCount = Object.keys(snap.bridge).length;
    const cloudCount = Object.keys(snap.cloud).length;
    toast(t("toast.refreshed", { bridge: bridgeCount, cloud: cloudCount }), "ok");
  } catch (e) {
    toast(t("toast.refreshFailed", { error: e.message }), "error");
  } finally {
    btn.disabled = false;
  }
}

// Scan — runs the bridge LAN scan through the server-side coordinator
// (POST /api/scan). The endpoint awaits the full drain (~18s typical, 20s max)
// and returns once sightings land on state.scan_results, so the next WS
// snapshot already reflects what the bridge saw. Devices registered with a
// pinned IP whose sighting drifted get an ERR_STATE 906 (surfaced in MSG);
// cloud-only devices that answered show up in scan_results for the card
// renderer to highlight.
async function doScan(_ev, btn) {
  btn.disabled = true;
  try {
    const result = await postScan();
    if (result.ok) {
      const unit = t(result.count === 1 ? "unit.device" : "unit.devices");
      toast(t("toast.scanComplete", { count: result.count, unit }), "ok");
    } else {
      toast(t("toast.scanFailed", { error: result.error || t("common.unknown") }), "error");
    }
  } finally {
    btn.disabled = false;
  }
}

// Theme toggle — flips the `dark` class on <html> and persists the choice. The
// initial application happens inline in <head> to avoid FOUC.
function doThemeToggle() {
  const dark = document.documentElement.classList.toggle("dark");
  localStorage.setItem("theme", dark ? "dark" : "light");
}

function doAddDevice() {
  if (!state.snapshot) {
    toast(t("toast.waitingBridge"), "error");
    return;
  }
  openAddModal();
}

// Reconfigure: tell the bridge to re-read its config and restart. Guarded by a
// confirm because it briefly disconnects the bridge (devices are not removed).
// `id: "bridge"` matches the bridge's command contract; the manager handles the
// resulting bridge/config clear + re-resolve, and an embedded bridge respawns.
async function doReconfigure() {
  const ok = await confirm({
    title: t("confirm.reconfigureTitle"),
    message: t("confirm.reconfigureMsg"),
    okLabel: t("confirm.reconfigureOk"),
  });
  if (!ok) return;
  await publishCommand({ action: "reconfigure", id: "bridge" });
}

// Load plugins newly dropped into the server's plugin dir (add-only, no restart).
async function doLoadNewPlugins() {
  await scanForPlugins();
}

// Restart the manager process in place — the "full reload" that picks up edited
// or removed plugins. Confirm-guarded: it briefly drops the WS (auto-reconnects)
// and restarts an embedded bridge.
async function doRestartManager() {
  const ok = await confirm({
    title: t("confirm.restartTitle"),
    message: t("confirm.restartMsg"),
    okLabel: t("confirm.restartOk"),
    danger: true,
  });
  if (!ok) return;
  try {
    const res = await fetch("/api/restart", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast(t("toast.restarting"), "ok");
  } catch (e) {
    toast(t("toast.restartFailed", { error: e.message }), "error");
  }
}

// Switch to a specific locale, then re-localize everything. No-op if it's
// already active. setLang() swaps the active dictionary + applyDom()s the static
// markup; applyI18n() then re-renders the JS-built surfaces (menu, cards, …).
async function selectLanguage(code) {
  if (code === getLang()) return;
  await setLang(code);
  applyI18n();
}

// Collapsed by default so the menu stays one row tall no matter how many
// locales ship; the toggle expands an indented, checkmarked sub-list in place.
let langMenuOpen = false;

// Render the language picker as a collapsible submenu: a single "🌐 Language ▸"
// row that expands an indented list of locales (active one ✓) on click. Keeps
// the hamburger short as languages grow. Re-runnable — it clears its own rows
// (parent + options) each time so a collapse leaves nothing stale behind.
//
// Scope is "global": this is the single app-wide language control, shown on
// every tab (manager and plugin pages alike). Switching it sets the shell's
// language and, via setLang() → langSubs, notifies plugins through
// ctx.onLangChange; a plugin follows the shell's language (falling back to
// English if it doesn't ship it) rather than carrying its own picker, so there
// is one switcher for the whole UI, not one per tab.
function registerLanguageActions() {
  unregisterHeaderActions((id) => id === "lang-toggle" || id.startsWith("lang-opt-"));
  const locales = getLocales();
  if (locales.length < 2) return; // nothing to choose → no picker at all

  const caret = `<span class="ml-1 text-xs text-slate-400 dark:text-slate-500">${langMenuOpen ? "▾" : "▸"}</span>`;
  registerHeaderAction({
    id: "lang-toggle",
    iconHtml: "🌐",
    labelHtml: `${t("header.language")}${caret}`,
    scope: "global",
    order: 45,
    dividerBefore: true,
    keepOpen: true, // expanding/collapsing must not dismiss the dropdown
    onClick: () => {
      langMenuOpen = !langMenuOpen;
      registerLanguageActions();
      renderActionsMenu();
    },
  });
  if (!langMenuOpen) return;

  const active = getLang();
  locales.forEach((code, i) => {
    const check = `<span class="w-3 text-center text-emerald-600 dark:text-emerald-400">${code === active ? "✓" : ""}</span>`;
    registerHeaderAction({
      id: `lang-opt-${code}`,
      iconHtml: "",
      // Indent under the toggle so the list reads as a nested group.
      labelHtml: `<span class="inline-flex items-center gap-1.5 pl-4">${check}${getLocaleName(code)}</span>`,
      scope: "global",
      order: 45 + (i + 1) * 0.01,
      onClick: () => selectLanguage(code),
    });
  });
}

// Built-in items — ids/order preserved so the menu (and the e2e suite) looks and
// behaves as before. `scope` is explicit so the manager's own actions split the
// same way plugins' do: "devices" = manager's Devices view only; "global" = every
// tab. Device-specific actions are manager-only; process/app-level ones global.
// Labels/titles come from the i18n layer; this is wrapped in a function so a
// language switch can re-register the built-ins with freshly translated text.
function registerBuiltinActions() {
  registerHeaderAction({ id: "device-add-btn", iconHtml: "+", labelHtml: t("header.addDevice"), scope: "devices", order: 10, onClick: doAddDevice });
  registerHeaderAction({ id: "wizard-header-btn", iconHtml: "☁", labelHtml: t("header.fetchCloud"), scope: "global", order: 20, onClick: openWizardModal });
  registerHeaderAction({ id: "scan-btn", iconHtml: "📡", labelHtml: t("header.scanLan"), scope: "devices", order: 30, onClick: doScan });
  registerHeaderAction({
    id: "theme-btn",
    iconHtml: `<span class="dark:hidden">🌙</span><span class="hidden dark:inline">☀</span>`,
    labelHtml: `<span class="dark:hidden">${t("header.darkMode")}</span><span class="hidden dark:inline">${t("header.lightMode")}</span>`,
    scope: "global",
    order: 40,
    onClick: doThemeToggle,
  });
  registerLanguageActions();
  registerHeaderAction({ id: "refresh-btn", iconHtml: "⟳", labelHtml: t("header.refresh"), scope: "devices", order: 50, onClick: doRefresh });
  registerHeaderAction({ id: "manage-plugins-btn", iconHtml: "🧩", labelHtml: t("header.managePlugins"), scope: "global", order: 55, title: t("header.managePluginsTitle"), onClick: openPluginsModal });
  registerHeaderAction({ id: "plugin-scan-btn", iconHtml: "📂", labelHtml: t("header.loadPlugins"), scope: "global", order: 60, title: t("header.loadPluginsTitle"), onClick: doLoadNewPlugins });
  registerHeaderAction({ id: "log-btn", iconHtml: "🔔", labelHtml: t("header.log"), scope: "global", order: 65, title: t("header.logTitle"), onClick: openLogModal });
  registerHeaderAction({
    id: "reconfigure-btn",
    iconHtml: "🔧",
    labelHtml: t("header.reconfigure"),
    scope: "devices",
    order: 100,
    dividerBefore: true,
    danger: true,
    title: t("header.reconfigureTitle"),
    onClick: doReconfigure,
  });
  registerHeaderAction({
    id: "restart-btn",
    iconHtml: "♻",
    labelHtml: t("header.restart"),
    scope: "global",
    order: 110,
    danger: true,
    title: t("header.restartTitle"),
    onClick: doRestartManager,
  });
}

// Re-localize everything after a language switch. setLang() already ran
// applyDom() over the static markup; here we refresh the surfaces built in JS:
// re-register the header items with translated labels, re-render the menu, and
// re-render the device view (cheap — the snapshot drives a full re-render
// anyway). Plugin-contributed menu items keep their own labels.
function applyI18n() {
  applyDom();
  registerBuiltinActions();
  renderActionsMenu();
  render();
}

// Re-render the "Xs ago" labels every 5 seconds without a full re-render.
setInterval(() => {
  for (const el of document.querySelectorAll("[data-lastseen]")) {
    el.textContent = formatAgo(Number(el.dataset.lastseen));
  }
}, 5000);

// ── Actions menu dismiss ─────────────────────────────────────────────────────
// Buttons are wired per-item by the registry; here we just close the dropdown
// after any item click (delegated, so it covers registry- and plugin-added
// items alike). The language submenu toggle opts out via [data-keep-open] so
// expanding/collapsing the locale list doesn't dismiss the whole menu.
const $actionsMenu = document.getElementById("actions-menu");
$actionsMenu?.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (btn && !btn.closest("[data-keep-open]")) $actionsMenu.removeAttribute("open");
});

// A hamburger menu is a transient popover, not a kept-open accordion: dismiss
// it when the user clicks anywhere outside it or presses Escape. The summary
// (the hamburger button itself) lives inside #actions-menu, so contains() keeps
// the document handler from fighting the native open-toggle on that click.
//
// Runs in the CAPTURE phase, on purpose: an in-menu click can re-render the
// panel (e.g. expanding the language submenu calls renderActionsMenu()), which
// detaches the clicked button before the bubble phase reaches us — at which
// point contains() would wrongly report it as "outside" and close the menu.
// Capturing evaluates contains() against the still-attached original target,
// before any such re-render.
document.addEventListener(
  "click",
  (e) => {
    if ($actionsMenu?.hasAttribute("open") && !$actionsMenu.contains(e.target))
      $actionsMenu.removeAttribute("open");
  },
  true,
);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $actionsMenu?.removeAttribute("open");
});

// ── Boot ─────────────────────────────────────────────────────────────────
// i18n loads first so every label / toast / render below resolves in the
// chosen language; it's best-effort (falls back to English / raw keys) and
// never blocks the boot. The static markup is already English, so the brief
// moment before applyDom() runs is at worst an un-translated flash.
async function boot() {
  await initI18n();
  applyDom();                  // localize the static markup in index.html
  registerBuiltinActions();    // header items with translated labels
  renderActionsMenu();

  initSyncModal();
  initWizardModal();
  initDeviceModal();
  initConfirmModal();
  initPluginsModal();
  initLogModal();
  connect();
  // Boot the plugin host. No-op (no tab bar, no DOM change) when no plugins
  // are installed — GET /api/plugins returns [].
  initPluginHost();
}

boot();
