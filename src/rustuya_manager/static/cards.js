// Device cards. The renderer over here owns the "stateful" card glue:
// expansion, sync-class lookup, IP resolution policy, action buttons.

import { state, expandedIds, saveExpanded } from "./state.js";
import {
  ICON_BASE, escapeHtml, formatDpsValue, formatAgo,
  liveDot, scanDot, iconButton, button, statusPill,
} from "./dom.js";
import { sync, publishCommand } from "./api.js";
import { openEditModal } from "./modal-device.js";
import { confirm } from "./modal-confirm.js";
import { t } from "./i18n.js";
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
  // Each problem class gets a faint wash of its category color so the card
  // body — not just the left stripe — telegraphs the state at a glance.
  // Synced + ungrouped stay neutral so attention lands on actionable rows.
  if (cls === "missing")  return "bg-sky-50 dark:bg-sky-900/40";
  if (cls === "orphan")   return "bg-rose-50 dark:bg-rose-900/40";
  if (cls === "mismatch") return "bg-amber-50 dark:bg-amber-900/40";
  return "bg-white dark:bg-slate-800";
}

// Color-class for a scan-result cell, picked by comparing what the cloud
// JSON says the field should be against what the LAN scan actually saw.
//
//   amber — cloud has no value to compare ("Auto" or empty); scan is
//           purely informational. Common case for fresh wizard imports.
//   rose  — cloud has a concrete value and it disagrees with scan;
//           something drifted (DHCP, manual reassign, swapped device).
//   ""    — values match, no color needed.
//
// Tailwind utility tokens, not full classes — appended to the value
// span's existing utility list in the renderer.
function scanFieldClass(cloudValue, scanValue) {
  const cloudEmpty = !cloudValue || cloudValue === "Auto";
  if (cloudEmpty) {
    if (!scanValue) return "";  // nothing useful either side, keep neutral
    return "text-amber-700 dark:text-amber-300";
  }
  if (!scanValue) return "";  // scan didn't carry this field — no claim to compare against
  if (String(cloudValue) === String(scanValue)) return "";
  return "text-rose-700 dark:text-rose-300";
}

function resolveIp(bridge, cloud) {
  const cloudIp = cloud?.ip;
  if (bridge) {
    if (bridge.ip && bridge.ip !== "Auto") {
      // Bridge has a concrete IP — operational truth.
      if (cloudIp && cloudIp !== "Auto" && cloudIp !== bridge.ip) {
        return {
          value: bridge.ip,
          tooltip: t("card.ipConflict", { bridge: bridge.ip, cloud: cloudIp }),
        };
      }
      return { value: bridge.ip, tooltip: "" };
    }
    // bridge.ip === "Auto" → no fixed IP; the bridge follows DHCP / dynamic
    // assignment. Report that literally rather than substituting the cloud
    // value, which is the address the cloud-of-record has on file, not where
    // the bridge is actually connected. A public cloud IP never reaches here —
    // Device.from_dict normalizes it to "Auto" — so a shown cloud IP is a LAN
    // address.
    const tip =
      t("card.ipDynamic") +
      (cloudIp && cloudIp !== "Auto" ? t("card.ipCloudFile", { ip: cloudIp }) : "");
    return { value: "Auto", tooltip: tip };
  }
  // Device not in bridge — cloud is all we have.
  return { value: cloudIp || "—", tooltip: "" };
}

function resolveVer(bridge, cloud) {
  // Mirror resolveIp for the protocol version: the bridge's negotiated version
  // is operational truth, so prefer it over the cloud's value. The cloud often
  // omits version entirely (→ "Auto"), so showing primary.version would print
  // "Auto" even when the bridge knows the real version — inconsistent with the
  // IP cell, which already prefers the bridge value.
  const cloudVer = cloud?.version;
  if (bridge) {
    if (bridge.version && bridge.version !== "Auto") {
      if (cloudVer && cloudVer !== "Auto" && cloudVer !== bridge.version) {
        return {
          value: bridge.version,
          tooltip: t("card.verConflict", { bridge: bridge.version, cloud: cloudVer }),
        };
      }
      return { value: bridge.version, tooltip: "" };
    }
    // bridge.version === "Auto" → auto-negotiated; fall back to any cloud hint.
    return { value: cloudVer || "Auto", tooltip: "" };
  }
  return { value: cloudVer || "—", tooltip: "" };
}

