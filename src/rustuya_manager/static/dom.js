// Pure DOM/format helpers. No app state, no fetches — just transforms and
// reusable element factories. Anything stateless that two or more modules
// need lives here.

export const ICON_BASE = "w-5 h-5 inline-flex items-center justify-center";

export function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function shorten(s, len = 12) {
  if (!s) return "";
  if (s.length <= len) return s;
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}

export function formatDpsValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function formatAgo(ts) {
  const sec = Math.max(0, Date.now() / 1000 - ts);
  if (sec < 1)     return "just now";
  if (sec < 60)    return `${Math.floor(sec)}s ago`;
  if (sec < 3600)  return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

// Stateless icon factories. Each returns an element that is sized to h-5 so
// it sits cleanly inside the right-cluster of a device card header.

export function liveDot(live) {
  // CSS-drawn dot, not the ● glyph. Font glyphs ride above the baseline,
  // making them look bottom-anchored next to other h-5 icons; a CSS circle
  // sits in the exact geometric center of the inline-flex container.
  const wrap = document.createElement("span");
  wrap.className = ICON_BASE;
  const dot = document.createElement("span");
  if (!live) {
    dot.className = "w-2 h-2 rounded-full border-2 border-slate-300 dark:border-slate-600";
    wrap.title = "no status received";
    wrap.appendChild(dot);
    return wrap;
  }
  const map = {
    online:  ["bg-emerald-500", "online", false],
    offline: ["border-2 border-slate-400 dark:border-slate-500", "offline", true],
    unknown: ["border-2 border-slate-300 dark:border-slate-600", "unknown", true],
  };
  const [cls, label, ring] = map[live.state] || ["bg-rose-500", String(live.state), false];
  dot.className = `w-2 h-2 rounded-full ${cls}${ring ? " bg-transparent" : ""}`;
  const code = live.code != null ? ` (code ${live.code})` : "";
  const msg = live.message ? `: ${live.message}` : "";
  wrap.title = `${label}${code}${msg}`;
  wrap.appendChild(dot);
  return wrap;
}

export function typeBadge(t) {
  const span = document.createElement("span");
  span.className =
    `${ICON_BASE} text-[10px] font-mono rounded border border-slate-200 dark:border-slate-600 text-slate-500 dark:text-slate-400`;
  if (t === "SubDevice") {
    span.textContent = "S";
    span.title = "Sub-device";
  } else {
    span.textContent = "W";
    span.title = "WiFi device";
  }
  return span;
}

export function iconButton(glyph, onClick, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.className =
    `${ICON_BASE} rounded border border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 text-slate-500 dark:text-slate-300 text-xs`;
  b.textContent = glyph;
  b.title = title;
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

export function button(label, onClick, variant = "default") {
  const styles = {
    default: "border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200",
    danger:  "border-rose-300 dark:border-rose-700 bg-white dark:bg-slate-700 hover:bg-rose-50 dark:hover:bg-rose-900/40 text-rose-700 dark:text-rose-300",
  }[variant];
  const b = document.createElement("button");
  b.type = "button";
  // h-5 matches the icons/dots so everything in the right cluster aligns.
  b.className = `h-5 px-2 inline-flex items-center rounded border text-[11px] ${styles}`;
  b.textContent = label;
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

export function statusPill(cls) {
  // Used by the synthetic missing-parent card; device cards use the left-edge
  // color strip + liveDot/typeBadge for the same information without labels.
  const map = {
    synced:    ["bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-700", "synced"],
    mismatch:  ["bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-700", "mismatch"],
    missing:   ["bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-700", "missing"],
    orphan:    ["bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 border-rose-200 dark:border-rose-700", "orphan"],
    ungrouped: ["bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-600", "ungrouped"],
  };
  const [s, label] = map[cls];
  return `<span class="text-[10px] px-1.5 py-0.5 rounded-full border ${s} uppercase tracking-wide">${label}</span>`;
}

const $toasts = document.getElementById("toast-container");
export function toast(msg, kind = "ok") {
  const styles = {
    ok:    "bg-slate-900 dark:bg-slate-200 text-white dark:text-slate-900",
    error: "bg-rose-600 dark:bg-rose-500 text-white",
  }[kind];
  const t = document.createElement("div");
  t.className = `pointer-events-auto text-xs px-3 py-2 rounded shadow ${styles}`;
  t.textContent = msg;
  $toasts.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}
