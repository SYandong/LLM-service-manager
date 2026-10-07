# Generated-By: Codex / gpt-6.1-sol
"""Strict standard-library configuration for the standalone fleet observer."""

from dataclasses import dataclass, fields
import ipaddress
import math
from pathlib import Path

from llmsvc.fleet.ingest import read_json


def canonical_ip(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("IP address must be a string")
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


@dataclass(frozen=True)
class ObserverConfig:
    listen_host: str
    listen_port: int
    fleet_snapshot_path: str
    fleet_db_path: str
    ip_containers_path: str
    host_ips: tuple = ()
    fleet_ingest_interval_seconds: float = 30.0
    fleet_stale_after_seconds: float = 180.0
    fleet_active_window_seconds: float = 900.0
    fleet_idle_limit_hours: float = 6.0
    fleet_raw_retention_days: int = 14
    fleet_hourly_retention_days: int = 180
    event_history_size: int = 1000
    event_heartbeat_seconds: float = 15.0
    request_timeout_seconds: float = 10.0

    def __post_init__(self):
        try:
            host = ipaddress.ip_address(canonical_ip(self.listen_host))
        except (ValueError, TypeError) as error:
            raise ValueError("invalid_observer_listen_host") from error
        private = host.is_private or (host.version == 4 and host in ipaddress.ip_network("100.64.0.0/10"))
        if not private or host.is_unspecified or host.is_multicast or "%" in self.listen_host:
            raise ValueError("invalid_observer_listen_host")
        if type(self.listen_port) is not int or not 1 <= self.listen_port <= 65535:
            raise ValueError("invalid_observer_listen_port")
        paths = []
        for key in ("fleet_snapshot_path", "fleet_db_path", "ip_containers_path"):
            value = getattr(self, key)
            if not isinstance(value, str) or not Path(value).is_absolute() or any(ord(char) < 32 for char in value):
                raise ValueError("invalid_observer_path")
            paths.append(Path(value).resolve())
        if len(set(paths)) != len(paths):
            raise ValueError("overlapping_observer_files")
        if not isinstance(self.host_ips, (list, tuple)) or len(self.host_ips) > 32:
            raise ValueError("invalid_observer_host_ips")
        addresses = []
        for value in self.host_ips:
            try:
                normalized = canonical_ip(value)
                address = ipaddress.ip_address(normalized)
            except (ValueError, TypeError) as error:
                raise ValueError("invalid_observer_host_ips") from error
            if address.is_unspecified or address.is_multicast or "%" in value or normalized in addresses:
                raise ValueError("invalid_observer_host_ips")
            addresses.append(normalized)
        object.__setattr__(self, "host_ips", tuple(addresses))
        limits = {"fleet_ingest_interval_seconds": 3600, "fleet_stale_after_seconds": 86400,
                  "fleet_active_window_seconds": 604800, "fleet_idle_limit_hours": 8760,
                  "event_heartbeat_seconds": 300, "request_timeout_seconds": 300}
        for key, maximum in limits.items():
            value = getattr(self, key)
            if type(value) not in (int, float) or not 0 < value <= maximum or not math.isfinite(value):
                raise ValueError("invalid_observer_interval")
        for key in ("fleet_raw_retention_days", "fleet_hourly_retention_days"):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= 3650:
                raise ValueError("invalid_observer_retention")
        if self.fleet_hourly_retention_days < self.fleet_raw_retention_days:
            raise ValueError("invalid_observer_retention")
        if type(self.event_history_size) is not int or not 1 <= self.event_history_size <= 10000:
            raise ValueError("invalid_observer_event_capacity")

    @property
    def fleet_claims_enabled(self):
        return False

    @property
    def collectors(self):
        # This is the existing presentation/identity interface, not a collector.
        return {"ip_containers_path": self.ip_containers_path, "host_ips": self.host_ips}

    @classmethod
    def from_mapping(cls, payload):
        allowed = {field.name for field in fields(cls)}
        if not isinstance(payload, dict) or set(payload) - allowed - {"_generated_by", "_comments"}:
            raise ValueError("invalid_observer_config")
        try:
            return cls(**{key: value for key, value in payload.items() if key in allowed})
        except TypeError as error:
            raise ValueError("invalid_observer_config") from error

    @classmethod
    def load(cls, path):
        return cls.from_mapping(read_json(path, 64 * 1024))
