# Generated-By: Codex / gpt-5.6-luna
"""Bounded startup sampling before bootstrap admission, using real Collector."""

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport
from llmsvc.bootstrap import BootstrapController, BootstrapError
from llmsvc.collectors import Collector
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController
from llmsvc.scheduler import Scheduler
from llmsvc.state import GPUState
from llmsvc.store import IntentStore


class StartupProbes:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_units = False

    def gpus(self):
        return (GPUState(0, "fixture", 100, 0, 100),)

    def processes(self):
        return {}

    def units(self):
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError("startup gate deadline")
        if self.fail_units:
            raise RuntimeError("fixture units failed")
        return {"vllm-default.service": {"model": "default", "unit_active": False,
                                          "gpu": None, "util": .4, "port": 8101}}

    def memory(self):
        return 500

    def running(self):
        return {"default": "stopped"}

    def events(self):
        return SimpleNamespace(states={"default": "stopped"}, count=lambda name: 0)

    def health(self, url):
        return False

    def sleeping(self, url):
        return None

    def owner(self, process, units):
        return None


def bootstrap_fixture(tmp_path):
    probes = StartupProbes()
    metadata = {"unit": "vllm-default.service", "port": 8101,
                "daemon_url": "http://127.0.0.1:8101", "util": .4,
                "budget_gb": 40, "weights_gb": 10, "is_default": True,
                "cold_start_seconds": 120}
    launcher = Path(__file__).parents[1] / "deploy/vllm-launch"
    launcher_config = tmp_path / "launcher.json"
    launcher_config.write_text(json.dumps({"scheduler_url": "http://127.0.0.1:8011"}))
    source_config = tmp_path / "source.yaml"
    source_config.write_text("hooks:\n  on_startup:\n    preload: [default]\n")
    spec = {
        "model": "default", "util": .4,
        "command": ["/bin/false", "--port", "8101", "--gpu-memory-utilization", ".4"],
        "launcher_path": str(launcher),
        "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "launcher_config_path": str(launcher_config),
        "launcher_config_sha256": hashlib.sha256(launcher_config.read_bytes()).hexdigest(),
        "migration_command": ["/bin/false"], "manifest_sha256": "a" * 64,
        "base_config_sha256": hashlib.sha256(source_config.read_bytes()).hexdigest(),
        "target_config_sha256": "c" * 64,
        "timeout_seconds": 1,
    }
    config = SchedulerConfig(
        "127.0.0.1", 8011, read_only=False, state_db_path=str(tmp_path / "state.sqlite"),
        bootstrap_enabled=True, placement_enabled=True, model_actions_enabled=True,
        catalog_enabled=True, catalog_mode="maintenance", bootstrap=spec,
        collectors={"swap_url": "http://127.0.0.1:8000", "models": {"default": metadata}},
        registry={"config_path": str(source_config), "shared_roots": [str(tmp_path)],
                  "daemon_port_range": [8101, 8199]},
    )
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    collector = Collector(
        {"default": metadata}, swap_url="http://127.0.0.1:8000", probes=probes,
        activity_reader=SimpleNamespace(
            read=lambda now: {"default": {"last_used": time.time() - 1000,
                                            "requests_last_hour": 0,
                                            "requests_last_10m": 0,
                                            "source_container": "fixture"}},
            last_error=None, last_error_code=""),
    )
    scheduler = Scheduler(config, collect=collector, store=store)
    transport = ManagedModelTransport(
        swap_url="http://127.0.0.1:8000", models={"default": metadata}, systemctl="/bin/false")
    scheduler.placement = PlacementController(scheduler, transport)
    calls = []

    class NoEffects:
        def request(self, operation, context, *, deadline):
            calls.append(operation)
            if operation == "bootstrap_preflight":
                return {"transaction_id": context["transaction_id"],
                        "manifest_sha256": context["manifest_sha256"],
                        "default_model": context["default_model"],
                        "default_unit": context["default_unit"],
                        "source_origin": context["source_origin"],
                        "observed_at": time.monotonic(), "ready": False}
            raise AssertionError("bootstrap effect backend must not be reached")

    controller = BootstrapController(scheduler, backend=NoEffects())
    return scheduler, store, collector, probes, controller, calls


