# Standalone fleet observer

The observer reports independently operated LLM services and GPU jobs. It reads
the existing host scanner and container-IP exports, ingests them into the fleet
history database, and serves a read-only HTTP API. It does not construct or
import the scheduler, shared collectors, intent store, native adapters or model
action controllers. Service owners and administrators coordinate changes to
their own workloads.

## Configuration and entrypoint

Use the dedicated JSON example at
[deploy/fleet-observer/fleet-observer.example.json](../deploy/fleet-observer/fleet-observer.example.json).
Validate configuration before starting the observer:

```sh
python3 -m llmsvc.fleet.observer --config /absolute/fleet-observer.json --check-config
python3 -m llmsvc.fleet.observer --config /absolute/fleet-observer.json
```

The installed command is `llmsvc-fleet-observer`. Python 3.10's standard library
is sufficient for the backend. `--check-config` reads only the bounded private
configuration file; it creates no database, runtime, directories, locks or
observation artifacts.

The strict flat JSON format requires `listen_host`, `listen_port`,
`fleet_snapshot_path`, `fleet_db_path` and `ip_containers_path`. Paths must be
absolute and point to distinct files. The listener requires a specific private,
loopback or carrier-grade NAT IP address, rather than a wildcard address. Host
scanner and IP files must be regular trusted exports without group/other write
permission. `_generated_by` and `_comments` are optional metadata; unknown keys,
including scheduler and claim switches, are rejected.

| Optional setting | Default | Meaning |
|---|---|---|
| `host_ips` | `[]` | Advertised host service addresses, in preferred order. |
| `fleet_ingest_interval_seconds` | 30 | Poll the existing scanner export; repeated snapshots are ignored. |
| `fleet_stale_after_seconds` | 180 | Older observations and identity mappings become unknown. |
| `fleet_active_window_seconds` | 900 | Recent observed activity window. |
| `fleet_idle_limit_hours` | 6 | Informational reminder for eligible inactive services. |
| `fleet_raw_retention_days` | 14 | Minute-sample retention, preserving existing policy. |
| `fleet_hourly_retention_days` | 180 | Hourly retention; must cover raw retention. |
| `event_history_size` | 1000 | Notification buffer, bounded to 1–10000 events. |
| `event_heartbeat_seconds` | 15 | SSE heartbeat interval. |
| `request_timeout_seconds` | 10 | Accepted socket timeout. |

The observer writes only its history state. Production installation gives its
nonlogin service account read access to exports and a dedicated writable state
directory. The runtime uses a private umask. The existing archive worker remains
separate, with a read-only source database and a private writable archive
directory; see [FLEET_SESSION_LOGS.md](FLEET_SESSION_LOGS.md).

## Read API

Only these routes are served:

| Route | Result |
|---|---|
| `GET /v1/fleet` | Schema-1 services, GPU measurements/ownership, freshness, direct service addresses and an `observer_incarnation`. |
| `GET /v1/fleet?mine=1` | Filter by the actual socket peer's freshly mapped container, including its generic GPU jobs. |
| `GET /v1/fleet/history?service=ID&hours=24` | Existing minute samples, preserving nulls, gaps and reset flags. |
| `GET /v1/fleet/history?service=ID&hours=168` | Existing hourly history with actual partial-window coverage. |
| `GET /v1/events` | Bounded observation notifications and heartbeats. |

Every non-GET verb returns 405 with `Allow: GET`. Scheduling, placement, model,
registry, native and claim routes return 404 for GET. Forwarded headers never
specify the owner for `mine`; a missing, stale or ambiguous IP export returns
403 for that filter. Host UID attribution remains observation metadata and does
not grant caller identity or process actions.

Schema 1 retains `claims_enabled: false`, `claim: null` for every service and
`llmsvc_shared.models_loaded: []`. Historical claim rows remain in SQLite but
are excluded from classification and cannot change idle, active, over-limit or
unknown status. Wildcard sharing and inactivity reminders remain informational.
Missing observations, counters and coverage keep their existing unknown/null
semantics. Generic GPU jobs have ownership and memory observations, without
invented LLM activity or tokens.

## Notifications and reconnect

Each observer process generates a 32-character lowercase hexadecimal
`observer_incarnation`. SSE responses expose it in `X-Observer-Incarnation`,
and every event carries the same field. Regular event records retain `id`,
`timestamp`, `kind`, `model` and `detail`. `fleet_status_changed` describes an
observed status transition; `fleet_snapshot_changed` requests a refresh after
new source observations or freshness/error changes.

Reconnect using `GET /v1/events?since=N&incarnation=INCARNATION`.
`Last-Event-ID` is accepted as the cursor; if both forms are present, they must
agree. Cursors are nonnegative integers no larger than `2^63-1`.
The server sends `cursor_reset` with `id: 0` before replaying its current bounded
buffer when an incarnation changes, a cursor is ahead, its replay window has
expired, or a positive cursor is supplied without an incarnation. Its
`detail.reason` identifies the condition. Treat this event as a reset before
normal ID filtering: discard queued old events, clear the old cursor, adopt the
new incarnation and refresh the fleet view. Heartbeat comments do not advance
the cursor. Closing the observer wakes waiting streams.

SSE is a refresh hint, rather than a durable usage or lossless audit stream.
Clients keep their existing periodic GET refresh and polling fallback when a
subscription is unavailable. Durable minute/hour history and compressed usage
records remain the evidence sources.

## History continuity

Transfer the authoritative database with a SQLite-consistent backup, including
committed WAL data, after pausing its former writer. Preserve process identities,
counter baselines, the source watermark, lifecycle and existing raw/hourly rows.
Do not reset the database when changing endpoints or restart the old writer
alongside the observer. Repeated ingestion of an already committed snapshot
creates no duplicate counts.

Move existing private archive summaries, daily JSONL and resume checkpoints
with the database's observation lineage. The archive worker and retention
semantics are unchanged. Operational retirement, transfer and rollback require
their verified unit/process inventory and operator procedure; starting this
observer alone does not stop or modify any model service or other workload.

<!-- Generated-By: Codex / gpt-6.1-sol -->
