// rustuya-manager web client.
//
// The server is the source of truth: every WS frame carries the full state,
// so we just re-render. With dozens of devices this is well under a millisecond
// of work and keeps the client logic flat — no diffing, no patching, no store.

// ── State (write-once per WS frame) ─────────────────────────────────────────
let snapshot = null;
let filter = "all";
let query = "";
let sortKey = localStorage.getItem("sortKey") || "id";
// Per-device collapse state, persisted across reloads. Default: all collapsed.
const expandedIds = new Set(
  JSON.parse(localStorage.getItem("expandedIds") || "[]")
);
function saveExpanded() {
  localStorage.setItem("expandedIds", JSON.stringify([...expandedIds]));
}
function toggleExpand(id) {
  if (expandedIds.has(id)) expandedIds.delete(id);
  else expandedIds.add(id);
  saveExpanded();
  renderDevices();
}

// ── Element refs ────────────────────────────────────────────────────────────
const $list = document.getElementById("device-list");
const $empty = document.getElementById("empty-state");
const $conn = document.getElementById("conn-badge");
const $rootLabel = document.getElementById("root-label");
const $templates = document.getElementById("templates-block");
const $filterTabs = document.getElementById("filter-tabs");
const $toasts = document.getElementById("toast-container");
const $search = document.getElementById("search-input");
const $sort = document.getElementById("sort-select");
const $banner = document.getElementById("cloud-banner");
const $dropzone = document.getElementById("cloud-dropzone");
const $pickBtn = document.getElementById("cloud-pick-btn");
const $fileInput = document.getElementById("cloud-file-input");
const $wizardOpen = document.getElementById("wizard-open-btn");
const $wizardModal = document.getElementById("wizard-modal");
const $wizardClose = document.getElementById("wizard-close");
const $wizardCancel = document.getElementById("wizard-cancel");
const $wizardStart = document.getElementById("wizard-start");
const $wizardUserCode = document.getElementById("wizard-user-code");
const $wizardBody = document.getElementById("wizard-body");
const $wizardQrImage = document.getElementById("wizard-qr-image");
const $wizardWorkingMsg = document.getElementById("wizard-working-message");
const $wizardDoneMsg = document.getElementById("wizard-done-message");
const $wizardErrorMsg = document.getElementById("wizard-error-message");
const $warnings = document.getElementById("warnings");
const $syncBar = document.getElementById("sync-bar");
const $modal = document.getElementById("sync-modal");
const $modalBody = document.getElementById("sync-modal-body");
const $modalTitle = document.getElementById("sync-modal-title");
const $modalSubtitle = document.getElementById("sync-modal-subtitle");
const $modalApply = document.getElementById("sync-modal-apply");
const $modalCancel = document.getElementById("sync-modal-cancel");
const $modalClose = document.getElementById("sync-modal-close");
const $modalProgress = document.getElementById("sync-modal-progress");

// ── Connection ──────────────────────────────────────────────────────────────
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function setConn(state) {
  // Short labels keep the badge compact; the longest ("connecting") fits in
  // ~80 px. The fixed width prevents header reflow on state transitions.
  const styles = {
    connecting: ["bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600", "connecting", true],
    live:       ["bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-300 dark:border-emerald-700", "live", false],
    lost:       ["bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 border-rose-300 dark:border-rose-700", "lost", true],
  };
  const [cls, label, pulse] = styles[state];
  $conn.className = `text-xs px-2 py-1 rounded-full border ${cls} inline-flex items-center justify-center w-[96px] whitespace-nowrap gap-1`;
  $conn.innerHTML = `<span class="${pulse ? "pulse-dot " : ""}leading-none">●</span><span>${label}</span>`;
}

let backoffMs = 500;
function connect() {
  setConn("connecting");
  const ws = new WebSocket(wsUrl());
  ws.onopen = () => { backoffMs = 500; setConn("live"); };
  ws.onmessage = (ev) => {
    snapshot = JSON.parse(ev.data);
    render();
  };
  ws.onclose = () => {
    setConn("lost");
    backoffMs = Math.min(backoffMs * 2, 8000);
    setTimeout(connect, backoffMs);
  };
  ws.onerror = () => ws.close();
}

// ── Cloud upload ────────────────────────────────────────────────────────────
async function uploadCloud(file) {
  try {
    const text = await file.text();
    JSON.parse(text); // sanity-check on the client side too
    const res = await fetch("/api/cloud", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: text,
    });
    if (!res.ok) {
      toast(`upload failed: ${await res.text()}`, "error");
      return;
    }
    const body = await res.json();
    let msg = `loaded ${body.count} devices`;
    if (body.persisted_to) msg += ` — saved to ${body.persisted_to}`;
    toast(msg, "ok");
  } catch (e) {
    toast(`upload error: ${e.message}`, "error");
  }
}

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

