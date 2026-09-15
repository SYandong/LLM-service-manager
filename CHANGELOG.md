# Changelog

## Unreleased

### Operations

- Maintenance adapter failures now include the operation, exit code and a bounded, sanitised
  stderr tail in the `MaintenanceError` text, in `CommandBackend.last_failure`, and in
  `config_change_result.error`, so the adapter's own reason is visible without `strace`.
- Failed configuration transactions now carry the exception message and its `__cause__`/
  `__context__` chain in `job.error`, so the registry queue snapshot and `GET /v1/models`
  show why the transaction failed.
- `catalog_cycle_error` and `fault_error` now log the failure message and are rate-limited to
  at most one line per 60 seconds for the same failure, with a `repeats` count of the suppressed
  copies instead of thousands of identical lines.

## 1.2.1 — 2026-09-16

Patch release; Python distribution `1.2.1`. It carries one merged PR, #269:
`tool_call_parser` and `reasoning_parser` in `llmsvc.json` accept `false` to
remove the option inherited from the base, which a non-thinking model cloned
from a base with a reasoning parser needs. That is below the normal five-PR
cadence, so the reviewed release PR records `Release-Exception: #256` for the
user-authorized delivery.

### Model registration

- `tool_call_parser` and `reasoning_parser` in `llmsvc.json` also accept `false`, which removes
  the option inherited from the base (and, for the tool parser, `--enable-auto-tool-choice`).
  A non-thinking model cloned from a base with `--reasoning-parser qwen3` otherwise returns every
  reply in the `reasoning` field with an empty `content` (Qwen3-Coder-30B-A3B on 2026-09-16).

## 1.2.0 — 2026-09-16

Minor release; Python distribution `1.2.0`. It carries one merged PR, #267:
`llmsvc.json` gains the `tool_call_parser`, `reasoning_parser`, `speculative`
and `max_num_seqs` overrides so a model cloned from a base with MTP speculative
decoding can drop it, and the collector reports known zero activity for models
absent from a successful usage-history query so a never-served model can be
unregistered. That is below the normal five-PR cadence, so the reviewed release
PR records `Release-Exception: #256` for the user-authorized delivery.

### Model registration

- `llmsvc.json` accepts four more whitelisted overrides for the cloned vLLM command:
  `tool_call_parser` (also ensures `--enable-auto-tool-choice`), `reasoning_parser`,
  `speculative` (`false` drops the inherited `--speculative-config`, needed when the base uses
  MTP and the new weights have no MTP head) and `max_num_seqs` (1–4096). They edit only the vLLM
  segment of the cloned command; the wrapper and launcher segments are untouched and every other
  key is still rejected.

### Scheduling

- A model absent from a successful usage-history query now reports known zero request
  aggregates instead of unknown ones. A registered model that never served a request (for
  example one whose first cold start failed) could not be unregistered: every removal was
  blocked on `unknown_activity`.

## 1.1.6 — 2026-09-16

Stable patch release; Python distribution `1.1.6`. It carries one merged PR,
#265: the reload gate no longer treats retained catalog metadata (a model
unregistered through `/srv/models`, stopped with an inactive unit and no
event-relay entry) as `activity_unknown`, which had blocked every later catalog
transaction on the production site. That is below the normal five-PR cadence,
so the reviewed release PR records `Release-Exception: #256` for the
user-authorized delivery.

### Scheduling

- The reload gate no longer reports `activity_unknown` for a model that is `stopped` with an
  inactive unit. Retained catalog metadata (a model unregistered through `/srv/models`) has no
  event-relay entry, so its in-flight count is unknown by construction; that left every later
  catalog transaction blocked behind the retained entry it was supposed to retire. A stopped
  model without a process cannot have requests in flight.

## 1.1.5 — 2026-09-15

Stable patch release; Python distribution `1.1.5`. It carries one merged PR,
#263: a scheduler restart after a fully released catalog transaction no longer
fences placement, which on the native-maintenance site had left every restart
after an add/remove transaction on `catalog_reconciliation_required`. That is
below the normal five-PR cadence, so the reviewed release PR records
`Release-Exception: #256` for the user-authorized delivery.

### Scheduling

- A scheduler restart after a fully released catalog transaction (the live
  configuration is still the released candidate, the receipt is retired, the
  queue is unfenced) now publishes that generation without fencing placement.
  Previously every restart after an add/remove transaction stopped on
  `catalog_reconciliation_required`, and the explicit re-proof could not pass
  because post-release maintenance-profile retirement changes the adapter's
  scope hash (2026-09-15 site incident). A released checkpoint whose
  configuration was edited afterwards still fences as before.

## 1.1.4 — 2026-09-15

Stable patch release; Python distribution `1.1.4`. It carries one merged PR,
#261, which fixes the reconciler's remaining preconditions (boolean
`catalog_pending`, wall-clock snapshot age) and adds a real-object integration
test; without it v1.1.3's directory-driven registration never acts. That is
below the normal five-PR cadence, so the reviewed release PR records
`Release-Exception: #256` for the user-authorized delivery.

### Models

- The directory reconciler's preconditions used two wrong shapes: it treated
  the boolean `store.catalog_pending()` as an optional record (so every tick
  reported `catalog_pending`) and judged snapshot freshness with its monotonic
  clock instead of the scheduler's wall clock (so every orphan was
  `snapshot_blocked`). Both fixed; the test fakes now mirror the real shapes
  and an integration test drives the reconciler through the real scheduler,
  registry and catalog objects.

## 1.1.3 — 2026-09-15

Stable patch release; Python distribution `1.1.3`. It carries one merged PR,
#259, the reconciler catalog-binding fix without which v1.1.2's directory-driven
registration never submits anything. That is below the normal five-PR cadence,
so the reviewed release PR records `Release-Exception: #256` for the
user-authorized v1.1.3 delivery.

### Models

- The directory reconciler compared the registry's `submit_change` callback with
  the catalog's by identity; bound methods are fresh objects on every access, so
  every tick reported `catalog_not_connected` and nothing was ever submitted.
  It now compares by equality, like the scheduler's own check, and the test
  fake hands out a real bound method so this cannot regress silently.

## 1.1.2 — 2026-09-15

Stable patch release; Python distribution `1.1.2`. It carries one merged PR,
#257 (maintenance-transaction deadline fix and the directory-driven model
catalog). That is below the normal five-PR cadence, so the reviewed release PR
records `Release-Exception: #256` for the user-authorized v1.1.2 delivery.

### Models

- Model registration is directory-driven: a model is served iff a one-level
  subdirectory of a configured shared root contains a valid `llmsvc.json`. The
  scheduler's `DirectoryReconciler` registers `pending` candidates at the next
  idle moment and unregisters records whose descriptor was deleted or renamed
  (stopping a running model first). Hand-written models are never touched.
