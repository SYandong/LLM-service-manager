# Fleet rollout and evidence (#310)

This runbook prepares a separately authorized rollout of the opt-in fleet
observer and the placement 503 fix. It records what was measured. A passing
fixture or an available release is not a completed site deployment.

## Prepared artifacts

Record the reviewed commit and current CI for #303–#309, the release/bundle
hashes, patched llama-swap build `v252-llmsvc.3`, launcher hash, scanner/config
hashes and the previous runtime/build needed for rollback. Keep site addresses,
container labels, raw snapshots and credentials in private evidence. Public
issues contain anonymized summaries and hashes, not those raw files.

Use the existing release generation and shared `llm` trampoline. Keep its file
mount and inference endpoints. A feature release follows RELEASING; don't label
new code as a published version until the publisher has verified the actual
release. Do not tag manually or race the publisher. The ordinary read-only
upgrader neither installs the host scanner nor replaces llama-swap; it does not
enable fleet in the operator-owned YAML.

Changing the native image also changes any pinned image/profile binding. The
candidate and retained image must be supported by the reviewed site transition
and provenance checks. Do not replace the executable under an unchanged hash,
edit a recovery fence or assume the maintenance preflight implements apply.
If the selected site's writable replacement path is unsupported, record it as
blocked and retain the current running instance.

## Disposable rehearsal

1. Run the complete Python 3.10 suite, Go patch tests and the CPU-only actual
   patched-swap/unmodified-official-wrapper fixture. Retain 503 status/header/body/elapsed time,
   transient 409 control and normal startup evidence. Test loading SSE separately:
   once HTTP headers are committed, its error is a frame, not a new HTTP status.
   Include the wrapper's journal-forwarder case: an inherited output pipe must
   not add the upstream ten-second drain delay to the refusal.
2. Run the host scanner against injected proc/GPU/metrics fixtures. Rehearse its
   installer/uninstaller in a disposable root; inspect the exact files and
   timer plan. A dry-run must not create files or execute systemctl.
3. Enable fleet only in an isolated scheduler with sanitized snapshots and an
   independent temporary database. Check schema rejection, failed/partial discovery,
   stale/gap/reset behavior, history, owner restrictions and claim dry-run.
   Include 21k samples in the performance check; preserve elapsed time and row
   counts. Do not request or start an actual model for this rehearsal.
4. Exercise the CLI and new TUI at 100×30 and 80×24. Verify the copied standard-
   library CLI, `status --shared --json`, optional installed TUI imports and
   the retained `legacy-tui`. Verify read-only upgrade health checks use the
   explicit shared view when fleet is disabled.

## Site activation

Prepare the concrete site paths, exact unit/profile/current instance, consistent
backups and rollback before requesting activation authority. Record that authority
with the operation in #310. These steps do not authorize an operation merely
because its command appears below.

Install only the reviewed scanner and its unit/timer. Reuse the existing host
export directory and read-only mount; do not widen listeners or change another
container's files. A scanner's successful exit must be followed by a fresh,
complete, bounded snapshot check in both host and observing container.

```sh
# After the specific installation has been authorized and paths verified.
systemctl enable --now llmsvc-fleet-scan.timer
systemctl is-enabled llmsvc-fleet-scan.timer
systemctl is-active llmsvc-fleet-scan.timer
systemctl list-timers --all llmsvc-fleet-scan.timer
```

Both enablement and activation must be true; starting alone is insufficient.
Record the unit/config hashes and next scheduled run, and check the new file's
timestamp advances across two runs. Verify completeness flags, ownership and
0644 output. A running timer, an empty process list inside a container or a
successful GET alone is insufficient evidence of full host discovery.

Enable `fleet_enabled` in the explicitly approved operator configuration with
its independent DB and snapshot paths; keep model action/placement/automation
settings and scheduler ledger unchanged. Claims are independent fleet metadata
and require the configured claims flag plus fresh direct peer-to-container
mapping even in a read-only model scheduler. Disable `fleet_claims_enabled`
during initial observation if declarations are not yet authorized.

For A3, switch only in the authorized native maintenance window after fresh
in-flight/instance/protection/rollback checks. Binary replacement is a separate
operation from Python runtime upgrades and requires its own reviewed path.
Keep the previous executable and launcher hash; verify the new live image, not
just a file on disk. Test one unloaded synthetic/requested model only when
the site conditions and operation authority permit it; retain actual latency,
503/Retry-After/code and the normal/transient controls. Don't create GPU pressure
by interrupting existing work.

## Three-day shadow record

The current fleet plan requests at least 72 hours of unannounced observation.
This is M7 site acceptance; it does not delay local implementation or replace
the deterministic checks above. Start/end, every daily check and every missing
interval must be recorded. No shadow run is claimed by this runbook.

| Observation | Private evidence | Public summary |
|---|---|---|
| Start/end and elapsed period | UTC timestamps, reviewed runtime/config hashes | elapsed hours, commit, hashes |
| Timer persistence | is-enabled/is-active, next run, authorized reboot if separately allowed | enabled/active and tested scope |
| Discovered inference services | fresh host proc/GPU and snapshot association with PID/start | matched count, unknown count, exceptions |
| Loopback endpoints | bound listener/netns identity and bounded metrics result | matched count and scrape failures |
| GPU ownership | host compute query, complete attribution flags, snapshot | per-card totals, unaccounted memory |
| Counter reconciliation | two raw metrics samples, interval and baseline/epoch | requests/token differences and error |
| Stale detection | approved timer pause, retained generated_at, API/banner | timeout and recovery time; no inference change |
| Gaps/resets/partial inventory | missing periods, completeness, restart ticks and counters | coverage, unknown states and repairs |
| Claims | private direct IP mapping age and own/other/dry-run checks | permissions and zero-write preview results |

Compare only the same service incarnation, metric labels and exact sample pair.
For each nonzero reference delta, relative error is
`abs(ingested_delta - reference_delta) / abs(reference_delta)` and must be below
2%. Zero references require exact zero; report resets and missing baselines
separately, not as zero error. A difference over a five-minute gap can establish
flow totals but cannot establish active minutes throughout that gap. A recorded
active minute is sampled activity, not continuously measured utilization.

Check at the beginning and on each of three subsequent days, preserving the
full interval and any gaps. If the scanner is paused for the stale test, record
authorization, pause/resume times and restored enablement; it controls no model
process. Failures remain explicit until repaired and measured again. Group
announcement is deferred until the record is accepted by the maintainer.

## Rollback and remaining decisions

Fleet rollback stops/disables only its owned scanner timer and disables fleet
ingestion/claims through the authorized scheduler configuration transition.
Retain the independent DB, claims and snapshots for reconciliation; do not
restore an old scheduler ledger or stop observed workloads. Keep the existing
IP-export timer/mount. The installer may remove only unchanged owned files and
must preserve modified files and evidence. Verify previous timer enablement
before reversing installation.

Runtime rollback uses the retained generation and existing reviewed transaction
protocol. Native/launcher rollback uses the separately prepared site procedure
with the same fresh identity/quiet/protection gates. A stale backup is not
current account state. A failed or unknown side effect remains unresolved;
do not clear fences or replay a start/stop to make the record look complete.

The next-minor legacy TUI deletion is #311 and needs zero-use evidence or
maintainer confirmation. #28 target rewriting and #168 closure remain decisions;
this rollout observes and coordinates, and does not retire another user's
service. The [announcement](../docs/FLEET_ANNOUNCEMENT.md) is a draft only and
makes no deployment claim.

<!-- Generated-By: Codex / unknown model -->
