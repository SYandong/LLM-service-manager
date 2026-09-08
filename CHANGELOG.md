# Changelog

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
