# Synthetic LoRA mechanics smoke (#21)

`synthetic_lora.py` creates an explicitly untrained **zero-delta** rank-one
Qwen2 adapter. It reads only cached `config.json`, writes a new private directory
outside the base, and refuses an existing output or unsupported dimensions.
No PEFT install, model download, base-weight rewrite or training is required.
Both q_proj factors are BF16 zeros for every layer. Safetensors metadata and
`fixture.json` label the artifact as mechanics-only and record SHA-256 values.
It cannot measure fine-tune quality or prove a nonzero adapter effect.

The format follows the pinned vLLM0.28.0
[PEFT helper](https://github.com/vllm-project/vllm/blob/v0.28.0/vllm/lora/peft_helper.py)
and [checkpoint loader](https://github.com/vllm-project/vllm/blob/v0.28.0/vllm/lora/lora_model.py).
This deliberately supports the inspected `Qwen2ForCausalLM` q_proj layout only;
another architecture needs an explicitly validated fixture shape.

```sh
# Supply an existing cached base and a fresh output name; dry-run creates nothing.
python3 deploy/synthetic_lora.py --base /path/to/cached/qwen2 \
  --output /tmp/ops-synthetic-adapter --dry-run
python3 deploy/synthetic_lora.py --base /path/to/cached/qwen2 \
  --output /tmp/ops-synthetic-adapter
```

Measured CPU validation: installed vLLM0.28.0, torch2.13.0 and safetensors0.8.0
accepted all28 q_proj layers/56 tensors at rank1 for the cached Qwen2.5 7B shape
(hidden3584). The three fixture files totaled409662 bytes. The actual installed
`PEFTHelper.validate_legal` and `LoRAModel.from_local_checkpoint(device='cpu')`
accepted it; all factors were zero, base configuration hash unchanged, and the
owned temporary directory removed. CUDA visibility was disabled and no model
was started. PEFT is absent and was not needed. This establishes CPU loader
compatibility, **not** successful GPU kernel execution or sleep retention.

To reproduce the CPU loader check in an existing vLLM environment after generating
the fixture, point `ADAPTER_DIR` at that new directory. The final assignment only
disables pinned allocations for this CPU-only check, not a runtime setting:

```sh
CUDA_VISIBLE_DEVICES='' HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ADAPTER_DIR=/tmp/ops-synthetic-adapter /path/to/vllm/python - <<'PY'
import os
import torch
from vllm.config.lora import LoRAConfig
from vllm.lora.peft_helper import PEFTHelper
import vllm.lora.lora_model as loader
helper = PEFTHelper.from_local_dir(os.environ['ADAPTER_DIR'], 512)
helper.validate_legal(LoRAConfig(max_lora_rank=8))
loader.PIN_MEMORY = False
adapter = loader.LoRAModel.from_local_checkpoint(
    os.environ['ADAPTER_DIR'], {'q_proj'}, helper,
    lora_model_id=1, device='cpu', dtype=torch.bfloat16,
)
print({'rank': adapter.rank, 'layers': len(adapter.loras)})
PY
```

## Executable isolated GPU harness

`lora_smoke.py` subclasses the existing [lifecycle runner](LIFECYCLE_SMOKE.md),
retaining shared `gpu-test.lock`, fresh host GPU/process/memory and serving
checks, exact unit/environment ownership, alternate loopback port, offline
cache isolation and owned cleanup. Copy/edit `lora.example.json`; `source` must
be the reviewed collector checkout. No production launcher, routing or config
is invoked. Production swap is read only for an initial activity snapshot; this
is a test scheduling guard, **not** continuous quiet or reload authorization.

```sh
python3 deploy/lora_smoke.py --config /path/to/private/lora.json --dry-run
# Only the ops test owner may execute after fresh idle preflight succeeds.
python3 deploy/lora_smoke.py --config /path/to/private/lora.json
```

Dry-run creates no lock/file/process/socket. A real invocation has at most300s
including cleanup, reserves45s for cleanup and stops admitting new measurement
phases with60s left. Startup has a separate bounded limit. A unique transient
unit also has `RuntimeMaxSec`, `Restart=no` and bounded stop time. Foreign GPU
processes, busy/unknown serving, insufficient fresh host RAM/free VRAM, existing
unit identity or deadline expiry block progress. Cleanup stops only the matching
run-token unit and removes only its owner-marked temporary directory; ambiguous
active ownership preserves files for inspection. Other workloads are never
stopped or slept to make room.

The adapter is generated inside that owned directory before startup. The test
vLLM uses `--enable-lora`, capacity one adapter and max rank8 (fixture rank1),
plus `VLLM_ALLOW_RUNTIME_LORA_UPDATING=True`. All these flags are test-local.
The phase sequence is:

1. Readiness and base inference on the unique base name; record memory.
2. One runtime `POST /v1/load_lora_adapter`; require the adapter in `/v1/models`
   and an actual adapter-name completion, then record memory.
3. Level1 sleep and actual collector sleeping; record memory. Wake and actual
   collector awake, then require list visibility **and another adapter request**
   without a second load. A successful list alone never passes retention.
4. Unload only this adapter, require it absent from the list, then stop the owned
   test unit and verify stopped/cleanup through the inherited runner.

Private evidence records monotonic HTTP phase latency, GPU used MiB and unit
`MemoryCurrent` (null if unknown), request-content digests and full failure/cleanup
outcomes. Memory deltas compare the **same LoRA-enabled daemon** before/after load;
they do not measure the static cost of enabling LoRA versus a separate no-LoRA
process. Unit cgroup memory is not trusted host MemAvailable for admission.
Equal zero-delta texts show deterministic agreement only, not learned behavior.
A timeout/failed request leaves partial phase evidence and a nonzero result.

## Current live boundary and next acceptance

At the preparation inventory all four GPUs had foreign compute processes;
there was no eligible idle card, so **no GPU run occurred**. This is a transient
capacity gate, not a missing-adapter/library dependency. The next eligible run
must recheck all conditions under the shared lock rather than reuse that inventory.
GPU runtime load, sleep/wake retention, memory deltas and request latency remain
**NOT MEASURED**. Long-term stability/calibration are likewise NOT MEASURED;
there is no day/week wait.

This harness targets direct vLLM. It neither measures nor alters llama-swap alias
routing or reload. Registry owns the separate [LoRA/DESIGN conclusions](../docs/LORA.md)
and #53/#60 quiet/adoption/old-server settlement guarantees. Follow-on #23 event
latency and #26 first-request acceptance need an appropriately isolated actual
runtime/request measurement; this CPU fixture is not either acceptance.

<!-- Generated-By: Codex / gpt-6-astra -->
