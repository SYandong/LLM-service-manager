# Generated-By: Codex / gpt-6-astra
"""Opt-in pin writes with authoritative transport identity and no GPU actions."""

import http.client
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.policy import plan_idle_sleep
from llmsvc.scheduler import IntentWriteError, Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, MemoryState, ModelState, Pin, StateSnapshot
from llmsvc.store import IntentStore


@pytest.fixture
def service(tmp_path):
    database = tmp_path / "intent.sqlite"
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, state_db_path=str(database),
        collectors={"ip_containers": {"127.0.0.1": "actual-container", "192.0.2.1": "spoof-container"}})
    store = IntentStore(database, action_lock=threading.RLock())
    clock = [10000.0]
    observed = StateSnapshot(sampled_at=10000,
        models=(ModelState("model", state="awake", gpu=0, weights_gb=40, cold_start_seconds=120),),
        activity=(Activity("model", last_request_at=9000, requests_last_hour=0, in_flight=0),),
        memory=MemoryState(500, 0))
    scheduler = Scheduler(config, lambda: observed, store=store, clock=lambda: clock[0])
    scheduler.sample_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield scheduler, server.server_address, database, clock
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)
        store.close()


def request(address, method, route, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request(method, route, body=json.dumps(body) if body is not None else None,
                           headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def pin_body(**changes):
    return {"model": "model", "until": 11000, "by": "claimed-owner", **changes}


def test_spoofed_body_and_forwarded_headers_do_not_control_owner(service):
    scheduler, address, _, _ = service
    status, result = request(address, "POST", "/v1/pin", pin_body(by="spoof-container"),
        {"X-Forwarded-For": "192.0.2.1", "Forwarded": "for=192.0.2.1", "X-Real-IP": "192.0.2.1"})
    assert status == 200
    assert result == {"model": "model", "until": 11000.0, "by": "actual-container"}
    status, state = request(address, "GET", "/v1/state")
    assert state["read_only"] is False
    assert state["pins"] == [result]
    assert scheduler.events_since(0)[-1].detail["by"] == "actual-container"
    assert "spoof-container" not in json.dumps(scheduler.events_since(0)[-1].detail)


def test_unmapped_peer_has_explicit_ip_instead_of_claimed_owner(service):
    scheduler, address, _, _ = service
    scheduler.config = replace(scheduler.config, collectors={})
    status, result = request(address, "POST", "/v1/pin", pin_body())
    assert status == 200
    assert result["by"] == "ip:127.0.0.1"


def test_unpin_is_idempotent_and_audits_the_actual_actor(service):
    scheduler, address, _, _ = service
    assert request(address, "POST", "/v1/pin", pin_body())[0] == 200
    for _ in range(2):
        status, result = request(address, "DELETE", "/v1/pin/model", headers={"X-Forwarded-For": "192.0.2.1"})
        assert status == 200
        assert result == {"model": "model", "by": "actual-container"}
    assert not scheduler.snapshot().pins
    event = scheduler.events_since(0)[-1]
    assert event.kind == "unpin" and event.detail["by"] == "actual-container"


def test_http_pin_retained_over_store_restart_expiry_and_fault_observations(service):
    scheduler, address, database, clock = service
    assert request(address, "POST", "/v1/pin", pin_body())[0] == 200
    scheduler.store.close()
    scheduler.store = IntentStore(database, action_lock=scheduler.action_lock)
    assert scheduler.snapshot().pins == (Pin("model", 11000, "actual-container"),)
    assert not plan_idle_sleep(scheduler.snapshot()).actions
    original = scheduler.snapshot()
    for state, active, health in (("stopped", False, None), ("unknown", True, False), ("awake", True, True)):
        scheduler.collect = lambda: replace(original, models=(replace(original.models[0], state=state, unit_active=active, health_ok=health),))
        scheduler.sample_once()
        assert scheduler.snapshot().pins == (Pin("model", 11000, "actual-container"),)
    clock[0] = 11000
    assert not scheduler.snapshot().pins
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT model, owner FROM llmsvc_pins").fetchall() == [("model", "actual-container")]
    scheduler.store.close()


def test_live_write_waits_for_existing_global_lock(service):
    scheduler, address, _, _ = service
    result = []
    started, finished = threading.Event(), threading.Event()
    def write():
        started.set()
        result.append(request(address, "POST", "/v1/pin", pin_body()))
        finished.set()
    with scheduler.action_lock:
        worker = threading.Thread(target=write)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        assert not scheduler.store.active(10000)[0]
    worker.join(2)
    assert finished.is_set() and result[0][0] == 200
    assert len(scheduler.snapshot().pins) == 1


def test_live_config_preview_invokes_no_writer_and_preserves_claimed_preview_label(service, monkeypatch):
    scheduler, address, database, _ = service
    before = database.read_bytes(), scheduler.snapshot(), scheduler.events_since(0)
    def fail(*args, **kwargs):
        raise AssertionError("preview called a writer")
    monkeypatch.setattr(scheduler.store, "put_pin", fail)
    monkeypatch.setattr(scheduler.store, "remove_pin", fail)
    status, result = request(address, "POST", "/v1/pin?dry_run=1", pin_body())
    assert status == 200 and result["would"][0]["by"] == "claimed-owner"
    assert request(address, "DELETE", "/v1/pin/model?dry_run=1")[0] == 200
    assert (database.read_bytes(), scheduler.snapshot(), scheduler.events_since(0)) == before


def test_default_readonly_rejects_live_pin_without_calling_store(service, monkeypatch):
    scheduler, address, database, _ = service
    scheduler.config = replace(scheduler.config, read_only=True)
    def fail(*args, **kwargs):
        raise AssertionError("read-only called a writer")
    monkeypatch.setattr(scheduler.store, "put_pin", fail)
    before = database.read_bytes()
    assert request(address, "POST", "/v1/pin", pin_body()) == (405, {"error": "read_only", "message": "Scheduler is read-only"})
    assert database.read_bytes() == before
    with pytest.raises(IntentWriteError) as exc:
        scheduler.write_pin("pin", pin_body(), source_ip="127.0.0.1")
    assert exc.value.status == 405


@pytest.mark.parametrize("route", ["/v1/free", "/v1/wake/model", "/v1/place", "/v1/models"])
def test_other_live_actions_remain_disabled(service, route):
    _, address, _, _ = service
    assert request(address, "POST", route, {}) == (405, {"error": "operation_not_enabled"})


@pytest.mark.parametrize("changes,status,error", [
    ({"until": 10000}, 400, "invalid_request"),
    ({"until": "2030-01-01"}, 400, "invalid_request"),
    ({"until": float("nan")}, 400, "invalid_request"),
    ({"model": "missing"}, 404, "unknown_model"),
    ({"by": 1}, 400, "invalid_request"),
    ({"extra": True}, 400, "invalid_request"),
])
def test_invalid_live_pin_is_not_persisted(service, changes, status, error):
    scheduler, address, database, _ = service
    before = database.read_bytes(), scheduler.events_since(0)
    assert request(address, "POST", "/v1/pin", pin_body(**changes)) == (status, {"error": error})
    assert (database.read_bytes(), scheduler.events_since(0)) == before


def test_store_failure_is_sanitized_and_publishes_no_success_event(service, monkeypatch):
    scheduler, address, _, _ = service
    before = scheduler.events_since(0)
    def fail(*args):
        raise sqlite3.OperationalError("private database path")
    monkeypatch.setattr(scheduler.store, "put_pin", fail)
    assert request(address, "POST", "/v1/pin", pin_body()) == (503, {"error": "intent_store_unavailable"})
    assert scheduler.events_since(0) == before


def test_missing_store_is_503_and_does_not_create_a_database(tmp_path):
    path = tmp_path / "missing.sqlite"
    service = Scheduler(SchedulerConfig("127.0.0.1", 8011, read_only=False, state_db_path=str(path)))
    with pytest.raises(IntentWriteError) as exc:
        service.write_pin("pin", pin_body(), source_ip="127.0.0.1")
    assert exc.value.status == 503
    assert not path.exists()


def test_configured_model_can_be_pinned_despite_unknown_telemetry(service):
    scheduler, address, _, _ = service
    scheduler.config = replace(scheduler.config, collectors={**scheduler.config.collectors, "models": {"configured": {}}})
    scheduler.collect = lambda: StateSnapshot(errors=("memory_unknown",))
    scheduler.sample_once()
    assert request(address, "POST", "/v1/pin", pin_body(model="configured"))[0] == 200
    assert scheduler.snapshot().pins[0].model == "configured"


def test_live_pin_preserves_required_request_shape(service):
    scheduler, address, _, _ = service
    assert request(address, "POST", "/v1/pin", {"model": "model", "until": 11000}) == (400, {"error": "invalid_request"})
    assert not scheduler.snapshot().pins



def test_reserve_intent_route_validates_input_without_model_action_optin(service):
    scheduler, address, _, _ = service
    assert request(address, "POST", "/v1/reserve", {}) == (400, {"error": "invalid_request"})
    assert scheduler.snapshot().reserves == ()
    assert not scheduler.config.model_actions_enabled
