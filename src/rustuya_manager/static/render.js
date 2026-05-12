// Top-level rendering. Each WS frame calls render(); the renderers below
// are also exported so smaller subsystems (filter tab click, search input)
// can re-run just the affected sub-tree without a full re-render.

import { state } from "./state.js";
import { escapeHtml } from "./dom.js";
import { deviceCard, missingParentCard, classifyDevice, primaryDevice } from "./cards.js";

const $list = document.getElementById("device-list");
const $empty = document.getElementById("empty-state");
const $templates = document.getElementById("templates-block");
const $filterTabs = document.getElementById("filter-tabs");
const $banner = document.getElementById("cloud-banner");
const $warnings = document.getElementById("warnings");
const $syncBar = document.getElementById("sync-bar");

export function render() {
  if (!state.snapshot) return;
  renderTemplates();
  renderFilterCounts();
  renderWarnings();
  renderBanner();
  renderSyncBar();
  renderDevices();
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

  for (const btn of $syncBar.querySelectorAll("[data-sync-scope]")) {
    const scope = btn.dataset.syncScope;
    if (scope === "all") continue; // always visible when total > 0
    const n = counts[scope] || 0;
    btn.classList.toggle("hidden", n === 0);
    const countSpan = btn.querySelector("[data-count]");
    if (countSpan) countSpan.textContent = `(${n})`;
  }
}

export function renderTemplates() {
  const t = state.snapshot.templates;
  if (!t) return;
  $templates.innerHTML = "";
  // Root is surfaced here (rather than as a header label) so the header
  // stays compact and doesn't wrap to two lines on narrow viewports.
  const lines = [
    ["root",    t.root],
    ["command", t.command],
    ["event",   t.event],
    ["message", t.message],
    ["scanner", t.scanner],
    ["payload", t.payload],
  ];
  for (const [k, v] of lines) {
    const row = document.createElement("div");
    // flex + min-w-0 + break-all so long payload templates wrap within the
    // value column instead of running off the right edge of the panel.
    row.className = "flex";
    row.innerHTML = `<span class="text-slate-400 dark:text-slate-500 w-20 shrink-0">${k}</span><span class="flex-1 min-w-0 break-all">${escapeHtml(v)}</span>`;
    $templates.appendChild(row);
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
  for (const btn of $filterTabs.querySelectorAll("button[data-filter]")) {
    const key = btn.dataset.filter;
    const span = btn.querySelector("[data-count]");
    const n = counts[key] ?? 0;
    if (span) span.textContent = n > 0 ? n : "";
    const style = FILTER_STYLES[key] || FILTER_STYLES.all;
    btn.className = `${FILTER_BASE} ${state.filter === key ? style.active : style.idle}`;
    // Tabs with 0 fade to a quieter style so the eye lands on actionable ones.
    btn.classList.toggle("opacity-50", n === 0 && key !== "all" && state.filter !== key);
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
  if (state.filter === "all") return true;
  return cls === state.filter;
}

// Live-status rank: online first so actionable devices bubble to the top.
// Unknown/no-status sinks to the bottom so the noise stays out of the way.
const LIVE_RANK = { online: 0, offline: 1, unknown: 2 };

// Sync-category rank for the "category" sort. Actionable classes come first
// so attention-needing devices cluster at the top; synced trails because
// nothing's wrong; ungrouped (no cloud loaded) lands last as context-only.
const CATEGORY_RANK = { mismatch: 0, missing: 1, orphan: 2, synced: 3, ungrouped: 4 };

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

export function renderDevices() {
  $list.innerHTML = "";
  const entries = visibleEntries();
  if (entries.length === 0) {
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
