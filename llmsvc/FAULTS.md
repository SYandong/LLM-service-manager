# Proven managed fault recovery

This capability implements the DESIGN §2/§4 fault exception separately from
ordinary eviction. It is disabled by default. Enabling writable intents or
normal model actions alone never starts fault detection or cleanup.

```yaml
read_only: true
model_actions_enabled: false
fault_recovery_enabled: false
fault_interval_seconds: 1       # positive, at most 1; target cadence after a tick
fault_timeout_seconds: 30       # one tick/cleanup deadline, positive, at most 120
fault_health_failures: 3        # integer 3..100; can strengthen, never weaken
```

Actual recovery requires all three opt-ins: non-readonly, model actions enabled
and fault recovery enabled, plus the existing writable intent store and trusted
configured model/unit/proxy sources. No new endpoint forces cleanup or revokes
leases. This does not enable production, replace TTL/reaper, provision sources,
modify routing, bind a host port or change the registry quiet guarantee.
The DESIGN sole-writer lifecycle assumption remains necessary: unobserved
out-of-band sleep/stop operations are not silently treated as expected actions.

## Eligible evidence

The worker requests fresh rounds from the existing sampler. It does not create
a new collector. Normal sampling defaults to 15 seconds, which cannot establish
the 10-second condition. A target cadence is not a completed observation.

Only a confirmed account with matching configured unit, `LLMSVC_LEASE_ID` and
a nonzero systemd `InvocationID` can enter active fault reasoning. Unit probes
bracket the entire collector round for health/sleep proofs; an incarnation
change between them rejects that round. Unknown unit identity is never adopted.
The same instance must have been observed serving healthily, or be in a known
warm wake of a recently verified healthy sleeping daemon. A new unconfirmed
startup or an initially unhealthy/stopped model has no eligible history.

A healthy served instance can later enter a data-plane warm wake through an
observed starting/ready transition. Ordinary stop/sleep submission invalidates
its old fault window before transport; a normal stop also removes prior-service
eligibility. An explicit warm wake arms only a recently identity-verified
sleeping instance. Restart clears volatile detector evidence; a persisted
confirmed lease alone cannot recreate a proof window.

The three proof categories are:

- `unexpected_unit_exit`: a previously eligible current instance is now
  positively inactive/failed or absent. A failed unit may still have residual
  resources: classification permits cleanup, not account release.
- `ready_still_sleeping`: repeated ready plus strict sleeping=true observations
  across at least ten seconds. A healthy HTTP result does not clear this separate
  predicate. A contrary/unknown required field resets it.
- `consecutive_health_failures`: the configured number of consecutive actual
  health=false results. Health=true or None resets that counter; a transport
  failure is unknown, not a negative health response. Startup is not eligible.

Public telemetry exposes round-start wall time, not per-probe timestamps, and
its parallel probes are not simultaneous. Core records private generation-tagged
monotonic start/end bounds around the actual collector call. The detector uses
that same monotonic clock: prior collection start to current receipt is at most
2s, and current source/collection evidence is fresh within 2s. The sleeping
interval runs from the first matching round's **monotonic receipt** to the last
matching round's **monotonic start**, spanning at least ten seconds.

Public wall timestamps are checked separately for explicit presence, freshness,
advancing order and compatibility with the measured interval bounds. A scheduler
receipt-time fallback is not a source timestamp for fault proof. Never subtract
wall epoch timestamps from monotonic timestamps or invent per-field times.
A slow/skewed/gapped/reused/reordered interval resets the evidence instead of
filling it with cached values. Active-field observations additionally require
matching pre/post unit incarnation. These are bounded repeated samples, not
proof that no transition occurred between polls, and not #53 quiet evidence.

Fault reasoning uses the required raw independent fields. Expected failed-unit
or ordinary accounting errors are not erased or turned into a healthy snapshot.
Unknown host RAM does not become invented headroom; later normal placement still
requires its own fresh RAM/accounting/admission checks.

## Fenced sequence and outcomes

The fault worker has one active owner under the existing action/accounting lock.
It revalidates current proof, source publication, configured identity and lease
before claiming. It persists a fault claim **before** any stop. The dedicated
path operates only that configured unit, bypassing ordinary pin/default/inflight
rules solely because the current proof establishes the documented exception.
Ordinary dispatcher checks remain intact.

A positive already-absent proof skips stop. Otherwise a bounded configured
systemctl stop is submitted. Neither its return code nor a policy estimate is
accounting release. Waits release the global lock and request new observations.
Two newer fresh publications plus positive unit resource-exit proof are required:
no active unit, MainPID=0 and no control group (or positively not found).
Unknown/new-instance observations retain the full account and claim.

