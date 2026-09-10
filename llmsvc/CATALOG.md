# Runtime catalog lifecycle (#157 / #159)

`CatalogRuntime` connects the existing reload queue to configured collectors,
managed transports, placement, automation and event filtering. It grants no
admission from discovery, a registry record, a pin or an observed generation.
The HTTP registry write paths fail closed until a trusted reload execution source and profile/instance providers are wired. Once explicitly connected, the existing add/rm payloads return existing queued job receipts and one scheduler worker drives them; no second llm command is introduced. This lifecycle is exercised by real temporary queue/HTTP/store/copied-CLI fixtures.

`catalog_enabled` is a separate strict boolean, default false. Actual internal
`enqueue` / `process_once` / `reconcile` also require non-readonly operation, a
writable intent store and an explicitly supplied read-only proof verifier. The
entrypoint supplies no fabricated verifier. `prepare` and `dry_run` create no
catalog ID, object generation, DB row, queue entry or transport request.

`prepare(candidate_bytes, trusted_profiles, binding=CandidateBinding)` validates
an immutable prospective generation. Every candidate model needs explicit
trusted weights, budget/util, known default role, distinct canonical vllm unit,
daemon port and direct literal-IP endpoint. No resource size is inherited from
an arbitrary command, macro or base name. A new temporary model cannot be default;
same-name profile changes are rejected in this registration slice and require separate configuration reconciliation. Removed metadata with unresolved lease/fault/recovery references is
retained for observation and lease reconciliation (including reserved endpoints), while active admission and
normal transport paths exclude that name. Removed pinned/default names block.
Global collector/source settings are unchanged by this transaction.

`submit_change` connects the existing registry callback, composing its pure model edit with the existing generation planner, explicit trusted profiles and instance. `enqueue` preserves registry descriptions, late prechecks and cleanup as well as catalog membership protection.
`process_once` claims before config effects, verifies the exact candidate, source
instance, original marker/job and complete adoption/settlement/cleanup evidence,
then publishes collector, transport, admission and event generation under the
single action lock. The queue retires its marker; old objects close and drains
finish outside the lock; final current proof and durable phase release precede
unfencing. All phases share the original operation deadline. Python callbacks
and filesystem I/O are not claimed to be preemptible.

Collection captures `(catalog_epoch, collector)` before releasing the lock.
Late old successes and errors cannot replace a new snapshot. Old buffered relay
rows are reported as `catalog_events_discarded`, with their old epoch and local
counts, never forwarded as new-generation state. Transport references validate
their epoch and global fence before submission. Busy model operations prevent
installation; older waiting placement requests revalidate through their original
transport. Accounting writers independently consult the persistent fence. Pin
and reserve protection records remain writable.

The first actual catalog claim atomically creates schema **5**, retaining all
v2-v4 pins, reserves, leases, ordinary recovery claims and fault claims. One
bounded JSON checkpoint in `llmsvc_catalog` records transaction/job IDs, old/new
epochs and manifests, base/candidate hashes, CandidateBinding and the original
marker bytes/digest. Phases are `claimed`, `published`, `released`, `aborted`.
An uncommitted abort retains the previous released checkpoint (one bounded backup, not an unbounded history); static first-transaction aborts keep normal restart behavior. Only a provably uncommitted transaction can abort; published-but-unreleased
state remains fenced even after marker unlink or failed receipt restoration.
Older readers reject schema 5. Disabling this feature is not a schema downgrade.

Startup restores exact observation metadata from a checkpoint whose global
source settings still match. It keeps actions fenced until a fresh explicit
proof/retirement reconciliation; it does not replay config, reload, cleanup,
stop or wake. `reconcile` is a separate internal verified install/retirement
operation, not an HTTP proof/force-clear endpoint. An absent marker or a volatile
queue latch is insufficient authority. Missing source, foreign receipt, failed
fsync, partial proof or a failed final checkpoint retains the global fence. Failed old collector/relay closes keep their handles for retry; a bridge is marked closed only after its real subscription and consumer stop. Explicit reconciliation cannot release the fence while any retirement is incomplete.

Deployment is coordinated with ops #169: the current reviewed, rehearsed,
reversible maintenance rollout is authorized. Default-off/read-only upgrade does
not create schema 5. Before any actual migration preserve a consistent complete
ledger and configure a supported proof source; rollback must reconcile binary,
configuration and live resources. Never delete rows/claims, edit user_version or
restore a stale ledger over active resources to bypass a fence. Current user
inference URLs/model IDs and the single shared llm command remain unchanged.

Tests use temporary SQLite/config files, deterministic barriers, fake units and
real loopback scheduler HTTP. These prove implementation boundaries, not live
quiet/settlement or long-term stability/calibration, which remain NOT MEASURED.

<!-- Generated-By: Codex / gpt-6-astra -->