// ── Rendering ───────────────────────────────────────────────────────────────
function render() {
  if (!snapshot) return;
  renderRoot();
  renderTemplates();
  renderFilterCounts();
  renderWarnings();
  renderBanner();
  renderSyncBar();
  renderDevices();
}

function renderWarnings() {
  const ws = snapshot.warnings || {};
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
    banner.innerHTML = `
      <div class="font-medium uppercase tracking-wide text-[11px] mb-0.5">${escapeHtml(w.level || "warning")} · ${escapeHtml(k)}</div>
      <div>${escapeHtml(w.message || "")}</div>
    `;
    $warnings.appendChild(banner);
  }
}

function renderSyncBar() {
  if (!snapshot.cloud_loaded) {
    $syncBar.classList.add("hidden");
    return;
  }
  const counts = {
    mismatch: snapshot.diff.mismatched.length,
    missing: snapshot.diff.missing.length,
    orphan: snapshot.diff.orphaned.length,
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

function renderRoot() {
  const root = snapshot.templates?.root || "";
  $rootLabel.textContent = root ? `root: ${root}` : "";
}

function renderTemplates() {
  const t = snapshot.templates;
  if (!t) return;
  $templates.innerHTML = "";
  const lines = [
    ["command", t.command],
    ["event",   t.event],
    ["message", t.message],
    ["scanner", t.scanner],
    ["payload", t.payload],
  ];
  for (const [k, v] of lines) {
    const row = document.createElement("div");
    row.innerHTML = `<span class="text-slate-400 dark:text-slate-500 w-20 inline-block">${k}</span><span>${escapeHtml(v)}</span>`;
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
const _FILTER_BASE = "px-2 py-1 rounded border text-xs";

// Counts live inside the filter tabs themselves — one row that doubles as
// summary + filter UI. The "all" count is total devices (cloud ∪ bridge).
function renderFilterCounts() {
  const totalIds = new Set([
    ...Object.keys(snapshot.cloud),
    ...Object.keys(snapshot.bridge),
  ]);
  const counts = {
    all: totalIds.size,
    synced: snapshot.diff.synced.length,
    mismatch: snapshot.diff.mismatched.length,
    missing: snapshot.diff.missing.length,
    orphan: snapshot.diff.orphaned.length,
  };
  for (const btn of $filterTabs.querySelectorAll("button[data-filter]")) {
    const key = btn.dataset.filter;
    const span = btn.querySelector("[data-count]");
    const n = counts[key] ?? 0;
    if (span) span.textContent = n > 0 ? n : "";
    const style = FILTER_STYLES[key] || FILTER_STYLES.all;
    btn.className = `${_FILTER_BASE} ${filter === key ? style.active : style.idle}`;
    // Tabs with 0 fade to a quieter style so the eye lands on actionable ones.
    btn.classList.toggle("opacity-50", n === 0 && key !== "all" && filter !== key);
  }
}

function renderBanner() {
  // Show the upload banner only when no cloud is loaded.
  if (!snapshot.cloud_loaded) $banner.classList.remove("hidden");
  else $banner.classList.add("hidden");
}

// Sync class for a device id. With no cloud loaded, everything is "ungrouped"
// — we don't compute diff at all (the bridge IS the source of truth).
function classifyDevice(id) {
  if (!snapshot.cloud_loaded) return "ungrouped";
  if (snapshot.diff.missing.includes(id)) return "missing";
  if (snapshot.diff.orphaned.includes(id)) return "orphan";
  if (snapshot.diff.mismatched.some((m) => m.id === id)) return "mismatch";
  return "synced";
}

function primaryDevice(id) {
  return snapshot.cloud[id] || snapshot.bridge[id] || null;
}

// ── Tree building ──────────────────────────────────────────────────────────
// Each top-level entry is either a WiFi device, a parent gateway with kids,
// or a "missing parent" placeholder when sub-devices reference a parent_id
// that doesn't exist in cloud or bridge.
function buildTree() {
  const allIds = new Set([
    ...Object.keys(snapshot.cloud),
    ...Object.keys(snapshot.bridge),
  ]);

  // Group sub-devices by parent_id
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

  // Build top-level entries: real devices first, then synthetic missing-parent
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

// ── Filtering & sorting ────────────────────────────────────────────────────
function matchesQuery(id) {
  if (!query) return true;
  const dev = primaryDevice(id);
  if (!dev) return false;
  const haystack = [id, dev.name, dev.ip, dev.cid].join(" ").toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function matchesFilter(cls) {
  if (filter === "all") return true;
  return cls === filter;
}

function sortValue(id) {
  const dev = primaryDevice(id);
  if (!dev) return "";
  switch (sortKey) {
    case "name":      return (dev.name || "").toLowerCase();
    case "type":      return dev.type || "";
    case "status":    return dev.status || "";
    case "last_seen": {
      const t = snapshot.last_seen[id];
      // Sort most-recent first → negate for natural ascending compare
      return t ? -t : Infinity;
    }
    default:          return id;
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

function renderDevices() {
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

// ── Card renderers ─────────────────────────────────────────────────────────
function deviceCard(id, cls, isChild) {
  const cloud = snapshot.cloud[id];
  const bridge = snapshot.bridge[id];
  const primary = cloud || bridge;
  const ipInfo = resolveIp(bridge, cloud);
  const dps = snapshot.dps[id] || {};
  const lastSeen = snapshot.last_seen[id];
  const live = snapshot.live_status?.[id];
  const isExpanded = expandedIds.has(id);

  const edgeColor = computeEdgeColor(cls, live);
  const indent = isChild ? "ml-4 md:ml-8" : "";

  const card = document.createElement("div");
  card.className = `bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 border-l-4 ${edgeColor} p-3 ${indent} cursor-pointer`;
  card.title = `${cls}${primary.type ? ` · ${primary.type}` : ""}${live?.state ? ` · ${live.state}` : ""}`;
  // Tap anywhere on the card to expand/collapse. Buttons inside stop the
  // event from propagating up so they don't accidentally toggle.
  card.addEventListener("click", (ev) => {
    if (ev.target.closest("button, input, a, [contenteditable]")) return;
    toggleExpand(id);
  });

  // ── Header row 1: name (or id) + caret + status icons + actions ────────
  const headerTop = document.createElement("div");
  headerTop.className = "flex items-center gap-2 min-w-0";
  const nameOrId = primary.name && primary.name !== "N/A" ? primary.name : id;
  headerTop.innerHTML = `
    ${isChild ? '<span class="text-slate-300 dark:text-slate-600 text-sm shrink-0">└</span>' : ""}
    <span class="font-medium text-sm text-slate-900 dark:text-slate-100 truncate min-w-0">${escapeHtml(nameOrId)}</span>
  `;
  const rightCluster = document.createElement("span");
  rightCluster.className = "ml-auto flex items-center gap-1.5 shrink-0";
  rightCluster.appendChild(liveDot(live));
  rightCluster.appendChild(typeBadge(primary.type));
  appendInlineActions(rightCluster, id, cls, cloud, bridge, primary);
  rightCluster.appendChild(expandCaret(id, isExpanded));
  headerTop.appendChild(rightCluster);
  card.appendChild(headerTop);

  // ── Header row 2: id (when name was shown) + last-seen ─────────────────
  const showSecondaryId =
    primary.name && primary.name !== "N/A" && primary.name !== id;
  const headerBottom = document.createElement("div");
  headerBottom.className = "flex items-center gap-2 mt-0.5";
  headerBottom.innerHTML = showSecondaryId
    ? `<span class="font-mono text-[11px] text-slate-400 dark:text-slate-500 truncate">${escapeHtml(id)}</span>`
    : `<span></span>`;
  if (lastSeen) {
    const ls = document.createElement("span");
    ls.className = "ml-auto text-[10px] text-slate-400 dark:text-slate-500 shrink-0";
    ls.dataset.lastseen = String(lastSeen);
    ls.textContent = formatAgo(lastSeen);
    headerBottom.appendChild(ls);
  }
  card.appendChild(headerBottom);

  if (!isExpanded) return card;

  // ── Expanded body: field grid + mismatch reasons + DPS chips ───────────
  // Sub-devices live behind a gateway, so IP and KEY are meaningless for
  // them — only the CID and parent relationship matter. WiFi devices show
  // IP/KEY/VER + any live error message from the bridge.
  const grid = document.createElement("div");
  grid.className = "mt-2 grid grid-cols-2 md:grid-cols-4 gap-x-3 gap-y-0.5 text-xs text-slate-600 dark:text-slate-400";
  let fields;
  if (primary.type === "SubDevice") {
    fields = [
      ["CID", primary.cid || "—"],
      ["PARENT", shorten(primary.parent_id) || "—"],
    ];
  } else {
    fields = [
      ["IP", ipInfo.value, ipInfo.tooltip],
      ["KEY", primary.key ? shorten(primary.key) : "—"],
      ["VER", primary.version],
    ];
    if (live?.message) fields.push(["MSG", live.message]);
  }
  for (const entry of fields) {
    const [k, v, tooltip] = entry;
    const f = document.createElement("div");
    f.className = "flex gap-1 min-w-0";
    const titleAttr = tooltip || String(v).length > 16
      ? ` title="${escapeHtml(tooltip || String(v))}"` : "";
    f.innerHTML = `<span class="text-slate-400 dark:text-slate-500 shrink-0">${k}</span><span class="font-mono truncate min-w-0"${titleAttr}>${escapeHtml(String(v))}</span>`;
    grid.appendChild(f);
  }
  card.appendChild(grid);

  if (cls === "mismatch") {
    const m = snapshot.diff.mismatched.find((m) => m.id === id);
    if (m) {
      const reasons = document.createElement("div");
      reasons.className = "mt-1.5 text-xs text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700 rounded px-2 py-1";
      reasons.innerHTML = m.reasons.map(escapeHtml).join("<br>");
      card.appendChild(reasons);
    }
  }

  const dpsEntries = Object.entries(dps).filter(
    ([, v]) => v !== "" && v !== null && v !== undefined
  );
  if (dpsEntries.length > 0) {
    const dpsRow = document.createElement("div");
    dpsRow.className = "mt-1.5 flex flex-wrap gap-1";
    for (const [k, v] of dpsEntries) {
      const chip = document.createElement("span");
      chip.className = "text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 border border-slate-200 dark:border-slate-600 text-slate-700 dark:text-slate-200";
      chip.textContent = `dp${k}=${formatDpsValue(v)}`;
      dpsRow.appendChild(chip);
    }
    card.appendChild(dpsRow);
  }

  return card;
}

function computeEdgeColor(cls, live) {
  if (cls === "mismatch") return "border-l-amber-400";
  if (cls === "missing")  return "border-l-sky-400";
  if (cls === "orphan")   return "border-l-rose-400";
  if (cls === "ungrouped") return "border-l-slate-300";
  // synced — differentiate by live online state
  if (live?.state === "offline") return "border-l-slate-400";
  if (live?.state === "online")  return "border-l-emerald-400";
  return "border-l-slate-200";  // synced but no live signal yet
}

// Header-row icons are all 20×20 (h-5 w-5) with centered content so they
// line up vertically next to the text buttons. liveDot / typeBadge /
// iconButton share the same outer dimensions.

const _ICON_BASE = "w-5 h-5 inline-flex items-center justify-center";

function liveDot(live) {
  // CSS-drawn dot, not the ● glyph. Font glyphs ride above the baseline,
  // making them look bottom-anchored next to other h-5 icons; a CSS circle
  // sits in the exact geometric center of the inline-flex container.
  const wrap = document.createElement("span");
  wrap.className = _ICON_BASE;
  const dot = document.createElement("span");
  if (!live) {
    dot.className = "w-2 h-2 rounded-full border-2 border-slate-300 dark:border-slate-600";
    wrap.title = "no status received";
    wrap.appendChild(dot);
    return wrap;
  }
  const map = {
    online:  ["bg-emerald-500", "online", false],
    offline: ["border-2 border-slate-400 dark:border-slate-500", "offline", true],
    unknown: ["border-2 border-slate-300 dark:border-slate-600", "unknown", true],
  };
  const [cls, label, ring] = map[live.state] || ["bg-rose-500", String(live.state), false];
  dot.className = `w-2 h-2 rounded-full ${cls}${ring ? " bg-transparent" : ""}`;
  const code = live.code != null ? ` (code ${live.code})` : "";
  const msg = live.message ? `: ${live.message}` : "";
  wrap.title = `${label}${code}${msg}`;
  wrap.appendChild(dot);
  return wrap;
}

function typeBadge(t) {
  const span = document.createElement("span");
  span.className =
    `${_ICON_BASE} text-[10px] font-mono rounded border border-slate-200 dark:border-slate-600 text-slate-500 dark:text-slate-400`;
  if (t === "SubDevice") {
    span.textContent = "S";
    span.title = "Sub-device";
  } else {
    span.textContent = "W";
    span.title = "WiFi device";
  }
  return span;
}

function expandCaret(id, isExpanded) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `${_ICON_BASE} text-slate-400 dark:text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 text-xs`;
  b.textContent = isExpanded ? "▾" : "▸";
  b.title = isExpanded ? "Collapse" : "Expand";
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleExpand(id);
  });
  return b;
}

function iconButton(glyph, onClick, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.className =
    `${_ICON_BASE} rounded border border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 text-slate-500 dark:text-slate-300 text-xs`;
  b.textContent = glyph;
  b.title = title;
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

function appendInlineActions(container, id, cls, cloud, bridge, primary) {
  if (cls === "missing") {
    container.appendChild(button("Add", () => sync("add", primary)));
  } else if (cls === "orphan") {
    container.appendChild(button("Remove", () => sync("remove", primary), "danger"));
  } else if (cls === "mismatch") {
    container.appendChild(button("Update", () => sync("add", cloud)));
  }
  if (cls !== "missing") {
    container.appendChild(
      iconButton("↻", () => publishCommand({ action: "get", id }), "Query status from bridge"),
    );
  }
}

// Decides which IP to surface in the card and what extra context to put in
// the tooltip. See the call site for the policy.
function resolveIp(bridge, cloud) {
  const cloudIp = cloud?.ip;
  if (bridge) {
    if (bridge.ip && bridge.ip !== "Auto") {
      // Bridge has a concrete IP — operational truth.
      if (cloudIp && cloudIp !== "Auto" && cloudIp !== bridge.ip) {
        return {
          value: bridge.ip,
          tooltip: `Bridge connects to ${bridge.ip}; cloud reports ${cloudIp}`,
        };
      }
      return { value: bridge.ip, tooltip: "" };
    }
    // bridge.ip === "Auto" → no fixed IP; the bridge follows DHCP / dynamic
    // assignment. Report that literally; substituting the cloud value would
    // be misleading because it's usually the external NAT'd address, not LAN.
    const tip =
      "IP is dynamic (DHCP/auto)." +
      (cloudIp && cloudIp !== "Auto" ? `\nCloud reports: ${cloudIp} (typically external/NAT, not LAN)` : "");
    return { value: "Auto", tooltip: tip };
  }
  // Device not in bridge — cloud is all we have.
  return { value: cloudIp || "—", tooltip: "" };
}

function missingParentCard(parent_id) {
  const card = document.createElement("div");
  card.className = "bg-white dark:bg-slate-800 rounded-lg border-2 border-dashed border-sky-300 dark:border-sky-700 p-3 md:p-4";
  card.innerHTML = `
    <div class="flex flex-wrap items-center gap-2">
      <span class="font-mono text-sm text-slate-700 dark:text-slate-200">${escapeHtml(parent_id)}</span>
      ${statusPill("missing")}
      <span class="text-[10px] px-1.5 py-0.5 rounded-full border bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600">missing parent</span>
    </div>
    <div class="text-xs text-slate-500 dark:text-slate-400 mt-1">
      Sub-device(s) reference this parent, but the parent is not in cloud or bridge.
    </div>
  `;
  return card;
}

function button(label, onClick, variant = "default") {
  const styles = {
    default: "border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200",
    danger:  "border-rose-300 dark:border-rose-700 bg-white dark:bg-slate-700 hover:bg-rose-50 dark:hover:bg-rose-900/40 text-rose-700 dark:text-rose-300",
  }[variant];
  const b = document.createElement("button");
  b.type = "button";
  // h-5 matches the icons/dots so everything in the right cluster aligns.
  b.className = `h-5 px-2 inline-flex items-center rounded border text-[11px] ${styles}`;
  b.textContent = label;
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

function statusPill(cls) {
  // Used only by missingParentCard now; device cards use the left-edge color
  // strip + liveDot/typeBadge for the same information without text labels.
  const map = {
    synced:    ["bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-700", "synced"],
    mismatch:  ["bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-700", "mismatch"],
    missing:   ["bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-700", "missing"],
    orphan:    ["bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 border-rose-200 dark:border-rose-700", "orphan"],
    ungrouped: ["bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-600", "ungrouped"],
  };
  const [s, label] = map[cls];
  return `<span class="text-[10px] px-1.5 py-0.5 rounded-full border ${s} uppercase tracking-wide">${label}</span>`;
}

// ── Actions (POST /api/command) ────────────────────────────────────────────
function buildCommandBody(action, dev) {
  const body = { action, id: dev.id, name: dev.name };
  if (action === "add") {
    if (dev.type === "WiFi") {
      if (dev.key && dev.key !== "Auto") body.key = dev.key;
      if (dev.ip && dev.ip !== "Auto") body.ip = dev.ip;
      if (dev.version && dev.version !== "Auto") body.version = dev.version;
    } else {
      if (dev.cid) body.cid = dev.cid;
      if (dev.parent_id) body.parent_id = dev.parent_id;
    }
  }
  return body;
}

async function sync(action, dev) {
  await publishCommand(buildCommandBody(action, dev));
}

async function publishCommand(body) {
  const result = await postCommand(body);
  if (result.ok) {
    toast(`${body.action} → ${body.id || "bridge"} sent`, "ok");
  } else {
    toast(`error: ${result.error}`, "error");
  }
  return result;
}

// Silent variant: returns {ok, error} without toasting. Used in batch loops
// where we want per-item status reporting in the modal instead of a flood of
// toasts.
async function postCommand(body) {
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) return { ok: false, error: await res.text() };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ── Sync confirm modal ─────────────────────────────────────────────────────
// Plan: list of items the user is about to apply. The modal lets them
// uncheck individual rows, then we publish each checked item sequentially
// and reflect per-row status.
let currentPlan = null;
let applying = false;

function buildPlan(scope) {
  const plan = [];
  if (scope === "all" || scope === "mismatch") {
    for (const m of snapshot.diff.mismatched) {
      const dev = snapshot.cloud[m.id];
      if (!dev) continue;
      plan.push({
        scope: "mismatch",
        id: m.id,
        action: "add",  // re-publishing add updates fields on the bridge
        dev,
        reasons: m.reasons,
        checked: true,
        status: "pending",
        error: null,
      });
    }
  }
  if (scope === "all" || scope === "missing") {
    for (const id of snapshot.diff.missing) {
      const dev = snapshot.cloud[id];
      if (!dev) continue;
      plan.push({
        scope: "missing",
        id, action: "add", dev, reasons: [],
        checked: true, status: "pending", error: null,
      });
    }
  }
  if (scope === "all" || scope === "orphan") {
    for (const id of snapshot.diff.orphaned) {
      const dev = snapshot.bridge[id];
      if (!dev) continue;
      plan.push({
        scope: "orphan",
        id, action: "remove", dev, reasons: [],
        checked: true, status: "pending", error: null,
      });
    }
  }
  return plan;
}

function openSyncModal(scope) {
  if (!snapshot) return;
  currentPlan = buildPlan(scope);
  if (currentPlan.length === 0) {
    toast("Nothing to sync in that category", "ok");
    return;
  }
  const titles = {
    all: "Sync everything",
    mismatch: "Apply mismatches",
    missing: "Add missing devices",
    orphan: "Remove orphans",
  };
  $modalTitle.textContent = titles[scope] || "Sync changes";
  applying = false;
  $modalApply.disabled = false;
  $modalApply.textContent = "Apply";
  $modalCancel.disabled = false;
  $modalClose.disabled = false;
  $modalProgress.textContent = "";
  renderModal();
  $modal.classList.remove("hidden");
}

function closeSyncModal() {
  if (applying) return; // refuse to close mid-apply
  $modal.classList.add("hidden");
  currentPlan = null;
}

function renderModal() {
  $modalBody.innerHTML = "";

  // Group by scope
  const groups = { mismatch: [], missing: [], orphan: [] };
  for (const item of currentPlan) groups[item.scope].push(item);

  const titles = {
    mismatch: ["Update mismatched", "border-amber-200 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/30 text-amber-800 dark:text-amber-200"],
    missing:  ["Add missing",       "border-sky-200 dark:border-sky-700 bg-sky-50 dark:bg-sky-900/30 text-sky-800 dark:text-sky-200"],
    orphan:   ["Remove orphans",    "border-rose-200 dark:border-rose-700 bg-rose-50 dark:bg-rose-900/30 text-rose-800 dark:text-rose-200"],
  };

  for (const scope of ["mismatch", "missing", "orphan"]) {
    const items = groups[scope];
    if (items.length === 0) continue;
    const [title, cls] = titles[scope];

    const section = document.createElement("section");
    section.className = `border rounded ${cls}`;
    section.innerHTML = `
      <div class="px-3 py-2 flex items-center gap-2 border-b border-current/10">
        <strong class="text-sm">${title}</strong>
        <span class="text-xs">${items.length}</span>
        <label class="ml-auto text-xs flex items-center gap-1 cursor-pointer">
          <input type="checkbox" data-toggle-all="${scope}" checked class="rounded">
          <span>select all</span>
        </label>
      </div>
    `;
    const list = document.createElement("ul");
    list.className = "divide-y divide-current/10 bg-white/60 dark:bg-slate-800/40";
    for (const item of items) {
      const idx = currentPlan.indexOf(item);
      list.appendChild(renderPlanRow(item, idx));
    }
    section.appendChild(list);
    $modalBody.appendChild(section);
  }

  updateApplyButton();
}

function renderPlanRow(item, index) {
  const li = document.createElement("li");
  li.className = "px-3 py-2 flex items-center gap-2 text-sm";
  li.innerHTML = `
    <input type="checkbox" data-plan-idx="${index}" ${item.checked ? "checked" : ""} class="rounded shrink-0" ${applying ? "disabled" : ""}>
    <div class="flex-1 min-w-0">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-mono text-xs">${escapeHtml(item.id)}</span>
        <span class="text-xs text-slate-500 dark:text-slate-400">${escapeHtml(item.dev.name || "—")}</span>
        <span class="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">${item.action}</span>
      </div>
      ${item.reasons.length ? `<div class="text-[11px] text-slate-600 dark:text-slate-300 mt-1">${item.reasons.map(escapeHtml).join("<br>")}</div>` : ""}
    </div>
    <span data-status-idx="${index}" class="text-xs shrink-0">${statusLabel(item)}</span>
  `;
  return li;
}

function statusLabel(item) {
  switch (item.status) {
    case "pending":     return '<span class="text-slate-400 dark:text-slate-500">pending</span>';
    case "in_progress": return '<span class="text-slate-700 dark:text-slate-200">…</span>';
    case "ok":          return '<span class="text-emerald-600 dark:text-emerald-400">✓</span>';
    case "error":       return `<span class="text-rose-600 dark:text-rose-400" title="${escapeHtml(item.error || "")}">✘</span>`;
    default:            return "";
  }
}

function updateApplyButton() {
  const selected = currentPlan?.filter((i) => i.checked).length ?? 0;
  $modalApply.textContent = applying
    ? `Applying… (${currentPlan.filter((i) => i.status === "ok" || i.status === "error").length}/${selected})`
    : selected === 0
      ? "Apply"
      : `Apply ${selected} change${selected === 1 ? "" : "s"}`;
  $modalApply.disabled = applying || selected === 0;
}

async function applyBatch() {
  if (!currentPlan || applying) return;
  const selected = currentPlan.filter((i) => i.checked);
  if (selected.length === 0) return;

  applying = true;
  $modalCancel.disabled = true;
  $modalClose.disabled = true;
  // Disable all row checkboxes
  for (const el of $modalBody.querySelectorAll('input[type="checkbox"]')) el.disabled = true;
  updateApplyButton();

  let okCount = 0;
  let errCount = 0;
  for (const item of selected) {
    item.status = "in_progress";
    updateRowStatus(item);
    updateApplyButton();
    const res = await postCommand(buildCommandBody(item.action, item.dev));
    if (res.ok) {
      item.status = "ok";
      okCount++;
    } else {
      item.status = "error";
      item.error = res.error;
      errCount++;
    }
    updateRowStatus(item);
    updateApplyButton();
  }

  applying = false;
  $modalProgress.textContent = errCount === 0
    ? `All ${okCount} change${okCount === 1 ? "" : "s"} applied`
    : `${okCount} succeeded, ${errCount} failed`;
  toast(errCount === 0 ? `Synced ${okCount}` : `Synced ${okCount}, failed ${errCount}`, errCount === 0 ? "ok" : "error");
  // Re-enable cancel/close so the user can dismiss after reviewing
  $modalCancel.disabled = false;
  $modalClose.disabled = false;
  $modalApply.textContent = "Done";
  $modalApply.disabled = false;
  // Repurpose the apply button to a "close" once done
  $modalApply.onclick = () => { closeSyncModal(); $modalApply.onclick = applyBatch; };
}

function updateRowStatus(item) {
  const idx = currentPlan.indexOf(item);
  const el = $modalBody.querySelector(`[data-status-idx="${idx}"]`);
  if (el) el.innerHTML = statusLabel(item);
}

// Modal event wiring
$modalBody.addEventListener("change", (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLInputElement) || t.type !== "checkbox") return;
  if (applying) { t.checked = !t.checked; return; }
  if (t.dataset.toggleAll) {
    const scope = t.dataset.toggleAll;
    for (const item of currentPlan) {
      if (item.scope === scope) item.checked = t.checked;
    }
    renderModal();
    return;
  }
  if (t.dataset.planIdx !== undefined) {
    const idx = Number(t.dataset.planIdx);
    if (currentPlan[idx]) currentPlan[idx].checked = t.checked;
    updateApplyButton();
  }
});

$modalCancel.addEventListener("click", closeSyncModal);
$modalClose.addEventListener("click", closeSyncModal);
$modalApply.onclick = applyBatch;
// Esc closes when not applying
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$modal.classList.contains("hidden")) {
    closeSyncModal();
  }
});
// Backdrop click closes (only when not mid-apply)
$modal.addEventListener("click", (e) => {
  if (e.target === $modal) closeSyncModal();
});

// Top-bar category buttons → open modal
$syncBar.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-sync-scope]");
  if (!btn) return;
  openSyncModal(btn.dataset.syncScope);
});

