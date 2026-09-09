# Observation and isolated smoke preparation

Refs #8, #20 and #21. This preparation consumes the merged scheduler/CLI and the
merged telemetry #41 and intent/preview #50 contracts (integrated source
`e80b550`). It does not activate a service,
mount a host file, replace production configuration, or start an observer.

## Offline observation summaries (#8 / #16)

Current validation uses bounded, minutes-scale checks and deterministic replay;
there is no mandatory day/week development, release or completion wait. Historical
long-duration plans below are not current calendar gates. Long-term stability and
threshold calibration remain **NOT MEASURED**, and publishing or analyzing a
summary grants no producer/timer, GPU, host-mount or production authority.

`summarize_observation.py` reads completed capture snapshot/manifest pairs without
network, subprocesses, probes or writes to the input directory. It uses only the
Python 3.10 standard library. All implementation, deterministic fixtures and usage
ship together; it does not require an observer to be activated first.

```sh
# Preview the exact JSON report: validates input but creates no output file.
python3 deploy/summarize_observation.py --input-dir /path/to/finished-capture \
  --interval-seconds 15 --output /path/to/private/summary.json --dry-run
# Write one new private JSON file, preserving the original artifacts.
python3 deploy/summarize_observation.py --input-dir /path/to/finished-capture \
  --interval-seconds 15 --output /path/to/private/summary.json
# The same information as a reproducible long-form CSV.
python3 deploy/summarize_observation.py --input-dir /path/to/finished-capture \
  --interval-seconds 15 --format csv --output /path/to/private/summary.csv
# Example two-minute window; replace these with the actual recorded bounds.
python3 deploy/summarize_observation.py --input-dir /path/to/finished-capture \
  --window-start 2026-09-08T12:00:00Z --window-end 2026-09-08T12:02:00Z
```

The output parent must exist. Output files are private (0600), atomically published
and never overwrite an existing file; output within the capture directory is
rejected. Without `--output`, the report goes to stdout. `--dry-run` performs the
same offline reads and prints the report without creating a file. Exit0 means a
report was produced, **not** that its input is complete or accepted; invocation or
output errors return2. Bad individual artifacts remain explicit report entries.
Use a completed/retained directory or a stable copy: concurrent capture writes can
legitimately appear as incomplete snapshot/manifest pairs during analysis.

The immediate directory's `manifest-*.json` records identify snapshot files. The
reader bounds each file to16MiB, rejects traversal/symlink/special-file references,
checks the exact snapshot SHA256, and requires understood schema-v1 non-dry-run
capture records. It reports rejected/missing/corrupt files, wrong hashes and
snapshots without a verified association. The inventory records manifest/snapshot
hashes for understood verified pairs; a matching checksum is integrity evidence
relative to that manifest,
not independent authenticity or proof that the service ran continuously.

Record ordering means **lexicographic manifest filename order**, not filesystem
mtime or inferred arrival order. Repeated references/payloads are counted once;
conflicting captures at the same timestamp are excluded. Timestamps are normalized
to UTC; neither current time nor file mtime enters statistics. The CSV contains
`field,json_value` rows for every JSON leaf, including errors and empty collections.
`field` is an escaped JSON Pointer (for example `/gpus/0/identity`), preserving
source names with punctuation; `json.loads(json_value)` retains type/null distinctions. String values remain
JSON-quoted rather than executable spreadsheet formula cells.

Per-source `valid` means successful, non-truncated capture transport with valid
recorded timing. Missing, failed and truncated sources remain separately counted;
truncation is also a failure, so those counts overlap. Raw commands, stdout,
exception details and model/request payloads are not exported or executed.
For GPU summaries, select the HTTP state source with `--state-source` (default
`scheduler-state`). The command requires its reported URL/type to match capture
provenance. Different source/config identities are reported by opaque hashes and
are not pooled; analyze them separately. This check does not prove an unchanged
runtime version or process behind an unchanged endpoint/config path.

