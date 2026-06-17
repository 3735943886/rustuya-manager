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

## 1. Embedded bridge: an asyncio task on the bridge's own tokio runtime

When started with `--embed-bridge`, the manager runs a
`pyrustuyabridge.PyBridgeServer` *inside its own process* instead of
talking to a separate bridge over MQTT.
[_spawn_embedded_bridge](../src/rustuya_manager/cli.py) schedules it as an
`asyncio.Task` that `await`s the binding's `start_async()`. The manager is
otherwise pure asyncio (FastAPI + uvicorn + aiomqtt), so an asyncio task is
the native fit — but the choice rests on more than heuristic tidiness, and
the reasoning below is worth keeping because the obvious objection ("an
awaited bridge couples to the manager's loop") turns out to be false.

The embedded bridge shares **no Python state** with the manager. The only
channel between them is the MQTT broker — the same one used when the bridge
runs in a separate process. That fact carries most of this section: it is
why the bridge runs on its own tokio threads regardless of how it is hosted
(§1.1), and why the hosting choice does not change MQTT behaviour (§1.2).

### 1.1 Two runtimes in one process

`start_async()` is **not** a thin coroutine that runs the bridge on the
Python loop. It uses `future_into_py` to spawn the bridge onto
`pyo3-async-runtimes`' own **multi-threaded tokio** (Rust async) runtime and
bridges only a *single completion future* back to the manager's asyncio loop —
that future resolves when the server shuts down. So two runtimes coexist: the
manager's asyncio loop, and the bridge's tokio runtime on its own OS threads.
The PyO3 layer releases the GIL while the bridge runs, so the two execute
truly in parallel.

The consequence that matters: **the bridge's MQTT and device work runs on
tokio's own threads, not on the manager's loop.** Blocking or stalling the
manager's loop — a slow request handler, a briefly un-pumped loop — does not
starve the bridge. As the bridge's own internals put it (rustuya-bridge
`docs/internals.md` §11.3), "the shared resource is the GIL, not the asyncio
loop." The only steady-state Python re-entry is log forwarding, which
reacquires the GIL per record; a host that pins the GIL with a CPU-bound
Python burst can make log delivery jittery, but device handling keeps
running.

This is also why the *other* entry point, `start()` — a blocking,
thread-hosted call that builds its own dedicated tokio runtime — would have
**identical steady-state throughput**. Both run the bridge on tokio threads
with the GIL released; the hosting choice is about lifecycle and integration,
not bridge speed. §1.2 makes that comparison concrete.

### 1.2 Why `start_async()` over `start()`

The binding exposes both entry points. `start()` blocks the calling thread
and must be hosted on a `threading.Thread`; `start_async()` is awaited on the
manager's existing loop. Because §1.1 establishes that **both run the bridge
on its own tokio threads**, the comparison is *not* about MQTT isolation or
throughput — those are identical either way. It is purely about how the
manager hosts and tears down the bridge:

| | `start_async()` (asyncio task — what we use) | `start()` (thread) |
| --- | --- | --- |
| tokio runtime | shared `pyo3-async-runtimes` runtime, multi-thread | fresh dedicated multi-thread runtime per server |
| Footprint | shares the one `pyo3-async-runtimes` runtime | a full tokio runtime per embedded bridge |
| If the manager's loop stalls | bridge MQTT/device work unaffected (own tokio threads); only *observation* of shutdown completion is deferred until the loop runs again — the Rust cleanup still executes | bridge unaffected (own threads, GIL released) |
| Cancellation / lifecycle | `await` / cancel on the loop; `run()` exceptions propagate directly to the supervisor task | manual: `stop()` + `thread.join` |
| Integration surface | one paradigm; depends on `pyo3-async-runtimes` init/teardown | manager must own a thread plus a join barrier |

The deciding factors, in order:

1. **One concurrency paradigm.** The manager is asyncio end to end; an
   `asyncio.Task` needs no thread, no cross-thread `join`, and no lock to
   guard the live-server reference — the supervisor's `run()` and `stop()`
   run on the same loop and never preempt each other mid-statement.
2. **Direct failure routing.** An awaited task surfaces an unexpected
   `run()` exception straight to the supervisor on the loop instead of
   dropping it inside a daemon thread. `_EmbeddedBridgeSupervisor`
   ([../src/rustuya_manager/cli.py](../src/rustuya_manager/cli.py)) catches
   it there, logs it, backs off, and respawns under a rate limit — and a
   future "manager fails closed if the bridge dies" feature would already
   have the exception in hand on the right thread.