// ── Wizard (Tuya Cloud login) ──────────────────────────────────────────────
// State machine mirrors the backend's WizardState enum. Backend response on
// any wizard endpoint is { state, qr_image_data_url, message, error, ... };
// we render the matching pane and poll while the flow is in progress.
let wizardPollTimer = null;

function openWizardModal() {
  showWizardPane("idle");
  $wizardModal.classList.remove("hidden");
  $wizardStart.disabled = false;
  $wizardStart.textContent = "Start";
  $wizardUserCode.value = localStorage.getItem("tuyaUserCode") || "";
  $wizardUserCode.focus();
}

function closeWizardModal() {
  stopWizardPoll();
  $wizardModal.classList.add("hidden");
}

function showWizardPane(name) {
  for (const pane of $wizardBody.querySelectorAll("[data-wizard-pane]")) {
    pane.classList.toggle("hidden", pane.dataset.wizardPane !== name);
  }
}

function applyWizardSession(s) {
  switch (s.state) {
    case "idle":
      showWizardPane("idle");
      $wizardStart.disabled = false;
      $wizardStart.textContent = "Start";
      break;
    case "requesting_qr":
      showWizardPane("requesting_qr");
      $wizardStart.disabled = true;
      break;
    case "awaiting_scan":
      showWizardPane("awaiting_scan");
      if (s.qr_image_data_url) $wizardQrImage.src = s.qr_image_data_url;
      $wizardStart.disabled = true;
      break;
    case "logged_in":
    case "fetching":
      showWizardPane("working");
      $wizardWorkingMsg.textContent = s.message || "Working…";
      $wizardStart.disabled = true;
      break;
    case "done":
      showWizardPane("done");
      $wizardDoneMsg.textContent = s.message || `Loaded ${s.devices_count} devices`;
      $wizardStart.disabled = true;
      $wizardStart.textContent = "Close";
      $wizardStart.disabled = false;
      stopWizardPoll();
      // Auto-close after a brief moment so the user sees the success state
      setTimeout(() => {
        if (wizardCurrentState() === "done") closeWizardModal();
      }, 2500);
      break;
    case "error":
      showWizardPane("error");
      $wizardErrorMsg.textContent = s.error || s.message || "Unknown error";
      $wizardStart.disabled = false;
      $wizardStart.textContent = "Try again";
      stopWizardPoll();
      break;
  }
}

