# Releases

## Cadence and ownership

The user authorized GitHub prereleases and PR-count batching in #90.
Prepare one alpha whenever **five qualifying PRs** have merged into main since
its last published tag commit. Count feature, fix, test, documentation and
maintenance PRs; exclude pure `chore(release)` version/changelog maintenance PRs
so publication cannot trigger itself. Recount against the actual remote tag
and main, not a stale status snapshot.

There is no daily cap, date delay, complete-feature-group or milestone
prerequisite. An alpha may be an incremental snapshot with explicit limits.
Urgent fixes may release below five PRs with the reason recorded by integration.
Keep at most one candidate in flight; continue that candidate instead of opening
a duplicate. Additional merged PRs included in its actual release commit belong
to the same batch. After publication, reset the counting baseline to that exact
tag commit. Unchanged state requires neither a release nor a model wakeup.

The initial series uses GitHub tags `v0.1.0-alpha.N` and Python distribution
versions `0.1.0aN`. Advance N only for a new immutable release. The first alpha
covers the read-only scheduler/CLI/TUI evaluation path. Later alphas describe
newly included behavior and remaining acceptance without implying completion.
The first stable `v0.1.0` requires the planned product and environment acceptance
through M6, including bounded validation and deployment/retirement gates.
An alpha does not close incomplete milestone issues.

Per the user's #108 instruction, validation uses bounded minutes-scale checks
and deterministic regression/replay rather than mandatory day/week soak waits.
Record the measured window and unmeasured long-term behavior explicitly; do not
use elapsed calendar time as a release gate or claim short tests prove long-term
stability. Existing correctness, Fable/CI and relevant operational gates remain.

Integration owns subsequent release coordination. A single release owner
prepares the version bump, changelog and artifacts in an isolated worktree.
Other implementation lanes continue their own files; coordinate the three
version literals in `pyproject.toml`, `llmsvc/__init__.py` and `cli/llm`.
The release PR also updates README release-page links, wheel filenames and
command availability to match the included code, coordinating these narrow
changes with the README owner. Do not describe an unmerged API as released.
Root owns the initial #76 release. Use a `chore/<issue>-release-...` branch and a
`chore(release): ...` commit/PR title under this explicit user authorization.

## Publication gate

1. Create a scoped issue and release PR. Describe supported behavior,
   experimental components and incomplete acceptance precisely in CHANGELOG.
   The initial release depends on merged #73 and #75.
2. Run the offline suite and package/CLI/TUI checks on the release candidate.
   Local tests must not contact a live model service without the existing
   idle-GPU test gate. `tests/test_smoke.py` is a live service suite; excluding it
   from local offline verification must be disclosed. The normal PR CI remains
   unchanged and runs its complete suite on Python 3.10.
3. Require explicit Fable approval of the current release PR head, current
   successful CI and resolved blockers. Apply the ROADMAP's squash/commit
   verification rules. New commits invalidate prior approval.
4. Pin the merged release commit. Build wheel and sdist from a clean checkout
   of that commit using the project's setuptools build backend. Verify the
   wheel installs and reports the same version through `llmsvc`,
   `llmsvc-scheduler --version` and `llm --version`; import the installed optional
   TUI outside the checkout. Rebuild the wheel from the sdist and verify its
   installed entry points too.
5. Create an annotated tag pointing at that exact reviewed commit, not whatever
   `main` points to later. Publish a GitHub prerelease with the wheel, sdist,
   standalone `llm` script, `SHA256SUMS` and a release manifest identifying the
   commit, versions and validation evidence. Do not tag an unreviewed worktree.
6. Verify the remote tag, prerelease flag and uploaded asset names/checksums.
   Record the release URL and completed version in the coordination issue.
   If publication is interrupted, inspect the existing tag/release and finish
   missing assets; never overwrite an existing published version with new code.

This authorization covers this repository's GitHub Releases. PyPI publishing,
production rollout, model/process shutdowns and announcements to a group are
separate actions and retain their existing authority and acceptance requirements.
Installation remains read-only by default; publishing does not enable mutation
flags, change llama-swap/reaper settings or start a production observer.

<!-- Generated-By: Codex / gpt-6-astra -->
