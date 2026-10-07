# Generated-By: Codex / gpt-6.1-sol
"""Bounded observer notifications; the fleet database remains the history source."""

from collections import deque
import copy
import json
import threading
import time
import uuid

MAX_EVENT_ID = 2 ** 63 - 1
MAX_EVENT_BYTES = 16 * 1024


class FleetEvents:
    def __init__(self, capacity=1000, *, clock=time.time):
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError("invalid_observer_event_capacity")
        self.incarnation = uuid.uuid4().hex
        self.clock = clock
        self.closed = threading.Event()
        self.changed = threading.Condition()
        self._events = deque(maxlen=capacity)
        self._next_id = 1

    def emit(self, kind, *, model=None, detail=None):
        if kind not in ("fleet_status_changed", "fleet_snapshot_changed"):
            raise ValueError("invalid_observer_event_kind")
        with self.changed:
            if self.closed.is_set():
                return None
            if self._next_id > MAX_EVENT_ID:
                raise ValueError("observer_event_id_exhausted")
            event = {"id": self._next_id, "timestamp": self.clock(), "kind": kind,
                     "model": model, "detail": copy.deepcopy(detail or {}),
                     "observer_incarnation": self.incarnation}
            if len(json.dumps(event, allow_nan=False).encode()) > MAX_EVENT_BYTES:
                raise ValueError("observer_event_too_large")
            self._next_id += 1
            self._events.append(event)
            self.changed.notify_all()
            return copy.deepcopy(event)

    def reset_reason(self, cursor, incarnation):
        with self.changed:
            if incarnation is not None and incarnation != self.incarnation:
                return "incarnation_changed"
            if cursor and incarnation is None:
                return "incarnation_required"
            if cursor >= self._next_id:
                return "cursor_ahead"
            if cursor and self._events and cursor < self._events[0]["id"] - 1:
                return "cursor_expired"
            return None

    def reset_event(self, reason):
        with self.changed:
            return {"id": 0, "timestamp": self.clock(), "kind": "cursor_reset", "model": None,
                    "detail": {"reason": reason, "latest_id": self._next_id - 1,
                               "oldest_id": self._events[0]["id"] if self._events else None},
                    "observer_incarnation": self.incarnation}

    def events_since(self, cursor, timeout=0):
        with self.changed:
            self.changed.wait_for(lambda: self.closed.is_set() or (self._events and self._events[-1]["id"] > cursor),
                                  timeout=timeout)
            return tuple(copy.deepcopy(event) for event in self._events if event["id"] > cursor)

    def close(self):
        with self.changed:
            self.closed.set()
            self.changed.notify_all()
