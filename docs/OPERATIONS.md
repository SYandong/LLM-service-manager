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

## Read-only runtime upgrades and host pull (#169)

`deploy/upgrade.sh` upgrades an existing installation; it does not uninstall it.
Copy/edit [upgrade.example.json](../deploy/upgrade.example.json) to match the
installation manifest and existing shared directory (`cli_path` must be that
shared directory's `llm`). Keep the file-bind trampoline and LXD profile unchanged.
The updater reads/verifies `trampoline_path` and never writes it. It changes only
the directory-side `llm`, `bin/llm-run`, owned scheduler launcher/unit and one
shared `current` pointer. Client launchers resolve that pointer once and exec a
versioned environment. Prior scheduler/TUI environments and generations remain
available; no automatic garbage collection removes running clients' imports.

A release bundle has `deployment.json`, the standalone CLI, pinned application
and dependency wheels, and a bootstrap pip wheel. Every payload is hashed; the
application wheel's script must match the standalone CLI (apart from the wheel's
normalized shebang). Installation uses `venv --without-pip`, runs pip from its
verified wheel and installs only explicit offline wheels. No ensurepip, network
fallback, source build or global package installation occurs. The optional TUI
is checked in the same new shared environment as the scheduler.

```sh
# Read-only planning: no files, lock, installer or service command is created.
deploy/upgrade.sh apply --root /path/to/disposable/root \
  --settings /path/to/upgrade.json --bundle /path/to/verified/bundle --dry-run
# Prepare/validate/switch within the disposable root; no host systemctl there.
deploy/upgrade.sh apply --root /path/to/disposable/root \
  --settings /path/to/upgrade.json --bundle /path/to/verified/bundle
# Use the exact transaction ID from the result.
deploy/upgrade.sh rollback --root /path/to/disposable/root \
  --settings /path/to/upgrade.json --transaction TRANSACTION_ID --dry-run
```

For an authorized installed-site upgrade use `--root /` after rehearsal. Both
candidate `--check-config` and `--once` must pass. Configured live host memory
must remain available. Before each cutover the updater checks fresh inference
activity; v252 omits `requests` for an empty explicit `snapshot`, which is
accepted under the existing collector contract. Missing/invalid snapshots still
block. This is a scheduler-restart guard, not a new trusted reload/quiet proof.
Only `llmsvc-scheduler.service` is restarted. Health requires a sampled read-only
state, the selected runtime in the service process, shared CLI version/status,
and unchanged active llama-swap/reaper. No inference endpoint, alias, data-plane
config, TTL/reaper, model launcher or scheduler ledger is changed.

The site YAML is operator-owned. A changed hash since initial installation is
not silently ignored or replaced: the exact current bytes are saved in the
private transaction, validated and rechecked before/after switching. The new
manifest records that observed config and retains the old manifest in history.
Unit/CLI/dispatcher and original-backup integrity still must pass. A concurrent
config/pointer edit blocks the transaction; rollback never overwrites an unknown
operator edit. Successful upgrades use manifest schema2; old install/uninstall
is not a replacement upgrade path. A failed restart/health check restores actual
previous pointer/files/config state and restarts the prior scheduler. Retained
generations are not deleted. An interrupted transaction blocks a new upgrade;
use its exact `rollback --transaction` to reconcile known before/after bytes,
never delete the journal or bypass hashes. Conflicting external edits require
explicit reconciliation before another mutation.

Host automation uses [pull.example.json](../deploy/pull.example.json) and one
`deploy/pull_release.py --settings /path/to/pull.json` invocation per poll. It
uses outbound HTTPS to the configured GitHub repository, ignores drafts,
resolves the actual tag commit, verifies release manifest/bundle checksums and
rejects archive links/traversal/unlisted payloads. It pushes a fresh versioned
input directory and invokes the existing reviewed upgrader over LXC; there is
no inbound CI runner, profile edit or second deployment owner. `--tag` selects
an explicit released tag; `--dry-run` performs no network/write/command. Successful
same-tag polls are no-ops; failed/pending attempts require operator reconciliation
rather than an unattended retry loop. Render the single host service/timer into a new review directory with
`--render-systemd /path/to/new/unit-review` (`--dry-run` writes nothing), then
install/enable only that reviewed pair. Rendering starts no service. The timer
uses the configured poll interval and the host cache lock excludes concurrent
manual invocations; do not install competing pollers.

