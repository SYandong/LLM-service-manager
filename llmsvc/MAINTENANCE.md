# Explicit maintenance transition

This mode connects the existing registry/catalog worker to an explicitly
configured site adapter. It does not change the ordinary hot-reload quiet proof,
introduce another inference proxy, or expose an HTTP proof/force-clear endpoint.

## Configuration and bootstrap

`catalog_mode` defaults to `hot_reload`. `maintenance` additionally needs
`catalog_enabled`, `model_actions_enabled`, non-readonly operation, an absolute
`maintenance_command` argv and explicit `catalog_profiles`. Current-model
profile completions cannot disagree with existing configured fields. New model
profiles must pass the existing catalog validation; no weights or budgets are
inferred as measurements from a name, macro or parent model.

The normal bootstrap constructs `CommandBackend`, `MaintenanceController`, the
existing `CatalogRuntime` and its registry callbacks. It does not execute an
adapter during validation/construction. The ordinary entrypoint remains safe
when maintenance is not selected. An observation-only or incomplete adapter is
not a usable effect/proof source and must remain blocked.

The existing scheduler entrypoint has an operator-only
`--maintenance-recover observe|rollback` mode. Actual recovery binds the normal
control endpoint before any recovery effect, rejecting a concurrently running
normal scheduler at that endpoint. It is a bounded one-shot invocation, not a
second background deployer. `--dry-run` reports intent without adapter calls,
port binding or recovery writes. Site execution remains the deployment owner's
responsibility; this is not a tenant HTTP recovery capability.

## Protocol and evidence

The configured executable receives an operation argument and one JSON envelope
on stdin: operation, context, remaining timeout and correlated request ID.
Stdout/stderr allocation and subprocess time are bounded; there is no shell.
Replies must match the request/transaction, and observation timestamps must be
inside that request's monotonic interval. Timeout or nonzero exit is not proof
that an external action was undone. Adapter output/stderr is not copied into
public error messages.

The selected `stop_instance` method is an explicit controlled interruption. It
does not pretend a socket activation or pause API exists. Old process/scope,
attributable helpers/native jobs, retained backend identities and current
configuration require independent positive observations. The new verified
instance may listen while the catalog/placement gate remains held. Pre-cleanup
adoption requires known retained-backend integrity; it may report cleanup=false
until the protected removal callback runs. Final proof/release still requires
cleanup=true. A submitted cleanup stop that already exited can reconcile only
its exact captured account from fresh exit evidence before final cleanup proof;
this does not resend the stop or release an unobserved account. The final
proof's exclusion fact concerns the retired old instance, not an invented gate
on the new listener. The normal QuietPeriod is never filled with synthetic zeros.

`InstanceTransitionProof` distinguishes the old and new InstanceIdentity and
scope hashes, current configuration hash and exact marker. Its six required
facts are strict booleans. A rollback proof explicitly selects the base hash and
also establishes settlement of the failed attempt. Same-instance evidence is
not substituted to satisfy a hot-reload verifier.

## Checkpoint and recovery

First actual maintenance claim atomically creates schema6, linked to the catalog
transaction/job. The separate bounded context retains exact base bytes, stable
process scope/actors, backend lease/incarnation bindings, candidate/restored
identities, side-effect submission/acknowledgement records and observations.
Schemas2–5 and all model accounts/protections remain readable and preserved;
older binaries reject6. Default-off/readonly/dry-run creates no maintenance row.

A submitted stop/start is never resent because its acknowledgement is missing.
A separately bound positive observation may establish its outcome without
rewriting it as a timely command ACK. Failed/unknown helpers hold the original
configuration or partial transaction; elapsed time does not settle them.

Rollback is explicit, uses one remaining deadline and cannot replay a completed
transaction. It verifies this attempt's exit before staging/restoring the exact
base bytes, and requires a distinct restored instance and actual base adoption.
The old manifest is republished under a fresh rollback epoch. Current ledger
rows are never restored from the configuration backup. Failed or uncertain
rollback stays fenced. Source retirement releases the action lock so readers can
finish. Final release rechecks shutdown, checkpoint, source and marker after the
last proof. Persisted actor inventories can grow as attributable helpers are
observed, but cannot discard an earlier actor or change a bound process scope.

An unstarted claim can be aborted only when its durable effect map is empty,
its own exact receipt remains bound, and the base bytes are unchanged. It uses
the existing strict receipt-retirement primitive; arbitrary caller flags cannot
clear a marker. No instance transition is claimed by this no-effect abort.

For removed running models, the existing protected dispatcher checks default,
pin, inflight, freshness, unit and captured lease/incarnation. The site adapter
can only receive that exact target. Positive unit absence precedes account
release; proxy exit, policy estimates and transport ACKs do not release budgets.

## Validation scope

Tests run real owned loopback proxy/helper processes and temporary SQLite/config
files. They cover forward old/new transition, delayed/failed helper outcomes,
failed start, exact base rollback, distinct restored identity, no native replay,
late protection, observed model-account release and endpoint exclusion. Those
fixtures are not production quiet/settlement or long-term calibration evidence.
No production migration or site action is performed by installing these modules.

## Explicit native witness phases

Optional `native_witness` pins supported source revisions, executable digests and
dialects. When enabled for maintenance it additionally requires `phase_images`
with `old`, `candidate` and `restored` image digests from that allowlist. The normal
catalog factory mounts `BoundNativeGenerationReader` through its maintenance
controller; construction performs no I/O. Unconfigured maintenance retains its
existing adapter protocol.

Before effects the durable maintenance record includes immutable
`native_provenance`: the canonical endpoint, complete checked witness settings
and the base generation. Old, candidate and restored instances use their own
saved identities and intended image, with configuration bytes and reader results
checked within the existing deadline. Successful candidate observations retain
the actual pin and sampled identity; `settlement_confirmed` remains null in that
visibility receipt. Independent helper, backend and cleanup proof is still
mandatory.

The adapter's opt-in `configuration_file_confirmed` reports only its own fresh
file/instance evidence. Core obtains generation from the bound reader; an adapter
must not claim that file bytes alone prove native generation visibility. The
legacy unconfigured adapter path is unchanged. Site adapters must explicitly
support the new context before enabling these settings.

The new strict maintenance-record field is rejected by older readers, including
on restart; it is never hidden in an extensible observations dictionary that an
old writer could ignore. Records without the field remain readable, but changing
witness mode/settings while a transaction is pending blocks execution. Preserve
the full ledger for rollback; do not remove the field or rebuild it from current
settings. Default-off and dry-run create no new provenance record.

The base must already contain a valid generation scalar. An untagged base cannot
provide a rollback witness with the existing strict reader, so this mode refuses
before source effects. This implementation does not silently stamp a live source,
change a binary or provide a source installer. A phase allowlist is not proof that
a site adapter can install another image; actual source transition and rollback
still require the designated deployment owner's reviewed implementation/proofs.

<!-- Generated-By: Codex / gpt-6-astra -->
