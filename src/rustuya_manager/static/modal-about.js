// The "About" menu item: manager/bridge versions, resolved MQTT topics, and
// any unmet plugin requirements. The content itself is still built by
// render.js's renderTemplates() into #templates-block on every WS frame
// (unchanged since before this lived in an in-page collapsible) — this
// module only owns the modal's open/close chrome, mirroring modal-log.js.
//
// Opening also re-renders directly rather than relying on the next WS frame:
// render()'s per-frame call skips renderTemplates() while a plugin tab is
// active (state.currentPage !== "devices"), and "About" is a global item
// reachable from any tab, so without this the content could be stale from
// whichever tab was last on Devices.

import { state } from "./state.js";
import { renderTemplates } from "./render.js";

const $modal = document.getElementById("about-modal");
const $close = document.getElementById("about-modal-close");
const $done = document.getElementById("about-modal-done");

function close() {
  $modal.classList.add("hidden");
}

export function openAboutModal() {
  if (state.snapshot) renderTemplates();
  $modal.classList.remove("hidden");
}

export function initAboutModal() {
  $close.addEventListener("click", close);
  $done.addEventListener("click", close);
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
