// Tuya Cloud login flow. State machine mirrors the backend's WizardState
// enum. The backend response on any wizard endpoint is
//   { state, qr_image_data_url, message, error, devices_count, ... }
// We render the matching pane and poll while the flow is in progress.
// Cached user_code lives in localStorage so repeat runs are one click.

const $wizardOpen = document.getElementById("wizard-open-btn");
const $wizardModal = document.getElementById("wizard-modal");
const $wizardClose = document.getElementById("wizard-close");
const $wizardCancel = document.getElementById("wizard-cancel");
const $wizardStart = document.getElementById("wizard-start");
const $wizardUserCode = document.getElementById("wizard-user-code");
const $wizardBody = document.getElementById("wizard-body");
const $wizardQrImage = document.getElementById("wizard-qr-image");
const $wizardWorkingMsg = document.getElementById("wizard-working-message");
const $wizardDoneMsg = document.getElementById("wizard-done-message");
const $wizardErrorMsg = document.getElementById("wizard-error-message");
const $wizardHeaderBtn = document.getElementById("wizard-header-btn");

let wizardPollTimer = null;

async function openWizardModal() {
  showWizardPane("idle");
  $wizardModal.classList.remove("hidden");
  $wizardStart.disabled = false;
  $wizardStart.textContent = "Start";
  // Prefill priority: server's tuyacreds.json (cross-browser) > localStorage
  // (this-browser fallback). The server read is best-effort and shouldn't
  // block the modal — open first, populate when the response lands.
  $wizardUserCode.value = localStorage.getItem("tuyaUserCode") || "";
  $wizardUserCode.focus();
  try {
    const res = await fetch("/api/wizard/info");
    if (res.ok) {
      const { saved_user_code } = await res.json();
      if (saved_user_code && !$wizardUserCode.value) {
        $wizardUserCode.value = saved_user_code;
      }
    }
  } catch (e) { /* offline / endpoint missing — fall back to localStorage value */ }
}

function closeWizardModal() {
  stopWizardPoll();
  $wizardModal.classList.add("hidden");
}

function showWizardPane(name) {
  for (const pane of $wizardBody.querySelectorAll("[data-wizard-pane]")) {
    pane.classList.toggle("hidden", pane.dataset.wizardPane !== name);
  }
}

function applyWizardSession(s) {
  switch (s.state) {
    case "idle":
      showWizardPane("idle");
      $wizardStart.disabled = false;
      $wizardStart.textContent = "Start";
      break;
    case "requesting_qr":
      showWizardPane("requesting_qr");
      $wizardStart.disabled = true;
      break;
    case "awaiting_scan":
      showWizardPane("awaiting_scan");
      if (s.qr_image_data_url) $wizardQrImage.src = s.qr_image_data_url;
      $wizardStart.disabled = true;
      break;
    case "logged_in":
    case "fetching":
      showWizardPane("working");
      $wizardWorkingMsg.textContent = s.message || "Working…";
      $wizardStart.disabled = true;
      break;
    case "done":
      showWizardPane("done");
      $wizardDoneMsg.textContent = s.message || `Loaded ${s.devices_count} devices`;
      $wizardStart.textContent = "Close";
      $wizardStart.disabled = false;
      stopWizardPoll();
      // Auto-close after a brief moment so the user sees the success state
      setTimeout(() => {
        if (wizardCurrentState() === "done") closeWizardModal();
      }, 2500);
      break;
    case "error":
      showWizardPane("error");
      $wizardErrorMsg.textContent = s.error || s.message || "Unknown error";
      $wizardStart.disabled = false;
      $wizardStart.textContent = "Try again";
      stopWizardPoll();
      break;
  }
}

function wizardCurrentState() {
  for (const pane of $wizardBody.querySelectorAll("[data-wizard-pane]")) {
    if (!pane.classList.contains("hidden")) return pane.dataset.wizardPane;
  }
  return null;
}

async function startWizard() {
  const userCode = $wizardUserCode.value.trim();
  if (userCode) localStorage.setItem("tuyaUserCode", userCode);

  // If we're sitting on the "done" or "error" pane, restart cleanly
  const cur = wizardCurrentState();
  if (cur === "done") { closeWizardModal(); return; }
  if (cur === "idle" || cur === "error") {
    try {
      const res = await fetch("/api/wizard/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_code: userCode }),
      });
      const session = await res.json();
      applyWizardSession(session);
      startWizardPoll();
    } catch (e) {
      $wizardErrorMsg.textContent = `network error: ${e.message}`;
      showWizardPane("error");
    }
  }
}

function startWizardPoll() {
  stopWizardPoll();
  wizardPollTimer = setInterval(async () => {
    try {
      const res = await fetch("/api/wizard/status");
      const session = await res.json();
      applyWizardSession(session);
    } catch (e) {
      // Transient network errors are fine; keep polling.
    }
  }, 1500);
}

function stopWizardPoll() {
  if (wizardPollTimer) {
    clearInterval(wizardPollTimer);
    wizardPollTimer = null;
  }
}

async function cancelWizard() {
  stopWizardPoll();
  try {
    await fetch("/api/wizard/cancel", { method: "POST" });
  } catch (e) { /* ignore */ }
  closeWizardModal();
}

export function initWizardModal() {
  $wizardOpen?.addEventListener("click", openWizardModal);
  // The header has its own permanent entry point — same modal, same handler.
  // Lets users re-fetch from Tuya cloud after the initial run (e.g. after
  // adding a new device on the phone) without deleting tuyadevices.json first.
  $wizardHeaderBtn?.addEventListener("click", openWizardModal);
  $wizardClose?.addEventListener("click", cancelWizard);
  $wizardCancel?.addEventListener("click", cancelWizard);
  $wizardStart?.addEventListener("click", startWizard);
  $wizardModal?.addEventListener("click", (e) => {
    if (e.target === $wizardModal) cancelWizard();
  });
  $wizardUserCode?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") startWizard();
  });
  // ESC mirrors the X / Cancel buttons so the wizard's dismissal story
  // matches every other modal in the app (confirm, device, sync).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$wizardModal.classList.contains("hidden")) {
      cancelWizard();
    }
  });
}
