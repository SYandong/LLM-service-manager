# Generated-By: Claude Code / claude-fable-5-1
"""Explicit per-model sleep/stop/preload tests against a disposable backend."""

import threading
import time
from dataclasses import replace

import pytest

from llmsvc.actions import ActionDispatchError, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot


def awake(name, **overrides):
    model = ModelState(name, state="awake", gpu=0, unit="vllm-" + name + ".service", unit_active=True,
                       health_ok=True, is_sleeping=False, swap_state="ready", weights_gb=40,
                       budget_gb=80, resident_gb=80, cold_start_seconds=120)
    return replace(model, **overrides)


class Backend:
    """Simulated observations only; a call never proves a state transition."""

    def __init__(self, names=("a", "b")):
        self.models = {name: awake(name) for name in names}
        self.free = {0: 20.0}
        self.available = 500.0
        self.pins = ()
        self.errors = ()
        self.inflight = {}
        self.lock = threading.Lock()
        self.calls = []
        self.apply_actions = True
        self.fail_stop = False

    def collect(self):
        with self.lock:
            sleeping = sum(m.weights_gb for m in self.models.values() if m.state == "sleeping")
            return StateSnapshot(
                sampled_at=time.time(), models=tuple(self.models.values()), pins=self.pins,
                gpus=tuple(GPUState(index, total_gb=200, used_gb=200 - free, free_gb=free, external_gb=0)
                           for index, free in self.free.items()),
                memory=MemoryState(self.available, sleeping),  # Core overrides budget/floor from config.
                activity=tuple(Activity(name, time.time() - 1000, 0, 0, self.inflight.get(name, 0), ("container",))
                               for name in self.models),
                errors=self.errors)

    def apply(self, kind, name):
        with self.lock:
            model = self.models[name]
            if kind == "sleep":
                self.models[name] = replace(model, state="sleeping", is_sleeping=True, resident_gb=2)
                self.free[model.gpu] += 30
                self.available -= model.weights_gb
            elif kind == "stop":
                self.models[name] = replace(model, state="stopped", unit_active=False, health_ok=None,
                                            is_sleeping=None, resident_gb=None, swap_state="stopped")
                self.free[model.gpu] += 2 if model.state == "sleeping" else 30
                if model.state == "sleeping":
                    self.available += model.weights_gb
            else:
                self.models[name] = replace(model, state="awake", gpu=0, unit_active=True, health_ok=True,
                                            is_sleeping=False, resident_gb=80, swap_state="ready")
                if model.state == "sleeping":
                    self.free[0] -= 30
                    self.available += model.weights_gb
                else:
                    self.free[0] -= 80


class Transport:
    def __init__(self, backend):
        self.backend = backend
        self.models = {name: {} for name in backend.models}

    def unit_for_model(self, name):
        if name not in self.models:
            raise ActionDispatchError("unmanaged_model")
        return "vllm-" + name + ".service"

    def http_request(self, method, path, *, deadline):
        from urllib.parse import unquote
        if method == "POST":
            name = unquote(path.rsplit("/", 1)[1])
            self.backend.calls.append(("sleep", name))
            if self.backend.apply_actions:
                self.backend.apply("sleep", name)
            return 200
        name = unquote(path[len("/upstream/"):-1])
        self.backend.calls.append(("wake", name))
        if self.backend.apply_actions:
            self.backend.apply("wake", name)
        return 404

    def stop_unit(self, unit, *, deadline):
        name = unit[len("vllm-"):-len(".service")]
        self.backend.calls.append(("stop", name))
        if self.backend.fail_stop:
            return 1
        if self.backend.apply_actions:
            self.backend.apply("stop", name)
        return 0


def setup(tmp_path, backend=None, **overrides):
    backend = backend or Backend()
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
                             state_db_path=str(tmp_path / "unused.sqlite"), free_timeout_seconds=1,
                             wake_timeout_seconds=1, action_observe_seconds=0.3, action_poll_seconds=0.005,
                             **overrides)
    service = Scheduler(config, backend.collect)
    controller = ModelActionController(service, Transport(backend))
    service.model_actions = controller
    service.sample_once()
    return service, controller, backend


def kinds(service, *names):
    return [(event.kind, event.model) for event in service.events_since(0) if event.kind in names]


def test_sleep_moves_one_awake_model_and_confirms_the_observed_state(tmp_path):
    service, controller, backend = setup(tmp_path)
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "ready" and result["error"] is None
    assert result["model"] == "a" and result["state"] == "sleeping"
    assert backend.calls == [("sleep", "a")]
    assert backend.models["b"].state == "awake"  # Only the requested model moves.
    assert not controller.pending
    assert kinds(service, "sleep_requested", "sleep_result") == [("sleep_requested", "a"), ("sleep_result", "a")]


