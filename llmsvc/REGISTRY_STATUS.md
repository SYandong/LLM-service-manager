# Read-only registry queue and recovery status

`GET /v1/registry` uses the same optional `registry` configuration as the
[model list and preview API](REGISTRY_API.md). It accepts no query, request body,
native source URL or proof. The response is:

```json
{"writes_enabled": false, "blocked_by": [], "queue": {
  "schema_version": 1, "observed_at_monotonic": 1000.0,
  "jobs": [], "pending_ids": [], "fenced": false,
  "recovery": {"status": "none", "fenced": false, "marker_valid": null,
    "candidate_file_matches": null, "candidate_generation_visible": false,
    "settlement_confirmed": null, "blocked_by": []}
}}
```

The example illustrates the queue shape; actual top-level `blocked_by` always
includes `registry_writes_disabled` and the current quiet/admission/fault
limitations. An empty queue is not permission to commit. The existing unfed
quiet source remains `inflight_stream_unknown`.

`queue` is the existing `ModelRegistry.queue_snapshot()` result. A job's
`status` is its current read-only projection, while `recorded_status` retains
the stored lifecycle state. A queued job may be shown as `blocked` or
`timed_out` without being executed, consumed or removed. `source: memory`
identifies current-process jobs. `observed_at_monotonic` and job elapsed time
are local to that process; never compare them to a wall timestamp or a prior
process's clock.

After restart an existing recovery marker may supply a job with
`source: recovery_marker`, `status: reconciliation_required`, and null
`elapsed_seconds`, `remaining_seconds` and `config_committed`. Disappeared
precommit jobs are not reconstructed. Missing jobs never imply success.
Malformed/unreadable/oversized markers remain fenced and report explicit
recovery blockers. A matching candidate digest is not adoption or settlement.
This bridge supplies no native evidence; `settlement_confirmed` remains null.

Unconfigured registry returns HTTP503 `registry_not_configured`. Unavailable
or unserializable inspection returns HTTP503 `registry_unavailable`.
Query/body misuse returns HTTP400 `invalid_request`. Readable fenced state
returns HTTP200 with the fence and unknown evidence intact. There is no HTTP
reconcile/retry/clear operation or arbitrary client-supplied verifier.

The read uses the existing action lock and owner's detached snapshot. It
starts no queue worker, validator, native HTTP reader, unit/proxy action or
expiry task and creates no jobs, events, staging files or ledger writes.
Observation producers retain their independent mutex and can update while a
read-only job precheck holds the action lock. This is not continuous-source
certification or a new #53 quiet guarantee.

Tests use actual temporary queue/config/marker fixtures behind loopback HTTP,
including restart, timeout projections and deterministic concurrent observation.
No production reload/routing/TTL/reaper change is authorized. Full #19/#20
activation remains separate; long-term stability and calibration NOT MEASURED.

<!-- Generated-By: Codex / gpt-6-astra -->
