# Generated-By: Codex / gpt-5.6-luna
"""Bounded pinned-model wake log parsing and scheduler integration."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

import pytest

from llmsvc.wake_progress import WakeProgressReader, parse_log_line


def test_parse_log_line_accepts_only_pinned_prefixes_and_drops_untrusted_text():
    assert parse_log_line(b"2026/09/12 12:00:00 Wake up sent, waiting for healthy state") == "health_wait"
    assert parse_log_line(b"\x1b[31mStarted daemon with PID 42\x1b[0m") is None
    assert parse_log_line(b"vLLM daemon not reachable (connection refused), attempting to wake up") == "wake_attempted"
    assert parse_log_line(b"loading weights 50% http://private.invalid") is None
    assert parse_log_line(b"\xffStarted daemon with PID 42") is None
    assert parse_log_line(b"x" * (16 * 1024 + 1)) is None


class ChunkedResponse:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=None))

    def read(self, _size):
        return next(self.chunks, b"")

    read1 = read

    def close(self):
        self.closed = True


def test_reader_reassembles_fragments_bounds_bytes_and_emits_fixed_detail():
    response = ChunkedResponse([
        b"Started daemon with PID ", b"44, waiting for healthy state\nloading 30%\n",
        b"Waiting for vLLM to be healthy after wake up\n",
    ])
    requests, details = [], []

    def opener(request, timeout):
        requests.append((request.full_url, timeout))
        return response

    reader = WakeProgressReader(opener=opener, base_url="http://127.0.0.1:8000",
                                model="org/model name", configured_models={"org/model name"},
                                deadline=time.monotonic() + 2,
                                emit=details.append)
    reader.start()
    deadline = time.monotonic() + 1
    while len(details) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert reader.close(timeout=2)
    assert requests[0][0].endswith("/logs/stream/org%2Fmodel%20name?no-history=1")
    assert [item["stage"] for item in details] == ["process_started", "health_wait", "unavailable"]
    assert all(item["source"] == "llama-swap" and item["source_model"] == "org/model name"
               and item["progress_source"] == "per_model_log"
               and item["trusted_for_quiet"] is False
               and set(item) == {"stage", "source", "source_model", "progress_source", "log_epoch",
                                 "sequence", "received_at", "trusted_for_quiet"}
               for item in details)
    assert response.closed


def test_reader_delayed_headers_can_be_cancelled_without_wake_error():
    entered = threading.Event()
    release = threading.Event()

    def opener(_request, timeout):
        entered.set()
        release.wait(5)
        raise OSError("cancelled")

    details = []
    reader = WakeProgressReader(opener=opener, base_url="http://127.0.0.1:8000",
                                model="model", configured_models={"model"}, deadline=time.monotonic() + 5,
                                emit=details.append)
    reader.start()
    assert entered.wait(1)
    # The reader is advisory; a blocked header must not hold the action path.
    release.set()
    assert reader.close(timeout=2)
    assert details == [] or details[-1]["stage"] == "unavailable"


def test_reader_oversized_stream_becomes_unavailable_without_raw_payload():
    response = ChunkedResponse([b"x" * (256 * 1024 + 1)])
    details = []
    reader = WakeProgressReader(opener=lambda *_args, **_kwargs: response,
                                base_url="http://127.0.0.1:8000", model="model", configured_models={"model"},
                                deadline=time.monotonic() + 2, emit=details.append)
    reader.start()
    assert reader.close(timeout=2)
    assert [item["stage"] for item in details] == ["unavailable"]


def test_reader_rejects_reserved_or_unconfigured_monitor_ids():
    kwargs = {"opener": lambda *_args, **_kwargs: None, "base_url": "http://127.0.0.1:8000",
              "configured_models": {"model"}, "deadline": time.monotonic() + 1,
              "emit": lambda _detail: None}
    for name in ("proxy", "upstream", "other"):
        with pytest.raises(ValueError, match="canonical model"):
            WakeProgressReader(model=name, **kwargs)
    with pytest.raises(ValueError, match="origin"):
        WakeProgressReader(model="model", **{**kwargs, "base_url": "http://127.0.0.1:8000/api"})


def test_real_chunked_stream_delivers_short_line_before_eof_and_closes_promptly():
    ready, second, release = threading.Event(), threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.path == "/logs/stream/model?no-history=1"
            assert self.headers.get("Accept") == "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            body = b"Started daemon with PID 42, waiting for healthy state\n"
            self.wfile.write(("%x\r\n" % len(body)).encode() + body + b"\r\n")
            self.wfile.flush()
            ready.set()
            time.sleep(1.3)
            body = b"Waiting for vLLM to be healthy after wake up\n"
            self.wfile.write(("%x\r\n" % len(body)).encode() + body + b"\r\n")
            self.wfile.flush()
            second.set()
            release.wait(5)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = build_opener(ProxyHandler({}), HTTPRedirectHandler())
        details = []
        reader = WakeProgressReader(opener=opener.open,
                                    base_url="http://127.0.0.1:%s" % server.server_port,
                                    model="model", configured_models={"model"},
                                    deadline=time.monotonic() + 5, emit=details.append)
        reader.start()
        assert ready.wait(2)
        deadline = time.monotonic() + 4
        while len(details) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert second.is_set()
        assert [item["stage"] for item in details[:2]] == ["process_started", "health_wait"]
        started = time.monotonic()
        release.set()
        assert reader.close(timeout=2)
        assert time.monotonic() - started < 2
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_stalled_headers_are_bounded_and_do_not_block_wake_reader_cleanup():
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            entered.set()
            release.wait(5)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = build_opener(ProxyHandler({}), HTTPRedirectHandler())
        details = []
        reader = WakeProgressReader(opener=opener.open,
                                    base_url="http://127.0.0.1:%s" % server.server_port,
                                    model="model", configured_models={"model"},
                                    deadline=time.monotonic() + 2, emit=details.append)
        reader.start()
        assert entered.wait(2)
        started = time.monotonic()
        assert reader.close(timeout=2)
        assert time.monotonic() - started < 2
        assert not details or details[-1]["stage"] == "unavailable"
        release.set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
