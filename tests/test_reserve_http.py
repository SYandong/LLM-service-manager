# Generated-By: Codex / gpt-6-astra
"""Mounted reserve API with disposable DB and fake managed-unit effects."""

import http.client
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

from llmsvc.actions import ModelActionController
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.store import IntentStore
from test_reserve_evacuation import evacuation, system
from test_reserve_intents import payload


@contextmanager
def running_http(scheduler):
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(address, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=4)
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def api(evacuation):
    scheduler, controller, state, transport, leases = evacuation
    scheduler.config = replace(scheduler.config, collectors={**scheduler.config.collectors,
        "ip_containers": {"127.0.0.1": "actual-owner"}})
    scheduler.reservation_actions = controller
    with running_http(scheduler) as address:
        yield scheduler, address, controller, state, transport, leases


def test_post_then_delete_returns_persisted_record_and_proven_evacuation(api):
    scheduler, address, _, state, _, _ = api
    status, result = request(address, "POST", "/v1/reserve", payload(), {"X-Forwarded-For":"192.0.2.33"})
    assert status == 200 and result["by"] == "actual-owner"
    assert result["gpu"] == 0 and result["size_gb"] == 80
    assert result["evacuation"] == {"status":"complete", "stopped":["a","b"], "skipped":[]}
    assert scheduler.store.reserve(result["id"]).by == "actual-owner"
    assert state["calls"] == ["a","b"]
    for _ in range(2):
        code, deleted = request(address, "DELETE", "/v1/reserve/"+result["id"])
        assert code == 200 and deleted == {"id":result["id"], "by":"actual-owner"}
    assert not scheduler.snapshot().reserves
    assert state["calls"] == ["a","b"]  # Delete never wakes, restarts or stops.


def test_pin_only_optin_saves_blocked_intent_without_enabling_model_actions(api):
    scheduler, address, _, state, _, _ = api
    scheduler.config = replace(scheduler.config, model_actions_enabled=False)
    status, result = request(address, "POST", "/v1/reserve", payload())
    assert status == 200 and result["evacuation"]["status"] == "blocked"
    assert result["evacuation"]["stopped"] == [] and state["calls"] == []
    assert scheduler.store.reserve(result["id"]) is not None


def test_partial_transport_failure_keeps_intent_id_and_confirmed_effect(api):
    scheduler, address, _, state, _, leases = api
    state["mode"] = "exit-error"
    status, result = request(address, "POST", "/v1/reserve", payload())
    assert status == 200 and result["evacuation"]["status"] == "partial"
    assert result["evacuation"]["stopped"] == ["a"]
    assert result["evacuation"]["error"] == "transport_rejected"
    assert scheduler.store.reserve(result["id"]) is not None
    assert scheduler.store.lease(leases["b"])[0].status == "confirmed"


@pytest.mark.parametrize("invalid", [{"until":float("inf")}, {"until":None}, {"until":1},
    {"size_gb":-1}, {"gpu":True}, {"by":""}, {"id":"client-selected"}])
def test_invalid_post_is_400_before_any_persistence_or_transport(api, invalid):
    scheduler, address, _, state, _, _ = api
    status, result = request(address, "POST", "/v1/reserve", payload(**invalid))
    assert status == 400 and result["error"] == "invalid_request"
    assert not scheduler.snapshot().reserves and state["calls"] == []


def test_readonly_defaults_and_dry_run_zero_effects_with_hypothetical_label(api, monkeypatch):
    scheduler, address, controller, state, transport, _ = api
    existing = scheduler._save_reserve(payload(), source_ip="127.0.0.1")
    scheduler.stop()
    scheduler.config = replace(scheduler.config, read_only=True)
    before = open(scheduler.config.state_db_path, "rb").read()
    events = scheduler.events_since(0)
    monkeypatch.setattr("llmsvc.scheduler.uuid.uuid4", lambda: pytest.fail("preview allocated ID"))
    scheduler.collect = lambda: pytest.fail("preview collected")
    controller.accounting.probe = lambda *a, **k: pytest.fail("preview inspected unit")
    transport.run = lambda *a, **k: pytest.fail("preview acted")
    for method, path, body in (("POST","/v1/reserve",payload()), ("DELETE","/v1/reserve/"+existing.id,None)):
        assert request(address, method, path, body)[0] == 405
    code, preview = request(address, "POST", "/v1/reserve?dry_run=1", payload())
    assert code == 200 and preview["would"][0]["by"] == "spoofed-owner"
    assert "id" not in preview["would"][0]
    assert [action["kind"] for action in preview["would"]] == ["reserve","stop","stop"]
    code, preview = request(address, "DELETE", "/v1/reserve/"+existing.id+"?dry_run=1")
    assert code == 200 and preview == {"would":[{"kind":"unreserve","id":existing.id}], "blocked_by":[]}
    assert scheduler.events_since(0) == events and state["calls"] == []
    assert open(scheduler.config.state_db_path, "rb").read() == before