def test_sleeping_model_is_ready_without_a_second_unload(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="sleeping", is_sleeping=True, resident_gb=2)
    service, controller, backend = setup(tmp_path, backend)
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "ready" and result["state"] == "sleeping" and result["error"] is None
    assert backend.calls == []
    assert kinds(service, "sleep_requested") == []


def test_sleep_of_a_stopped_model_reports_model_not_resident(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="stopped", unit_active=False, health_ok=None,
                                  is_sleeping=None, resident_gb=None, swap_state="stopped")
    service, controller, backend = setup(tmp_path, backend)
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "model_not_resident"
    assert result["state"] == "stopped" and backend.calls == []


@pytest.mark.parametrize("operation", ["sleep_model", "stop_model"])
def test_in_flight_requests_block_both_explicit_transitions(tmp_path, operation):
    backend = Backend()
    backend.inflight = {"a": 2}
    service, controller, backend = setup(tmp_path, backend)
    result = getattr(controller, operation)("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "in_flight"
    assert backend.calls == []


@pytest.mark.parametrize("operation", ["sleep_model", "stop_model"])
def test_pinned_models_are_never_slept_or_stopped(tmp_path, operation):
    backend = Backend()
    backend.pins = (Pin("a", time.time() + 600, "owner"),)
    service, controller, backend = setup(tmp_path, backend)
    result = getattr(controller, operation)("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "pinned_until"
    assert backend.calls == []


def test_sleep_admission_blocks_instead_of_stopping_another_sleeping_model(tmp_path):
    backend = Backend()
    backend.models["b"] = replace(backend.models["b"], state="sleeping", is_sleeping=True, resident_gb=2)
    backend.available = 170.0  # 170 - 40 < the 150 GiB host floor.
    service, controller, backend = setup(tmp_path, backend)
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "memory_budget"
    assert backend.calls == []
    assert backend.models["b"].state == "sleeping"  # The other sleeper is untouched.


def test_stop_takes_an_awake_model_out_of_service(tmp_path):
    service, controller, backend = setup(tmp_path)
    result = controller.stop_model("a", by="caller")
    assert result["status"] == "ready" and result["state"] == "stopped" and result["error"] is None
    assert backend.calls == [("stop", "a")]
    assert kinds(service, "stop_requested", "stop_result") == [("stop_requested", "a"), ("stop_result", "a")]


def test_stop_of_a_sleeping_model_releases_its_resident_weights(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="sleeping", is_sleeping=True, resident_gb=2)
    backend.available = 460.0
    service, controller, backend = setup(tmp_path, backend)
    result = controller.stop_model("a", by="caller")
    assert result["status"] == "ready" and result["state"] == "stopped"
    assert backend.calls == [("stop", "a")]


def test_default_model_is_never_stopped_by_an_explicit_request(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], is_default=True)
    service, controller, backend = setup(tmp_path, backend)
    result = controller.stop_model("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "default_model"
    assert backend.calls == []
    # The same default model may still be slept (DESIGN §4 protection table).
    assert controller.sleep_model("a", by="caller")["status"] == "ready"


def test_stopped_model_is_ready_without_a_second_systemctl_stop(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="stopped", unit_active=False, health_ok=None,
                                  is_sleeping=None, resident_gb=None, swap_state="stopped")
    service, controller, backend = setup(tmp_path, backend)
    result = controller.stop_model("a", by="caller")
    assert result["status"] == "ready" and result["state"] == "stopped"
    assert backend.calls == []


def test_rejected_stop_request_is_failed_rather_than_an_assumed_exit(tmp_path):
    backend = Backend()
    backend.fail_stop = True
    service, controller, backend = setup(tmp_path, backend)
    result = controller.stop_model("a", by="caller")
    assert result["status"] == "failed" and result["error"] == "transport_rejected"
    assert result["state"] == "awake" and backend.models["a"].state == "awake"
    assert not controller.pending


def test_preload_cold_starts_then_sleeps_the_new_daemon(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="stopped", gpu=None, unit_active=False,
                                  health_ok=None, is_sleeping=None, resident_gb=None, swap_state="stopped")
    backend.free = {0: 120.0}
    service, controller, backend = setup(tmp_path, backend)
    result = controller.preload("a", by="caller")
    assert result["status"] == "ready" and result["error"] is None
    assert result["state"] == "sleeping" and result["already_resident"] is False
    assert backend.calls == [("wake", "a"), ("sleep", "a")]
    assert not controller.pending
    assert kinds(service, "preload_requested", "preload_result") == [
        ("preload_requested", "a"), ("preload_result", "a")]


@pytest.mark.parametrize("state,overrides", [
    ("awake", {}),
    ("sleeping", {"state": "sleeping", "is_sleeping": True, "resident_gb": 2}),
])
def test_preload_never_touches_a_model_whose_weights_are_resident(tmp_path, state, overrides):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], **overrides)
    service, controller, backend = setup(tmp_path, backend)
    result = controller.preload("a", by="caller")
    assert result["status"] == "ready" and result["already_resident"] is True
    assert result["state"] == state and backend.calls == []


