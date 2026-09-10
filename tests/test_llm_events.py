# Generated-By: Codex / gpt-6-astra
"""Scheduler SSE decoding and real-loopback cursor/cleanup regressions."""

import io
import json
import runpy
import threading
import time
from email.message import Message
from pathlib import Path

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer, SchedulerHandler


@pytest.fixture
def api():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))


def frame(event_id, **values):
    item = {"id": event_id, "kind": "sleep", "timestamp": 1800000000,
            "model": "synthetic", "detail": {}}
    item.update(values)
    return ("id: %s\nevent: %s\ndata: %s\n\n" % (
        event_id, item["kind"], json.dumps(item))).encode()


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for SSE condition")


def collect_until(reader, count):
    seen = []

    def collect():
        seen.extend(reader.drain()["events"])
        return len(seen) >= count

    wait_for(collect)
    return seen


def test_multiline_utf8_comments_and_partial_eof(api):
    body = (b'\xef\xbb\xbf: connected\r\n\r\n'
            b'id: 3\r\nevent: sleep\r\ndata: {"id":3,"kind":"sleep",\r\n'
            + 'data: "timestamp":1800000000,"model":"模型"}\r\n\r\n'.encode()
            + b': heartbeat\n\n' + frame(4).rstrip(b'\n'))
    events = list(api["iter_events"](io.BytesIO(body)))
    assert [event["id"] for event in events] == [3]
    assert events[0]["model"] == "模型"


@pytest.mark.parametrize("newline", [b'\n', b'\r\n', b'\r'])
def test_sse_line_endings(api, newline):
    events = list(api["iter_events"](io.BytesIO(frame(1).replace(b'\n', newline))))
    assert [event["id"] for event in events] == [1]


@pytest.mark.parametrize("body", [
    b'id: nope\ndata: {}\n\n', b'id: 1\ndata: nope\n\n',
    b'id: 1\ndata: []\n\n', b'id: 1\ndata: {"id":2}\n\n',
    b'id: 1\ndata: {"id":true,"kind":"x","timestamp":1}\n\n',
    b'id: 1\ndata: {"id":1,"kind":"x","timestamp":NaN}\n\n',
    b'data: \xff\n\n', b':' + b'x' * 65536 + b'\n',
    (b'data: ' + b'x' * 65000 + b'\n') * 5,
])
def test_invalid_or_oversized_frames_fail_explicitly(api, body):
    with pytest.raises(api["ClientError"]):
        list(api["iter_events"](io.BytesIO(body)))


