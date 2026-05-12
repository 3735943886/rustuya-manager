// Generic confirm dialog. Promise-based so callers can `await confirm({...})`
// rather than wiring callbacks. Reused for any destructive action that
// warrants a beat of friction (currently: device remove).

import { escapeHtml } from "./dom.js";

const $modal = document.getElementById("confirm-modal");
const $title = document.getElementById("confirm-modal-title");
const $body = document.getElementById("confirm-modal-body");
const $ok = document.getElementById("confirm-modal-ok");
const $cancel = document.getElementById("confirm-modal-cancel");
const $close = document.getElementById("confirm-modal-close");

let activeResolver = null;

function close(result) {
  $modal.classList.add("hidden");
  if (activeResolver) {
    const r = activeResolver;
    activeResolver = null;
    r(result);
  }
}

export function confirm({ title = "Confirm", message = "", okLabel = "OK", danger = false } = {}) {
  // Reject the previous outstanding prompt if any (the new one supersedes it).
  if (activeResolver) {
    const prev = activeResolver;
    activeResolver = null;
    prev(false);
  }
  $title.textContent = title;
  $body.innerHTML = escapeHtml(message).replace(/\n/g, "<br>");
  $ok.textContent = okLabel;
  $ok.className = danger
    ? "text-sm px-4 py-1.5 rounded bg-rose-600 hover:bg-rose-700 text-white"
    : "text-sm px-4 py-1.5 rounded bg-slate-900 hover:bg-slate-800 dark:bg-slate-200 dark:text-slate-900 dark:hover:bg-white text-white";
  $modal.classList.remove("hidden");
  $ok.focus();
  return new Promise((resolve) => { activeResolver = resolve; });
}

export function initConfirmModal() {
  $ok.addEventListener("click", () => close(true));
  $cancel.addEventListener("click", () => close(false));
  $close.addEventListener("click", () => close(false));
  $modal.addEventListener("click", (e) => {
    if (e.target === $modal) close(false);
  });
  document.addEventListener("keydown", (e) => {
    if ($modal.classList.contains("hidden")) return;
    if (e.key === "Escape") { e.preventDefault(); close(false); }
    else if (e.key === "Enter") { e.preventDefault(); close(true); }
  });
}
