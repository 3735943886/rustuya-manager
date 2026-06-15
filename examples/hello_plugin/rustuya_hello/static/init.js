// Eagerly-loaded init module (registered via ctx.add_header_init in __init__.py).
// Runs once at UI boot — before/regardless of opening the Hello tab — so the
// hamburger menu item below is always available. `init(ctx)` receives the same
// context a page mount gets, plus ctx.addHeaderAction.

export function init(ctx) {
  ctx.addHeaderAction({
    id: "hello-ping", // namespaced so it can't clash with a built-in id
    iconHtml: "⚡",
    labelHtml: "Ping (hello)",
    // No `scope` → defaults to this plugin's own tab ("hello"), so the item
    // shows only while the Hello tab is active. Pass `scope: "global"` to show
    // it on every tab instead.
    onClick: async () => {
      try {
        const res = await ctx.api("/api/hello/ping", { method: "GET" });
        ctx.toast(`pong — ${res?.pings ?? "?"} pings`, "ok");
      } catch (e) {
        ctx.toast(`ping failed: ${e.message}`, "error");
      }
    },
  });
}