def test_bootstrap_waits_for_running_first_sample_before_admission(tmp_path):
    scheduler, store, collector, probes, controller, calls = bootstrap_fixture(tmp_path)
    worker = threading.Thread(target=lambda: scheduler.start(sampling_only=True))
    worker.start()
    attempt = None
    try:
        assert probes.entered.wait(2)
        result = {}
        waiter_entered = threading.Event()
        original_wait = scheduler.await_initial_sample

        def wait_with_receipt(deadline):
            waiter_entered.set()
            return original_wait(deadline)

        scheduler.await_initial_sample = wait_with_receipt

        def run_bootstrap():
            try:
                controller.run()
            except BootstrapError as exc:
                result["error"] = str(exc)

        attempt = threading.Thread(target=run_bootstrap)
        attempt.start()
        assert waiter_entered.wait(2)
        assert attempt.is_alive()
        assert store.bootstrap_checkpoint() is None and calls == []
        probes.release.set()
        attempt.join(3)
        assert not attempt.is_alive()
        assert result["error"] == "bootstrap preflight is unknown or busy"
        assert scheduler.snapshot().sampled_at is not None
        assert scheduler.snapshot().errors == ()
        assert store.bootstrap_checkpoint() is None and calls == ["bootstrap_preflight"]
    finally:
        probes.release.set()
        if attempt is not None:
            attempt.join(3)
            assert not attempt.is_alive()
        scheduler.stop()
        worker.join(3)
        assert not worker.is_alive()
        store.close()


def test_bootstrap_initial_probe_failure_stays_unknown_before_claim(tmp_path):
    scheduler, store, collector, probes, controller, calls = bootstrap_fixture(tmp_path)
    probes.fail_units = True
    worker = threading.Thread(target=lambda: scheduler.start(sampling_only=True))
    worker.start()
    try:
        assert probes.entered.wait(2)
        probes.release.set()
        deadline = time.monotonic() + 2
        while scheduler.snapshot().sampled_at is None and time.monotonic() < deadline:
            time.sleep(.01)
        with pytest.raises(BootstrapError, match="initial placement is not admissible"):
            controller.run()
        assert scheduler.snapshot().errors
        assert store.bootstrap_checkpoint() is None and calls == []
    finally:
        probes.release.set()
        scheduler.stop()
        worker.join(3)
        assert not worker.is_alive()
        store.close()


def test_direct_initial_wait_resamples_after_transient_published_failure(tmp_path):
    scheduler, store, collector, probes, controller, calls = bootstrap_fixture(tmp_path)
    probes.release.set()
    calls_count = {"value": 0}

    def transient_then_clean():
        calls_count["value"] += 1
        if calls_count["value"] == 1:
            raise OSError("transient fixture probe failure")
        return collector.collect()

    scheduler.collect = transient_then_clean
    try:
        first = scheduler.sample_once()
        assert first.sampled_at is None and "collection_failed" in first.errors
        clean = scheduler.await_initial_sample(time.monotonic() + 1)
        assert calls_count["value"] == 2
        assert clean.errors == () and clean.models
    finally:
        scheduler.stop()
        store.close()


def test_bootstrap_stop_during_initial_wait_keeps_claim_and_effects_empty(tmp_path):
    scheduler, store, collector, probes, controller, calls = bootstrap_fixture(tmp_path)
    worker = threading.Thread(target=lambda: scheduler.start(sampling_only=True))
    worker.start()
    attempt = None
    stopper = None
    try:
        assert probes.entered.wait(2)
        waiter_entered = threading.Event()
        original_wait = scheduler.await_initial_sample

        def wait_with_receipt(deadline):
            waiter_entered.set()
            return original_wait(deadline)

        scheduler.await_initial_sample = wait_with_receipt
        result = {}

        def run_bootstrap():
            try:
                controller.run()
            except BootstrapError as exc:
                result["error"] = str(exc)

        attempt = threading.Thread(target=run_bootstrap)
        attempt.start()
        assert waiter_entered.wait(2)
        stopper = threading.Thread(target=scheduler.stop)
        stopper.start()
        assert scheduler.stopping.wait(2)
        probes.release.set()
        attempt.join(3)
        stopper.join(3)
        assert not attempt.is_alive() and not stopper.is_alive()
        assert result["error"] == "bootstrap is disabled or stopping"
        assert store.bootstrap_checkpoint() is None and calls == []
    finally:
        probes.release.set()
        if attempt is not None:
            attempt.join(3)
        if stopper is not None:
            stopper.join(3)
        if not scheduler.stopping.is_set():
            scheduler.stop()
        worker.join(3)
        assert not worker.is_alive()
        store.close()


@pytest.mark.parametrize("reason", ["expired", "stopping"])
def test_initial_wait_does_not_start_new_io_after_expiry_or_shutdown(tmp_path, reason):
    scheduler, store, collector, probes, controller, calls = bootstrap_fixture(tmp_path)
    calls_count = {"value": 0}
    original = scheduler.collect

    def counted_collect():
        calls_count["value"] += 1
        return original()

    scheduler.collect = counted_collect
    try:
        if reason == "stopping":
            scheduler.stopping.set()
            deadline = time.monotonic() + 1
        else:
            deadline = time.monotonic() - 1
        scheduler.await_initial_sample(deadline)
        assert calls_count["value"] == 0
    finally:
        probes.release.set()
        scheduler.stop()
        store.close()
