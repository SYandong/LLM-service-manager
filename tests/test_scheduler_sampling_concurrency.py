# Generated-By: Codex / gpt-5.6-luna
"""Deterministic Collector/Scheduler sampling races with bounded CPU probes."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.collectors import Collector
from llmsvc.scheduler import Scheduler
from llmsvc.state import GPUState, MemoryState, StateSnapshot
from llmsvc.config import SchedulerConfig


class GateProbes:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.hold_next = False
        self.sleeping_state = False
        self.free_gb = 99

    def gpus(self):
        return (GPUState(0, "fixture", 100, 100 - self.free_gb, self.free_gb),)

    def processes(self):
        return {}

    def units(self):
        self.calls += 1
        if self.hold_next:
            self.hold_next = False
            self.entered.set()
            assert self.release.wait(3)
        return {"vllm-m.service": {"model": "m", "unit_active": True,
                                   "gpu": 0, "util": .4, "port": 9001}}

    def memory(self):
        return 500

    def running(self):
        return {"m": "ready"}

    def events(self):
        return SimpleNamespace(states={"m": "ready"}, count=lambda name: 0)

    def health(self, url):
        return True

    def sleeping(self, url):
        return self.sleeping_state

    def owner(self, process, units):
        return None


@pytest.fixture
def real_collector_scheduler():
    probes = GateProbes()
    activity_reader = SimpleNamespace(read=lambda now: {
        "m": {"last_used": time.time() - 1000, "requests_last_hour": 0,
               "requests_last_10m": 0, "source_container": "fixture"}},
                                      last_error=None,
                                      last_error_code="")
    collector = Collector(
        {"m": {"unit": "vllm-m.service", "daemon_url": "http://127.0.0.1:9001",
                "weights_gb": 10, "cold_start_seconds": 120, "is_default": False}},
        swap_url="http://127.0.0.1:9000", probes=probes,
        activity_reader=activity_reader, deadline=1.8,
    )
    scheduler = Scheduler(SchedulerConfig("127.0.0.1", 8011), collector)
    try:
        yield scheduler, collector, probes
    finally:
        scheduler.stop()


def test_concurrent_collector_round_does_not_publish_busy_snapshot(real_collector_scheduler):
    scheduler, collector, probes = real_collector_scheduler
    initial = scheduler.sample_once()
    assert initial.models and scheduler._sample_published == 1
    reconcile_calls = []
    scheduler.placement = SimpleNamespace(reconcile=lambda: reconcile_calls.append(True))
    probes.entered.clear()
    probes.release.clear()
    probes.hold_next = True
    first = threading.Thread(target=scheduler.sample_once)
    first.start()
    try:
        assert probes.entered.wait(3)

        second = scheduler.sample_once()
        assert second == initial
        assert "collector: concurrent round" not in second.errors
        assert scheduler._sample_published == 1
        assert scheduler._sample_bounds[0] == 1
        assert len(reconcile_calls) == 0
    finally:
        probes.release.set()
        first.join(3)
        assert not first.is_alive()

    published = scheduler.snapshot()
    assert published.models and "collector: concurrent round" not in published.errors
    assert published.sampled_at != initial.sampled_at
    assert scheduler._sample_published == 2
    assert scheduler._sample_bounds[0] == 2
    assert len(reconcile_calls) == 1


def test_real_collector_failure_publishes_unknown(real_collector_scheduler):
    scheduler, collector, probes = real_collector_scheduler
    probes.release.set()
    scheduler.sample_once()
    scheduler.collect = lambda: (_ for _ in ()).throw(OSError("fixture probe failed"))
    failed = scheduler.sample_once()
    assert failed.models == ()
    assert "collection_failed" in failed.errors


def test_mixed_collector_errors_are_not_treated_as_contention(real_collector_scheduler):
    scheduler, collector, probes = real_collector_scheduler
    initial = scheduler.sample_once()

    scheduler.collect = lambda: replace(
        initial, sampled_at=time.time(),
        errors=("collector: concurrent round", "memory: fixture probe failed"),
    )
    mixed = scheduler.sample_once()

    assert mixed is not initial
    assert mixed.errors == ("collector: concurrent round", "memory: fixture probe failed")
    assert scheduler._sample_published == 2
    assert mixed.sampled_at != initial.sampled_at


def test_stop_does_not_deadlock_on_a_bounded_collector_round(real_collector_scheduler):
    scheduler, collector, probes = real_collector_scheduler
    probes.hold_next = True
    sampling = threading.Thread(target=scheduler.sample_once)
    sampling.start()
    stopping = threading.Thread(target=scheduler.stop)
    try:
        assert probes.entered.wait(3)
        stopping.start()
        stopping.join(3)
        assert not stopping.is_alive()
    finally:
        probes.release.set()
        sampling.join(3)
        if stopping.is_alive():
            stopping.join(3)
        assert not sampling.is_alive()
        assert not stopping.is_alive()


def test_action_refresh_needs_a_new_sample_after_reused_post_effect_observation(real_collector_scheduler, tmp_path):
    scheduler, collector, probes = real_collector_scheduler
    scheduler.config = replace(
        scheduler.config, read_only=False, model_actions_enabled=True,
        state_db_path=str(tmp_path / "action.sqlite"), free_timeout_seconds=1,
        action_observe_seconds=.8, action_poll_seconds=.005,
    )
    scheduler.sample_once()
    transport = ManagedModelTransport(
        swap_url="http://127.0.0.1:9000", systemctl="fixture",
        models={"m": {"unit": "vllm-m.service", "is_default": False}},
        run=lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    busy_events = [threading.Event(), threading.Event()]
    busy_count = [0]
    busy_count_lock = threading.Lock()
    post_effect_published = threading.Event()
    first_hold = threading.Event()
    second_hold = threading.Event()
    orchestrator_errors = []
    orchestrator_threads = []
    original_collect = collector.collect

    def collect_with_receipt():
        result = original_collect()
        if "collector: concurrent round" in result.errors:
            with busy_count_lock:
                index = busy_count[0]
                busy_count[0] += 1
            if index < len(busy_events):
                busy_events[index].set()
        return result

    scheduler.collect = collect_with_receipt
    original_emit = scheduler.emit

    def emit_with_receipt(kind, **kwargs):
        event = original_emit(kind, **kwargs)
        if kind == "state" and scheduler._sample_published >= 2:
            post_effect_published.set()
        return event

    scheduler.emit = emit_with_receipt

    def hold_collector_for_action_refresh():
        try:
            with collector.lock:
                first_hold.set()
                if not busy_events[0].wait(3):
                    orchestrator_errors.append("first collector contention was not observed")
            scheduler.sample_once()  # Genuine post-effect observation.
            if not post_effect_published.wait(3):
                orchestrator_errors.append("post-effect sample was not published")
            with collector.lock:
                second_hold.set()
                if not busy_events[1].wait(3):
                    orchestrator_errors.append("second collector contention was not observed")
        except Exception as exc:
            orchestrator_errors.append(type(exc).__name__ + ": " + str(exc))

    def action_request(method, path, *, deadline):
        assert method == "POST" and path.endswith("/m")
        probes.sleeping_state = True
        probes.free_gb += 30
        thread = threading.Thread(target=hold_collector_for_action_refresh, daemon=True)
        orchestrator_threads.append(thread)
        thread.start()
        assert first_hold.wait(3)
        return 200

    transport.http_request = action_request
    controller = ModelActionController(scheduler, transport)
    scheduler.model_actions = controller
    try:
        result = controller.free({"need_gb": 10}, by="fixture")
        assert result["status"] == "complete", result
        assert result["freed_gb"] == 30
        assert not orchestrator_errors
        assert busy_count[0] >= 2
        assert second_hold.is_set()
        assert scheduler._sample_published >= 5
    finally:
        scheduler.stopping.set()
        for thread in orchestrator_threads:
            thread.join(3)
            assert not thread.is_alive()
