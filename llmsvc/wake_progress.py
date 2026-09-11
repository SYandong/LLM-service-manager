# Generated-By: Codex / gpt-5.6-luna
"""Bounded, untrusted per-model wake progress from llama-swap logs.

The stream is advisory only.  It never establishes readiness, quiet, source
continuity, or resource settlement.  The reader is deliberately independent
of the scheduler action lock so a delayed log response cannot delay wake.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.parse import quote


MAX_LINE_BYTES = 16 * 1024
MAX_STREAM_BYTES = 256 * 1024
_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_GO_TIMESTAMP = re.compile(rb"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{6,9})?\s+")

# These are byte prefixes from the pinned vllm-wrapper source.  Values after a
# prefix (URL, PID and error text) are intentionally discarded.
_PREFIXES = (
    (b"Starting vllm-wrapper serve on ", "wrapper_started"),
    (b"vLLM daemon not reachable (", "wake_attempted"),
    (b"Wake up failed: ", "start_attempted"),
    (b"Started daemon with PID ", "process_started"),
    (b"Wake up sent, waiting for healthy state", "health_wait"),
    (b"Waiting for vLLM to be healthy after wake up", "health_wait"),
    (b"Failed to start daemon: ", "start_failed"),
    (b"vLLM health check failed after wake up: ", "health_failed"),
)


def parse_log_line(raw):
    """Return one fixed safe stage for a source-verified line, or ``None``."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_LINE_BYTES:
        return None
    value = bytes(raw).strip()
    # The pinned wrapper writes plain text.  Do not normalize terminal control
    # sequences from an untrusted daemon into an apparently trusted prefix.
    if _ANSI.search(value) or any(byte < 0x20 for byte in value):
        return None
    value = _GO_TIMESTAMP.sub(b"", value, count=1)
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for prefix, stage in _PREFIXES:
        if value.startswith(prefix):
            return stage
    return None


class WakeProgressReader:
    """Read one bounded model log stream until wake completion or deadline."""

    def __init__(self, *, opener, base_url, model, deadline, emit,
                 monotonic=time.monotonic, wall_clock=time.time):
        if not callable(opener) or not isinstance(base_url, str) or not base_url:
            raise ValueError("wake progress reader requires an HTTP opener and origin")
        if not isinstance(model, str) or not model or model in (".", ".."):
            raise ValueError("wake progress reader requires a canonical model")
        if not callable(emit) or not callable(monotonic) or not callable(wall_clock):
            raise ValueError("wake progress reader callbacks are required")
        self.opener = opener
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.deadline = deadline
        self.emit = emit
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.epoch = "%x" % id(self)
        self.sequence = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._response = None
        self.thread = None
        self._unavailable_sent = False
        self._saw_stage = False

    @property
    def url(self):
        return (self.base_url + "/logs/stream/" + quote(self.model, safe="")
                + "?no-history=1")

    def start(self):
        if self.thread is not None:
            raise RuntimeError("wake progress reader already started")
        self.thread = threading.Thread(target=self._run, name="llm-wake-progress", daemon=True)
        self.thread.start()

    def _detail(self, stage):
        self.sequence += 1
        return {
            "stage": stage,
            "source": "llama-swap",
            "source_model": self.model,
            "progress_source": "per_model_log",
            "log_epoch": self.epoch,
            "sequence": self.sequence,
            "received_at": self.wall_clock(),
            "trusted_for_quiet": False,
        }

    def _emit(self, stage):
        if self._stop.is_set():
            return
        if stage != "unavailable":
            self._saw_stage = True
        try:
            self.emit(self._detail(stage))
        except Exception:
            # A presentation callback must never keep the wake request alive.
            self._stop.set()

    def _unavailable(self):
        if not self._saw_stage and not self._unavailable_sent and not self._stop.is_set():
            self._unavailable_sent = True
            self._emit("unavailable")

    def _interrupt(self):
        with self._lock:
            response = self._response
        if response is None:
            return
        try:
            fp = getattr(response, "fp", None)
            raw = getattr(fp, "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
        except OSError:
            pass
        try:
            response.close()
        except OSError:
            pass

    def close(self, timeout=2.0):
        self._stop.set()
        self._interrupt()
        if self.thread is None:
            return True
        self.thread.join(timeout=max(0.0, float(timeout)))
        return not self.thread.is_alive()

    def _run(self):
        response = None
        total = 0
        pending = bytearray()
        try:
            remaining = self.deadline - self.monotonic()
            if remaining <= 0:
                self._unavailable()
                return
            request = Request(self.url, headers={"Accept": "text/plain"})
            # Header acquisition is bounded and asynchronous; wake itself does
            # not wait for it.  A short read timeout also makes close prompt.
            response = self.opener(request, timeout=min(1.0, remaining))
            with self._lock:
                self._response = response
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.settimeout(min(0.5, max(0.05, remaining)))
            while not self._stop.is_set() and self.monotonic() < self.deadline:
                try:
                    chunk = response.read(4096)
                except socket.timeout:
                    # An idle stream is still within this wake window; do not
                    # turn an ordinary read timeout into a terminal failure.
                    continue
                if not chunk:
                    self._unavailable()
                    return
                total += len(chunk)
                if total > MAX_STREAM_BYTES:
                    self._unavailable()
                    return
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    if len(line) > MAX_LINE_BYTES:
                        self._unavailable()
                        return
                    stage = parse_log_line(line.rstrip(b"\r"))
                    if stage is not None:
                        self._emit(stage)
            if not self._stop.is_set():
                self._unavailable()
        except (HTTPError, URLError, OSError, ValueError, UnicodeError, TimeoutError, TypeError):
            self._unavailable()
        finally:
            with self._lock:
                self._response = None
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
