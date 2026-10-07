# Host observation exports

<!-- Generated-By: OpenCode / deepseek-v4.1-flash -->

The patched llama-swap writes the request peer address as `client_ip` in
`activity.metadata_json`. The scheduler's usage report maps those addresses to
container names with a small JSON file that this host-side export regenerates
every couple of minutes:

```json
{"generated_at": 1700000000, "containers": {"10.0.0.10": "llmsvc"}}
```

Keeping the export on the host (and the map read-only inside the container)
means the container never needs access to the LXD socket.

## Install the timer

Copy the script and units to the host, then enable the timer:

```bash
sudo install -d /usr/local/lib/llmsvc
sudo install -m 0755 llmsvc-export-ip-containers.sh /usr/local/lib/llmsvc/
sudo install -m 0644 llmsvc-export-ip-containers.service /etc/systemd/system/
sudo install -m 0644 llmsvc-export-ip-containers.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llmsvc-export-ip-containers.timer
```

The script writes `/var/lib/llmsvc-host-export/ip-containers.json` atomically
(a same-directory `.tmp` file, then `mv -f`). The export directory must exist on
the host before the first run.

## Expose the directory to the container

Mount the **directory**, not the file: the atomic rename replaces the inode, so
a file bind would pin the container to a stale copy.

```bash
lxc config device add llmsvc llmsvc-host-export disk \
  source=/var/lib/llmsvc-host-export \
  path=/var/lib/llmsvc-host/export \
  readonly=true
```

## Point the scheduler at it

```yaml
collectors:
  ip_containers_path: /var/lib/llmsvc-host/export/ip-containers.json
```

Optional companions in the same `collectors` block:

```yaml
  # Peers that mean "the host machine"; default is [127.0.0.1, ::1].
  host_ips: ["127.0.0.1", "::1"]
  # Timezone for by=day bucketing; default UTC.
  usage_timezone: UTC
```

A missing, unreadable or invalid export is treated as an empty map: the report
stays available and its `attribution.map_source` says `config`, `file`,
`config+file` or `none`. Static `ip_containers` entries win over the file for
the same IP.

## Inference fleet scanner (#306)

`llmsvc-fleet-scan.py` is a standalone Python 3.10 program with only standard
library imports. On the host it discovers vLLM, ollama, sglang and llama-server
API processes, resolves LXC ownership from cgroups, and aggregates GPU memory
from descendant processes such as `VLLM::EngineCore`. Unmatched GPU workloads
export container, PID, GPU, memory, a bounded `comm` and verified host ownership
when available. The scanner has no
workload control operations. It queries vLLM `/metrics` and ollama `/api/ps`;
activity for the other engines remains unknown. Engine version is unknown when
there is no verified version source.

The root oneshot service writes `fleet.json` beside `ip-containers.json` with
a same-directory temporary file, mode 0644 and atomic rename. Reuse the read-only
directory bind shown above. The consumer reads
`/var/lib/llmsvc-host/export/fleet.json`; enable fleet ingestion separately in
the scheduler. Installation does not modify a container, its mounts, llama-swap
configuration, workload units or any existing IP exporter.

### Configuration and bounded observations

`fleet-scan.example.json` is valid JSON. Its `_comments` object documents each
setting; the scanner ignores that object and `_generated_by`. Ordinary JSON
comments such as `//` are unsupported. Copy the example to a separate file,
adjust absolute executable/output/proc paths and exact-token `match_rules`, and
pass that file with installer `--config`. Unknown settings or attempts to raise
hard bounds fail config validation. Existing valid configuration is preserved
when no `--config` is supplied.

The default timer starts after 60 seconds, repeats at 60 seconds and uses
`AccuracySec=5s`. The installer sets its repeat period from
`sample_interval_seconds`, which accepts 30 through 300 seconds; reinstall
through the rollback path when changing that value. Timing is nominal rather
than a promise that every sample completes at exactly that interval.