def test_preload_reports_partial_when_the_cold_start_cannot_be_slept(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="stopped", gpu=None, unit_active=False,
                                  health_ok=None, is_sleeping=None, resident_gb=None, swap_state="stopped")
    backend.models["b"] = replace(backend.models["b"], state="sleeping", is_sleeping=True, resident_gb=2)
    backend.free = {0: 120.0}
    # The already sleeping 40 GiB leaves no resident-weight budget for a second.
    service, controller, backend = setup(tmp_path, backend, memory_budget_gb=60.0)
    result = controller.preload("a", by="caller")
    assert result["status"] == "partial" and result["error"] == "memory_budget"
    assert result["state"] == "awake" and backend.calls == [("wake", "a")]
    assert backend.models["b"].state == "sleeping"


@pytest.mark.parametrize("operation,expected", [
    ("sleep", [{"kind": "sleep", "model": "a", "reason": "user_sleep", "gpu": 0}]),
    ("stop", [{"kind": "stop", "model": "a", "reason": "user_stop", "gpu": 0}]),
])
def test_dry_run_previews_the_planned_action_without_collecting_or_dispatching(tmp_path, operation, expected):
    service, controller, backend = setup(tmp_path)
    before = service.events_since(0)
    result = getattr(controller, operation + "_model")("a", by="caller", dry_run=True)
    assert result == {"would": expected, "blocked_by": []}
    assert backend.calls == [] and service.events_since(0) == before


def test_preload_dry_run_previews_the_cold_start_and_the_following_sleep(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="stopped", gpu=None, unit_active=False,
                                  health_ok=None, is_sleeping=None, resident_gb=None, swap_state="stopped")
    backend.free = {0: 120.0}
    service, controller, backend = setup(tmp_path, backend)
    result = controller.preload("a", by="caller", dry_run=True)
    assert result == {"would": [{"kind": "wake", "model": "a", "reason": "user_preload", "gpu": None},
                                {"kind": "sleep", "model": "a", "reason": "user_preload", "gpu": None}],
                      "blocked_by": []}
    assert backend.calls == []


def test_dry_run_reports_the_protection_blocker_without_acting(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], is_default=True)
    service, controller, backend = setup(tmp_path, backend)
    result = controller.stop_model("a", by="caller", dry_run=True)
    assert result["would"] == []
    assert [item["reason"] for item in result["blocked_by"]] == ["default_model"]
    assert backend.calls == []


def test_a_cold_start_in_progress_excludes_a_concurrent_sleep_of_the_same_model(tmp_path):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="stopped", gpu=None, unit_active=False,
                                  health_ok=None, is_sleeping=None, resident_gb=None, swap_state="stopped")
    backend.free = {0: 120.0}
    service, controller, backend = setup(tmp_path, backend)
    service.config = replace(service.config, wake_timeout_seconds=5)
    preloaded = []
    waiting = threading.Event()
    release = threading.Event()
    original = controller.transport.http_request

    def paused_wake(method, path, *, deadline):
        if method == "GET":
            waiting.set()
            release.wait(5)
        return original(method, path, deadline=deadline)

    controller.transport.http_request = paused_wake
    worker = threading.Thread(target=lambda: preloaded.append(controller.preload("a", by="one")))
    worker.start()
    try:
        assert waiting.wait(5)
        # The upstream request runs without the action lock, so this observes the
        # pending set rather than merely blocking on the lock.
        second = controller.sleep_model("a", by="two")
    finally:
        release.set()
        worker.join(10)
    assert second["status"] == "blocked" and second["error"] == "operation_in_progress"
    assert preloaded[0]["status"] == "ready" and preloaded[0]["state"] == "sleeping"
    assert backend.calls == [("wake", "a"), ("sleep", "a")]
    assert not controller.pending


def test_disabled_model_actions_block_every_explicit_transition(tmp_path):
    service, controller, backend = setup(tmp_path)
    service.config = replace(service.config, model_actions_enabled=False)
    for operation in ("sleep_model", "stop_model", "preload"):
        result = getattr(controller, operation)("a", by="caller")
        assert result["status"] == "blocked" and result["error"] == "operation_not_enabled"
    assert backend.calls == []


def test_unmanaged_models_are_never_explicit_action_targets(tmp_path):
    service, controller, backend = setup(tmp_path)
    controller.transport.models.pop("b")
    for operation in ("sleep_model", "stop_model", "preload"):
        result = getattr(controller, operation)("b", by="caller")
        assert result["status"] == "blocked" and result["error"] == "model_retired"
    assert backend.calls == []


def test_stale_observations_block_instead_of_assuming_the_last_known_state(tmp_path):
    service, controller, backend = setup(tmp_path)
    backend.errors = ("collector_failed",)
    result = controller.sleep_model("a", by="caller")
    assert result["status"] == "blocked" and result["error"] == "unknown_or_stale_snapshot"
    assert backend.calls == []
