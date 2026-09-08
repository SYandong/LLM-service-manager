# Generated-By: Codex / gpt-6-astra
"""Guarded single-action dispatch; no live transport or HTTP mount is defaulted.

Submission is not a confirmed state transition or measured resource release.
The future execution loop must observe, revalidate and replan after each action.
"""

import json
import logging
import math
import re
import sqlite3
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


class ManagedModelTransport:
    """Bounded transports limited to configured model paths and unit names."""

    def __init__(self, *, swap_url, models, systemctl, monotonic=time.monotonic, run=None):
        import subprocess
        from urllib.parse import urlsplit
        from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        parts = urlsplit(swap_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError("model action swap_url must be an HTTP(S) origin without credentials/query/path")
        if not isinstance(systemctl, str) or not systemctl:
            raise ValueError("configured systemctl command is required")
        if not isinstance(models, dict) or not models or any(not isinstance(value, dict) for value in models.values()):
            raise ValueError("model actions require a nonempty configured model mapping")
        self.models = {name: dict(value) for name, value in models.items()}
        self.units = {}
        self.paths = set()
        for name, model in self.models.items():
            if not isinstance(name, str) or not name or name in (".", ".."):
                raise ValueError("invalid configured model name")
            unit = model.get("unit", "vllm-" + name + ".service")
            if not isinstance(unit, str) or not re.fullmatch(r"vllm-[A-Za-z0-9_.@-]+\.service", unit):
                raise ValueError("model actions require a configured managed vllm unit")
            if unit in self.units.values():
                raise ValueError("model action unit aliases are ambiguous")
            self.units[name] = unit
            encoded = quote(name, safe="")
            self.paths.update((("POST", "/api/models/unload/" + encoded), ("GET", "/upstream/" + encoded + "/")))
        self.swap_url = swap_url.rstrip("/")
        self.systemctl = systemctl
        self.monotonic = monotonic
        self.run = run or subprocess.run
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _remaining(self, deadline):
        remaining = deadline - self.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("model action deadline exceeded")
        return remaining

    def unit_for_model(self, model):
        if model not in self.units:
            raise ActionDispatchError("unmanaged_model")
        return self.units[model]

    def http_request(self, method, path, *, deadline):
        from urllib.error import HTTPError
        from urllib.request import Request
        if (method, path) not in self.paths:
            raise ActionDispatchError("unapproved_model_path")
        request = Request(self.swap_url + path, method=method)
        try:
            with self.opener.open(request, timeout=self._remaining(deadline)) as response:
                return response.status
        except HTTPError as exc:
            status = exc.code
            exc.close()
            return status

    def stop_unit(self, unit, *, deadline):
        if unit not in self.units.values():
            raise ActionDispatchError("unmanaged_unit")
        result = self.run([self.systemctl, "stop", unit], capture_output=True, text=True,
                          check=False, timeout=self._remaining(deadline))
        return result.returncode


class ModelActionController:
    """Explicit free/wake operations; no automatic policy loop or lease writer.

    A free operation serializes its measurement window. Pending model operations
    are protected from other controller operations while waits release the lock.
    Reported release is observed net free memory, not causal attribution or a
    policy estimate. Accounting is never released here.
    """

    def __init__(self, scheduler, transport, *, monotonic=time.monotonic, settings=None):
        from llmsvc.policy import PolicySettings
        self.scheduler = scheduler
        self.transport = transport
        self.monotonic = monotonic
        self.settings = settings or PolicySettings()
        self.pending = set()
        self.free_active = False
        self.dispatcher = ModelActionDispatcher(
            action_lock=scheduler.action_lock, snapshot=self._snapshot,
            http_request=transport.http_request, stop_unit=transport.stop_unit,
            timeout_seconds=scheduler.config.request_timeout_seconds,
            max_snapshot_age_seconds=scheduler.config.max_snapshot_age_seconds,
            enabled=True, monotonic=monotonic, wall_clock=scheduler.clock)

    def _snapshot(self):
        from dataclasses import replace
        snapshot = self.scheduler.snapshot()
        # Trusted configured default metadata may strengthen, never weaken, protection.
        models = tuple(replace(model, is_default=True)
                       if self.transport.models.get(model.name, {}).get("is_default") is True else model
                       for model in snapshot.models)
        return replace(snapshot, models=models)

    def _locked(self, deadline):
        from contextlib import contextmanager
        @contextmanager
        def lock():
            remaining = deadline - self.monotonic()
            if remaining <= 0 or not self.scheduler.action_lock.acquire(timeout=remaining):
                raise ActionDispatchError("deadline_exceeded")
            try:
                yield
            finally:
                self.scheduler.action_lock.release()
        return lock()

    def _enabled(self):
        if self.scheduler.config.read_only:
            raise ActionDispatchError("read_only")
        if not self.scheduler.config.model_actions_enabled:
            raise ActionDispatchError("operation_not_enabled")

    def _refresh(self, deadline):
        if self.monotonic() >= deadline or self.scheduler.stopping.is_set():
            raise ActionDispatchError("deadline_exceeded")
        self.scheduler.sample_once()  # Collector I/O and publication run without our action lock.
        return self._snapshot()

    def _fresh(self, snapshot):
        now = self.scheduler.clock()
        return (_known(snapshot.sampled_at) and _known(now)
                and 0 <= now - snapshot.sampled_at <= self.scheduler.config.max_snapshot_age_seconds
                and not snapshot.errors)

    def _model(self, snapshot, name):
        unit = self.transport.unit_for_model(name)
        models = [model for model in snapshot.models if model.name == name]
        if len(models) != 1:
            raise ActionDispatchError("unknown_or_duplicate_model")
        model = models[0]
        if model.unit not in (None, unit):
            raise ActionDispatchError("configured_unit_mismatch")
        return model

    def plan_free(self, snapshot, **payload):
        from dataclasses import replace
        from llmsvc.policy import plan_free
        from llmsvc.state import Pin
        protected = {}
        for model in snapshot.models:
            if model.name not in self.transport.models:
                protected[model.name] = "unmanaged_model"
            elif model.unit != self.transport.unit_for_model(model.name):
                protected[model.name] = "configured_unit_mismatch"
            elif model.name in self.pending:
                protected[model.name] = "operation_in_progress"
        # Detached policy-only protection, never published or persisted as user pins.
        until = self.scheduler.clock() + self.scheduler.config.free_timeout_seconds + 1
        pins = snapshot.pins + tuple(Pin(name, until, "operation_guard") for name in protected)
        decision = plan_free(replace(snapshot, pins=pins), settings=self.settings, **payload)
        blockers = tuple(replace(blocker, reason=protected[blocker.model])
                         if blocker.model in protected and blocker.reason == "pinned_until" else blocker
                         for blocker in decision.blocked_by)
        return replace(decision, blocked_by=blockers)

    @staticmethod
    def _metric(snapshot, *, ram, gpu_ids):
        if ram:
            return snapshot.memory.host_available_gb if _known(snapshot.memory.host_available_gb) else None
        selected = [gpu for gpu in snapshot.gpus if gpu.index in gpu_ids]
        if len(selected) != len(gpu_ids) or not selected or any(not _known(gpu.free_gb) for gpu in selected):
            return None
        return sum(gpu.free_gb for gpu in selected)

    def _effect(self, action, snapshot):
        models = [model for model in snapshot.models if model.name == action.model]
        if len(models) != 1:
            return False
        model = models[0]
        if model.unit != self.transport.unit_for_model(action.model):
            return False
        if action.kind == "sleep":
            return (model.state == "sleeping" and model.is_sleeping is True and model.unit_active is True
                    and (action.gpu is None or model.gpu == action.gpu))
        return model.state == "stopped" and model.unit_active is False

    def _wait_effect(self, action, deadline):
        last = self._snapshot()
        effect_seen_at = None
        while self.monotonic() < deadline and not self.scheduler.stopping.is_set():
            try:
                last = self._refresh(deadline)
                with self._locked(deadline):
                    applied = self._fresh(last) and self._effect(action, last)
                    if applied and effect_seen_at is not None and last.sampled_at > effect_seen_at:
                        # This round started after a prior round had confirmed
                        # the effect. Its memory probes are not pre-effect data
                        # from the same non-atomic collector round.
                        return last, True
                    effect_seen_at = last.sampled_at if applied else None
                    self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                             max(0, deadline - self.monotonic())))
            except ActionDispatchError as exc:
                if exc.reason != "deadline_exceeded":
                    raise
                break
        return last, False

    def free(self, payload, *, by, dry_run=False):
        if dry_run:
            return self.scheduler.preview("free", payload)
        if not isinstance(payload, dict):
            raise ValueError("free body must be a JSON object")
        self.scheduler._keys(payload, {"gpu", "ram", "need_gb"})
        ram = payload.get("ram", False)
        gpu = payload.get("gpu")
        need = payload.get("need_gb")
        if type(ram) is not bool or ("gpu" in payload and (type(gpu) is not int or gpu < 0)):
            raise ValueError("invalid free selection")
        if need is not None and not _known(need):
            raise ValueError("invalid need_gb")
        deadline = self.monotonic() + self.scheduler.config.free_timeout_seconds
        with self._locked(deadline):
            self._enabled()
            if self.free_active:
                raise ActionDispatchError("free_in_progress")
            self.free_active = True
        result = {"freed_gb": None, "slept": [], "stopped": [], "skipped": [], "status": "blocked",
                  "measurement": "net_host_available_gib" if ram else "net_gpu_free_gib",
                  "measured_at": None, "measurement_complete": False}
        confirmed = set()
        measured_effects = 0
        pending = None
        before = None
        gpu_ids = set()
        last = self._snapshot()

        def record(snapshot, action=None):
            nonlocal last, measured_effects
            last = snapshot
            metric = self._metric(snapshot, ram=ram, gpu_ids=gpu_ids)
            if before is not None and metric is not None and self._fresh(snapshot):
                result["freed_gb"] = max(0.0, metric - before)
                result["measured_at"] = snapshot.sampled_at
                result["measurement_complete"] = True
                if action is not None:
                    measured_effects += 1
            elif action is not None and measured_effects == 0:
                result["freed_gb"] = None
                result["measured_at"] = None
            if action is not None:
                key = "slept" if action.kind == "sleep" else "stopped"
                if action.model not in result[key]:
                    result[key].append(action.model)
                confirmed.add((action.kind, action.model))

        try:
            last = self._refresh(deadline)
            gpu_ids = {gpu} if gpu is not None else {item.index for item in last.gpus}
            before = self._metric(last, ram=ram, gpu_ids=gpu_ids)
            record(last)
            while self.monotonic() < deadline:
                with self._locked(deadline):
                    self._enabled()
                    snapshot = self._snapshot()
                    if not self._fresh(snapshot):
                        result["status"] = "partial" if confirmed else "blocked"
                        result["skipped"].append({"model": None, "reason": "unknown_or_stale_snapshot"})
                        break
                    if before is None:
                        result["skipped"].append({"model": None, "reason": "measurement_unavailable"})
                        break
                    if need is not None and result["freed_gb"] is not None and result["freed_gb"] >= need:
                        result["status"] = "complete"
                        result["skipped"] = [item for item in result["skipped"]
                                             if item["reason"] != "insufficient_reclaimable_memory"]
                        break
                    remaining = None if need is None else max(0, need - (result["freed_gb"] or 0))
                    decision = self.plan_free(snapshot, gpu=gpu, ram=ram, need_gb=remaining)
                    result["skipped"] = [asdict(blocker) for blocker in decision.blocked_by]
                    if not decision.actions:
                        result["status"] = "partial" if confirmed and result["skipped"] else ("blocked" if result["skipped"] else "complete")
                        break
                    action = decision.actions[0]  # Never execute the rest of an old plan.
                    if (action.kind, action.model) in confirmed:
                        result["status"] = "no_progress"
                        result["skipped"].append({"model": action.model, "reason": "repeated_action"})
                        break
                    self._model(snapshot, action.model)
                    if action.model in self.pending:
                        raise ActionDispatchError("operation_in_progress")
                    self.pending.add(action.model)
                    pending = action.model
                    previous_release = result["freed_gb"]
                    previous_host = snapshot.memory.host_available_gb
                    try:
                        self.dispatcher.execute(action, dry_run=False, deadline=deadline)
                    except ActionDispatchError as exc:
                        dispatch_error = exc
                    else:
                        dispatch_error = None
                if dispatch_error is not None:
                    # A failed request may have taken effect. Recollect once and
                    # preserve observed partial results, but do not keep acting.
                    observed = self._refresh(deadline)
                    if dispatch_error.attempted and self._fresh(observed) and self._effect(action, observed):
                        effect_seen_at = observed.sampled_at
                        observed = self._refresh(deadline)
                        if self._fresh(observed) and observed.sampled_at > effect_seen_at and self._effect(action, observed):
                            record(observed, action)
                    result["status"] = "partial" if confirmed else "failed"
                    result["error"] = dispatch_error.reason
                    result["error_model"] = action.model
                    result["measurement_complete"] = (not dispatch_error.attempted or (action.kind, action.model) in confirmed) and self._metric(observed, ram=ram, gpu_ids=gpu_ids) is not None
                    break
                observation_deadline = min(deadline, self.monotonic() + self.scheduler.config.action_observe_seconds)
                observed, applied = self._wait_effect(action, observation_deadline)
                if not applied:
                    result["status"] = "partial" if confirmed else ("timeout" if self.monotonic() >= deadline else "no_progress")
                    result["error"] = "effect_not_confirmed"
                    result["error_model"] = action.model
                    result["measurement_complete"] = False
                    break
                record(observed, action)
                if self._metric(observed, ram=ram, gpu_ids=gpu_ids) is None:
                    result["status"] = "partial"
                    result["error"] = "measurement_unavailable"
                    result["error_model"] = action.model
                    result["measurement_complete"] = False
                    break
                preparing_ram = (action.reason == "sleep_memory_admission" and _known(previous_host)
                                 and _known(observed.memory.host_available_gb)
                                 and observed.memory.host_available_gb > previous_host)
                if result["freed_gb"] <= previous_release and not preparing_ram:
                    result["status"] = "no_progress"
                    result["error"] = "no_measured_release"
                    result["error_model"] = action.model
                    break
                with self._locked(deadline):
                    self.pending.discard(pending)
                    pending = None
            else:
                result["status"] = "partial" if confirmed else "timeout"
                result["error"] = "deadline_exceeded"
                result["measurement_complete"] = False
        except ActionDispatchError as exc:
            result["status"] = "partial" if confirmed else "failed"
            result["error"] = exc.reason
            result["error_model"] = pending
            result["measurement_complete"] = False
        finally:
            with self.scheduler.changed:
                if pending is not None:
                    self.pending.discard(pending)
                self.free_active = False
                self.scheduler.emit("free_result", detail={"by": by, **result})
                self.scheduler.changed.notify_all()
        return result

    @staticmethod
    def _ready(model):
        return (model.state == "awake" and model.unit_active is True and model.health_ok is True
                and model.is_sleeping is False and model.swap_state == "ready")

    def wake_model(self, snapshot, name):
        if not self._fresh(snapshot):
            raise ActionDispatchError("unknown_or_stale_snapshot")
        model = self._model(snapshot, name)
        if model.name in self.pending:
            raise ActionDispatchError("operation_in_progress")
        if model.state not in ("awake", "sleeping", "stopped"):
            raise ActionDispatchError("model_state_changed")
        if model.state != "stopped" and model.unit != self.transport.unit_for_model(name):
            raise ActionDispatchError("configured_unit_mismatch")
        if model.state == "stopped":
            if model.unit_active is not False:
                raise ActionDispatchError("model_state_changed")
        elif (model.unit_active is not True or model.health_ok is not True
              or model.is_sleeping is not (model.state == "sleeping")):
            raise ActionDispatchError("model_state_changed")
        activity = [item for item in snapshot.activity if item.model == name]
        if len(activity) != 1 or type(activity[0].in_flight) is not int or activity[0].in_flight < 0:
            raise ActionDispatchError("unknown_in_flight")
        if model.is_default and model.state != "stopped" and model.gpu != self.settings.exclusive_gpu:
            raise ActionDispatchError("default_requires_exclusive_gpu")
        if self._ready(model):
            return model
        if activity[0].in_flight:
            raise ActionDispatchError("in_flight")
        peers = {item.name: item for item in snapshot.models}
        for name_pending in self.pending:
            peer = peers.get(name_pending)
            if peer is None or model.gpu is None or peer.gpu is None or model.gpu == peer.gpu:
                # Reserve no new budget here; simply avoid overlapping our own
                # wake admissions against the same (or not-yet-known) GPU.
                raise ActionDispatchError("operation_in_progress")
        memory = snapshot.memory
        if not all(_known(value) for value in (memory.host_available_gb, memory.sleeping_weights_gb,
                                               memory.budget_gb, memory.host_min_available_gb)):
            raise ActionDispatchError("unknown_memory")
        if model.state == "stopped":
            if not _known(model.weights_gb):
                raise ActionDispatchError("unknown_memory")
            if memory.host_available_gb - model.weights_gb < memory.host_min_available_gb:
                raise ActionDispatchError("memory_budget")
        if model.state == "sleeping":
            gpus = [gpu for gpu in snapshot.gpus if gpu.index == model.gpu]
            if (len(gpus) != 1 or not _known(gpus[0].free_gb) or not _known(model.budget_gb)
                    or not _known(model.resident_gb)):
                raise ActionDispatchError("unknown_gpu_capacity")
            if model.is_default and model.gpu != self.settings.exclusive_gpu:
                raise ActionDispatchError("default_requires_exclusive_gpu")
            if gpus[0].free_gb < max(0, model.budget_gb - model.resident_gb):
                raise ActionDispatchError("insufficient_gpu_memory")
        return model

    def wake(self, name, *, by, dry_run=False):
        if dry_run:
            return self.scheduler.preview("wake", {"model": name})
        started = self.monotonic()
        deadline = started + self.scheduler.config.wake_timeout_seconds
        result = {"model": name, "status": "blocked", "ready": False, "elapsed_seconds": 0.0, "cold_start": False}
        owned = False
        try:
            self._enabled()
            self._refresh(deadline)
            with self._locked(deadline):
                self._enabled()
                initial = self._snapshot()
                model = self.wake_model(initial, name)
                result["cold_start"] = model.state == "stopped"
                if self._ready(model):
                    result.update(status="ready", ready=True)
                    return result
                self.pending.add(name)
                owned = True
                self.scheduler.emit("wake_requested", model=name, detail={"by": by, "cold_start": result["cold_start"]})
            # No action lock during this request: it may synchronously reenter
            # /v1/place through the data-plane launcher before returning.
            try:
                status = self.transport.http_request("GET", "/upstream/" + quote(name, safe="") + "/", deadline=deadline)
                error = None if type(status) is int and (200 <= status < 300 or status == 404) else "upstream_rejected"
            except Exception:
                error = "upstream_error"
            if error:
                observed = self._refresh(deadline)
                candidates = [model for model in observed.models if model.name == name]
                ready = (self._fresh(observed) and observed.sampled_at > initial.sampled_at
                         and len(candidates) == 1 and self._ready(candidates[0])
                         and candidates[0].unit == self.transport.unit_for_model(name)
                         and (not candidates[0].is_default or candidates[0].gpu == self.settings.exclusive_gpu))
                result.update(status="partial" if ready else "failed", ready=ready, error=error)
                return result
            progress = None
            while self.monotonic() < deadline and not self.scheduler.stopping.is_set():
                observed = self._refresh(deadline)
                with self._locked(deadline):
                    candidates = [model for model in observed.models if model.name == name]
                    model = candidates[0] if len(candidates) == 1 else None
                    if model is not None and self._fresh(observed):
                        if model.unit not in (None, self.transport.unit_for_model(name)):
                            result.update(status="failed", error="configured_unit_mismatch")
                            break
                        if model.is_default and model.state == "awake" and model.gpu != self.settings.exclusive_gpu:
                            result.update(status="blocked", error="default_requires_exclusive_gpu")
                            break
                        if (observed.sampled_at > initial.sampled_at and self._ready(model)
                                and model.unit == self.transport.unit_for_model(name)):
                            result.update(status="ready", ready=True)
                            break
                    state = (model.state, model.swap_state) if model is not None else ("unknown", None)
                    if state != progress:
                        progress = state
                        self.scheduler.emit("wake_progress", model=name, detail={"state": state[0], "swap_state": state[1]})
                    self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                             max(0, deadline - self.monotonic())))
            else:
                result.update(status="timeout", error="readiness_timeout")
        except ActionDispatchError as exc:
            result.update(status="timeout" if exc.reason == "deadline_exceeded" else "blocked", error=exc.reason)
        finally:
            result["elapsed_seconds"] = max(0.0, self.monotonic() - started)
            with self.scheduler.changed:
                if owned:
                    self.pending.discard(name)
                self.scheduler.emit("wake_result", model=name, detail={"by": by, **result})
                self.scheduler.changed.notify_all()
        return result


