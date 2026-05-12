// Add / Edit device modal. The two modes share a single form — Edit pre-fills
// from the device's current snapshot data and locks the type radio (changing
// WiFi ↔ SubDevice mid-edit is dangerous because the bridge keys off id+type).
// Both modes publish `add`, which the bridge upserts.

import { state } from "./state.js";
import { publishCommand } from "./api.js";
import { toast } from "./dom.js";

const $modal = document.getElementById("device-modal");
const $title = document.getElementById("device-modal-title");
const $close = document.getElementById("device-modal-close");
const $cancel = document.getElementById("device-modal-cancel");
const $submit = document.getElementById("device-modal-submit");

const $fId      = document.getElementById("device-form-id");
const $fName    = document.getElementById("device-form-name");
const $fTypeWifi = document.getElementById("device-form-type-wifi");
const $fTypeSub  = document.getElementById("device-form-type-sub");
const $blockWifi = document.getElementById("device-form-block-wifi");
const $blockSub  = document.getElementById("device-form-block-sub");
const $fIp      = document.getElementById("device-form-ip");
const $fKey     = document.getElementById("device-form-key");
const $fVersion = document.getElementById("device-form-version");
const $fCid     = document.getElementById("device-form-cid");
const $fParent  = document.getElementById("device-form-parent");

let editingId = null;  // null in add mode

function showBlocks() {
  const isWifi = $fTypeWifi.checked;
  $blockWifi.classList.toggle("hidden", !isWifi);
  $blockSub.classList.toggle("hidden", isWifi);
}

function setLocked(locked) {
  // Lock the type radio + id field in edit mode. Type swap mid-edit could
  // break the bridge id↔type relationship; id swap would be a different
  // device entirely (use remove+add instead).
  $fTypeWifi.disabled = locked;
  $fTypeSub.disabled = locked;
  $fId.readOnly = locked;
  $fId.classList.toggle("opacity-60", locked);
}

function clearForm() {
  $fId.value = "";
  $fName.value = "";
  $fTypeWifi.checked = true;
  $fTypeSub.checked = false;
  $fIp.value = "";
  $fKey.value = "";
  $fVersion.value = "";
  $fCid.value = "";
  $fParent.value = "";
  showBlocks();
}

function fillFromDevice(dev) {
  $fId.value = dev.id || "";
  $fName.value = dev.name && dev.name !== "N/A" ? dev.name : "";
  if (dev.type === "SubDevice") {
    $fTypeSub.checked = true;
    $fTypeWifi.checked = false;
    $fCid.value = dev.cid || "";
    $fParent.value = dev.parent_id || "";
  } else {
    $fTypeWifi.checked = true;
    $fTypeSub.checked = false;
    $fIp.value = dev.ip && dev.ip !== "Auto" ? dev.ip : "";
    $fKey.value = dev.key || "";
    $fVersion.value = dev.version && dev.version !== "Auto" ? dev.version : "";
  }
  showBlocks();
}

export function openAddModal() {
  editingId = null;
  $title.textContent = "Add device";
  $submit.textContent = "Add";
  clearForm();
  setLocked(false);
  $modal.classList.remove("hidden");
  $fId.focus();
}

export function openEditModal(id) {
  const snap = state.snapshot;
  if (!snap) return;
  const dev = snap.bridge[id] || snap.cloud[id];
  if (!dev) {
    toast(`device ${id} not found`, "error");
    return;
  }
  editingId = id;
  $title.textContent = "Edit device";
  $submit.textContent = "Save";
  fillFromDevice(dev);
  setLocked(true);
  $modal.classList.remove("hidden");
  $fName.focus();
}

function closeModal() {
  $modal.classList.add("hidden");
  editingId = null;
}

// Auto-fill id from `{ip|cid}_{name}` when blank, mirroring the previous
// manager's behavior so quick-add doesn't need typing the id explicitly.
function autoId() {
  const name = $fName.value.trim();
  const base = $fTypeWifi.checked ? $fIp.value.trim() : $fCid.value.trim();
  if (!base) return "";
  return name ? `${base}_${name}` : base;
}

async function submitForm() {
  const isWifi = $fTypeWifi.checked;
  const id = $fId.value.trim() || autoId();
  if (!id) {
    toast("device id is required (or fill IP/CID for auto-id)", "error");
    return;
  }
  const name = $fName.value.trim();
  const body = { action: "add", id };
  if (name) body.name = name;
  if (isWifi) {
    const ip = $fIp.value.trim();
    const key = $fKey.value.trim();
    const version = $fVersion.value.trim();
    if (ip) body.ip = ip;
    if (key) body.key = key;
    if (version) body.version = version;
  } else {
    const cid = $fCid.value.trim();
    const parent = $fParent.value.trim();
    if (!cid) {
      toast("sub-device needs CID", "error");
      return;
    }
    body.cid = cid;
    if (parent) body.parent_id = parent;
  }
  $submit.disabled = true;
  try {
    const result = await publishCommand(body);
    if (result.ok) closeModal();
  } finally {
    $submit.disabled = false;
  }
}

export function initDeviceModal() {
  $fTypeWifi.addEventListener("change", showBlocks);
  $fTypeSub.addEventListener("change", showBlocks);
  $close.addEventListener("click", closeModal);
  $cancel.addEventListener("click", closeModal);
  $submit.addEventListener("click", submitForm);
  $modal.addEventListener("click", (e) => {
    if (e.target === $modal) closeModal();
  });
  // Submit on Enter from any text field; Esc closes.
  $modal.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeModal(); }
    else if (e.key === "Enter" && e.target.tagName === "INPUT") {
      e.preventDefault();
      submitForm();
    }
  });
}
