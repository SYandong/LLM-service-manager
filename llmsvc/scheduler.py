# Generated-By: Codex / gpt-6-astra
"""Sampling, usage and opt-in pin intent writes under one accounting lock."""

import copy
import json
import logging
import math
import sqlite3
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


class IntentWriteError(RuntimeError):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


class DataPlaneBridge:
    """One bounded consumer; producer drain always finishes before publication.

    A busy action lock retains one detached batch, never blocks the source
    reader, and lets its bounded buffer report local discard counts next round.
    """

    def __init__(self, scheduler, relay):
        self.scheduler = scheduler
        self.relay = relay
        self.pending = None
        self.thread = None
        self.stopping = threading.Event()
        self.closed = False
        self.shutdown_discard = None

    def start(self):
        if self.closed or self.thread is not None:
            raise RuntimeError("data-plane bridge already started or closed")
        self.relay.start()
        self.thread = threading.Thread(target=self._run, name="llmsvc-event-relay", daemon=True)
        self.thread.start()

    def drain_once(self, max_events=None):
        # Only the consumer thread calls this, or close() after joining it.
        if self.pending is None:
            self.pending = self.relay.drain(max_events=self.scheduler.config.data_plane_event_batch_size if max_events is None else max_events)
        if not self.scheduler.action_lock.acquire(blocking=False):
            return False
        try:
            batch = self.pending
            for event in batch["events"]:
                self.scheduler.emit(event["kind"], model=event["model"], detail=event["detail"])
            if batch["dropped"]:
                self.scheduler.emit("data_plane_dropped", detail={
                    "source": "llama-swap", "received_at": self.scheduler.clock(),
                    "trusted_for_quiet": False, "dropped": batch["dropped"],
                    "dropped_by_reason": batch["dropped_by_reason"], "upstream_loss_unknown": True})
            self.pending = None
            return True
        finally:
            self.scheduler.action_lock.release()

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.drain_once()
            except Exception as exc:
                LOG.warning(json.dumps({"kind": "data_plane_bridge_error", "error_type": type(exc).__name__}))
            self.stopping.wait(self.scheduler.config.data_plane_event_interval_seconds)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopping.set()
        try:
            self.relay.close()  # Interrupt and join the upstream reader first.
        finally:
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=self.scheduler.config.request_timeout_seconds)
                if self.thread.is_alive():
                    raise RuntimeError("data-plane bridge consumer did not stop")
            # Producer is closed. At most one pending batch plus one full source
            # buffer is forwarded; never loop waiting for the action lock.
            self.drain_once()
            if self.pending is None:
                self.drain_once(max_events=self.scheduler.config.data_plane_event_capacity)
            if self.pending is not None:
                remainder = self.relay.drain(max_events=self.scheduler.config.data_plane_event_capacity)
                reasons = dict(self.pending["dropped_by_reason"])
                for reason, count in remainder["dropped_by_reason"].items():
                    reasons[reason] = reasons.get(reason, 0) + count
                self.shutdown_discard = {
                    "kind": "data_plane_bridge_shutdown_discard", "source": "llama-swap",
                    "received_at": self.scheduler.clock(), "trusted_for_quiet": False,
                    "unpublished_events": len(self.pending["events"]) + len(remainder["events"]),
                    "dropped": self.pending["dropped"] + remainder["dropped"],
                    "dropped_by_reason": reasons, "upstream_loss_unknown": True}
                LOG.warning(json.dumps(self.shutdown_discard))
                self.pending = None