Read-only bundles/site configuration may use the authorized automatic path.
Non-read-only configuration or model-action/placement/automation/fault/recovery
opt-ins are rejected with an approval-required error: future unattended M2
changes retain a manual review/approval gate, not a config flag that bypasses it.
During an explicit scheduler shutdown, a new mutating HTTP request may receive
HTTP 503 `scheduler_stopping`; this means no new pin/action/place/reserve,
registry or bootstrap write was admitted. Already accepted work keeps its
existing bounded deadline and reconciliation rules. Read-only state and dry-run
previews retain their existing behavior; retry mutations only after the
replacement scheduler is ready.
Release publication is integration-owned; see [RELEASING](RELEASING.md). A same-
version bootstrap rehearsal does not satisfy the separate real-tag **version
change** acceptance. Record actual staging/live success, failure, rollback and
unchanged trampoline/profile/config/backup evidence with the operation; no
calendar wait or unmeasured stability claim is required.

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
scheduler commit/config hashes and readiness results. Use bounded minutes-scale
checks and deterministic replay; there is no mandatory day/week development,
release or completion wait. Before production actions, verify the bounded
alternate-port dry-run, protection and rollback checks and obtain the specific
activation authority. Report #16 occupancy over the actual measured span;
long-term stability and threshold calibration remain **NOT MEASURED**.

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

## Preview readiness after configured-runtime integration

Merged #41 collectors and #50 previews do not make an empty collector
configuration usable. `collectors: {}` produces unknown state, and preview
responses must be checked for `blocked_by` even when HTTP status is 200.
RAM decisions require a verified live host-memory source plus known weight
budgets; cached snapshots and container cgroup meminfo are not substitutes.
Keep host availability null until the trusted-source proposal is approved and
validated. Collection errors conservatively block current previews.

The bounded configured runtime/CLI check, 0.8-second probe candidate, expected
host-RAM blocker and exact observer/host-source permission proposals are recorded
in [deployment observation preparation](../deploy/OBSERVATION.md). This evidence
advances API/CLI integration only; bounded installed-service restart/journal
and rollback verification remain separate. Long-term stability and calibration
are NOT MEASURED. No production activation is implied. Comment-preserving YAML, watcher/adoption/quiet-source design alignment
and executable production rollback must be resolved before the first #19 write
or #11 TTL transition.

The optional, default-omitted `registry` block in
[scheduler.example.yaml](../deploy/scheduler.example.yaml) mounts temporary-model
list and dry-run previews only; see [the registry API contract](../llmsvc/REGISTRY_API.md).
It enables no actual configuration writes or reload worker, and a valid preview
is not commit readiness. The configured source paths are not provisioned.

Recovery-marker inspection requires a regular file with exactly one hardlink
(`st_nlink == 1`). A backup that hardlinks the live marker keeps the queue fenced;
use separate backup copies. Do not remove the marker or treat native generation
visibility as proof of old-server settlement or permission to clear a fence.

## Production transitions and rollback (#11–#14)

These are pending runbook gates, not changes applied by this PR:

1. Collect bounded alternate-port dry-run/replay evidence and current green
   tests for protection, memory admission, lease accounting and failure recovery.
   Record actual timing, samples, gaps and failures; no calendar soak is required.
2. Preserve the complete baseline config/scripts and record existing timer/unit
   enablement. Obtain the specific production cutover authority in the issue.
