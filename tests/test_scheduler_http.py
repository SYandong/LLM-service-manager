# Generated-By: Codex / gpt-6-astra
"""Real loopback HTTP tests with synthetic observations and no GPU work."""

import http.client
import json
import threading

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, ModelState, StateSnapshot


@pytest.fixture
def service():
    config = SchedulerConfig("127.0.0.1", 8011, event_heartbeat_seconds=0.05)
    snapshot = StateSnapshot(gpus=(GPUState(0, free_gb=42),),
                             models=(ModelState("test", state="sleeping"),),
                             activity=(Activity("test", in_flight=0),))
    scheduler = Scheduler(config, lambda: snapshot)
    scheduler.sample_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield scheduler, server.server_address
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(address, method, path, body=None):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request(method, path, body=body)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_state_matches_collected_snapshot(service):
    scheduler, address = service
    code, data = request(address, "GET", "/v1/state")
    assert code == 200
    assert data == json.loads(json.dumps(scheduler.snapshot().to_dict()))
    assert data["gpus"][0]["free_gb"] == 42
    assert data["models"][0]["state"] == "sleeping"


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/free"), ("POST", "/v1/free?dry_run=1"),
    ("POST", "/v1/place"), ("POST", "/v1/pin"),
    ("DELETE", "/v1/pin/test"), ("PUT", "/v1/state"),
])
def test_all_mutations_are_rejected_without_changing_state(service, method, path):
    scheduler, address = service
    before = scheduler.snapshot()
    events = scheduler.events_since(0)
    code, data = request(address, method, path, body='{"model":"test"}')
    assert code == 405
    assert data["error"] == "read_only"
    assert scheduler.snapshot() == before
    assert scheduler.events_since(0) == events


@pytest.mark.parametrize("query", ["since=-1", "since=", "since=x", "since=1&since=2"])
def test_invalid_event_cursor_is_json_error(service, query):
    _, address = service
    code, _ = request(address, "GET", "/v1/events?" + query)
    assert code == 400


def test_sse_cursor_and_state_reads_progress_during_event_wait(service):
    scheduler, address = service
    latest = scheduler.events_since(0)[-1].id
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", "/v1/events", headers={"Last-Event-ID": str(latest)})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        assert response.readline() == b": connected\n"
        assert response.readline() == b"\n"
        assert request(address, "GET", "/v1/state")[0] == 200
        event = scheduler.emit("test", detail={"hello": "world"})
        lines = []
        for _ in range(12):
            line = response.readline()
            lines.append(line)
            if line.startswith(b"data:"):
                break
        assert f"id: {event.id}\n".encode() in lines
        assert b"event: test\n" in lines
        data = json.loads(lines[-1].decode()[6:])
        assert data["detail"] == {"hello": "world"}
    finally:
        connection.close()


def test_unknown_route(service):
    _, address = service
    assert request(address, "GET", "/missing")[0] == 404