The state source must contain schema-v1 scheduler JSON. Its actual collector
`sampled_at`, rather than merely capture time, controls GPU samples. A state more
than `--max-state-age-seconds` old (default30), or a future/invalid timestamp, is
excluded. Repeated cached collector timestamps do not inflate distributions;
conflicting GPU values at one collector timestamp are excluded. Without explicit
window bounds, distributions retain all fresh state values from accepted captures,
including a cached sample just before capture start or a refresh during its HTTP
request. Explicit bounds filter both capture and collector timestamps. Capture
and usable-state cadence are reported separately against capture bounds (or the
explicit window); coverage may contain fewer points than the distribution. Missing
source data cannot become a measured zero.

Pass `--interval-seconds` from the actual capture schedule (for example, the
configured timer cadence). The defaults15seconds and2seconds tolerance are
assumptions, not detected settings; using the wrong interval distorts missing-
interval estimates. `--tolerance-seconds` must remain less than the interval
and describe expected capture jitter, not hide observed gaps. Coverage reports actual first/last timestamps, observed span, leading/
trailing gaps and internal gaps beyond interval+tolerance. Missing intervals are
**estimates against the configured cadence and window boundaries**, not a count
of proven producer failures. Bounds inferred from the files cannot reveal losses
before the first or after the last capture; explicit bounds make those edges
visible. A complete sampled window still does not prove continuous-running time.
No long-term stability or automatic calibration is inferred, even from a long
span; short data and gaps remain visible rather than being filled or extrapolated.

Per-GPU identity uses UUID when present, otherwise an explicit index fallback.
Index-only identity cannot prove the physical card stayed the same. Each group
reports known/unknown/missing counts and the **reported external GiB** distribution
(min/max/mean, interpolated p50/p95/p99, zero/positive sample counts and histogram).
`--bucket-gib` sets histogram width (default10); unknown/invalid occupancy never
becomes zero. These are sample-weighted values, not duration-weighted exposure or
independently verified foreign-process ownership. They support bounded inspection
and replay inputs, not an automatic #16 threshold or #28 absence conclusion.

## Exact observation configuration

`scheduler.observation.yaml` records the seven configured model identifiers,
direct daemon ports, transient-unit names, util values and default-model flag
from the routing configuration read on 2026-09-08. The scheduler listens on the
alternate loopback port 8011 and remains read-only. Change the bind address only
to an explicitly selected container/private address during an approved install.

Probe binaries, activity database, swap URL, deadlines and per-model endpoints
are explicit. Daemon URLs never use `/upstream`, which could wake a model. The
core and collector RAM thresholds match but remain candidates pending #12
confirmation. `weights_gb` and cold-start estimates are omitted because they
have not been measured; weight-file size is not substituted for sleep RAM.
Empty origin mapping cannot reconstruct missing historical source data.

`host_meminfo_path: null` is intentional. It makes the host-RAM probe unavailable
until the trusted-source proposal below is approved and verified. Never replace
it with container `/proc/meminfo`, a saved `host-memory.json`, or a copied static
meminfo file. The current telemetry factory performs a fresh read but does not
prove mount provenance or reject arbitrary stale files on its own.

The config can be validated in a disposable combined checkout using core's
`--check-config` and the exact telemetry `build_collector(config.collectors)`
factory, closing the collector afterward. Construction/validation does not run
probes. That is configuration validation, not installed-service or production
acceptance. #41 is now merged; installed-runtime and final activation
authority remain separate gates.

## Configured runtime and preview readiness

Merged #50 can expose preview endpoints while remaining read-only. An HTTP 200
or empty `would` list is not proof of a feasible action: inspect `blocked_by`.
With `collectors: {}`, observations are unknown and previews must remain blocked.
Use the configured #41 collector factory for real observations. RAM-dependent
policies additionally need trusted live host memory and measured weight budgets;
never substitute stale samples or container cgroup meminfo to clear a blocker.
Current core conservatively blocks previews on any snapshot collection error.