function wizardCurrentState() {
  for (const pane of $wizardBody.querySelectorAll("[data-wizard-pane]")) {
    if (!pane.classList.contains("hidden")) return pane.dataset.wizardPane;
  }
  return null;
}

async function startWizard() {
  const userCode = $wizardUserCode.value.trim();
  if (userCode) localStorage.setItem("tuyaUserCode", userCode);

  // If we're sitting on the "done" or "error" pane, restart cleanly
  const cur = wizardCurrentState();
  if (cur === "done") { closeWizardModal(); return; }
  if (cur === "idle" || cur === "error") {
    try {
      const res = await fetch("/api/wizard/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_code: userCode }),
      });
      const session = await res.json();
      applyWizardSession(session);
      startWizardPoll();
    } catch (e) {
      $wizardErrorMsg.textContent = `network error: ${e.message}`;
      showWizardPane("error");
    }
  }
}

function startWizardPoll() {
  stopWizardPoll();
  wizardPollTimer = setInterval(async () => {
    try {
      const res = await fetch("/api/wizard/status");
      const session = await res.json();
      applyWizardSession(session);
    } catch (e) {
      // Transient network errors are fine; keep polling.
    }
  }, 1500);
}

function stopWizardPoll() {
  if (wizardPollTimer) {
    clearInterval(wizardPollTimer);
    wizardPollTimer = null;
  }
}

