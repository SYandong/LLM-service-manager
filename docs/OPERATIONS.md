# Operations

The scheduler is being introduced alongside llama-swap. The canonical gates are
in [DESIGN §7](DESIGN.md#7-部署与验证) and [ROADMAP](ROADMAP.md). Installation
and observation do not authorize a production policy or routing change.

## Staged installation (#8)

`deploy/install.sh`, `uninstall.sh`, and `rollback.sh` are Python 3.10 wrappers.
Each requires an explicit `--root` and `--settings`; `--dry-run` prints a plan
without creating a venv, files, leases, or services. JSON action messages go to
stdout; actual `/` operations also use the system journal via syslog.

Edit `deploy/deployment.example.json` for the target paths and service user. The
installer creates a fresh venv, installs the project, copies the standalone CLI
to the shared location, and writes a journal-enabled restart-on-failure unit.
The unit always invokes the scheduler with `--dry-run`; it is **not activated**
by installation. Start from `deploy/scheduler.example.yaml`, using the #6 configuration interface
and an alternate bind port (DESIGN uses 8011). Installation validates it with the
installed scheduler `--check-config`. Empty collectors report unknown; configure
the integrated #4/#5 adapter before counting observation acceptance.

The complete existing llama-swap configuration, launch script, and reaper script
are copied byte-for-byte to the private installation backup directory. The
manifest maps each original path to its saved copy and SHA-256, including every
model-level `ttl`; it is the full pre-scheduler backup described in DESIGN.
Missing backup inputs, existing installation destinations, overlapping paths,
and symlink paths fail before installing. Do not copy credentials into a public
fixture or commit the generated backups.

For a disposable rehearsal, create a directory outside the source tree and put
sanitized baseline files at the configured backup paths below that root:

```sh
mkdir /tmp/llmsvc-stage
# Populate baseline fixture paths under /tmp/llmsvc-stage first.
deploy/install.sh --settings /tmp/deployment.json --root /tmp/llmsvc-stage \
  --source /path/to/integrated/source --config /tmp/scheduler.yaml --dry-run
deploy/install.sh --settings /tmp/deployment.json --root /tmp/llmsvc-stage \
  --source /path/to/integrated/source --config /tmp/scheduler.yaml
# Optional offline installation: add --wheelhouse /path/to/prepared/wheels.
deploy/rollback.sh --settings /tmp/deployment.json --root /tmp/llmsvc-stage --dry-run
deploy/rollback.sh --settings /tmp/deployment.json --root /tmp/llmsvc-stage
deploy/uninstall.sh --settings /tmp/deployment.json --root /tmp/llmsvc-stage
```

Staging paths are a rehearsal, not a relocatable venv to copy into production.
Use the target interpreter in `settings.python`. Installation should run as the
configured service user; when a privileged installer uses a different user,
review file ownership and access before activation. The example uses root,
matching the existing container service administration model.

The rollback command restores saved bytes only in a staging root, without any
host `systemctl`, reload, or GPU operation. It prints the production rollback
sequence but deliberately rejects applying it to `/` until the live quiet-period
and protection checks have been integrated. Live uninstall similarly requires a
reviewed archive/rollback handoff. Uninstall refuses changed managed files and
nonempty state directories rather than deleting observations or persistent pin
records. These limits are explicit outstanding #8 integration work.

## Observation and evidence

`deploy/capture.py` takes configurable, bounded, read-only probes and writes
private timestamped snapshots plus SHA-256 manifests. A failed or timed-out
source is recorded as unavailable, never as an empty/idle machine. Configured
command argv is trusted administrator input; only use read-only commands.
Raw snapshots can contain model paths, IPs, and process details: keep them
private and use the telemetry lane's sanitized exporter for public fixtures.

```sh
python3 deploy/capture.py --help
# Copy/edit capture.example.json for the observation endpoint and local probes.
python3 deploy/capture.py --config /path/to/capture.json \
  --output-dir /path/to/private/observations --dry-run
python3 deploy/capture.py --config /path/to/capture.json \
  --output-dir /path/to/private/observations
```

Record start/end timestamps, interval, missing samples, source failures,
scheduler commit/config hashes, and readiness results. One capture does not
satisfy the one-week requirement. A full day on the alternate port with actions
disabled is required **before enabling any production actions**. The one-week
M1 dataset supplies the shared-GPU threshold decision for #16.

Verified on 2026-09-08: a Python 3.10.12 venv installed the #6 scheduler at
`cad2958`, validated its YAML, restored complete baseline bytes (including model
TTL), and uninstalled with only the original three baseline files remaining.
A bounded loopback HTTP smoke returned 200 from `/v1/state`, with read-only mode
and `collectors_not_configured` explicit; its own process was then terminated.
No systemd service, GPU workload, or production setting was changed.
Eight deployment regression tests cover rollback, dry-run, failed installation,
modified state/files, symlinks, missing backups, and tampered manifests.

Initial read-only container/host evidence on that date showed the original
`globalTTL: 600`, no per-model TTL/concurrency override, an active reaper timer,
and no managed model units. One GPU had an external engine using about 130 GiB;
the other three were near-empty. This is historical evidence, not permission to
assume the next test window is idle.

## Production transitions and rollback (#11–#14)

These are pending runbook gates, not changes applied by this PR:

1. Collect the full-day alternate-port dry-run evidence and current green tests
   for protection, memory admission, lease accounting, and failure recovery.
2. Preserve the complete baseline config/scripts and record existing timer/unit
   enablement. Obtain the specific production cutover authority in the issue.
3. #11: install the pin-aware launcher guard before exposing durable pin intent;
   move `globalTTL` to zero only with scheduler fixed ten-minute protected idle
   sleep ready. Inspect every model TTL; `ttl: 0` means never unload, while `-1`
   inherits the global value. The old launcher's dry-run is unsafe as a preview:
   its guard is after eviction, so never invoke it to prove zero mutation.
4. #12: disable the old reaper timer only when scheduler memory-policy tests and
   agreed budget thresholds are ready. Do not leave simultaneous policy writers.
5. #13: prepare per-model `concurrencyLimit: 64` as a candidate, validate the
   complete config, and use the quiet-period queue to apply it. Acceptance is a
   measured 32-concurrent-request run without 429 responses; a config diff or
   mocked response cannot satisfy it.
6. #14: retain the original launcher for rollback. Adopt the thin launcher only
   after the place/confirm/release contract and concurrent accounting tests are
   integrated. Measure cold-start latency against the retained baseline.

Production rollback order remains DESIGN §7: stop/disable scheduler and its
associated timers; restore original launch/reaper and enable the reaper timer;
validate the complete saved llama-swap configuration, then restore/reload only
at the quiet-period gate (zero in-flight continuously for five seconds, no
awake pinned model, batch sleep-memory admission). Inspect the gap between the
last check and reload and record any interrupted requests. Reconcile units with
`/running`, then observe the original idle timeout. Each step must be recorded
and independently repeatable; stopping only the scheduler while TTL is zero is
an incomplete rollback.

## GPU smoke ownership

Only ops schedules live GPU tests and holds the shared `gpu-test.lock` for the
whole test. Read fresh host and container GPU process/memory/utilization data,
managed units, and in-flight evidence immediately before starting. Container
PID visibility can hide a host process; an empty container process list alone
is insufficient. Unknown in-flight status blocks a launch.

Use a cached small model, an alternate port, a unique unit, sufficient free
memory, and a target duration of at most five minutes. Recheck before launch;
abort when another workload appears. Stop or remove only resources created by
the test. Never sleep/stop an existing model, download large weights, or change
production routing/TTL/reaper to make room. Registry supplies its LoRA fixture
and version prerequisites to ops; it does not run a competing GPU test.

## Retirement gates (#26–#28)

README/release instructions follow the integrated M5 interfaces and a fresh-user
rehearsal. Legacy code and tests remain until deployment and zero-consumer
proofs satisfy #27. For #28, identify each other-container service, its owner,
consumer routing and rollback procedure, then obtain that owner's explicit
shutdown consent. No group announcement or shutdown is authorized by a brief
idle-GPU smoke permission. A one-week absence observation needs actual
start/end records; it cannot be marked complete immediately.

<!-- Generated-By: Codex / gpt-6-astra -->