class ReservationController:
    """Bounded evacuation outcome for a persisted intent (DESIGN §4.4).

    Only confirmed, configured sleeping daemons are candidates. A saved reserve
    does not authorize awake relocation, orphan adoption or automatic recovery.
    """

    def __init__(self, scheduler, *, accounting=None, monotonic=time.monotonic):
        self.scheduler = scheduler
        self.monotonic = monotonic
        self.controller = scheduler.model_actions
        if accounting is None and self.controller is not None:
            from llmsvc.leases import PlacementController
            accounting = PlacementController(scheduler, self.controller.transport, monotonic=monotonic)
        self.accounting = accounting

    def _plan(self, snapshot, reserve, *, stopped=()):
        from dataclasses import replace
        from llmsvc.policy import Decision, plan_reserve
        from llmsvc.state import Blocker, Pin
        sampled = snapshot.sampled_at
        if not _known(sampled) or not 0 <= self.scheduler.clock()-sampled <= self.scheduler.config.max_snapshot_age_seconds:
            return Decision(blocked_by=(Blocker(None, "unknown_or_stale_snapshot", reserve.gpu),))
        controller = self.controller
        enabled = self.scheduler.config.model_actions_enabled and controller is not None and self.accounting is not None
        models = []
        guarded = {}
        extra = []
        confirmed = {lease.model for lease in snapshot.leases if lease.status == "confirmed"}
        metadata = controller.transport.models if controller is not None else self.scheduler.config.collectors.get("models", {})
        for model in snapshot.models:
            if metadata.get(model.name, {}).get("is_default") is True:
                model = replace(model, is_default=True)
            models.append(model)
            if model.gpu is None and model.state != "stopped":
                extra.append(Blocker(model.name, "unknown_model_gpu"))
            if model.gpu != reserve.gpu:
                continue
            if model.state == "awake":
                extra.append(Blocker(model.name, "awake_model_untouched", reserve.gpu))
            elif model.state == "stopped" and model.name not in stopped:
                extra.append(Blocker(model.name, "exit_not_verified_by_reserve", reserve.gpu))
            if model.state == "sleeping" and enabled:
                unit = controller.transport.units.get(model.name)
                if unit is None or model.unit != unit or self.accounting.transport.units.get(model.name) != unit:
                    guarded[model.name] = "unmanaged_or_changed_unit"
                elif model.name not in confirmed:
                    guarded[model.name] = "unleased_model"
                elif model.name in controller.pending or controller.free_active:
                    guarded[model.name] = "operation_in_progress"
        for lease in snapshot.leases:
            if lease.gpu == reserve.gpu and lease.status in ("pending", "stale"):
                extra.append(Blocker(lease.model, "outstanding_lease", reserve.gpu))
        # Detached exclusions reuse the existing pure policy; no synthetic pin
        # is saved or published as user intent.
        pins = snapshot.pins + tuple(Pin(name, self.scheduler.clock()+self.scheduler.config.reserve_timeout_seconds+1,
                                        "reserve_guard") for name in guarded)
        decision = plan_reserve(replace(snapshot, models=tuple(models), pins=pins), gpu=reserve.gpu)
        blockers = tuple(replace(blocker, reason=guarded[blocker.model])
                         if blocker.model in guarded and blocker.reason == "pinned_until" else blocker
                         for blocker in decision.blocked_by) + tuple(extra)
        if not enabled:
            blockers += tuple(Blocker(action.model, "model_actions_not_enabled", reserve.gpu) for action in decision.actions)
            return replace(decision, actions=(), blocked_by=blockers)
        return replace(decision, blocked_by=blockers)

    def _intent_error(self, reserve):
        if self.scheduler.config.read_only:
            return "read_only"
        if self.scheduler.store is None or self.scheduler.store.read_only:
            return "intent_store_unavailable"
        current = self.scheduler.store.reserve(reserve.id)
        if current != reserve:
            return "reservation_removed_or_changed"
        if current.until <= self.scheduler.clock():
            return "reservation_expired"
        return None

    def evacuate(self, reserve, *, deadline=None, dry_run=False):
        """Return an observed outcome; the saved intent is never rolled back."""
        from llmsvc.leases import LeaseError
        from llmsvc.state import Blocker
        if dry_run:
            with self.scheduler.action_lock:
                decision = self._plan(self.scheduler.snapshot(), reserve)
                return {"would": [asdict(action) for action in decision.actions],
                        "blocked_by": [asdict(blocker) for blocker in decision.blocked_by]}
        if self.scheduler.config.read_only:
            raise ActionDispatchError("read_only")
        deadline = deadline if deadline is not None else self.monotonic()+self.scheduler.config.reserve_timeout_seconds
        stopped = []
        skipped = []
        error = None
        controller = self.controller
        try:
            while self.monotonic() < deadline:
                if self.scheduler.stopping.is_set():
                    error = "scheduler_stopping"
                    break
                remaining = deadline-self.monotonic()
                if not self.scheduler.action_lock.acquire(timeout=max(0, remaining)):
                    error = "deadline_exceeded"
                    break
                action = None
                failure = None
                try:
                    error = self._intent_error(reserve)
                    if error:
                        break
                    snapshot = self.scheduler.snapshot()
                    decision = self._plan(snapshot, reserve, stopped=stopped)
                    skipped = list(decision.blocked_by)
                    if not decision.actions:
                        break
                    action = decision.actions[0]
                    if action.kind != "stop":
                        error = "reserve_action_not_supported"
                        break
                    account = next((lease for lease in snapshot.leases if lease.model == action.model and lease.status == "confirmed"), None)
                    identity = self.accounting._inspect(action.model, deadline)
                    if account is None or not identity.active or identity.lease_id != account.lease_id:
                        error = "unit_identity_unconfirmed"
                        skipped.append(Blocker(action.model, error, reserve.gpu))
                        break
                    # Expiry or a newly published wake/protection can invalidate
                    # admission during the bounded identity probe. Recheck now.
                    error = self._intent_error(reserve)
                    if not error and not self.scheduler.config.model_actions_enabled:
                        error = "model_actions_not_enabled"
                    models = [model for model in self.scheduler.snapshot().models if model.name == action.model]
                    if not error and (len(models) != 1 or models[0].state != "sleeping" or models[0].is_sleeping is not True):
                        error = "model_no_longer_sleeping"
                    if error:
                        skipped.append(Blocker(action.model, error, reserve.gpu))
                        break
                    controller.pending.add(action.model)
                    try:
                        controller.dispatcher.execute(action, dry_run=False, deadline=deadline)
                    except ActionDispatchError as exc:
                        failure = exc.reason
                    except Exception:
                        controller.pending.discard(action.model)
                        raise
                finally:
                    self.scheduler.action_lock.release()
                if action is None:
                    break
                try:
                    confirmed = self.accounting._observe_victim(action,
                        min(deadline, self.monotonic()+self.scheduler.config.action_observe_seconds), reconcile_exit=True)
                    if confirmed:
                        stopped.append(action.model)
                    self.scheduler.emit("reserve_action_result", model=action.model, detail={
                        "reserve_id": reserve.id, "action": asdict(action), "confirmed": confirmed,
                        "error": failure if failure else (None if confirmed else "exit_unconfirmed"), "dry_run": False})
                    if failure or not confirmed:
                        error = failure or ("deadline_exceeded" if self.monotonic() >= deadline else "exit_unconfirmed")
                        skipped.append(Blocker(action.model, error, reserve.gpu))
                        break
                except LeaseError:
                    error = "deadline_exceeded"
                    break
                finally:
                    with self.scheduler.changed:
                        controller.pending.discard(action.model)
                        self.scheduler.changed.notify_all()
            else:
                error = "deadline_exceeded"
        except (sqlite3.Error, OSError, ValueError):
            error = "intent_store_unavailable"
        except Exception as exc:
            LOG.warning(json.dumps({"kind": "reserve_execution_error", "reserve_id": reserve.id,
                                    "error_type": type(exc).__name__}))
            error = "reserve_execution_failed"
        if error and not any(blocker.reason == error for blocker in skipped):
            skipped.append(Blocker(None, error, reserve.gpu))
        incomplete = bool(skipped or error)
        result = {"status": ("partial" if stopped else "blocked") if incomplete else "complete",
                  "stopped": stopped, "skipped": [asdict(blocker) for blocker in skipped]}
        if error:
            result["error"] = error
        self.scheduler.emit("reserve_result", detail={"reserve_id": reserve.id, **result, "dry_run": False})
        return result