On **2026-09-08 06:35:58 UTC**, a bounded configured check ran in the service
container using Python 3.10.12, the merged source `e80b550` and PyYAML 6.0.2's pure
Python modules delivered in memory. No package or container file was installed.
A single real collection observed **four GPUs and seven models in 0.538 seconds**.
The ephemeral loopback `/v1/state` JSON and the actual CLI's `status --json`
matched the same fixed sample exactly. Rendered CLI output retained unknown host
RAM and unknown historical origins.

`POST /v1/free?dry_run=1` with `{"ram":true}` returned `would: []` and
`blocked_by: [{model: null, reason: "memory: ValueError"}]`. This is the expected
unconfigured-host-source blocker, not a successful RAM release. Snapshot and
event history were unchanged by the preview. The temporary HTTP server and
collector were closed; no service, store, mount, model workload or persistent
container file was created. This does not prove systemd restart/journal behavior,
installed-package acceptance or long-term stability/calibration.

The initial 0.5-second probe timeout produced one explicit GPU TimeoutExpired;
it was not interpreted as an idle card. The candidate now uses 0.8 seconds per
probe, below the factory's one-second limit, while retaining the 1.8-second round
deadline. The subsequent run observed all four cards. This is a measured bounded
check, not a promise that future probes cannot time out.

`observer-activation-proposal.json` separates an eventual owned read-only observer
start/capture schedule from model actions and production cutover. It requires
final permission and verification that destinations/port/unit are unused; it does
not install a timer, enable boot startup, or activate anything. Exact deployment
paths, evidence retention/capacity and capture schedule must be reviewed before
that permission. Its rollback section preserves the mandatory executable
production-rollback gate before #11/#19 writes; a stage-only byte restoration is
not sufficient for those transitions.

## Trusted host-memory proposal for final permission

`host-memory-proposal.json` contains the exact LXC device add/remove argv and the
one collector setting to change. The proposed source is a direct **read-only
bind of the host procfs `/proc/meminfo`** to `/run/llmsvc-host/meminfo` in the
service container. It needs no file-copy producer, timer, daemon or mount of the
entire host `/proc`. A procfs read generates current values; there is no snapshot
age to reinterpret as freshness. Filesystem mtime is not a valid proc freshness
check.

Before permission/application, verify that the device name and target path are
unused and that `findmnt -T /proc/meminfo` on the host reports `proc`. The prepared
argv is a proposal, not an instruction to run it during this unapproved slice.
The final approved executor must first print a zero-mutation dry-run plan and
record the actual device/config operation as a structured journal event.

After an approved mount, before selecting the path in scheduler config:

1. Read mountinfo/findmnt inside the container and verify the target is backed by
   the intended procfs file and mounted read-only, not `fuse.lxcfs` or a regular
   saved file. Confirm both source and target contain `MemTotal`/`MemAvailable`.
2. Compare adjacent host and target reads: `MemTotal` must agree exactly; record
   timestamps and raw `MemAvailable` values without requiring equality across
   separate reads. Repeat the reads to confirm availability; do not synthesize
   memory pressure to manufacture a change.
3. On provenance/read/parse failure, keep the collector setting null and report
   host availability unknown. The telemetry parser's successful parse alone is
   insufficient permission to use an unverified path for admission.
4. In an isolated config/fake-source check, removal/read failure must remain an
   unavailable observation, not a retained old value. Keep all actions disabled.

Rollback restores the observation setting to null before removing only the named
LXC device. Never replace it with cgroup values. Device/config changes and any
scheduler restart need the final scoped permission. Any additional observer
activation needs an explicit bounded scope, integrated runtime readiness and
final authority.
No permission here includes TTL, reaper, production routing or model operations.

## LoRA test decision and budget

The locked read-only preflight at **2026-09-08 06:09:52 UTC** observed:

- GPUs 0, 2 and 3: 5 MiB used, 143152 MiB free, 0% utilization, no host compute
  process on those cards. GPU 1 had an external engine using 130412 MiB.
- No running managed models and a fresh SSE in-flight snapshot with zero
  requests. The reaper was executing its ordinary timer job; it was not changed.
