# First managed default bootstrap

Bootstrap is an explicit one-shot migration through the existing scheduler,
placement API and lease-aware launcher. It is default-off. It neither adopts an
unleased process nor removes preload to satisfy maintenance preflight. Ordinary
maintenance must still reject an unleased preload.

The operator uses `llmsvc-scheduler --config ... --bootstrap-default`, or explicit
`--bootstrap-recover observe|resume|rollback`. The normal control endpoint is
bound before any migration effect. Only sampling and this bounded bootstrap run;
automatic model/fault/catalog workers are not started by the one-shot. A normal
scheduler is then started by the deployment procedure after successful handoff.
An occupied control endpoint refuses the attempt. Normal invocation never
implicitly starts bootstrap, including after an automatic read-only upgrade.

## Configured authority and pinned inputs

`bootstrap_enabled` defaults to false. Bootstrap also requires non-read-only
placement, catalog and model-action opt-ins, maintenance mode, a writable
configured intent store and the configured default metadata. `bootstrap` supplies
one default `model`, numeric `util`, explicit absolute `command` argv, pinned
`launcher_path`/`launcher_sha256`, pinned launcher JSON configuration path/hash,
absolute `migration_command` argv, `manifest_sha256`, exact source
`base_config_sha256`/`target_config_sha256`, and `timeout_seconds` (at most900).
The reviewed manifest and deployment owner bind actual paths, unit definitions,
old/new artifact bytes, default preload, aliases and rollback conditions.

The command's port and GPU utilization must match the configured daemon and
lease request. The launcher JSON must target this exact control endpoint and
existing place/confirm/release paths. Its daemon EnvironmentFile is retained
from the verified launcher configuration; it is not inferred from the native
proxy environment. Checked launcher bytes/configuration are captured before
migration, rather than reread later as executable authority. Bootstrap HTTP does
not use environment proxies or follow redirects carrying its private capability.

## Durable sequence

1. Read-only preflight proves the exact old source/listener/configuration, fresh
   zero requests, absent relevant legacy backends and preserved default preload.
2. Create a schema7 bootstrap claim before migration effects. An existing active
   allocation, fault/recovery/catalog claim, or another bootstrap prevents this
   first-topology migration. Existing pins/reserves and historical accounts remain.
3. Record stage submission. Ops' pinned manifest stages the reviewed unit,
   launcher and configuration transition; source/actor/helper absence and hashes
   must be positively observed before starting the default.
4. The existing launcher requests `POST /v1/place`. Lease creation and its
   attachment to the bootstrap claim are one SQLite transaction. The full budget
   is reserved before return. Bootstrap cannot evict another model.
5. Record start submission before the existing `systemd-run` call. Fresh unit
   absence, physical GPU/RAM and unexpired reservation are rechecked; configured
   default placement/protection and budget floors remain unchanged.
6. Actual health and the existing lease confirm protocol establish a confirmed
   default account. The configured unit's lease token and GPU must agree. A
   command return or unrelated health response is not a replacement for this.
7. Record native activation submission. Only a bound new instance, preserved
   preload/configuration and actual default-account/native readiness complete the
   migration. A positive later observation may settle an unknown response, but
   is not recorded as a timely command acknowledgement.

Schema7 remains readable for current state even when bootstrap execution is
disabled. Older schema6 readers reject it. Never downgrade by removing claims,
rows or schema markers. The first-bootstrap schema explicitly records that no
catalog exists yet; creating a real catalog irreversibly marks that fact so a
later deleted catalog cannot be mistaken for first initialization.

A pending claim fences ordinary mutations. Only the internal launcher's random
capability can operate its selected default/lease through the existing paths.
The store rechecks that scope; capability rotation on explicit recovery also
invalidates old waiting request threads. The capability is hashed in the claim
and is not logged or exposed through a tenant proof endpoint.

## Source exclusion and unknowns

The raw public snapshot retains source-down errors and unknown activity. Only the
authorized bootstrap placement/confirmation path may exclude the exact expected
`running/events` connection-refused errors after a fresh manifest-bound proof of
deliberate source absence. It does not manufacture inflight zero or quiet, erase
other probe errors, or permit eviction. GPU, RAM, unit and health observations
must still be fresh. Unsupported/stale source exclusion blocks the operation.

## Interruption and rollback

`observe` only reconciles an existing outcome/account; it does not repeat native
start or activate a stage that was never submitted. `resume` may continue an
unstarted stage or use its actual still-current prepared lease, but never resend
an unknown default launch. An already launched default must be observed through
its recorded unit/token/health before confirmation. Missing/ambiguous evidence
retains the full budget and bootstrap fence.

Before any default launch submission, explicit file-only rollback can release
only its own pending lease after the existing positive-exit checks. Ops restores
verified files without restarting an unleased old preload. If stop was never
submitted, a fresh exact-original-instance/zero-request proof may instead retain
that existing source while rolling back files; absence is not fabricated. After launch
submission, rollback refuses to stop a running default or pretend the attempt
never happened. It never restores an old ledger snapshot. Unknown rollback or
activation results retain evidence rather than clearing the claim by elapsed time.

The complete feature includes ops' wildcard/listener and executable migration
manifest implementation and its bounded native/site validation. Core tests use
owned CPU processes, actual HTTP/health/lease calls and temporary SQLite, with
explicitly simulated GPU/systemd/native-source proofs. Those fixtures do not
establish site readiness, continuous quiet or long-term stability.

<!-- Generated-By: Codex / gpt-6-astra -->