def test_restart_retains_intent_without_automatic_evacuation_and_filters_expiry(api):
    scheduler, address, _, state, transport, _ = api
    scheduler.config = replace(scheduler.config, model_actions_enabled=False)
    status, result = request(address, "POST", "/v1/reserve", payload())
    assert status == 200
    scheduler.stop()
    store = IntentStore(scheduler.config.state_db_path, action_lock=threading.RLock())
    restarted = Scheduler(replace(scheduler.config, model_actions_enabled=True), scheduler.collect, store=store)
    restarted.model_actions = ModelActionController(restarted, transport)
    try:
        restarted.sample_once()
        assert restarted.snapshot().reserves[0].id == result["id"]
        assert state["calls"] == []  # Startup sampling never resumes evacuation.
        restarted.clock = lambda: result["until"]+1
        assert restarted.snapshot().reserves == ()
        assert store.reserve(result["id"]) is not None
        with running_http(restarted) as endpoint:
            assert request(endpoint, "DELETE", "/v1/reserve/"+result["id"])[0] == 200
        assert store.reserve(result["id"]) is None and state["calls"] == []
    finally:
        restarted.stop()
        store.close()


def test_unavailable_store_is_503_and_delete_rejects_body(api):
    scheduler, address, _, state, _, _ = api
    code, _ = request(address, "DELETE", "/v1/reserve/unknown", {"id":"override"})
    assert code == 400
    store = scheduler.store
    scheduler.store = None
    try:
        assert request(address, "POST", "/v1/reserve", payload())[0] == 503
    finally:
        scheduler.store = store
    assert state["calls"] == []


def test_default_controller_uses_bounded_managed_unit_probe_without_place_api(api):
    scheduler, address, _, state, transport, leases = api
    scheduler.reservation_actions = None
    run = transport.run
    inspections = []
    def managed_run(argv, **kwargs):
        if argv[1] != "show":
            return run(argv, **kwargs)
        from types import SimpleNamespace
        name = next(name for name, unit in transport.units.items() if unit == argv[2])
        inspections.append(name)
        assert 0 < kwargs["timeout"] <= scheduler.config.lease_probe_seconds
        observation = state["observations"][name]
        if observation.exited:
            output = "LoadState=not-found\nActiveState=inactive\nMainPID=0\nControlGroup=\nEnvironment=\n"
        else:
            output = "LoadState=loaded\nActiveState=active\nMainPID=123\nControlGroup=/fixture\nEnvironment=LLMSVC_LEASE_ID="+leases[name]+"\n"
        return SimpleNamespace(returncode=0, stdout=output)
    transport.run = managed_run
    code, result = request(address, "POST", "/v1/reserve", payload())
    assert code == 200 and result["evacuation"]["status"] == "complete"
    assert state["calls"] == ["a", "b"] and set(inspections) == {"a", "b"}
    assert scheduler.placement is None and scheduler.config.placement_enabled is False


def test_http_place_exclusion_takes_effect_until_reservation_deleted(api):
    from llmsvc.state import ModelState
    scheduler, address, controller, state, transport, _ = api
    state["models"]["c"] = ModelState("c", state="stopped", unit="vllm-c.service", unit_active=False,
        util=0.1, weights_gb=10)
    state["counts"]["c"] = 0
    transport.models["c"] = {"util":0.1, "weights_gb":10}
    transport.units["c"] = "vllm-c.service"
    scheduler.placement = controller.accounting
    scheduler.config = replace(scheduler.config, placement_enabled=True, model_actions_enabled=False, placement_wait_seconds=0.5)
    scheduler.sample_once()
    code, saved = request(address, "POST", "/v1/reserve", payload(size_gb=1))
    assert code == 200
    code, blocked = request(address, "POST", "/v1/place", {"model":"c", "util":0.1})
    assert code == 409 and "reserved" in [row["reason"] for row in blocked["blockers"]]
    assert not any(lease.model == "c" for lease, _ in scheduler.store.leases())
    assert request(address, "DELETE", "/v1/reserve/"+saved["id"])[0] == 200
    code, placed = request(address, "POST", "/v1/place", {"model":"c", "util":0.1})
    assert code == 200 and placed["gpu"] == 0
    assert state["calls"] == []