async function cancelWizard() {
  stopWizardPoll();
  try {
    await fetch("/api/wizard/cancel", { method: "POST" });
  } catch (e) { /* ignore */ }
  closeWizardModal();
}

$wizardOpen?.addEventListener("click", openWizardModal);
$wizardClose?.addEventListener("click", cancelWizard);
$wizardCancel?.addEventListener("click", cancelWizard);
$wizardStart?.addEventListener("click", startWizard);
$wizardModal?.addEventListener("click", (e) => {
  if (e.target === $wizardModal) cancelWizard();
});
$wizardUserCode?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") startWizard();
});

// ── Toasts ─────────────────────────────────────────────────────────────────
function toast(msg, kind = "ok") {
  const styles = {
    ok:    "bg-slate-900 dark:bg-slate-200 text-white dark:text-slate-900",
    error: "bg-rose-600 dark:bg-rose-500 text-white",
  }[kind];
  const t = document.createElement("div");
  t.className = `pointer-events-auto text-xs px-3 py-2 rounded shadow ${styles}`;
  t.textContent = msg;
  $toasts.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ── Helpers ────────────────────────────────────────────────────────────────
function shorten(s, len = 12) {
  if (!s) return "";
  if (s.length <= len) return s;
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}

function formatDpsValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function formatAgo(ts) {
  const sec = Math.max(0, Date.now() / 1000 - ts);
  if (sec < 1)     return "just now";
  if (sec < 60)    return `${Math.floor(sec)}s ago`;
  if (sec < 3600)  return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

// Re-render the "Xs ago" labels every 5 seconds without a full re-render.
setInterval(() => {
  for (const el of document.querySelectorAll("[data-lastseen]")) {
    el.textContent = formatAgo(Number(el.dataset.lastseen));
  }
}, 5000);

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// ── Filter / search / sort wiring ──────────────────────────────────────────
$filterTabs.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-filter]");
  if (!btn) return;
  filter = btn.dataset.filter;
  if (snapshot) renderFilterCounts();   // reapply active/idle styles
  renderDevices();
});

