# Design: manager plugin runtime — reactive DPs & in-process daemons

> **Status: implemented — Pillars 1 & 2 shipped (`PLUGIN_API_VERSION = 2`).**
> This started as a pre-implementation design record and is kept current. It
> fixes the decisions and explains *why* the runtime is shaped as it is; the live
> API is in `plugins.py` (`PluginContext`) and `mqtt.py` (`BridgeClient._route`
> and the derived-DP publish path), which are the reference for exact signatures.
> It defines what runs *inside* the manager and where the boundary is — services
> that must outlive manager restarts are independent and not hosted here (§6).
> MQTT correctness is the top constraint. Source references use function names,
> not line numbers.

**The manager's plugin runtime has two pillars, sharing one in-process
`(device_id, dp, value)` bus:**

1. **Reactive — derived DPs.** A plugin watches decoded DPs and computes a
   *derived DP* (combine inputs, accumulate, republish). Event-driven; §2–§5.
2. **In-process daemons.** A plugin runs a *long-lived supervised async
   coroutine* (`add_service`) — the general, Python-first way to run a daemon
   inside the manager; §6.

The general mechanism is in-process Python (most future plugins are Python).
The one exception — services that cannot tolerate a manager restart, like a
Matter fabric — are independent deployments the manager only *talks to*, never
hosts (§7).

**Scope: this builds the manager-side *conduit*, not any specific consumer.**
The deliverable now is the general runtime (Pillars 1+2) — the hooks future
plugins plug into. Matter is **not** built here; it is a future consumer that
will plug in later (externally via the existing MQTT/UI surfaces, or in-process
via this runtime). §7 records *why* the conduit's boundary sits where it does,
using Matter only as the illustrating example — not as work in this scope.

---

## 1. Goal

Let a plugin delegate real-time, stateful Python to the manager daemon: the
manager hands a plugin decoded `(device_id, dp, value)` updates over an
in-process bus; the plugin reacts to them (Pillar 1) and/or runs a long-lived
service that uses the same bus (Pillar 2).

- **Now:** combine DPs from a Tuya device into a derived value, expose it to
  Home Assistant (Pillar 1).
- **General future:** Python plugins that run their own daemons in-process
  (Pillar 2) — schedulers, aggregators, protocol adapters that tolerate a
  restart blip.
- **The hard exception:** a Matter fabric — independent service, not hosted
  (§7).

The manager already owns the Tuya half (`publish_command` outbound,
`merge_dps` inbound). The runtime adds a clean in-process boundary on top.

---

## 2. Architecture: the in-process DP bus

```
Tuya ──[decode]──▶ (device_id, dp, value) ──▶ plugin (handler or service) ──▶ derived value / set
        manager         DP bus (function calls)        react / long-lived loop
```

The seam is **function calls, not MQTT**: the bus carries already-decoded DPs,
so a plugin never re-implements the bridge's payload template — preserving the
byte-identical constraint between manager and bridge. Both pillars use the same
bus: a reactive handler and a daemon's loop both call `watch_dps` /
`set_device_dp` / `derived_dp`.

The single interception point is **`BridgeClient._route`**, immediately after
`State.merge_dps` returns for a device event. The bus dispatch runs *outside*
`State._changed` (merge_dps holds the condition lock; handlers must not run
under it).

---

