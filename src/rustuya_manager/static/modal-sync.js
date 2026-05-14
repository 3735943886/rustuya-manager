// Bulk-sync modal. Builds a plan from the current diff, lets the user
// uncheck individual rows, then publishes each checked item sequentially
// and reflects per-row status. Modal owns its own element refs and the
// `currentPlan` / `applying` state — nothing else in the app needs them.

import { state } from "./state.js";
import { escapeHtml, toast } from "./dom.js";
import { postCommand, buildCommandBody } from "./api.js";

const $modal = document.getElementById("sync-modal");
const $modalBody = document.getElementById("sync-modal-body");
const $modalTitle = document.getElementById("sync-modal-title");
const $modalApply = document.getElementById("sync-modal-apply");
const $modalCancel = document.getElementById("sync-modal-cancel");
const $modalClose = document.getElementById("sync-modal-close");
const $modalProgress = document.getElementById("sync-modal-progress");
const $syncBar = document.getElementById("sync-bar");

let currentPlan = null;
let applying = false;

function buildPlan(scope) {
  const snap = state.snapshot;
  const plan = [];
  if (scope === "all" || scope === "mismatch") {
    for (const m of snap.diff.mismatched) {
      const dev = snap.cloud[m.id];
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
    for (const id of snap.diff.missing) {
      const dev = snap.cloud[id];
      if (!dev) continue;
      plan.push({
        scope: "missing",
        id, action: "add", dev, reasons: [],
        checked: true, status: "pending", error: null,
      });
    }
  }
  if (scope === "all" || scope === "orphan") {
    for (const id of snap.diff.orphaned) {
      const dev = snap.bridge[id];
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

export function openSyncModal(scope) {
  if (!state.snapshot) return;
  currentPlan = buildPlan(scope);
  if (currentPlan.length === 0) {
    toast("Nothing to sync in that category", "ok");
    return;
  }
  const titles = {
    all: "Apply all changes",
    mismatch: "Update mismatches",
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

export function closeSyncModal() {
  if (applying) return; // refuse to close mid-apply
  $modal.classList.add("hidden");
  currentPlan = null;
}

function renderModal() {
  $modalBody.innerHTML = "";

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
    // Reflect the actual selection state. Hard-coding `checked` here caused
    // a double-bug where the toggle visually disagreed with its items in
    // both the "all selected" and "none selected" cases.
    const allChecked = items.every((i) => i.checked);
    const someChecked = items.some((i) => i.checked);

    const section = document.createElement("section");
    section.className = `border rounded ${cls}`;
    section.innerHTML = `
      <div class="px-3 py-2 flex items-center gap-2 border-b border-current/10">
        <strong class="text-sm">${title}</strong>
        <span class="text-xs">${items.length}</span>
        <label class="ml-auto text-xs flex items-center gap-1 cursor-pointer">
          <input type="checkbox" data-toggle-all="${scope}"${allChecked ? " checked" : ""} class="rounded">
          <span>select all</span>
        </label>
      </div>
    `;
    // `indeterminate` is a property, not an HTML attribute — it has to be
    // set in JS after the element exists.
    const toggleAll = section.querySelector(`input[data-toggle-all="${scope}"]`);
    if (toggleAll) toggleAll.indeterminate = someChecked && !allChecked;
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

// Display verb per scope. `item.action` stays bound to the MQTT command
// vocabulary ("add"/"remove") because that's what `buildCommandBody` and the
// bridge expect — re-publishing "add" on a mismatch is how field drift is
// reconciled, so we surface that intent as "update" to the user without
// touching the wire payload.
const ROW_LABELS = { mismatch: "update", missing: "add", orphan: "remove" };

function renderPlanRow(item, index) {
  const li = document.createElement("li");
  // items-start so a wrapped reasons block doesn't push the checkbox down to
  // center on a tall row.
  li.className = "px-3 py-2 flex items-start gap-2 text-sm";
  // `for=`/`id=` pairing: the browser routes any click inside the <label>
  // back to its target checkbox natively — same pattern as the section's
  // "select all" toggle, so the whole row reads consistently as one widget.
  // The status pill stays outside the label since it's display-only.
  const cbId = `sync-plan-cb-${index}`;
  li.innerHTML = `
    <input type="checkbox" id="${cbId}" data-plan-idx="${index}" ${item.checked ? "checked" : ""} class="mt-1 rounded shrink-0" ${applying ? "disabled" : ""}>
    <label for="${cbId}" class="flex-1 min-w-0 cursor-pointer">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-mono text-xs break-all">${escapeHtml(item.id)}</span>
        <span class="text-xs text-slate-500 dark:text-slate-400 break-words">${escapeHtml(item.dev.name || "—")}</span>
        <span class="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">${ROW_LABELS[item.scope]}</span>
      </div>
      ${item.reasons.length ? `<div class="text-[11px] text-slate-600 dark:text-slate-300 mt-1 break-all">${item.reasons.map(escapeHtml).join("<br>")}</div>` : ""}
    </label>
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
  $modalCancel.disabled = false;
  $modalClose.disabled = false;
  $modalApply.textContent = "Done";
  $modalApply.disabled = false;
  // Repurpose the apply button to "close" once done — reverted below on reopen
  $modalApply.onclick = () => { closeSyncModal(); $modalApply.onclick = applyBatch; };
}

function updateScopeToggle(scope) {
  const items = currentPlan?.filter((i) => i.scope === scope) ?? [];
  if (items.length === 0) return;
  const el = $modalBody.querySelector(`input[data-toggle-all="${scope}"]`);
  if (!el) return;
  const allChecked = items.every((i) => i.checked);
  const someChecked = items.some((i) => i.checked);
  el.checked = allChecked;
  el.indeterminate = someChecked && !allChecked;
}

function updateRowStatus(item) {
  const idx = currentPlan.indexOf(item);
  const el = $modalBody.querySelector(`[data-status-idx="${idx}"]`);
  if (el) el.innerHTML = statusLabel(item);
}

// Wire modal events. These are owned by the modal module so app.js doesn't
// need to re-export internal handlers like applyBatch.
export function initSyncModal() {
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
      if (currentPlan[idx]) {
        currentPlan[idx].checked = t.checked;
        // Keep the scope's select-all toggle in sync — without this an
        // individual uncheck would leave the toggle stuck on "all selected".
        updateScopeToggle(currentPlan[idx].scope);
      }
      updateApplyButton();
    }
  });

  $modalCancel.addEventListener("click", closeSyncModal);
  $modalClose.addEventListener("click", closeSyncModal);
  $modalApply.onclick = applyBatch;

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$modal.classList.contains("hidden")) {
      closeSyncModal();
    }
  });
  $modal.addEventListener("click", (e) => {
    if (e.target === $modal) closeSyncModal();
  });

  $syncBar.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-sync-scope]");
    if (!btn) return;
    openSyncModal(btn.dataset.syncScope);
  });
}
