// Top-level rendering. Each WS frame calls render(); the renderers below
// are also exported so smaller subsystems (filter tab click, search input)
// can re-run just the affected sub-tree without a full re-render.

import { state, ALL_CATEGORIES } from "./state.js";
import { escapeHtml, toast } from "./dom.js";
import { deviceCard, missingParentCard, classifyDevice, primaryDevice } from "./cards.js";
import { t } from "./i18n.js";
import { checkVersions, applyBridgeTemplates } from "./api.js";
import { confirm } from "./modal-confirm.js";

const $list = document.getElementById("device-list");
const $empty = document.getElementById("empty-state");
const $templates = document.getElementById("templates-block");
const $filterTabs = document.getElementById("filter-tabs");
const $banner = document.getElementById("cloud-banner");
const $warnings = document.getElementById("warnings");
const $syncBar = document.getElementById("sync-bar");

// The Info panel's "check now" button is rebuilt on every render, so its click
// is handled by one delegated listener on the stable parent. It spins the icon
// while the forced PyPI check runs; the resulting state bump refreshes the
// version chips over the WS (this re-render swaps in a fresh, idle button).
if ($templates) {
  $templates.addEventListener("click", async (e) => {
    if (e.target.closest("#req-apply-btn")) {
      await applyRequirementFix(e.target.closest("#req-apply-btn"));
      return;
    }
    const btn = e.target.closest("#version-check-btn");
    if (!btn || btn.dataset.busy) return;
    btn.dataset.busy = "1";
    btn.disabled = true;
    btn.classList.add("animate-spin");
    let ok = false;
    try {
      const res = await checkVersions();
      ok = !!res && res.ok !== false;
    } catch {
      ok = false;
    } finally {
      btn.classList.remove("animate-spin");
      btn.disabled = false;
      delete btn.dataset.busy;
    }
    toast(ok ? t("info.checkDone") : t("info.checkFailed"), ok ? "ok" : "error");
  });
}