## 3. Locked decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Plugins (handlers and daemons) run **in-process** on the manager's single asyncio loop. | Already the plugin model; Python-first; no subprocess. |
| D2 | The seam is the **DP bus (function calls)**, not an MQTT round-trip. | No double hop, no topic-space collision, lowest latency. |
| D3 | Inbound hook fires from **`_route`**, after `merge_dps`, never under the state lock. | Single decoded chokepoint; avoids deadlock/blocking on the lock. |
| D4 | The API is **low-level primitives**, not a declarative rule DSL. | User asked for flexibility; declarative sugar can be layered later. |
| D5 | Derived republish uses a **distinct `{type}` segment (`derived`)** on the real device's id. | Sibling topic to `active`/`passive`/`state` → no retained-snapshot overwrite; the bridge scavenger wildcards `{type}` and matches by id, so retained derived topics are **cleared on device removal automatically**. |
| D6 | The manager **must not re-ingest** `{type}=derived` echoes through the device-event path. | Otherwise its own publish stamps `last_seen`/liveness and can loop. Filter on `{type}` in `_route`. |
| D7 | **Rule-level cleanup is the manager's job**; device-removal cleanup is the bridge's. | The scavenger only fires on `remove`/`clear`/`reconfigure`. A rule deleted while its device still exists self-clears via `publish_raw(topic, "", retain=True)`. |
| D8 | Derived DPs flow **outward only** — never into `State`/the WS snapshot. | No UI exposure; wire stays byte-identical to a plugin-less manager. |
| D9 | **Phase 0–1 were folded into v0.1.0; the memory soak was reset** when they landed (rc58). | Validate the new server-side execution surface *inside* the v0.1.0 soak. |
| D10 | `add_service` is an **in-process async coroutine — the only daemon form.** | User's choice. Blocking work is the plugin's own `asyncio.to_thread`; the manager offers no thread/child-process daemon surface. Minimal, flexible for async Python. |
| D11 | **Continuity-critical / hard-isolation services are NOT manager-hosted.** | The manager re-execs (`os.execvp` in `_reexec_process`) on every plugin install/enable/disable + config change. In-process daemons restart cleanly there (fine for general work); a service that can't tolerate that (a Matter fabric) is independent + MQTT. See §7. |
| D12 | Where a clean rebuild and a patch-to-reuse compete, **rebuild clean.** | [[feedback_design_over_reuse]]. Drove the native-Rust Matter daemon over reusing the `matterbridge` pyo3 binding. |

---

## 4. Why `{type}=derived` is correct (verified against the bridge)

The bridge's `{type}` is a per-message topic segment, not a device class.
`spawn_retain_scavenger`:

1. Subscribes to `tpl_to_wildcard(event_topic)` → `{root}/event/+/{id}`,
   wildcarding the `{type}` position, so it collects retained messages of
   **every** type value including `derived`.
2. Matches each retained topic by `{id}`/`{name}`/`{cid}` (plus a quoted-id
   payload fallback). `{type}` is **not** a match criterion.
3. Clears matches with an empty retained payload — **regardless of who
   published them**.

So a manager-published retained `{root}/event/derived/{id}` is cleared when
device `{id}` is removed, for free, with cascade semantics. This is why D5 is
preferred over a hand-built tombstone ledger. **Caveat (D7):** the scavenger
fires only on bridge registry mutation; any other reason a derived value should
vanish must be cleared explicitly.

---

## 5. Plugin API surface (additions to `PluginContext`)

```python
# ── Pillar 1: reactive — decoded DP updates ──────────────────────────────
ctx.watch_dps(handler)                 # async (device_id, dps: dict, origin) -> None
ctx.watch_device(device_id, handler)   # filtered convenience
ctx.watch_dp(device_id, dp, handler)   # filtered convenience
#   origin is "device" for a live event and "retained" for the retained-snapshot
#   replay on (re)connect, so a handler can tell an initial-state seed from a real
#   change. Derived echoes are filtered at _route and never fire watchers.
#   handler keeps its own state in a closure → stateful/accumulator logic is free.

# derived output — a registered object with a lifecycle, not fire-and-forget:
vd = ctx.derived_dp(device_id, dp, *, retain=None)
await vd.set(value)     # render {type}=derived topic, publish (retain mirrors config), record
await vd.clear()        # publish empty retained — the rule-level cleanup path (D7)
#   retain=None → mirror the bridge's mqtt_retain. The manager renders BOTH the
#   topic and the byte-identical payload; the plugin never touches bridge config.

await ctx.set_device_dp(device_id, dp, value)   # outbound → publish_command("set", …)

# ── Pillar 2: in-process daemon — one supervised async coroutine ──────────
ctx.add_service(coro_factory)
#   Manager-owned lifecycle: started after bootstrap, restarted with crash
#   backoff, cancelled (graceful) on shutdown AND before a re-exec. The coroutine
#   uses the same bus (watch_dps / set_device_dp / derived_dp). Blocking work →
#   the plugin's own asyncio.to_thread. State that must survive a restart →
#   persist to disk (re-exec re-runs register() and re-starts the service).
```

