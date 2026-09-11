# Generated-By: Codex / gpt-5.6-luna
"""Deterministic Collector/Scheduler sampling races with bounded CPU probes."""

import threading
from types import SimpleNamespace

import pytest

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

    def gpus(self):
        return (GPUState(0, "fixture", 100, 1, 99),)

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
        return False

    def owner(self, process, units):
        return None


@pytest.fixture
def real_collector_scheduler():
    probes = GateProbes()
    collector = Collector(
        {"m": {"unit": "vllm-m.service", "daemon_url": "http://127.0.0.1:9001",
                "weights_gb": 10, "is_default": True}},
        swap_url="http://127.0.0.1:9000", probes=probes, deadline=1.8,
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
    assert probes.entered.wait(3)

    second = scheduler.sample_once()
    assert second == initial
    assert "collector: concurrent round" not in second.errors
    assert scheduler._sample_published == 1
    assert scheduler._sample_bounds[0] == 1
    assert len(reconcile_calls) == 0
    probes.release.set()
    first.join(3)

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


def test_stop_does_not_deadlock_on_a_bounded_collector_round(real_collector_scheduler):
    scheduler, collector, probes = real_collector_scheduler
    probes.hold_next = True
    sampling = threading.Thread(target=scheduler.sample_once)
    sampling.start()
    assert probes.entered.wait(3)

    stopping = threading.Thread(target=scheduler.stop)
    stopping.start()
    stopping.join(3)
    assert not stopping.is_alive()
    probes.release.set()
    sampling.join(3)
    assert not sampling.is_alive()
