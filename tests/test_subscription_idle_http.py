# Generated-By: Codex / gpt-6-astra
"""Real HTTP regressions for quiet, partial and failed SSE transports (#185)."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmsvc.collectors.relay import DataPlaneEventRelay


def frame(state):
    envelope = {"type": "modelStatus", "data": [{"id": "m", "state": state}]}
    return b"event:message\ndata:" + json.dumps(envelope).encode() + b"\n\n"


@contextmanager
def source(write):
    release = threading.Event()
    opened = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            opened.append(self.path)
            try:
                write(self, release, len(opened))
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected if the subscription closes its owned socket.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), release, opened
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def headers(handler, chunked=False):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    if chunked:
        handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    handler.wfile.flush()


def collect_until(relay, events, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        events.extend(relay.drain()["events"])
        if predicate(events):
            return
        time.sleep(.005)
    pytest.fail("expected transport event did not arrive")


def states(events):
    return [e["detail"]["state"] for e in events if e["kind"] == "data_plane_state"]


@pytest.mark.parametrize("chunked", [False, True])
def test_idle_past_connect_timeout_keeps_one_stream_and_delivers_later_event(chunked):
    send_later = threading.Event()

    def write(handler, release, number):
        def emit(state):
            data = frame(state)
            if chunked:
                data = ("%x\r\n" % len(data)).encode() + data + b"\r\n"
            handler.wfile.write(data)
            handler.wfile.flush()

        headers(handler, chunked=chunked)
        emit("stopped")
        if send_later.wait(3):
            emit("ready")
        release.wait(3)

    with source(write) as (url, release, opened):
        relay = DataPlaneEventRelay(url, ["m"], timeout=.2, reconnect_delay=.02)
        events = []
        relay.start()
        try:
            collect_until(relay, events, lambda items: states(items) == ["stopped"])
            # The protocol permits a connected source to have nothing to emit.
            assert not send_later.wait(.7)
            events.extend(relay.drain()["events"])
            assert not [e for e in events if e["kind"] == "data_plane_error"]
            assert opened == ["/api/events"]
            send_later.set()
            collect_until(relay, events, lambda items: states(items) == ["stopped", "ready"])
            assert all(e["detail"]["trusted_for_quiet"] is False for e in events)
        finally:
            send_later.set()
            relay.close()
        assert not relay.subscription._thread.is_alive()


@pytest.mark.parametrize("partial", [b"", b"event:message\ndata:{", b"event:message\n"])
def test_close_interrupts_idle_or_incomplete_frame_without_reconnect(partial):
    sent = threading.Event()

    def write(handler, release, number):
        headers(handler)
        handler.wfile.write(partial)
        handler.wfile.flush()
        sent.set()
        release.wait(3)

    with source(write) as (url, release, opened):
        relay = DataPlaneEventRelay(url, ["m"], timeout=.2, reconnect_delay=.02)
        events = []
        relay.start()
        try:
            assert sent.wait(2)
            collect_until(relay, events, lambda items: any(
                e["detail"].get("status") == "connected" for e in items))
            assert not release.wait(.7)
            events.extend(relay.drain()["events"])
            assert opened == ["/api/events"]
            assert not [e for e in events if e["kind"] == "data_plane_error"]
            started = time.monotonic()
            relay.close()
            assert time.monotonic() - started < .5
            assert not relay.subscription._thread.is_alive()
        finally:
            relay.close()


def test_response_header_timeout_remains_a_real_error():
    def write(handler, release, number):
        release.wait(3)

    with source(write) as (url, release, opened):
        relay = DataPlaneEventRelay(url, ["m"], timeout=.2, reconnect_delay=999)
        events = []
        relay.start()
        try:
            collect_until(relay, events, lambda items: any(
                e["kind"] == "data_plane_error" and e["detail"]["reason"] == "timeout"
                for e in items))
            assert opened == ["/api/events"]
        finally:
            relay.close()
        assert not relay.subscription._thread.is_alive()


def test_real_eof_still_reconnects_and_reports_transport_loss():
    def write(handler, release, number):
        headers(handler)
        handler.wfile.write(frame("stopped" if number == 1 else "ready"))
        handler.wfile.flush()
        if number > 1:
            release.wait(3)

    with source(write) as (url, release, opened):
        relay = DataPlaneEventRelay(url, ["m"], timeout=.2, reconnect_delay=.02)
        events = []
        relay.start()
        try:
            collect_until(relay, events, lambda items: states(items) == ["stopped", "ready"])
            assert len(opened) == 2
            assert any(e["kind"] == "data_plane_error" and e["detail"]["reason"] == "disconnected"
                       for e in events)
        finally:
            relay.close()


@pytest.mark.parametrize("payload", [b"x" * 257, (b"data:" + b"x" * 80 + b"\n") * 4])
def test_real_stream_line_and_frame_limits_remain_bounded(payload):
    def write(handler, release, number):
        headers(handler)
        handler.wfile.write(payload)
        handler.wfile.flush()
        release.wait(3)

    with source(write) as (url, release, opened):
        relay = DataPlaneEventRelay(url, ["m"], timeout=.2, reconnect_delay=999,
                                   max_frame_bytes=256)
        events = []
        relay.start()
        try:
            collect_until(relay, events, lambda items: any(
                e["kind"] == "data_plane_error" and e["detail"]["reason"] == "invalid_event"
                for e in items))
            assert opened == ["/api/events"]
        finally:
            relay.close()
