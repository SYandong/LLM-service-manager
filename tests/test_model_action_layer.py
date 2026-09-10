# Generated-By: Codex / gpt-6-astra
"""Observation-driven free/wake tests, with only disposable simulated backends."""

import threading
import time
from dataclasses import replace

import pytest

from llmsvc.actions import ActionDispatchError, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot


class Backend:
    def __init__(self, names=("a", "b")):
        self.models = {name: ModelState(name, state="awake", gpu=0, unit="vllm-" + name + ".service",
            unit_active=True, health_ok=True, is_sleeping=False, swap_state="ready",
            weights_gb=40, budget_gb=80, resident_gb=80, cold_start_seconds=120) for name in names}
        self.free = {0: 20.0}
        self.available = 500.0
        self.pins = ()
        self.errors = ()
        self.inflight = {}
        self.lock = threading.Lock()
        self.samples = 0
        self.calls = []
        self.release = 30.0
        self.fail_model = None
        self.apply_fail = False
        self.apply_actions = True
        self.after_apply = None

    def collect(self):
        with self.lock:
            self.samples += 1
            return StateSnapshot(sampled_at=time.time(), models=tuple(self.models.values()), pins=self.pins,
                gpus=tuple(GPUState(index, total_gb=200, used_gb=200-free if free is not None else None,
                                   free_gb=free, external_gb=0) for index, free in self.free.items()),
                memory=MemoryState(self.available, sum(m.weights_gb for m in self.models.values() if m.state == "sleeping")),
                activity=tuple(Activity(name, time.time()-1000, 0, 0, self.inflight.get(name, 0), ("container",)) for name in self.models),
                errors=self.errors)

    def apply(self, kind, name):
        with self.lock:
            model = self.models[name]
            if kind == "sleep":
                self.models[name] = replace(model, state="sleeping", is_sleeping=True, resident_gb=2)
                self.free[model.gpu] += self.release
                self.available -= model.weights_gb
            else:
                self.models[name] = replace(model, state="stopped", unit_active=False, health_ok=None,
                                            is_sleeping=None, resident_gb=None, swap_state="stopped")
                self.free[model.gpu] += 2 if model.state == "sleeping" else self.release
                if model.state == "sleeping":
                    self.available += model.weights_gb
            if self.after_apply:
                self.after_apply(kind, name)


class Transport:
    def __init__(self, backend, names=None):
        self.backend = backend
        self.models = {name: {} for name in (names if names is not None else backend.models)}

    def unit_for_model(self, name):
        if name not in self.models:
            raise ActionDispatchError("unmanaged_model")
        return "vllm-" + name + ".service"

    def http_request(self, method, path, *, deadline):
        from urllib.parse import unquote
        assert method == "POST" and path.startswith("/api/models/unload/")
        name = unquote(path.rsplit("/", 1)[1])
        self.backend.calls.append(("sleep", name))
        failed = name == self.backend.fail_model
        if self.backend.apply_actions and (not failed or self.backend.apply_fail):
            self.backend.apply("sleep", name)
        return 500 if failed else 200

    def stop_unit(self, unit, *, deadline):
        name = unit[len("vllm-"):-len(".service")]
        self.backend.calls.append(("stop", name))
        if self.backend.apply_actions:
            self.backend.apply("stop", name)
        return 0


def setup(tmp_path, backend=None, names=None):
    backend = backend or Backend()
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
        state_db_path=str(tmp_path/"unused.sqlite"), free_timeout_seconds=0.5, wake_timeout_seconds=0.3,
        action_observe_seconds=0.06, action_poll_seconds=0.005)
    service = Scheduler(config, backend.collect)
    transport = Transport(backend, names)
    controller = ModelActionController(service, transport)
    service.model_actions = controller
    service.sample_once()
    return service, controller, backend


def test_measured_release_is_not_the_policy_residency_estimate(tmp_path):
    service, controller, backend = setup(tmp_path)
    result = controller.free({"need_gb": 25}, by="caller")
    assert backend.calls == [("sleep", "a")]
    assert result["freed_gb"] == 30  # Policy estimated 80 - 2 = 78.
    assert result["status"] == "complete"
    assert result["slept"] == ["a"] and result["stopped"] == []
    assert result["measured_at"] is not None
    assert result["measurement"] == "net_gpu_free_gib"
    assert not service.model_actions.pending


def test_replans_after_confirmed_action_and_respects_new_pin(tmp_path):
    service, controller, backend = setup(tmp_path)
    def pin_next(kind, name):
        backend.pins = (Pin("b", time.time()+100, "new-owner"),)
    backend.after_apply = pin_next
    result = controller.free({"need_gb": 80}, by="caller")
    assert backend.calls == [("sleep", "a")]
    assert result["freed_gb"] == 30 and result["status"] == "partial"
    assert any(item["model"] == "b" and item["reason"] == "pinned_until" for item in result["skipped"])
    assert service.snapshot().pins[0].model == "b"


