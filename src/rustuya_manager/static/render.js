// Top-level rendering. Each WS frame calls render(); the renderers below
// are also exported so smaller subsystems (filter tab click, search input)
// can re-run just the affected sub-tree without a full re-render.

import { state, ALL_CATEGORIES } from "./state.js";
import { escapeHtml } from "./dom.js";
import { deviceCard, missingParentCard, classifyDevice, primaryDevice } from "./cards.js";
import { t } from "./i18n.js";

const $list = document.getElementById("device-list");
const $empty = document.getElementById("empty-state");
const $templates = document.getElementById("templates-block");
const $filterTabs = document.getElementById("filter-tabs");
const $banner = document.getElementById("cloud-banner");
const $warnings = document.getElementById("warnings");
const $syncBar = document.getElementById("sync-bar");

export function render() {
  if (!state.snapshot) return;
  // When a plugin page is active, the device view is hidden — skip its work
  // entirely (it re-runs when the user switches back via showPage()). With no
  // plugins currentPage is always "devices", so this is a no-op there.
  if (state.currentPage !== "devices") return;
  renderTemplates();
  renderFilterCounts();
  renderWarnings();
  renderBanner();
  renderSyncBar();
  renderDevices();
}

// ── Defer push-driven re-renders while the user is mid-gesture ──────────────
// Each WS frame rebuilds the device list (renderDevices does `innerHTML = ""`),
// which would drop an in-progress text selection, an open <select>, or a drag —
// the "I dragged, waited, and it let go" bug. So a frame that lands mid-gesture
// is remembered, not applied, and re-rendered the moment the gesture ends. The
// list still converges to the latest state; it just never yanks the DOM out
// from under an active interaction. User-initiated renders (filter/search/sort)
// keep calling render() directly, so they stay instant.
let _pointerDown = false;
let _pendingPush = false;

function userIsInteracting() {
  if (_pointerDown) return true; // a drag (text selection, slider, …) in progress
  const a = document.activeElement;
  if (a && a.tagName === "SELECT") return true; // an open native dropdown keeps focus
  const sel = window.getSelection && window.getSelection();
  if (sel && sel.type === "Range" && String(sel).length) return true; // a live text selection
  return false;
}

function flushPendingRender() {
  if (_pendingPush && !userIsInteracting()) {
    _pendingPush = false;
    render();
  }
}

if (typeof document !== "undefined") {
  // Capture phase so the flag is set before any handler that might re-render.
  document.addEventListener("pointerdown", () => { _pointerDown = true; }, true);
  document.addEventListener("pointerup", () => { _pointerDown = false; flushPendingRender(); }, true);
  document.addEventListener("selectionchange", flushPendingRender);
  // focusout fires as a <select>/field is left; let focus settle, then flush.
  document.addEventListener("focusout", () => setTimeout(flushPendingRender, 0));
}

// WS frames render through here so an incoming snapshot can't interrupt a gesture.
export function renderFromPush() {
  if (userIsInteracting()) {
    _pendingPush = true;
    return;
  }
  render();
}

export function renderWarnings() {
  const ws = state.snapshot.warnings || {};
  const keys = Object.keys(ws);
  if (keys.length === 0) {
    $warnings.classList.add("hidden");
    $warnings.innerHTML = "";
    return;
  }
  $warnings.classList.remove("hidden");
  $warnings.innerHTML = "";
  const styles = {
    warning: "bg-amber-50 dark:bg-amber-900/30 border-amber-300 dark:border-amber-700 text-amber-900 dark:text-amber-200",
    error:   "bg-rose-50 dark:bg-rose-900/30 border-rose-300 dark:border-rose-700 text-rose-900 dark:text-rose-200",
  };
  for (const k of keys) {
    const w = ws[k];
    const cls = styles[w.level] || styles.warning;
    const banner = document.createElement("div");
    banner.className = `rounded-lg border p-3 text-sm ${cls}`;
    // break-words on the message so a long unbroken token (e.g. a template
    // dumped into the warning text) wraps within the banner.
    banner.innerHTML = `
      <div class="font-medium uppercase tracking-wide text-[11px] mb-0.5 break-all">${escapeHtml(w.level || "warning")} · ${escapeHtml(k)}</div>
      <div class="break-words">${escapeHtml(w.message || "")}</div>
    `;
    $warnings.appendChild(banner);
  }
}