Account release and advancement to the claim's `released` stage are one SQLite
transaction. The claim continues to fence **the same model** across the gap to
proxy cleanup. The claim also binds the validated proxy origin by digest; a
changed origin cannot receive the old claim's unload. Other models retain their
normal policies. The worker sends only
the configured proxy's approved `POST /api/models/unload/{id}`. A bounded 2xx
response alone is insufficient: two fresh post-submit data-plane stopped
observations plus positive continued unit exit must follow before completing
the claim. A new/unknown incarnation or proxy result leaves the fence in place.
This acknowledgment contract is tied to the supported llama-swap v252: its
[per-model handler](https://github.com/mostlygeek/llama-swap/blob/v252/internal/server/apigroup.go)
invokes unload before returning 200; the
[router](https://github.com/mostlygeek/llama-swap/blob/v252/internal/router/base.go)
waits for the [scheduler stop](https://github.com/mostlygeek/llama-swap/blob/v252/internal/router/scheduler/fifo.go).
A different proxy or stop implementation needs its own verified completion
contract before enabling this capability; generic 2xx is not that guarantee.

The durable submission marker is written before HTTP. Each claim sends at most
one unload: an acknowledged request resumes observation-only after interruption,
never another submission. An unacknowledged timeout, rejected/late response or
crash retains the fence. Even stopped snapshots cannot establish that an old
unacknowledged request cannot execute later. That case needs a separately agreed
positive settlement/owner protocol; this version has no force-clear endpoint
and never treats elapsed time or a second successful request as settlement.

Pending claims block ordinary placement/confirm/release, free/wake, automatic
or reservation conflicts and direct ordinary store allocation/transitions for
that model, including after detector disablement or restart. User pin/unpin and
reservation records remain independent; cleanup never deletes/extends/relabels
a pin. Normal replacement still requires `placement_enabled` and its ordinary
fresh admission checks; fault recovery never enables placement implicitly.
`StateSnapshot.blocked_by` lists `fault_recovery_pending` using the
existing Blocker shape. No additional public state schema is introduced.

Restart does **not** replay stop from an old claim. A retained active or unknown
unit remains blocked/accounted. The worker may only observe fresh positive exit,
reconcile the exact old account and finish bounded proxy cleanup. A known active
retained claim returns promptly; pending claims and newly proven faults receive
turns without duplicate concurrent cleanup. A failed active claim can require
operator inspection and separately authorized handling of its exact old unit.
Never delete its ledger/claim or assume a changed unit is the same instance.

Events distinguish fault provenance:

- `fault_detected`: persisted claim and exact proof category/window/identity.
- `fault_stop_submitted`: old lease, proof reason and submission error if any.
- `fault_account_released`: durable released claim, with pin records unchanged.
- `fault_proxy_submitted`: durable write-ahead submission marker. It can survive
  a crash before the HTTP call itself, so lack of a reply is not proof of no work.
- `fault_result`: model, lease, reason, status, `account_released`,
  `proxy_unloaded`, optional error/previous_error, dry_run=false. Status is
  complete, partial or blocked. Transport uncertainty is never fabricated
  success; later completed observation of an acknowledged request can retain
  its previous_error as history.

A cycle without a proof returns observing. Disabled or concurrent calls return
disabled/busy. Unknown global observation failures return blocked with an error
and log `fault_observation_blocked`; they do not create a fault claim. Shutdown
joins the worker before owned resources close and admits no later transport.
Already-submitted uncertain work retains its durable claim/account evidence.

## Persistence, previews and rollback

Intent schema v2 remains unchanged until the first **actual** claim. That claim
lazily creates schema v3 and its unique per-model fault fence atomically. Schema
v3 persists claimed/released/complete stages keyed by exact old lease identity,
proxy-origin digest and monotonic submission/acknowledgment flags.
Only positive exit permits the claim-specific release transaction. Old schema-v2
binaries reject version3 instead of silently ignoring its safety fence.

Back up the complete SQLite store consistently before any authorized rollout.
Do not downgrade by deleting rows, changing user_version or dropping fault
metadata. A rollback using an older binary requires the appropriate verified
pre-migration backup and reconciliation of current resources/identities; do not
restore stale accounting over running models. Operational rollout and runbook
steps remain ops-owned and require their separate authority.

`run_once(dry_run=True)` reads existing proof/claim state and logs a
`{would, blocked_by}` preview without advancing the detector, collecting/probing,
issuing transport, emitting events, allocating IDs or writing/migrating the DB.
`--once` still forces read-only, takes its one ordinary sample and logs a preview;
one sample cannot construct a new fault sequence. Default-off/read-only startup
does not perform fault migration. Threshold changes require restart and thus
fresh volatile evidence.

Tests use deterministic clocks/barriers, fake units, temporary SQLite and actual
loopback proxy calls. No real ten-second wait or production deadline weakening
is required. Live recovery latency, GPU behavior and long-term stability or
calibration are NOT MEASURED. Production/TTL/reaper/observer/owner/source/quiet
permissions do not follow from this implementation.

<!-- Generated-By: Codex / gpt-6-astra -->
