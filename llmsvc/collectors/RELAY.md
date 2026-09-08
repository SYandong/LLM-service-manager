# Optional data-plane UI relay (#23)

`DataPlaneEventRelay` reuses the SSE transport, parsing, reconnection and socket
cleanup of `InflightSubscription`. It always uses `ordered_source=False` and
has no quiet observer. It does not start automatically or change any model,
configuration, release, or production setting.

```python
from llmsvc.collectors.relay import DataPlaneEventRelay

relay = DataPlaneEventRelay(swap_url, list(configured_models), capacity=256)
relay.start()
# Core's bounded consumer tick, outside the stream reader:
batch = relay.drain(max_events=128)
for item in batch["events"]:
    scheduler.emit(item["kind"], model=item["model"], detail=item["detail"])
if batch["dropped"]:
    scheduler.emit("data_plane_dropped", detail={
        "source": "llama-swap",
        "count": batch["dropped"],
        "reasons": batch["dropped_by_reason"],
        "upstream_loss_unknown": True,
    })
# On shutdown, core calls close(), then drains any remaining diagnostics.
relay.close()
```

This is a consumer example, not an installed daemon hook. Core owns explicit
opt-in configuration, start/close, bounded drain scheduling, global event IDs,
and the scheduler SSE bridge. Defaults are capacity 256 (allowed 1–4096), drain
limit 128, connection timeout 10 s and reconnect delay 1 s. A drain is capped by
the buffer size. Model IDs are an explicit configured allowlist: at most 1024
entries, each at most 256 characters without control characters. Recreate the
integration when the configured model list changes; unlisted models are filtered.

For an existing core-owned subscription, attach optional
`event_buffer=DataPlaneEventBuffer(model_ids, capacity=256)` instead of opening a
second connection. Never pass `scheduler.emit` as a reader callback. Drain
releases the buffer's brief internal mutex before any external callback or
scheduler lock. The producer never waits for queue capacity. Overflow drops the
new summary and increments an out-of-band counter; a full buffer or small drain
cannot starve that report. Dedup tracks successfully queued summaries, allowing
a later repeat to recover the current observation after overflow.

`drain()` returns:

- `events`: detached FIFO records `{kind, model, detail}`.
- `dropped`: locally discarded summary offers/rejected rows or frames since the
  previous drain, distinguished by `dropped_by_reason`.
- `dropped_by_reason`: keys buffer_full/unlisted_model/invalid_event/limit_exceeded;
  missing keys mean zero.
- `upstream_loss_unknown: true`: these counters never quantify upstream loss.

| Kind | Model | Additional detail |
|---|---|---|
| `data_plane_state` | Allowlisted ID | state: starting/ready/stopping/stopped |
| `data_plane_inflight` | null | count: validated aggregate or null; operation: snapshot/upsert/remove/unknown |
| `data_plane_connection` | null | status: connecting/connected/disconnected/closed |
| `data_plane_error` | null | reason: timeout/disconnected/invalid_event/limit_exceeded/read_failed |

Every detail includes source=llama-swap, trusted_for_quiet=false, and received_at
(local Unix receipt time, not an upstream timestamp). Core preserves receipt
time; client presentation should show concise source/state summaries. Retained
items preserve local order, not a cross-host clock guarantee. Reconnection
republishes model states; unchanged states/counts are deduplicated per connection.

Only the listed fields enter the queue. Raw logData is ignored without parsing
its payload. Request bodies, IDs, headers, source IPs, display names, descriptions,
profile names and exception text are never relayed. Malformed model snapshots
report a fixed error/discard reason. Data-plane stopped means proxy state; it is
not a claim that the vLLM daemon was hard-stopped or slept.

UI summaries and drop counts are **not** policy or quiet evidence. Quiet callbacks,
unknown resets and #53 remain unchanged, including when the UI buffer is full.
The wrapper cannot opt into trusted mode. Adapter tests use CPU-only synthetic
and loopback traces. True dual-source scheduler delivery awaits core's hook and
integration fixture; real free-event latency remains ops-owned acceptance.
An adapter-only PR must not close #23. Alpha.1 is immutable; release timing is
integration-owned and no tag follows automatically from a merge.

<!-- Generated-By: Codex / gpt-6-astra -->