export function renderSyncBar() {
  const snap = state.snapshot;
  if (!snap.cloud_loaded) {
    $syncBar.classList.add("hidden");
    return;
  }
  const counts = {
    mismatch: snap.diff.mismatched.length,
    missing: snap.diff.missing.length,
    orphan: snap.diff.orphaned.length,
  };
  const total = counts.mismatch + counts.missing + counts.orphan;
  if (total === 0) {
    $syncBar.classList.add("hidden");
    return;
  }
  $syncBar.classList.remove("hidden");

  // Counts intentionally don't render inside these buttons — the filter
  // tabs right below already show the same numbers, so re-displaying them
  // here is duplication. We still use the per-scope counts to hide buttons
  // whose category is empty.
  for (const btn of $syncBar.querySelectorAll("[data-sync-scope]")) {
    const scope = btn.dataset.syncScope;
    if (scope === "all") continue; // always visible when total > 0
    const n = counts[scope] || 0;
    btn.classList.toggle("hidden", n === 0);
  }
}

export function renderTemplates() {
  const snap = state.snapshot;
  // Mode badge on the <summary> — visible even while the drawer is collapsed.
  // Conflict = --embed-bridge was requested but we're on an external bridge
  // (an external one already owned the root, so the embed was aborted). The
  // backend also carries this as the `embedded_bridge_aborted` warning.
  const mode = snap.bridge_mode || "external";
  const conflict =
    (snap.embed_requested && mode === "external") ||
    !!(snap.warnings && snap.warnings.embedded_bridge_aborted);
  renderBridgeModeBadge(mode, conflict);

  // NB: the i18n function is imported as `t` at module scope, so the resolved
  // templates object must NOT shadow it here — later rows call t("…") for the
  // mode label / update chip / conflict note.
  const tpl = snap.templates;
  if (!tpl) return;
  $templates.innerHTML = "";
  // Root is surfaced here (rather than as a header label) so the header
  // stays compact and doesn't wrap to two lines on narrow viewports.
  const lines = [
    ["root",    tpl.root],
    ["command", tpl.command],
    ["event",   tpl.event],
    ["message", tpl.message],
    ["scanner", tpl.scanner],
    ["payload", tpl.payload],
  ];
  for (const [k, v] of lines) {
    const row = document.createElement("div");
    // flex + min-w-0 + break-all so long payload templates wrap within the
    // value column instead of running off the right edge of the panel.
    row.className = "flex";
    row.innerHTML = `<span class="text-slate-400 dark:text-slate-500 w-20 shrink-0">${k}</span><span class="flex-1 min-w-0 break-all">${escapeHtml(v)}</span>`;
    $templates.appendChild(row);
  }

  // Diagnostics block: manager + bridge versions, then bridge-reported totals
  // from the latest status reply (device_count is the full fleet size; mqtt
  // drops is the cumulative publish-drop count, highlighted when non-zero like
  // the warning banner). The bridge row folds its mode in as a "· embedded/
  // external" suffix — the standalone mode row is gone, the summary badge
  // already carries it — and goes amber on the embed→external conflict. manager
  // and bridge each show an "update available" chip when the online check found
  // a newer release on PyPI.
  const divider = document.createElement("div");
  divider.className = "border-t border-slate-200 dark:border-slate-700 my-1";
  $templates.appendChild(divider);
  const drops = Number(snap.mqtt_drop_count || 0);
  const modeLabel =
    mode === "embedded" ? t("bridge.modeEmbedded") : t("bridge.modeExternal");
  const diag = [
    ["manager", snap.manager_version || "—", { update: !!snap.manager_update }],
    ["bridge", snap.bridge_version || "—", { suffix: modeLabel, warn: conflict, update: !!snap.bridge_update }],
    ["devices", snap.device_count != null ? String(snap.device_count) : "—", {}],
    ["mqtt drops", String(drops), { warn: drops > 0 }],
  ];
  for (const [k, v, opts] of diag) {
    $templates.appendChild(diagRow(k, v, opts));
  }

  // Amber dot on the (possibly collapsed) summary whenever anything can be
  // updated — the cue to expand and read which component it is.
  const dot = document.getElementById("info-update-dot");
  if (dot) {
    const anyUpdate = !!(snap.manager_update || snap.bridge_update);
    dot.classList.toggle("hidden", !anyUpdate);
    dot.title = anyUpdate ? t("info.updateAvailable") : "";
  }

  // Spell out the conflict — a colored "external" value is easy to miss.
  if (conflict) {
    const note = document.createElement("div");
    note.className =
      "mt-1 text-amber-700 dark:text-amber-300 break-words whitespace-normal";
    const warnMsg = snap.warnings && snap.warnings.embedded_bridge_aborted;
    note.textContent =
      "⚠ " +
      ((warnMsg && warnMsg.message) || t("bridge.conflictNote"));
    $templates.appendChild(note);
  }
}

