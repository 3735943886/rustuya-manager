// Thin wrappers around the manager's REST endpoints. Two flavors of
// command publish:
//   - postCommand   : silent, returns {ok, error}. Used by batch loops that
//                     report status per-row inside the sync modal.
//   - publishCommand: toasts on completion. Used by the per-card buttons.

import { toast } from "./dom.js";
import { t } from "./i18n.js";

export function buildCommandBody(action, dev) {
  const body = { action, id: dev.id, name: dev.name };
  if (action === "add") {
    if (dev.type === "WiFi") {
      if (dev.key && dev.key !== "Auto") body.key = dev.key;
      if (dev.ip && dev.ip !== "Auto") body.ip = dev.ip;
      if (dev.version && dev.version !== "Auto") body.version = dev.version;
    } else {
      if (dev.cid) body.cid = dev.cid;
      if (dev.parent_id) body.parent_id = dev.parent_id;
    }
  }
  return body;
}

export async function postCommand(body) {
  try {
    const res = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) return { ok: false, error: await res.text() };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

export async function publishCommand(body) {
  const result = await postCommand(body);
  if (result.ok) {
    toast(t("toast.commandSent", { action: body.action, id: body.id || "bridge" }), "ok");
  } else {
    toast(t("toast.commandError", { error: result.error }), "error");
  }
  return result;
}

export async function sync(action, dev) {
  await publishCommand(buildCommandBody(action, dev));
}

export async function postScan() {
  // Trigger the shared LanScanCoordinator on the server. The actual
  // sighting list rides the next WS snapshot (state.scan_results), so
  // this returns just `{ok, count}` to drive the header button's
  // toast. 503 indicates the bridge isn't connected.
  try {
    const res = await fetch("/api/scan", { method: "POST" });
    if (!res.ok) return { ok: false, error: await res.text() };
    return await res.json();
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ── Plugin catalog / management ────────────────────────────────────────────
// All return parsed JSON on success, or {ok:false, error} on failure, so the
// management modal can render the error inline rather than throwing.
async function _postJson(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) return { ok: false, error: await res.text() };
    return await res.json();
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

export async function getCatalog() {
  try {
    const res = await fetch("/api/plugins/catalog");
    if (!res.ok) return { ok: false, error: await res.text() };
    return await res.json();
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

export const installPlugin = (id) => _postJson("/api/plugins/install", { id });
export const updatePlugin = (id) => _postJson("/api/plugins/update", { id });
export const uninstallPlugin = (id) => _postJson("/api/plugins/uninstall", { id });
export const togglePlugin = (id, enabled) => _postJson("/api/plugins/toggle", { id, enabled });

export async function uploadCloud(file) {
  try {
    const text = await file.text();
    JSON.parse(text); // sanity-check on the client side too
    const res = await fetch("/api/cloud", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: text,
    });
    if (!res.ok) {
      toast(t("toast.uploadFailed", { error: await res.text() }), "error");
      return;
    }
    const body = await res.json();
    let msg = t("toast.cloudLoaded", { count: body.count });
    if (body.persisted_to) msg += t("toast.cloudSaved", { path: body.persisted_to });
    toast(msg, "ok");
  } catch (e) {
    toast(t("toast.uploadError", { error: e.message }), "error");
  }
}