3. #11: install the pin-aware launcher guard before exposing durable pin intent;
   move `globalTTL` to zero only with scheduler fixed ten-minute protected idle
   sleep ready. Inspect every model TTL; `ttl: 0` means never unload, while `-1`
   inherits the global value. The old launcher's dry-run is unsafe as a preview:
   its guard is after eviction, so never invoke it to prove zero mutation.
4. #12: disable the old reaper timer only when scheduler memory-policy tests and
   agreed budget thresholds are ready. Do not leave simultaneous policy writers.
5. #13: use the [concurrency preparation and measured fixture](../deploy/CONCURRENCY.md)
   for per-model `concurrencyLimit: 64`, exact full-config backup/candidate and
   rollback review bytes. The real pinned-swap/fake-backend fixture proves32 held
   requests complete without429 and detects default-limit rejection. Actual vLLM/
   live-serving acceptance and guarded quiet/adoption/authority remain separate;
   neither the fixture nor a config diff authorizes production application.
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

## Allocated-model configuration recovery (#93)

This procedure applies in a separately authorized control-plane maintenance
window; it requests no running-service change. If an active allocation's model
is missing from `collectors.models`, or its configured unit name changed, the
scheduler retains the complete allocation. It does not trust a unit merely
because its name appears in SQLite. `lease_model_unobserved` and
`configured_unit_mismatch` are safety blocks, not instructions to erase the
record or stop an arbitrary unit.