// One row in the Info panel's diagnostics block: a fixed-width muted label, a
// value that can wrap, an optional muted "· suffix" (the bridge mode), and an
// optional amber "update available" chip. The value goes amber+bold when warn.
function diagRow(label, value, { warn = false, suffix = null, update = false } = {}) {
  const row = document.createElement("div");
  row.className = "flex items-center";
  const valueCls = warn
    ? "flex-1 min-w-0 break-all text-amber-600 dark:text-amber-400 font-medium"
    : "flex-1 min-w-0 break-all";
  const suffixHtml = suffix
    ? `<span class="text-slate-400 dark:text-slate-500"> · ${escapeHtml(suffix)}</span>`
    : "";
  const chipHtml = update
    ? `<span class="ml-2 shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border border-amber-300 dark:border-amber-700">${escapeHtml(t("info.updateAvailable"))}</span>`
    : "";
  row.innerHTML =
    `<span class="text-slate-400 dark:text-slate-500 w-20 shrink-0">${escapeHtml(label)}</span>` +
    `<span class="${valueCls}">${escapeHtml(value)}${suffixHtml}</span>` +
    chipHtml;
  return row;
}

// Fill the mode badge on the Info <summary> so embedded/external (and the
// conflict) is visible even with the drawer collapsed.
function renderBridgeModeBadge(mode, conflict) {
  const el = document.getElementById("bridge-info-badge");
  if (!el) return;
  el.classList.remove("hidden");
  if (conflict) {
    el.textContent = `${t("bridge.modeExternal")} ⚠`;
    el.className =
      "text-xs px-2 py-0.5 rounded-full border bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border-amber-300 dark:border-amber-700 font-medium";
  } else if (mode === "embedded") {
    el.textContent = t("bridge.modeEmbedded");
    el.className =
      "text-xs px-2 py-0.5 rounded-full border bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-200 border-emerald-300 dark:border-emerald-700";
  } else {
    el.textContent = t("bridge.modeExternal");
    el.className =
      "text-xs px-2 py-0.5 rounded-full border bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600";
  }
}