// Gather the edited template fields + retain checkbox from the requirements
// section, confirm the reconfigure cost, and push via set_config. The bridge
// clears retained state under the old scheme and restarts, so this is gated
// behind an explicit, danger-styled confirm — never silent.
async function applyRequirementFix(btn) {
  if (btn.dataset.busy) return;
  const templates = {};
  for (const inp of $templates.querySelectorAll("[data-req-template]")) {
    templates[inp.dataset.reqTemplate] = inp.value.trim();
  }
  const retainBox = $templates.querySelector("[data-req-retain]");
  const retain = retainBox ? !!retainBox.checked : undefined;
  if (!Object.keys(templates).length && retain === undefined) return;

  const ok = await confirm({
    title: t("req.confirmTitle"),
    message: t("req.confirmBody"),
    okLabel: t("req.apply"),
    danger: true,
  });
  if (!ok) return;

  btn.dataset.busy = "1";
  btn.disabled = true;
  const res = await applyBridgeTemplates(templates, retain);
  if (res && res.ok !== false) {
    toast(t("req.applied"), "ok");
  } else {
    // 400 with field errors comes back as a JSON-ish string; surface as-is.
    toast(t("req.applyFailed") + (res && res.error ? `: ${res.error}` : ""), "error");
    btn.dataset.busy = "";
    btn.disabled = false;
  }
}

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

  // ── Topics ── the resolved MQTT topic templates. Root is surfaced here
  // (rather than as a header label) so the header stays compact and doesn't
  // wrap to two lines on narrow viewports.
  $templates.appendChild(sectionHeading(t("info.topics")));
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

  // ── Status ── manager + bridge versions, then bridge-reported totals from
  // the latest status reply (device_count is the full fleet size; mqtt drops
  // is the cumulative publish-drop count, highlighted when non-zero like the
  // warning banner). The bridge row folds its mode in as a "· embedded/external"
  // suffix — the standalone mode row is gone, the summary badge already carries
  // it — and goes amber on the embed→external conflict. manager and bridge each
  // carry a version chip: amber "update available" when PyPI has a newer build,
  // a quiet "latest" when the check ran and they're current, nothing when the
  // check couldn't reach PyPI (latest unknown).
  const divider = document.createElement("div");
  divider.className = "border-t border-slate-200 dark:border-slate-700 my-1";
  $templates.appendChild(divider);
  // Status heading carries a "check now" button (forces an immediate PyPI check
  // past the daily cache). The button is rebuilt every render, so its click is
  // handled by a delegated listener on the stable #templates-block parent.
  const statusHead = sectionHeading(t("info.status"));
  statusHead.classList.add("flex", "items-center");
  statusHead.insertAdjacentHTML(
    "beforeend",
    `<button id="version-check-btn" type="button" title="${escapeHtml(t("info.checkNow"))}" aria-label="${escapeHtml(t("info.checkNow"))}" class="ml-auto shrink-0 text-sm leading-none px-1 rounded text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700">↻</button>`,
  );
  $templates.appendChild(statusHead);
  const drops = Number(snap.mqtt_drop_count || 0);
  const modeLabel =
    mode === "embedded" ? t("bridge.modeEmbedded") : t("bridge.modeExternal");
  // installed + latest both known ⇒ "update" or "latest"; else no chip.
  const verChip = (installed, latest, update) =>
    installed == null || latest == null ? null : update ? "update" : "latest";
  const diag = [
    ["manager", snap.manager_version || "—", { chip: verChip(snap.manager_version, snap.manager_latest, snap.manager_update) }],
    ["bridge", snap.bridge_version || "—", { suffix: modeLabel, warn: conflict, chip: verChip(snap.bridge_version, snap.bridge_latest, snap.bridge_update) }],
    ["devices", snap.device_count != null ? String(snap.device_count) : "—", {}],
    ["mqtt drops", String(drops), { warn: drops > 0 }],
  ];
  for (const [k, v, opts] of diag) {
    $templates.appendChild(diagRow(k, v, opts));
  }

  // ── Plugin requirements ── topic/retain needs declared by plugins, checked
  // against the live bridge config. Absent for a plugin-less build (the backend
  // omits the key). Rendered after Status; carries the guided fix.
  const req = snap.bridge_requirements;
  if (req) renderRequirements(req);

  // Amber dot on the (possibly collapsed) summary whenever anything can be
  // updated OR a plugin requirement is unmet — the cue to expand and look.
  const dot = document.getElementById("info-update-dot");
  if (dot) {
    const anyUpdate = !!(snap.manager_update || snap.bridge_update);
    const reqUnmet = !!(req && !req.satisfied);
    dot.classList.toggle("hidden", !anyUpdate && !reqUnmet);
    dot.title = reqUnmet
      ? t("req.unmet")
      : anyUpdate
        ? t("info.updateAvailable")
        : "";
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

// Render the "Plugin requirements" Info-panel section from snap.bridge_requirements.
// Shows each constrained topic's met/unmet status; for unmet ones, an editable
// field pre-filled with the manager's recommended template. A single Apply
// button pushes every edit via set_config (behind a reconfigure-cost confirm).
function renderRequirements(report) {
  const divider = document.createElement("div");
  divider.className = "border-t border-slate-200 dark:border-slate-700 my-1";
  $templates.appendChild(divider);

  const head = sectionHeading(t("req.heading"));
  head.classList.add("flex", "items-center");
  const pill = report.satisfied
    ? `<span class="ml-auto shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800">${escapeHtml(t("req.allMet"))}</span>`
    : `<span class="ml-auto shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border border-amber-300 dark:border-amber-700">${escapeHtml(t("req.actionNeeded"))}</span>`;
  head.insertAdjacentHTML("beforeend", pill);
  $templates.appendChild(head);

  for (const [key, info] of Object.entries(report.topics || {})) {
    const row = document.createElement("div");
    row.className = "mt-1";
    const icon = info.satisfied ? "✓" : "⚠";
    const iconCls = info.satisfied
      ? "text-emerald-600 dark:text-emerald-400"
      : "text-amber-600 dark:text-amber-400";
    // Header line: "✓ event" + sources.
    const who = (info.sources || []).map((s) => s.source).join(", ");
    row.innerHTML =
      `<div class="flex items-baseline gap-1"><span class="${iconCls} shrink-0">${icon}</span>` +
      `<span class="font-medium">${escapeHtml(key)}</span>` +
      `<span class="text-slate-400 dark:text-slate-500 truncate">${escapeHtml(who)}</span></div>`;

    if (info.satisfied) {
      row.insertAdjacentHTML(
        "beforeend",
        `<div class="pl-4 text-slate-400 dark:text-slate-500 break-all">${escapeHtml(info.current)}</div>`,
      );
    } else {
      const bits = [];
      if (info.missing.length) bits.push(t("req.needs") + " " + info.missing.map((p) => "{" + p + "}").join(" "));
      if (info.forbidden.length) bits.push(t("req.remove") + " " + info.forbidden.map((p) => "{" + p + "}").join(" "));
      row.insertAdjacentHTML(
        "beforeend",
        `<div class="pl-4 text-amber-700 dark:text-amber-300">${escapeHtml(bits.join(" · "))}</div>` +
          `<input type="text" data-req-template="${escapeHtml(key)}" value="${escapeHtml(info.recommended)}" ` +
          `class="mt-0.5 ml-4 w-[calc(100%-1rem)] px-1 py-0.5 rounded border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 font-mono text-xs break-all" />`,
      );
    }
    // "Not honored" notes — a source's must_not_have overridden by present-wins.
    for (const s of info.sources || []) {
      if (s.unhonored && s.unhonored.length) {
        row.insertAdjacentHTML(
          "beforeend",
          `<div class="pl-4 text-slate-400 dark:text-slate-500">${escapeHtml(
            t("req.notHonored", { source: s.source, ph: s.unhonored.map((p) => "{" + p + "}").join(" ") }),
          )}</div>`,
        );
      }
    }
    $templates.appendChild(row);
  }

  // Retain — only ever "needs True".
  if (report.retain) {
    const r = report.retain;
    const row = document.createElement("div");
    row.className = "mt-1";
    if (r.satisfied) {
      row.innerHTML = `<div class="flex items-baseline gap-1"><span class="text-emerald-600 dark:text-emerald-400 shrink-0">✓</span><span class="font-medium">retain</span><span class="text-slate-400 dark:text-slate-500">${escapeHtml((r.sources || []).join(", "))}</span></div>`;
    } else {
      row.innerHTML =
        `<div class="flex items-baseline gap-1"><span class="text-amber-600 dark:text-amber-400 shrink-0">⚠</span><span class="font-medium">retain</span><span class="text-slate-400 dark:text-slate-500">${escapeHtml((r.sources || []).join(", "))}</span></div>` +
        `<label class="pl-4 flex items-center gap-1 text-amber-700 dark:text-amber-300"><input type="checkbox" data-req-retain checked /> ${escapeHtml(t("req.setRetain"))}</label>`;
    }
    $templates.appendChild(row);
  }

  if (!report.satisfied) {
    const actions = document.createElement("div");
    actions.className = "mt-2 pl-4";
    actions.innerHTML =
      `<button id="req-apply-btn" type="button" class="text-sm px-2 py-0.5 rounded bg-amber-600 hover:bg-amber-700 text-white">${escapeHtml(t("req.apply"))}</button>` +
      `<div class="mt-1 text-[11px] text-slate-400 dark:text-slate-500 break-words whitespace-normal">${escapeHtml(t("req.applyNote"))}</div>`;
    $templates.appendChild(actions);
  }
}

// A muted uppercase subheading that groups the Info panel into sections
// ("Topics", "Status"). `spaced` adds top margin to separate it from the
// preceding group.
function sectionHeading(text, { spaced = false } = {}) {
  const h = document.createElement("div");
  h.className =
    (spaced ? "mt-2 " : "") +
    "text-[10px] uppercase tracking-wide font-semibold text-slate-400 dark:text-slate-500";
  h.textContent = text;
  return h;
}

// One row in the Info panel's Status section: a fixed-width muted label, a
// value that can wrap, an optional muted "· suffix" (the bridge mode), and an
// optional version chip — "update" (amber, a newer build exists) or "latest"
// (quiet emerald, current). The value goes amber+bold when warn.
function diagRow(label, value, { warn = false, suffix = null, chip = null } = {}) {
  const row = document.createElement("div");
  row.className = "flex items-center";
  const valueCls = warn
    ? "flex-1 min-w-0 break-all text-amber-600 dark:text-amber-400 font-medium"
    : "flex-1 min-w-0 break-all";
  const suffixHtml = suffix
    ? `<span class="text-slate-400 dark:text-slate-500"> · ${escapeHtml(suffix)}</span>`
    : "";
  let chipHtml = "";
  if (chip === "update") {
    chipHtml = `<span class="ml-2 shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border border-amber-300 dark:border-amber-700">${escapeHtml(t("info.updateAvailable"))}</span>`;
  } else if (chip === "latest") {
    chipHtml = `<span class="ml-2 shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800">${escapeHtml(t("info.latest"))}</span>`;
  }
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
  // Standalone on the summary it needs the "bridge" noun to read on its own
  // (the inline row suffix can stay terse — its row is already labelled).
  if (conflict) {
    el.textContent = `${t("bridge.modeExternalBadge")} ⚠`;
    el.className =
      "text-xs px-2 py-0.5 rounded-full border bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border-amber-300 dark:border-amber-700 font-medium";
  } else if (mode === "embedded") {
    el.textContent = t("bridge.modeEmbeddedBadge");
    el.className =
      "text-xs px-2 py-0.5 rounded-full border bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-200 border-emerald-300 dark:border-emerald-700";
  } else {
    el.textContent = t("bridge.modeExternalBadge");
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
