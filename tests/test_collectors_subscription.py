# Generated-By: Codex / gpt-6-astra
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmsvc.collectors.subscription import InflightSubscription, subscribe_inflight


class Stream:
    def __init__(self, frames):
        self.lines = queue.Queue()
        self.closed = False
        for frame in frames:
            if isinstance(frame, BaseException):
                self.lines.put(frame)
            else:
                for line in frame:
                    self.lines.put(line)

    def readline(self, limit):
        item = self.lines.get(timeout=1)
        if isinstance(item, BaseException):
            raise item
        if self.closed and item == b"":
            return b""
        return item

    def close(self):
        self.closed = True
        self.lines.put(b"")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BlockingStream:
    def __init__(self):
        self.closed = threading.Event()

    def readline(self, limit):
        if self.closed.wait(timeout=2):
            return b""
        raise TimeoutError("blocked")

    def close(self):
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def message(kind, data):
    envelope = {"type": kind, "data": json.dumps(data)}
    return [b"event:message\n", b"data:" + json.dumps(envelope).encode() + b"\n", b"\n"]


def comment():
    return [b": heartbeat\n"]


def run(frames, **kwargs):
    calls = []
    heartbeats = []
    kwargs.setdefault("ordered_source", True)
    sub = InflightSubscription(
        "http://llama-swap",
        lambda value, *, connected=True: calls.append((value, connected)),
        on_heartbeat=lambda: heartbeats.append("beat"),
        stream_factory=lambda: Stream(frames),
        reconnect_delay=999,
        timeout=.1,
        **kwargs,
    )
    sub._emit_unknown()
    try:
        with Stream(frames) as stream:
            sub._consume(stream)
    except Exception:
        sub._emit_unknown()
    return calls, heartbeats


def test_snapshot_upsert_remove_observes_ordered_aggregate_counts():
    calls, heartbeats = run([
        message("logData", {"source": "proxy", "data": "raw logs are ignored"}),
        message("profileChanged", {"active": None}),
        message("inflight", {"operation": "snapshot"}),
        message("inflight", {"operation": "upsert", "request": {"id": "a", "modelID": "m1"}}),
        message("inflight", {"operation": "upsert", "request": {"id": "b", "modelID": "m2"}}),
        message("inflight", {"operation": "remove", "id": "a"}),
        message("inflight", {"operation": "remove", "id": "b"}),
        EOFError(),
    ])

    assert calls == [
        (None, False),
        (0, True),
        (1, True),
        (2, True),
        (1, True),
        (0, True),
        (None, False),
    ]
    assert heartbeats == []


@pytest.mark.parametrize("kwargs", [
    {"timeout": True},
    {"timeout": float("inf")},
    {"reconnect_delay": False},
    {"reconnect_delay": float("nan")},
    {"max_frame_bytes": True},
    {"max_requests": False},
    {"ordered_source": "false"},
    {"ordered_source": 1},
])
def test_constructor_rejects_ambiguous_or_non_finite_options(kwargs):
    with pytest.raises(ValueError):
        InflightSubscription("http://llama-swap", lambda value, *, connected=True: None, **kwargs)


def test_disconnect_invalidates_and_reconnect_uses_fresh_snapshot():
    streams = iter([
        Stream([
            message("inflight", {"operation": "snapshot", "requests": [{"id": "old", "modelID": "m"}]}),
            EOFError(),
        ]),
        Stream([
            message("inflight", {"operation": "snapshot"}),
            EOFError(),
        ]),
    ])
    calls = []
    sub = InflightSubscription(
        "http://llama-swap",
        lambda value, *, connected=True: calls.append((value, connected)),
        stream_factory=lambda: next(streams),
        reconnect_delay=0,
        timeout=.1,
        ordered_source=True,
    )
    thread = threading.Thread(target=sub._run)
    thread.start()
    deadline = time.monotonic() + 2
    while calls.count((None, False)) < 3 and time.monotonic() < deadline:
        time.sleep(.01)
    sub.close()
    thread.join(timeout=1)

    assert calls[:5] == [(None, False), (1, True), (None, False), (0, True), (None, False)]


@pytest.mark.parametrize("frame", [
    message("unknown", {"anything": True}),
    message("inflight", {"operation": "upsert", "request": {"id": "a", "modelID": "m"}}),
    message("inflight", {"operation": "snapshot", "requests": [{"id": "", "modelID": "m"}]}),
    [b"event:not-message\n", b"data:{}\n", b"\n"],
])
def test_unknown_malformed_or_out_of_order_events_reset_to_unknown(frame):
    calls, _ = run([frame, EOFError()])
    assert calls == [(None, False), (None, False)]


def test_replacement_snapshot_and_unknown_remove_reset_to_unknown():
    calls, _ = run([
        message("inflight", {"operation": "snapshot"}),
        message("inflight", {"operation": "snapshot", "requests": [{"id": "fresh", "modelID": "m"}]}),
        message("inflight", {"operation": "remove", "id": "missing"}),
        EOFError(),
    ])

    assert calls == [(None, False), (0, True), (None, False), (1, True), (None, False)]


def test_incremental_after_reconnect_is_discarded_until_snapshot():
    calls, _ = run([
        message("inflight", {"operation": "upsert", "request": {"id": "a", "modelID": "m"}}),
        EOFError(),
    ])

    assert calls == [(None, False), (None, False)]