Discovery, proc reads, process-tree traversal, subprocess waits and parsing
share `scan_budget_seconds`, default 20 seconds and configurable from 0.1
through 120 seconds. Each GPU inventory/compute query
uses `gpu_query_timeout_seconds`, default five seconds and configurable from
0.01 through 30 seconds, capped by the remaining scan budget. Target HTTP and
listener commands retain the `target_timeout_seconds` maximum of two seconds.
When raising the scan budget, set the scanner service's `TimeoutStartSec` above
that budget with room for cleanup and publication; the supplied unit remains
22 seconds for the default configuration. A failed command kills only the
scanner's own query/helper child and waits at most 0.25 seconds for it to exit.
An uninterruptible child can outlast that wait; its result remains unavailable,
and it remains confined to the scanner service's cgroup for cleanup.
Responses are limited to 4 MiB;
the final JSON is limited to 2 MiB. Limits also cover processes, socket FDs,
services, GPU rows, proc bytes and metric line/label lengths. Remaining targets
are marked `scrape_skipped`. Discovery accepts up to 256 KiB and 4096 arguments
per command line, within the global 64 MiB proc-byte and configured scan budgets.
Exceeding either command-line limit leaves inventory incomplete; exported
`argv_redacted` remains limited to 1024 characters. `max_services` accepts
1 through 256 and defaults to 256. Exceeding this cap adds `service_limit` and sets
`inventory_complete=false`; omitted instances remain unknown and must not be
ended by the consumer. Service start timestamps require the canonical `btime`
from `/proc/stat`; an unavailable or invalid value fails the scan with
`boot_time_unavailable`. A complete scan/export failure, including an oversized
snapshot, returns nonzero and retains the previous export. Its old timestamp
allows the consumer to identify stale data.

Only the target's network namespace is entered. The scanner pins an open
namespace FD, checks process start ticks and cgroup, verifies that the target
owns the listener socket, and repeats those checks after the GET. Responses
from changed identities are discarded. URLs use a verified numeric listener
address and fixed read-only path. Requests use GET, disable environment proxies
and reject every redirect. The helper stays in the host mount/PID/user
namespaces. Process argv is redacted before export; HTTP bodies and exception
texts never enter error messages. The service's outer timeout also cleans up
its own command/helper processes.

For Ollama, a configured wildcard bind may use either IPv4 or IPv6 in the
process-owned socket table. With a known configured port, the scanner accepts
one owned wildcard listener across these families and connects through that
listener's actual numeric loopback address. Multiple matching listeners,
different ports, and mismatched specific addresses remain unavailable. A
successful `/api/ps` response with an empty model list means no models are loaded;
it does not supply request, in-flight or token counters.

The schema is version 1 and includes explicit unknown/completeness markers:

| Field | Meaning |
|---|---|
| `inventory_complete` | All eligible proc entries were examined within limits. Missing instances may only be ended when this is true. |
| `gpu_inventory_complete` | The full global GPU query succeeded. Failure produces `gpus: []`, `host.gpu_count: null` and an error. |
| `gpu_attribution_complete` | Compute-app enumeration and checked process/tree ownership were complete. |
| Service `gpu_observation_complete` | The service's GPU assignment is a complete observation. An empty list with false is unknown. |
| Service `listener_observation_complete` | The numeric bind/port passed process, namespace and socket-ownership checks. Activity for unsupported engines remains unknown. |
| Service `listener_ipv6_only` | An exact-inode kernel socket diagnostic for an IPv6 wildcard listener. False permits an IPv4 URI; null leaves address-family support unknown. |
| `sample_interval_seconds` | Nominal host sampling interval, independent of the consumer's read cadence. |
| Service `metrics_series_id` | Fingerprint of counter series identities; a change invalidates consumer counter baselines. |
| Other process `host` | True only with matching PID/user/mount namespace identities against host `/proc/1`, a validated real UID and stable process checks; false for a resolved LXC container; null when unverified. |
| `host_uid`, `host_user` | Optional verified host owner metadata. Numeric UID identifies the owner; the sanitized username is a display label of at most 128 characters and may remain null. |

Host services retain `container: null, host: true`; unknown GPU ownership retains
`container: null, comm: "unknown"`. A null container alone does not establish
host ownership. The scanner rechecks PID/start ticks, cgroup, real UID and
namespace identities around host identification. Services retain their existing
boolean `host` field for compatibility and gain UID/user metadata only after
positive verification. Username lookup reads the configured `host_passwd_path`
(default `/etc/passwd`) as a regular file without following a final symlink,
at most once per scan and at most 64 KiB within the shared byte/time budget.
It performs no NSS lookup. Missing, invalid or ambiguous passwd entries retain
the verified numeric UID with a null username. A raced or inaccessible compute
PID retains known GPU memory as an unknown occupant and marks attribution
incomplete. Missing model/version values remain null.

A short-lived process that exits during discovery is skipped once its PID
directory is confirmed absent from the same accessible proc root. Live,
unreadable or identity-changing PIDs keep inventory incomplete. A disappeared
PID still listed by NVIDIA retains its measured memory with unknown ownership.
The export uses counters from the metric whitelist, sums counter series, takes
the latest `*_created` timestamp to detect worker resets, and averages bounded
KV-cache usage gauges. Conflicting sleep-state gauges remain unknown. It does
not calculate idle state, historical deltas, claims or policy decisions.
`tests/fixtures/host_fleet_snapshot.json` is a synthetic producer-generated
example with host/container/unknown ownership and an ollama model unknown.

