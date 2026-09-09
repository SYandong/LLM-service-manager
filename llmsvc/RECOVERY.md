# Ordinary sleeping recovery

This opt-in executor consumes the existing pure `plan_sleeping_recovery` and
`plan_relocation` policies. It is separate from proven-fault cleanup and never
inherits its pin/default/inflight exception. It does not change registry files,
reload configuration or provide a proof/reconcile/force-clear HTTP endpoint.

## Configuration and scheduling

```yaml
sleeping_recovery_enabled: false
sleeping_recovery_timeout_seconds: 900
```

The timeout is finite, positive and at most 900 seconds. Actual work additionally
requires `automation_enabled`, `model_actions_enabled`, a writable intent store
and `read_only: false`. Recent-use relocation requires `placement_enabled`
before stopping the source. Pin-only intent mode never enables this executor.

The existing automation worker runs its memory/idle/pressure cycle first, then
one sleeping-recovery operation. Its cadence follows completion. Concurrent
calls to the same recovery controller cannot reset the active deadline or start
a second source stop. Automatic policy and recovery controllers refuse overlap.
The sampler, intent requests and lease confirmations can progress during waits.
Shutdown prevents later submissions and waits within the active operation's
remaining bound; an already submitted remote operation can remain uncertain.

Recovery uses a direct literal-IP HTTP(S) proxy origin. Hostname-only, scoped-IP
or invalid-port origins are reported as `unsupported_recovery_origin` before
source actions. Prepared requests freeze the approved origin/path and use an
absolute socket watchdog for connect/TLS/headers, including header trickles.
They do not use a resolver, environment proxy or redirects. Existing standalone
manual wake/free transport compatibility is unchanged. Python callbacks and
unresponsive local storage cannot be made preemptible by these network bounds;
late results never authorize further effects or acknowledgement.

## Selection and admission

A known unused sleeper can be retired. A recently used sleeper needs a feasible
replacement on a different GPU before any source action. Source and victim
eligibility excludes unconfigured or changed units, missing confirmed accounts,
active operations, fault/recovery fences and unknown signals; ordinary
pin/default/inflight protection remains in force.

Core supplies `replacement_requests` on both pure recovery entry points. These
stopped request descriptions use current configured and durable-lease util /
budget floors plus configured cold-start weights. They do not replace observed
source accounting or manufacture a pressure trigger. The preflight subtracts
the replacement and pending/stale-start weights from the hypothetical
post-source-stop host memory. Missing profile/weights or an impossible GPU/RAM
admission yields zero source actions. Actual admission later uses fresh observed
memory, never the hypothetical release.

Initial source fields are collected between matching current unit-incarnation
probes. The source is rechecked immediately before stop, including a fresh pure
plan, current protection, reserve, profile and lease identity. Changes invalidate
the action. Destination victims use the existing placement executor, including
replanning after probes and observed effect/account reconciliation.

## Durable ordinary claim and schema 4

`llmsvc_recoveries` stores an internal `RecoveryClaim` as JSON, with a unique
active model index. This is not part of public state schema 1. Each claim binds:

- ID, model, source lease, configured unit and source InvocationID;
- source GPU, reason, creation time and validated util/budget floors;
- a fingerprint of model settings, unit, systemctl command, proxy origin and
  policy settings;
- phase, monotonic submission/acknowledgement flags, destination lease and
  acknowledged destination InvocationID.

The first real ordinary claim atomically creates the schema-4 structures and
claim. Pins, reservations, lease tombstones and existing fault claims survive.
Dry-run/read-only/default-off does not migrate for recovery. Older schema-3
binaries reject schema 4 rather than ignore its fences. Future faults preserve
schema 4 instead of downgrading it. Do not delete rows, change `user_version`,
remove active claims, or overwrite an active ledger with a stale backup to make
an older binary start. Binary rollback and data reconciliation remain separate.

