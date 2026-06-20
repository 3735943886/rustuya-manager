# derived_plugin — reactive DP bus example

A throwaway example proving the rustuya-manager **reactive plugin runtime**
(`api_version >= 2`): watch a device's DPs, combine them, and publish the result
as a *derived DP* on the device's `{type}=derived` event topic — where Home
Assistant (or anything) can read it like a normal device value.

```bash
pip install -e examples/derived_plugin
# point it at a real device + DPs (defaults shown):
export RUSTUYA_DERIVED_DEVICE=demo-device
export RUSTUYA_DERIVED_A=1        # input DP a
export RUSTUYA_DERIVED_B=2        # input DP b
export RUSTUYA_DERIVED_OUT=99     # output (derived) DP id
rustuya-manager --web
```

The combiner is a logical AND of two booleans; swap in any function of the
inputs. The plugin holds the latest values in a closure, recomputes on each
event, and calls `derived_dp(device, "99").set(result)`.

What it exercises:

- `ctx.watch_device(device_id, handler)` — react to a device's events
  (`handler(device_id, dps, origin)`); the manager hands decoded `{dp: value}`,
  no MQTT parsing in the plugin.
- `ctx.derived_dp(device_id, dp).set(value)` — publish the derived value; the
  manager renders the `{type}=derived` topic and a byte-faithful payload, and
  mirrors the bridge's `mqtt_retain`. The bridge's retain scavenger clears it
  automatically when the device is removed.
- `ctx.set_device_dp(device_id, dp, value)` is available for the reverse
  (external → Tuya) direction.

Not shipped with the manager and not for production — it is a host-surface
demonstration only.