def test_heartbeat_only_on_intact_subscription_after_snapshot():
    calls, heartbeats = run([
        comment(),
        [b"event:message\n", b": inside frame\n", b"data:" + json.dumps({"type": "logData", "data": "{}"}).encode() + b"\n", b"\n"],
        message("inflight", {"operation": "snapshot"}),
        comment(),
        EOFError(),
    ])

    assert calls == [(None, False), (0, True), (None, False)]
    assert heartbeats == ["beat"]


def test_untrusted_source_does_not_emit_heartbeat():
    calls, heartbeats = run([
        message("inflight", {"operation": "snapshot"}),
        comment(),
        EOFError(),
    ], ordered_source=False)

    assert calls == [(None, False), (0, False), (None, False)]
    assert heartbeats == []


def test_no_local_elapsed_timer_heartbeat_is_invented():
    calls, heartbeats = run([
        message("inflight", {"operation": "snapshot"}),
        EOFError(),
    ])

    assert calls == [(None, False), (0, True), (None, False)]
    assert heartbeats == []


def test_untrusted_v252_source_reports_counts_without_certifying_quiet():
    calls, _ = run([
        message("inflight", {"operation": "snapshot"}),
        message("inflight", {"operation": "upsert", "request": {"id": "a", "modelID": "m"}}),
        EOFError(),
    ], ordered_source=False)

    assert calls == [(None, False), (0, False), (1, False), (None, False)]


def test_callback_sequence_is_serialized_for_slow_observer():
    entered = []
    released = threading.Event()

    def on_inflight(value, *, connected=True):
        entered.append((value, connected))
        if value == 0 and connected:
            released.wait(timeout=1)

    sub = InflightSubscription(
        "http://llama-swap",
        on_inflight,
        stream_factory=lambda: Stream([
            message("inflight", {"operation": "snapshot"}),
            message("inflight", {"operation": "upsert", "request": {"id": "a", "modelID": "m"}}),
        ]),
        reconnect_delay=999,
        timeout=.1,
        ordered_source=True,
    )
    thread = threading.Thread(target=sub._run)
    thread.start()
    deadline = time.monotonic() + 2
    while (0, True) not in entered and time.monotonic() < deadline:
        time.sleep(.01)
    time.sleep(.05)
    assert entered == [(None, False), (0, True)]
    released.set()
    deadline = time.monotonic() + 2
    while (1, True) not in entered and time.monotonic() < deadline:
        time.sleep(.01)
    sub.close()
    thread.join(timeout=1)
    assert entered[:3] == [(None, False), (0, True), (1, True)]


def test_bounded_subscription_resets_and_shutdown_cleans_thread():
    calls = []
    sub = subscribe_inflight(
        "http://llama-swap",
        lambda value, *, connected=True: calls.append((value, connected)),
        stream_factory=lambda: Stream([
            message("inflight", {"operation": "snapshot"}),
            message("inflight", {"operation": "upsert", "request": {"id": "a", "modelID": "m"}}),
        ]),
        reconnect_delay=999,
        timeout=.1,
        max_requests=1,
        ordered_source=True,
    )
    deadline = time.monotonic() + 2
    while calls.count((None, False)) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    sub.close()

    assert calls == [(None, False), (0, True), (1, True), (None, False)]
    assert sub._thread is not None
    assert not sub._thread.is_alive()


def test_close_unblocks_reader_and_invalidates_observer():
    calls = []
    stream = BlockingStream()
    sub = subscribe_inflight(
        "http://llama-swap",
        lambda value, *, connected=True: calls.append((value, connected)),
        stream_factory=lambda: stream,
        reconnect_delay=999,
        timeout=.1,
    )
    deadline = time.monotonic() + 2
    while calls != [(None, False)] and time.monotonic() < deadline:
        time.sleep(.01)

    sub.close()

    assert calls == [(None, False), (None, False)]
    assert sub._thread is not None
    assert not sub._thread.is_alive()


def test_http_close_unblocks_buffered_reader_and_invalidates_observer():
    started = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/events"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            started.set()
            release.wait(timeout=5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []
    try:
        sub = subscribe_inflight(
            "http://127.0.0.1:" + str(server.server_port),
            lambda value, *, connected=True: calls.append((value, connected)),
            reconnect_delay=999,
            timeout=.2,
        )
        deadline = time.monotonic() + 2
        while (not started.is_set() or calls != [(None, False)]) and time.monotonic() < deadline:
            time.sleep(.01)

        sub.close()

        assert calls == [(None, False), (None, False)]
        assert sub._thread is not None
        assert not sub._thread.is_alive()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_http_open_failure_closes_connection(monkeypatch):
    from llmsvc.collectors import subscription
    closed = []

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            raise TimeoutError('headers never arrived')

        def close(self):
            closed.append(True)

    monkeypatch.setattr(subscription, 'HTTPConnection', Connection)
    with pytest.raises(TimeoutError):
        subscription._HTTPEventStream('http://localhost', .1)
    assert closed == [True]


def test_subscription_rejects_upstream_model_routing_before_any_request():
    from llmsvc.collectors.subscription import _HTTPEventStream
    with pytest.raises(ValueError, match='upstream'):
        _HTTPEventStream('http://localhost/upstream/model', .1)
