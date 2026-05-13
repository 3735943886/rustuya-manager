// Device cards. The renderer over here owns the "stateful" card glue:
// expansion, sync-class lookup, IP resolution policy, action buttons.

import { state, expandedIds, saveExpanded } from "./state.js";
import {
  ICON_BASE, escapeHtml, shorten, formatDpsValue, formatAgo,
  liveDot, iconButton, button, statusPill,
} from "./dom.js";
import { sync, publishCommand } from "./api.js";
import { openEditModal } from "./modal-device.js";
import { confirm } from "./modal-confirm.js";
// Cycle: render.js imports deviceCard from this module. The import is fine
// because ES module bindings are live — by the time toggleExpand fires from
// a click handler, both modules are fully evaluated.
import { renderDevices } from "./render.js";

export function classifyDevice(id) {
  const snap = state.snapshot;
  if (!snap.cloud_loaded) return "ungrouped";
  if (snap.diff.missing.includes(id)) return "missing";
  if (snap.diff.orphaned.includes(id)) return "orphan";
  if (snap.diff.mismatched.some((m) => m.id === id)) return "mismatch";
  return "synced";
}

export function primaryDevice(id) {
  return state.snapshot.cloud[id] || state.snapshot.bridge[id] || null;
}

export function toggleExpand(id) {
  if (expandedIds.has(id)) expandedIds.delete(id);
  else expandedIds.add(id);
  saveExpanded();
  renderDevices();
}

function computeEdgeColor(cls, live) {
  // 400 in light mode, 500 in dark — the more saturated 500 reads better
  // against the slate-800 card background. Slate variants step away from
  // mid-gray (which would blend with the card border) into colors that
  // can be told apart at a glance when scanning a column.
  if (cls === "mismatch") return "border-l-amber-400 dark:border-l-amber-500";
  if (cls === "missing")  return "border-l-sky-400 dark:border-l-sky-500";
  if (cls === "orphan")   return "border-l-rose-400 dark:border-l-rose-500";
  if (cls === "ungrouped") return "border-l-slate-300 dark:border-l-slate-500";
  if (live?.state === "offline") return "border-l-slate-400 dark:border-l-slate-500";
  if (live?.state === "online")  return "border-l-emerald-400 dark:border-l-emerald-500";
  return "border-l-slate-200 dark:border-l-slate-600";
}

function computeCardBg(cls) {
  // Synced + ungrouped cards stay at the baseline (max text-vs-bg contrast).
  // Problem cards (mismatch / missing / orphan) pull the background toward
  // the text color so contrast *drops*: that's what "muddy / needs fixing"
  // actually means. Crucially this is asymmetric per mode — go darker in
  // light (toward black text) and lighter in dark (toward white text).
  if (cls === "synced" || cls === "ungrouped") {
    return "bg-white dark:bg-slate-800";
  }
  return "bg-slate-100 dark:bg-slate-700";
}

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

function expandCaret(id, isExpanded) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `${ICON_BASE} text-slate-400 dark:text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 text-xs`;
  b.textContent = isExpanded ? "▾" : "▸";
  b.title = isExpanded ? "Collapse" : "Expand";
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleExpand(id);
  });
  return b;
}

async function removeWithConfirm(id, name) {
  const display = name && name !== "N/A" ? `${name} (${id})` : id;
  const ok = await confirm({
    title: "Remove device?",
    message: `Remove ${display} from the bridge?\nThis publishes a 'remove' command and cannot be undone by the manager.`,
    okLabel: "Remove",
    danger: true,
  });
  if (ok) await publishCommand({ action: "remove", id });
}

function appendInlineActions(container, id, cls, cloud, bridge, primary) {
  if (cls === "missing") {
    container.appendChild(button("Add", () => sync("add", primary)));
  } else if (cls === "mismatch") {
    container.appendChild(button("Update", () => sync("add", cloud)));
  }
  // Orphan no longer gets a dedicated "Remove" text button — the 🗑 icon
  // below covers it (with a confirm) and avoids two ways to do the same
  // thing right next to each other.
  //
  // Edit/remove icons only make sense for devices the bridge knows about,
  // which excludes the "missing" class (cloud-only). Top-bar "+" handles
  // the add-from-scratch flow for those.
  if (cls !== "missing") {
    container.appendChild(
      iconButton("✎", () => openEditModal(id), "Edit device"),
    );
    container.appendChild(
      iconButton("🗑", () => removeWithConfirm(id, primary.name), "Remove device", "danger"),
    );
    container.appendChild(
      iconButton("↻", () => publishCommand({ action: "get", id }), "Query status from bridge"),
    );
  }
}

export function deviceCard(id, cls, isChild) {
  const snap = state.snapshot;
  const cloud = snap.cloud[id];
  const bridge = snap.bridge[id];
  const primary = cloud || bridge;
  const ipInfo = resolveIp(bridge, cloud);
  const dps = snap.dps[id] || {};
  const lastSeen = snap.last_seen[id];
  const live = snap.live_status?.[id];
  const isExpanded = expandedIds.has(id);

  const edgeColor = computeEdgeColor(cls, live);
  const indent = isChild ? "ml-4 md:ml-8" : "";

  const card = document.createElement("div");
  // 4 px strip in light mode, 6 px in dark — the wider stripe gives the
  // color more visual mass against the darker card so it remains a usable
  // scanning cue.
  card.className = `${computeCardBg(cls)} rounded-lg border border-slate-200 dark:border-slate-700 border-l-4 dark:border-l-[6px] ${edgeColor} p-3 ${indent} cursor-pointer`;
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
  // No type badge — sub-devices are visually distinguished by their tree
  // indentation under the parent gateway, so a "W"/"S" letter is redundant.
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
    const m = snap.diff.mismatched.find((m) => m.id === id);
    if (m) {
      const reasons = document.createElement("div");
      // break-all (not break-words) because reasons often contain long
      // monospace keys/IDs with no whitespace to break on. The user wants
      // to *read* these in full, so wrap rather than truncate+ellipsis.
      reasons.className = "mt-1.5 text-xs text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700 rounded px-2 py-1 break-all";
      reasons.innerHTML = m.reasons.map(escapeHtml).join("<br>");
      card.appendChild(reasons);
    }
  }

  const dpsEntries = Object.entries(dps).filter(
    ([, v]) => v !== "" && v !== null && v !== undefined
  );
  if (dpsEntries.length > 0) {
    // min-w-0 on the wrapper is what actually allows truncate to kick in
    // inside a flex layout — without it, the parent stretches to the chip's
    // natural width and overflows the card. Each chip caps at max-w-[20rem]
    // and uses `truncate` to ellipsize long values (e.g. base64 status
    // blobs); the full value is still available via the hover title.
    const dpsRow = document.createElement("div");
    dpsRow.className = "mt-1.5 flex flex-wrap gap-1 min-w-0";
    for (const [k, v] of dpsEntries) {
      const chip = document.createElement("span");
      chip.className = "inline-block align-middle text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 border border-slate-200 dark:border-slate-600 text-slate-700 dark:text-slate-200 max-w-full md:max-w-[18rem] truncate";
      const text = `dp${k}=${formatDpsValue(v)}`;
      chip.textContent = text;
      chip.title = text;
      dpsRow.appendChild(chip);
    }
    card.appendChild(dpsRow);
  }

  return card;
}

export function missingParentCard(parent_id) {
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