- `llm import`, `llm add`, `llm rm` and the HTTP `POST`/`DELETE /v1/models`
  write endpoints are removed; the write routes now return 405
  `registry_writes_removed`. `llm models` lists `pending` / `configured` /
  `invalid` / `orphaned` discovery rows. Discovery statuses were renamed from
  `importable`/`imported`.
- New configuration keys `model_reconcile_enabled` (default true) and
  `model_reconcile_interval_seconds` (default 30, up to 3600) bound the
  reconciler cadence and its 60 s-doubling backoff (capped at 3600 s).
- New event kind `model_reconcile` records every submitted action and caught
  failure; `GET /v1/models` gains a `reconcile: {enabled, last}` field.

### Scheduling

- A `catalog_mode: maintenance` configuration transaction now has its own
  deadline: `maintenance_timeout_seconds` (default 300 s, finite, up to 900 s)
  bounds every maintenance effect and observation, including operator recovery,
  rollback and restore. It previously shared the HTTP request timeout
  (`operation_timeout`, at most 60 s and 10 s on the site), so a slow model
  sleep plus proxy shutdown could leave the transaction `reconciliation_required`
  and make rollback fail in its observation stage. Ordinary hot reloads,
  adapter preflight and validation keep the old bound.

## 1.1.1 — 2026-09-15

Stable patch release; Python distribution `1.1.1`. It carries the merged
bugfix-only batch #251, #252 and #253. That is below the normal five-PR cadence,
so the reviewed release PR records `Release-Exception: #254` for the
user-authorized v1.1.1 patch delivery.

### Models

