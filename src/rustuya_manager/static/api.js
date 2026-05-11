// Thin wrappers around the manager's REST endpoints. Two flavors of
// command publish:
//   - postCommand   : silent, returns {ok, error}. Used by batch loops that
//                     report status per-row inside the sync modal.
//   - publishCommand: toasts on completion. Used by the per-card buttons.

import { toast } from "./dom.js";

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
    toast(`${body.action} → ${body.id || "bridge"} sent`, "ok");
  } else {
    toast(`error: ${result.error}`, "error");
  }
  return result;
}

export async function sync(action, dev) {
  await publishCommand(buildCommandBody(action, dev));
}

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
      toast(`upload failed: ${await res.text()}`, "error");
      return;
    }
    const body = await res.json();
    let msg = `loaded ${body.count} devices`;
    if (body.persisted_to) msg += ` — saved to ${body.persisted_to}`;
    toast(msg, "ok");
  } catch (e) {
    toast(`upload error: ${e.message}`, "error");
  }
}