- Installed vLLM 0.28.0 and llama-swap v252/e31a1ad. A cached Qwen2.5 7B base has
  four weight files totaling 15231271888 bytes. This is disk size, not RAM cost.
- No `adapter_config.json` or matching adapter weights found in the bounded
  inventory of the configured model store and root HF cache (72 directories,
  depth at most six). This is not a claim about every directory on the machine.

**Decision: the LoRA GPU smoke was not started.** The exact missing prerequisite
at that time was a cached compatible adapter with declared base/rank/target
modules and a semantic probe. The later user-authorized
[synthetic zero-delta fixture](LORA_SMOKE.md) now removes the supplied-fixture
dependency for mechanics validation: its installed vLLM CPU loader check passes.
It is explicitly untrained and cannot establish fine-tune quality or nonzero
adapter semantics. Do not download large assets or label base-only inference
as LoRA acceptance. The five-minute end-to-end duration is also not yet measured for a
complete base/no-LoRA versus LoRA comparison. These observations are historical;
a later launch requires a new locked preflight, including in-flight evidence.

`smoke.example.json` makes missing paths/ports/rank/probe explicit rather than
filling them with guesses. Review against #44 `docs/LORA.md`:

1. Hold the shared `gpu-test.lock` throughout a run. Recheck host and container
   process lists, utilization/free memory, model units, in-flight requests and
   RAM immediately before launch. Unknown evidence blocks it. Use a fresh run ID
   and unused daemon/wrapper/swap ports; existing workloads always keep priority.
2. Resolve the cached adapter's base, weight files and rank before setting LoRA
   flags. Run offline with eager execution and the short context/token settings
   in the JSON; do not compile/download opportunistically. Start only a unique
   test transient unit, bypassing the production placement/eviction launcher.
   Use a watchdog and a bounded RuntimeMaxSec, reserving cleanup within 300s.
3. Separate baseline-without-LoRA and LoRA-enabled startup measurements, keeping
   dtype/context/concurrency identical. Record steady GPU/RAM before/after enable
   and adapter load, load latency and direct base/adapter request results. Stop
   adding phases once less than 60s remains. A partial phase is not a comparison.
4. At level 1, record `/sleep` completion and `/is_sleeping`, then `/wake_up` and
   actual base/adapter inference. Adapter-list presence alone is not retention;
   compare the supplied semantic probe before/after. Level 2 is excluded from
   this bounded initial smoke because restoring discarded weights is additional
   work, not an ordinary wake.
5. When direct adapter inference is correct and time remains, compare an isolated
   swap config's fixed base name/alias with an explicit adapter route. Record the
   upstream `model` actually sent. Touch only test config files; production
   watcher/reload is never involved. Cleanup removes only recorded test resources.

## Reload smoke review (independent of GPU availability)

Prepare the first reload experiment with a fake streaming upstream, an isolated
llama-swap process/config and test wrapper processes. It can run without a LoRA
adapter or GPU, but requires its own bounded executable harness and evidence
schema before execution; this slice does not claim it ran.

Use an ordered continuous stream with verified heartbeat/disconnect semantics;
#41's per-round initial SSE snapshots do not prove five continuous quiet seconds.
Separate the admission/queue test (sustained traffic, awake pin and insufficient
batch RAM must block) from the residual reload race test. Only the latter admits
new requests between the last safe observation and config adoption.

Record monotonic and UTC timestamps for the last safe check, atomic rename,
one selected reload trigger (watcher **or** signal), observed adoption, and each
request's start/end. Classify completed, HTTP 5xx, truncated stream and timeout
separately; do not count receipt of HTTP 200 headers as completed streaming.
Exercise both the existing wrapper sleep/termination chain and the candidate
query `mode=wait` path, including any second sleep triggered by SIGTERM. Measure
actual upstream calls and interruptions rather than treating a sent signal or
local `apply_seconds` as the interruption window. Any zero-interruption result
is scoped to the tested chain/load and does not amend DESIGN automatically.

<!-- Generated-By: Codex / gpt-6-astra -->