def test_failed_second_action_preserves_measured_partial_and_stops(tmp_path):
    _, controller, backend = setup(tmp_path)
    backend.fail_model = "b"
    result = controller.free({}, by="caller")
    assert backend.calls == [("sleep", "a"), ("sleep", "b")]
    assert result["slept"] == ["a"] and result["freed_gb"] == 30
    assert result["status"] == "partial" and result["error"] == "transport_rejected"
    assert result["measurement_complete"] is False
    assert backend.samples >= 4


def test_failed_request_with_observed_effect_preserves_it_without_more_actions(tmp_path):
    _, controller, backend = setup(tmp_path)
    backend.fail_model = "a"
    backend.apply_fail = True
    result = controller.free({}, by="caller")
    assert backend.calls == [("sleep", "a")]
    assert result["status"] == "partial" and result["slept"] == ["a"]
    assert result["freed_gb"] == 30


def test_acknowledgement_without_effect_is_no_progress(tmp_path):
    _, controller, backend = setup(tmp_path)
    backend.apply_actions = False
    result = controller.free({}, by="caller")
    assert backend.calls == [("sleep", "a")]
    assert result["status"] == "no_progress" and result["error"] == "effect_not_confirmed"
    assert result["slept"] == [] and result["freed_gb"] == 0
    assert result["measurement_complete"] is False


def test_observed_sleep_without_net_release_stops_instead_of_sleeping_everything(tmp_path):
    _, controller, backend = setup(tmp_path)
    backend.release = 0
    result = controller.free({}, by="caller")
    assert backend.calls == [("sleep", "a")]
    assert result["status"] == "no_progress" and result["error"] == "no_measured_release"
    assert result["slept"] == ["a"] and result["freed_gb"] == 0


def test_unmanaged_models_are_never_action_targets(tmp_path):
    _, controller, backend = setup(tmp_path, names=("b",))
    result = controller.free({}, by="caller")
    assert backend.calls == [("sleep", "b")]
    assert any(item["model"] == "a" and item["reason"] == "unmanaged_model" for item in result["skipped"])


def test_ram_admission_stop_on_other_gpu_can_precede_target_sleep(tmp_path):
    backend = Backend(("a", "old"))
    backend.models["old"] = replace(backend.models["old"], state="sleeping", is_sleeping=True, gpu=1, resident_gb=2)
    backend.free[1] = 100
    backend.available = 170
    _, controller, backend = setup(tmp_path, backend)
    result = controller.free({"gpu": 0, "need_gb": 25}, by="caller")
    assert backend.calls == [("stop", "old"), ("sleep", "a")]
    assert result["stopped"] == ["old"] and result["slept"] == ["a"]
    assert result["freed_gb"] == 30 and result["status"] == "complete"


def test_ram_free_uses_observed_host_difference_not_weights(tmp_path):
    backend = Backend(("a",))
    backend.models["a"] = replace(backend.models["a"], state="sleeping", is_sleeping=True, resident_gb=2)
    def concurrent_consumer(kind, name):
        backend.available -= 25  # Only net +15 is visible from the +40 release.
    backend.after_apply = concurrent_consumer
    _, controller, backend = setup(tmp_path, backend)
    result = controller.free({"ram": True}, by="caller")
    assert result["freed_gb"] == 15
    assert result["measurement"] == "net_host_available_gib" and result["stopped"] == ["a"]


def test_unknown_host_source_blocks_ram_free_without_fabricating_zero(tmp_path):
    backend = Backend(("a",))
    backend.models["a"] = replace(backend.models["a"], state="sleeping", is_sleeping=True, resident_gb=2)
    backend.available = None
    backend.errors = ("memory: unavailable",)
    _, controller, backend = setup(tmp_path, backend)
    result = controller.free({"ram": True}, by="caller")
    assert result["freed_gb"] is None and result["status"] == "blocked"
    assert not backend.calls


def test_dry_run_does_not_collect_dispatch_or_append_events(tmp_path):
    service, controller, backend = setup(tmp_path)
    before = service.snapshot(), service.events_since(0), backend.samples
    result = controller.free({}, by="caller", dry_run=True)
    assert result["would"]
    assert (service.snapshot(), service.events_since(0), backend.samples) == before
    assert not backend.calls and not (tmp_path/"unused.sqlite").exists()


