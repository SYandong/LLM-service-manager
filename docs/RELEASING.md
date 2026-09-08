# Releases

## Cadence and ownership

The user authorized `chore(release)` releases and delegated cadence to the
coordinator in #76. Batch a useful, independently verifiable group of changes
into an alpha, normally at most once per calendar day (Asia/Singapore).
A significant regression fix may justify an immediate additional alpha.
Do not publish on every merge or on every timer tick. Unchanged state requires
neither a release nor a model wakeup.

The initial series uses GitHub tags `v0.1.0-alpha.N` and Python distribution
versions `0.1.0aN`. Advance N only for a new immutable release. The first alpha
covers the read-only scheduler/CLI/TUI evaluation path. Subsequent candidates
should add complete protected-action, lease/placement, model-registration or
UI-operation slices with corresponding evidence, rather than just more files.
The first stable `v0.1.0` requires the planned product and environment acceptance
through M6, including observation periods and deployment/retirement gates.
An alpha does not close incomplete milestone issues.

Integration owns subsequent release coordination. A single release owner
prepares the version bump, changelog and artifacts in an isolated worktree.
Other implementation lanes continue their own files; coordinate the three
version literals in `pyproject.toml`, `llmsvc/__init__.py` and `cli/llm`.
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
