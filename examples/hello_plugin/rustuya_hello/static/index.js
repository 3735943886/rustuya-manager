// Example plugin page. Exported `mount(rootEl, ctx)` is called once by the
// manager's client-side plugin host when the "Hello" tab is first opened.
//
// Demonstrates the frontend ctx surface:
//   ctx.getState()      latest WS snapshot (incl. snapshot.plugins.hello)
//   ctx.onState(cb)     live updates as new WS frames arrive
//   ctx.api(path,opts)  auth/JSON fetch wrapper
//   ctx.toast / confirm  shared UI helpers

export function mount(rootEl, ctx) {
  rootEl.innerHTML = `
    <div class="bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 p-4 space-y-3 text-sm">
      <h2 class="text-base font-semibold">Hello plugin</h2>
      <p class="text-slate-500 dark:text-slate-400">
        Proves the plugin host: state namespace over WS, an API round-trip, and
        live MQTT-driven updates. Publish JSON to <code>hello/&lt;anything&gt;</code>
        to see the bottom block update live.
      </p>
      <button id="hello-ping" type="button"
        class="text-xs px-3 py-1.5 rounded bg-slate-900 hover:bg-slate-800 dark:bg-slate-200 dark:text-slate-900 dark:hover:bg-white text-white font-medium">
        Call /api/hello/ping
      </button>
      <pre id="hello-state" class="text-xs bg-slate-50 dark:bg-slate-900 rounded p-3 overflow-auto"></pre>
    </div>
  `;

  const $state = rootEl.querySelector("#hello-state");
  const renderState = (snapshot) => {
    const data = snapshot?.plugins?.hello ?? null;
    $state.textContent = JSON.stringify(data, null, 2) || "(no state yet)";
  };

  renderState(ctx.getState());
  ctx.onState(renderState);

  rootEl.querySelector("#hello-ping").addEventListener("click", async () => {
    try {
      const res = await ctx.api("/api/hello/ping");
      ctx.toast(`ping → pings=${res.pings}`, "ok");
    } catch (e) {
      ctx.toast(`ping failed: ${e.message}`, "error");
    }
  });
}
