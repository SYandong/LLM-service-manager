# LLM Service Manager

> **项目转型中（2026-09）**：本仓库正在从"单后端 vLLM 代理"改造成 **llama-swap 之上的调度控制面**：多 GPU 放置、按显存/内存压力休眠、保底模型、`llm free / pin / reserve` 用户命令，以及一个终端 UI。
> 路线图见 [`docs/ROADMAP.md`](docs/ROADMAP.md)，设计见 [`docs/DESIGN.md`](docs/DESIGN.md)，协作规范见 [`AGENTS.md`](AGENTS.md)。
> 下面的内容描述的是 legacy 代理（`vllm_service/`），已冻结，只修 bug，计划在 M6 下线。

---

# vLLM Service Manager (legacy)

A local tool to start and manage a vLLM model server for a group sharing one machine.

## Requirements

- conda environment `vllm` with vLLM 0.19.0 and transformers >= 5.5.0
- `LD_LIBRARY_PATH` set to include the conda env's lib directory (see Setup)

## Setup

```bash
conda create -n vllm python=3.11 -y
conda activate vllm
pip install vllm==0.19.0
pip install "transformers>=5.5.0"

# Fix libstdc++ version mismatch
conda install -c conda-forge libstdcxx-ng -y
mkdir -p $CONDA_PREFIX/etc/conda/activate.d
echo 'export LD_LIBRARY_PATH=$CONDA_PREFIX/lib:$LD_LIBRARY_PATH' > $CONDA_PREFIX/etc/conda/activate.d/env_vars.sh
```

## Configuration

`config/server.yaml` defines the default model and serving parameters:

```yaml
model: "google/gemma-4-31B-it"
host: "0.0.0.0"
port: 8000
backend_port: 8001
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: false
reasoning_parser: "deepseek_r1"
```

Supported model IDs:

- `google/gemma-4-31B-it`
- `Qwen/Qwen3-4B-Instruct-2507`

## Usage

```bash
conda activate vllm
cd <project-directory>

python -m vllm_service start
python -m vllm_service stop
python -m vllm_service status
python -m vllm_service restart --model <model-id-or-service-visible-path>
```

`start` starts the proxy as a background service on the user-facing port, loads
the default model, shows the backend vLLM startup output, and returns after the
model is ready. The proxy reads the OpenAI request body's `model` field. If that model is not
currently running, it restarts vLLM on `backend_port`, waits for readiness, and
then forwards the original request. If the request omits `model`, the proxy uses
the default `model` from `config/server.yaml`. `restart --model ...` restarts
the background proxy, overrides that default for the current service run, loads
that model, shows the backend vLLM startup output, and returns after the model
is ready. It does not start raw vLLM on the user-facing port.

The `model` value may be either:

- a Hugging Face model ID, such as `Qwen/Qwen3-4B-Instruct-2507`
- a model path that is readable from the service host, such as a path in shared
  storage mounted on the service host

For fine-tuned local models, use the path as it exists on the service host. Do
not use a path that only exists on the user's laptop or workstation.

## Connecting to the service

The service exposes an OpenAI-compatible API at `http://10.86.229.182:8000/v1`.

```python
from openai import OpenAI

client = OpenAI(base_url="http://10.86.229.182:8000/v1", api_key="unused")

response = client.chat.completions.create(
    model="Qwen/Qwen3-4B-Instruct-2507",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)

# Fine-tuned/local model: replace this with the path visible to the service host.
response = client.chat.completions.create(
    model="<path-visible-to-vllm-service>",
    messages=[{"role": "user", "content": "Hello from my fine-tuned model!"}],
)
print(response.choices[0].message.content)
```

## Logs

Server logs are written to `var/log/vllm.log`.
