# Generated-By: Codex / gpt-6-astra
"""Validated scheduler configuration, independent of the legacy proxy config."""

import ipaddress
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def canonical_ip(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("IP address must be a string")
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


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
    registry: dict[str, Any] = field(default_factory=dict)
    state_db_path: str = ""
    data_plane_events_enabled: bool = False
    data_plane_event_capacity: int = 256
    data_plane_event_batch_size: int = 128
    data_plane_event_interval_seconds: float = 0.2
    data_plane_event_timeout_seconds: float = 2.0
    data_plane_event_reconnect_seconds: float = 1.0
    max_snapshot_age_seconds: float = 30.0
    placement_enabled: bool = False
    placement_wait_seconds: float = 120.0
    lease_timeout_seconds: float = 900.0
    lease_probe_seconds: float = 1.0
    model_actions_enabled: bool = False
    automation_enabled: bool = False
    automation_interval_seconds: float = 15.0
    automation_cycle_timeout_seconds: float = 120.0
    automation_idle_seconds: float = 600.0
    automation_policy: str = "fixed_idle"
    automation_exclusive_ttl_seconds: float = 3600.0
    automation_shared_ttl_seconds: float = 300.0
    automation_shared_external_threshold_gb: float = 1.0
    automation_shared_free_threshold_gb: float = 10.0
    sleeping_recovery_enabled: bool = False
    sleeping_recovery_timeout_seconds: float = 900.0
    fault_recovery_enabled: bool = False
    fault_interval_seconds: float = 1.0
    fault_timeout_seconds: float = 30.0
    fault_health_failures: int = 3
    free_timeout_seconds: float = 120.0
    reserve_timeout_seconds: float = 120.0
    wake_timeout_seconds: float = 900.0
    action_observe_seconds: float = 10.0
    action_poll_seconds: float = 0.2

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
                     "request_timeout_seconds", "memory_budget_gb", "host_min_available_gb",
                     "max_snapshot_age_seconds", "free_timeout_seconds", "reserve_timeout_seconds", "wake_timeout_seconds",
                     "action_observe_seconds", "action_poll_seconds", "placement_wait_seconds",
                     "lease_timeout_seconds", "lease_probe_seconds", "data_plane_event_interval_seconds",
                     "data_plane_event_timeout_seconds", "data_plane_event_reconnect_seconds",
                     "automation_interval_seconds", "automation_cycle_timeout_seconds", "automation_idle_seconds",
                     "automation_exclusive_ttl_seconds", "automation_shared_ttl_seconds",
                     "fault_interval_seconds", "fault_timeout_seconds", "sleeping_recovery_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if type(self.data_plane_events_enabled) is not bool:
            raise ValueError("data_plane_events_enabled must be a boolean")
        for name in ("data_plane_event_capacity", "data_plane_event_batch_size"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 4096:
                raise ValueError(f"{name} must be an integer in 1..4096")
        if not isinstance(self.automation_policy, str) or self.automation_policy not in ("fixed_idle", "gpu_pressure"):
            raise ValueError("automation_policy must be fixed_idle or gpu_pressure")
        for name in ("automation_shared_external_threshold_gb", "automation_shared_free_threshold_gb"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be a finite non-negative number")
        if type(self.fault_recovery_enabled) is not bool:
            raise ValueError("fault_recovery_enabled must be a boolean")
        if self.fault_interval_seconds > 1 or self.fault_timeout_seconds > 120:
            raise ValueError("fault intervals must be <=1s and fault timeouts <=120s")
        if type(self.fault_health_failures) is not int or not 3 <= self.fault_health_failures <= 100:
            raise ValueError("fault_health_failures must be an integer in 3..100")
        if type(self.automation_enabled) is not bool:
            raise ValueError("automation_enabled must be a boolean")
        if type(self.sleeping_recovery_enabled) is not bool:
            raise ValueError("sleeping_recovery_enabled must be a boolean")
        if self.sleeping_recovery_timeout_seconds > 900:
            raise ValueError("sleeping_recovery_timeout_seconds must be <=900")
        if self.automation_cycle_timeout_seconds > 120:
            raise ValueError("automation_cycle_timeout_seconds must not exceed 120")
        if self.reserve_timeout_seconds > 120:
            raise ValueError("reserve_timeout_seconds must not exceed 120")
        if self.placement_wait_seconds > 120:
            raise ValueError("placement_wait_seconds must not exceed 120")
        if type(self.placement_enabled) is not bool:
            raise ValueError("placement_enabled must be a boolean")
        if type(self.model_actions_enabled) is not bool:
            raise ValueError("model_actions_enabled must be a boolean")
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if not isinstance(self.state_db_path, str):
            raise ValueError("state_db_path must be a string")
        if not self.read_only and not self.state_db_path.strip():
            raise ValueError("writable pin intent mode requires state_db_path")
        if not isinstance(self.collectors, dict):
            raise ValueError("collectors must be a mapping")
        if not isinstance(self.registry, dict):
            raise ValueError("registry must be a mapping")
        if self.registry:
            required = {"config_path", "shared_roots", "daemon_port_range"}
            limits = {"config_max_bytes", "model_config_max_bytes", "weight_index_max_bytes"}
            if not required <= set(self.registry) or set(self.registry)-required-{"reserved_ports"}-limits:
                raise ValueError("registry requires config_path/shared_roots/daemon_port_range and known optional keys")
            for key in limits & self.registry.keys():
                value = self.registry[key]
                if type(value) is not int or not 1 <= value <= 16777216:
                    raise ValueError(f"registry.{key} must be an integer within 1..16777216")
            path = self.registry["config_path"]
            roots = self.registry["shared_roots"]
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError("registry.config_path must be an absolute path")
            if (not isinstance(roots, list) or not roots or any(not isinstance(root, str) or not Path(root).is_absolute() for root in roots)):
                raise ValueError("registry.shared_roots must be nonempty absolute paths")
            ports = self.registry["daemon_port_range"]
            reserved = self.registry.get("reserved_ports", [])
            if (not isinstance(ports, list) or len(ports) != 2
                    or any(type(port) is not int or not 1 <= port <= 65535 for port in ports) or ports[0] > ports[1]):
                raise ValueError("registry.daemon_port_range must be [first,last] within 1..65535")
            if not isinstance(reserved, list) or any(type(port) is not int or not 1 <= port <= 65535 for port in reserved):
                raise ValueError("registry.reserved_ports must contain valid integer ports")
        owners = self.collectors.get("ip_containers", {})
        if not isinstance(owners, dict):
            raise ValueError("collectors.ip_containers must be a mapping")
        normalized = {}
        for source, owner in owners.items():
            source = canonical_ip(source)
            if not isinstance(owner, str) or not owner.strip():
                raise ValueError("configured container names must be nonempty strings")
            if source in normalized and normalized[source] != owner:
                raise ValueError("conflicting container mappings for the same IP")
            normalized[source] = owner

    def owner_for_ip(self, source_ip: str) -> str:
        """Use the socket peer, never a body label or forwarded header."""
        source = canonical_ip(source_ip)
        for configured_ip, owner in self.collectors.get("ip_containers", {}).items():
            if canonical_ip(configured_ip) == source:
                return owner
        return "ip:" + source


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
