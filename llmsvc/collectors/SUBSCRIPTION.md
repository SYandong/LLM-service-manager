# Continuous inflight observations

The adapter for core/registry is separate from the 15-second state collector:

```python
from llmsvc.collectors.subscription import InflightSubscription

subscription = InflightSubscription(
    swap_url,
    on_inflight,                 # callback(count_or_none, connected=bool)
    on_heartbeat=on_heartbeat,    # callback(), only for received SSE comments
    ordered_source=False,        # Required for the currently deployed v252
)
subscription.start()
# Core must call subscription.close() during shutdown.
```

Core supplies these callbacks using its existing condition/global lock:

```python
def on_inflight(inflight, *, connected=True):
    with scheduler.changed:
        quiet.observe(inflight, connected=connected)
        scheduler.changed.notify_all()


def on_heartbeat():
    with scheduler.changed:
        quiet.heartbeat()
        scheduler.changed.notify_all()
```

Construction/start/stop never changes a model, configuration or GPU. The adapter
has one reader/observer path so callbacks preserve receive order. Every connection
starts unknown; reconnect needs a new snapshot. Malformed messages, unexpected
removals, bounds violations and disconnects invalidate quiet. Replacement
snapshots reset the quiet interval because they can follow missing history.
Frames and retained request IDs are bounded; raw log contents are discarded.
Use the direct data-plane base URL; `/upstream` model routing is rejected.
`close()` interrupts socket reads and reports an error if its worker cannot stop
within the configured timeout. Observer callbacks must finish promptly.

The default `ordered_source=False` reports observed counts with `connected=False`.
This deliberately keeps `QuietPeriod` blocked. A live TCP connection is not a
claim that every application event arrived. v252 can drop queued messages and
supplies no sequence cursor or periodic heartbeat; see the verified
[design blocker #53](https://github.com/SYandong/LLM-service-manager/issues/53).
There are no locally fabricated heartbeat timers.

`ordered_source=True` is reserved for an independently verified ordered source
with reliable gap signaling (or controlled fixtures). It is not a switch to make
the current v252 source safe. Core must not enable that setting for production
until #53 is resolved. Only received SSE comments after a valid complete snapshot
and outside an unfinished frame can refresh a trusted subscription's heartbeat.

An unknown source, stale interval or disconnection must never trigger reload.
This PR supplies the callback/lifecycle adapter and offline verification, not
live quiet-period acceptance or a production configuration change.

<!-- Generated-By: Codex / gpt-6-astra -->
