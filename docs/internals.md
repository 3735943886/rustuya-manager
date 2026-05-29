# rustuya-manager — Internals

> **For contributors.** The [README](../README.md) covers what the manager
> does and how to run it. This document explains *why* a few non-obvious
> design choices are the way they are — the kind of thing that's too much
> detail for the README but that you'd otherwise have to reverse-engineer
> from the code. Read selectively; you don't need any of this to use the
> manager.
>
> Source references use function names rather than line numbers, since
> lines drift across refactors. Open the linked file and search by name.

---

## 1. Embedded bridge: why a thread, not an asyncio task

When started with `--embed-bridge`, the manager runs a
`pyrustuyabridge.PyBridgeServer` *inside its own process* instead of
talking to a separate bridge over MQTT. The spawn happens in
[_spawn_embedded_bridge](../src/rustuya_manager/cli.py) on a plain
`threading.Thread(daemon=True)`. At first glance that looks like a smell:
the manager is otherwise pure asyncio (FastAPI + uvicorn + aiomqtt), and
mixing threads into an asyncio app is usually a sign something's off. It
isn't here. This section records why.

### 1.1 Two runtimes in one process

`PyBridgeServer.start()` is **not** a coroutine — it's a blocking call that
spins up the bridge's own **tokio** (Rust async) runtime and runs until the
server exits. From the binding's docstring:

> Start the server and block the current thread until it exits. The Python
> GIL is released while running.

Two facts fall out of that:

1. **It can't be `await`ed.** It's blocking sync, not a Python awaitable, so
   there's no way to schedule it as a task on the manager's asyncio loop
   directly. Running it on the main thread would freeze the loop entirely.
2. **It releases the GIL.** Because the PyO3 layer detaches the GIL for the
   whole run, a daemon thread hosting `start()` runs the bridge *truly in
   parallel* with the Python asyncio loop — they don't fight over the GIL.

So a dedicated thread isn't an arbitrary choice; it's the natural host for a
blocking, GIL-releasing foreign runtime. This is exactly the case asyncio's
own `run_in_executor` / `asyncio.to_thread` exist for — except those target
*bounded* blocking work and draw from a shared pool. An indefinitely-running
server would permanently occupy a pool slot, so a purpose-built
`threading.Thread` is the more correct tool than the executor helpers.

### 1.2 Why not `start_async()`?

The binding also exposes `start_async()` — "Start the server asynchronously
in the Python asyncio event loop." It's tempting as the single-loop,
heuristic-satisfying option. We deliberately don't use it.

The key realization: `start_async()` does **not** give you one event loop. It
bridges the bridge's tokio futures onto the Python loop via
`pyo3-async-runtimes`, so tokio is still there — it's just driven in
lockstep with the Python loop instead of on its own threads.

| | `start()` (thread — what we use) | `start_async()` |
| --- | --- | --- |
| tokio runtime | dedicated, multi-threaded, isolated | shared (`pyo3-async-runtimes`), pumped with the Python loop |
| If a Python handler stalls the loop | bridge MQTT keeps running (other thread, GIL released) | bridge progress stalls too |
| Worker parallelism | scanner + mqtt + listeners on their own threads | coupled to the single Python thread |
| Integration surface | minimal — independent of the Python loop | depends on `pyo3-async-runtimes` runtime init/teardown |
| Cancellation / lifecycle | manual: `stop()` + `join` (see §1.3) | `await` / cancel from asyncio (cleaner) |

The deciding factor is **MQTT correctness, which is this project's top
priority.** A dedicated tokio runtime means a hiccup anywhere in the
manager's web/asyncio layer — a slow request handler, a blocked coroutine,
a loop that briefly stops being pumped — cannot starve the bridge's MQTT
loop. `start_async()` would couple the two: the same hiccup would delay
bridge publishing. We trade the cleaner asyncio lifecycle for runtime
isolation, on purpose.

**The one genuine edge `start_async()` has** is failure observability: a
daemon thread that dies (an unexpected `run()` exception) does so silently —
the manager's loop never sees it. With `start_async()` the exception would
surface on the awaited task, so the manager could detect and react to bridge
death directly. We don't consider this decisive, because (a) the manager
already notices a dead bridge through its MQTT bootstrap / presence
signalling (the `bridge_offline` warning path), so detection isn't exclusive
to the async route, and (b) it doesn't outweigh the isolation above.

**When `start_async()` *would* win** — noted so the trade-off is honest, not
dogma: if you embedded *many* bridges in one process (the thread route spins
up a full multi-threaded tokio runtime per server; the async route shares
one `pyo3-async-runtimes` runtime), or ran short-lived bridges that start and
stop frequently inside the loop (the awaitable lifecycle is cleaner there),
or simply didn't rank MQTT isolation first. The manager hits none of these:
it embeds exactly one bridge for the whole process lifetime.

**Why the thread fits the manager's model anyway.** The embedded bridge is
the *secondary* mode. The manager's primary, default job is to manage a
`rustuya-bridge` running as a **separate process**, talking to it only over
MQTT — fully isolated, no shared memory, no shared runtime. The embedded
thread is just that same arrangement folded into one process: a bridge on
its own runtime that the manager reaches over the broker, never via shared
Python state. So the thread route isn't an exception to the manager's design
— it mirrors it. `start_async()` would be the odd one out, fusing two things
the manager otherwise keeps deliberately at arm's length.

### 1.3 Shutdown: `no_signals=True` + `stop()`

The flip side of running the bridge on its own thread is that shutdown has
to cross the thread boundary cleanly. Two rules make it deterministic:

**The manager owns signals.** [run](../src/rustuya_manager/cli.py) installs
the process's SIGINT/SIGTERM handlers (`loop.add_signal_handler` →
`stop_event.set`). The embedded bridge is therefore spawned with
`no_signals=True` so it does *not* install its own handlers — two signal
handlers competing for the same signal in one process is a race. A signal
now flows down exactly one path:

```
SIGINT  →  manager loop handler  →  stop_event.set()
        →  run() unblocks  →  finally:  _close_embedded_bridge(server)
```

**`stop()`, not `close()`.** [_close_embedded_bridge](../src/rustuya_manager/cli.py)
calls `server.stop()` — the binding's sync, lock-free cancel (added in
pyrustuyabridge 0.2.0rc5). It trips the bridge's internal
`CancellationToken`, which lives *outside* the `BridgeServer` mutex, so
`run()` observes the cancel, returns, and performs graceful MQTT cleanup
(retained-config clear + state flush) on its way out. The caller's
`thread.join(timeout=5)` is the barrier that waits for that to finish.

This replaced an earlier `server.close()` + fixed `asyncio.sleep(0.1)`. That
was subtly broken: `close()` needed the `BridgeServer` mutex that a running
`start()` holds for the entire duration of `run()`. With an OS signal the
bridge's own handler would return `run()` first and release the lock, so
`close()` happened to work — but on a **non-signal** shutdown (e.g. the web
server erroring out) nothing released the lock, `close()` could never
acquire it, and MQTT cleanup was silently skipped, leaking retained state.
The out-of-mutex token closes that gap: `stop()` needs no lock and no
asyncio runtime, so both shutdown paths now run identical graceful cleanup.

### 1.4 Single-use

Once `stop()` (or a completed `run()`) has cancelled the token, the server
is spent — `start()` / `start_async()` reject reuse rather than silently
no-op'ing. To restart, construct a fresh `PyBridgeServer`. The manager never
restarts an embedded bridge in-process, so this doesn't constrain it; it's
noted here so a future "restart the bridge without restarting the manager"
feature doesn't assume reuse works.