The core diagnostic contract under [#93](https://github.com/SYandong/LLM-service-manager/issues/93)
is `lease_configuration_required` in journal/SSE, with `model`, `lease_id`,
`persisted_unit`, `configured_unit` (null when absent), and
`next_action=restore_verified_model_configuration` and `budget_retained: true`.
Repeated notices coalesce while the same mismatch persists; restoring identity
or transitioning the allocation clears that notice, and a daemon restart can
report it again. This event requires the separately reviewed
[core #96 implementation](https://github.com/SYandong/LLM-service-manager/pull/96); the
merged #88 baseline can retain the allocation without this actionable notice.
Absence of the new event is not proof that identity or accounting is safe.

1. Retain the complete current SQLite ledger and its backup (v2, v3 after an
   actual fault claim, or v4 after an ordinary recovery claim; see both recovery
   restrictions below). Record
   the event or observed blocker, lease ID, current state and last verified model
   configuration. Do not delete an active ledger, remove SQL rows, fabricate a
   tombstone, rename the unit to bypass checks or infer stop authority from the
   error.
2. Restore the complete original trusted model entry from reviewed configuration
   or a verified backup: model name, exact unit, daemon probe URL, util, weights,
   default protection and any explicit budget. The persisted unit name identifies
   what needs verification; it is not sufficient authority to adopt that unit.
   If the identity or original metadata cannot be verified, keep the allocation
   charged and investigate through a separately approved procedure.
3. In the authorized maintenance window, restart only the scheduler with that
   restored configuration and the **same ledger**. No data-plane stop, unload or
   new revoke API is part of this recovery. The implementation does not repair
   or hot-reload the configuration automatically. Preflight configuration checks
   and previews remain read-only; they do not perform the recovery transition.
   While `globalTTL` is zero, scheduler downtime also suspends scheduler-driven
   idle sleep; the data plane does not take over that idle policy. Keep the
   separately authorized restart window short and bounded, and verify scheduler
   readiness before leaving maintenance. A prolonged outage requires the reviewed
   rollback plan, not an unplanned TTL/reaper change.
4. Check fresh collector and configured-unit observations. A healthy active unit
   with matching `LLMSVC_LEASE_ID` and GPU confirms the same allocation. A
   loading, mismatched-token or unknown unit retains its budget (stale on
   recovery). Release requires a bounded configured-unit inspection proving
   absence, or inactive/failed with `MainPID=0` and an empty control group,
   **and** a fresh collector observing that exact configured unit stopped.
   A failed probe, missing health response or `MainPID=0` alone is insufficient.
5. Confirm the existing lease's resulting status and that pin intent is intact.
   Only proven exit permits the next placement to reuse its budget. Existing
   confirm/release endpoints keep their guards: 503 means uncertainty; 409 on
   confirm means revoked/superseded. No force-revoke endpoint is introduced.

Schema v2 remains incompatible with older binaries. Preserve the pre-upgrade
backup for a separately planned rollback, but do not overwrite an active v2
ledger with an old backup: that would lose allocations created since the
backup. Reconcile live allocations before any approved rollback. This recovery
procedure provides neither automatic orphan cleanup nor permission to stop
production workloads. See the [launcher budget clarification](../deploy/LAUNCHER.md)
for the distinction between requested `lease.util` and authoritative `budget_gb`.

Core owns the diagnostic and regression implementation. Its #93 acceptance
covers restored trusted mapping with the same ledger, healthy confirmation,
proven-exit release, unknown-state retained budget, unchanged pin, subsequent
placement only after proven absence, and read-only reconciliation with no probe,
writer or recovery-event mutation. Offline fixtures establish those code paths;
they are not live recovery evidence. This documentation does not close #93,
#14 or the remaining reserve/victim/fault-recovery acceptance.

## Fault fences and ledger rollback (#130)

The [fault-recovery contract](../llmsvc/FAULTS.md) adds an independent,
default-off fault opt-in; effects also require model actions and non-read-only
operation. The first **actual fault claim** atomically migrates schema v2 to v3.
Default-off/read-only startup and dry-run do not perform this fault migration.
Older schema-v2 binaries reject v3. Restoring model metadata under #93 does not
clear a fault fence or make an older binary compatible.

Before a separately authorized rollout, retain a verified, consistent SQLite
backup of the **whole ledger**, including allocations, pin intents and any
pending fault claims. Use a SQLite-consistent backup, not a copy of only the
main database file while a writer may hold newer WAL contents. Record the schema,
runtime/configuration identity and backup digest. Preserve the current ledger
and fences through any rollback assessment. An older-binary rollback needs the
appropriate verified pre-migration backup **and** reconciliation of current
resources, lease/unit identities and pending claims. A historical backup cannot
represent later allocations or unsettled unloads: do not overwrite active
accounting with it, delete rows/claims, change `user_version` or drop fault
metadata. This version supplies no executable downgrade or SQL recovery remedy.

A persisted claim blocks conflicting operations on its model even after the
worker is disabled or restarted. Positive unit exit must precede account release;
account release does not mean proxy cleanup or model recovery completed. Pin
records remain intact. Restart does not replay an old stop against an active,
unknown or changed unit. Retain the fence and any unreleased budget when identity
or exit cannot be proved; inspection is not permission to stop a workload.

Each claim permits **at most one unload submission**, with its durable marker
written before HTTP. Submitted-but-unacknowledged work remains fenced after a
crash (including before HTTP), timeout, rejected response or late response.
Stopped snapshots alone cannot prove that request will not execute later. No
resend, time-only recovery, force-clear endpoint or manual SQL remedy is provided;
a separate positive-settlement/owner protocol is **not implemented**. A timely
2xx acknowledgment is accepted only with current identity/configuration and
valid opt-ins. An acknowledged claim may resume observation-only, requiring
two fresh post-submission stopped observations plus positive unit-exit proofs
and current identity checks before completion. HTTP 200 alone is insufficient.

These are recovery limits, not authority to upgrade/migrate/restart/stop a
service or activate an observer, production policy, TTL/reaper or host source.
Live recovery latency and long-term stability/calibration are **NOT MEASURED**;
bounded checks and deterministic replay carry no calendar-wait requirement.

## Ordinary sleeping recovery and schema-v4 rollback (#160)

`sleeping_recovery_enabled` defaults to false. When explicitly enabled, it runs
sequentially after the existing automation cycle; effects additionally require
`automation_enabled`, `model_actions_enabled` and non-read-only operation.
Recent-use relocation also requires `placement_enabled` **before source stop**.
`sleeping_recovery_timeout_seconds` defaults to 900 and must be finite, greater
than zero and at most 900: source cleanup, cold wake and destination confirmation
share this one budget. Waits release the action lock; manual wake/reserve APIs
are unchanged. Ordinary pin/default/identity/account protections still apply.

The first **actual ordinary recovery claim** atomically migrates a v2/v3 ledger
to schema v4. Ordinary claims are separate from fault claims; existing pins,
reserves, accounts and fault records are preserved. Default-off, read-only and
dry-run do not perform this migration. Older schema-v3 readers reject v4;
disabling this option is not a schema downgrade or a way to clear a stored fence.
Preserve a consistent whole-ledger backup and reconcile current resources and
all pending claims before any separately approved rollback. Do not delete
claims/rows, edit `user_version` or overwrite active accounting with a stale
backup. No executable downgrade or manual SQL recovery is provided here.

An uncertain stop, unload or wake remains fenced across restart/disable. A new
process may reconcile fresh evidence and close already-acknowledged completed
work; it cannot replay old transport or resend an unknown submission. There is
no force-clear path. Positive exit precedes source-account release, and partial
progress stays explicit; policy estimates are not measured freed bytes.
This note authorizes no rollout, restart, production schema migration or
TTL/reaper change. Live recovery latency and long-term calibration remain
**NOT MEASURED**; no calendar wait substitutes for those measurements.

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
idle-GPU smoke permission. Record actual discovery timestamps and scope.
Long-term absence/stability are NOT MEASURED; do not invent them or impose a
calendar wait. Verified consumers, owner consent and rollback still control
any retirement action.

<!-- Generated-By: Codex / gpt-6-astra -->
<!-- Generated-By: Codex / gpt-5.6-luna -->

## Watcher-only native generation evidence (#60)

The isolated CPU fixture and measured active-config/old-resource boundary are in
[`deploy/WATCHER_WITNESS.md`](../deploy/WATCHER_WITNESS.md). It uses the pinned
v252 binary, temporary config/processes and native MCP reads; it installs no
notifier or observer. New generation visibility and generic reload-completion
logs do not establish old-server settlement. Keep recovery/barrier and #53
quiet/production gates until an independent settlement contract is verified.

<!-- Generated-By: Codex / gpt-6-astra -->

### Explicit native maintenance adapter

A read-only installation without confirmed preload leases first needs the
[managed-start bootstrap](../deploy/BOOTSTRAP.md). Its native and daemon
EnvironmentFile bindings are verified separately; preserve the default preload,
existing endpoints and all ledger records. The source-off stage reports source
telemetry as unknown while core validates the narrowly authorized first account.

The same catalog worker can use the opt-in native instance transition described
in [deploy/MAINTENANCE.md](../deploy/MAINTENANCE.md). The profile pins the service,
image, configuration and backend identities, and replaces generic shutdown-log
inference with attributable helper job outcomes. Provisioning that profile,
native helper command and dedicated attempt environment file is a separate
reviewed site step; the read-only release puller does not activate it.

Provisioning the managed `cmdStop` also changes ordinary llama-swap unloads:
they invoke the same native helper, which verifies the configured backend,
requests level-1 sleep, confirms sleeping, and signals only the matching wrapper.
Outside a maintenance claim this runs directly and creates no maintenance claim
or helper job. This production unload-path change belongs in the reviewed site
transition and rollback record even when no catalog change is being submitted;
it must not be introduced implicitly by a read-only runtime update. Sleeping or
stopping the wrapper does not establish backend exit or release its lease budget.

Retain the source-owned start/stop/helper records and exact job units while any
claim is pending. Never reset/delete failed helper records or restore a stale
ledger to force rollback. An absent PID after an unknown submitted start is not
proof that no candidate ran. Known pre-command refusal and positively bound
new/base instances have separate recovery paths. A configuration rollback keeps
current account releases; it does not recreate a removed model's old lease.

<!-- Generated-By: Codex / gpt-6-astra -->

<!-- Generated-By: Codex / gpt-5.6-luna -->