3. **No per-bridge runtime footprint.** `start()` would build a full
   multi-threaded tokio runtime for the one embedded bridge; the async
   route shares the single `pyo3-async-runtimes` runtime.

What we give up by **not** hosting on a thread is minor for one long-lived
bridge: a fully isolated dedicated runtime, independence from
`pyo3-async-runtimes`' init/teardown, and the property that shutdown
*observation* never waits on loop health (the Rust cleanup runs regardless;
only our awaiting of it waits). The bridge internals name one host profile
where those would matter — "a host that must run CPU-bound sync work on its
loop is better served by `.start()` or the binary" — but the manager is
I/O-bound (FastAPI + aiomqtt), so it is not that host. Embedding *many*
bridges, or short-lived ones cycling fast, would also still favour the async
route, not the thread.

**Why this mirrors the manager's primary model.** The embedded bridge is the
*secondary* mode. The manager's default role is to manage a `rustuya-bridge`
running as a **separate process**, reached only over MQTT — no shared memory,
no shared runtime. Embedding folds that arrangement into one process without
changing the channel: the manager still reaches the bridge through the
broker, never via shared Python state. That is also what makes mixing a
foreign tokio runtime into an asyncio app safe here — the hazard the
heuristic guards against, a loop and worker threads racing over shared
mutable Python state, has no shared state to race over. The broker is
process-external and serialises every exchange.

### 1.3 Shutdown: `no_signals=True` + `stop()`

Running the bridge as a supervised task still needs a deterministic
shutdown across two runtimes — the manager's asyncio loop and the bridge's
tokio runtime. Two rules make it so:

**The manager owns signals.** [run](../src/rustuya_manager/cli.py) installs
the process's SIGINT/SIGTERM handlers (`loop.add_signal_handler` →
`stop_event.set`). The embedded bridge is therefore spawned with
`no_signals=True` so it does *not* install its own handlers — two signal
handlers competing for the same signal in one process is a race. A signal
now flows down exactly one path:

```
SIGINT  →  manager loop handler  →  stop_event.set()
        →  run() unblocks  →  finally:  _close_embedded_bridge(supervisor)
```

**`stop()`, not `close()`.** [_close_embedded_bridge](../src/rustuya_manager/cli.py)
calls `supervisor.stop()`, which in turn trips the live server's
`stop()` — the binding's sync, lock-free cancel (added in
pyrustuyabridge 0.2.0rc5). It trips the bridge's internal
`CancellationToken`, which lives *outside* the `BridgeServer` mutex, so
`run()` observes the cancel, returns, and performs graceful MQTT cleanup
(retained-config clear + state flush) on its way out. The caller's
`await asyncio.wait_for(task, timeout=5)` is the barrier that waits for that
to finish — and cancels a wedged cleanup rather than hanging shutdown.

This replaced an earlier `server.close()` followed by a fixed
`asyncio.sleep(0.1)`. That arrangement contained a subtle defect:
`close()` required the `BridgeServer` mutex that a running `run()` holds
for its entire duration. With an OS signal the bridge's own
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
indistinguishable on the bridge side: both leave `start_async()` resolving
without an exception. The manager-side supervisor resolves the ambiguity
with its own `asyncio.Event`:

- Bridge cancels its token (reconfigure, or any clean self-termination)
  → `start_async()` resolves → supervisor sees its stop-Event clear →
  constructs a fresh `PyBridgeServer` and loops.
- Manager calls `supervisor.stop()` → sets stop-Event AND trips the live
  server's token → `start_async()` resolves → supervisor sees stop-Event
  set → exits the loop.

Crashes (`start_async()` raises) take a third path: log the traceback, wait
`_CRASH_BACKOFF_SEC` on the same Event via `wait_for` (so a stop request
during backoff returns immediately rather than always waiting the full
window), then
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
restart cost is bounded: a fresh `PyBridgeServer` setup + retained-config
re-subscribe (the shared `pyo3-async-runtimes` tokio runtime is *not*
rebuilt per restart, unlike the old thread route's per-server runtime), on
the order of a few hundred milliseconds, which is the implicit budget any
user of `reconfigure` already accepts.
