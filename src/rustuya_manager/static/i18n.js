// Minimal i18n for the manager web UI. No build step, no dependency — same
// shape as the rest of the client: a tiny ES module the others import.
//
// The catalog of languages is data-driven: the server enumerates the locale
// JSON files under static/locales/ and exposes them at GET /api/locales, so
// dropping a `xx.json` next to en.json/ko.json makes "xx" selectable with no
// code change. English is always loaded as the fallback layer, so a key only
// translated in en.json still resolves in every other language.
//
//   en.json (source of truth) + ko.json + …   — flat, namespaced keys
//   t(key, vars)                               — lookup + {placeholder} fill
//   data-i18n / data-i18n-attr / data-i18n-html — markup hooks (see applyDom)

// English is the permanent fallback; `messages` is the active locale (=== the
// fallback object when the active locale is English, so no double fetch).
let fallback = {};
let messages = {};
let current = "en";
let available = ["en"];
let defaultLang = "en";
// code → native display name (each catalog's own `lang.name`), from /api/locales
// so the picker can list "English / 한국어 / 日本語" without fetching every file.
let names = {};

// Subscribers notified after a language switch (see onLangChange) — lets a
// plugin re-render its own JS-built UI, which applyDom() can't reach.
const langSubs = new Set();

async function fetchLocale(code) {
  const res = await fetch(`/static/locales/${code}.json`, { cache: "no-store" });
  if (!res.ok) throw new Error(`locale ${code}: HTTP ${res.status}`);
  return res.json();
}

// Resolve the locale to show on first load: explicit saved choice wins, then a
// prefix match on the browser's language, else the server-declared default.
function pickInitialLang() {
  const saved = localStorage.getItem("lang");
  if (saved && available.includes(saved)) return saved;
  const nav = (navigator.language || "").slice(0, 2).toLowerCase();
  if (available.includes(nav)) return nav;
  return defaultLang;
}

// Load the locale list + the fallback (en) and active locale dictionaries. Best
// effort throughout — any failure degrades to English / raw keys rather than
// blocking the boot, since the static markup is already English.
export async function initI18n() {
  try {
    const res = await fetch("/api/locales", { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data.available) && data.available.length) {
        available = data.available;
      }
      if (data.default) defaultLang = data.default;
      if (data.names && typeof data.names === "object") names = data.names;
    }
  } catch {
    /* offline / endpoint missing — stay en-only */
  }
  try {
    fallback = await fetchLocale("en");
  } catch {
    fallback = {};
  }
  current = pickInitialLang();
  messages = current === "en" ? fallback : await fetchLocale(current).catch(() => fallback);
  // Backfill names for the two catalogs we loaded, in case the server didn't
  // supply the map (older build). The picker still labels every advertised
  // locale — any code with no known name falls back to the code itself.
  if (!names.en && fallback["lang.name"]) names.en = fallback["lang.name"];
  if (!names[current] && messages["lang.name"]) names[current] = messages["lang.name"];
  document.documentElement.lang = current;
}

// Translate `key`, filling {name} placeholders from `vars`. Lookup order:
// active locale → English fallback → the key itself (so a missing key is
// visible rather than blank).
export function t(key, vars) {
  let s = messages[key] ?? fallback[key] ?? key;
  if (vars) {
    s = s.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  }
  return s;
}

// Subscribe to language switches; returns an unsubscribe fn. The callback runs
// with the new code AFTER setLang() has swapped the active dictionary and
// re-applied the static markup, so a plugin can re-render its own JS-built UI
// (which applyDom only reaches via [data-i18n] nodes) in the new language.
export function onLangChange(cb) {
  langSubs.add(cb);
  return () => langSubs.delete(cb);
}

export function getLocales() {
  return available.slice();
}

// code → native display name. Falls back to the code for any locale whose
// catalog didn't declare a `lang.name`.
export function getLocaleName(code) {
  return names[code] || code;
}

export function getLang() {
  return current;
}

// Switch the active locale, persist the choice, and re-apply the static
// markup. Callers that own dynamic content (header menu, device cards) re-run
// their own renderers after this resolves — see applyI18n() in app.js.
export async function setLang(code) {
  if (!available.includes(code) || code === current) return;
  messages = code === "en" ? fallback : await fetchLocale(code).catch(() => fallback);
  current = code;
  localStorage.setItem("lang", code);
  document.documentElement.lang = code;
  applyDom();
  // Notify plugins so they can re-render their own JS-built UI. Isolated so one
  // bad handler can't break the switch or the other subscribers.
  for (const cb of langSubs) {
    try {
      cb(current);
    } catch (e) {
      console.error("onLangChange handler threw", e);
    }
  }
}

// Walk the markup and localize it in place:
//   [data-i18n]="key"            → textContent  (plain text; safest, default)
//   [data-i18n-html]="key"       → innerHTML    (for copy with inline <strong>/<code>;
//                                                locale JSON is a trusted first-party asset)
//   [data-i18n-attr]="a:key;b:k" → element attributes (placeholder, title, label, …)
const ATTR_PAIR_SEP = /\s*;\s*/;
export function applyDom(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.getAttribute("data-i18n"));
  }
  for (const el of root.querySelectorAll("[data-i18n-html]")) {
    el.innerHTML = t(el.getAttribute("data-i18n-html"));
  }
  for (const el of root.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of el.getAttribute("data-i18n-attr").split(ATTR_PAIR_SEP)) {
      if (!pair) continue;
      const idx = pair.indexOf(":");
      if (idx < 0) continue;
      const attr = pair.slice(0, idx).trim();
      const key = pair.slice(idx + 1).trim();
      if (attr && key) el.setAttribute(attr, t(key));
    }
  }
}
