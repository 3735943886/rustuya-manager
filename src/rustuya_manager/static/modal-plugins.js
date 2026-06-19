// Manage-plugins modal — host-owned UI over the curated catalog.
//
// This is deliberately NOT a plugin tab: it's part of the manager shell so it
// works with zero plugins installed and never depends on any plugin being
// present. It drives the lifecycle endpoints in web.py:
//   GET  /api/plugins/catalog   list + per-entry install state
//   POST /api/plugins/install   add-only, takes effect live (no restart)
//   POST /api/plugins/update    replace on disk            (restart required)
//   POST /api/plugins/uninstall remove from disk           (restart required)
//   POST /api/plugins/toggle    enable/disable flag        (restart required)
//
// Install is the only action that lands live; everything that must drop or swap
// already-imported code returns restart_required, which surfaces the amber
// "Restart now" button (POST /api/restart, then the WS reconnect + boot-id
// change reloads the page to pick up the new tab set).

import { escapeHtml, toast, button } from "./dom.js";
import { confirm } from "./modal-confirm.js";
import { t } from "./i18n.js";
import {
  getCatalog,
  installPlugin,
  updatePlugin,
  uninstallPlugin,
  togglePlugin,
} from "./api.js";

const $modal = document.getElementById("plugins-modal");
const $body = document.getElementById("plugins-modal-body");
const $subtitle = document.getElementById("plugins-modal-subtitle");
const $note = document.getElementById("plugins-modal-note");
const $done = document.getElementById("plugins-modal-done");
const $close = document.getElementById("plugins-modal-close");
const $restart = document.getElementById("plugins-modal-restart");

// Sticky across actions within one open session: once any action needs a
// restart, the prompt stays until the user restarts or closes the modal.
let needsRestart = false;

function close() {
  $modal.classList.add("hidden");
}

function badge(text, cls) {
  return `<span class="text-[11px] px-1.5 py-0.5 rounded ${cls}">${escapeHtml(text)}</span>`;
}

function statusBadges(p, apiVersion) {
  const out = [];
  if (p.installed) {
    out.push(
      p.enabled
        ? badge(t("plugins.installed"), "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300")
        : badge(t("plugins.disabled"), "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300")
    );
    if (p.update_available) {
      out.push(badge(t("plugins.updateAvailable"), "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300"));
    }
  }
  if ((p.min_api || 1) > apiVersion) {
    out.push(badge(t("plugins.needsNewer"), "bg-rose-100 text-rose-700 dark:bg-rose-900/50 dark:text-rose-300"));
  }
  return out.join(" ");
}

// Re-fetch the catalog and re-render the rows. Called on open and after every
// action so the displayed state always matches the ledger on disk.
async function refresh() {
  const data = await getCatalog();
  if (data.ok === false) {
    $body.innerHTML = `<div class="text-sm text-rose-600 dark:text-rose-400">${escapeHtml(t("plugins.catalogError", { error: data.error || t("common.unknown") }))}</div>`;
    return;
  }
  const plugins = data.plugins || [];
  const apiVersion = data.api_version || 1;
  $subtitle.textContent = t("plugins.subtitle", { count: plugins.length, version: apiVersion });
  $note.textContent = data.managed
    ? t("plugins.noteManaged")
    : t("plugins.noteUnmanaged");

  if (plugins.length === 0) {
    $body.innerHTML = `<div class="text-sm text-slate-500 dark:text-slate-400">${escapeHtml(t("plugins.empty"))}</div>`;
    return;
  }

  $body.replaceChildren(
    ...plugins.map((p) => renderRow(p, apiVersion, Boolean(data.managed)))
  );
}