class Scheduler:
    def __init__(self, config: SchedulerConfig,
                 collect: Optional[Callable[[], StateSnapshot]] = None, *,
                 usage: Optional[Callable[..., dict]] = None,
                 store: Optional[IntentStore] = None, clock: Callable[[], float] = time.time,
                 event_relay=None):
        self.config = config
        self.collect = collect
        self.model_actions = None
        self.placement = None
        self._usage = usage
        self._collector_closed = False
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
        self.sample_requested = threading.Event()
        self.events_closed = threading.Event()
        if event_relay is not None and not config.data_plane_events_enabled:
            raise ValueError("data-plane bridge requires explicit opt-in")
        self.event_bridge = DataPlaneBridge(self, event_relay) if event_relay is not None else None
        self._thread = None
        self._sample_started = 0
        self._sample_published = 0

    def _unknown(self, reason: str) -> StateSnapshot:
        return StateSnapshot(memory=MemoryState(
            budget_gb=self.config.memory_budget_gb,
            host_min_available_gb=self.config.host_min_available_gb,
        ), errors=(reason,), read_only=self.config.read_only)

    def snapshot(self) -> StateSnapshot:
        with self.action_lock:
            snapshot = self._snapshot
            if self.store is not None:
                try:
                    pins, reserves = self.store.active(self.clock())
                    snapshot = replace(snapshot, pins=pins, reserves=reserves)
                    from llmsvc.leases import accounting_snapshot
                    snapshot = accounting_snapshot(snapshot, self.store.leases())
                except Exception:
                    snapshot = replace(snapshot, errors=snapshot.errors + ("intent_store_unavailable",))
            return snapshot


    def preview(self, operation: str, payload: dict) -> dict:
        """Pure policy/intent preview; no executor, event append or store writer."""
        if operation in ("place", "confirm", "release"):
            if self.placement is None:
                raise IntentWriteError(405, "operation_not_enabled")
            result = self.placement.preview(operation, payload)
            LOG.info(json.dumps({"kind": "action_preview", "operation": operation, "dry_run": True, **result}, allow_nan=False))
            return result
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
                decision = self.model_actions.plan_free(snapshot, **payload) if self.model_actions else plan_free(snapshot, **payload)
                result = {"would": [asdict(a) for a in decision.actions],
                          "estimated_freed_gb": decision.estimated_freed_gb}
            elif operation == "wake":
                self._keys(payload, {"model"}, required={"model"})
                nonempty(payload["model"], "model")
                if self.model_actions is None:
                    return {"would": [], "blocked_by": [{"model": payload["model"], "reason": "operation_not_enabled"}]}
                from llmsvc.actions import ActionDispatchError
                try:
                    model = self.model_actions.wake_model(snapshot, payload["model"])
                    result = {"would": [] if self.model_actions._ready(model) else [
                        {"kind": "wake", "model": model.name, "reason": "user_wake", "gpu": model.gpu}]}
                except ActionDispatchError as exc:
                    return {"would": [], "blocked_by": [{"model": payload["model"], "reason": exc.reason}]}
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

    def run_model_action(self, operation: str, payload: dict, *, source_ip: str) -> dict:
        from llmsvc.actions import ActionDispatchError
        if self.config.read_only:
            raise IntentWriteError(405, "read_only")
        if not self.config.model_actions_enabled or self.model_actions is None:
            raise IntentWriteError(405, "operation_not_enabled")
        owner = self.config.owner_for_ip(source_ip)
        try:
            if operation == "free":
                return self.model_actions.free(payload, by=owner)
            if operation == "wake":
                self._keys(payload, {"model"}, required={"model"})
                nonempty(payload["model"], "model")
                return self.model_actions.wake(payload["model"], by=owner)
            raise IntentWriteError(405, "operation_not_enabled")
        except ActionDispatchError as exc:
            status = 409 if exc.reason in ("free_in_progress", "operation_in_progress") else 503
            raise IntentWriteError(status, exc.reason) from exc

    def run_placement(self, operation, payload):
        if self.placement is None:
            raise IntentWriteError(405, "operation_not_enabled")
        try:
            if operation == "place":
                return self.placement.place(payload)
            return self.placement.finish(operation, payload["lease_id"])
        except (sqlite3.Error, OSError) as exc:
            raise IntentWriteError(503, "intent_store_unavailable") from exc

    def write_pin(self, operation: str, payload: dict, *, source_ip: str) -> dict:
        """Persist pin intent only; model lifecycle actions remain disabled."""
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        with self.changed:
            if self.config.read_only:
                raise IntentWriteError(405, "read_only")
            if operation not in ("pin", "unpin"):
                raise IntentWriteError(405, "operation_not_enabled")
            if self.store is None or self.store.read_only:
                raise IntentWriteError(503, "intent_store_unavailable")
            owner = self.config.owner_for_ip(source_ip)
            if operation == "pin":
                self._keys(payload, {"model", "until", "by"}, required={"model", "until", "by"})
                model = nonempty(payload["model"], "model")
                nonempty(payload["by"], "by")  # Existing request shape; not authoritative.
                known = any(item.name == model for item in self._snapshot.models)
                configured = model in self.config.collectors.get("models", {})
                if not known and not configured:
                    raise IntentWriteError(404, "unknown_model")
                pin = Pin(model, self._until(payload["until"], self.clock()), owner)
                validate_pin(pin)
            else:
                self._keys(payload, {"model"}, required={"model"})
                model = nonempty(payload["model"], "model")
            try:
                if operation == "pin":
                    result = self.store.put_pin(pin)
                else:
                    self.store.remove_pin(model)
                    result = {"model": model, "by": owner}
            except (sqlite3.Error, OSError) as exc:
                raise IntentWriteError(503, "intent_store_unavailable") from exc
            # Persist, publish and wake waiters under the same accounting lock.
            self.emit(operation, model=model, detail={**result, "dry_run": False})
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

    def usage(self, *, days: int = 7, by: str = "container") -> dict:
        """Read usage outside action_lock; unknown origins remain unknown."""
        if type(days) is not int or days < 1 or days > (2**63 - 1) // 86400:
            raise ValueError("days must be a positive supported integer")
        if by not in ("container", "ip", "model"):
            raise ValueError("unsupported usage grouping")
        unknown = {"days": days, "by": by, "known": False, "error": "usage_not_configured",
                   "rows": [], "totals": {"requests": None, "input_tokens": None, "output_tokens": None}}
        if self._usage is None:
            return unknown
        try:
            result = self._usage(days=days, by=by)
            if not isinstance(result, dict) or type(result.get("known")) is not bool:
                raise TypeError("invalid usage result")
            json.dumps(result, allow_nan=False)
            return result
        except Exception as exc:
            LOG.warning(json.dumps({"kind": "usage_error", "error_type": type(exc).__name__}))
            return {**unknown, "error": "usage_unavailable"}

    def quiet_callbacks(self, quiet):
        """Adapters for a future continuous stream; no stream is mounted here.

        Observations use QuietPeriod's own brief mutex before notification.
        Skipping a busy notification must never delay the next observation.
        """
        def notify_if_unlocked():
            if self.action_lock.acquire(blocking=False):
                try:
                    self.changed.notify_all()
                finally:
                    self.action_lock.release()

        def on_inflight(inflight, *, connected=True):
            quiet.observe(inflight, connected=connected)
            notify_if_unlocked()

        def on_heartbeat():
            quiet.heartbeat()
            notify_if_unlocked()

        return on_inflight, on_heartbeat

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
                lambda: self.events_closed.is_set() or any(e.id > cursor for e in self._events),
                timeout=timeout,
            )
            return tuple(copy.deepcopy(e) for e in self._events if e.id > cursor)

    def sample_once(self) -> StateSnapshot:
        with self.action_lock:
            self._sample_started += 1
            generation = self._sample_started
        try:
            snapshot = self.collect() if self.collect else self._unknown("collectors_not_configured")
            if not isinstance(snapshot, StateSnapshot):
                raise TypeError("collector must return StateSnapshot")
            snapshot = replace(
                snapshot, sampled_at=snapshot.sampled_at if snapshot.sampled_at is not None else time.time(),
                read_only=self.config.read_only,
                memory=replace(snapshot.memory, budget_gb=self.config.memory_budget_gb,
                               host_min_available_gb=self.config.host_min_available_gb),
            )
            json.dumps(snapshot.to_dict(), allow_nan=False)
        except Exception as exc:
            # Never serve a stale healthy snapshot as current after probe failure.
            snapshot = self._unknown("collection_failed")
            self.emit("collection_error", detail={"error_type": type(exc).__name__})
        with self.changed:
            if generation < self._sample_published:
                return self.snapshot()
            self._sample_published = generation
            self._snapshot = snapshot
            self.emit("state", detail={"sampled_at": snapshot.sampled_at,
                                       "errors": list(snapshot.errors)})
            self.changed.notify_all()
        # Action-triggered rounds need the same exit reconciliation as periodic
        # rounds, so a proven stop can unblock measured free and waiting place.
        if self.placement is not None:
            try:
                self.placement.reconcile()
            except Exception as exc:
                self.emit("lease_reconcile_error", detail={"error_type": type(exc).__name__})
        return self.snapshot()

    def request_sample(self):
        """Wake the existing sampler; callers never wait for collector I/O here."""
        self.sample_requested.set()

    def _run(self):
        while not self.stopping.is_set():
            self.sample_requested.clear()
            started = time.monotonic()
            self.sample_once()
            delay = max(0.0, self.config.sample_interval_seconds - (time.monotonic() - started))
            self.sample_requested.wait(delay)

    def start(self):
        with self.action_lock:
            if self._thread is not None:
                raise RuntimeError("scheduler already started")
            if self.stopping.is_set():
                raise RuntimeError("scheduler already stopped")
            self._thread = threading.Thread(target=self._run, name="llmsvc-sampler", daemon=True)
        try:
            if self.event_bridge is not None:
                self.event_bridge.start()
            self._thread.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.stopping.set()
        self.sample_requested.set()
        try:
            if self.event_bridge is not None:
                self.event_bridge.close()
        finally:
            try:
                if self._thread is not None and self._thread.is_alive():
                    self._thread.join(timeout=self.config.request_timeout_seconds)
                with self.action_lock:
                    if self._collector_closed:
                        return
                    self._collector_closed = True
                close = getattr(self.collect, "close", None)
                if close is not None:
                    close()
            finally:
                # SSE may send its final buffered relay events before closing.
                self.events_closed.set()
                with self.changed:
                    self.changed.notify_all()