The consumer's `/v1/fleet` service metadata includes an HTTP base URI in
`api_address`, `api_access` (`shared`, `local_only`, `direct` or `unknown`) and
`idle_time_sensitive`. A verified wildcard listener (`0.0.0.0` or `::`) defaults
to Shared and is exempt from idle-time over-limit display. Its URI uses a fresh
same-family address from `collectors.ip_containers_path` for that container.
Multiple valid addresses use stable numeric order. For an IPv6 wildcard, the
scanner queries `NETLINK_SOCK_DIAG` in the same pinned network namespace and
matches the returned LISTEN socket bind, port and inode. A known false
`INET_DIAG_SKV6ONLY` permits a trusted IPv4 URI too; a true value only permits
IPv6. The existing IP exporter currently supplies IPv4. The diagnostic requires
Linux IPv6 socket-diagnostic support and permission to enter the target network
namespace; it does not clone workload FDs or enter another namespace type.
Permission failures, timeouts, mismatched sockets and missing attributes retain
null; they never imply dual-stack support. The extra helper is bounded to
250 ms within the existing scan budget. Unknown or stale addresses leave the
URI null.

For host wildcard listeners, add the host's known nonloopback addresses to the
consumer's existing `collectors.host_ips`; the first usable same-family address
is selected. The default loopback-only list does not invent a host address.
Loopback listeners retain a local HTTP URI and `local_only` access; a specific
nonloopback listener supplies its observed address with `direct` access. IPv6
URIs use brackets. Fresh listener evidence is independent of activity support,
and stale or unverified listeners expose `unknown` access with a null URI.
These metadata and idle display rules do not control workload processes.

### Inspect, install and remove

First validate configuration and inspect a staged installation. These commands
do not activate a timer, enter a workload namespace or scan real host processes:

```bash
python3 -I -B llmsvc-fleet-scan.py --config fleet-scan.example.json --check-config
./install-fleet-scan.sh --root /tmp/llmsvc-fleet-stage --dry-run
./install-fleet-scan.sh --root /tmp/llmsvc-fleet-stage
./uninstall-fleet-scan.sh --root /tmp/llmsvc-fleet-stage
```

`--root` stages host-relative paths under the chosen absolute directory and
makes no systemctl calls. It retains runtime paths in the staged files; it is
an installation fixture for inspection. `--dry-run` reads and reports but
creates/writes nothing. Install paths reject symlinks and parent traversal.

After authorization for this specific host installation, run from this
directory with the reviewed configuration:

```bash
sudo ./install-fleet-scan.sh --config /path/to/reviewed-fleet-scan.json
sudo systemctl status llmsvc-fleet-scan.timer
sudo journalctl -t llmsvc-fleet-scan
```

Live installation persists a recovery receipt, installs the scanner/config/
units, runs `daemon-reload`, then `enable --now llmsvc-fleet-scan.timer`.
The only service state it changes belongs to this scanner. Receipt backups
include exact prior/installed file bytes and modes and prior timer enabled/active
state. The private receipt uses schema 2; the exported fleet snapshot remains
schema 1. File phases and submitted/acknowledged systemctl actions are persisted
with file and directory fsync. An installation lock serializes receipt changes.
An install failure restores registered files when external actions are settled.
To restore a prior installation or remove a first
installation, use either bounded path:

```bash
sudo python3 -I -B fleet-scan-admin.py rollback
sudo ./uninstall-fleet-scan.sh
```

Rollback/uninstall verifies registered bytes and modes, disables/stops this timer and its
own scan service, restores previous files and timer state, and removes its
receipt. A failed file restoration can be retried: a completed restoration is
recognized from its exact registered bytes/mode, and remaining file phases
resume. Unknown edits after installation or during restoration are preserved by
refusing to overwrite them. Save those edits and restore the registered bytes
before rollback. Exports
and the IP map are retained, and shared directories are left in place. A prior
receipt must be rolled back before installing a different scanner revision.
Keep this administration script or checkout available for rollback.

Systemctl queries require successful, complete property output, the exact unit
ID and fragment path, no drop-ins, and known state values. Errors and unsupported
observations never count as an absent/inactive unit. A command is marked
submitted before execution and acknowledged only after successful return; an
uncertain command keeps its receipt and blocks automatic resubmission or file
restoration. The tool provides no force-clear path for an uncertain external
action. Unsupported receipt versions also fail without guessing prior state.

Verification uses fake proc/GPU data and temporary local HTTP servers. It does
not establish real-host inventory completeness, production enablement or
long-term sampling reliability.

<!-- Generated-By: Codex / gpt-6.1-sol -->
<!-- Generated-By: Codex / unknown model -->
