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
import { initPluginHost } from "./plugins.js";
import { registerHeaderAction, renderActionsMenu } from "./header-actions.js";

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
    toast(`Refreshed — bridge ${bridgeCount}, cloud ${cloudCount}`, "ok");
  } catch (e) {
    toast(`Refresh failed: ${e.message}`, "error");
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
      toast(`Scan complete — ${result.count} device${result.count === 1 ? "" : "s"} seen on LAN`, "ok");
    } else {
      toast(`Scan failed: ${result.error || "unknown"}`, "error");
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
    toast("waiting for bridge state…", "error");
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
    title: "Reconfigure bridge",
    message:
      "The bridge will re-read its configuration and briefly disconnect. " +
      "Devices are not removed.",
    okLabel: "Reconfigure",
  });
  if (!ok) return;
  await publishCommand({ action: "reconfigure", id: "bridge" });
}

// Built-in items — ids/scopes/order preserved so the menu (and the e2e suite)
// looks and behaves exactly as before.
registerHeaderAction({ id: "device-add-btn", iconHtml: "+", labelHtml: "Add device", scope: "devices", order: 10, onClick: doAddDevice });
registerHeaderAction({ id: "wizard-header-btn", iconHtml: "☁", labelHtml: "Fetch from cloud", order: 20, onClick: openWizardModal });
registerHeaderAction({ id: "scan-btn", iconHtml: "📡", labelHtml: "Scan LAN", scope: "devices", order: 30, onClick: doScan });
registerHeaderAction({
  id: "theme-btn",
  iconHtml: `<span class="dark:hidden">🌙</span><span class="hidden dark:inline">☀</span>`,
  labelHtml: `<span class="dark:hidden">Dark mode</span><span class="hidden dark:inline">Light mode</span>`,
  order: 40,
  onClick: doThemeToggle,
});
registerHeaderAction({ id: "refresh-btn", iconHtml: "⟳", labelHtml: "Refresh", scope: "devices", order: 50, onClick: doRefresh });
registerHeaderAction({
  id: "reconfigure-btn",
  iconHtml: "🔧",
  labelHtml: "Reconfigure bridge",
  scope: "devices",
  order: 100,
  dividerBefore: true,
  danger: true,
  title: "Tell the bridge to re-read its config and restart",
  onClick: doReconfigure,
});
renderActionsMenu();

// Re-render the "Xs ago" labels every 5 seconds without a full re-render.
setInterval(() => {
  for (const el of document.querySelectorAll("[data-lastseen]")) {
    el.textContent = formatAgo(Number(el.dataset.lastseen));
  }
}, 5000);

// ── Actions menu dismiss ─────────────────────────────────────────────────────
// Buttons are wired per-item by the registry; here we just close the dropdown
// after any item click (delegated, so it covers registry- and plugin-added
// items alike).
const $actionsMenu = document.getElementById("actions-menu");
$actionsMenu?.addEventListener("click", (e) => {
  if (e.target.closest("button")) $actionsMenu.removeAttribute("open");
});

// ── Init modals + open the socket ──────────────────────────────────────────
initSyncModal();
initWizardModal();
initDeviceModal();
initConfirmModal();
connect();
// Boot the plugin host. No-op (no tab bar, no DOM change) when no plugins
// are installed — GET /api/plugins returns [].
initPluginHost();