$search.addEventListener("input", (e) => {
  query = e.target.value.trim();
  renderDevices();
});

// `/` focuses search, ESC clears
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $search) {
    e.preventDefault();
    $search.focus();
  } else if (e.key === "Escape" && document.activeElement === $search) {
    $search.value = "";
    query = "";
    renderDevices();
  }
});

$sort.value = sortKey;
$sort.addEventListener("change", (e) => {
  sortKey = e.target.value;
  localStorage.setItem("sortKey", sortKey);
  renderDevices();
});

const $refreshBtn = document.getElementById("refresh-btn");
$refreshBtn.addEventListener("click", async () => {
  // Keep the label stable — the refresh usually completes in <100ms and a
  // "refreshing…" flicker just makes the button size jitter. Disabled is
  // enough visual feedback; the toast confirms completion.
  $refreshBtn.disabled = true;
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    snapshot = await res.json();
    render();
    await postCommand({ action: "status", id: "bridge" });
    const bridgeCount = Object.keys(snapshot.bridge).length;
    const cloudCount = Object.keys(snapshot.cloud).length;
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
const $themeBtn = document.getElementById("theme-btn");
$themeBtn?.addEventListener("click", () => {
  const dark = document.documentElement.classList.toggle("dark");
  localStorage.setItem("theme", dark ? "dark" : "light");
});

connect();
