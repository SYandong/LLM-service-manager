# Generated-By: Codex / gpt-6-astra
"""Continuous llama-swap inflight SSE subscription for quiet-period gates."""

import json
import math
import socket
import threading
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit

from .events import EventSnapshot


class _HTTPEventStream:
    def __init__(self, url, timeout):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("swap_url must be an HTTP(S) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("swap_url must not include credentials, query, or fragment")
        if "/upstream" in parts.path:
            raise ValueError("event probes must not use model upstream routing")
        path = (parts.path.rstrip("/") or "") + "/api/events"
        cls = HTTPSConnection if parts.scheme == "https" else HTTPConnection
        self.conn = cls(parts.hostname, parts.port, timeout=timeout)
        self.response = None
        try:
            self.conn.request("GET", path, headers={"Accept": "text/event-stream"})
            self.response = self.conn.getresponse()
            if self.response.status != 200:
                raise ConnectionError("event subscription rejected")
        except Exception:
            self.close()
            raise

    def readline(self, limit):
        return self.response.readline(limit)

    def close(self):
        sock = self._socket()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.response is not None:
            self.response.close()
        self.conn.close()

    def _socket(self):
        if getattr(self.conn, "sock", None) is not None:
            return self.conn.sock
        fp = getattr(self.response, "fp", None)
        for attr in ("raw", "_sock"):
            fp = getattr(fp, attr, None)
            if fp is None:
                return None
        return fp

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class InflightSubscription:
    """Observe aggregate inflight counts from an ordered `/api/events` stream."""

    def __init__(self, swap_url, on_inflight, *, on_heartbeat=None,
                 stream_factory=None, timeout=10, reconnect_delay=1,
                 max_frame_bytes=1024 * 1024, max_requests=100000,
                 ordered_source=False):
        if not callable(on_inflight):
            raise ValueError("on_inflight callback is required")
        if on_heartbeat is not None and not callable(on_heartbeat):
            raise ValueError("on_heartbeat must be callable")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if (isinstance(reconnect_delay, bool) or not isinstance(reconnect_delay, (int, float))
                or not math.isfinite(reconnect_delay) or reconnect_delay < 0):
            raise ValueError("reconnect_delay must be non-negative")
        if isinstance(max_frame_bytes, bool) or not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if type(ordered_source) is not bool:
            raise ValueError("ordered_source must be a bool")
        self.swap_url = swap_url.rstrip("/")
        self.on_inflight = on_inflight
        self.on_heartbeat = on_heartbeat
        self.stream_factory = stream_factory
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay
        self.max_frame_bytes = max_frame_bytes
        self.max_requests = max_requests
        self.ordered_source = ordered_source
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="inflight-subscription", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        stream = self._stream
        if stream is not None and hasattr(stream, "close"):
            stream.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.timeout)
            if thread.is_alive():
                raise RuntimeError("inflight subscription worker did not stop")

    def _run(self):
        self._emit_unknown()
        while not self._stop.is_set():
            try:
                with self._open() as stream:
                    self._stream = stream
                    self._consume(stream)
            except Exception:
                pass
            finally:
                self._stream = None
                self._emit_unknown()
            if self._stop.wait(self.reconnect_delay):
                break

    def _open(self):
        if self.stream_factory is not None:
            return self.stream_factory()
        return _HTTPEventStream(self.swap_url, self.timeout)

    def _consume(self, stream):
        state = EventSnapshot()
        frame = []
        frame_bytes = 0
        while not self._stop.is_set():
            raw = stream.readline(self.max_frame_bytes + 1)
            if not raw:
                raise ConnectionError("SSE stream ended")
            if len(raw) > self.max_frame_bytes:
                raise ValueError("SSE frame line exceeds limit")
            line = raw.rstrip(b"\r\n")
            if line == b"":
                if frame:
                    self._dispatch(frame, state)
                    frame = []
                    frame_bytes = 0
                continue
            if line.startswith(b":"):
                if (self.ordered_source and not frame and state.have_requests
                        and self.on_heartbeat is not None):
                    self.on_heartbeat()
                continue
            frame_bytes += len(line)
            if frame_bytes > self.max_frame_bytes:
                raise ValueError("SSE frame exceeds limit")
            frame.append(line)

    def _dispatch(self, lines, state):
        event = "message"
        data = []
        for line in lines:
            if line.startswith(b"event:"):
                event = line[6:].strip().decode("utf-8")
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip())
        if event != "message" or not data:
            raise ValueError("unknown SSE event")
        envelope = json.loads(b"\n".join(data))
        if not isinstance(envelope, dict):
            raise ValueError("invalid SSE envelope")
        kind = envelope.get("type")
        if kind in ("logData", "activity", "uiConfig", "profile", "profileChanged", "modelStatus"):
            return
        if kind != "inflight":
            raise ValueError("unknown llama-swap event")
        payload = envelope.get("data")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("invalid inflight event")
        if payload.get("operation") == "snapshot" and state.have_requests:
            self._emit_unknown()
        if payload.get("operation") == "remove" and state.have_requests and payload.get("id") not in state.requests:
            raise ValueError("unknown inflight removal")
        state.feed(envelope)
        if not state.have_requests:
            raise ValueError("inflight state unknown")
        if len(state.requests) > self.max_requests:
            raise BufferError("inflight request bound exceeded")
        self.on_inflight(len(state.requests), connected=self.ordered_source)

    def _emit_unknown(self):
        self.on_inflight(None, connected=False)


def subscribe_inflight(swap_url, on_inflight, *, on_heartbeat=None, **kwargs):
    subscription = InflightSubscription(
        swap_url, on_inflight, on_heartbeat=on_heartbeat, **kwargs)
    subscription.start()
    return subscription
