# Changelog

## 0.1.0-alpha.2 — 2026-09-08

Incremental preview after alpha.1; Python distribution `0.1.0a2`.
Includes #80, #81, #82, #84, #85, #86, #88 and #89.

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
- Bounded cached-base lifecycle tooling and measured isolated evidence (#80);
  current directory/partial-acceptance guidance (#86).
- Release cadence now follows batches of five merged non-release-only PRs,
  without a daily cap or complete-feature-group prerequisite (#90).

### Upgrade and rollback

Writable intent-store opens migrate schema v1 to v2 while preserving pins and
reservations. Read-only v1 opens do not migrate. Older binaries reject v2:
back up the database before upgrade and reconcile live allocations before any
rollback; never delete an active allocation ledger to downgrade. Keep allocated
models in the trusted collector configuration until their accounts can be
safely reconciled. Removing one can block admission with an unobserved lease;
a dedicated recovery procedure remains follow-up work.

### Experimental and incomplete

Read-only remains the default. Model actions and placement have separate,
default-off opt-ins; installation or publication enables neither. Placement
requiring eviction, orphan cleanup, reserve evacuation and proven fault cleanup
remain incomplete. Live launcher/latency/release acceptance is still pending.

Dual-source event relay, native reload adoption/old-server settlement, reliable
quiet evidence and LoRA acceptance remain incomplete. Automatic reload stays
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
