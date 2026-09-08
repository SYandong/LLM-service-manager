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

The runner bypasses the production eviction/placement launcher. It starts the
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
installation, day/week observation or production-policy acceptance. Actual
LoRA still requires its compatible cached adapter and semantic fixture. Test
cleanup may never stop/sleep existing workloads to make space.

<!-- Generated-By: Codex / gpt-6-astra -->
