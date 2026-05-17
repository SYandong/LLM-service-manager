import pytest
from pathlib import Path
from vllm_service.config import load_config, ServerConfig


def test_load_config(tmp_path):
    yaml_content = """
model: "Qwen/Qwen3-32B"
host: "127.0.0.1"
port: 8000
backend_port: 8001
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: true
reasoning_parser: "deepseek_r1"
"""
    config_file = tmp_path / "server.yaml"
    config_file.write_text(yaml_content)
    config = load_config(config_file)
    assert isinstance(config, ServerConfig)
    assert config.model == "Qwen/Qwen3-32B"
    assert config.host == "127.0.0.1"
    assert config.port == 8000
    assert config.backend_port == 8001
    assert config.gpu_memory_utilization == 0.9
    assert config.max_model_len == 32768
    assert config.enable_reasoning is True
    assert config.reasoning_parser == "deepseek_r1"
