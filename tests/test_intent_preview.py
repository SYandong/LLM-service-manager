# Generated-By: Codex / gpt-6-astra
"""Policy/API dry-runs consume durable intent without invoking any writer."""

import http.client
import json
import threading
from dataclasses import replace

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.policy import plan_idle_sleep
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, Reserve, StateSnapshot
from llmsvc.store import IntentStore


def observed():
    return StateSnapshot(sampled_at=10000,
        gpus=(GPUState(0, total_gb=100, free_gb=20, external_gb=0),),
        models=(ModelState("model", state="awake", gpu=0, resident_gb=40,
                           budget_gb=40, weights_gb=40, cold_start_seconds=120),),
        activity=(Activity("model", 9000, 0, 0, 0),),
        memory=MemoryState(500, 0))


def scheduler(store=None):
    result = Scheduler(SchedulerConfig("127.0.0.1", 8011), observed, store=store, clock=lambda: 10000)
    result.sample_once()
    return result


def test_restored_pin_survives_unit_failure_and_recovery(tmp_path):
    path = tmp_path / "state.sqlite"
    lock = threading.RLock()
    store = IntentStore(path, action_lock=lock)
    store.put_pin(Pin("model", 11000, "owner"))
    store.close()
    store = IntentStore(path, action_lock=lock, read_only=True)
    service = scheduler(store)
    assert service.action_lock is lock
    assert not service.preview("free", {})["would"]
    assert not plan_idle_sleep(service.snapshot()).actions
    service.collect = lambda: replace(observed(), models=(replace(observed().models[0], state="stopped", unit_active=False),))
    service.sample_once()
    assert service.snapshot().pins == (Pin("model", 11000, "owner"),)
    service.collect = observed
    service.sample_once()
    assert not service.preview("free", {})["would"]
    service.clock = lambda: 11000
    assert not service.snapshot().pins
    # This tests retained intent, not the later fault-cleanup executor.
    store.close()


def test_preview_estimates_are_not_reported_as_measured_release():
    result = scheduler().preview("free", {"need_gb": 30})
    assert result["would"] == [{"kind": "sleep", "model": "model", "reason": "free", "gpu": 0}]
    assert result["estimated_freed_gb"] == 38
    assert "freed_gb" not in result


def test_stale_observations_block_preview():
    service = scheduler()
    service.clock = lambda: 10031
    assert service.preview("free", {}) == {"would": [], "blocked_by": [{"model": None, "reason": "stale_snapshot"}]}


def test_closed_store_fails_closed_without_clearing_pin(tmp_path):
    store = IntentStore(tmp_path / "state.sqlite", action_lock=threading.RLock())
    service = scheduler(store)
    store.close()
    assert service.preview("free", {})["would"] == []
    assert "intent_store_unavailable" in service.snapshot().errors


@pytest.mark.parametrize("operation,payload", [
    ("pin", {"model": "model", "until": 9999, "by": "owner"}),
    ("pin", {"model": "model", "until": "2030-01-01", "by": "owner"}),
    ("pin", {"model": "missing", "until": 20000, "by": "owner"}),
    ("reserve", {"gpu": True, "size_gb": 80, "until": 20000, "by": "owner"}),
    ("reserve", {"gpu": 0, "size_gb": -1, "until": 20000, "by": "owner"}),
    ("free", {"gpu": 1}), ("free", {"ram": "false"}), ("free", {"surprise": True}),
])
def test_invalid_payload_rejected(operation, payload):
    with pytest.raises(ValueError):
        scheduler().preview(operation, payload)


def test_http_previews_change_neither_database_snapshot_nor_events(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    store = IntentStore(path, action_lock=threading.RLock())
    store.put_reserve(Reserve("existing", 0, 10, 20000, "owner"))
    service = scheduler(store)
    before = (path.read_bytes(), service.snapshot(), service.events_since(0))
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run invoked a writer")
    monkeypatch.setattr(store, "_write", forbidden)
    server = SchedulerHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    cases = [
        ("POST", "/v1/free?dry_run=1", {}, "sleep"),
        ("POST", "/v1/pin?dry_run=1", {"model": "model", "until": "2030-01-01T00:00:00Z", "by": "owner"}, "pin"),
        ("POST", "/v1/reserve?dry_run=1", {"gpu": 0, "size_gb": 80, "until": 20000, "by": "owner"}, "reserve"),
        ("DELETE", "/v1/pin/model?dry_run=1", None, "unpin"),
        ("DELETE", "/v1/reserve/existing?dry_run=1", None, "unreserve"),
    ]
    try:
        for method, route, body, kind in cases:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request(method, route, body=json.dumps(body) if body is not None else None)
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read())["would"][0]["kind"] == kind
            finally:
                connection.close()
        assert (path.read_bytes(), service.snapshot(), service.events_since(0)) == before
    finally:
        service.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)
        store.close()


def test_zero_need_matches_pure_policy_no_action():
    assert scheduler().preview("free", {"need_gb": 0})["would"] == []
