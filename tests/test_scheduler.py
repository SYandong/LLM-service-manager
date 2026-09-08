# Generated-By: Codex / gpt-6-astra
"""Read-only publication, failure reporting and lock-release regressions."""

import threading
from dataclasses import replace

import pytest

from llmsvc.config import SchedulerConfig, load_config
from llmsvc.scheduler import Scheduler
from llmsvc.state import GPUState, ModelState, StateSnapshot


def config(**kwargs):
    return SchedulerConfig(listen_host="127.0.0.1", listen_port=8011, **kwargs)


def test_default_snapshot_and_memory_thresholds_are_explicit():
    scheduler = Scheduler(config(memory_budget_gb=300))
    snapshot = scheduler.sample_once()
    assert snapshot.errors == ("collectors_not_configured",)
    assert snapshot.read_only
    assert snapshot.memory.host_available_gb is None
    assert snapshot.memory.budget_gb == 300


def test_collection_failure_does_not_keep_a_stale_healthy_snapshot():
    def fail():
        raise OSError("private detail must not escape")
    scheduler = Scheduler(config(), lambda: StateSnapshot(models=(ModelState("test", state="awake"),)))
    assert scheduler.sample_once().models[0].state == "awake"
    scheduler.collect = fail
    assert scheduler.sample_once().errors == ("collection_failed",)
    assert scheduler.snapshot().sampled_at is None
    assert not scheduler.snapshot().models
    assert "private detail" not in str(scheduler.events_since(0))


def test_nonfinite_observation_is_reported_as_collection_failure():
    scheduler = Scheduler(config(), lambda: StateSnapshot(gpus=(GPUState(0, free_gb=float("nan")),)))
    assert scheduler.sample_once().errors == ("collection_failed",)


def test_collection_io_does_not_hold_the_action_lock():
    entered, release = threading.Event(), threading.Event()
    def collect():
        entered.set()
        assert release.wait(2)
        return StateSnapshot()
    scheduler = Scheduler(config(), collect)
    thread = threading.Thread(target=scheduler.sample_once)
    thread.start()
    try:
        assert entered.wait(1)
        assert scheduler.action_lock.acquire(timeout=0.2)
        scheduler.action_lock.release()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_event_wait_releases_lock_and_wakes_on_publication():
    scheduler = Scheduler(config())
    result = []
    thread = threading.Thread(target=lambda: result.extend(scheduler.events_since(0, timeout=2)))
    thread.start()
    scheduler.emit("test", detail={"value": 1})
    thread.join(1)
    assert not thread.is_alive()
    assert result[0].kind == "test"
    result[0].detail["value"] = 99
    assert scheduler.events_since(0)[0].detail["value"] == 1


def test_history_is_bounded_and_stop_unblocks_waiters():
    scheduler = Scheduler(config(event_history_size=2))
    for _ in range(3):
        scheduler.emit("test")
    assert [event.id for event in scheduler.events_since(0)] == [2, 3]
    thread = threading.Thread(target=lambda: scheduler.events_since(3, timeout=30))
    thread.start()
    scheduler.stop()
    thread.join(1)
    assert not thread.is_alive()


def test_config_example_and_unknown_keys(tmp_path):
    example = load_config("tests/fixtures/core_scheduler.yaml")
    assert example.sample_interval_seconds == 15
    path = tmp_path / "bad.yaml"
    path.write_text("listen_host: 127.0.0.1\nlisten_port: 8011\nunknown: true\n")
    with pytest.raises(ValueError, match="keys"):
        load_config(str(path))


@pytest.mark.parametrize("change", [
    {"listen_host": "0.0.0.0"}, {"listen_host": "8.8.8.8"},
    {"listen_port": True}, {"listen_port": 0}, {"read_only": False},
    {"sample_interval_seconds": float("inf")}, {"sample_interval_seconds": 0},
    {"event_history_size": False}, {"collectors": []},
])
def test_invalid_config_is_rejected(change):
    with pytest.raises(ValueError):
        replace(config(), **change)


def test_collector_cannot_enable_actions_or_override_configured_memory():
    from llmsvc.state import MemoryState
    scheduler = Scheduler(config(memory_budget_gb=250), lambda: StateSnapshot(
        read_only=False, memory=MemoryState(host_available_gb=400, budget_gb=999),
    ))
    snapshot = scheduler.sample_once()
    assert snapshot.read_only is True
    assert snapshot.memory.budget_gb == 250
    assert snapshot.memory.host_available_gb == 400


def test_collector_factory_receives_only_probe_config(monkeypatch):
    import sys
    import types
    from llmsvc.__main__ import build_collector
    module = types.ModuleType("llmsvc.collectors")
    received = []
    def factory(options):
        received.append(options)
        return lambda: StateSnapshot()
    module.build_collector = factory
    monkeypatch.setitem(sys.modules, "llmsvc.collectors", module)
    collector = build_collector(config(collectors={"probe": "example"}))
    assert received == [{"probe": "example", "memory_budget_gb": 200.0, "host_min_available_gb": 150.0}]
    assert isinstance(collector(), StateSnapshot)
    assert build_collector(config()) is None
