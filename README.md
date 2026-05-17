# vLLM Service Manager

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
python -m vllm_service start --model Qwen/Qwen3-4B-Instruct-2507
python -m vllm_service stop
python -m vllm_service status
python -m vllm_service restart --model google/gemma-4-31B-it
```

## Connecting to the service

The service exposes an OpenAI-compatible API at `http://10.86.229.182:8000/v1`.

```python
from openai import OpenAI

client = OpenAI(base_url="http://10.86.229.182:8000/v1", api_key="unused")

response = client.chat.completions.create(
    model=client.models.list().data[0].id,
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

## Logs

Server logs are written to `var/log/vllm.log`.
