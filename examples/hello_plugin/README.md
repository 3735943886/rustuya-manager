# hello_plugin — example rustuya-manager plugin

A throwaway plugin that proves the manager's plugin host exposes all six
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
| Header menu item | eager `static/init.js` → `ctx.addHeaderAction` ("Ping (hello)") |

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
   - Or, without leaving Devices: open the ☰ menu → **Ping (hello)** (added by
     the plugin's eager `init.js`) → same toast, no tab switch.
3. Publish an MQTT message and watch the tab update live:
   ```bash
   mosquitto_pub -t 'hello/world' -m '{"msg":"hi"}'
   ```

Uninstall to confirm the manager returns to its exact plugin-less behaviour
(no tab bar, no `plugins` key in the WS snapshot):

```bash
pip uninstall rustuya-hello-plugin
```

## As a drop-in (no install)

The same plugin works without pip — copy the `rustuya_hello/` package into a
plugin directory and point the manager at it:

```bash
cp -r examples/hello_plugin/rustuya_hello /path/to/plugins/
rustuya-manager --web --plugin-dir /path/to/plugins
```

In Docker the image already sets `PLUGIN_DIR=/data/plugins`, so mounting the
package there (`-v ./plugins:/data/plugins`) and restarting is enough. Drop-in
plugins can't install dependencies and run in-process — only use ones you trust.
