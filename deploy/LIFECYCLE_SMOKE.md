# Cached-base collector lifecycle smoke

Refs #4. `lifecycle_smoke.py` is an ops-only Linux host runner. It requires the
existing LXC CLI, nvidia-smi, Python 3.10+, PyYAML, a reviewed collector source
checkout, and complete cached local weights. Copy `lifecycle.example.json`, fill
its absolute source/shared-lock/evidence paths, and review its exact target.
No package, model download, mount, persistent observer or production cutover is
performed by the runner.

```sh
python3 deploy/lifecycle_smoke.py --config /path/to/lifecycle.json --dry-run
# Only under the existing idle-test authorization, after the dry-run is reviewed:
python3 deploy/lifecycle_smoke.py --config /path/to/lifecycle.json
```

Dry-run creates no lock/directory/unit or HTTP request. A real run holds the
shared GPU lock, then verifies host GPU processes, utilization/free memory,
fresh production in-flight state and stopped production model states. Missing
or busy evidence blocks startup. It checks complete cached weight shards and
rejects caches larger than20GiB and requires fresh host `/proc/meminfo`
headroom of at least64GiB (test-only, not the #12 production threshold), then
uses a UUID `vllm-ops-life-*.service`, an unused loopback port, and an owned cache
directory. Before startup it repeats the preflight. During loading/inference it
monitors the candidate card and serving activity; a foreign/unclassified process
or new serving activity aborts only the test.

The default `direct` mode bypasses the production eviction/placement launcher. It starts the
cached base directly using eager mode, bf16, 512 context, one sequence, eight
output tokens maximum and the configured util. Offline environment variables
prevent opportunistic downloads. Cache paths are isolated. Development sleep
endpoints are enabled only on this loopback test instance. Before each mutating
HTTP request it verifies the unit's exact ownership token and the endpoint's
unique served-model identifier.

The work deadline reserves45 seconds for cleanup within a maximum300-second
wall budget; systemd independently enforces RuntimeMaxSec and a20-second stop
limit. Startup has its own bound. Actual collector states are checked before
sleep, during level1 sleep, after wake, and after owned-unit cleanup. Unknown
host memory/activity remain unknown; the test model is not registered in
production llama-swap. The test controls its own requests and never infers LoRA
retention from these probes. Logs and exact parameters/results remain private.

Measured on2026-09-08 with the cached Qwen2.5 7B base and vLLM0.28.0:

| Observation | Measured value |
|---|---:|
| First attempt | Startup failed in about43s: FlashInfer sampler required unavailable nvcc |
| Bounded retry | Used existing runtime's `VLLM_USE_FLASHINFER_SAMPLER=0`; no dependency installed |
| Systemd-run initiation to observed healthy | 36.26s (polling measurement, not an exact engine-ready timestamp) |
| Base inference | HTTP200, response `OK` |
| Awake resident memory | 28.434 GiB |
| Level1 sleeping resident memory | 0.764 GiB |
| Awake after wake | 28.412 GiB |
| Collector sequence | awake → sleeping → awake → stopped |
| Complete retry including cleanup | About51s |
| Post-cleanup GPU0 | 5MiB used, no compute process |

Both unique units and ports were gone, their cache directories and test reaper
markers absent, and the preexisting GPU1 engine remained unchanged in the final
check. The first failure is retained as evidence. The final helper adds the separate
host-RAM/cache-size guard and usage-telemetry opt-out after this historical run;
those additions have offline validation and are not retroactive RAM-admission
evidence for the recorded GPU run. The sampler setting is explicit
because the isolated test deliberately avoids importing an entire production
environment file.

This is a direct-daemon cached-base lifecycle/readiness baseline. It does not
satisfy production launcher latency, scheduler free execution, LoRA, permanent
installation or production-policy acceptance. Use bounded measurements and
deterministic replay; long-term stability and calibration remain NOT MEASURED,
not a day/week completion gate. Actual LoRA still requires its compatible cached
adapter and semantic fixture. Test cleanup may never stop/sleep existing
workloads to make space.

## Scheduler action mode (#9 / #10 / #23)

Set `mode: scheduler-actions` using
[scheduler-action.example.json](scheduler-action.example.json). It is integrated
into the same runner and shared GPU lock; it does not install a second production
driver or add an API. Its extra native/wrapper binaries must be already available
and SHA256-pinned. The scheduler interpreter and reviewed source must exist;
there are no package or model downloads. `--dry-run` validates inputs without
creating locks, files, units, sockets or requests.

This mode creates one isolated native source, scheduler, profile, empty ledger
and UUID model namespace. It stages only owner-marked temporary files. Native
configuration has no preload and retains only this test model. A single bounded
inference request to that source starts the official wrapper and the **existing
lease-aware launcher**, which calls actual place, starts its assigned daemon,
checks health and confirms the lease. The harness never inserts a confirmed
account. A guard rejects an unexpected GPU before the launcher starts anything;
the existing release path still requires proven absence.

The sampler uses an owned wrapper around real `nvidia-smi --id <selected GPU>`.
Indices are not renumbered and foreign processes on that card are not filtered
out. A separate read-only systemctl adapter exposes only the test unit, so other
models cannot enter this isolated scheduler’s configured model set; it refuses
all stop/start commands. The inherited host-side inventory continues checking real GPU ownership and
production serving activity. The configured host meminfo path must be an existing
live bind of the host file: device/inode identity is compared with the host's
`/proc/meminfo` before starting. Static snapshots and container meminfo are not
admission evidence. No host bind or production configuration is created here.
The isolated scheduler retains its standard RAM admission floor in addition to
the inherited host preflight; a passing GPU preflight does not override a policy
RAM refusal.

After the recorded cold request, the harness waits at most40 seconds for the
existing pure free preview to permit this model. The policy's >30-second idle
condition, pin/in-flight protection and RAM constraints remain intact. Unknown
activity or another blocker fails the run instead of editing timestamps or
changing policy. `cold_start_cost_seconds` is an explicit ranking input, not a
measured cold-start latency; measurements are recorded separately.

The action sequence uses actual `POST /v1/free`, a fresh observed sleeping state
and residual GPU memory, then actual `POST /v1/wake/<model>`. Before and after each
action the same confirmed lease, full budget and exact daemon PID/start/invocation
must remain bound. Normal unload invokes a test-local guard around the official
wrapper `sleep --vllm-url` command: it verifies the daemon token/lease/listener
and wrapper scope, confirms sleeping, then signals only the wrapper's pidfd. No
unverified numeric `stop-pid` is passed to another process. Scheduler-actions
mode allows stable primary-model bystanders only after selected-GPU/process/RAM
guards; direct lifecycle mode keeps its strict all-model quiet preflight.

The existing standalone CLI's `SchedulerClient` and `EventReader` collect the
scheduler result event. Evidence includes a **local** request UUID, model, actual
lease/unit identity, pre-request cursor, result event ID, HTTP duration, event
receipt duration and before/after resident memory. The API does not carry a server
request ID or a lease ID in `free_result`: correlation is explicitly limited to
the isolated single-operation scope, cursor and exact response fields. It is not
a claim that those wire fields exist. Missing results, observed duplicate matches,
gaps, disconnects, resets, partial/refused operations and unknown measurements
fail validation. Event receipt can precede HTTP return; its signed offset is kept.
These measurements do not certify continuous quiet or old-server settlement.

If an action returns a partial measured response or no response at all, the
helper preserves the raw response (when present), local request/model context,
client timing/deadline, parsed HTTP status/payload when available, and completed
identity checks in the private phase receipt. A transport error remains a failed
nonzero result; missing evidence stays unknown, and the receipt does not invent native acknowledgement,
settlement, or cleanup confirmation.
The current standalone client exposes a parsed error payload only when its
`ClientError` carries one; otherwise a known HTTP status and sanitized error text
are retained while payload availability and parsing remain unknown.

All helper requests run in bounded token-tagged units so the host runner can keep
checking GPU/serving conditions during long requests. The total remains at most
300 seconds with45 seconds reserved for cleanup. Runtime caps and stop limits are
applied to each owned unit. Cleanup stops only the matching daemon, asks the real
lease API to release after proven exit, and kills only matching test control-unit
cgroups before removal, preventing late native unload callbacks from reaching an
uncertain daemon. Unknown ownership or release leaves the private ledger/files
for inspection. Existing workloads are never stopped or slept to make room.

CPU contract tests run the actual scheduler HTTP server, empty SQLite ledger,
existing launcher, a real loopback health child and scheduler SSE. Hardware,
systemd and source sleep/wake observations are explicitly fixtures. They verify
place/confirm accounting, free/wake/event behavior, refusal and cleanup boundaries;
they **do not measure vLLM/GPU sleep, wake or UI latency**. The real chain remains
NOT MEASURED until an eligible GPU and reviewed harness permit the bounded run.
No old direct-lifecycle, LoRA, native-maintenance or UI receipt is relabeled as
this mode's acceptance; long-term stability/calibration remain NOT MEASURED.

<!-- Generated-By: Codex / gpt-6-astra -->
<!-- Generated-By: Codex / gpt-5.6-luna -->
