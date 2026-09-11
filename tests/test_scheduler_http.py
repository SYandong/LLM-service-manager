# Generated-By: Codex / gpt-6-astra
"""Real loopback HTTP tests with synthetic observations and no GPU work."""

import http.client
import json
import threading
import time
from types import SimpleNamespace

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.actions import ActionDispatchError, ModelActionController
from llmsvc.leases import LeaseError, PlacementController
from llmsvc.scheduler import IntentWriteError, Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.store import IntentStore
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
    ("POST", "/v1/free"),
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


def test_http_pin_commits_before_stop_and_new_pin_rejects_after_stop(tmp_path):
    db = tmp_path / "ledger.sqlite"
    config = SchedulerConfig("127.0.0.1", 18091, read_only=False, state_db_path=str(db),
                             sample_interval_seconds=.02)
    lock = threading.RLock()
    store = IntentStore(str(db), action_lock=lock)
    snapshot = StateSnapshot(gpus=(GPUState(0, free_gb=42),),
                             models=(ModelState("first", state="awake"), ModelState("second", state="awake")),
                             activity=(Activity("first", in_flight=0), Activity("second", in_flight=0)))
    scheduler = Scheduler(config, lambda: snapshot, store=store)
    scheduler.sample_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    entered, release, stop_started, stop_done = (threading.Event() for _ in range(4))
    original_put = store.put_pin
    calls = []
    def put_pin(pin, *args, **kwargs):
        calls.append(("enter", pin.model, scheduler.stopping.is_set()))
        if pin.model == "first":
            entered.set()
            assert release.wait(3)
        result = original_put(pin, *args, **kwargs)
        calls.append(("commit", pin.model, scheduler.stopping.is_set()))
        return result
    store.put_pin = put_pin
    scheduler.start(); serving.start()
    responses = []
    first = stopper = None
    try:
        body = json.dumps({"model": "first", "until": time.time() + 60, "by": "fixture"})
        first = threading.Thread(target=lambda: responses.append(request(server.server_address, "POST", "/v1/pin", body=body)))
        first.start(); assert entered.wait(2)
        stopper = threading.Thread(target=lambda: (stop_started.set(), scheduler.stop(), stop_done.set()))
        stopper.start(); assert stop_started.wait(1)
        release.set(); assert stop_done.wait(3)
        first.join(3); stopper.join(3)
        second_body = json.dumps({"model": "second", "until": time.time() + 60, "by": "fixture"})
        status, payload = request(server.server_address, "POST", "/v1/pin", body=second_body)
        assert status == 503 and payload["error"] == "scheduler_stopping"
        pins = [pin.model for pin in store.active(time.time())[0]]
        assert pins == ["first"]
        assert calls == [("enter", "first", False), ("commit", "first", False)]
        assert responses[0][0] == 200
    finally:
        release.set()
        for worker in (first, stopper):
            if worker is not None: worker.join(3)
        assert all(worker is None or not worker.is_alive() for worker in (first, stopper))
        if serving.is_alive(): server.shutdown()
        server.server_close()
        serving.join(3)
        if not scheduler.stopping.is_set(): scheduler.stop()
        store.close()


def test_lock_queued_pin_and_unpin_reject_after_stop_without_side_effects(tmp_path):
    db = tmp_path / "queued.sqlite"
    config = SchedulerConfig("127.0.0.1", 18094, read_only=False, state_db_path=str(db))
    store = IntentStore(str(db), action_lock=threading.RLock())
    scheduler = Scheduler(config, lambda: StateSnapshot(
        gpus=(GPUState(0, free_gb=42),), models=(ModelState("model", state="awake"),),
        activity=(Activity("model", in_flight=0),)), store=store)
    scheduler.sample_once()
    queued = threading.Event()
    original_pending = store.bootstrap_pending
    pending_calls = [0]
    def bootstrap_pending(*args, **kwargs):
        pending_calls[0] += 1
        if pending_calls[0] >= 2: queued.set()
        return original_pending(*args, **kwargs)
    store.bootstrap_pending = bootstrap_pending
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    serving.start()
    responses = []
    pin_body = json.dumps({"model": "model", "until": time.time() + 60, "by": "fixture"})
    workers = [
        threading.Thread(target=lambda: responses.append(("pin", request(server.server_address, "POST", "/v1/pin", body=pin_body)))),
        threading.Thread(target=lambda: responses.append(("unpin", request(server.server_address, "DELETE", "/v1/pin/model")))),
    ]
    before_events = scheduler.events_since(0)
    try:
        with scheduler.action_lock:
            for worker in workers: worker.start()
            assert queued.wait(2)
            scheduler.stopping.set()
        for worker in workers: worker.join(3)
        assert all(not worker.is_alive() for worker in workers)
        assert sorted(responses) == [("pin", (503, {"error": "scheduler_stopping"})),
                                     ("unpin", (503, {"error": "scheduler_stopping"}))]
        assert store.active(time.time()) == ((), ())
        assert scheduler.events_since(0) == before_events
    finally:
        for worker in workers: worker.join(3)
        if serving.is_alive(): server.shutdown()
        server.server_close(); serving.join(3); store.close()


