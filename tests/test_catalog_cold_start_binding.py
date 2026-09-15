# Generated-By: OpenCode / deepseek-v4.1-flash
"""Catalog-built collectors must read the measured cold starts like the startup one.

v1.3.0 bound ``collector.cold_starts`` only on the collector built at startup;
``CatalogRuntime._construct`` then installed a fresh collector with a ``None``
source, so ``/v1/state`` reported null cold starts while the table held values.
"""

import threading
from types import SimpleNamespace

from llmsvc.activity import ActivityReader
from llmsvc.catalog import CatalogRuntime
from llmsvc.config import SchedulerConfig


class Store:
    def __init__(self):
        self.source = {"m": 42.0}

    def cold_starts(self):
        return dict(self.source)

    def catalog_checkpoint(self):
        return None


class Collectable:
    def __init__(self):
        self.cold_starts = None


def runtime_with(collector_factory):
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, catalog_enabled=True,
        state_db_path="state.sqlite",
        collectors={"swap_url": "http://127.0.0.1:1", "models": {"m": {}}})
    store = Store()
    action_lock = threading.RLock()
    scheduler = SimpleNamespace(config=config, action_lock=action_lock, store=store,
        catalog=None, catalog_epoch="0" * 32, model_actions=None, placement=None,
        collect=None, event_bridge=None, faults=None, automation=None,
        sleeping_recovery=None, monotonic=lambda: 0.0, changed=threading.Condition(action_lock),
        _unknown=lambda reason: None)
    queue = SimpleNamespace(action_lock=action_lock)
    runtime = CatalogRuntime(scheduler, queue, collector_factory=collector_factory,
        relay_factory=lambda config: None,
        transport_factory=lambda config, models: SimpleNamespace())
    return runtime, store


def manifest():
    return {"sources": {}, "active": {"m": {}}, "retained": {}}


def test_catalog_construct_binds_the_store_cold_start_source():
    runtime, store = runtime_with(lambda config: Collectable())
    collector, _, _ = runtime._construct(manifest())
    assert collector.cold_starts == store.cold_starts
    assert collector.cold_starts() == {"m": 42.0}


def test_catalog_construct_accepts_a_bare_callable_collector():
    runtime, _ = runtime_with(lambda config: (lambda: None))
    collector, _, _ = runtime._construct(manifest())
    assert callable(collector) and not hasattr(collector, "cold_starts")


class ReaderCollector:
    def __init__(self, reader):
        self.activity_reader = reader
        self.cold_starts = None


def test_catalog_publish_rebinds_the_usage_and_report_callables(tmp_path):
    reader = ActivityReader(tmp_path / "activity.sqlite")
    runtime, _ = runtime_with(lambda config: ReaderCollector(reader))
    collector, relay, transport = runtime._construct(manifest())

    with runtime.scheduler.action_lock:
        runtime._publish(manifest(), "1" * 32, (collector, relay, transport))

    scheduler = runtime.scheduler
    assert scheduler._usage is not None
    assert scheduler._usage_report is not None
    assert scheduler._usage_report is scheduler._usage.usage_report
