# Generated-By: OpenCode / deepseek-v4.1-flash
"""Owned command-transition observations for explicit model actions (#247).

The transition is command intent only: it must be visible while an operation
waits, owned by exactly one operation, preserved across nested preload steps,
and always cleaned up on success, failure, dry-run and teardown.
"""

import threading
from dataclasses import replace

from llmsvc.actions import TransitionRegistry
from llmsvc.state import observed_phase, transition_name
from test_model_sleep_stop_preload import Transport, awake, setup


def stopped(name):
    return replace(awake(name), state="stopped", unit_active=False, health_ok=None,
                   is_sleeping=None, swap_state="stopped", resident_gb=None)


def sleeping(name):
    return replace(awake(name), state="sleeping", is_sleeping=True, resident_gb=2)


def model(service, name):
    return next(item for item in service.snapshot().models if item.name == name)


# --------------------------------------------------------------- pure helpers

def test_observed_phase_keeps_unknown_unknown():
    assert observed_phase("awake") == "GPU"
    assert observed_phase("sleeping") == "MEM"
    assert observed_phase("stopped") == "SSD"
    assert observed_phase("unknown") is None
    assert observed_phase(None) is None


def test_transition_name_covers_the_four_command_transitions_and_stop_variants():
    assert transition_name("stopped", "MEM") == "SSDtoMEM"
    assert transition_name("stopped", "GPU") == "SSDtoGPU"
    assert transition_name("sleeping", "GPU") == "MEMtoGPU"
    assert transition_name("awake", "MEM") == "GPUtoMEM"
    assert transition_name("awake", "SSD") == "GPUtoSSD"
    assert transition_name("sleeping", "SSD") == "MEMtoSSD"
    # No transition is claimed when the phase is unchanged or cannot be known.
    assert transition_name("awake", "GPU") is None
    assert transition_name("sleeping", "MEM") is None
    assert transition_name("unknown", "GPU") is None
    assert transition_name(None, "MEM") is None


# ------------------------------------------------------------------- registry

def test_registry_duplicate_owner_cannot_clear_an_active_transition():
    registry = TransitionRegistry()
    owner = registry.begin("m", "SSDtoMEM")
    duplicate = registry.begin("m", "GPUtoMEM")
    assert owner is not None and duplicate is not None and owner != duplicate
    # First owner wins; the nested target never overwrites the outer metadata.
    assert registry.snapshot() == {"m": "SSDtoMEM"}
    registry.end("m", duplicate)
    assert registry.snapshot() == {"m": "SSDtoMEM"}
    registry.end("m", owner)
    assert registry.snapshot() == {}
    registry.end("m", owner)  # Idempotent cleanup is harmless.
    assert registry.snapshot() == {}


def test_registry_none_transition_registers_nothing():
    registry = TransitionRegistry()
    assert registry.begin("m", None) is None
    assert registry.snapshot() == {}
    registry.end("m", None)
    assert registry.snapshot() == {}


def test_registry_concurrent_acquisition_leaves_no_leak_or_early_removal():
    registry = TransitionRegistry()
    started = threading.Barrier(8)
    results = []
    errors = []

    def worker():
        try:
            started.wait(timeout=5)
            token = registry.begin("m", "SSDtoGPU")
            results.append(token)
        except Exception as exc:  # pragma: no cover - failure reporting only
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    assert registry.snapshot() == {"m": "SSDtoGPU"}
    # Exactly one token owns the entry; ending all tokens clears it exactly once.
    for token in results:
        registry.end("m", token)
    assert registry.snapshot() == {}


# ---------------------------------------------------------------- integration

class RecordingTransport(Transport):
    """Records the snapshot seen at the observation boundary."""

    def __init__(self, backend):
        super().__init__(backend)
        self.seen = []

    def record(self, service, name):
        self.seen.append((model(service, name).transition, model(service, name).state))


def capture_wait(service, transport, name):
    def wait(action, deadline):
        transport.record(service, name)
        return service.snapshot(), True
    return wait


def test_sleep_exposes_gputomem_while_waiting_and_cleans_up(tmp_path):
    service, controller, backend = setup(tmp_path)
    transport = RecordingTransport(backend)
    controller.transport = transport
    controller.dispatcher.http_request = transport.http_request
    controller._wait_effect = capture_wait(service, transport, "a")
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "ready"
    assert transport.seen == [("GPUtoMEM", "awake")]
    assert model(service, "a").transition is None
    assert controller.transitions.snapshot() == {}


