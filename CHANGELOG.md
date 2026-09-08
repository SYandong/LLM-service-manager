# Changelog

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
