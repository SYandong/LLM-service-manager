from pathlib import Path

from .config import ServerConfig

_PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = _PROJECT_ROOT / "var" / "log" / "vllm.log"


def build_command(config: ServerConfig) -> list[str]:
    cmd = [
        "vllm", "serve", config.model,
        "--host", config.host,
        "--port", str(config.port),
        "--gpu-memory-utilization", str(config.gpu_memory_utilization),
        "--max-model-len", str(config.max_model_len),
    ]
    if config.enable_reasoning:
        cmd += ["--enable-reasoning", "--reasoning-parser", config.reasoning_parser]
    return cmd
