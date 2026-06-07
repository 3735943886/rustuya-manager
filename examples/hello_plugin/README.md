# hello_plugin — example rustuya-manager plugin

A throwaway plugin that proves the manager's plugin host exposes all five
surfaces. **Not** part of the manager package (absent from its package-data and
entry points); it lives here purely for manual end-to-end verification.

## What it exercises

| Surface | Where |
|---|---|
| API router | `GET /api/hello/ping` (returns a ping counter) |
| MQTT subscription | `hello/#` → writes the `hello` state namespace |
| State namespace | `"hello"`, broadcast over the existing `/ws` snapshot |
| UI page | a "Hello" tab serving `static/index.js` |
| Bridge client | `ctx.bridge_client` (available for publishing) |

## Manual e2e

```bash
# from the manager repo, into the same venv the manager runs in
pip install -e examples/hello_plugin
rustuya-manager --web        # a "Hello" tab appears next to "Devices"
```

Then:
1. Open the UI → click the **Hello** tab → the state block renders.
2. Click **Call /api/hello/ping** → toast shows the incrementing counter, and the
   state block updates live (namespace → WS → `onState`).
3. Publish an MQTT message and watch the tab update live:
   ```bash
   mosquitto_pub -t 'hello/world' -m '{"msg":"hi"}'
   ```

Uninstall to confirm the manager returns to its exact plugin-less behaviour
(no tab bar, no `plugins` key in the WS snapshot):

```bash
pip uninstall rustuya-hello-plugin
```