def test_pending_wake_is_skipped_by_free(tmp_path):
    service, controller, backend = setup(tmp_path)
    with service.action_lock:
        controller.pending.add("a")
    result = controller.free({}, by="caller")
    assert backend.calls == [("sleep", "b")]
    assert any(item["reason"] == "operation_in_progress" for item in result["skipped"])


@pytest.mark.parametrize("payload", [[], None, {"ram": "false"}, {"gpu": True}, {"gpu": None}, {"need_gb": -1}, {"extra": 1}])
def test_invalid_free_payload_rejected_before_action(tmp_path, payload):
    _, controller, backend = setup(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        controller.free(payload, by="caller")
    assert not backend.calls


def test_guard_rejection_never_claims_an_external_effect_as_our_action(tmp_path):
    _, controller, backend = setup(tmp_path)
    def denied(action, **kwargs):
        backend.apply("sleep", action.model)  # Simulate an unrelated concurrent effect.
        raise ActionDispatchError("pinned", attempted=False)
    controller.dispatcher.execute = denied
    result = controller.free({}, by="caller")
    assert result["status"] == "failed" and result["slept"] == []
    assert result["freed_gb"] == 0 and not backend.calls


def test_missing_post_measurement_keeps_confirmed_partial_but_not_final_total(tmp_path):
    _, controller, backend = setup(tmp_path)
    def lose_measurement(kind, name):
        backend.free[0] = None
    backend.after_apply = lose_measurement
    result = controller.free({}, by="caller")
    assert result["status"] == "partial" and result["slept"] == ["a"]
    assert result["error"] == "measurement_unavailable" and result["measurement_complete"] is False
    assert result["freed_gb"] is None and result["measured_at"] is None
    assert backend.calls == [("sleep", "a")]


def test_configured_default_metadata_cannot_be_masked_by_observation(tmp_path):
    backend = Backend(("a",))
    backend.models["a"] = replace(backend.models["a"], state="sleeping", is_sleeping=True, resident_gb=2)
    _, controller, backend = setup(tmp_path, backend)
    controller.transport.models["a"]["is_default"] = True
    result = controller.free({"ram": True}, by="caller")
    assert result["status"] == "blocked" and result["stopped"] == []
    assert any(item["reason"] == "default_model" for item in result["skipped"])
    assert not backend.calls


def test_free_window_serialization_rejects_second_request_while_observing(tmp_path):
    service, controller, backend = setup(tmp_path)
    service.config = replace(service.config, action_observe_seconds=0.3)
    backend.apply_actions = False
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.free({"need_gb": 10}, by="first")))
    worker.start()
    deadline = time.monotonic()+0.2
    while not backend.calls and time.monotonic()<deadline:
        time.sleep(0.001)
    assert backend.calls
    with pytest.raises(ActionDispatchError, match="free_in_progress"):
        controller.free({}, by="second")
    backend.apply("sleep", "a")
    worker.join(1)
    assert results[0]["status"] == "complete"


def test_release_measurement_is_sampled_after_effect_confirmation(tmp_path):
    service, controller, backend = setup(tmp_path, Backend(("a",)))
    backend.release = 0
    original_collect = backend.collect
    delayed = [False]
    def collect_with_delayed_metric():
        snapshot = original_collect()
        if backend.models["a"].state == "sleeping" and not delayed[0]:
            delayed[0] = True
            backend.free[0] += 30  # First sleep-confirming round captured older GPU data.
        return snapshot
    service.collect = collect_with_delayed_metric
    result = controller.free({"need_gb": 25}, by="caller")
    assert result["status"] == "complete" and result["freed_gb"] == 30
    assert backend.calls == [("sleep", "a")]