function renderRow(p, apiVersion, managed) {
  const row = document.createElement("div");
  row.className =
    "border border-slate-200 dark:border-slate-700 rounded-md p-3 flex flex-col gap-2";

  const ver = p.installed && p.installed_version ? p.installed_version : p.version;
  const home = p.homepage
    ? `<a href="${escapeHtml(p.homepage)}" target="_blank" rel="noopener" class="text-xs text-sky-600 dark:text-sky-400 hover:underline">${escapeHtml(t("plugins.homepage"))}</a>`
    : "";
  row.innerHTML = `
    <div class="flex items-start gap-2">
      <div class="min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="font-medium text-sm">${escapeHtml(p.name || p.id)}</span>
          <span class="text-xs text-slate-400">v${escapeHtml(ver || "?")}</span>
          ${statusBadges(p, apiVersion)}
        </div>
        <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${escapeHtml(p.description || "")}</div>
        ${home}
      </div>
    </div>`;

  const actions = document.createElement("div");
  actions.className = "flex flex-wrap gap-2";
  const incompatible = (p.min_api || 1) > apiVersion;

  if (!p.installed) {
    actions.appendChild(
      disableIf(
        button(t("plugins.install"), () => act(() => installPlugin(p.id), p.id, t("plugins.verbInstalled")), "sky"),
        !managed || incompatible
      )
    );
  } else {
    if (p.update_available) {
      actions.appendChild(
        disableIf(
          button(t("plugins.update"), () => act(() => updatePlugin(p.id), p.id, t("plugins.verbUpdated")), "amber"),
          incompatible
        )
      );
    }
    actions.appendChild(
      p.enabled
        ? button(t("plugins.disable"), () => act(() => togglePlugin(p.id, false), p.id, t("plugins.verbDisabled")))
        : button(t("plugins.enable"), () => act(() => togglePlugin(p.id, true), p.id, t("plugins.verbEnabled")))
    );
    actions.appendChild(
      button(t("plugins.uninstall"), () => uninstall(p.id, p.name || p.id), "danger")
    );
  }
  row.appendChild(actions);
  return row;
}

function disableIf(btn, disabled) {
  if (disabled) {
    btn.disabled = true;
    btn.classList.add("opacity-50", "cursor-not-allowed");
  }
  return btn;
}

// Run a mutating action, surface the result, set the restart prompt if needed,
// and re-render. `verb` is the success past-tense ("Installed"/"Updated"/…).
async function act(fn, id, verb) {
  const res = await fn();
  if (res.ok === false) {
    toast(t("plugins.actionFailed", { id, error: res.error || t("common.failed") }), "error");
    return;
  }
  toast(t("plugins.actionOk", { verb, id }), "ok");
  if (res.restart_required) setRestart();
  await refresh();
}

async function uninstall(id, label) {
  const ok = await confirm({
    title: t("plugins.uninstallTitle"),
    message: t("plugins.uninstallMsg", { label }),
    okLabel: t("plugins.uninstall"),
    danger: true,
  });
  if (!ok) return;
  await act(() => uninstallPlugin(id), id, t("plugins.verbUninstalled"));
}

function setRestart() {
  needsRestart = true;
  $restart.classList.remove("hidden");
  $note.textContent = t("plugins.restartRequired");
}

async function doRestart() {
  try {
    const res = await fetch("/api/restart", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast(t("toast.restarting"), "ok");
    close();
  } catch (e) {
    toast(t("toast.restartFailed", { error: e.message }), "error");
  }
}

export function openPluginsModal() {
  needsRestart = false;
  $restart.classList.add("hidden");
  $body.innerHTML = `<div class="text-sm text-slate-500 dark:text-slate-400">${escapeHtml(t("plugins.loading"))}</div>`;
  $modal.classList.remove("hidden");
  refresh();
}

export function initPluginsModal() {
  $done.addEventListener("click", close);
  $close.addEventListener("click", close);
  $restart.addEventListener("click", doRestart);
  $modal.addEventListener("click", (e) => {
    if (e.target === $modal) close();
  });
  document.addEventListener("keydown", (e) => {
    if (!$modal.classList.contains("hidden") && e.key === "Escape") {
      e.preventDefault();
      close();
    }
  });
}
