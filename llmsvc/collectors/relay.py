# Generated-By: Codex / gpt-6-astra
"""Bounded, sanitized data-plane summaries for UI consumption only."""

import json
import math
import threading
import time
from collections import deque


class DataPlaneEventBuffer:
    """One subscription producer; consumers drain without calling external code.

    No publisher callback runs here. Queue operations use only a brief internal
    mutex, never wait for queue space, and never acquire the scheduler lock.
    """

    def __init__(self, model_ids, *, capacity=256, clock=time.time):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError('relay capacity must be between 1 and 4096')
        if not isinstance(model_ids, (list, tuple, set, frozenset)) or len(model_ids) > 1024:
            raise ValueError('model_ids must be a bounded collection')
        if any(not isinstance(name, str) or not 1 <= len(name) <= 256
               or any(ord(c) < 32 or ord(c) == 127 for c in name) for name in model_ids):
            raise ValueError('invalid configured model ID')
        if not callable(clock):
            raise ValueError('clock must be callable')
        self.model_ids = frozenset(model_ids)
        self.capacity = capacity
        self.clock = clock
        self._queue = deque()
        self._lock = threading.Lock()
        self._dropped = {}
        self._states = {}
        self._last_count = object()

    def _now(self):
        value = self.clock()
        return value if type(value) in (int, float) and math.isfinite(value) else None

    def _drop_locked(self, reason, count=1):
        self._dropped[reason] = self._dropped.get(reason, 0) + count

    def _append_locked(self, kind, model, detail, received_at):
        if len(self._queue) >= self.capacity:
            self._drop_locked('buffer_full')
            return False
        self._queue.append({'kind': kind, 'model': model, 'detail': {
            'source': 'llama-swap', 'trusted_for_quiet': False,
            'received_at': received_at, **detail,
        }})
        return True

    def observe(self, envelope):
        """Accept only modelStatus; logs and arbitrary other payloads are ignored."""
        if not isinstance(envelope, dict) or envelope.get('type') != 'modelStatus':
            return
        received_at = self._now()
        try:
            payload = envelope.get('data')
            if isinstance(payload, str):
                if len(payload) > 1024 * 1024:
                    raise ValueError('model snapshot exceeds limit')
                payload = json.loads(payload)
            if not isinstance(payload, list) or len(payload) > 1024:
                raise ValueError('invalid model status snapshot')
            states, filtered = {}, 0
            for item in payload:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    raise ValueError('invalid model state')
                model = item['id']
                if model not in self.model_ids:
                    filtered += 1
                    continue
                state = item.get('state')
                if state not in ('starting', 'ready', 'stopping', 'stopped') or model in states:
                    raise ValueError('invalid or duplicate model state')
                states[model] = state
        except (TypeError, ValueError):
            self.error('invalid_event')
            return
        with self._lock:
            if filtered:
                self._drop_locked('unlisted_model', filtered)
            for model, state in states.items():
                if self._states.get(model) != state:
                    if self._append_locked('data_plane_state', model, {'state': state}, received_at):
                        self._states[model] = state

    def inflight(self, count, operation):
        if ((count is not None and (type(count) is not int or not 0 <= count <= 2**63 - 1))
                or operation not in ('snapshot', 'upsert', 'remove', 'unknown')):
            self.error('invalid_event')
            return
        received_at = self._now()
        with self._lock:
            if count != self._last_count:
                if self._append_locked('data_plane_inflight', None,
                                       {'count': count, 'operation': operation}, received_at):
                    self._last_count = count

    def connection(self, status):
        if status not in ('connecting', 'connected', 'disconnected', 'closed'):
            raise ValueError('invalid connection status')
        received_at = self._now()
        with self._lock:
            if status == 'connected':
                self._states.clear()
                self._last_count = object()
            self._append_locked('data_plane_connection', None, {'status': status}, received_at)

    def error(self, reason):
        if reason not in ('timeout', 'disconnected', 'invalid_event', 'limit_exceeded', 'read_failed'):
            reason = 'read_failed'
        received_at = self._now()
        with self._lock:
            if reason in ('invalid_event', 'limit_exceeded'):
                self._drop_locked(reason)
            self._append_locked('data_plane_error', None, {'reason': reason}, received_at)

    def drain(self, max_events=128):
        """Return detached FIFO items and out-of-band local discard counts.

        Discards are reported even when max_events is smaller than the queue;
        sustained overflow cannot starve the diagnostic. v252 upstream loss is
        unknown, and is never included in these measured local counters.
        """
        if type(max_events) is not int or max_events < 1:
            raise ValueError('max_events must be a positive integer')
        with self._lock:
            events = [self._queue.popleft() for _ in range(min(max_events, len(self._queue)))]
            dropped = self._dropped
            self._dropped = {}
        return {'events': events, 'dropped': sum(dropped.values()),
                'dropped_by_reason': dropped, 'upstream_loss_unknown': True}


class DataPlaneEventRelay:
    """Optional UI-only subscription; core owns start, bounded drain, and close."""

    def __init__(self, swap_url, model_ids, *, capacity=256, timeout=10,
                 reconnect_delay=1, max_frame_bytes=1024 * 1024,
                 max_requests=100000, stream_factory=None):
        from .subscription import InflightSubscription
        self.buffer = DataPlaneEventBuffer(model_ids, capacity=capacity)
        self.subscription = InflightSubscription(
            swap_url, lambda count, *, connected: None,
            ordered_source=False, event_buffer=self.buffer,
            timeout=timeout, reconnect_delay=reconnect_delay,
            max_frame_bytes=max_frame_bytes, max_requests=max_requests,
            stream_factory=stream_factory,
        )

    def start(self):
        self.subscription.start()

    def close(self):
        self.subscription.close()

    def drain(self, max_events=128):
        return self.buffer.drain(max_events)