- `llm import` works on a site whose `catalog_mode: maintenance` `cmdStop` uses
  the native-maintenance helper form (`helper` subcommand with a single
  `--model <base>`): the clone rewrites only that model token and keeps the
  remaining tokens verbatim, while the `--vllm-url` form and its port-consistency
  check are unchanged and the two forms stay mutually exclusive (#251).
- Import derives the candidate's maintenance-profile row from the configured
  `maintenance_command` `--profile` path and rewrites it atomically (temp file
  plus rename, mode 0600, other fields and key order preserved) before the
  adapter's first scoped command; a rejected submission rolls back, and a
  removed model's row is dropped only after the transaction is released. Dry
  runs and non-maintenance modes never touch that file (#251).

### Scheduling

- Model import accepts thin-launcher options after the unit token
  (`vllm-launch <util> <unit> --config ...`); those options are copied verbatim
  while a second unit-like token or delimiter is still rejected (#252).
- The collector deadline may be up to 12 s and the probe timeout up to 6 s
  (booleans still rejected), so a heavily loaded host's slow `nvidia-smi` no
  longer leaves an error snapshot that blocks placement. Site configuration
  chooses the actual values and the default behavior is unchanged (#252).

### TUI

- The model menu gains `Copy model endpoint address`, which copies the shared
  llama-swap OpenAI-compatible base URL from the optional independent
  `api_url` / `LLM_API_URL` setting (a root path is normalized to `/v1`). It is
  never derived from the scheduler management `url`; when unset the item is
  greyed with a `needs api_url` reason and `/help` explains the setting. Copying
  only requests the terminal clipboard and sends no model request (#253).
- GPU card numbers right-align to a shared width per render, so one/two/three
  digit values, fractions and unknown `?` no longer misalign the bars or labels;
  both bars keep the same real `total_gb` denominator (#253).
- Submitting a model action writes the target transition phase into the STATE
  cell immediately, before HTTP or polling. The start state is captured once at
  dispatch from the latest observation and kept through execution; unknown
  starts show honest `loading`/`queued` and the separate `[qN]` queue marker is
  preserved. It is a local session-intent display only: the snapshot, policy
  inputs and byte progress are untouched, and cancel, failure, completion and
  dry-run leave no residue (#253).
- The footer always leads with the running version, read from the CLI's single
  argparse literal via `app_version`, so no new release literal is added; long
  connection/queue messages no longer push it off. The redundant model-name /
  `source unavailable` detail row and the `Menu for ...` notice are removed,
  while real errors, confirmations and `status --json` diagnostics remain
  (#253).

### Release process

- The #248 publisher gate is unchanged. This patch delivery records an explicit
  reviewed `Release-Exception: #254` in the release PR because the batch has
  three qualifying PRs rather than five.

### Compatibility and validation limits

This entry describes the merged behavior; it does not claim live site
validation, production deployment or GPU operations. Import, launcher-option and
maintenance-profile behavior is covered by the merged regression tests, not by a
live maintenance run. Collector deadline/timeout values are configuration
choices, not calibrated facts, and a slower deadline is not proof of reliable
host sampling. TUI endpoint copying depends on `api_url` being configured and
on terminal clipboard support, and the presentation changes are not visual
acceptance. Default read-only behavior, protection gates, dry-run and ledger
constraints are unchanged.

## 1.1.0 — 2026-09-14

Feature release; Python distribution `1.1.0`. It carries the #247 TUI
management work under the reviewed #248 delivery exception, which also closes
the alpha series in favour of stable minor (features) and patch (bugfix)
releases.

### TUI

- Management commands are queued for the current TUI session in submit order
  and executed one at a time through the existing scheduler API, so a second
  command for a busy model is no longer dropped (#247).
- A model's queued command can be cancelled before it submits HTTP; the active
  command cannot be cancelled. Cancellation and dequeue races cannot emit a
  cancelled item, and exit discards unsubmitted session items without replaying
  unknown results or claiming a remote operation was interrupted (#247).
- Model state exposes an optional `transition` field describing the scheduler's
  active management operation (`SSDtoMEM`, `SSDtoGPU`, `MEMtoGPU`, `GPUtoMEM`,
  and, when freeing, `GPUtoSSD`/`MEMtoSSD`). The TUI shows a queued marker and
  the grounded transition rather than pretending to observe byte copies, does
  not invent a starting location, and shows no transition for dry runs (#247).
- Clicking outside the model menu, including into the command input, dismisses
  it; in-menu actions and right-click cancellation keep working (#247).
- GPU cards keep their index across empty, partially missing and recovered
  samples and mark unknown values as unavailable instead of dropping rows or
  reusing stale readings; the six-GPU capacity bars and the larger bordered
  command input are visible on wide and narrow terminals (#247).

### Release process

- The publisher accepts a real Codex approval (`CODEX-APPROVED <full SHA>` with
  the exact `Generated-By: Codex / gpt-6-astra` watermark) from the same shared
  trusted account as Fable. It checks the single latest review from that account
  before any harness filtering, so a later rejection, stale commit,
  missing/ambiguous/suffixed watermark, conflicting marker or equal-timestamp
  tie fails closed instead of falling back to an earlier approval (#248).
- `release-manifest.json` records generic `review_url`/`review_harness` fields
  additively and keeps the legacy `fable_review` URL only for actual Fable
  reviews, so a Codex approval is never mislabeled (#248).
- Stable feature releases increment the minor version and bugfix releases
  increment the patch version; the alpha series is historical and remains
  parseable/verifiable but no new alpha is published (#248).

### Compatibility and validation limits

This entry describes the merged behavior; it does not claim live site
validation, production deployment or GPU operations, and the per-issue
acceptance evidence for #247 remains tracked on that issue. Existing
protection, dry-run and ledger constraints are unchanged.

## 1.0.0 — 2026-09-14

First stable release; Python distribution `1.0.0`. The qualifying PRs since
alpha.14 are #227, #229, #231, #233, #235, #241, #242, #243, #244 and #245.

### Scheduling

- Placement chooses from a configurable pool (`placement_gpus`) with
  `first_fit` or `best_fit` packing; accounting still covers every observed
  GPU and the exclusive GPU (`exclusive_gpu`) must be inside the pool. The
  shared-card external-usage threshold is a configuration key
  (`shared_external_threshold_gb`) instead of a hard-coded 1 GB, and every
  controller starts from the same `SchedulerConfig.policy_settings()` (#241).
- The launcher terminates a waiting `vllm-wrapper` when the scheduler refuses
  placement, so llama-swap reports the failure at once instead of after the
  wrapper's full wait timeout; lock timeouts and dry runs never signal (#241).
- Per-model actions `POST /v1/sleep/{model}`, `POST /v1/stop/{model}` and
  `POST /v1/preload/{model}` with `llm sleep`, `llm stop` and `llm preload`;
  protections (in-flight, pin, default model never stops) and memory admission
  apply, and previews are pure (#243).
- Bounded cold-wake progress and the scheduler wake cold route (#233, #235);
  mutations are rejected after scheduler stopping (#227); read-only
  maintenance preflight (#229); cleanup observation after daemon exit is
  bounded (#231).

### Models

- Models placed under a shared root with an `llmsvc.json` descriptor
  (`base`, optional `name`, `util`, `max_model_len`, `aliases`, `weights_gb`)
  are discovered read-only, listed by `llm models` and `GET /v1/models`, and
  imported explicitly with `llm import NAME` or `llm import --all`. Weights are
  measured from the safetensors index and the scheduler profile is generated
  from the import, so imported models need no hand-written catalog profile
  (#244).

### TUI

- The TUI is command-first: the command line has focus on start and keeps it
  through mouse clicks; keys follow Claude Code (Enter, Tab completion,
  history, Esc, double Ctrl+C, Ctrl+D, Ctrl+L, Ctrl+O). Model rows open an
  English menu (Load into memory, Bring online, Sleep to memory, Free from
  memory, Copy name, Copy status line, Insert into command line) with inline
  confirmation for stops; GPU bars and event lines are copyable; state
  refreshes every 0.5 s and on every scheduler event (#245).

### Release process

- The publisher accepts stable `vX.Y.Z` tags alongside the alpha series,
  orders every alpha before every stable tag and marks stable releases as the
  repository's latest (#242).

### Compatibility and validation limits

Default configuration behaves as alpha.14: `placement_gpus` unset means every
GPU, `first_fit`, a 1 GB external threshold, and automation, fault recovery and
sleeping recovery stay default-off. This release does not claim tensor-parallel
placement, automatic reload outside `catalog_mode: maintenance`, or unattended
deployment; site enablement of the new keys and the per-model actions is an
operator configuration step.

## 0.1.0-alpha.14 — 2026-09-11

Incremental startup and isolated scheduler-action safety release; Python distribution
`0.1.0a14`. The five qualifying PRs since alpha13 are #215, #217, #219, #221
and #223.

### Corrected and added

- Bootstrap waits for the first valid scheduler observation before initial
  placement, while genuine probe errors, expired deadlines and shutdown still
  fail closed without retrying indefinitely (#215).
- Scheduler-action preflight permits only proven selected-GPU isolation with
  stable primary bystanders; direct lifecycle quiet checks remain strict (#217).
- The isolated stop wrapper uses the supported `sleep --vllm-url` command and
  preserves identity checks before its bound signal (#219).
- Failed action receipts retain available response/status, local request/model,
  client timing/deadline and completed identity checks; unknown acknowledgement,
  effect settlement and cleanup remain explicit (#221).
- The isolated harness no longer forwards optional journal-unit metadata into the
  wrapper command, so lifecycle pipes can close while diagnostics remain in the
  systemd journal (#223).

### Compatibility and validation limits

Bootstrap, native-witness, maintenance and scheduler-action modes remain
default-off. This release does not claim free/wake native acknowledgement, live
GPU behavior, managed-site adoption, automatic upgrade, continuous quiet,
settlement, producer upgrade, TTL/reaper replacement or production routing.
Existing inference endpoints, model IDs/aliases, default, pin and inflight
protections, and the single `llm` command remain unchanged.

Maintenance schema-v6 records containing `native_provenance` are rejected by
alpha12 and older readers; schema7 bootstrap records retain the same older-reader
refusal. An old binary plus the current ledger is not a general rollback path.
The candidate uses bounded CPU/loopback validation only; long-term
stability/calibration and live deployment acceptance remain **NOT MEASURED**.

## 0.1.0-alpha.13 — 2026-09-11

Incremental scheduler and maintenance safety release; Python distribution
`0.1.0a13`. The normal five-PR batch was triggered by #202, #206, #207, #209
and #210; the candidate also includes the necessary terminal-cleanup repair
#211.

### Corrected and added

- The first managed-default bootstrap remains explicit and default-off. It
  persists schema7 claims and real place/launch/health/confirm progress without
  adopting an unleased preload, restoring stale ledgers or replaying unknown
  effects (#202).
- Isolated scheduler-action validation and bounded ownership/cleanup checks use
  the actual lease, process identity, cgroup and environment evidence (#206,
  #210). A failed or unknown observation remains fenced; no live GPU acceptance
  is implied by offline or bounded fixtures.
- Maintenance phases bind immutable native provenance and phase image pins.
  Records containing `native_provenance` are rejected by alpha12 and older
  binaries; an old binary plus the current ledger is not a general rollback
  path. Existing schema7 bootstrap records retain the same forward-compatibility
  boundary (#207).
- Collector contention no longer publishes a fake fresh measurement or runs
  action reconciliation. Mixed probe errors remain visible, real failures stay
  unknown, and action confirmation requires a genuinely newer observation
  (#209).
- Terminal cleanup rejects replacement identities and retains the ledger when
  the owned daemon exit/identity proof is not the original instance (#210,
  #211).

### Compatibility and validation limits

Bootstrap, native-witness and maintenance modes remain disabled by default.
This release does not claim managed-site adoption, source installation,
continuous quiet, settlement, producer upgrade, TTL/reaper replacement or
production routing. Existing inference endpoints, model IDs/aliases, default,
pin and inflight protections, and the single `llm` command remain unchanged.

The maintenance schema-v6 number remains unchanged, but records containing
`native_provenance` require the newer reader; alpha12 and older binaries reject
them. Schema7 bootstrap records retain the same older-reader refusal boundary.
Neither old binaries plus the current ledger nor stale-ledger restoration is a
general rollback path. The candidate's functional tree passed the complete
Python 3.10 suite (2848 tests, 172.56 seconds), matched 45 wheel entries when rebuilt
from the sdist, and passed independent offline minimal/TUI installs, dependency
checks, isolated CLI/TUI imports and release-manifest/checksum validation.
Bounded CPU/loopback fixtures do not establish GPU or production behavior, and
long-term stability/calibration remain **NOT MEASURED**. Current-head CI and
exact Fable approval remain required before this draft can be made ready.

## 0.1.0-alpha.12 — 2026-09-11

Urgent activity-query repair; Python distribution `0.1.0a12`.
Includes #192 and #203 since the immutable alpha.11 tag, using the
user-authorized #167 urgent-fix exception below the normal five-PR threshold.

### Corrected and added

- Activity reads reuse each model's latest timestamp from the summary and seek
  its highest-ID row instead of ranking all historical rows (#203). Counts and
  latest source remain in the same read transaction; timestamp ties, future-row
  filtering, old-only models and unknown origins keep their prior meaning. A
  missing seek result fails as unknown instead of returning a mixed snapshot.
  Existing indexes are used when present; no index, schema or data is written.
  Reader80ms/collector1.8s budgets remain unchanged. Deterministic SQLite work
  and concurrent-WAL regressions establish the reduced work and snapshot
  correctness, not a guarantee against scheduling or lock-related deadlines.
- Explicit, default-off instance maintenance connects the registry/catalog
  transaction to the configured native executor (#192). It records old/new/
  restored identities and submissions durably, preserves unknown outcomes, and
  requires independent source/helper/backend/cleanup proof before release.
  Native helper completion is attributable to owned jobs, never a generic log
  or HTTP200. Adoption is distinct from completed protected cleanup; recovery
  settles only captured submitted-stop accounts from fresh positive exit.
- Maintenance operation scope is documented in AGENTS and DESIGN. Opt-in gates,
  pinned profiles, exact instance checks, default/pin/RAM protection and fresh
  zero-in-flight checks remain required. Fresh zeros are not the ordinary
  hot-reload continuous-quiet proof. Provisioning managed cmdStop helpers changes
  ordinary unload handling too and remains a separate reviewed site step.

### Compatibility and validation limits

The first actual maintenance claim lazily upgrades the ledger to schema6;
read-only/default-off/dry-run operation does not migrate it. Older readers reject
schema6. Preserve current claims/accounts: a stale ledger restore, marker
removal or replay of unknown effects is not rollback. Pending first-managed-start
bootstrap/schema7 and source-origin replacement are not included in this release.

The existing sole publisher and read-only deployment puller preserve current
site settings, inference endpoints, model aliases and the fixed CLI mounts.
This release does not enable maintenance, replace the source, or change TTL/
reaper/model settings. Installed activity behavior is checked in a bounded
window after deployment; genuine deadlines remain unknown and no fixed
hour/day/week wait, historical-cause claim or zero-error guarantee is introduced.
Long-term stability/calibration and desktop clipboard delivery remain separately
unmeasured unless their own acceptance evidence is supplied.

## 0.1.0-alpha.11 — 2026-09-10

Urgent export-path repair; Python distribution `0.1.0a11`.
Includes #194, #195, #196 and #198 since the immutable alpha.10 tag.
The user-authorized urgent-fix exception releases this four-PR batch for #197.

### Corrected

- Export-path edit controls and printable characters retain their input order
  when a terminal delivers them in one batch (#198). Clearing a field and typing
  a replacement filename no longer lets a delayed edit erase the new text.
  Only this field uses the existing native Input actions in its own message
  queue; no dependency, global key routing or button cooldown changes. Save
  remains explicit, full UTF-8, mode0600 and refuses existing files/symlinks;
  filenames cannot become scheduler commands.
- Test-only repairs separate post-action functional observations from the
  intentionally short negative deadline (#194), and trace relay dispatch,
  buffer admission/discard, actual publication IDs and UI delivery (#195).
  Rejected notifications produce truthful discard evidence, not an invented
  notification. These controlled regressions do not reconstruct every prior
  host scheduling failure.
- Large activity/usage functional fixtures keep their data and default reader
  budget while controlling the test clock (#196). Real cancellation tests stay
  separate; an explicit opt-in performance tool retains the wall-clock target,
  records its chosen measurement window and never treats cancelled empty results
  as successful zero. Ordinary tests add no load-based skips or production
  deadline changes.

### Compatibility and validation limits

The read-only upgrade uses the existing six-asset publisher and sole reversible
puller; versions, endpoints, model settings and fixed CLI mounts retain their
contracts. Maintenance/schema6 and source-origin upgrade work are not included.
Genuine activity deadlines and unavailable source attribution remain unknown.

Prior alpha.10 terminal checks remain separate partial/narrow attempts. The new
path-ordering regression is validated after this reviewed release is installed;
publication alone is not an installed test. Clipboard requests still depend on
terminal support and do not confirm delivery to a user's desktop. Long-term
stability, continuous quiet and settlement are not inferred from these checks.

## 0.1.0-alpha.10 — 2026-09-10

Urgent user-facing repairs; Python distribution `0.1.0a10`.
Includes #175, #182, #183, #187, #186 and #190 since the immutable alpha.9 tag.

### Added and corrected

- Quiet SSE connections remain open after successful response headers instead
  of treating normal idle time as a read failure (#187). This prevents idle
  timeout/reconnect cycles from replaying all initial model snapshots. Connection
  and header I/O retain the configured timeout; EOF/network errors reconnect,
  line/frame memory limits remain, and explicit close interrupts idle or partial
  reads. Silence is not liveness or continuous-quiet evidence. Root's real HTTP
  tests and a bounded source comparison distinguish the fixed behavior from
  the previous idle reconnects without performing a model action.
- Event presentation now uses compact human-readable changes and stable counters,
  coalescing repeated snapshots/errors without losing the bounded raw history
  (#190). Details provides a frozen selectable view; user-triggered Copy requests
  terminal clipboard access and Save text exports full UTF-8 text to a new file.
  Default copy uses a compact summary when no selection exists, oversized
  selections get an explicit fallback, and existing files/symlinks are not
  overwritten. Clipboard delivery to a remote desktop is not falsely confirmed.
- Activity failures now report fixed redacted reasons for deadline, locked/schema,
  parse and other read failures (#186). The collector distinguishes parent-round
  expiry from an unfinished prior probe; SQLite budget cancellation cannot be
  mistaken for missing schema, empty success or known zero totals. The UI labels
  partial updates and separates unavailable reads from valid unattributed counts.
  Reader80ms/collector1.8s budgets are unchanged; no retry, stale-count cache or
  source-identity guess was introduced. High load may still cause real deadlines;
  successful reads do not establish a historical failure's cause.
- Trusted catalog transactions install collector, transport, admission and event
  generations coherently (#175). Explicit profile/instance/quiet/adoption/
  settlement/cleanup capabilities are required; ordinary entrypoint defaults do
  not invent them. Source binding, old-generation results, failed retirement,
  foreign/missing receipts and same-name reactivation cannot bypass durable
  catalog fences. Removed resources retain observation/accounting metadata until
  fresh absence and resolved references permit retirement; queue stage is not
  global action readiness. Existing configured add/rm payloads return queued jobs,
  not a claim of successful live installation.
- Relay tests await each specifically injected frame's dispatch completion rather
  than an arbitrary lifetime count (#182), preserving reader ownership, actual
  reconnect, no-request dispatch and provenance/redaction assertions. The
  controlled synchronization proof is not an exact historical scheduling replay.
- Retire the old single-backend proxy source, dashboard/configuration and their
  dedicated tests and example (#27). The retirement itself leaves current CLI/TUI, scheduler, model protection
  and deployment behavior unchanged. Active documentation and package source
  selection use the current control plane. Historical release/roadmap records and
  previously published immutable artifacts remain available for provenance.
- Source retirement performs no additional site shutdown, uninstall, dormant-unit
  deletion or change to the shared CLI mounts. Current bounded consumer evidence
  and the previously authorized stop are recorded separately; arbitrary renamed
  consumers or future manual invocations are not certified absent forever.

### Compatibility and limits

The first actual catalog claim lazily and atomically upgrades a v2-v4 intent
ledger to schema5 while preserving prior protections/accounts. Read-only,
default-off and dry-run do not migrate it. Older readers reject5; no stale-ledger
restore, forced marker/claim clear or unproved replay is a rollback mechanism.
Schema6 and the proposed maintenance-transition executor are not included.

The reviewed automatic publisher supplies the same six-asset offline deployment
contract. Ops uses the single reversible read-only puller and checks the actual
installed version before the bounded postdeployment observation. Future unattended
M2 changes still require approval; this publication changes no model routing,
TTL/reaper or fixed CLI binds. Missing native origin/mapping may still yield
unknown container attribution (#170). Copy depends on terminal clipboard support;
Save text is the portable fallback. User-visible copy/export availability is
checked on the installed release, not inferred merely from a merged PR.

Live post-fix behavior is measured separately from candidate tests. Long-term
stability/calibration remains **NOT MEASURED**; no hour/day/week waiting claim or
zero-loss/quiet/settlement promise is inferred from a silent event connection.

## 0.1.0-alpha.9 — 2026-09-10

Incremental preview; Python distribution `0.1.0a9`.
Includes #171, #172, #173, #176 and #178 since the immutable alpha.8 tag.

### Added and corrected

- The TUI updates keyed model cells and ordered event batches incrementally,
  preserving selection and avoiding heartbeat log spam (#172). Compact GPU bars,
  a single status line and measured elapsed time replace ambiguous progress.
  Stages are labelled observed; configured cold-start totals are not live ETA.
  In the recorded synthetic loopback/real-PTY 100x30 run, 60 seconds used 0.2333%
  of one CPU core, key-to-PTY output was 14.92–28.22 ms and steady-state table
  clears were zero. This excludes remote display latency; visual confirmation
  and deployed acceptance remain separate.
- Trusted successful main CI can publish an approved release PR automatically
  (#171). Exact Fable/head CI, merge-tree identity, version and PR-count gates
  precede building; publication is serialized and immutable, and downloaded
  assets are verified before a draft becomes public. PR/fork events do not
  publish. Existing releases are never replaced.
- Versioned read-only upgrades and one outbound host release consumer preserve
  site configuration, fixed CLI trampoline/profile mounts and prior environments
  (#173). Candidates install offline, validate configuration/runtime and switch
  only after fresh preflight; failed restart/health checks restore the recorded
  prior state without overwriting unknown operator edits. Existing venv clients
  can finish using their original generation. The legacy installer does not
  handle versioned manifests.
- Upgrade preflight accepts the explicit v252 empty inflight snapshot when its
  empty `requests` list is omitted (#178). Missing snapshots, null/wrong types
  and nonempty requests still block. This is a scheduler-restart observation,
  not proof of continuous quiet for model/configuration reload.
- The relay regression now deliberately exercises a real reconnect while proving
  every stream belongs to the original worker and local dispatch creates none
  (#176). Both terminal sizes retain provenance, redaction and cleanup checks;
  production timeouts and the shared fixture are unchanged.

### Compatibility and limits

The release adds `deployment.tar.gz` to the existing wheel, sdist, standalone
`llm`, manifest and checksums. It includes schema-1 deployment metadata, fixed
bootstrap pip and resolved CPython3.10/Linux-x86_64 runtime/TUI wheels, verified
through offline installs without ensurepip. Upgrade manifests use schema2;
this is distinct from the scheduler intent ledger, whose existing lazy
schema-v4 recovery contract is unchanged by this batch. No new project dependency.

Read-only automatic upgrades and the current reviewed reversible rollout are
authorized under #169. Future unattended M2 TTL/reaper/launcher changes still
require approval. Actual tag-to-installed-version and live rollback evidence
remain #169 acceptance, not an implication of staging or publication. The first
preflight refusal is preserved; no forced bypass is introduced.

Registry/catalog submission and schema5 in draft #175 are not part of this batch.
Actual add/rm writes remain disabled without their separately implemented trusted
capabilities. Quiet/adoption/independent settlement and protected-resource gates
remain real requirements. No inference endpoint, model ID, TTL/reaper or shared
file-bind change is performed by publishing this release. Live model latency and
long-term stability/calibration are **NOT MEASURED**; bounded tests have no
mandatory day/week waiting period.

## 0.1.0-alpha.8 — 2026-09-09

Incremental preview; Python distribution `0.1.0a8`.
Includes #154, #156, #163, #162 and #164 since the immutable alpha.7 tag.

### Added and corrected

- Read-only registry queue/recovery status is available through scheduler HTTP,
  standalone CLI and TUI (#154). Existing jobs, process-local progress, restart
  nulls and recovery blockers are reported without proof submission, worker
  activation, reconciliation or fence clearing.
- Model lists expose configuration inventory and add/rm previews expose bounded,
  whitelisted plans (#156). Planned ports and digests are not reservations or
  adopted runtime state; unknown observations remain unknown. Model records and
  inventory now share one captured config read (#162), preventing mixed-generation
  responses during an external replacement.
- Protected sleeping recovery is implemented behind an independent default-off
  opt-in (#164). Known-unused sleepers may retire; recent-use sleepers relocate
  only after a feasible different-GPU and cold-RAM preflight using real profile
  floors and pending start weights. Ordinary default/pin/inflight protections
  remain. Destination admission is rechecked after probes, and its lease is
  atomically bound to the durable recovery claim through existing placement.
- Recovery stop/unload/cold-wake shares one finite budget, at most 900 seconds;
  placement remains capped at 120 seconds within that budget. Recovery HTTP(S)
  requires a prepared literal-IP origin, no DNS/proxy/redirect and an absolute
  socket watchdog. Manual free/wake transport compatibility is unchanged.
- Relay UI tests now distinguish a later malformed-event error from an earlier
  timeout by reason and event ID (#163). Deterministic failure/repair coverage
  preserves both errors and provenance without relaxing production deadlines;
  the historical timeout cause remains unmeasured.

### Compatibility and limits

No new dependency. The first actual ordinary recovery claim lazily and atomically
upgrades a v2/v3 ledger to schema v4 while preserving pins, reservations, leases
and fault records. Default-off/read-only/dry-run does not perform this migration.
Older schema-v3 binaries reject v4; do not delete claims, change the schema number
or restore a stale ledger to force binary rollback.

Submitted but unacknowledged or ambiguous recovery operations remain durably
fenced across restart/disable. Restart may reconcile proven observations but
never replays old stop/unload/wake effects. A successful HTTP response alone is
not settlement or confirmed destination readiness. Callback and storage stalls
are not made preemptible by the network deadline. See `llmsvc/RECOVERY.md` and
`docs/OPERATIONS.md` before planning any separately authorized activation.

Actual registry writes remain disabled (405). Read-only inventory/planning does
not install a runtime model catalog or prove quiet, native adoption or independent
old-resource settlement. No production routing, source upgrade, TTL/reaper,
model action, observer or legacy retirement is performed by this release.
Live recovery latency and long-term stability/calibration are **NOT MEASURED**;
bounded offline/loopback tests are not production acceptance.

## 0.1.0-alpha.7 — 2026-09-09

Incremental preview; Python distribution `0.1.0a7`.
Includes #148, #146, #150, #142 and #151 since the immutable alpha.6 tag.

### Added and corrected

- Configured temporary-model listing and add/rm previews now connect the actual
  registry to scheduler HTTP and the standalone CLI/TUI (#142). Lists return
  temporary metadata; previews retain global disabled-write, unknown-quiet,
  admission and fault blockers. Unconfigured registry reports 503, unsafe sources
  503, invalid requests 400 and pending-marker previews 409. Actual registration
  writes remain 405 in every mode; no reload worker or placeholder success is
  activated. The default example leaves registry configuration commented out.
- Registry source reads have finite allocation caps, regular-file/no-follow
  descriptor checks and identity revalidation (#142). Default YAML/model-config
  caps are 1 MiB and the weight-index cap is 8 MiB; each configured limit must be
  an integer from 1 byte through 16 MiB. Canonical configured paths are required;
  confined model-cache links retain their documented resolution behavior. These
  are allocation/special-file guards, not a wall-clock bound on stalled storage.
- Source IP parsing shares the core canonicalization contract (#148): IPv6 and
  IPv4-mapped addresses map consistently; malformed or absent origins remain
  unknown while valid request/token totals survive. Conflicting equivalent owner
  mappings are rejected. Historical origins are never reconstructed.
- Python registry inventory/projected-preview methods report configuration and
  observed state separately (#146). Planned ports/digests are not reservations
  or adoption proofs; the richer HTTP/UI detail envelope remains future work.
- Pure generation-candidate planning preserves unrelated YAML bytes and binds
  the planned candidate digest to supplied identity data (#151). Caller-supplied
  nonce/identity is not proof of historical uniqueness or current process identity.
  No file, queue, network, validator or notifier effect occurs.
- Shared lease tests give positive grants functional headroom and explicitly
  retain short negative deadlines (#150). Deterministic pre-decision delay proves
  the fixture failure boundary; production timeouts/protection/accounting are
  unchanged and the historical host delay is not claimed as measured.

### Compatibility and limits

No new dependency or database migration. Existing opt-ins, lazy fault schema-v3
and unknown-unload fences remain. The temporary registry list is not the
OpenAI data-plane model list or proof a configuration was adopted. Add/rm
previews do not create jobs, stage files, emit scheduler events or perform model
actions. The closed stacked #147 status feature and #152 detail expansion are
not included in this release; there is no HTTP proof/reconcile/force-clear path.

The source-reader fix supports source-bearing data, but installing it cannot
supply origins absent from the deployed v252 producer. Candidate producer tests
and any future source/service/store transition remain separate from publication.
Reliable quiet, native adoption and independent old-resource settlement still
require their actual evidence. No production source upgrade, routing, TTL/reaper,
host bind, observer, model action or legacy retirement is performed here.
Unmeasured live behavior and long-term stability/calibration remain explicitly
**NOT MEASURED**; no day/week calendar wait is a development gate.

## 0.1.0-alpha.6 — 2026-09-09

Incremental preview; Python distribution `0.1.0a6`.
Includes #134, #136, #138, #139 and #140 since the immutable alpha.5 tag.

### Added and corrected

- `unreserve ID` is available in the standalone CLI and TUI (#138), using the
  existing idempotent reservation DELETE API. IDs are encoded, preview is
  write-free, and a lost reply is never automatically retried. Successful removal
  refreshes the reservation markers without waking or restarting a model.
- The README now leads with released user/admin workflows and distinguishes the
  scheduler from the inference endpoint, keeping legacy instructions in a
  historical appendix (#134). The legacy client example requires an explicit
  environment-provided endpoint and fails before SDK import when absent (#136).
- Ops can generate a synthetic zero-delta LoRA fixture and run a bounded
  mechanics harness (#139). The installed vLLM CPU loader accepted the fixture;
  no GPU model was started because the recorded preflight was busy. Adapter-name
  requests after sleep/wake are required by the harness; list visibility alone
  is insufficient. Synthetic fixtures make no tuning-quality claim.
- Python registry APIs expose detached queue snapshots and bounded persisted
  recovery inspection (#140). Restarted objects retain marker diagnostics while
  vanished in-memory queues/timing stay unknown. Native generation visibility
  alone never establishes settlement or removes a fence.

### Compatibility and limits

No new dependency or database migration. The experimental reload verifier now
requires an explicit marker-bound `RecoveryProof`; a bool or partial/truthy
inspection result cannot clear recovery state. There is no current production
caller and no new proof source, HTTP job/reconcile endpoint or reload worker.
Marker reads reject multiple hardlinks; hardlink-based backup tooling can leave
inspection fenced. Preserve the authoritative marker and accounting; this is
not permission to remove evidence or force recovery.

Existing lazy fault-claim schema-v3 and unknown-unload fence restrictions from
alpha.5 remain. All default read-only and separate intent/action/automation/fault
opt-ins are unchanged; publishing does not activate any of them. The LoRA GPU
load/sleep/wake retention, memory increment and request latency are **NOT
MEASURED**; CPU loading is not serving or llama-swap alias-routing proof.

Configured registry list/add/rm HTTP/CLI integration is still under development
and is not included in this batch. Reliable quiet, adoption and old-resource
settlement remain requirements for real configuration changes. Real newcomer
onboarding, production rollout/retirement and owner consent remain separately
tracked. No production migration, routing, TTL/reaper, host bind, observer or
model action is performed by this release. Long-term stability/calibration are
**NOT MEASURED**, with no mandatory day/week calendar waits.

## 0.1.0-alpha.5 — 2026-09-09

Incremental preview; Python distribution `0.1.0a5`.
Includes #123, #126, #128, #129 and #131 since the immutable alpha.4 tag.

### Added and corrected

- Optional per-GPU pressure/idle cycles (#126) retain the default fixed-idle
  policy, prioritize memory pressure and select exactly one TTL planner. The
  candidate exclusive/shared thresholds are configurable, not calibrated facts.
- Default-off recovery for proven managed faults (#131) binds fresh bounded
  observations to confirmed lease/unit/incarnation identity. A durable claim
  precedes stop; positive exit proof precedes atomic account release. Pins and
  ordinary healthy-model protections remain intact. Restart does not replay
  stale stops, and the retained claim blocks conflicting same-model operations.
- The free-controller adapter now uses explicit eligibility exclusions (#129),
  retaining real user-pin provenance and independent exclusion reasons.
- A held 32-client CPU harness and per-model concurrency configuration guidance
  (#123) distinguish admitted streams from reused slots. These fixtures do not
  establish real vLLM throughput or authorize live configuration changes.
- Victim-fixture setup receives functional timing headroom before its initial
  grant (#128), with setup diagnostics and explicit busy/idle event barriers.
  Production deadlines and dedicated short-deadline negative tests are unchanged;
  the historical CI scheduling delay was not measured.

### Compatibility and limits

No new dependency. Existing read-only and independent action/automation gates
remain. Fault recovery additionally requires its own explicit opt-in. Its 1 Hz
worker requests the whole existing sampler: confirm actual collection rounds
remain reliably below 2 seconds before separately authorized activation. Slow
or gapped evidence resets recovery decisions; the increased probe load is not
proof of source continuity or reliable quiet.

The first actual fault claim lazily and atomically migrates the ledger from v2
to v3. Default-off/read-only startup and dry-run do not perform this migration.
Older v2 binaries reject v3. Preserve the complete ledger, pins and pending
claims plus a consistent backup; rollback requires current-resource/identity
reconciliation. Never overwrite active accounting with a stale backup or remove
claim rows to bypass a fence. See [fault rollback guidance](docs/OPERATIONS.md#fault-fences-and-ledger-rollback-130).

Each claim submits unload at most once, with a durable marker before HTTP.
Timely, identity-checked 2xx acknowledgment still requires two fresh stopped/exit
observations. Unacknowledged, crashed, rejected or timed-out submissions retain
the model fence across restart/disable: this version supplies no forced-clear,
retry-based recovery or positive-settlement/owner protocol. Availability may
remain blocked even after the old process exits; HTTP 200 alone is not recovery.

Actual live fault recovery, per-GPU response latency and long-term stability/
calibration are **NOT MEASURED**. Bounded tests replace calendar waits, not proof.
Orphan adoption, relocation, reliable quiet/adoption/settlement, host-source
configuration, LoRA and operational/owner acceptance remain separate work.
This release performs no production migration, routing, TTL/reaper replacement,
host bind, observer or model action and does not complete a milestone.

## 0.1.0-alpha.4 — 2026-09-09

Incremental preview; Python distribution `0.1.0a4`.
Includes #114, #117, #118, #119 and #122 since the immutable alpha.3 tag.

### Added and corrected

- Default-off automatic memory-pressure and fixed-idle policy cycles (#119).
  Effects require automation and model-action opt-ins, non-read-only mode and
  trusted configured accounts. Each bounded cycle executes one protected action,
  observes confirmed effects/accounting, and replans from fresh state. Cycles do
  not overlap; the default idle threshold is 600 seconds. One-shot operation
  remains read-only and logs a pure preview.
- Placement, reservation and automatic policies use explicit internal exclusion
  reasons instead of synthetic user Pins (#122). Existing actions/ranking and
  full-budget accounting are preserved. Real pin plus independent exclusion now
  reports both reasons without overwriting user pin provenance. The remaining
  free-controller adapter is separately tracked in #121.
- Mounted reserve CLI/TUI integration tests and API guidance (#117) cover real
  temporary HTTP/SQLite complete/blocked/partial outcomes, retained intent IDs,
  expiry/deletion and ambiguous-response no-retry behavior. No new client runtime
  command is added by this validation slice.
- Remaining ops runbook/lifecycle calendar prerequisites are replaced with
  bounded measurements and replay (#114/#118). Capture summaries require the
  actual configured interval; long-term stability/calibration are NOT MEASURED.

### Compatibility and limits

No schema migration or new dependency. Default read-only behavior and independent
intent/action/automation gates remain; publishing or installing enables no model
operation. Automatic sleep retains full accounting and automatic stop needs
positive exit/account proof and two newer observations. Unknown or unleased
models remain blocked; no orphan adoption, forced cleanup or estimated release.

Production TTL/reaper replacement, per-GPU pressure/TTL integration, relocation,
reliable quiet/adoption/settlement, host-source configuration, LoRA and owner
acceptance remain separate work. This release changes no production routing,
TTL/reaper, host bind or observer and does not complete a milestone. Existing
schema-v2 ledger preservation and bounded operational checks still apply.

## 0.1.0-alpha.3 — 2026-09-08

Incremental preview; Python distribution `0.1.0a3`.
Includes #109, #110, #111, #112 and #113 since the immutable alpha.2 tag.

### Added and corrected

- Reserve CLI/TUI input, preview and explicit saved-intent/evacuation outcome
  handling (#110), paired with actual reserve POST/DELETE persistence and
  bounded evacuation of eligible sleeping models (#112). Saved intentions
  exclude the whole GPU while active; blocked/partial evacuation retains the
  record. Preview keeps a hypothetical request label; actual writes attribute
  ownership from the socket peer. Only proved exits enter the stopped list.
- Read-only pinned-v252 native-generation witness with bounded I/O and explicit
  instance/candidate binding (#111). It is not mounted in the scheduler and
  never proves old-server settlement, reliable quiet or an applied reload.
- Offline observation summaries verify captured hashes/schema/source identity
  and report sample coverage, gaps, unknowns and per-GPU external occupancy
  distributions in JSON/CSV (#113). No new observer or model probe is started.
- Bounded minutes-scale validation and deterministic replay replace mandatory
  day/week calendar waits (#109). Long-term stability and calibration are
  **NOT MEASURED**, rather than inferred from a short run.

### Compatibility and limits

The existing schema-v2 ledger remains unchanged. Reserve intent writes require
explicit writable intent mode; actual evacuation also requires the separate
model-action opt-in. Default read-only behavior remains. DELETE removes an
intent without waking/restarting models; expiry/deletion cancels future steps.
HTTP200 reports a saved intent, not necessarily completed evacuation. Clients
must inspect complete/blocked/partial outcomes and must not retry ambiguous POSTs.
Awake/protected/unknown/unleased models stay blocked; no relocation, orphan
adoption or forced cleanup is added.

Native-generation visibility is not old-server settlement and does not satisfy
the reliable five-second quiet requirement. Offline coverage does not establish
continuous uptime or choose production thresholds. Trusted host-memory input,
real action/TTL/reaper integration, LoRA and owner-authorized retirement retain
their applicable acceptance and authorization requirements. Publishing enables
no production routing, TTL/reaper, host bind, observer or model action and
completes no milestone automatically.

## 0.1.0-alpha.2 — 2026-09-08

Incremental preview after alpha.1; Python distribution `0.1.0a2`.
Includes #80, #81, #82, #84, #85, #86, #87, #88, #89, #91, #92, #96,
#97, #98, #99, #101, #102, #103, #104 and #105. The pure release PR #95
is excluded from the twenty-PR batch count.

### Added and corrected

- Standalone and TUI pin/unpin, free and wake commands with authoritative owner
  display, explicit partial/failed outcomes, separate response waits, measured
  release versus estimates, and cancel-focused RAM-free confirmation.
- TUI f/p/w shortcuts prefill commands for explicit submission; pin duration
  stays required and focused input/drafts are preserved.
- Separately opt-in guarded free/wake execution with observed completion and
  fresh memory measurements; late older samples cannot replace newer activity.
- Separately opt-in direct-fit placement, durable model-unique leases,
  confirm/release endpoints, restart reconciliation and bounded admission waits.
- Placement can stop one policy-selected, durably accounted victim, confirm
  observed exit/account release and replan before a grant when both placement
  and model-action opt-ins are enabled (#99). Unknown or unleased daemons remain
  protected; the overall wait stays bounded.
- Retained leases with missing or changed trusted model identities emit a
  diagnostic, preserving budget and pin. Restoring verified original identity
  and reopening the same ledger enables existing reconciliation (#96).
- Bounded cached-base lifecycle tooling and measured isolated evidence (#80);
  current directory/partial-acceptance guidance (#86).
- Pinned v252 watcher-only native-generation fixture and measured records (#91),
  plus single-trigger adoption/settlement design wording (#87). These do not
  enable a notifier: generation visibility and generic completion logs do not
  establish old-server settlement.
- Bounded sanitized data-plane relay (#92) and opt-in scheduler SSE bridge
  (#102). Native and data-plane events share one scheduler stream with explicit
  provenance, global IDs and separate local-discard reasons. CLI and TUI
  end-to-end CPU fixtures and presentation are included (#103/#104); upstream
  loss stays unknown and real event-latency acceptance remains pending.
- Trusted-identity recovery and launcher budget/error handling documentation
  (#98/#101), including bounded scheduler maintenance while TTL is zero.
- RAM-confirmation regression assertions tolerate background state polls while
  proving exactly one confirmed write and a subsequent refresh (#97).
- Correct overly tight placement-victim test deadlines and add diagnostic
  assertions and deterministic regressions proving exit reconciliation and two
  fresh observations before a new lease (#105). Production deadlines and
  protection rules are unchanged; the original pre-publication CI failure is
  retained under #100.
- Release cadence now follows batches of five merged non-release-only PRs,
  without a daily cap or complete-feature-group prerequisite (#90).

### Upgrade and rollback

Writable intent-store opens migrate schema v1 to v2 while preserving pins and
reservations. Read-only v1 opens do not migrate. Older binaries reject v2:
back up the database before upgrade and reconcile live allocations before any
rollback; never delete an active allocation ledger to downgrade. Keep allocated
models in the trusted collector configuration until their accounts can be
safely reconciled. Removing one can block admission with an unobserved lease;
restore the verified original mapping and reconcile the same ledger. The full
operational runbook is included in #98/#101; this release authorizes no restart
or live recovery.

### Experimental and incomplete

Read-only remains the default. Model actions and placement have separate,
default-off opt-ins; installation or publication enables neither. Placement of
durably accounted victims is available for evaluation; unleased daemon handling,
orphan cleanup, reserve evacuation and proven fault cleanup
remain incomplete. Live launcher/latency/release acceptance is still pending.

Dual-source event delivery is available behind a default-off server option;
actual production event latency remains unverified. Native reload adoption/
old-server settlement, reliable quiet evidence and LoRA acceptance remain
incomplete. Automatic reload stays
disabled. The isolated base-model result does not certify these requirements.
Required day/week observations, administrator/threshold decisions, production
rollout and owner-approved legacy retirement remain separate gates. No milestone
is completed by this alpha and no production observer is started.

## 0.1.0-alpha.1 — 2026-09-08

First preview of the llama-swap control plane. Python distribution version:
`0.1.0a1`. This release is for read-only evaluation; it does not complete the
M0–M6 roadmap or authorize production activation.

### Available for evaluation

- Python 3.10+ scheduler with bounded telemetry, state snapshots, scheduler
  events, and source-aware activity/usage aggregation.
- Standalone standard-library `llm status` and `llm usage`, including JSON and
  narrow-terminal output. Optional Textual dashboard, usage view and event
  reconnection; non-TTY and missing-Textual fallback remain supported.
- Pure protection, memory admission, feasible placement and pressure policies
  with offline replay coverage; staged deployment and capture tooling.
- Fixes for TUI teardown/late responses (#73) and separation of semantic usage
  fixtures from the unchanged production read deadline (#75).

### Experimental and incomplete

- Intent storage, policy previews, opt-in pin writes, guarded request dispatch,
  and registry/reload planning are included as implementation components.
  Complete live actuation, observed resource release, placement leases and
  cold-start/wake integration are still being developed.
- Automatic configuration reload remains disabled until trustworthy continuous
  in-flight observations and configuration-adoption evidence are established.
- TUI commands remain read-only. Data-plane/scheduler event unification, live
  acceptance and optional LoRA evidence are not completed by this preview.
- Production changes, required day/week observations, administrator settings
  and legacy retirement keep their existing acceptance gates. Legacy code is
  still present. No service is switched by publishing or installing this release.

Release process and cadence: [docs/RELEASING.md](docs/RELEASING.md).

<!-- Generated-By: Codex / gpt-6-astra -->
<!-- Generated-By: Codex / gpt-5.6-luna -->
<!-- Generated-By: OpenCode / deepseek-v4.1-flash -->
