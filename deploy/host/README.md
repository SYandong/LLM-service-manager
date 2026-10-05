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
export only container, PID, GPU, memory and a bounded `comm`. The scanner has no
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
share a monotonic budget of at most 20 seconds. Each external command/GET uses
at most two seconds and the remaining budget. Responses are limited to 4 MiB;
the final JSON is limited to 2 MiB. Limits also cover processes, socket FDs,
services, GPU rows, proc bytes and metric line/label lengths. Remaining targets
are marked `scrape_skipped`. Discovery accepts up to 256 KiB and 4096 arguments
per command line, within the global 64 MiB proc-byte and 20-second scan budgets.
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

The schema is version 1 and includes explicit unknown/completeness markers:

| Field | Meaning |
|---|---|
| `inventory_complete` | All eligible proc entries were examined within limits. Missing instances may only be ended when this is true. |
| `gpu_inventory_complete` | The full global GPU query succeeded. Failure produces `gpus: []`, `host.gpu_count: null` and an error. |
| `gpu_attribution_complete` | Compute-app enumeration and checked process/tree ownership were complete. |
| Service `gpu_observation_complete` | The service's GPU assignment is a complete observation. An empty list with false is unknown. |
| `sample_interval_seconds` | Nominal host sampling interval, independent of the consumer's read cadence. |
| Service `metrics_series_id` | Fingerprint of counter series identities; a change invalidates consumer counter baselines. |

Host services retain `container: null, host: true`; unknown GPU ownership retains
`container: null, comm: "unknown"`. Missing model/version values remain null.
The export uses counters from the metric whitelist, sums counter series, takes
the latest `*_created` timestamp to detect worker resets, and averages bounded
KV-cache usage gauges. Conflicting sleep-state gauges remain unknown. It does
not calculate idle state, historical deltas, claims or policy decisions.
`tests/fixtures/host_fleet_snapshot.json` is a synthetic producer-generated
example with host/container/unknown ownership and an ollama model unknown.

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
