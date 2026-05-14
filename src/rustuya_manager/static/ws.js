// WebSocket connection management. Owns the badge and the reconnect loop.
// Each incoming frame rewrites the shared snapshot and triggers a full
// re-render — there's no incremental diffing on the client.

import { state } from "./state.js";
import { render } from "./render.js";

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
    connecting: ["bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600", "connecting", true],
    live:       ["bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-300 dark:border-emerald-700", "live", false],
    lost:       ["bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 border-rose-300 dark:border-rose-700", "lost", true],
  };
  const [cls, text, pulse] = styles[label];
  $conn.className = `text-xs px-2 py-1 rounded-full border ${cls} inline-flex items-center justify-center w-[96px] whitespace-nowrap gap-1`;
  $conn.innerHTML = `<span class="${pulse ? "pulse-dot " : ""}leading-none">●</span><span>${text}</span>`;
}

let backoffMs = 500;

export function connect() {
  setConn("connecting");
  const ws = new WebSocket(wsUrl());
  ws.onopen = () => { backoffMs = 500; setConn("live"); };
  ws.onmessage = (ev) => {
    state.snapshot = JSON.parse(ev.data);
    render();
  };
  ws.onclose = () => {
    setConn("lost");
    backoffMs = Math.min(backoffMs * 2, 8000);
    setTimeout(connect, backoffMs);
  };
  ws.onerror = () => ws.close();
}
