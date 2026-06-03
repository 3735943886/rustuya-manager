# rustuya-manager — Internals

> **For advanced users.** The [README](../README.md) covers what the manager
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
`threading.Thread(daemon=True)`. At first glance this design appears
questionable: the manager is otherwise pure asyncio (FastAPI + uvicorn +
aiomqtt), and mixing threads into an asyncio application is generally
indicative of a structural problem. The hazard underlying that heuristic —
concurrent access to shared mutable Python state from both the loop and
the thread — does not arise here: the embedded bridge shares no Python
state with the manager. The only channel between them is the MQTT broker,
the same one used when the bridge runs in a separate process. §1.2
develops the point; the rest of this section explains why the bridge
runs as a blocking call on a thread rather than as an awaited
`start_async()` task on the loop.

### 1.1 Two runtimes in one process

`PyBridgeServer.start()` is **not** a coroutine — it's a blocking call that
spins up the bridge's own **tokio** (Rust async) runtime and runs until the
server exits. From the binding's docstring:

> Start the server and block the current thread until it exits. The Python
> GIL is released while running.

Two facts follow:

1. **It cannot be awaited as-is.** `start()` is a blocking synchronous
   call, not a coroutine; it cannot be scheduled on the manager's asyncio
   loop directly, and running it on the main thread would freeze the loop
   entirely. The binding does expose `start_async()` as an awaitable
   counterpart — §1.2 below explains why we nonetheless choose `start()`.
2. **It releases the GIL.** Because the PyO3 layer detaches the GIL for the
   whole run, a daemon thread hosting `start()` runs the bridge *truly in
   parallel* with the Python asyncio loop — the two do not contend for the
   GIL.

Once `start()` is chosen (per §1.2), a dedicated thread is not an arbitrary
host; it is the natural one for a blocking, GIL-releasing foreign runtime.
This is the scenario asyncio's own `run_in_executor` / `asyncio.to_thread`
exist to serve — except those target *bounded* blocking work and draw from
a shared pool. An indefinitely-running server would permanently occupy a
pool slot, so a purpose-built `threading.Thread` is the more appropriate
tool than the executor helpers.

### 1.2 Why not `start_async()`?

The binding also exposes `start_async()` — "Start the server asynchronously
in the Python asyncio event loop." It's tempting as the single-loop,
heuristic-satisfying option. We deliberately don't use it.

The key observation: `start_async()` does **not** yield a single event loop. It
bridges the bridge's tokio futures onto the Python loop via
`pyo3-async-runtimes`, so tokio is still there — it's just driven in
lockstep with the Python loop instead of on its own threads.

| | `start()` (thread — what we use) | `start_async()` |
| --- | --- | --- |
| tokio runtime | dedicated, multi-threaded, isolated | shared (`pyo3-async-runtimes`), pumped with the Python loop |
| If a Python handler stalls the loop | bridge MQTT continues to run (other thread, GIL released) | bridge progress stalls too |
| Worker parallelism | scanner + mqtt + listeners on their own threads | coupled to the single Python thread |
| Integration surface | minimal — independent of the Python loop | depends on `pyo3-async-runtimes` runtime init/teardown |
| Cancellation / lifecycle | manual: `stop()` + `join` (see §1.3) | `await` / cancel from asyncio (cleaner) |

The deciding factor is **MQTT correctness, which is this project's top
priority.** A dedicated tokio runtime means that a stall anywhere in the
manager's web/asyncio layer — a slow request handler, a blocked coroutine,
a loop that briefly stops being pumped — cannot starve the bridge's MQTT
loop. `start_async()` would couple the two: the same stall would delay
bridge publishing. We trade the cleaner asyncio lifecycle for runtime
isolation, deliberately.

**The one residual advantage of `start_async()`** is programmatic
failure routing: an awaited task surfaces an unexpected `run()`
exception directly to the manager's asyncio loop, where the thread
route would otherwise drop it inside the daemon. The thread route
narrows that gap with `_EmbeddedBridgeSupervisor`
([../src/rustuya_manager/cli.py](../src/rustuya_manager/cli.py)): the
supervisor wraps every iteration of `PyBridgeServer.start()` in a
`try`, logs the traceback via the manager's logger, backs off, and
respawns under a rate limit. What it does NOT do is hand the exception
object back to the asyncio loop for a structured reaction, so a future
"manager fails closed if the bridge crashes" feature would still be a
few lines lighter on the async route. For everything else —
operator-visible logs, automated restart, restart-on-reconfigure (see
§1.3) — the supervisor closes the observability gap that originally
favoured `start_async()`. Wedged-but-alive bridges remain the
`bridge_offline` MQTT-presence path's job, on either route.

