# Generated-By: Codex / gpt-6-astra
"""Usage HTTP contract with a real read-only SQLite adapter and synthetic rows."""

import http.client
import json
import sqlite3
import threading
import time

import pytest

from llmsvc.__main__ import build_collector, build_usage
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer


@pytest.fixture
def service(tmp_path):
    database = tmp_path / "activity.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE activity (id INTEGER PRIMARY KEY, ts_created INTEGER, model_id TEXT, input_tokens INTEGER, output_tokens INTEGER, metadata_json TEXT)")
        db.execute("INSERT INTO activity VALUES (1, ?, 'example', 11, 22, NULL)", (int(time.time()) - 1,))
    config = SchedulerConfig("127.0.0.1", 8011, collectors={"swap_url": "http://127.0.0.1:9", "activity_path": str(database)})
    collector = build_collector(config)
    scheduler = Scheduler(config, collector, usage=build_usage(collector))
    # Do not sample: no external probes are needed for the usage endpoint.
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    before = database.read_bytes()
    try:
        yield scheduler, server.server_address
        assert database.read_bytes() == before
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)


def get(address, path):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_usage_source_unknown_with_known_totals_and_no_action_lock(service):
    scheduler, address = service
    # Handler executes on another thread; acquiring action_lock would deadlock.
    with scheduler.action_lock:
        status, result = get(address, "/v1/usage")
    assert status == 200
    assert result["days"] == 7 and result["by"] == "container"
    assert result["known"] is True
    assert result["totals"] == {"requests": 1, "input_tokens": 11, "output_tokens": 22}
    assert result["rows"][0]["container"] == "unknown"
    assert result["rows"][0]["source_known"] is False
    assert not scheduler.events_since(0)


@pytest.mark.parametrize("by", ["container", "ip", "model"])
def test_supported_groupings(service, by):
    _, address = service
    status, result = get(address, f"/v1/usage?days=1&by={by}")
    assert status == 200
    assert result["by"] == by
    assert result["totals"]["requests"] == 1


@pytest.mark.parametrize("query", ["days=0", "days=-1", "days=1.5", "days=", "days=true",
    "by=unknown", "days=1&days=2", "extra=1", "days=" + "9" * 30])
def test_invalid_query(service, query):
    _, address = service
    assert get(address, "/v1/usage?" + query)[0] == 400


def test_unconfigured_or_failed_reader_returns_unknown_503(service):
    scheduler, address = service
    scheduler._usage = None
    status, result = get(address, "/v1/usage")
    assert status == 503 and result["known"] is False
    assert result["totals"] == {"requests": None, "input_tokens": None, "output_tokens": None}
    def fail(**kwargs):
        raise OSError("private backend details")
    scheduler._usage = fail
    status, result = get(address, "/v1/usage")
    assert status == 503
    assert result["error"] == "usage_unavailable"
    assert "private" not in json.dumps(result)
