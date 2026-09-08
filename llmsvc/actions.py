# Generated-By: Codex / gpt-6-astra
"""Guarded single-action dispatch; no live transport or HTTP mount is defaulted.

Submission is not a confirmed state transition or measured resource release.
The future execution loop must observe, revalidate and replan after each action.
"""

import json
import logging
import math
import re
import threading
import time
from dataclasses import asdict
from typing import Any, Callable, Optional, Protocol
from urllib.parse import quote

from llmsvc.state import Action, StateSnapshot

LOG = logging.getLogger("llmsvc.actions")


class ActionExecutor(Protocol):
    """Existing execution interface under the scheduler's global action lock.

    Dry-run invokes no writer or persistent intent/accounting mutation. Resource
    waits release the associated condition lock, then reacquire and revalidate;
    this single-request preparatory implementation performs no resource wait.
    """

    def execute(self, action: Action, *, dry_run: bool) -> dict[str, Any]:
        ...


class ActionDispatchError(RuntimeError):
    def __init__(self, reason: str, *, attempted: bool = False):
        super().__init__(reason)
        self.reason = reason
        # A transport error/timeout can follow a remote mutation. Never assume
        # rollback or budget release from an error or a successful submission.
        self.attempted = attempted


def _known(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class ModelActionDispatcher:
    """Prepare sleep/direct-stop requests under the existing scheduler RLock.

    http_request(method, path, *, deadline) returns an HTTP status integer.
    stop_unit(unit, *, deadline) returns the systemctl exit code. Both are
    REQUIRED injected operations with no live defaults; they must apply the
    remaining monotonic deadline to their I/O. Python cannot preempt an arbitrary
    callback, so completion after the deadline is reported as uncertain failure.

    Callers supply fresh scheduler.snapshot and its SAME action_lock. No polling,
    resource wait, lease/accounting mutation or implicit wrapper sleep occurs.
    Wake/cold-start dispatch is intentionally deferred: a synchronous upstream
    request can call back into placement and must not wait holding this lock.
    """

    def __init__(self, *, action_lock, snapshot: Callable[[], StateSnapshot],
                 http_request: Callable[..., int], stop_unit: Callable[..., int],
                 timeout_seconds: float, max_snapshot_age_seconds: float,
                 enabled: bool = False, monotonic=time.monotonic, wall_clock=time.time):
        if not isinstance(action_lock, type(threading.RLock())):
            raise ValueError("action_lock must be the scheduler RLock")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        for value in (timeout_seconds, max_snapshot_age_seconds):
            if not _known(value) or value <= 0:
                raise ValueError("timeouts must be finite positive numbers")
        if not all(callable(value) for value in (snapshot, http_request, stop_unit)):
            raise ValueError("snapshot and transport callbacks are required")
        self.action_lock = action_lock
        self.snapshot = snapshot
        self.http_request = http_request
        self.stop_unit = stop_unit
        self.timeout_seconds = timeout_seconds
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.enabled = enabled
        self.monotonic = monotonic
        self.wall_clock = wall_clock

    def _validate(self, action, snapshot, dry_run):
        if not isinstance(action, Action) or action.kind not in ("sleep", "stop"):
            raise ActionDispatchError("operation_not_enabled")
        if not isinstance(action.model, str) or not action.model or action.model in (".", ".."):
            raise ActionDispatchError("invalid_model")
        if (not isinstance(action.reason, str) or not action.reason
                or (action.gpu is not None and (type(action.gpu) is not int or action.gpu < 0))):
            raise ActionDispatchError("invalid_action")
        if not isinstance(snapshot, StateSnapshot):
            raise ActionDispatchError("unknown_snapshot")
        if not dry_run and snapshot.read_only is not False:
            raise ActionDispatchError("read_only")
        observed = snapshot.sampled_at
        now = self.wall_clock()
        if (not _known(observed) or not _known(now) or not 0 <= now - observed <= self.max_snapshot_age_seconds
                or snapshot.errors):
            raise ActionDispatchError("unknown_or_stale_snapshot")
        models = [model for model in snapshot.models if model.name == action.model]
        if len(models) != 1:
            raise ActionDispatchError("unknown_or_duplicate_model")
        model = models[0]
        if model.state not in ("awake", "sleeping") or model.unit_active is not True:
            raise ActionDispatchError("model_state_changed")
        if action.gpu is not None and action.gpu != model.gpu:
            raise ActionDispatchError("placement_changed")
        for pin in snapshot.pins:
            if pin.model == model.name and (not _known(pin.until) or pin.until > now):
                raise ActionDispatchError("pinned")
        activity = [item for item in snapshot.activity if item.model == model.name]
        if len(activity) != 1 or type(activity[0].in_flight) is not int or activity[0].in_flight < 0:
            raise ActionDispatchError("unknown_in_flight")
        if activity[0].in_flight:
            raise ActionDispatchError("in_flight")
        if action.kind == "stop":
            if model.is_default is not False:
                raise ActionDispatchError("default_or_unknown_role")
            if not isinstance(model.unit, str) or not re.fullmatch(r"vllm-[A-Za-z0-9_.@-]+\.service", model.unit):
                raise ActionDispatchError("invalid_unit")
            if sum(item.unit == model.unit for item in snapshot.models) != 1:
                raise ActionDispatchError("ambiguous_unit")
        else:
            if model.state != "awake":
                raise ActionDispatchError("model_state_changed")
            memory = snapshot.memory
            sleepers = [item for item in snapshot.models if item.state == "sleeping"]
            amounts = (model.weights_gb, memory.host_available_gb, memory.sleeping_weights_gb,
                       memory.budget_gb, memory.host_min_available_gb)
            if (not all(_known(value) for value in amounts)
                    or any(not _known(item.weights_gb) for item in sleepers)
                    or any(item.state == "unknown" for item in snapshot.models)):
                raise ActionDispatchError("unknown_memory")
            sleeping = max(memory.sleeping_weights_gb, sum(item.weights_gb for item in sleepers))
            if (sleeping + model.weights_gb > memory.budget_gb
                    or memory.host_available_gb - model.weights_gb < memory.host_min_available_gb):
                raise ActionDispatchError("memory_budget")
        return model

    def execute(self, action: Action, *, dry_run: bool, deadline: Optional[float] = None) -> dict[str, Any]:
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        if not dry_run and not self.enabled:
            raise ActionDispatchError("executor_disabled")
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise ValueError("deadline must be finite")
        limit = self.monotonic() + self.timeout_seconds
        if deadline is not None:
            limit = min(limit, deadline)
        remaining = limit - self.monotonic()
        if remaining <= 0 or not self.action_lock.acquire(timeout=remaining):
            raise ActionDispatchError("deadline_exceeded")
        attempted = False
        try:
            try:
                snapshot = self.snapshot()
            except Exception as exc:
                raise ActionDispatchError("snapshot_unavailable") from exc
            model = self._validate(action, snapshot, dry_run)
            if self.monotonic() >= limit:
                raise ActionDispatchError("deadline_exceeded")
            if dry_run:
                result = {"would": [asdict(action)]}
            else:
                attempted = True
                try:
                    if action.kind == "sleep":
                        status = self.http_request("POST", "/api/models/unload/" + quote(action.model, safe=""), deadline=limit)
                        successful = type(status) is int and 200 <= status < 300
                    else:
                        # Direct stop only. There is no proven implicit cmdStop
                        # or wrapper-sleep guarantee, and no hidden sleep call.
                        status = self.stop_unit(model.unit, deadline=limit)
                        successful = type(status) is int and status == 0
                except Exception as exc:
                    raise ActionDispatchError("transport_error", attempted=True) from exc
                if self.monotonic() >= limit:
                    raise ActionDispatchError("deadline_exceeded", attempted=True)
                if not successful:
                    raise ActionDispatchError("transport_rejected", attempted=True)
                result = {"action": asdict(action), "status": "submitted", "confirmed": False}
            LOG.info(json.dumps({"kind": "action_dispatch", "dry_run": dry_run, **result}, allow_nan=False))
            return result
        except ActionDispatchError as exc:
            LOG.warning(json.dumps({"kind": "action_dispatch_failed", "reason": exc.reason,
                                    "attempted": attempted or exc.attempted, "dry_run": dry_run}))
            raise
        finally:
            self.action_lock.release()