Existing surfaces (`add_mqtt_subscription`, `publish_raw`, `state_namespace`,
`add_api_router`, pages) are unchanged — and are exactly how a plugin talks to
an *external* service (§7). `derived_dp` is a thin wrapper over `publish_raw`;
full control stays available via `publish_raw` + `bridge_config()`.
`PLUGIN_API_VERSION` is now `2`; a plugin gates on these methods with
`ctx.api_version >= 2`.

---

## 6. In-process daemon supervision (Pillar 2)

`add_service` is supervised by `ServiceSupervisor` (plugins.py), which reuses
the embedded-bridge supervision pattern (`_EmbeddedBridgeSupervisor`:
`asyncio.Event` stop, crash backoff, `await wait_for(task, …)` shutdown) — clean
reuse of a mechanism that already fits, not a patch.

- **start** after bootstrap (templates resolved, MQTT connected) so the service
  can use the bus immediately.
- **crash** → exponential backoff restart, capped by a circuit breaker; the
  backoff is interruptible by shutdown.
- **stop** → cancel + awaited with a timeout, on shutdown and before a re-exec,
  so the service can drain.
- **re-exec** replaces the process; the service does not survive it but is
  re-registered and re-started automatically. The gap is seconds — acceptable
  for general daemons (a scheduler, an aggregator). A service whose external
  peers cannot tolerate that gap does not belong here → §7.
- **soak** — services run under one supervised task group so the soak telemetry
  attributes growth to a named plugin.

There is intentionally **no thread / child-process / external daemon form** in
the API (D10). Blocking work is the plugin's own `asyncio.to_thread`.

---

## 7. The boundary: where the conduit stops (illustrated by Matter)

This section explains *why the conduit's boundary sits where it does*. No work
here builds Matter — it is only the example that defines the edge.

Some services must be **24/7 and survive manager restarts**. An in-process
daemon (§6) restarts on re-exec, so it cannot provide that. Such services are
**independent deployments the manager does not host** — and the conduit for them
**already exists**: the manager talks to any external system with
`add_mqtt_subscription` + a UI page. They need no new manager surface and do
**not** use `add_service`.

A Matter fabric is the canonical case (if it dropped on every plugin operation,
controllers would see the bridge go offline and reset commissioning). So when a
Matter plugin is built *later*, it plugs into the conduit as an external
consumer — an independent service over MQTT plus a thin observe/commission UI
plugin — designed in its own repos (`../matterdaemon/docs/`,
`../rustuya-matter/docs/`), not here. Rust there is because Matter is *complex*,
not because daemons must be Rust — general plugin daemons stay Python in-process
(§6).

Matter is only **one** such consumer. The same boundary holds for any critical
independent daemon — a Zigbee/Z-Wave bridge, a cloud-sync daemon, anything whose
sessions must outlive a manager restart. Nothing in this conduit is
Matter-specific; it is a **general shared space**, and each consumer picks the
surface its needs dictate.

The takeaway for *this* document: the conduit has two surfaces — the in-process
runtime (Pillars 1+2, new) and the existing MQTT/UI surfaces (for external
consumers, critical or not). A future plugin plugs into whichever fits.

---

## 8. Invariants (must hold — these are the failure modes)

1. **No overwrite.** Derived publishes use `{type}=derived` only; never
   `{type}=state`/`active`/`passive` (would clobber the bridge's retained
   snapshot).
