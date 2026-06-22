// WebSocket connection management. Owns the badge and the reconnect loop.
// Each incoming frame rewrites the shared snapshot and triggers a full
// re-render — there's no incremental diffing on the client.

import { state } from "./state.js";
import { renderFromPush } from "./render.js";
import { notifyPluginState } from "./plugins.js";
import { t } from "./i18n.js";

const $conn = document.getElementById("conn-badge");

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

export function setConn(label) {
  // Short labels keep the badge compact; the longest ("connecting") fits in
  // ~80 px. The fixed width prevents header reflow on state transitions
  // and keeps the dot visually centered alongside the word — a dot-only
  // shrunk badge ended up looking off-center against the title text.
  const styles = {
    connecting: ["bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600", true],
    live:       ["bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-300 dark:border-emerald-700", false],
    lost:       ["bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 border-rose-300 dark:border-rose-700", true],
  };
  const [cls, pulse] = styles[label];
  const text = t(`conn.${label}`);
  $conn.className = `text-xs px-2 py-1 rounded-full border ${cls} inline-flex items-center justify-center w-[96px] whitespace-nowrap gap-1`;
  $conn.innerHTML = `<span class="${pulse ? "pulse-dot " : ""}leading-none">●</span><span>${text}</span>`;
}

let backoffMs = 500;

// Boot id from the last snapshot we saw. The server stamps a per-process id on
// every frame; a different id on a fresh frame means a new manager process is
// serving us (re-exec via "Restart manager", or a container restart). null
// until the first frame, so the initial connect never triggers a reload.
let lastBootId = null;

export function connect() {
  setConn("connecting");
  const ws = new WebSocket(wsUrl());
  ws.onopen = () => { backoffMs = 500; setConn("live"); };
  ws.onmessage = (ev) => {
    const snap = JSON.parse(ev.data);
    // Auto-F5 on a manager restart. The tab bar is built once at page load
    // (initPluginHost) and isn't rebuilt on a WS reconnect alone, so a restart
    // that adds or removes a plugin would otherwise need a manual refresh to
    // show up. A full reload re-runs the boot path, picking up new/removed/
    // edited plugin tabs and any changed HTML/JS — exactly what F5 would. A
    // transient reconnect keeps the same id and is left untouched.
    const bootId = snap.boot_id;
    if (bootId != null) {
      if (lastBootId != null && bootId !== lastBootId) {
        location.reload();
        return;
      }
      lastBootId = bootId;
    }
    state.snapshot = snap;
    renderFromPush();  // defer if the user is mid-gesture (drag/selection/open <select>)
    // Fan the frame out to plugin pages (no-op when no plugins are installed).
    notifyPluginState(state.snapshot);
  };
  ws.onclose = () => {
    setConn("lost");
    backoffMs = Math.min(backoffMs * 2, 8000);
    setTimeout(connect, backoffMs);
  };
  ws.onerror = () => ws.close();
}
