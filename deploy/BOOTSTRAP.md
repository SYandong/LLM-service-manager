# First managed startup (#201)

`bootstrap_native.py` supplies the deployment side of the core bootstrap
transaction. It does not create, adopt, confirm or release a lease. Core persists
its claim before external effects and uses the existing lease-aware launcher for
the first default's place/start/health/confirm sequence. Ordinary maintenance
still rejects an unleased preload. Removing that preload is not a migration.

Run the adapter through the existing bounded command protocol:

```text
<approved-python> -B <approved-source>/deploy/maintenance_executor.py --profile <private-bootstrap-profile.json> <operation>
```

The private dispatch profile contains `bootstrap_adapter: true`, an absolute
`manifest_path`, and the exact file `manifest_sha256`. It is separate from the
native maintenance profile installed by the transaction. The outer
`operation/context/timeout_seconds/request_id` envelope and correlated response
are unchanged. Every effect result's `accepted` is an ACK; the core must still
validate the independent stage and instance proof fields.

## Immutable manifest and file boundaries

A schema-1 manifest specifies a precreated private `state_dir`, canonical
`artifact_root`, `default_model`, `default_unit`, the old `source_profile`, and
exactly these six `files` entries:

| Entry | Target |
|---|---|
| `unit_fragment` | Existing pinned native unit definition |
| `native_config` | Existing native configuration |
| `launcher` | Site-selected lease-aware launcher implementation |
| `launcher_config` | Its explicitly selected configuration |
| `native_profile` | Pinned profile used after staging |
| `attempt_environment` | Dedicated initially empty native attempt file |

Each entry has an immutable artifact `source`, exact `target`, `before` metadata
(or null for an absent target), and `after` metadata. Metadata is
`{sha256, mode, uid, gid}`. Targets must have existing canonical parents; symlinks
and multiply linked files are refused. Inputs are bounded regular files with
verified hashes. Private targets use mode 0600. The manifest selects all paths;
request bodies cannot choose a service, file or executable.

Declare `native_environment_files` as the ordered list of existing native
EnvironmentFile `{path, sha256}` bindings, and `daemon_environment_file` as an
independently observed `{path, sha256}` binding. The staged launcher's
`systemd_run.environment_file` must name that daemon file. Do not infer that the
native and daemon environments are interchangeable. Values are neither returned
in adapter diagnostics nor copied into the command context.

The unit delta is restricted to replacing its single Service `Restart` directive
with `Restart=no` and inserting the dedicated attempt EnvironmentFile immediately
after it. Every other unit byte remains unchanged. Existing environment files
are retained; untracked lifecycle hooks are rejected. The new native config
retains the configured single default preload and model inventory. Alias and
other model settings and all unrelated top-level native settings are checked
against the baseline. Review the resolved wrapper/launcher/proxy commands and
required address families against the pinned native version before producing the
manifest; unresolved or narrowed endpoint mappings are not acceptable.

## Operations and measured outcomes

- `bootstrap_preflight` reads the pinned source image/argv, unit incarnation,
  process/cgroup actors, literal owned event endpoints and complete model rows.
  Every configured legacy backend unit/job must be absent, all native model rows
  stopped, and in-flight zero. Only exact pinned direct-child read-only
  auxiliaries are allowed. Files, environment hashes and default preload must
  match. This creates no receipt, lock file, account or service.
- `bootstrap_stage` requires the immutable preflight `source_identity` and core's
  submitted fence. It exclusively creates an operation directory and private
  backups. After installing only the approved unit fragment and daemon-reloading,
  it rebinds the same PID/start to that fragment and repeats idle/actor checks.
  It signals only that source through a pidfd. The remaining files are installed
  only after actual old actors/cgroup and every configured legacy unit/job are
  absent. A staged receipt needs matching installed bytes and positive absence;
  process exit or a command ACK alone is insufficient.
- `bootstrap_activate` requires the existing confirmed default account and its
  actual unit/lease/GPU binding. It refuses extra backends, unknown prior effects
  or changed staged inputs. It validates the exact native config, checks listener
  availability, records submission and writes bound attempt tags before starting
  only the configured native unit. Readiness requires a new bound source
  incarnation, actual default backend identity, native wrapper/config/preload
  observations and the required owned address-family probes.
- `bootstrap_observe` acquires only an existing shared lock and derives outcomes
  from the durable record, actual file bytes, source/actor absence, unit bindings
  and kernel attempt tags. A completed file stage can be recognized after a lost
  callback without replaying it. While the source is absent, `in_flight` is null;
  the executor does not make public source telemetry look healthy.
- `bootstrap_rollback` restores only matching transaction-owned files when core
  says no default launch was submitted, no account exists, and every backend is
  absent. Before a source-stop submission it may restore the fragment while
  retaining the exact original idle source process. After source exit it leaves
  the old source stopped: starting its unleased preload is not rollback. A running
  default, unknown submitted stop/start, foreign target or corrupt backup blocks
  unsafe restoration. No ledger is restored and no default is stopped.

Context uses `transaction_id == bootstrap_id`, `manifest_sha256`, `default_model`
and `default_unit`. `effects[operation]` must contain `submitted: true` and
`acknowledged: false` before stage, activate or rollback. Activation receives the
existing confirmed `account` row; rollback additionally requires
`launch_submitted: false` and `account: null`. `--dry-run` performs no command,
lock, file or service mutation. Core's final entrypoint and checkpoint validators
must bind these fields in the same #201 implementation before deployment.

A process restart or mode disablement is not permission to rerun a submitted
operation. A changed kernel boot ID also blocks old attempt tags from certifying
a new source instance; retain the claim for explicit recovery. Partial file writes
and unknown transport outcomes retain their records and budgets. Observe first. Rollback also records its submission and
cannot be blindly rerun after interruption. OS/filesystem work cannot be made
hard-real-time; the external bounded process protocol contains the execution
window and preserves uncertainty after timeout.

## Reproducible isolated stage/rollback fixture

The fixture below creates one uniquely named native unit and temporary files,
uses the pinned cached binary, and preserves a synthetic default preload that
runs `/bin/false`. It never starts a backend, creates a lease, or runs inference.
It verifies actual native/systemd source absence, file staging and rollback. This
is separate from core's first managed-account acceptance.

```sh
python3.10 deploy/bootstrap_rehearsal.py \
  --binary /path/to/pinned/llama-swap --binary-sha256 <verified-sha256> \
  --python /path/to/approved/python3.10 \
  --output /path/to/new/private-receipt-directory --seconds 45 --dry-run
```

Execution without `--dry-run` needs root and the existing isolated-test authority.
`--unit-dir` is configurable. The fixture reserves cleanup time, uses a runtime
cap and start limit, stops/removes only its own exact unit, and retains private
receipts. It speaks the actual correlated adapter CLI protocol; a synthetic
submitted request fence is clearly labeled and is not a core schema-7 claim.
Use the final combined core/native test for real account/first-start completion.

A site rollout additionally needs reviewed combined code, exact-current-head
Fable and CI, immutable runtime artifacts, fresh use/identity/protection/memory
proofs and the single ops executor. The read-only puller does not enable bootstrap
or maintenance. Preserve the existing endpoints, aliases, default behavior,
working host-memory source and fixed `llm` trampoline. No TTL/reaper/routing change
or unrelated workload stop is implied.

<!-- Generated-By: Codex / gpt-6-astra -->