@pytest.mark.parametrize("observation_seconds", [0.5, 0.06])
def test_new_activity_and_ram_pressure_after_first_action_block_further_effects(tmp_path, observation_seconds):
    service, controller, backend = setup(tmp_path)
    service.config = replace(service.config, action_observe_seconds=observation_seconds)
    # Functional confirmation and deadline rejection use the same observations.
    # Advance the fixture clock at the real condition-wait boundary; never make
    # either result depend on a shared host scheduling within 60 milliseconds.
    clock = [100.0]
    wall_origin = time.time()
    service.clock = lambda: wall_origin + clock[0] - 100.0
    controller.monotonic = lambda: clock[0]
    controller.dispatcher.monotonic = controller.monotonic
    controller.dispatcher.wall_clock = service.clock
    trace = []
    collect = backend.collect
    def observed_sample():
        snapshot = replace(collect(), sampled_at=service.clock())
        trace.append((clock[0], snapshot.models[0].state, snapshot.gpus[0].free_gb))
        return snapshot
    service.collect = observed_sample
    def observation_gap(timeout=None):
        assert timeout == service.config.action_poll_seconds
        clock[0] += 0.08
        return False
    service.changed.wait = observation_gap
    def changing_conditions(kind, name):
        backend.inflight["b"] = 1
        backend.available = 150
    backend.after_apply = changing_conditions
    result = controller.free({"need_gb": 80}, by="caller")
    assert backend.calls == [("sleep", "a")], (result, trace)
    assert trace[0] == (100.0, "awake", 20.0)
    assert trace[1] == (100.0, "sleeping", 50.0)
    if observation_seconds == 0.06:
        assert trace == [(100.0, "awake", 20.0), (100.0, "sleeping", 50.0)]
        assert result["status"] == "no_progress" and result["error"] == "effect_not_confirmed"
        assert result["freed_gb"] == 0 and result["slept"] == []
        assert result["measurement_complete"] is False
        return
    assert len(trace) == 3 and trace[2][0] > trace[1][0], (result, trace)
    assert result["status"] == "partial" and result["freed_gb"] == 30, (result, trace)
    assert result["slept"] == ["a"] and result["measurement_complete"] is True
    assert any(item["model"] == "b" and item["reason"] == "in_flight" for item in result["skipped"])


def test_default_stays_awake_when_sleep_ram_admission_fails(tmp_path):
    backend = Backend(("a",))
    backend.available = 170
    _, controller, backend = setup(tmp_path, backend)
    controller.transport.models["a"]["is_default"] = True
    result = controller.free({}, by="caller")
    assert not backend.calls and backend.models["a"].state == "awake"
    assert any(item["reason"] == "memory_budget" for item in result["skipped"])


def test_same_gpu_wake_admissions_do_not_overlap_while_lock_is_released(tmp_path):
    backend = Backend()
    for name, model in backend.models.items():
        backend.models[name] = replace(model, state="sleeping", is_sleeping=True, resident_gb=2, swap_state="stopped")
    backend.free[0] = 150
    _, controller, backend = setup(tmp_path, backend)
    entered, release = threading.Event(), threading.Event()
    calls, result = [], []
    def wake_request(method, path, *, deadline):
        calls.append((method, path))
        entered.set()
        assert release.wait(0.5)
        with backend.lock:
            backend.models["a"] = replace(backend.models["a"], state="awake", is_sleeping=False, swap_state="ready", resident_gb=80)
        return 404
    controller.transport.http_request = wake_request
    worker = threading.Thread(target=lambda: result.append(controller.wake("a", by="one")))
    worker.start()
    try:
        assert entered.wait(0.2)
        blocked = controller.wake("b", by="two")
        assert blocked["status"] == "blocked" and blocked["error"] == "operation_in_progress"
        assert calls == [("GET", "/upstream/a/")]
    finally:
        release.set()
        worker.join(1)
    assert result[0]["status"] == "ready"


def test_repeated_timestamp_is_not_post_effect_measurement_evidence(tmp_path):
    service, controller, backend = setup(tmp_path, Backend(("a",)))
    fixed_time = time.time()
    service.collect = lambda: replace(backend.collect(), sampled_at=fixed_time)
    result = controller.free({}, by="caller")
    assert result["status"] == "no_progress"
    assert result["slept"] == [] and result["measurement_complete"] is False
    assert backend.calls == [("sleep", "a")]


def test_measured_goal_removes_stale_estimate_based_insufficiency(tmp_path):
    backend = Backend(("a",))
    backend.models["a"] = replace(backend.models["a"], resident_gb=4)
    _, controller, backend = setup(tmp_path, backend)
    result = controller.free({"need_gb": 25}, by="caller")
    assert result["status"] == "complete" and result["freed_gb"] == 30
    assert not any(item["reason"] == "insufficient_reclaimable_memory" for item in result["skipped"])


def test_older_collection_cannot_overwrite_newer_activity_observation(tmp_path):
    from llmsvc.state import StateSnapshot
    entered, release = threading.Event(), threading.Event()
    class DelayedSnapshot(StateSnapshot):
        def to_dict(self):
            entered.set()
            assert release.wait(1)
            return super().to_dict()
    old = DelayedSnapshot(sampled_at=time.time(), activity=(Activity("a", in_flight=0),))
    new = StateSnapshot(sampled_at=time.time(), activity=(Activity("a", in_flight=1),))
    observations = iter((old, new))
    service = Scheduler(SchedulerConfig("127.0.0.1", 8011), lambda: next(observations))
    worker = threading.Thread(target=service.sample_once)
    worker.start()
    try:
        assert entered.wait(1)
        service.sample_once()
    finally:
        release.set()
        worker.join(1)
    assert service.snapshot().activity[0].in_flight == 1
