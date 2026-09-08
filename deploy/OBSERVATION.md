# Observation and isolated smoke preparation

Refs #8, #20 and #21. This preparation consumes the merged scheduler/CLI and the
telemetry factory contract from #41 at `cd4a3607`. It does not activate a service,
mount a host file, replace production configuration, or start a full-day observer.

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
probes. That is configuration validation, not systemd, live data, one-day, or
one-week acceptance. #41 must be integrated before installing this config.

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
scheduler restart need the final scoped permission; the separate full-day
observer activation also needs integrated runtime readiness and final authority.
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
is a cached compatible adapter with real weights, declared base/rank/target
modules, and a semantic probe that demonstrates adapter behavior. Do not download
large assets, fabricate an adapter, or run a base-only test and label it LoRA
acceptance. The five-minute end-to-end duration is also not yet measured for a
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
