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

// ── Element refs ────────────────────────────────────────────────────────────
const $list = document.getElementById("device-list");
const $empty = document.getElementById("empty-state");
const $conn = document.getElementById("conn-badge");
const $rootLabel = document.getElementById("root-label");
const $templates = document.getElementById("templates-block");
const $summary = document.getElementById("summary");
const $filterTabs = document.getElementById("filter-tabs");
const $toasts = document.getElementById("toast-container");
const $search = document.getElementById("search-input");
const $sort = document.getElementById("sort-select");
const $banner = document.getElementById("cloud-banner");
const $dropzone = document.getElementById("cloud-dropzone");
const $pickBtn = document.getElementById("cloud-pick-btn");
const $fileInput = document.getElementById("cloud-file-input");

// ── Connection ──────────────────────────────────────────────────────────────
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function setConn(state) {
  const styles = {
    connecting: ["bg-slate-100 text-slate-600 border-slate-300", "● connecting…", true],
    live:       ["bg-emerald-100 text-emerald-700 border-emerald-300", "● live", false],
    lost:       ["bg-rose-100 text-rose-700 border-rose-300", "● disconnected — retrying", true],
  };
  const [cls, label, pulse] = styles[state];
  $conn.className = `text-xs px-2 py-1 rounded-full border ${cls}`;
  $conn.innerHTML = pulse
    ? `<span class="pulse-dot">${label.split(" ")[0]}</span>${label.slice(1)}`
    : label;
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
  renderSummary();
  renderBanner();
  renderDevices();
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
    row.innerHTML = `<span class="text-slate-400 w-20 inline-block">${k}</span><span>${escapeHtml(v)}</span>`;
    $templates.appendChild(row);
  }
}

