// Pure DOM/format helpers. No app state, no fetches — just transforms and
// reusable element factories. Anything stateless that two or more modules
// need lives here.

import { t } from "./i18n.js";

export const ICON_BASE = "w-5 h-5 inline-flex items-center justify-center";

export function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function formatDpsValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function formatAgo(ts) {
  const sec = Math.max(0, Date.now() / 1000 - ts);
  if (sec < 1)     return t("time.justNow");
  if (sec < 60)    return t("time.secondsAgo", { n: Math.floor(sec) });
  if (sec < 3600)  return t("time.minutesAgo", { n: Math.floor(sec / 60) });
  if (sec < 86400) return t("time.hoursAgo", { n: Math.floor(sec / 3600) });
  return t("time.daysAgo", { n: Math.floor(sec / 86400) });
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
    wrap.title = t("status.noStatus");
    wrap.appendChild(dot);
    return wrap;
  }
  const map = {
    online:  ["bg-emerald-500", t("status.online"), false],
    offline: ["border-2 border-slate-400 dark:border-slate-500", t("status.offline"), true],
    unknown: ["border-2 border-slate-300 dark:border-slate-600", t("status.unknown"), true],
  };
  const [cls, label, ring] = map[live.state] || ["bg-rose-500", String(live.state), false];
  dot.className = `w-2 h-2 rounded-full ${cls}${ring ? " bg-transparent" : ""}`;
  const code = live.code != null ? ` (code ${live.code})` : "";
  const msg = live.message ? `: ${live.message}` : "";
  wrap.title = `${label}${code}${msg}`;
  wrap.appendChild(dot);
  return wrap;
}

// Same slot as liveDot but tuned for missing-class cards, where MQTT live
// status is meaningless (the bridge doesn't know the device) but LAN-scan
// visibility carries the equivalent "is it reachable right now?" signal.
// Filled sky dot when the latest scan saw the device, dim ring otherwise.
// The class-color (sky) mirrors the missing edge stripe / wash, so the dot
// reads as part of the same category cue rather than a new color to decode.
export function scanDot(sighting) {
  const wrap = document.createElement("span");
  wrap.className = ICON_BASE;
  const dot = document.createElement("span");
  if (!sighting) {
    dot.className = "w-2 h-2 rounded-full border-2 border-slate-300 dark:border-slate-600";
    // Honest about ambiguity — without a backend "scan ever ran" flag we
    // can't tell "no scan yet" apart from "scan ran, didn't see it".
    wrap.title = t("scan.notSeen");
    wrap.appendChild(dot);
    return wrap;
  }
  dot.className = "w-2 h-2 rounded-full bg-sky-500";
  const ip = sighting.ip ? t("scan.atIp", { ip: sighting.ip }) : "";
  const ago = sighting.observed_at ? `, ${formatAgo(sighting.observed_at)}` : "";
  wrap.title = `${t("scan.sawDevice")}${ip}${ago}`;
  wrap.appendChild(dot);
  return wrap;
}

export function iconButton(glyph, onClick, title, variant = "default") {
  // `danger` tints the icon rose so destructive actions read as risky at a
  // glance; the confirm dialog is still the actual guard.
  const styles = {
    default:       "border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 text-slate-500 dark:text-slate-300",
    danger:        "border-rose-200 dark:border-rose-800 bg-white dark:bg-slate-700 hover:bg-rose-50 dark:hover:bg-rose-900/40 text-rose-600 dark:text-rose-400",
    // `danger-fill` is for the one-action-only orphan case: filled tile that
    // reads as THE primary action against the rose-tinted card background.
    "danger-fill": "border-rose-300 dark:border-rose-700 bg-rose-100 dark:bg-rose-900/70 hover:bg-rose-200 dark:hover:bg-rose-800 text-rose-700 dark:text-rose-200",
  }[variant];
  const b = document.createElement("button");
  b.type = "button";
  b.className = `${ICON_BASE} rounded border text-xs ${styles}`;
  b.textContent = glyph;
  b.title = title;
  b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return b;
}

export function button(label, onClick, variant = "default") {
  // Variants match the sync-class palette: sky = missing, amber = mismatch,
  // danger (rose) = destructive (orphan / remove). The per-card action
  // button gets the same hue as the device's edge stripe so the action and
  // the state read as one signal.
  const styles = {
    default: "border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200",
    sky:     "border-sky-300 dark:border-sky-700 bg-white dark:bg-slate-700 hover:bg-sky-50 dark:hover:bg-sky-900/40 text-sky-700 dark:text-sky-300",
    amber:   "border-amber-300 dark:border-amber-700 bg-white dark:bg-slate-700 hover:bg-amber-50 dark:hover:bg-amber-900/40 text-amber-700 dark:text-amber-300",
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
  // color strip + liveDot for the same information without labels.
  const map = {
    synced:    ["bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-700", t("pill.synced")],
    mismatch:  ["bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-700", t("pill.mismatch")],
    missing:   ["bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-700", t("pill.missing")],
    orphan:    ["bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-300 border-rose-200 dark:border-rose-700", t("pill.orphan")],
    ungrouped: ["bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-600", t("pill.ungrouped")],
  };
  const [s, label] = map[cls];
  return `<span class="text-[10px] px-1.5 py-0.5 rounded-full border ${s} uppercase tracking-wide">${label}</span>`;
}

const $toasts = document.getElementById("toast-container");

// Ring buffer of recent toasts for the Log menu. Every toast in the app —
// built-in or plugin — funnels through toast() below (plugins get it as
// ctx.toast), so this is the single capture point. In-memory, session-scoped,
// newest last.
const MAX_TOAST_LOG = 50;
const toastLog = [];
const toastListeners = new Set();

// A snapshot of the recorded toasts (oldest→newest). Callers that want
// newest-first reverse it themselves.
export function getToastLog() {
  return toastLog.slice();
}

// Drop all recorded toasts (the Log menu's "clear"). Notifies subscribers so an
// open log view empties live.
export function clearToastLog() {
  toastLog.length = 0;
  for (const fn of toastListeners) fn();
}

// Subscribe to toast-log changes (a new toast or a clear); returns an
// unsubscribe fn. The Log modal uses this to update while open.
export function onToastLog(fn) {
  toastListeners.add(fn);
  return () => toastListeners.delete(fn);
}

export function toast(msg, kind = "ok") {
  const styles = {
    ok:      "bg-slate-900 dark:bg-slate-200 text-white dark:text-slate-900",
    error:   "bg-rose-600 dark:bg-rose-500 text-white",
    warning: "bg-amber-600 dark:bg-amber-500 text-white dark:text-slate-900",
  }[kind];
  const el = document.createElement("div");
  el.className = `pointer-events-auto text-xs px-3 py-2 rounded shadow ${styles}`;
  el.textContent = msg;
  $toasts.appendChild(el);
  setTimeout(() => el.remove(), 3000);
  // Record for the Log menu, then notify any open log view.
  toastLog.push({ msg: String(msg), kind, at: Date.now() });
  if (toastLog.length > MAX_TOAST_LOG) toastLog.shift();
  for (const fn of toastListeners) fn();
}