function expandCaret(id, isExpanded) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `${ICON_BASE} text-slate-400 dark:text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 text-xs`;
  b.textContent = isExpanded ? "▾" : "▸";
  b.title = isExpanded ? t("card.collapse") : t("card.expand");
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleExpand(id);
  });
  return b;
}

async function removeWithConfirm(id, name) {
  const display = name && name !== "N/A" ? `${name} (${id})` : id;
  const ok = await confirm({
    title: t("confirm.removeTitle"),
    message: t("confirm.removeMsg", { device: display }),
    okLabel: t("common.remove"),
    danger: true,
  });
  if (ok) await publishCommand({ action: "remove", id });
}

function appendInlineActions(container, id, cls, cloud, bridge, primary) {
  // Tint the per-class action to its sync-class color so the button and
  // the device's edge stripe / filter tab read as one signal.
  if (cls === "missing") {
    container.appendChild(button(t("card.add"), () => sync("add", primary), "sky"));
  } else if (cls === "mismatch") {
    container.appendChild(button(t("card.update"), () => sync("add", cloud), "amber"));
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
      iconButton("✎", () => openEditModal(id), t("card.editDevice")),
    );
    // Orphan's primary (really, only) action is delete — promote the trash
    // icon to a filled rose tile so it reads as THE action on that card,
    // not just one of three equally-weighted icons.
    container.appendChild(
      iconButton(
        "🗑",
        () => removeWithConfirm(id, primary.name),
        t("card.removeDevice"),
        cls === "orphan" ? "danger-fill" : "danger",
      ),
    );
    container.appendChild(
      iconButton("↻", () => publishCommand({ action: "get", id }), t("card.queryStatus")),
    );
  }
}

