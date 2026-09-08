# Read-only telemetry

`build_collector(config)` returns a callable `Collector`; `collect()` returns the
canonical `StateSnapshot`. The scheduler owns the 15-second cadence and must
reject stale snapshots before taking actions. `close()` cancels queued probes.
No collector invokes inference, sleep, wake, unload, stop, or configuration writes.

Example of the mapping passed by core (under the scheduler's `collectors` key):

```yaml
collectors:
  swap_url: http://127.0.0.1:8000
  activity_path: /var/lib/llama-swap/activity.sqlite
  # Must be a trusted, live host source, such as a read-only host proc bind.
  # Omit until ops provides it: container /proc/meminfo is NOT host memory.
  # host_meminfo_path: /run/host/proc/meminfo
  proc_root: /proc                   # PID cgroup identification only
  nvidia_smi: nvidia-smi
  systemctl: systemctl
  deadline: 1.8                      # Strictly less than 2 seconds
  probe_timeout: 0.5
  memory_budget_gb: 200              # Candidate; stakeholder approval pending
  host_min_available_gb: 150         # Candidate; stakeholder approval pending
  ip_containers:
    192.0.2.10: example-container
  models:
    example-model:
      daemon_url: http://127.0.0.1:8101  # Direct daemon; never /upstream
      port: 8101
      util: 0.5
      weights_gb: 12                 # Configured estimate, not measured here
      cold_start_seconds: 60
      is_default: true
```

Unknown observations remain `None`, including in-flight requests without a valid
SSE snapshot. Failed probes add source/type errors without exposing raw commands,
URLs, log data, or response bodies. Late futures are discarded; a still-running
probe is not submitted again on the next round. A bounded worker pool prevents
unbounded thread accumulation. Parsing/cgroup attribution runs inside that same
round budget. No result from a previous round is relabelled as a new observation.

GPU memory uses GiB. Managed PIDs require cgroup membership in an observed
`vllm-*.service`; matching `MainPID` alone is insufficient across namespaces.
`external_gb` conservatively includes *all unattributed used memory*, including
invisible foreign-container allocations and driver overhead. It is an upper
bound, not the sum of visible foreign PID rows. If process collection fails,
managed/external fields remain unknown. Multi-GPU CUDA selectors remain unknown
because the current placement contract supports one GPU per model.

The three DESIGN section 2 fault signals are independent fields:

- `unit_active=False`: successful unit scan confirmed inactive/failed/absent service.
  A failed unit retains `state="unknown"` and a cleanup error until core reconciles
  resources, so policy cannot treat a crashed unit as cleanly stopped.
- `swap_state="ready"` and `is_sleeping=True`: the discrepancy is preserved.
- `health_ok=False`: the daemon explicitly returned a non-200 HTTP response;
  a transport/probe failure is unknown.

Core owns the consecutive-health-failure threshold, ten-second discrepancy timer,
fault recovery and retained pin records. A polling SSE snapshot cannot establish
a continuous five-second quiet period for configuration reload. That gate needs
an independent continuous stream/tracker in the core/registry integration.

The SSE parser accepts v252 `modelStatus` and `inflight` envelopes, discards
`logData`, and rebuilds from the initial snapshot each connection. Nonempty
in-flight updates and active daemon/unit states are covered by synthetic tests;
the live capture contained stopped models and an empty in-flight snapshot.
Protocol sources: [v252 API handlers](https://github.com/mostlygeek/llama-swap/blob/v252/internal/server/api.go)
and [v252 in-flight tracker](https://github.com/mostlygeek/llama-swap/blob/v252/internal/server/inflight.go).

`ActivityReader(path, ip_containers).read()` reports completed request counts in
inclusive trailing 10-minute/hour windows and the most recent row's source.
`usage(days=7, by="container")` provides whole-period requests/input/output token
counts. `by` also accepts `ip` and `model`. Source-column and metadata-source
variants are supported. Missing token columns or invalid token values make usage
unknown; missing source information creates an explicit `unknown` source group.
The reader opens SQLite with `mode=ro`, `query_only`, a query deadline, and an
explicitly closed connection; it never migrates the data-plane database.

The live 2026-09-08 v252 database had 31,605 rows, Unix-second `ts_created`, and
`metadata_json` containing only `fifo_priority`. It had **no `src` column or source
metadata**. IP mappings cannot reconstruct those historical origins. The live
7-day counts reconciled exactly with `/api/metrics/stats`: 31,605 requests,
40,480,504 input tokens and 4,847,906 output tokens. Sanitized evidence and parser
fixtures are in `tests/fixtures/telemetry/`; no request text or raw logs are saved.

<!-- Generated-By: Codex / gpt-6-astra -->
