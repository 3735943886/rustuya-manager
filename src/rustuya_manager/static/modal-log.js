// The Log menu: a session-scoped history of recent toasts. Every toast in the
// app funnels through dom.js toast() — built-in code and plugins alike (plugins
// get it as ctx.toast) — which records into a ring buffer. This modal lists the
// last N, newest first, and updates live while open.

import { escapeHtml, formatAgo, getToastLog, clearToastLog, onToastLog } from "./dom.js";
import { t } from "./i18n.js";

const $modal = document.getElementById("log-modal");
const $body = document.getElementById("log-modal-body");
const $close = document.getElementById("log-modal-close");
const $done = document.getElementById("log-modal-done");
const $clear = document.getElementById("log-modal-clear");

// Unsubscribe handle for the live toast-log subscription, held only while open.
let unsub = null;

// Dot color by toast kind, mirroring the toast styles.
const KIND_DOT = {
  ok: "bg-emerald-500",
  error: "bg-rose-500",
  warning: "bg-amber-500",
};

function renderRow(e) {
  const row = document.createElement("div");
  row.className = "flex items-start gap-2 text-sm";
  const dot = KIND_DOT[e.kind] || KIND_DOT.ok;
  // Consecutive repeats collapse into one row with a ×N badge (see dom.js).
  const count =
    e.count > 1
      ? `<span class="shrink-0 text-xs font-medium text-slate-500 dark:text-slate-400">×${e.count}</span>`
      : "";
  row.innerHTML =
    `<span class="mt-1.5 w-2 h-2 rounded-full shrink-0 ${dot}"></span>` +
    `<span class="flex-1 min-w-0 break-words text-slate-700 dark:text-slate-200">${escapeHtml(e.msg)}</span>` +
    count +
    // `at` is epoch ms; formatAgo wants seconds.
    `<span class="shrink-0 text-xs text-slate-400 dark:text-slate-500">${escapeHtml(formatAgo(e.at / 1000))}</span>`;
  return row;
}

function render() {
  const entries = getToastLog().reverse(); // newest first
  if (entries.length === 0) {
    $body.innerHTML = `<div class="text-sm text-slate-500 dark:text-slate-400">${escapeHtml(t("log.empty"))}</div>`;
    return;
  }
  $body.replaceChildren(...entries.map(renderRow));
}

function close() {
  $modal.classList.add("hidden");
  if (unsub) {
    unsub();
    unsub = null;
  }
}

export function openLogModal() {
  render();
  // Live-update while open (a new toast or a clear re-renders the list).
  unsub = onToastLog(render);
  $modal.classList.remove("hidden");
}

export function initLogModal() {
  $done.addEventListener("click", close);
  $close.addEventListener("click", close);
  // clearToastLog notifies subscribers, so the open list re-renders to empty.
  $clear.addEventListener("click", clearToastLog);
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
