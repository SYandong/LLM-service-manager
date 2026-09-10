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
   idle-GPU test gate. Run the complete current `tests` suite on Python 3.10;
   the retired proxy's live tests are no longer part of it. Older release
   receipts retain their historical exclusions; do not copy those exclusions
   into current validation. Model-running deployment harnesses remain separately
   owned by ops and require their actual idle/protection gates.
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

## Automated publication and deployment (#169)

The current user authorization permits automatic read-only upgrades and the
reviewed, rehearsed reversible maintenance rollout. Ops owns the single outbound
host deployment consumer, staging, current site configuration, health checks and
rollback; integration owns release preparation and this publisher. Preserve
existing inference endpoints, model compatibility and the file-bind `llm`
trampoline/profile mounts. Future unattended M2 changes to TTL/reaper/launcher
still require approval. Actual protection, quiet, adoption and settlement proof
conditions remain; authorization is not evidence that they hold.

`.github/workflows/release.yml` runs after **successful `ci` push runs on this
repository's main branch**. It never publishes for PR/fork events, tag pushes or
ordinary feature merges. The publisher re-fetches the CI run and checks the
unique merged `chore(release): v0.1.0-alpha.N` PR, exact final-head Fable marker
and reviewer identity, successful head CI, resolved review threads, identical
merge/head trees, version literals and changelog. Checkout credentials are not
persisted. One concurrency group serializes publication; the workflow uses only
GitHub's scoped token and never obtains host deployment credentials.

Prepare the next release PR after five qualifying merged PRs. The publisher
recounts first-parent PR merges from the previous published alpha tag; release
maintenance does not count. A reviewed release PR may explicitly record an early
release as `Release-Exception: #ISSUE — concrete reason`. Integration must tie
that exception to the user's authority and actual urgent/delivery need; elapsed
time or a completed wave is not an exception. There remains one candidate and
one publisher. Once this workflow is active, integration does not race it with a
second tag or release process.

The workflow builds on CPython 3.10 / Linux x86_64, re-builds the wheel from the
sdist and compares every wheel entry. It collects binary runtime/TUI dependency
wheels, then installs both minimal and TUI variants **offline** in separate venvs,
checks dependencies/versions, imports TUI outside the checkout and runs copied
CLI with `-I -S`. The successful exact merge CI supplies the full test evidence;
this packaging stage does not repeat that suite or run live model tests.

New automated releases have six assets: wheel, sdist, `llm`,
`deployment.tar.gz`, `release-manifest.json` and `SHA256SUMS`. The deployment bundle holds `deployment.json`, `llm` and
`wheelhouse/*.whl`, including this release, bootstrap pip 26.2.1 and its resolved
runtime/TUI wheels;
the manifest records each member hash and the target Python/platform. Deployment
must validate the entire checksum/manifest set and safely extract only those
members before `pip install --no-index --find-links ...`. The nested `deployment.json` uses schema_version 1 with tag/version/commit,
`scope: read_only`, app_wheel/cli/bootstrap_pip relative paths, install_wheels
(excluding bootstrap pip) and a files map of every payload's SHA-256. The outer
asset checksum authenticates this metadata; it cannot authenticate itself. Scope
specifies the permitted upgrade mode, not proof that the current site is read-only:
ops checks the actual configuration before applying. Create the venv without pip,
bootstrap through the shipped pip wheel and install the explicit install_wheels
with `--no-deps --no-index`; `pip check` verifies closure. Dependency upgrades are
not applied to the existing environment in place. Older releases (including
alpha.8's completed five-asset publication) remain immutable and are not silently
retrofitted with a wheelhouse.

`release-manifest.json` binds tag, Python version, exact merge, reviewed head,
Fable review URL, merge CI URL, previous baseline and qualifying PRs. Its `assets`
map contains payload byte lengths and SHA-256 values; `SHA256SUMS` covers those
payloads plus the manifest. After upload, the publisher downloads and verifies
all six assets before making the draft public. The tag is annotated and never
moved. A repeat run verifies an already-public release and exits without writes;
a complete matching draft can finish publication without rebuilding. A partial
or conflicting draft fails closed: restore missing bytes from the retained build
artifact after inspection, then rerun. Never overwrite existing uploaded bytes
with a fresh build or delete a conflicting tag to make the job green.

The host consumer polls published prereleases, not GitHub Actions events; releases
created with `GITHUB_TOKEN` need not trigger another workflow. It must ignore
drafts, verify the manifest/tag/asset contract and run its staging, fresh use check,
health and rollback sequence. A published tag is not automatic permission for
unattended M2 changes. #169 remains open until staging and actual upgrade/rollback
plus a real tag-to-`llm --version` transition have measured receipts. #168 visual
acceptance separately requires the user's screenshot/PTY feedback.

GitHub Releases and the above authorized deployment are in scope. PyPI uploads,
unrelated process shutdowns and group announcements remain separate. Publication
itself changes no site settings or production processes.

<!-- Generated-By: Codex / gpt-6-astra -->
