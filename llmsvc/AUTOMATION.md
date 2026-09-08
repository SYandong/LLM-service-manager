# Automatic memory and idle/pressure policy cycles

The daemon can run the existing `plan_memory_pressure` followed by either
`plan_idle_sleep` (M2 fixed idle, the default) or `plan_pressure_sleep` (M3
per-GPU TTL and shared pressure). It is disabled by default and is separate from
writable pin/reserve intents. It does not change llama-swap `globalTTL`, stop a reaper,
provision sources or authorize deployment.

## Configuration

```yaml
read_only: true
model_actions_enabled: false
automation_enabled: false
automation_interval_seconds: 15
automation_cycle_timeout_seconds: 120
automation_policy: fixed_idle # select gpu_pressure explicitly for M3 behavior
# Used only by fixed_idle:
automation_idle_seconds: 600
# Used only by gpu_pressure; existing PolicySettings candidate defaults:
automation_exclusive_ttl_seconds: 3600
automation_shared_ttl_seconds: 300
automation_shared_external_threshold_gb: 1
automation_shared_free_threshold_gb: 10
```

Real cycles require all three conditions: explicit `automation_enabled: true`,
`model_actions_enabled: true`, and `read_only: false`, with the existing writable
state database and trusted configured model/transport sources. Merely enabling
intent writes never starts automation. `automation_policy` accepts only
`fixed_idle` or `gpu_pressure`; selecting a mode does not enable the worker.
The fixed idle value configures `PolicySettings.fixed_ttl_seconds`. In pressure
mode the four `automation_*` TTL/threshold fields configure their corresponding
existing `PolicySettings` fields; no new ranking is introduced. Thresholds are
finite non-negative GiB values, durations finite positive seconds, and a cycle's
limit is at most120s. Invalid settings fail configuration validation.
The interval begins after a cycle finishes, so slow work cannot queue overlaps.

Existing `memory_budget_gb` and `host_min_available_gb` remain the memory inputs.
Action submission is also bounded by `request_timeout_seconds`; each observation
wait uses `action_observe_seconds` within the remaining cycle deadline.

## Cycle and protection

One worker requests fresh publications from the existing sampler. It does not
hold the global action lock while waiting for collector work. A cycle first
considers current memory pressure. If memory pressure has no action or blocker,
it considers only the selected TTL policy. The two TTL planners never run in
the same cycle mode. Unresolved memory pressure is not followed by more sleeps
that would add memory pressure.

In `gpu_pressure` mode the existing exclusive GPU selection (GPU0 by default)
uses a 3600s idle TTL, and shared GPUs use 300s, including the exact boundaries.
A shared model is also eligible when a fresh observation reports any external
process, external memory at or above the configured threshold, or free memory
below its threshold. Small driver residue below the external threshold alone
is not a trigger. Missing required GPU/activity data reports unknown blockers.
The pure policy still applies inflight/pin/default protection and sleep RAM
admission; a pressure signal does not bypass these checks.

These are conservative candidate settings, not measured calibration. Enabling
this mode is a separate operator choice; it does not replace production TTL or
reaper configuration by itself. Reverting `automation_policy` to `fixed_idle`
restores the existing fixed-idle planner without changing stored intents.

Only configured models with confirmed durable accounts are automatic candidates.
Unleased or changed-unit models are explicitly blocked, not silently adopted.
The pure policies retain default/pin/inflight/unknown protection and sleep RAM
admission. Defaults are never hard-stopped; an admitted idle default may sleep
as already defined by the pure policy. Failed sleep admission leaves protected
default/pinned models awake.

Each step selects one current policy action, verifies the unit's matching
`LLMSVC_LEASE_ID`, replans after that probe, and rechecks cancellation/opt-ins
before invoking the protected dispatcher under the same action/accounting lock.
Explicit policy exclusions protect unleased, mismatched-unit or pending models
without creating synthetic pins. A pending model is guarded from competing
free/wake/placement/reserve work.
Readiness waits release that lock, so pin writes, observations and other eligible
requests can progress. A new pin or changed RAM/activity invalidates the old
plan. Pressure signals and TTL eligibility are replanned after the identity probe as well; a cleared
pressure signal cannot authorize submission from a cached action list.

A sleep needs two newer observed sleeping rounds and retains its full confirmed
account. A stop needs configured-unit exit proof, durable account release and
two newer stopped observations. A transport success or policy estimate alone
never releases an account. Concurrent reserve intents remain persisted and keep
their whole-GPU placement exclusion; an automatic sleep retains the model's full
account and does not certify reserve evacuation or relocation. Reconciliation
is limited to the explicitly stopped model; it is not orphan/fault cleanup or an adoption protocol.

After confirmed effects, take another fresh round and replan. When host headroom
is the pressured resource or the stop funds sleep admission, its measured
availability must improve before proceeding. For the sleeping-weight budget,
confirmed membership must decrease. This accounting is not reported as measured
physical release. Repeated actions, failed requests, missing effects or missing
memory progress end the cycle; confirmed earlier actions remain in the result.

Shutdown intent is serialized with final dispatch on the existing global lock.
An already submitted action can finish; no later automatic step is admitted.
The worker observes cancellation, clears its pending/active markers and is joined
before closing owned resources. Late/unconfirmed observations retain accounting.
The source event reader remains independent of cycle/consumer backpressure.

## Preview and observations

`AutomaticPolicyController.run(dry_run=True)` returns a detached
`{would, blocked_by}` plan and writes an `automation_preview` structured log.
It does not sample, probe, submit a transport, append an action event, allocate
an ID or write the database. It can be called in read-only mode.

For an operator preview, use the existing one-shot command with automation and
model transport metadata configured:

```sh
python -m llmsvc --config scheduler.yaml --once
```

`--once` forces read-only access, takes its one ordinary observation, prints the
unchanged state JSON on stdout, and logs the automation preview. A missing state
DB is not created. `--check-config` starts neither a sampler nor an automation
worker. A long-running `--dry-run` daemon starts no automatic action worker.

Runtime results/events use the existing scheduler event stream:

- `automation_action_result`: action, confirmed, error, dry_run=false.
- `automation_result`: status, confirmed actions, blocked_by, optional error,
  dry_run=false. Status can be complete, blocked, partial, failed, no_progress
  or timeout. Direct calls return disabled without work if opt-ins are missing.
- Unexpected failures log only their exception type; a cycle that failed after
  a confirmed effect retains that partial result. No `freed_gb` estimate is
  presented as a measurement.

These are scheduler-side integration results. Production TTL/reaper replacement,
actual deployment and live operational evidence remain separate owner work.
The CPU fixtures simulate pressure appearing within one configured sampling
interval and use actual loopback HTTP, fake units and temporary SQLite. They do
not measure actual live <30s GPU response or authorize production activation.
Long-term stability/calibration are NOT MEASURED. Recent-use relocation,
registry/reload and orphan/fault recovery are separate slices.

<!-- Generated-By: Codex / gpt-6-astra -->