function renderSummary() {
  const counts = {
    synced: snapshot.diff.synced.length,
    mismatched: snapshot.diff.mismatched.length,
    missing: snapshot.diff.missing.length,
    orphaned: snapshot.diff.orphaned.length,
  };
  for (const el of $summary.querySelectorAll("[data-summary]")) {
    el.textContent = counts[el.dataset.summary] ?? 0;
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
  const dps = snapshot.dps[id] || {};
  const lastSeen = snapshot.last_seen[id];

  const card = document.createElement("div");
  card.className = `bg-white rounded-lg border border-slate-200 p-3 md:p-4 ${isChild ? "ml-4 md:ml-8 border-l-2 border-l-slate-300" : ""}`;

  // Header line
  const header = document.createElement("div");
  header.className = "flex flex-wrap items-center gap-2 mb-2";
  header.innerHTML = `
    ${isChild ? '<span class="text-slate-400 text-xs">└</span>' : ""}
    <span class="font-mono text-sm">${escapeHtml(id)}</span>
    <span class="text-sm text-slate-500">${escapeHtml(primary.name || "")}</span>
    ${statusPill(cls)}
    ${typePill(primary.type)}
    ${lastSeen ? `<span class="text-[10px] text-slate-400 ml-auto" data-lastseen="${lastSeen}">${formatAgo(lastSeen)}</span>` : ""}
  `;
  card.appendChild(header);

  // Field grid
  const grid = document.createElement("div");
  grid.className = "grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs text-slate-600";
  const fields = [
    ["IP", primary.ip],
    ["KEY", primary.key ? shorten(primary.key) : "—"],
  ];
  if (primary.type === "SubDevice") {
    fields.push(["CID", primary.cid || "—"], ["PARENT", shorten(primary.parent_id) || "—"]);
  } else {
    fields.push(["VER", primary.version], ["STATUS", primary.status || "—"]);
  }
  for (const [k, v] of fields) {
    const f = document.createElement("div");
    f.innerHTML = `<span class="text-slate-400">${k}</span> <span class="font-mono">${escapeHtml(String(v))}</span>`;
    grid.appendChild(f);
  }
  card.appendChild(grid);

  // Mismatch reasons
  if (cls === "mismatch") {
    const m = snapshot.diff.mismatched.find((m) => m.id === id);
    if (m) {
      const reasons = document.createElement("div");
      reasons.className = "mt-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1";
      reasons.innerHTML = m.reasons.map(escapeHtml).join("<br>");
      card.appendChild(reasons);
    }
  }

  // Live DPS chips
  if (Object.keys(dps).length > 0) {
    const dpsRow = document.createElement("div");
    dpsRow.className = "mt-2 flex flex-wrap gap-1";
    for (const [k, v] of Object.entries(dps)) {
      const chip = document.createElement("span");
      chip.className = "text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 border border-slate-200";
      chip.textContent = `dp${k}=${formatDpsValue(v)}`;
      dpsRow.appendChild(chip);
    }
    card.appendChild(dpsRow);
  }

  // Action row
  const actions = document.createElement("div");
  actions.className = "mt-3 flex flex-wrap gap-2";
  if (cls === "missing") {
    actions.appendChild(button("Add to bridge", () => sync("add", primary)));
  } else if (cls === "orphan") {
    actions.appendChild(button("Remove from bridge", () => sync("remove", primary), "danger"));
  } else if (cls === "mismatch") {
    actions.appendChild(button("Update bridge", () => sync("add", cloud)));
  }
  if (cls !== "missing") {
    actions.appendChild(button("Query status", () => publishCommand({ action: "get", id })));
  }
  if (actions.childElementCount > 0) card.appendChild(actions);

  return card;
}

function missingParentCard(parent_id) {
  const card = document.createElement("div");
  card.className = "bg-white rounded-lg border-2 border-dashed border-sky-300 p-3 md:p-4";
  card.innerHTML = `
    <div class="flex flex-wrap items-center gap-2">
      <span class="font-mono text-sm text-slate-700">${escapeHtml(parent_id)}</span>
      ${statusPill("missing")}
      <span class="text-[10px] px-1.5 py-0.5 rounded-full border bg-slate-100 text-slate-600 border-slate-300">missing parent</span>
    </div>
    <div class="text-xs text-slate-500 mt-1">
      Sub-device(s) reference this parent, but the parent is not in cloud or bridge.
    </div>
  `;
  return card;
}

function button(label, onClick, variant = "default") {
  const styles = {
    default: "border-slate-300 bg-white hover:bg-slate-100 text-slate-700",
    danger:  "border-rose-300 bg-white hover:bg-rose-50 text-rose-700",
  }[variant];
  const b = document.createElement("button");
  b.className = `text-xs px-3 py-1.5 rounded border ${styles}`;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function statusPill(cls) {
  const map = {
    synced:    ["bg-emerald-100 text-emerald-700 border-emerald-200", "synced"],
    mismatch:  ["bg-amber-100 text-amber-700 border-amber-200", "mismatch"],
    missing:   ["bg-sky-100 text-sky-700 border-sky-200", "missing"],
    orphan:    ["bg-rose-100 text-rose-700 border-rose-200", "orphan"],
    ungrouped: ["bg-slate-100 text-slate-600 border-slate-200", "ungrouped"],
  };
  const [s, label] = map[cls];
  return `<span class="text-[10px] px-1.5 py-0.5 rounded-full border ${s} uppercase tracking-wide">${label}</span>`;
}

function typePill(t) {
  return `<span class="text-[10px] px-1.5 py-0.5 rounded-full border bg-slate-100 text-slate-600 border-slate-300">${escapeHtml(t)}</span>`;
}

// ── Actions (POST /api/command) ────────────────────────────────────────────
async function sync(action, dev) {
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
  await publishCommand(body);
}

async function publishCommand(body) {
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.text();
      toast(`error: ${err}`, "error");
      return;
    }
    toast(`${body.action} → ${body.id || "bridge"} sent`, "ok");
  } catch (e) {
    toast(`network error: ${e.message}`, "error");
  }
}

// ── Toasts ─────────────────────────────────────────────────────────────────
function toast(msg, kind = "ok") {
  const styles = {
    ok:    "bg-slate-900 text-white",
    error: "bg-rose-600 text-white",
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
  for (const b of $filterTabs.querySelectorAll("button")) {
    b.classList.toggle("bg-slate-900", b === btn);
    b.classList.toggle("text-white", b === btn);
    b.classList.toggle("bg-white", b !== btn);
  }
  renderDevices();
});

$filterTabs.querySelector('[data-filter="all"]').classList.add("bg-slate-900", "text-white");
$filterTabs.querySelector('[data-filter="all"]').classList.remove("bg-white");

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

document.getElementById("refresh-btn").addEventListener("click", async () => {
  const res = await fetch("/api/state");
  snapshot = await res.json();
  render();
});

connect();
