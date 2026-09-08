# Generated-By: Codex / gpt-6-astra
"""Internal reserve persistence and validation proofs."""

import threading
import time
from dataclasses import replace

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import IntentWriteError, Scheduler
from llmsvc.state import GPUState, StateSnapshot
from llmsvc.store import IntentStore


@pytest.fixture
def service(tmp_path):
    store = IntentStore(tmp_path / "intents.sqlite", action_lock=threading.RLock())
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, state_db_path=str(tmp_path/"intents.sqlite"),
        collectors={"ip_containers": {"127.0.0.1": "trusted-owner"}})
    scheduler = Scheduler(config, lambda: StateSnapshot(sampled_at=time.time(),
        gpus=(GPUState(0, total_gb=100, free_gb=100, external_gb=0),)), store=store)
    scheduler.sample_once()
    try:
        yield scheduler
    finally:
        scheduler.stop()
        store.close()


def payload(**kwargs):
    return {"gpu": 0, "size_gb": 80, "until": time.time()+3600, "by": "spoofed-owner", **kwargs}


def test_authoritative_owner_and_restart_expiry_retains_row(service):
    request = payload()
    record = service._save_reserve(request, source_ip="::ffff:127.0.0.1")
    assert record.by == "trusted-owner" and record.id != "preview"
    path = service.config.state_db_path
    reader = IntentStore(path, action_lock=threading.RLock(), read_only=True)
    try:
        assert reader.active(time.time())[1] == (record,)
        assert reader.active(request["until"]+1)[1] == ()
        assert reader.reserve(record.id) == record
    finally:
        reader.close()
    assert service.snapshot().reserves == (record,)


def test_delete_is_idempotent_and_does_not_change_other_reservations(service):
    first = service._save_reserve(payload(), source_ip="127.0.0.1")
    second = service._save_reserve(payload(), source_ip="192.0.2.8")
    assert second.by == "ip:192.0.2.8"
    for _ in range(2):
        assert service._delete_reserve(first.id, source_ip="127.0.0.1") == {"id": first.id, "by": "trusted-owner"}
    assert service.store.reserve(first.id) is None
    assert service.snapshot().reserves == (second,)


@pytest.mark.parametrize("invalid", [{"until": float("inf")}, {"until": float("nan")}, {"until": True},
    {"until": 1}, {"until": "2030-01-01T00:00:00"}, {"size_gb": 0}, {"size_gb": float("inf")},
    {"size_gb": True}, {"gpu": True}, {"gpu": -1}, {"gpu": 1}, {"by": ""}, {"extra": 1}])
def test_invalid_input_never_creates_intent(service, invalid):
    with pytest.raises(ValueError):
        service._save_reserve(payload(**invalid), source_ip="127.0.0.1")
    assert not service.snapshot().reserves


def test_dry_run_never_allocates_id_writes_collects_or_publishes(service, monkeypatch):
    existing = service._save_reserve(payload(), source_ip="127.0.0.1")
    service.config = replace(service.config, read_only=True)
    events = service.events_since(0)
    before = open(service.config.state_db_path, "rb").read()
    monkeypatch.setattr("llmsvc.scheduler.uuid.uuid4", lambda: pytest.fail("dry-run allocated an ID"))
    service.collect = lambda: pytest.fail("dry-run collected")
    service.store.put_reserve = lambda *a, **k: pytest.fail("dry-run wrote")
    service.store.remove_reserve = lambda *a, **k: pytest.fail("dry-run deleted")
    preview = service._save_reserve(payload(), source_ip="127.0.0.1", dry_run=True)
    assert preview.by == "trusted-owner"
    service._delete_reserve(existing.id, source_ip="127.0.0.1", dry_run=True)
    assert service.events_since(0) == events
    assert open(service.config.state_db_path, "rb").read() == before


def test_readonly_and_missing_store_fail_before_writer(service):
    service.config = replace(service.config, read_only=True)
    with pytest.raises(IntentWriteError, match="read_only"):
        service._save_reserve(payload(), source_ip="127.0.0.1")
    service.config = replace(service.config, read_only=False)
    service.store = None
    with pytest.raises(IntentWriteError, match="intent_store_unavailable"):
        service._save_reserve(payload(), source_ip="127.0.0.1")