def test_stopping_http_mutation_matrix_is_fail_closed(tmp_path):
    db = tmp_path / "matrix.sqlite"
    config = SchedulerConfig("127.0.0.1", 18093, read_only=False, state_db_path=str(db),
                             model_actions_enabled=True, placement_enabled=True,
                             collectors={"models": {"model": {"unit": "vllm-model.service", "util": .2}}},
                             registry={"config_path": str(tmp_path / "models.yaml"),
                                       "shared_roots": [str(tmp_path)], "daemon_port_range": [8101, 8110]})
    store = IntentStore(str(db), action_lock=threading.RLock())
    scheduler = Scheduler(config, lambda: StateSnapshot(
        gpus=(GPUState(0, free_gb=42),), models=(ModelState("model", state="awake"),),
        activity=(Activity("model", in_flight=0),)), store=store)
    transport_calls = []
    transport = SimpleNamespace(
        models={"model": {"unit": "vllm-model.service", "util": .2}},
        units={"model": "vllm-model.service"},
        check_catalog=lambda: None,
        http_request=lambda *args, **kwargs: transport_calls.append(("http", args)) or 200,
        stop_unit=lambda *args, **kwargs: transport_calls.append(("stop", args)) or 0,
        unit_for_model=lambda name: "vllm-" + name + ".service",
        systemctl="systemctl",
    )
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.placement = PlacementController(scheduler, transport)
    scheduler.registry = SimpleNamespace()
    scheduler.catalog = SimpleNamespace(can_submit=lambda: True, submit_change=lambda *args, **kwargs: None)
    scheduler.sample_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    scheduler.stopping.set()
    until = time.time() + 60
    mutations = [
        ("POST", "/v1/pin", {"model": "model", "until": until, "by": "fixture"}),
        ("DELETE", "/v1/pin/model", None),
        ("POST", "/v1/free", {}),
        ("POST", "/v1/wake/model", {}),
        ("POST", "/v1/place", {"model": "model", "util": .2}),
        ("POST", "/v1/place/lease-1/confirm", None),
        ("POST", "/v1/place/lease-1/release", None),
        ("POST", "/v1/reserve", {"gpu": 0, "size_gb": 1, "until": until, "by": "fixture"}),
        ("DELETE", "/v1/reserve/reserve-1", None),
        ("POST", "/v1/models", {}),
    ]
    before_events = scheduler.events_since(0)
    try:
        results = [request(server.server_address, method, path,
                           body=json.dumps(body) if isinstance(body, dict) else body)
                   for method, path, body in mutations]
        for index, (status, payload) in enumerate(results):
            assert status == 503 and payload["error"] == "scheduler_stopping", (index, status, payload, results)
        assert store.active(time.time()) == ((), ())
        assert scheduler.events_since(0) == before_events
        assert transport_calls == []
    finally:
        server.shutdown(); server.server_close(); thread.join(3); store.close()


def test_direct_controller_admission_guards_reject_stopping_without_mutation(tmp_path):
    db = tmp_path / "ledger.sqlite"
    config = SchedulerConfig("127.0.0.1", 18092, read_only=False, state_db_path=str(db),
                             model_actions_enabled=True, placement_enabled=True,
                             collectors={"models": {"model": {"unit": "vllm-model.service", "util": .2}}})
    store = IntentStore(str(db), action_lock=threading.RLock())
    scheduler = Scheduler(config, lambda: StateSnapshot(), store=store)
    transport = SimpleNamespace(
        models={"model": {"unit": "vllm-model.service", "util": .2}},
        units={"model": "vllm-model.service"},
        check_catalog=lambda: None,
        http_request=lambda *args, **kwargs: 200,
        stop_unit=lambda *args, **kwargs: 0,
        unit_for_model=lambda name: "vllm-" + name + ".service",
        systemctl="systemctl",
    )
    action = ModelActionController(scheduler, transport)
    placement = PlacementController(scheduler, transport)
    scheduler.registry = SimpleNamespace()
    scheduler.catalog = SimpleNamespace(can_submit=lambda: True, submit_change=lambda *args, **kwargs: None)
    scheduler.stopping.set()
    try:
        with pytest.raises(ActionDispatchError, match="scheduler_stopping"): action._enabled()
        with pytest.raises(LeaseError, match="scheduler_stopping"): placement._enabled()
        with pytest.raises(IntentWriteError, match="scheduler_stopping"):
            with scheduler._reserve_lock(time.monotonic() + 1):
                pass
        with pytest.raises(IntentWriteError, match="scheduler_stopping"):
            scheduler.registry_request("POST", "/v1/models", {}, dry_run=False)
        from llmsvc.bootstrap import BootstrapController
        bootstrap = BootstrapController.__new__(BootstrapController)
        bootstrap.scheduler = scheduler
        bootstrap._enabled = lambda: None
        with pytest.raises(IntentWriteError, match="scheduler_stopping"):
            with bootstrap.http_scope("place", {}, "token", "127.0.0.1"):
                pass
        assert store.active(time.time()) == ((), ())
    finally:
        store.close()