def test_stop_from_sleeping_exposes_memtossd_and_cleans_up(tmp_path):
    service, controller, backend = setup(tmp_path)
    backend.models["a"] = sleeping("a")
    service.sample_once()
    transport = RecordingTransport(backend)
    controller.transport = transport
    controller.dispatcher.http_request = transport.http_request
    controller.dispatcher.stop_unit = transport.stop_unit
    controller._wait_effect = capture_wait(service, transport, "a")
    result = controller.stop_model("a", by="caller")
    assert result["status"] == "ready"
    assert transport.seen == [("MEMtoSSD", "sleeping")]
    assert model(service, "a").transition is None


def test_wake_from_stopped_exposes_ssdtogpu_and_cleans_up(tmp_path):
    service, controller, backend = setup(tmp_path)
    backend.models["a"] = stopped("a")
    service.sample_once()

    class WakeRecordingTransport(Transport):
        def __init__(self, inner_backend):
            super().__init__(inner_backend)
            self.seen = []

        def http_request(self, method, path, *, deadline):
            if method == "GET":
                self.seen.append(model(service, "a").transition)
            return super().http_request(method, path, deadline=deadline)

    transport = WakeRecordingTransport(backend)
    controller.transport = transport
    result = controller.wake("a", by="caller")
    assert result["status"] == "ready"
    assert transport.seen and all(label == "SSDtoGPU" for label in transport.seen)
    assert model(service, "a").transition is None
    assert controller.transitions.snapshot() == {}


def test_preload_keeps_outer_ssdtomem_across_nested_wake_and_sleep(tmp_path):
    service, controller, backend = setup(tmp_path)
    backend.models["a"] = stopped("a")
    service.sample_once()
    transport = RecordingTransport(backend)
    controller.transport = transport
    records = []
    nested = []

    def fake_wake(name, *, by, _deadline, _transition):
        records.append((model(service, name).transition, _transition))
        return {"model": name, "status": "ready", "ready": True, "cold_start": True,
                "elapsed_seconds": 0.0}

    def fake_sleep(kind, name, *, by, started, deadline, _transition):
        records.append((model(service, name).transition, _transition))
        return {"model": name, "status": "ready", "error": None, "elapsed_seconds": 0.0,
                "state": "sleeping"}

    nested.append(fake_wake)
    nested.append(fake_sleep)
    controller.wake = fake_wake
    controller._model_action = fake_sleep
    result = controller.preload("a", by="caller")
    assert result["status"] == "ready"
    # Outer SSDtoMEM is visible for both nested steps and neither may own it.
    assert records == [("SSDtoMEM", False), ("SSDtoMEM", False)]
    assert model(service, "a").transition is None
    assert controller.transitions.snapshot() == {}


def test_preload_failure_still_releases_the_outer_transition(tmp_path):
    service, controller, backend = setup(tmp_path)
    backend.models["a"] = stopped("a")
    service.sample_once()
    seen = []

    def fake_wake(name, *, by, _deadline, _transition):
        seen.append(model(service, name).transition)
        return {"model": name, "status": "blocked", "ready": False,
                "error": "memory_budget", "elapsed_seconds": 0.0}

    controller.wake = fake_wake
    result = controller.preload("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "memory_budget"
    assert seen == ["SSDtoMEM"]
    assert model(service, "a").transition is None
    assert controller.transitions.snapshot() == {}


def test_dry_run_registers_no_transition_or_side_effect(tmp_path):
    service, controller, backend = setup(tmp_path)
    before = list(backend.calls)
    controller.sleep_model("a", by="caller", dry_run=True)
    controller.stop_model("a", by="caller", dry_run=True)
    controller.preload("a", by="caller", dry_run=True)
    controller.wake("a", by="caller", dry_run=True)
    assert backend.calls == before
    assert controller.transitions.snapshot() == {}
    assert model(service, "a").transition is None


def test_unstarted_action_does_not_register_a_transition(tmp_path):
    service, controller, backend = setup(tmp_path)
    # Already sleeping: an explicit sleep is a ready no-op with no transition.
    backend.models["a"] = sleeping("a")
    service.sample_once()
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "ready"
    assert controller.transitions.snapshot() == {}
    assert model(service, "a").transition is None
