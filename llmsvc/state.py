# Generated-By: Codex / gpt-6-astra
"""Shared immutable observations and JSON contract for the control plane.

Timestamps are Unix seconds (UTC). Memory fields ending in ``_gb`` use GiB
(bytes / 1024**3). Unknown observations are None, never invented zero values.
Policy code consumes these records without performing I/O.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, Tuple


ModelStatus = Literal["awake", "sleeping", "stopped", "unknown"]


@dataclass(frozen=True)
class GPUProcess:
    pid: int
    used_gb: Optional[float] = None
    name: Optional[str] = None
    model: Optional[str] = None
    user: Optional[str] = None


@dataclass(frozen=True)
class GPUState:
    index: int
    uuid: Optional[str] = None
    total_gb: Optional[float] = None
    used_gb: Optional[float] = None
    free_gb: Optional[float] = None
    managed_gb: Optional[float] = None
    external_gb: Optional[float] = None
    utilization_percent: Optional[float] = None
    external_processes: Tuple[GPUProcess, ...] = ()


@dataclass(frozen=True)
class ModelState:
    name: str
    state: ModelStatus = "unknown"
    gpu: Optional[int] = None
    util: Optional[float] = None
    budget_gb: Optional[float] = None
    weights_gb: Optional[float] = None
    resident_gb: Optional[float] = None
    unit: Optional[str] = None
    unit_active: Optional[bool] = None
    health_ok: Optional[bool] = None
    is_sleeping: Optional[bool] = None
    swap_state: Optional[str] = None
    port: Optional[int] = None
    is_default: bool = False
    cold_start_seconds: Optional[float] = None


@dataclass(frozen=True)
class Activity:
    model: str
    last_request_at: Optional[float] = None
    requests_last_hour: Optional[int] = None
    requests_last_10m: Optional[int] = None
    in_flight: Optional[int] = None
    by: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Pin:
    model: str
    until: float
    by: str


@dataclass(frozen=True)
class Reserve:
    id: str
    gpu: int
    size_gb: float
    until: float
    by: str


@dataclass(frozen=True)
class Lease:
    lease_id: str
    model: str
    gpu: int
    util: float
    expires_at: float
    budget_gb: float
    status: Literal["pending", "stale", "confirmed", "released"] = "pending"


@dataclass(frozen=True)
class MemoryState:
    host_available_gb: Optional[float] = None
    sleeping_weights_gb: Optional[float] = None
    budget_gb: float = 200.0
    host_min_available_gb: float = 150.0


@dataclass(frozen=True)
class Blocker:
    model: Optional[str]
    reason: str
    gpu: Optional[int] = None
    user: Optional[str] = None
    in_flight: Optional[int] = None


@dataclass(frozen=True)
class Action:
    kind: Literal["sleep", "stop", "wake", "place"]
    model: str
    reason: str
    gpu: Optional[int] = None


@dataclass(frozen=True)
class Event:
    id: int
    timestamp: float
    kind: str
    model: Optional[str] = None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StateSnapshot:
    sampled_at: Optional[float] = None
    gpus: Tuple[GPUState, ...] = ()
    models: Tuple[ModelState, ...] = ()
    activity: Tuple[Activity, ...] = ()
    pins: Tuple[Pin, ...] = ()
    reserves: Tuple[Reserve, ...] = ()
    leases: Tuple[Lease, ...] = ()
    memory: MemoryState = field(default_factory=MemoryState)
    blocked_by: Tuple[Blocker, ...] = ()
    errors: Tuple[str, ...] = ()
    read_only: bool = True
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-ready mapping (tuples encode as arrays)."""
        return asdict(self)


@dataclass(frozen=True)
class FaultClaim:
    """Internal durable cleanup fence; not part of the public state schema."""

    lease_id: str
    model: str
    unit: str
    invocation_id: str
    gpu: int
    reason: str
    proved_at: float
    stage: Literal["claimed", "released", "complete"] = "claimed"
    error: Optional[str] = None
    proxy_origin_hash: str = ""
    proxy_submitted: bool = False
    proxy_acknowledged: bool = False


@dataclass(frozen=True)
class RecoveryClaim:
    """Internal ordinary recovery fence; never fault-cleanup authority."""

    id: str
    model: str
    source_lease_id: str
    unit: str
    invocation_id: str
    source_gpu: int
    reason: str
    created_at: float
    util_floor: float
    budget_floor_gb: float
    profile_hash: str
    relocate: bool
    stage: str = "claimed"
    stop_submitted: bool = False
    stop_acknowledged: bool = False
    proxy_submitted: bool = False
    proxy_acknowledged: bool = False
    wake_submitted: bool = False
    wake_acknowledged: bool = False
    destination_lease_id: Optional[str] = None
    destination_invocation_id: str = ""
    error: Optional[str] = None
