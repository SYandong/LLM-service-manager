from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    model: str
    host: str
    port: int
    gpu_memory_utilization: float
    max_model_len: int
    enable_reasoning: bool
    reasoning_parser: str
    backend_port: int = 8001


def load_config(path: Path) -> ServerConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return ServerConfig(**data)
