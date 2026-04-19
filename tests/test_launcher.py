from vllm_service.config import ServerConfig
from vllm_service.launcher import build_command


def _config(**overrides):
    defaults = dict(
        model="Qwen/Qwen3-32B",
        host="127.0.0.1",
        port=8000,
        gpu_memory_utilization=0.9,
        max_model_len=32768,
        enable_reasoning=True,
        reasoning_parser="deepseek_r1",
    )
    return ServerConfig(**{**defaults, **overrides})


def test_basic_command():
    cmd = build_command(_config())
    assert cmd[:3] == ["vllm", "serve", "Qwen/Qwen3-32B"]
    assert "--host" in cmd and "127.0.0.1" in cmd
    assert "--port" in cmd and "8000" in cmd
    assert "--gpu-memory-utilization" in cmd and "0.9" in cmd
    assert "--max-model-len" in cmd and "32768" in cmd


def test_reasoning_flags_included():
    cmd = build_command(_config(enable_reasoning=True))
    assert "--enable-reasoning" in cmd
    assert "--reasoning-parser" in cmd
    assert "deepseek_r1" in cmd


def test_reasoning_flags_excluded():
    cmd = build_command(_config(enable_reasoning=False))
    assert "--enable-reasoning" not in cmd
    assert "--reasoning-parser" not in cmd