// Filter tab styling. Each tab is colored per sync class so the row
// communicates state at a glance even without reading the labels.
const FILTER_STYLES = {
  all:      { active: "bg-slate-700 text-white border-slate-700 dark:bg-slate-200 dark:text-slate-900 dark:border-slate-200",
              idle:   "bg-white text-slate-700 border-slate-300 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-600" },
  synced:   { active: "bg-emerald-600 text-white border-emerald-600",
              idle:   "bg-emerald-50 text-emerald-700 border-emerald-300 dark:bg-emerald-900/30 dark:text-emerald-300 dark:border-emerald-700" },
  mismatch: { active: "bg-amber-600 text-white border-amber-600",
              idle:   "bg-amber-50 text-amber-700 border-amber-300 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-700" },
  missing:  { active: "bg-sky-600 text-white border-sky-600",
              idle:   "bg-sky-50 text-sky-700 border-sky-300 dark:bg-sky-900/30 dark:text-sky-300 dark:border-sky-700" },
  orphan:   { active: "bg-rose-600 text-white border-rose-600",
              idle:   "bg-rose-50 text-rose-700 border-rose-300 dark:bg-rose-900/30 dark:text-rose-300 dark:border-rose-700" },
};
const FILTER_BASE = "px-2 py-1 rounded border text-xs";

// Counts live inside the filter tabs themselves — one row that doubles as
// summary + filter UI. The "all" count is total devices (cloud ∪ bridge).
export function renderFilterCounts() {
  const snap = state.snapshot;
  const totalIds = new Set([
    ...Object.keys(snap.cloud),
    ...Object.keys(snap.bridge),
  ]);
  const counts = {
    all: totalIds.size,
    synced: snap.diff.synced.length,
    mismatch: snap.diff.mismatched.length,
    missing: snap.diff.missing.length,
    orphan: snap.diff.orphaned.length,
  };
  // "all" is active only when every real category is currently on — i.e.
  // nothing is being filtered out. Toggling any category off de-activates
  // the "all" pill so it's clear that the view is partial.
  const allOn = ALL_CATEGORIES.every((c) => state.filters.has(c));
  for (const btn of $filterTabs.querySelectorAll("button[data-filter]")) {
    const key = btn.dataset.filter;
    const span = btn.querySelector("[data-count]");
    const n = counts[key] ?? 0;
    if (span) span.textContent = n > 0 ? n : "";
    const style = FILTER_STYLES[key] || FILTER_STYLES.all;
    const on = key === "all" ? allOn : state.filters.has(key);
    btn.className = `${FILTER_BASE} ${on ? style.active : style.idle}`;
    // Tabs with 0 fade to a quieter style so the eye lands on actionable ones.
    btn.classList.toggle("opacity-50", n === 0 && key !== "all" && !on);
  }
}

export function renderBanner() {
  // Show the upload banner only when no cloud is loaded.
  if (!state.snapshot.cloud_loaded) $banner.classList.remove("hidden");
  else $banner.classList.add("hidden");
}

// ── Tree building ──────────────────────────────────────────────────────────
// Each top-level entry is either a WiFi device, a parent gateway with kids,
// or a "missing parent" placeholder when sub-devices reference a parent_id
// that doesn't exist in cloud or bridge.
function buildTree() {
  const snap = state.snapshot;
  const allIds = new Set([
    ...Object.keys(snap.cloud),
    ...Object.keys(snap.bridge),
  ]);

  const childrenByParent = new Map();
  const topLevel = [];
  for (const id of allIds) {
    const dev = primaryDevice(id);
    if (!dev) continue;
    if (dev.type === "SubDevice" && dev.parent_id) {
      if (!childrenByParent.has(dev.parent_id)) childrenByParent.set(dev.parent_id, []);
      childrenByParent.get(dev.parent_id).push(id);
    } else {
      topLevel.push(id);
    }
  }

  const entries = [];
  for (const id of topLevel) {
    entries.push({ kind: "device", id, children: childrenByParent.get(id) || [] });
    childrenByParent.delete(id);
  }
  // Anything left in childrenByParent is a sub-device whose parent doesn't exist anywhere.
  for (const [parent_id, kids] of childrenByParent.entries()) {
    entries.push({ kind: "missing_parent", id: parent_id, children: kids });
  }
  return entries;
}