2. **No re-ingestion.** `_route` drops `{type}=derived`; it never reaches
   `merge_dps`, so it cannot stamp `last_seen`, flip `retained_only`, or fire
   the inbound bus.
3. **No unpaired retain.** A retained `set()` is only allowed if a `clear()`
   path exists. Device-removal cleanup is delegated to the bridge scavenger
   (verified); rule-level cleanup is explicit (D7).
4. **No handler under the lock.** The bus dispatches after `merge_dps` returns.
5. **No silent loop.** A derived value derived from another derived value is
   allowed but depth-bounded; cross-plugin chains are capped and logged.
6. **No orphan service.** Every `add_service` coroutine is cancelled and awaited
   on shutdown/re-exec; none is left detached.

Each invariant gets a regression test (§10).

---

## 9. id strategy

- **Per-device (default):** derived value belongs to one device. Topic = event
  template with `{id}=device_id`, `{type}=derived`. Cascade cleanup on device
  removal is automatic.
- **Composite/synthetic (later):** combines DPs across devices into a new logical
  entity with no parent to cascade from → registered via the bridge `add`
  action, `remove` manager-owned. Deferred until a concrete need.

---

## 10. Phasing & test gates

All three phases shipped in v0.1.0; the gates below are the regression tests
that now guard them.

**Phase 0 — boundary contract (shipped).** API signatures, the `_route`
`{type}=derived` filter, `PLUGIN_API_VERSION` bump. No behaviour change. *Gate:*
existing plugin tests pass; a test asserts `{type}=derived` is dropped by
`_route` (invariant 2).

**Phase 1 — reactive (shipped).** Folded into v0.1.0; the soak was reset when
Phase 0–1 landed (D9). `watch_dps`/`watch_device`/`watch_dp`,
`derived_dp().set/clear`, `set_device_dp`, and the `examples/derived_plugin`
example: combine two Tuya DPs → derived DP → HA. *Gates:* (a) a watcher fires on
a real event and not on a derived echo; (b) `set()` publishes `{type}=derived`
with retain mirroring config; (c) `clear()` empties it; (d) a simulated device
`remove` leaves no orphan; (e) invariants 1–4 each have a test.

**Phase 2 — in-process daemons (shipped).** `add_service` supervision (§6,
`ServiceSupervisor`): start after bootstrap, crash backoff, graceful stop on
shutdown/re-exec, soak attribution. *Gates:* a service starts, drives the bus,
survives a crash via backoff, is cancelled+awaited on shutdown (invariant 6),
restarts cleanly across a re-exec.

**Matter (separate repos).** The independent ecosystem (§7) is phased in
`../matterdaemon/docs/` and `../rustuya-matter/docs/`. The manager's only
deliverable — a thin `gm/daemon/#` UI plugin — is a small, later addition with
no runtime coupling to this document.

---

## 11. Resolved decisions

- **Soak timing → D9:** Phase 0–1 folded into v0.1.0, soak clock reset (rc58).
- **UI exposure → D8:** derived DPs flow outward only; wire stays byte-identical.
- **Daemon form → D10:** `add_service` = in-process async coroutine only; no
  thread/child/external form.
- **Continuity-critical hosting → D11/D12:** not manager-hosted; independent
  service + MQTT (Matter = native Rust daemon, matterbridge dropped from the
  production path).

---

## 12. Risks

- **Blocking handler/service stalls the loop.** Mitigated by an opt-in soft
  timeout (off by default) and by `asyncio.to_thread` for blocking work.
- **Composite-id cleanup.** Deferred (§9); if it lands it reintroduces
  manager-owned lifecycle and must not regress the scavenger cascade.
- **HA must be told to read `{type}=derived`.** Consumer-side config; gates the
  Phase 1 demo end to end.
- **Matter ecosystem risks** (live-reconfig complexity, gm/ contract
  expressiveness) live in the Matter repos' docs.