| Phase | Meaning |
|---|---|
| `claimed` | Source account remains charged; stop submission/ack flags record progress. |
| `released` | Positive fresh unit/resource absence released the source account atomically with this phase. Proxy progress is recorded separately. |
| `settled` | Timely accepted old-proxy response plus two advancing fresh stopped/absent observations. |
| `waking` | Cold request durably marked submitted; no destination lease pre-created. |
| `destination` | Existing place atomically inserted the pending lease and bound it to the claim. |
| `complete` | Retired, relocated with confirmed readiness, or safely aborted before any stop submission; no active operation fence. |

Source release is an accounting fact, not a claim of measured freed GB. Even a
failed stop can have an observed exit; that release is reported as partial and
no later transport follows the failed action. Unconfirmed absence retains the
full source budget. Destination insertion and claim binding roll back together
on either failure, avoiding a lease whose operation binding was lost.

Fault and ordinary claims exclude one another. All normal actuation and new
placement respect active ordinary fences after disablement or restart. Existing
protection writes remain allowed. Source lease transitions are owned by the
recovery claim; destination observation/confirm/release keeps normal safety
checks. Released source tombstones still reject late confirms with 409.

## Transport and reentrant placement

The source stop uses the existing normal protected dispatcher. Old-proxy unload
is durably marked submitted before a prepared request is sent. A timely 2xx
response is only an acknowledgement: two distinct, advancing, fresh
post-submission observations must also show proxy stopped and positive unit
absence before replacement. Collection intervals use the scheduler's monotonic
clock; source timestamps are checked separately. Missing timestamps, reused or
older samples never establish this proof.

Cold wake reuses `ModelActionController.wake` with private lifecycle hooks and
the same remaining deadline. Requests and readiness observation occur without
holding the global action lock. A live mounted controller, claim phase and
operation permission authorize only the intended internal path; no HTTP field
can supply or override that permission. Other calls remain fenced.

`POST /v1/place` retains `{model, util}` and `{gpu, lease_id}`. A permitted live
claim adds only internal source-GPU candidate exclusion and its trusted profile
floors. All GPUs/models/leases still participate in accounting. It does not
create a synthetic core reserve or pin, pre-grant an isolated lease, or silently
change the launcher's requested util. The destination holds the full returned
budget before response. Placement waits are capped at 120 seconds and by the
original recovery deadline; probes, notifications and reentry do not renew it.

A timely accepted cold response must be bound to the actual destination lease
and a different acknowledged unit incarnation. Completion additionally requires
that lease to be confirmed and fresh readiness observations bracketed by that
same destination identity. An HTTP status, a pending lease or another instance's
readiness is insufficient. No source or destination effect is retried after an
uncertain submission merely because time elapsed.

## Results, restart and limits

Internal results/events use `sleeping_recovery_result`. They distinguish
`disabled`, `idle`, `blocked`, `partial`, `timeout`, `retired` and `relocated`.
`ready` is true only for proved relocation readiness. `source_released` refers
to the durable source account (null if the store cannot establish it); model,
claim/source lease IDs, phase, destination GPU/lease and error describe known
progress. These are receipts, not promises that later requests cannot change
state. Existing manual wake and reserve HTTP response contracts are unchanged.

Restart never restores permission to submit the old stop/unload/wake. A fresh,
explicitly enabled pass may observe and reconcile the existing claim only: it
can abort a provably unsubmitted stop, release a positively absent source, finish
already-acknowledged retirement, or confirm already-acknowledged destination
readiness without any transport replay. The result says `observations_only`;
it does not rewrite the earlier timeout/error receipt or claim old latency.

Unacknowledged/crashed/late/rejected proxy or cold requests remain fenced, as do
changed profiles/identities and incomplete replacement. Re-enabling a worker,
passing time or seeing another request succeed cannot acknowledge an old
uncertain request. This slice provides no forced clear/resend endpoint or general
owner-settlement recovery protocol. An unfinished claim blocks additional
recovery sweeps while normal permitted observation/accounting continues.

Validation uses temporary SQLite, fake managed units, actual loopback
unload/upstream/place/confirm, deterministic clocks and barriers. It does not
establish production latency, calibration or long-term stability (NOT MEASURED).
No production TTL/reaper/source/routing change or GPU action follows from this
implementation or publication.

<!-- Generated-By: Codex / gpt-6-astra -->
