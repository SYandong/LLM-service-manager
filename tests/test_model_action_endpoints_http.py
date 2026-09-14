# Generated-By: Claude Code / claude-fable-5-1
"""HTTP routing, gating and previews for /v1/sleep, /v1/stop and /v1/preload."""

import http.client
import json
import threading
from dataclasses import replace

import pytest

from llmsvc.actions import ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from test_model_sleep_stop_preload import Backend, Transport, awake


def request(address, method, path, body=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def service(tmp_path):
    backend = Backend(names=("a",))
    backend.free = {0: 120.0}
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
                             state_db_path=str(tmp_path / "unused.sqlite"), free_timeout_seconds=2,
                             wake_timeout_seconds=2, action_observe_seconds=0.5, action_poll_seconds=0.005)
    scheduler = Scheduler(config, backend.collect)
    scheduler.model_actions = ModelActionController(scheduler, Transport(backend))
    scheduler.sample_once()
    core = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: core.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield scheduler, core.server_address, backend
    finally:
        scheduler.stop()
        core.shutdown()
        core.server_close()
        thread.join(2)


def stopped(backend, name="a"):
    backend.models[name] = replace(awake(name), state="stopped", gpu=None, unit_active=False,
                                   health_ok=None, is_sleeping=None, resident_gb=None, swap_state="stopped")


def test_sleep_endpoint_returns_the_observed_transition(service):
    scheduler, address, backend = service
    status, result = request(address, "POST", "/v1/sleep/a")
    assert status == 200 and result["status"] == "ready" and result["error"] is None
    assert result["model"] == "a" and result["state"] == "sleeping"
    assert backend.calls == [("sleep", "a")]


def test_stop_endpoint_returns_the_observed_transition(service):
    scheduler, address, backend = service
    status, result = request(address, "POST", "/v1/stop/a")
    assert status == 200 and result["status"] == "ready" and result["state"] == "stopped"
    assert backend.calls == [("stop", "a")]


def test_preload_endpoint_cold_starts_then_sleeps(service):
    scheduler, address, backend = service
    stopped(backend)
    status, result = request(address, "POST", "/v1/preload/a")
    assert status == 200 and result["status"] == "ready" and result["state"] == "sleeping"
    assert result["already_resident"] is False
    assert backend.calls == [("wake", "a"), ("sleep", "a")]


@pytest.mark.parametrize("operation,expected", [
    ("sleep", [{"kind": "sleep", "model": "a", "reason": "user_sleep", "gpu": 0}]),
    ("stop", [{"kind": "stop", "model": "a", "reason": "user_stop", "gpu": 0}]),
    ("preload", []),
])
def test_dry_run_previews_without_any_model_action(service, operation, expected):
    scheduler, address, backend = service
    status, result = request(address, "POST", "/v1/%s/a?dry_run=1" % operation)
    assert status == 200 and result == {"would": expected, "blocked_by": []}
    assert backend.calls == []


def test_dry_run_reports_the_protection_blocker_for_a_default_model(service):
    scheduler, address, backend = service
    backend.models["a"] = replace(backend.models["a"], is_default=True)
    scheduler.sample_once()  # A preview is pure policy over the published snapshot.
    status, result = request(address, "POST", "/v1/stop/a?dry_run=1")
    assert status == 200 and result["would"] == []
    assert [item["reason"] for item in result["blocked_by"]] == ["default_model"]
    assert backend.calls == []


@pytest.mark.parametrize("operation", ["sleep", "stop", "preload"])
def test_read_only_scheduler_rejects_every_per_model_action(service, operation):
    scheduler, address, backend = service
    scheduler.config = replace(scheduler.config, read_only=True)
    status, result = request(address, "POST", "/v1/%s/a" % operation)
    assert status == 405 and result["error"] == "read_only"
    assert backend.calls == []


@pytest.mark.parametrize("operation", ["sleep", "stop", "preload"])
def test_disabled_model_actions_reject_every_per_model_action(service, operation):
    scheduler, address, backend = service
    scheduler.config = replace(scheduler.config, model_actions_enabled=False)
    status, result = request(address, "POST", "/v1/%s/a" % operation)
    assert status == 405 and result["error"] == "operation_not_enabled"
    assert backend.calls == []


@pytest.mark.parametrize("operation", ["sleep", "stop", "preload"])
def test_a_request_body_or_an_empty_model_is_rejected(service, operation):
    scheduler, address, backend = service
    assert request(address, "POST", "/v1/%s/a" % operation, {"model": "a"})[0] == 400
    assert request(address, "POST", "/v1/%s/" % operation)[0] == 400
    assert backend.calls == []


@pytest.mark.parametrize("path", ["/v1/sleep", "/v1/stop", "/v1/preload"])
def test_the_action_prefixes_are_not_collection_endpoints(service, path):
    scheduler, address, backend = service
    status, result = request(address, "POST", path)
    assert status == 405 and result["error"] == "operation_not_enabled"
    assert backend.calls == []


def test_encoded_model_paths_reach_the_configured_model(tmp_path):
    backend = Backend(names=("org/a b?#%",))
    backend.free = {0: 120.0}
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
                             state_db_path=str(tmp_path / "unused.sqlite"), free_timeout_seconds=2,
                             action_observe_seconds=0.5, action_poll_seconds=0.005)
    scheduler = Scheduler(config, backend.collect)
    scheduler.model_actions = ModelActionController(scheduler, Transport(backend))
    scheduler.sample_once()
    core = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: core.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        from urllib.parse import quote
        status, result = request(core.server_address, "POST", "/v1/sleep/" + quote("org/a b?#%", safe=""))
        assert status == 200 and result["model"] == "org/a b?#%" and result["status"] == "ready"
        assert backend.calls == [("sleep", "org/a b?#%")]
    finally:
        scheduler.stop()
        core.shutdown()
        core.server_close()
        thread.join(2)
