# Generated-By: Codex / gpt-6-astra
"""Read-only sampling and an event stream sharing the future action lock."""

import copy
import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from typing import Callable, Optional

from llmsvc.config import SchedulerConfig
from llmsvc.state import Event, MemoryState, StateSnapshot

LOG = logging.getLogger("llmsvc.scheduler")


class Scheduler:
    def __init__(self, config: SchedulerConfig,
                 collect: Optional[Callable[[], StateSnapshot]] = None):
        self.config = config
        self.collect = collect
        # One lock for action/accounting and publication. Slow read-only probes
        # run outside it; Condition.wait releases it for other handlers.
        self.action_lock = threading.RLock()
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
            return self._snapshot

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
        return snapshot

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
