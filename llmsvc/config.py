# Generated-By: Codex / gpt-6-astra
"""Validated scheduler configuration, independent of the legacy proxy config."""

import ipaddress
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SchedulerConfig:
    listen_host: str
    listen_port: int
    sample_interval_seconds: float = 15.0
    event_history_size: int = 1000
    event_heartbeat_seconds: float = 15.0
    request_timeout_seconds: float = 10.0
    memory_budget_gb: float = 200.0
    host_min_available_gb: float = 150.0
    read_only: bool = True
    collectors: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        try:
            address = ipaddress.ip_address(self.listen_host)
        except ValueError as exc:
            raise ValueError("listen_host must be a literal container or loopback IP") from exc
        if address.is_unspecified or address.is_multicast or not address.is_private:
            raise ValueError("listen_host must be a specific private/container or loopback IP")
        if type(self.listen_port) is not int or not 1 <= self.listen_port <= 65535:
            raise ValueError("listen_port must be an integer in 1..65535")
        if type(self.event_history_size) is not int or self.event_history_size < 1:
            raise ValueError("event_history_size must be a positive integer")
        for name in ("sample_interval_seconds", "event_heartbeat_seconds",
                     "request_timeout_seconds", "memory_budget_gb", "host_min_available_gb"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if self.read_only is not True:
            raise ValueError("M1 only supports read_only: true")
        if not isinstance(self.collectors, dict):
            raise ValueError("collectors must be a mapping")


def load_config(path: str) -> SchedulerConfig:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError("invalid scheduler YAML") from exc
    if not isinstance(data, dict):
        raise ValueError("scheduler configuration must be a mapping")
    try:
        return SchedulerConfig(**data)
    except TypeError as exc:
        raise ValueError(f"invalid scheduler configuration keys: {exc}") from exc