function matchesQuery(id) {
  if (!state.query) return true;
  const dev = primaryDevice(id);
  if (!dev) return false;
  const haystack = [id, dev.name, dev.ip, dev.cid].join(" ").toLowerCase();
  return haystack.includes(state.query.toLowerCase());
}

function matchesFilter(cls) {
  // "ungrouped" is the no-cloud-loaded state, not a real sync class — it
  // isn't togglable in the UI, so it always passes the filter.
  if (cls === "ungrouped") return true;
  return state.filters.has(cls);
}

// Live-status rank: online first so actionable devices bubble to the top.
// Unknown/no-status sinks to the bottom so the noise stays out of the way.
const LIVE_RANK = { online: 0, offline: 1, unknown: 2 };

// Sync-category rank for the "category" sort. Order goes from "presence
// itself is wrong" (missing → orphan) down to "presence right, just fields
// drifted" (mismatch), then "all good" (synced). This puts the per-device
// add/remove decisions at the top where they need real attention, with the
// mass-apply "push cloud fields" mismatches below them; synced trails as
// the no-op pile and ungrouped (no cloud loaded) lands last.
const CATEGORY_RANK = { missing: 0, orphan: 1, mismatch: 2, synced: 3, ungrouped: 4 };

function sortValue(id) {
  const dev = primaryDevice(id);
  if (!dev) return "";
  switch (state.sortKey) {
    case "name":   return (dev.name || "").toLowerCase();
    case "status": {
      const live = state.snapshot.live_status?.[id];
      return live ? (LIVE_RANK[live.state] ?? 9) : 9;
    }
    case "category": return CATEGORY_RANK[classifyDevice(id)] ?? 9;
    default:       return id;
  }
}

function compareIds(a, b) {
  const va = sortValue(a);
  const vb = sortValue(b);
  if (va < vb) return -1;
  if (va > vb) return 1;
  return 0;
}

// Decide whether a tree entry (and its children) should be visible after
// applying filter + search. An entry passes if any of:
//   - the entry itself matches both filter and search
//   - any of its children matches both
// When the parent matches, all its children are shown for context.
function visibleEntries() {
  const entries = buildTree();
  const out = [];
  for (const entry of entries) {
    const parentCls = entry.kind === "missing_parent" ? "missing" : classifyDevice(entry.id);
    const parentVisible = matchesFilter(parentCls) && matchesQuery(entry.id);

    const visibleChildren = entry.children.filter((cid) => {
      const cls = classifyDevice(cid);
      return matchesFilter(cls) && matchesQuery(cid);
    });

    if (parentVisible || visibleChildren.length > 0) {
      // Display children list: if parent is visible, show ALL children
      // (better context); otherwise show only matching ones.
      const displayKids = parentVisible ? entry.children : visibleChildren;
      out.push({ ...entry, displayKids });
    }
  }

  out.sort((a, b) => compareIds(a.id, b.id));
  for (const e of out) e.displayKids.sort(compareIds);
  return out;
}

function describeEmptyReason() {
  // Tell the user *why* the list is empty so they know how to recover.
  // The most common case used to be hidden behind a snap-back; now the
  // empty-state owns that explanation.
  if (state.filters.size === 0) {
    return t("empty.noCategory");
  }
  if (state.query) {
    return t("empty.noMatchQuery", { query: state.query });
  }
  return t("empty.noMatchFilter");
}

export function renderDevices() {
  $list.innerHTML = "";
  const entries = visibleEntries();
  if (entries.length === 0) {
    $empty.textContent = describeEmptyReason();
    $empty.classList.remove("hidden");
    return;
  }
  $empty.classList.add("hidden");

  for (const entry of entries) {
    if (entry.kind === "device") {
      $list.appendChild(deviceCard(entry.id, classifyDevice(entry.id), false));
    } else {
      $list.appendChild(missingParentCard(entry.id));
    }
    for (const cid of entry.displayKids) {
      $list.appendChild(deviceCard(cid, classifyDevice(cid), true));
    }
  }
}