class MemoryResponse(io.BytesIO):
    def __init__(self, body, content_type="text/event-stream"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.finished = threading.Event()

    def close(self):
        super().close()
        self.finished.set()


def test_reconnect_replays_cursor_without_duplicates(api):
    calls = []
    responses = [MemoryResponse(frame(1)), MemoryResponse(frame(1) + frame(2))]

    def open_response(request, timeout):
        calls.append((request.full_url, request.get_header("Last-event-id"), timeout))
        return responses[min(len(calls), 2) - 1]

    client = api["SchedulerClient"]("http://scheduler.invalid", timeout=1, opener=open_response)
    reader = api["EventReader"](client, retry_delay=0.02)
    reader.start()
    try:
        seen = collect_until(reader, 2)
        assert [event["id"] for event in seen] == [1, 2]
        assert calls[0] == ("http://scheduler.invalid/v1/events?since=0", "0", 1)
        assert calls[1] == ("http://scheduler.invalid/v1/events?since=1", "1", 1)
    finally:
        assert reader.close()
    assert all(response.closed for response in responses)
    assert not reader.thread.is_alive()


def test_bounded_queue_reports_loss_and_close_interrupts_backoff(api):
    response = MemoryResponse(b''.join(frame(i) for i in range(1, 6)))
    client = api["SchedulerClient"]("http://scheduler.invalid", timeout=1,
                                    opener=lambda *args, **kwargs: response)
    reader = api["EventReader"](client, queue_size=2, retry_delay=30)
    reader.start()
    try:
        assert response.finished.wait(2)
        update = reader.drain()
        assert update["cursor"] == 5
        assert update["dropped"] == 3
        assert [event["id"] for event in update["events"]] == [4, 5]
        started = time.monotonic()
        assert reader.close()
        assert time.monotonic() - started < 1
    finally:
        reader.close()


def test_wrong_content_type_closes_response(api):
    response = MemoryResponse(b'<html>not SSE</html>', content_type="text/html")
    client = api["SchedulerClient"]("http://scheduler.invalid", timeout=1,
                                    opener=lambda *args, **kwargs: response)
    reader = api["EventReader"](client, retry_delay=30)
    reader.start()
    try:
        assert response.finished.wait(2)
        update = reader.drain()
        assert "invalid content type" in update["status"]
        assert update["cursor"] == 0
        assert update["events"] == []
    finally:
        assert reader.close()


def test_server_history_gap_is_explicit(api):
    response = MemoryResponse(frame(10))
    client = api["SchedulerClient"]("http://scheduler.invalid", timeout=1,
                                    opener=lambda *args, **kwargs: response)
    reader = api["EventReader"](client, retry_delay=30)
    reader.start()
    try:
        assert response.finished.wait(2)
        update = reader.drain()
        assert update["missed"] == 9
        assert update["dropped"] == 0
        assert update["events"][0]["id"] == 10
    finally:
        assert reader.close()


@pytest.fixture
def core_stream():
    config = SchedulerConfig(listen_host="127.0.0.1", listen_port=1,
                             event_heartbeat_seconds=15, request_timeout_seconds=1)
    scheduler = Scheduler(config)
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    paths = []

    class TrackingHandler(SchedulerHandler):
        def do_GET(self):
            paths.append(self.path)
            super().do_GET()

    server.RequestHandlerClass = TrackingHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, scheduler, paths, "http://127.0.0.1:%s" % server.server_port
    finally:
        scheduler.stop()
        server.scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_core_delivery_and_idle_socket_cleanup(api, core_stream):
    _, scheduler, paths, url = core_stream
    reader = api["EventReader"](api["SchedulerClient"](url, timeout=1))
    reader.start()
    try:
        wait_for(lambda: reader.drain()["status"] == "SSE connected")
        started = time.monotonic()
        emitted = scheduler.emit("sleep", model="synthetic-fixture")
        seen = collect_until(reader, 1)
        assert seen[0]["id"] == emitted.id
        assert seen[0]["model"] == "synthetic-fixture"
        assert time.monotonic() - started < 1
        started = time.monotonic()
        assert reader.close()
        assert time.monotonic() - started < 1  # No wait for the 15-second heartbeat.
        assert paths == ["/v1/events?since=0"]
    finally:
        reader.close()
    assert not reader.thread.is_alive()


def test_known_daemon_restart_resets_cursor_but_disconnect_does_not(api, core_stream):
    server, old, paths, url = core_stream
    old.emit("state")
    old.emit("state")
    reader = api["EventReader"](api["SchedulerClient"](url, timeout=1), retry_delay=0.02)
    reader.start()
    try:
        assert [event["id"] for event in collect_until(reader, 2)] == [1, 2]
        replacement = Scheduler(old.config)
        replacement.emit("state", detail={"fixture": "new-daemon-history"})
        server.scheduler = replacement
        old.stop()
        wait_for(lambda: "/v1/events?since=2" in paths)
        before = reader.drain()
        assert before["cursor"] == 2
        assert before["generation"] == 0
        assert before["events"] == []
        reader.reset_cursor()  # Explicit known restart, no inference from disconnect.
        seen = collect_until(reader, 1)
        assert seen[0]["id"] == 1
        assert seen[0]["detail"] == {"fixture": "new-daemon-history"}
        assert reader.drain()["generation"] == 1
        assert paths[:3] == ["/v1/events?since=0", "/v1/events?since=2", "/v1/events?since=0"]
    finally:
        assert reader.close()


def test_dirty_notification_runs_outside_lock_and_comments_are_not_events(api):
    response = MemoryResponse(b': heartbeat\n\n' + frame(1) + b': heartbeat\n\n')
    reader = api["EventReader"](api["SchedulerClient"]("http://scheduler.invalid", timeout=1,
                               opener=lambda *args, **kwargs: response), retry_delay=30)
    updates = []
    changed = threading.Event()
    def notified():
        # drain takes the same lock: notification must happen outside it.
        updates.append(reader.drain())
        if any(item["events"] for item in updates):
            changed.set()
    reader.set_notify(notified)
    reader.start()
    try:
        assert changed.wait(2)
    finally:
        assert reader.close()
    assert [event["id"] for update in updates for event in update["events"]] == [1]
    assert any(update["status"] == "SSE connected" for update in updates)
    reader.set_notify(None)