**Conditions under which `start_async()` would be preferable** — noted so
the trade-off is presented honestly rather than as dogma: embedding *many*
bridges in one process (the thread route spins up a full multi-threaded
tokio runtime per server; the async route shares one
`pyo3-async-runtimes` runtime), running short-lived bridges that start and
stop frequently inside the loop (the awaitable lifecycle is cleaner there),
or simply not ranking MQTT isolation first. The manager satisfies none of
these conditions: it embeds exactly one bridge for the whole process lifetime.

**Why the thread aligns with the manager's model.** The embedded bridge is
the *secondary* mode. The manager's primary, default role is to manage a
`rustuya-bridge` running as a **separate process**, communicating with it
only over MQTT — fully isolated, with no shared memory and no shared
runtime. The embedded thread is the same arrangement folded into one
process: a daemon that conceptually should be a subprocess, hosted on a
thread purely as a deployment convenience (one fewer process to
supervise, no separate binary on disk). The manager reaches it through
the broker, never via shared Python state.

This is also what makes the asyncio-plus-thread mixing safe here. The
hazard the heuristic guards against — concurrent access to shared mutable
Python state from the asyncio loop and a worker thread — depends on
shared state existing. None does: the only channel between the two sides
is the MQTT broker, which is process-external and serialises every
exchange. The thread route is therefore not an exception to the manager's
design but a mirror of it; `start_async()` would be the anomalous choice,
fusing two layers that the manager otherwise deliberately keeps decoupled.

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
calls `supervisor.stop()`, which in turn trips the live server's
`stop()` — the binding's sync, lock-free cancel (added in
pyrustuyabridge 0.2.0rc5). It trips the bridge's internal
`CancellationToken`, which lives *outside* the `BridgeServer` mutex, so
`run()` observes the cancel, returns, and performs graceful MQTT cleanup
(retained-config clear + state flush) on its way out. The caller's
`thread.join(timeout=5)` is the barrier that waits for that to finish.

This replaced an earlier `server.close()` followed by a fixed
`asyncio.sleep(0.1)`. That arrangement contained a subtle defect:
`close()` required the `BridgeServer` mutex that a running `start()` holds
for the entire duration of `run()`. With an OS signal the bridge's own
handler would return `run()` first and release the lock, so `close()`
succeeded incidentally — but on a **non-signal** shutdown (e.g. the web
server raising an exception) nothing released the lock, `close()` could
never acquire it, and MQTT cleanup was silently skipped, leaving retained
state behind.
The out-of-mutex token closes that gap: `stop()` needs no lock and no
asyncio runtime, so both shutdown paths now run identical graceful cleanup.

**`_EmbeddedBridgeSupervisor` distinguishes "stop" from "reconfigure".**
Since rustuya-bridge `b98e152` (Python 0.2.0-rc.9) the bridge's
`reconfigure` action self-terminates `run()` through the *same*
`CancellationToken` that `stop()` trips, on the assumption that a
process supervisor will bring it back — standalone deploys put
`Restart=always` on systemd; the embedded case has to do the equivalent
in-process. From the outside, a reconfigure exit and a stop exit are
indistinguishable on the bridge side: both leave `start()` returning
without an exception. The manager-side supervisor resolves the ambiguity
with its own `threading.Event`:

- Bridge cancels its token (reconfigure, or any clean self-termination)
  → `start()` returns → supervisor sees its stop-Event clear → constructs
  a fresh `PyBridgeServer` and loops.
- Manager calls `supervisor.stop()` → sets stop-Event AND trips the live
  server's token → `start()` returns → supervisor sees stop-Event set →
  exits the loop.

Crashes (`start()` raises) take a third path: log the traceback, wait
`_CRASH_BACKOFF_SEC` on the same Event (so a stop request during backoff
returns immediately rather than always waiting the full window), then
respawn unless the rate limit
(`_MAX_RESTARTS_IN_WINDOW` exits in `_WINDOW_SEC`) has tripped — at
which point the supervisor surfaces an error log and gives up, leaving
recovery to a manager-process restart. The supervisor forces
`no_signals=True` regardless of caller input so the rule above this
paragraph cannot be silently bypassed.

### 1.4 Single-use

Once `stop()` (or a completed `run()`) has cancelled the token, the server
has been consumed — `start()` / `start_async()` reject reuse rather than
silently returning as a no-op. To restart, construct a fresh
`PyBridgeServer`.

The supervisor in §1.3 honours this rule by instantiating a new
`PyBridgeServer(**self._kwargs)` at the top of every loop iteration; the
previous version of this section noted that "the manager never restarts
an embedded bridge in-process", which is no longer true — the
`reconfigure` action explicitly depends on the supervisor doing exactly
that — but the underlying single-use constraint is unchanged. The
restart cost is bounded: tokio runtime spin-up + retained-config
re-subscribe, on the order of a few hundred milliseconds, which is the
implicit budget any user of `reconfigure` already accepts.