export function deviceCard(id, cls, isChild) {
  const snap = state.snapshot;
  const cloud = snap.cloud[id];
  const bridge = snap.bridge[id];
  const primary = cloud || bridge;
  const ipInfo = resolveIp(bridge, cloud);
  const verInfo = resolveVer(bridge, cloud);
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
  // event from propagating up so they don't accidentally toggle. We also
  // skip the toggle if the user is finishing a drag-to-select inside the
  // card — without this, dragging across IP/KEY/VER text fires a click on
  // mouseup, the card collapses, and the selection is lost before they
  // can Ctrl/Cmd-C it. The browser sets the selection on mouseup *before*
  // the click event, so a non-collapsed selection anchored in this card
  // is a reliable "they were selecting, not tapping" signal.
  card.addEventListener("click", (ev) => {
    if (ev.target.closest("button, input, a, [contenteditable]")) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && card.contains(sel.anchorNode)) return;
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
  // Missing cards have no MQTT live status to show (the bridge doesn't
  // know the device), so the same slot carries LAN-scan visibility
  // instead. Same color grammar as liveDot's filled-vs-ring pair, just
  // tinted sky to match the missing class.
  rightCluster.appendChild(
    cls === "missing" ? scanDot(snap.scan_results?.[id]) : liveDot(live),
  );
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
  // Retained MQTT messages don't carry a publish timestamp, so we can't
  // honestly show "X ago" for data that arrived as a retain on cold start.
  // The server flags those ids in `retained_only`; once a live event flows
  // in we drop the flag and the regular formatAgo takes over.
  if (lastSeen) {
    const ls = document.createElement("span");
    ls.className = "ml-auto text-[10px] text-slate-400 dark:text-slate-500 shrink-0";
    ls.dataset.lastseen = String(lastSeen);
    ls.textContent = formatAgo(lastSeen);
    headerBottom.appendChild(ls);
  } else if (snap.retained_only?.includes(id)) {
    const ls = document.createElement("span");
    ls.className = "ml-auto text-[10px] italic text-slate-400 dark:text-slate-500 shrink-0";
    ls.title = t("card.cachedTitle");
    ls.textContent = t("card.cached");
    headerBottom.appendChild(ls);
  }
  card.appendChild(headerBottom);

  // Collapsed cards in the `synced` category have no other visual signal
  // for a live bridge error — mismatch / missing / orphan already telegraph
  // a data-state problem via the edge stripe and washed background, so
  // overlaying a runtime error on top would be noise. Synced cards stay
  // neutral, so when one of them errors (e.g. ERR_STATE 906 / ip_mismatch)
  // the message would otherwise be invisible behind a collapsed row.
  // Render the formatted MSG as a third row so it reads at a glance; the
  // expanded view already shows the same message in the field grid.
  if (!isExpanded && cls === "synced" && live?.state === "offline" && live.message) {
    const errLine = document.createElement("div");
    errLine.className = "mt-0.5 text-[11px] text-rose-600 dark:text-rose-400 truncate min-w-0";
    errLine.title = live.message;
    errLine.textContent = `⚠ ${live.message}`;
    card.appendChild(errLine);
  }

  if (!isExpanded) return card;

  // ── Expanded body: field grid + mismatch reasons + DPS chips ───────────
  // Sub-devices live behind a gateway, so IP and KEY are meaningless for
  // them — only the CID and parent relationship matter. WiFi devices show
  // IP/KEY/VER + any live error message from the bridge.
  //
  // IP/KEY/VER are shown in full (no shorten/truncate). KEY is 32 hex chars
  // and gets its own full-width row so it can wrap with `break-all` without
  // forcing the IP/VER cells to grow. Tooltip is reserved for cells that
  // carry an explanatory note (mismatch IP, retained-only freshness, etc).
  const grid = document.createElement("div");
  grid.className = "mt-2 grid grid-cols-2 md:grid-cols-4 gap-x-3 gap-y-0.5 text-xs text-slate-600 dark:text-slate-400";
  let fields;
  if (primary.type === "SubDevice") {
    // Mobile: CID and PARENT each take a full row. Without the explicit
    // col-span-2, mobile's grid-cols-2 puts them side-by-side and PARENT
    // (a 22-char device id) wraps to two lines inside half a row, while
    // CID stays a single line — visually unbalanced. Force one-per-row
    // on mobile; desktop keeps them paired in a single row.
    fields = [
      [t("field.cid"), primary.cid || "—", "", "col-span-2 md:col-span-2"],
      [t("field.parent"), primary.parent_id || "—", "", "col-span-2 md:col-span-2"],
    ];
  } else {
    // Desktop: IP (1/4) + VER (1/4) + KEY (1/2) fit on one row. The KEY
    // is 32 hex chars in mono — comfortably fits half a desktop card
    // width, and IP/VER values (max ~15 / ~3 chars) don't need 1/2 each.
    // Mobile keeps the existing 2-row layout (IP|VER on row 1, KEY on
    // row 2) because col-span without an md: prefix applies everywhere.
    fields = [
      [t("field.ip"), ipInfo.value, ipInfo.tooltip, "md:col-span-1"],
      [t("field.ver"), verInfo.value, verInfo.tooltip, "md:col-span-1"],
      [t("field.key"), primary.key || "—", "", "col-span-2 md:col-span-2"],
    ];
    if (live?.message) fields.push([t("field.msg"), live.message, "", "col-span-2 md:col-span-4"]);
  }
  // For missing-class cards (cloud-only — bridge doesn't know them), if
  // the latest LAN scan saw the device, surface the observed IP/VER in
  // a second pair of cells so the user can compare cloud config against
  // what's actually on the wire. Add (the per-card action) still reads
  // from cloud only — these cells are display-only, never an input to
  // the publish path.
  //
  // Field tuple: [label, value, tooltip, colSpanClass, valueColorClass].
  // colSpan controls the cell width inside the grid; valueColor tints
  // the value span (used by SCAN rows to flag drift in amber/rose).
  const sighting = snap.scan_results?.[id];
  if (cls === "missing" && sighting) {
    fields.push(
      [t("field.scanIp"), sighting.ip || "—", t("field.scanTooltip"), "md:col-span-2", scanFieldClass(primary.ip, sighting.ip)],
      [t("field.scanVer"), sighting.version || "—", t("field.scanTooltip"), "md:col-span-2", scanFieldClass(primary.version, sighting.version)],
    );
  }
  for (const entry of fields) {
    const [k, v, tooltip, span, valueClass] = entry;
    const f = document.createElement("div");
    f.className = `flex gap-1 min-w-0 ${span || ""}`;
    const titleAttr = tooltip ? ` title="${escapeHtml(tooltip)}"` : "";
    const extra = valueClass ? ` ${valueClass}` : "";
    f.innerHTML = `<span class="text-slate-400 dark:text-slate-500 shrink-0">${k}</span><span class="font-mono break-all min-w-0${extra}"${titleAttr}>${escapeHtml(String(v))}</span>`;
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
      <span class="text-[10px] px-1.5 py-0.5 rounded-full border bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600">${escapeHtml(t("card.missingParent"))}</span>
    </div>
    <div class="text-xs text-slate-500 dark:text-slate-400 mt-1">
      ${escapeHtml(t("card.missingParentDesc"))}
    </div>
  `;
  return card;
}
