# Generated-By: Codex / gpt-6-astra
"""Read-only sampling and an event stream sharing the future action lock."""

import copy
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime
from typing import Callable, Optional

from llmsvc.config import SchedulerConfig
from llmsvc.state import Event, MemoryState, Pin, Reserve, StateSnapshot
from llmsvc.store import IntentStore, finite_positive, nonempty, validate_pin, validate_reserve

LOG = logging.getLogger("llmsvc.scheduler")


class Scheduler:
    def __init__(self, config: SchedulerConfig,
                 collect: Optional[Callable[[], StateSnapshot]] = None, *,
                 store: Optional[IntentStore] = None, clock: Callable[[], float] = time.time):
        self.config = config
        self.collect = collect
        # One lock for action/accounting and publication. Slow read-only probes
        # run outside it; Condition.wait releases it for other handlers.
        self.store = store
        self.clock = clock
        self.action_lock = store.action_lock if store is not None else threading.RLock()
        self.changed = threading.Condition(self.action_lock)
        self._snapshot = self._unknown("not_sampled")
        self._events = deque(maxlen=config.event_history_size)
        self._next_event_id = 1
        self.stopping = threading.Event()
        self._thread = None

    def _unknown(self, reason: str) -> StateSnapshot:
        return StateSnapshot(memory=MemoryState(
            budget_gb=self.config.memory_budget_gb,
            host_min_available_gb=self.config.host_min_available_gb,
        ), errors=(reason,))

    def snapshot(self) -> StateSnapshot:
        with self.action_lock:
            snapshot = self._snapshot
            if self.store is not None:
                try:
                    pins, reserves = self.store.active(self.clock())
                    snapshot = replace(snapshot, pins=pins, reserves=reserves)
                except Exception:
                    snapshot = replace(snapshot, errors=snapshot.errors + ("intent_store_unavailable",))
            return snapshot


    def preview(self, operation: str, payload: dict) -> dict:
        """Pure policy/intent preview; no executor, event append or store writer."""
        from llmsvc.policy import plan_free, plan_reserve
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        with self.action_lock:
            snapshot = self.snapshot()
            now = self.clock()
            sampled = snapshot.sampled_at
            if (isinstance(sampled, bool) or not isinstance(sampled, (int, float))
                    or not math.isfinite(sampled) or sampled > now
                    or now - sampled > self.config.max_snapshot_age_seconds):
                snapshot = replace(snapshot, errors=snapshot.errors + ("stale_snapshot",))
            decision = None
            if operation == "free":
                self._keys(payload, {"gpu", "ram", "need_gb"})
                if "gpu" in payload:
                    self._gpu(payload["gpu"], snapshot)
                if "ram" in payload and type(payload["ram"]) is not bool:
                    raise ValueError("ram must be a boolean")
                if payload.get("need_gb") is not None:
                    need = payload["need_gb"]
                    if isinstance(need, bool) or not isinstance(need, (int, float)) or not math.isfinite(need) or need < 0:
                        raise ValueError("need_gb must be finite and nonnegative")
                decision = plan_free(snapshot, **payload)
                result = {"would": [asdict(a) for a in decision.actions],
                          "estimated_freed_gb": decision.estimated_freed_gb}
            elif operation == "pin":
                self._keys(payload, {"model", "until", "by"}, required={"model", "until", "by"})
                pin = Pin(payload["model"], self._until(payload["until"], now), payload["by"])
                validate_pin(pin)
                if not any(m.name == pin.model for m in snapshot.models):
                    raise ValueError("unknown model")
                result = {"would": [{"kind": "pin", **asdict(pin)}]}
            elif operation == "reserve":
                self._keys(payload, {"gpu", "size_gb", "until", "by"}, required={"gpu", "size_gb", "until", "by"})
                self._gpu(payload["gpu"], snapshot)
                reserve = Reserve("preview", payload["gpu"], payload["size_gb"],
                                  self._until(payload["until"], now), payload["by"])
                validate_reserve(reserve)
                decision = plan_reserve(snapshot, gpu=reserve.gpu)
                record = asdict(reserve)
                del record["id"]  # A preview allocates no persistent ID.
                result = {"would": [{"kind": "reserve", **record}] + [asdict(a) for a in decision.actions]}
            elif operation in ("unpin", "unreserve"):
                key = "model" if operation == "unpin" else "id"
                self._keys(payload, {key}, required={key})
                nonempty(payload[key], key)
                result = {"would": [{"kind": operation, key: payload[key]}]}
            else:
                raise ValueError("unsupported preview operation")
            result["blocked_by"] = [asdict(b) for b in decision.blocked_by] if decision else []
            if snapshot.errors:
                result = {"would": [], "blocked_by": [{"model": None, "reason": error} for error in snapshot.errors]}
            LOG.info(json.dumps({"kind": "action_preview", "operation": operation, "dry_run": True, **result}, allow_nan=False))
            return result

    @staticmethod
    def _keys(payload, allowed, required=frozenset()):
        if set(payload) - allowed or required - set(payload):
            raise ValueError("invalid or missing request fields")

    @staticmethod
    def _gpu(gpu, snapshot):
        if type(gpu) is not int or gpu < 0:
            raise ValueError("gpu must be a nonnegative integer")
        if not any(g.index == gpu for g in snapshot.gpus):
            raise ValueError("unknown GPU")

    @staticmethod
    def _until(value, now):
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("until requires a timezone")
            value = parsed.timestamp()
        value = finite_positive(value, "until")
        if value <= now:
            raise ValueError("until must be in the future")
        return value

    def emit(self, kind: str, *, model: Optional[str] = None,
             detail: Optional[dict] = None) -> Event:
        with self.changed:
            event = Event(self._next_event_id, time.time(), kind, model,
                          copy.deepcopy(detail or {}))
            # Validate before publishing to keep JSON/SSE consumers usable.
            message = json.dumps(asdict(event), allow_nan=False)
            self._next_event_id += 1
            self._events.append(event)
            LOG.info(message)
            self.changed.notify_all()
            return copy.deepcopy(event)

    def events_since(self, cursor: int, timeout: float = 0.0) -> tuple[Event, ...]:
        with self.changed:
            self.changed.wait_for(
                lambda: self.stopping.is_set() or any(e.id > cursor for e in self._events),
                timeout=timeout,
            )
            return tuple(copy.deepcopy(e) for e in self._events if e.id > cursor)

    def sample_once(self) -> StateSnapshot:
        try:
            snapshot = self.collect() if self.collect else self._unknown("collectors_not_configured")
            if not isinstance(snapshot, StateSnapshot):
                raise TypeError("collector must return StateSnapshot")
            snapshot = replace(
                snapshot, sampled_at=snapshot.sampled_at if snapshot.sampled_at is not None else time.time(),
                read_only=True,
                memory=replace(snapshot.memory, budget_gb=self.config.memory_budget_gb,
                               host_min_available_gb=self.config.host_min_available_gb),
            )
            json.dumps(snapshot.to_dict(), allow_nan=False)
        except Exception as exc:
            # Never serve a stale healthy snapshot as current after probe failure.
            snapshot = self._unknown("collection_failed")
            self.emit("collection_error", detail={"error_type": type(exc).__name__})
        with self.changed:
            self._snapshot = snapshot
            self.emit("state", detail={"sampled_at": snapshot.sampled_at,
                                       "errors": list(snapshot.errors)})
            self.changed.notify_all()
        return self.snapshot()

    def _run(self):
        while not self.stopping.is_set():
            started = time.monotonic()
            self.sample_once()
            delay = max(0.0, self.config.sample_interval_seconds - (time.monotonic() - started))
            if self.stopping.wait(delay):
                break

    def start(self):
        with self.action_lock:
            if self._thread is not None:
                raise RuntimeError("scheduler already started")
            if self.stopping.is_set():
                raise RuntimeError("scheduler already stopped")
            self._thread = threading.Thread(target=self._run, name="llmsvc-sampler", daemon=True)
            self._thread.start()

    def stop(self):
        self.stopping.set()
        with self.changed:
            self.changed.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=self.config.request_timeout_seconds)
