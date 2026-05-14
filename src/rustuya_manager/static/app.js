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
import { uploadCloud, postCommand } from "./api.js";
import { connect } from "./ws.js";
import { render, renderDevices, renderFilterCounts } from "./render.js";
import { initSyncModal } from "./modal-sync.js";
import { initWizardModal } from "./modal-wizard.js";
import { initDeviceModal, openAddModal } from "./modal-device.js";
import { initConfirmModal } from "./modal-confirm.js";

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
    // "all" is a reset, not a toggle — always end up with every category on.
    state.filters = new Set(ALL_CATEGORIES);
  } else {
    if (state.filters.has(key)) state.filters.delete(key);
    else state.filters.add(key);
    // Empty filter would hide every card, which reads as a broken UI. Snap
    // back to all-on so the user always sees *something*.
    if (state.filters.size === 0) state.filters = new Set(ALL_CATEGORIES);
  }
  saveFilters();
  if (state.snapshot) renderFilterCounts();   // reapply active/idle styles
  renderDevices();
});

$search.addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  renderDevices();
});

// `/` focuses search, ESC clears
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $search) {
    e.preventDefault();
    $search.focus();
  } else if (e.key === "Escape" && document.activeElement === $search) {
    $search.value = "";
    state.query = "";
    renderDevices();
  }
});

$sort.value = state.sortKey;
$sort.addEventListener("change", (e) => {
  state.sortKey = e.target.value;
  localStorage.setItem("sortKey", state.sortKey);
  renderDevices();
});

// ── Refresh / theme ────────────────────────────────────────────────────────
const $refreshBtn = document.getElementById("refresh-btn");
$refreshBtn.addEventListener("click", async () => {
  // Keep the label stable — the refresh usually completes in <100ms and a
  // "refreshing…" flicker just makes the button size jitter. Disabled is
  // enough visual feedback; the toast confirms completion.
  $refreshBtn.disabled = true;
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
    $refreshBtn.disabled = false;
  }
});

// Theme toggle — flips the `dark` class on <html> and persists the choice.
// The initial application happens inline in the <head> to avoid FOUC; this
// handler only deals with user-initiated toggling.
document.getElementById("theme-btn")?.addEventListener("click", () => {
  const dark = document.documentElement.classList.toggle("dark");
  localStorage.setItem("theme", dark ? "dark" : "light");
});

// Re-render the "Xs ago" labels every 5 seconds without a full re-render.
setInterval(() => {
  for (const el of document.querySelectorAll("[data-lastseen]")) {
    el.textContent = formatAgo(Number(el.dataset.lastseen));
  }
}, 5000);

// ── Top-bar [+] → open add-device modal ────────────────────────────────────
document.getElementById("device-add-btn")?.addEventListener("click", () => {
  if (!state.snapshot) {
    toast("waiting for bridge state…", "error");
    return;
  }
  openAddModal();
});

// ── Mobile hamburger ───────────────────────────────────────────────────────
// Each menu item carries `data-mobile-action="<desktop-button-id>"`; we
// forward the click to that button so existing handlers stay the single
// source of truth, then close the <details> so the panel dismisses itself.
for (const el of document.querySelectorAll("[data-mobile-action]")) {
  el.addEventListener("click", () => {
    document.getElementById(el.dataset.mobileAction)?.click();
    el.closest("details")?.removeAttribute("open");
  });
}

// ── Init modals + open the socket ──────────────────────────────────────────
initSyncModal();
initWizardModal();
initDeviceModal();
initConfirmModal();
connect();
