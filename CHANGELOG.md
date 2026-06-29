# Changelog

All notable changes to this project are documented in this file, curated by
hand. This file is the single source of truth: the GitHub Release notes for each
tag are the matching `## [version]` section extracted from here by the release
workflow.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project versions are [PEP 440](https://peps.python.org/pep-0440/)
pre-releases (`0.1.0rcN`) until the `0.1.0` final. rc tags publish to TestPyPI;
the plain `0.1.0` tag will publish to PyPI.

## [Unreleased]

## [0.1.0rc72] — 2026-06-29

### Fixed

- **Reported bridge version froze at the value seen when the manager booted.**
  The Info panel reads the running bridge version from the retained
  `{root}/bridge/config` payload (`state.bridge_config_raw`), populated in
  `_on_bridge_config`. That store sat *after* the template-idempotence guard,
  which short-circuits whenever the topic/payload templates are unchanged. An
  in-place bridge upgrade (e.g. `bridgectl` + a systemd restart) republishes the
  config with a new `version` but identical templates, so the guard returned
  early and the raw config was never refreshed — the panel showed the boot-time
  version forever, and the Info "refresh" button (which only re-checks PyPI for
  the *latest* version) couldn't fix it. The raw config is now stored on every
  valid delivery, ahead of the guard; the setter self-guards on change and wakes
  WS listeners only then, so the version updates live without redundant
  broadcasts on identical retained re-deliveries.

### Dependencies

- Bumped `pyrustuyabridge` `>=0.3.0rc27` → `>=0.3.0rc28`. rc28 adds a
  `set_config` MQTT command and makes the six topic/template/retain settings
  config-file/`set_config` only (dropping their CLI flags + env vars on the
  bridge side). The manager is unaffected: the embedded bridge is configured via
  broker/root/state-file/log-level/creds kwargs (+ optional `config_path`), never
  those six as flags or env, and it reads templates from the retained
  `bridge/config` topic regardless.

## [0.1.0rc71] — 2026-06-29

### Dependencies

- Bumped `pyrustuyabridge` `>=0.3.0rc26` → `>=0.3.0rc27`. rc27 bumps the core
  `rustuya` crate rc7→rc9 (bridge-internal, transparent through the binding):
  device connect now accepts IPv6 literals, link-local zone ids, and
  NAT64-synthesized addresses, and the crypto stack moves to `aes-gcm` 0.11.0
  final. No manager-facing API change.

## [0.1.0rc70] — 2026-06-25

### Fixed

- Clear the wizard QR code on scan-done so it no longer lingers on the
  token-expiry path.

## [0.1.0rc69] — 2026-06-24

### Added

- Toast when a LAN scan starts so the ~20s wait isn't silent.

## [0.1.0rc68] — 2026-06-24

### Changed

- Renamed the "Log" menu to "Notifications", added a cloud-fetch toast, and
  unified the toast style across the UI.

## [0.1.0rc67] — 2026-06-24

### Changed

- Removed the now-redundant "Load new plugins" menu item.

## [0.1.0rc66] — 2026-06-24

### Changed

- Dedupe consecutive identical toasts; clearer "Refresh devices" label.

## [0.1.0rc65] — 2026-06-24

### Changed

- Toasts for the Log menu and the version-check button; i18n coverage and modal
  fixes.

## [0.1.0rc64] — 2026-06-24

### Added

- Info panel split into sections with a "latest" chip; manager and bridge
  version schemes unified for display.

## [0.1.0rc63] — 2026-06-23

### Dependencies

- Pinned `pyrustuyabridge` `>=0.3.0rc26`.

## [0.1.0rc62] — 2026-06-23

### Added

- Renamed "Bridge info" → "Info" and added an online update check (compares the
  running manager/bridge against the latest on PyPI/TestPyPI).

## [0.1.0rc61] — 2026-06-22

### Fixed

- Defer a push-driven re-render while the user is mid-gesture so the UI doesn't
  reset under their finger.

## [0.1.0rc60] — 2026-06-22

### Added

- Attention cue on header menu items and the hamburger.

## [0.1.0rc59] — 2026-06-21

### Added

- `ctx.data_dir(name)` — a CWD-independent persistent data directory for plugins.

## [0.1.0rc58] — 2026-06-21

### Added

- Plugin runtime: DP snapshot pull (`ctx.current_dps`) and a
  retained-vs-device watcher origin.

### Fixed

- Dismiss the hamburger via a capture-phase outside-click handler so a panel
  re-render can't immediately self-close it.
- Closed an XSS-through-DOM path in the i18n layer (CodeQL) and aligned the
  catalog-error test with the autofix.

## [0.1.0rc56] — 2026-06-20

### Added

- Live "Check for updates" — refresh the plugin catalog from the web.

## [0.1.0rc55] — 2026-06-20

### Changed

- Bumped the Home Assistant Discovery plugin catalog entry to 0.0.1rc8.

## [0.1.0rc54] — 2026-06-20

### Changed

- Bumped the Home Assistant Discovery plugin catalog entry to 0.0.1rc7.

## [0.1.0rc53] — 2026-06-20

### Changed

- `ruff format` compliance for the plugin runtime.

## [0.1.0rc52] — 2026-06-20

### Added

- **Reactive plugin runtime.** An in-process DP bus lets plugins `watch` device
  DPs, publish `derived` DPs, and `set` values; plus in-process service
  supervision (`ctx.add_service`) so a plugin can run a long-lived background
  task under the manager.

## [0.1.0rc51] — 2026-06-20

### Added

- Made the language picker global; bumped the HA Discovery catalog entry to
  0.0.1rc6.

## [0.1.0rc50] — 2026-06-20

### Added

- Language picker as a manager-scoped collapsible submenu.

## [0.1.0rc49] — 2026-06-19

### Added

- Language picker lists each locale as a direct, checkmarked item.
- Plugins get a language hook (`getLang` + `onLangChange`).
- Bumped the HA Discovery catalog entry to 0.0.1rc5.

## [0.1.0rc48] — 2026-06-19

### Added

- **Internationalized the UI** with a data-driven locale catalog (drop a
  `static/locales/xx.json` to add a language; key parity enforced).

## [0.1.0rc47] — 2026-06-17

### Changed

- **Re-host the embedded bridge as an `asyncio.Task` via `start_async()`**
  instead of a daemon thread. MQTT isolation/throughput is unchanged (the shared
  resource is the GIL, not the loop); the decision now rests on integration
  simplicity. Validated by real-broker e2e + a SIGTERM graceful-shutdown check
  (retained `bridge/config` clears after stop).

### Fixed

- Track the bundled `plugins.json` (it was being caught by the `*.json`
  gitignore).

## [0.1.0rc46] — 2026-06-16

### Added

- **In-app plugin catalog** — install / update / uninstall drop-in plugins from
  the UI.

## [0.1.0rc45] — 2026-06-15

### Fixed

- Serve plugin static assets with no-cache so updates aren't masked by a stale
  cache.

## [0.1.0rc44] — 2026-06-15

### Fixed

- Normalize public cloud IPs to `Auto` and treat `Auto` as a compare wildcard
  for IP and VER, so a dynamic/cloud-supplied address no longer shows as a
  spurious mismatch.

## [0.1.0rc43] — 2026-06-15

### Added

- Pin the plugin tab bar inside the sticky header so it stays put while the
  device list scrolls.

## [0.1.0rc42] — 2026-06-15

### Added

- Auto-reload on manager restart so new plugin tabs appear without a manual
  refresh.

## [0.1.0rc41] — 2026-06-15

### Added

- Per-tab header-action scope, symmetric for the manager and plugins.

## [0.1.0rc40] — 2026-06-15

### Added

- Runtime "Load new plugins" (add-only) and "Restart manager" menu items.

## [0.1.0rc39] — 2026-06-15

### Added

- Load drop-in plugins from a directory (`--plugin-dir`).

## [0.1.0rc38] — 2026-06-15

### Added

- "Bridge info" drawer (embedded vs external mode) with a plugin-extensible
  header menu.

## [0.1.0rc37] — 2026-06-14

### Fixed

- Compact header-menu icon; hide filter counts on mobile.

## [0.1.0rc36] — 2026-06-14

### Added

- Unified actions menu with a "Reconfigure bridge" action.

## [0.1.0rc35] — 2026-06-14

### Added

- **TLS + broker authentication for the manager's own MQTT connection**
  (`mqtts://`, `--mqtt-user`/`--mqtt-pass`).

## [0.1.0rc34] — 2026-06-14

### Added

- Page through the bridge's paginated `status` reply so large fleets aren't
  truncated.
- Surface the bridge version in the UI; redact bridge credentials before
  exposing the config to plugins.

### Changed

- Single-source the package version in `__init__.py:__version__`.

## [0.1.0rc33] — 2026-06-11

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc23` → `>=0.2.0rc24`.

## [0.1.0rc32] — 2026-06-10

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc21` → `>=0.2.0rc23`.

## [0.1.0rc31] — 2026-06-09

### Added

- Scope the header "Refresh" and other device-only actions to the Devices tab.

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc19` → `>=0.2.0rc21`.

## [0.1.0rc30] — 2026-06-08

### Fixed

- Parse multi-DP event payloads via the bridge's `parse_seed_dps` so a bare DPS
  object (no `{dp}` in the topic) is read byte-identically to the bridge.

## [0.1.0rc29] — 2026-06-08

### Added

- Plugin `ctx` read accessors and `publish_raw` (manager-host capabilities).

## [0.1.0rc28] — 2026-06-07

### Added

- **Universal, HA-agnostic plugin host** — the base layer third-party plugins
  build on.

## [0.1.0rc27] — 2026-06-07

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc17` → `>=0.2.0rc18`.

## [0.1.0rc26] — 2026-06-05

### Changed

- rc25 `ruff` fix + version bump.

## [0.1.0rc25] — 2026-06-05

### Added

- Handle the bridge's `clear` action ack.

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc17`.

## [0.1.0rc24] — 2026-06-04

### Added

- Persistent warning banner when the retained `bridge/config` is cleared (helps
  diagnose an `mqtt_root_topic` change).

### Dependencies

- Bumped `tuyawizard` `>=0.1.8` → `>=0.1.9`.

## [0.1.0rc23] — 2026-06-04

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc11` → `>=0.2.0rc16`.

## [0.1.0rc22] — 2026-06-03

### Added

- In-process supervisor for the embedded bridge so the bridge's `reconfigure`
  self-terminate gets a fresh in-process server (the native binary's
  `systemd Restart=always` equivalent).

### Fixed

- Precedence: distinguish "user passed a flag" from "argparse default" so
  `config.json` edits aren't silently overridden.

### Dependencies

- Bumped `pyrustuyabridge` `>=0.2.0rc8` → `>=0.2.0rc11` for `reconfigure`.

## [0.1.0rc21] — 2026-06-03

### Changed

- Delegate payload reverse-parsing to `pyrustuyabridge` so the manager's
  interpretation stays byte-identical to the bridge's.

### Added

- N-cycle memory-leak soak suite pinning the rc19/rc20 fixes.

## [0.1.0rc20] — 2026-06-01

### Fixed

- Close a per-call `requests.Session` leak by closing `TuyaWizard` in a
  `finally`.
- Shield the WS race-cleanup `gather` to stop a flaky `CancelledError`.

## [0.1.0rc19] — 2026-06-01

### Fixed

- **Per-WebSocket-connection memory leak** — race `wait_for_change` against
  `ws.receive` so a closed connection's waiter doesn't linger.

## [0.1.0rc18] — 2026-05-29

### Added

- Docker `EMBED_BRIDGE` env var (default 1) to opt out of the embedded bridge.

### Fixed

- Clean programmatic shutdown of the embedded bridge via `pyrustuyabridge`'s
  `stop()` (0.2.0rc5).

## [0.1.0rc17] — 2026-05-20

### Fixed

- Expanded-card layout polish; preserve drag-select.

## [0.1.0rc16] — 2026-05-20

### Fixed

- UI polish on the wizard and missing cards (scan-visibility dot; disable Start
  while a flow is in-flight).

## [0.1.0rc15] — 2026-05-20

### Added

- Centralized LAN scan coordinator; show the scan diff in expanded cards.

## [0.1.0rc14] — 2026-05-19

### Dependencies

- Pick up `pyrustuyabridge` 0.1.4 / `rustuya` 0.2.8.

## [0.1.0rc13] — 2026-05-19

### Added

- Bridge-side scan.

### Dependencies

- `tuyawizard` 0.1.7 QR-login fix.

## [0.1.0rc12] — 2026-05-18

### Fixed

- Tag retained-only devices instead of fake-stamping `last_seen`.

### Dependencies

- Pick up `pyrustuyabridge` 0.1.3 / `rustuya` 0.2.7.

## [0.1.0rc11] — 2026-05-17

### Changed

- **Migrated to `aiomqtt`** for the manager's MQTT connection.

## [0.1.0rc10] — 2026-05-15

### Added

- Header 📡 Scan button to surface IP-mismatched fixed-IP devices.

## [0.1.0rc9] — 2026-05-15

### Added

- IP-mismatch error visibility (renders the bridge's `ip_mismatch` 906 frame).

### Dependencies

- Dep bumps for the upstream IP-mismatch change.

## [0.1.0rc8] — 2026-05-15

### Added

- Tuya Cloud wizard "scan device IPs after fetch" toggle (off by default, so the
  bridge does runtime UDP discovery and survives DHCP IP rotation).

## [0.1.0rc7] — 2026-05-15

### Added

- **PUID/PGID entrypoint** (LinuxServer.io style) so a bind-mounted `/data` owned
  by a non-1000 UID works without pre-`chown`.

## [0.1.0rc6] — 2026-05-15

### Added

- **Docker image + publish workflow**; default web port 8080 → 8373.
- Playwright UI smoke suite; publishes gated on the same checks as CI.

## [0.1.0rc5] — 2026-05-14

### Changed

- Mobile UI polish: search clear button, filter "all" as a real toggle, folding
  sort dropdown, hamburger + sync-bar compaction.

## [0.1.0rc4] — 2026-05-13

### Added

- `--bridge-config` doubles as the broker/root source for the embedded bridge.

## [0.1.0rc3] — 2026-05-13

### Added

- `--bridge-config` flag to load / auto-create the embedded bridge's config.

## [0.1.0rc2] — 2026-05-13

### Added

- MQTT broker retry loop, the embed-bridge flag, and three new warning states.

## [0.1.0rc1] — 2026-05-12

### Added

- First TestPyPI release: reset to `0.1.0`, loopback default, `--auth`, and a
  systemd example.
