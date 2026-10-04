# Generated-By: Claude Code / claude-opus-5-5
"""Wedged-wake recovery (#300) with fake units, SQLite and a loopback llama-swap."""

import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.scheduler import Scheduler
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, StateSnapshot
from llmsvc.store import IntentStore
from llmsvc.wake_failure import WakeFailureRecoveryController

NAME = "qwen3.8-27b"
UNIT = "vllm-" + NAME + ".service"
OTHER = "gemma-4-31b-it-bf16"


def unit(name):
    return "vllm-" + name + ".service"


def wedged(name, gpu):
    return ModelState(name, state="sleeping", gpu=gpu, util=0.7, budget_gb=98, weights_gb=55,
                      resident_gb=88, unit=unit(name), unit_active=True, health_ok=True,
                      is_sleeping=True, swap_state="ready", cold_start_seconds=120)


@pytest.fixture
def system(tmp_path):
    state = {"now": 1791104461.0, "step": 15.0, "calls": [], "inflight": {NAME: 5, OTHER: 0},
             "unload_status": [], "stop_code": 0, "errors": ()}
    # The second model is a healthy awake neighbour unless a test wedges it.
    models = {NAME: wedged(NAME, 0),
              OTHER: replace(wedged(OTHER, 1), state="awake", is_sleeping=False)}
    observations = {name: UnitObservation(True, False, True, "lease-" + name, "a"*32) for name in models}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            name = self.path.rsplit("/", 1)[1]
            assert self.path == "/api/models/unload/" + name and name in models
            state["calls"].append(("unload", name))
            status = state["unload_status"].pop(0) if state["unload_status"] else 200
            if status == 200:
                models[name] = replace(models[name], swap_state="stopped")
                state["inflight"][name] = 0
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))
    thread.start()
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
        wake_failure_recovery_enabled=True, wake_failure_grace_seconds=60,
        state_db_path=str(tmp_path / "state.sqlite"), free_timeout_seconds=3,
        action_poll_seconds=0.005, request_timeout_seconds=1, lease_probe_seconds=0.1)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    for name, model in models.items():
        store.create_lease(Lease("lease-" + name, name, model.gpu, 0.7, state["now"] + 86400, 98), unit(name))
        store.transition_lease("lease-" + name, "confirmed")

    def collect():
        state["now"] += state["step"]
        sleeping = sum(m.weights_gb for m in models.values() if m.state == "sleeping")
        return StateSnapshot(sampled_at=state["now"], models=tuple(models.values()), errors=state["errors"],
            gpus=(GPUState(0, total_gb=140, free_gb=10, external_gb=40),
                  GPUState(1, total_gb=140, free_gb=10, external_gb=40)),
            memory=MemoryState(400, sleeping),
            activity=tuple(Activity(name, state["now"] - 1, 900, 80, state["inflight"][name], ("client",))
                           for name in models))

    scheduler = Scheduler(config, collect, store=store, clock=lambda: state["now"])

    def run(argv, **kwargs):
        name = argv[2][len("vllm-"):-len(".service")]
        assert argv == ["fake-systemctl", "stop", unit(name)] and kwargs["timeout"] > 0
        state["calls"].append(("stop", name))
        if state["stop_code"] == 0:
            # The backend dies; llama-swap still believes the model is ready.
            models[name] = replace(models[name], state="stopped", unit_active=False, health_ok=None,
                                   is_sleeping=None, resident_gb=0)
            observations[name] = UnitObservation(False, True)
        return SimpleNamespace(returncode=state["stop_code"])

    def make_transport(names=tuple(models)):
        return ManagedModelTransport(swap_url="http://127.0.0.1:" + str(upstream.server_port),
            models={name: {"unit": unit(name)} for name in names}, systemctl="fake-systemctl", run=run)

    transport = make_transport()
    scheduler.model_actions = ModelActionController(scheduler, transport)
    accounting = PlacementController(scheduler, transport, probe=lambda name, *, deadline: observations[name])
    recovery = WakeFailureRecoveryController(scheduler, accounting=accounting)
    try:
        yield SimpleNamespace(scheduler=scheduler, recovery=recovery, state=state, models=models,
                              transport=transport, store=store, make_transport=make_transport,
                              collect=collect, observations=observations)
    finally:
        scheduler.stop()
        store.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(2)


def observe(system, rounds):
    results = []
    for _ in range(rounds):
        system.scheduler.sample_once()
        results.append(system.recovery.run_once())
    return results


def recover(system):
    """Cross the grace period at the 15 s cadence, then let the sampler confirm."""
    statuses = observe(system, 5)  # 0, 15, 30, 45, 60 s: the fifth round is past grace.
    assert [r["status"] for r in statuses[:4]] == ["observing"] * 4, statuses
    return statuses[4]


def arm(system):
    """Reach the grace period manually, then let the running sampler confirm effects."""
    observe(system, 4)
    system.scheduler.sample_once()  # Fifth round: 60 s after the first mismatch.
    system.state["step"] = 0.01
    system.scheduler.start()  # Sampler only; confirmation needs fresh rounds.
    return system.recovery.run_once()


def test_incident_replay_stops_releases_and_unloads_despite_in_flight(system, caplog):
    caplog.set_level(logging.INFO)
    result = arm(system)
    assert result["status"] == "complete", result
    assert result["stop_confirmed"] and result["proxy_unloaded"] and result["in_flight"] == 5
    assert system.state["calls"] == [("stop", NAME), ("unload", NAME)]
    assert system.store.lease("lease-" + NAME)[0].status == "released"
    kinds = [event.kind for event in system.scheduler.events_since(0)]
    assert kinds.index("wake_failure_detected") < kinds.index("lease_released") < kinds.index("wake_failure_result")
    dispatch = [json.loads(r.message) for r in caplog.records if r.name == "llmsvc.actions"]
    assert dispatch[-1]["kind"] == "action_dispatch" and dispatch[-1]["action"]["reason"] == "wake_failed"
    assert NAME not in system.scheduler.model_actions.pending


def test_ordinary_stop_is_still_blocked_by_in_flight(system):
    system.scheduler.sample_once()
    result = system.scheduler.model_actions.stop_model(NAME, by="test")
    assert result["status"] == "blocked" and result["error"] == "in_flight"
    assert system.state["calls"] == []


def test_healthy_sleeper_with_stopped_data_plane_is_left_alone(system):
    system.models[NAME] = replace(system.models[NAME], swap_state="stopped")
    system.state["inflight"][NAME] = 0
    assert {r["status"] for r in observe(system, 10)} == {"observing"}
    assert system.state["calls"] == []


@pytest.mark.parametrize("protection", ["pin", "default"])
def test_pinned_or_default_wedge_is_reported_never_touched(system, protection):
    if protection == "pin":
        system.store.put_pin(Pin(NAME, system.state["now"] + 86400, "owner"))
        reason = "pinned_until"
    else:
        system.transport.models[NAME]["is_default"] = True
        reason = "default_model"
    results = observe(system, 8)
    assert results[4]["status"] == "blocked" and results[4]["blocked_by"][0]["reason"] == reason
    assert system.state["calls"] == []
    blocked = [e for e in system.scheduler.events_since(0) if e.kind == "wake_failure_blocked"]
    assert len(blocked) == 1 and blocked[0].detail["in_flight"] == 5  # Reported once per window.
    assert system.store.lease("lease-" + NAME)[0].status == "confirmed"


def test_unconfirmed_identity_blocks_before_any_stop(system):
    system.recovery.accounting.probe = lambda name, *, deadline: UnitObservation(True, False, True, "other")
    result = recover(system)
    assert result["status"] == "blocked" and result["error"] == "unit_identity_unconfirmed"
    assert system.state["calls"] == []
    # The window restarts: no immediate retry on the next round.
    assert observe(system, 1)[0]["status"] == "observing"


def test_rejected_unload_is_partial_and_keeps_released_account(system):
    system.state["unload_status"] = [500]
    result = arm(system)
    assert result["status"] == "partial" and result["error"] == "proxy_unload_rejected"
    assert result["stop_confirmed"] and not result["proxy_unloaded"]
    assert system.store.lease("lease-" + NAME)[0].status == "released"


def test_dry_run_previews_without_events_probes_or_transport(system, caplog):
    caplog.set_level(logging.INFO)
    system.scheduler.config = replace(system.scheduler.config, read_only=True)
    results = observe(system, 8)
    assert results[4]["status"] == "preview" and results[4]["would"][0]["reason"] == "wake_failed"
    assert system.state["calls"] == []
    assert not [e for e in system.scheduler.events_since(0) if e.kind.startswith("wake_failure")]
    previews = [json.loads(r.message) for r in caplog.records if r.name == "llmsvc.wake_failure"]
    # Logged once per (model, window), not on every later round of the same wedge.
    assert len(previews) == 1 and previews[0]["kind"] == "wake_failure_preview" and previews[0]["dry_run"]
    assert system.store.lease("lease-" + NAME)[0].status == "confirmed"


def test_worker_thread_runs_only_when_configured(system):
    system.scheduler.wake_failures = system.recovery
    system.state["step"] = 0.01
    system.scheduler.config = replace(system.scheduler.config, read_only=True, sample_interval_seconds=0.05)
    system.scheduler.start()
    assert system.scheduler._wake_failure_thread is not None and system.scheduler._wake_failure_thread.is_alive()
    system.scheduler.stop()
    assert not system.scheduler._wake_failure_thread.is_alive()
    assert system.state["calls"] == []


def test_config_flag_is_off_by_default_and_grace_is_bounded():
    config = SchedulerConfig("127.0.0.1", 8011)
    assert config.wake_failure_recovery_enabled is False and config.wake_failure_grace_seconds == 90
    for value in (10, 7200, float("inf"), True):
        with pytest.raises(ValueError):
            SchedulerConfig("127.0.0.1", 8011, wake_failure_grace_seconds=value)


def test_catalog_publication_rebinds_the_controller_and_a_wedge_still_completes(system):
    from llmsvc.catalog import CatalogRuntime
    scheduler = system.scheduler
    scheduler.placement = system.recovery.accounting  # Its fake probe survives publication.
    scheduler.wake_failures = old = system.recovery
    runtime = CatalogRuntime(scheduler, SimpleNamespace(action_lock=scheduler.action_lock),
        collector_factory=lambda config: system.collect, relay_factory=lambda config: None,
        transport_factory=lambda config, models: system.make_transport(tuple(models)))
    manifest = {"sources": {"swap_url": system.transport.swap_url}, "retained": {},
                "active": {name: {"unit": unit(name)} for name in system.models}}
    with scheduler.action_lock:
        runtime._publish(manifest, "1" * 32, runtime._construct(manifest))
    new = scheduler.wake_failures
    assert new is not old and new.controller is scheduler.model_actions
    assert new.accounting is scheduler.placement and not old.enabled()
    scheduler.wake_failures = None  # Drive it by hand; no worker thread in this test.
    system.recovery = new
    result = arm(system)
    assert result["status"] == "complete", result
    assert system.state["calls"] == [("stop", NAME), ("unload", NAME)]


def test_two_wedged_models_recover_one_per_sample(system):
    system.models[OTHER] = wedged(OTHER, 1)
    observe(system, 4)
    system.scheduler.sample_once()
    system.state["step"] = 0.01
    system.scheduler.start()
    first = system.recovery.run_once()
    assert first["status"] == "complete" and first["model"] == OTHER  # Sorted by name.
    assert system.state["calls"] == [("stop", OTHER), ("unload", OTHER)]
    second = {"status": "observing"}
    for _ in range(200):
        second = system.recovery.run_once()
        if second["status"] != "observing":
            break
        system.scheduler.request_sample()
        with system.scheduler.changed:
            system.scheduler.changed.wait(0.01)
    assert second["status"] == "complete" and second["model"] == NAME, second
    assert system.state["calls"][2:] == [("stop", NAME), ("unload", NAME)]


def test_rejected_stop_is_failed_and_never_unloads(system):
    system.state["stop_code"] = 1
    system.scheduler.config = replace(system.scheduler.config, free_timeout_seconds=0.5)
    result = arm(system)
    assert result["status"] == "failed" and result["error"] == "transport_rejected", result
    assert not result["stop_confirmed"] and not result["proxy_unloaded"]
    assert system.state["calls"] == [("stop", NAME)]
    assert system.store.lease("lease-" + NAME)[0].status == "confirmed"


def test_worker_survives_errors_without_busy_looping(system, caplog):
    caplog.set_level(logging.INFO)
    calls = []

    class Broken:
        def configured(self):
            return True

        def run_once(self):
            calls.append(1)
            raise RuntimeError("boom")

    system.scheduler.wake_failures = Broken()
    system.state["step"] = 0.01
    system.scheduler.config = replace(system.scheduler.config, sample_interval_seconds=0.05)
    system.scheduler.start()
    threading.Event().wait(0.5)
    assert system.scheduler._wake_failure_thread.is_alive()
    published = system.scheduler._sample_published
    system.scheduler.stop()
    # Paced by published samples (plus one), not spinning.
    assert 2 <= len(calls) <= published + 2
    errors = [json.loads(r.message) for r in caplog.records if "wake_failure_error" in r.message]
    assert len(errors) == 1 and errors[0]["error_type"] == "RuntimeError"  # Repeats are collapsed.


@pytest.mark.parametrize("guard", ["operation_in_progress", "fault_recovery_pending", "unleased_model"])
def test_executor_guards_block_instead_of_acting(system, guard):
    controller = system.scheduler.model_actions
    if guard == "operation_in_progress":
        controller.pending.add(NAME)
    elif guard == "fault_recovery_pending":
        controller._fault_pending = lambda name: name == NAME
    else:
        system.store.transition_lease("lease-" + NAME, "released")
    results = observe(system, 6)
    assert results[4]["status"] == "blocked" and results[4]["blocked_by"][0]["reason"] == guard, results[4]
    assert system.state["calls"] == []


def test_unfinished_unload_is_retried_on_a_later_sample_and_bounded(system):
    system.state["unload_status"] = [500, 200]
    result = arm(system)
    assert result["status"] == "partial" and result["error"] == "proxy_unload_rejected"
    retry = None
    for _ in range(200):
        retry = system.recovery.run_once()
        if retry.get("attempt"):
            break
        system.scheduler.request_sample()
        with system.scheduler.changed:
            system.scheduler.changed.wait(0.01)
    assert retry["status"] == "complete" and retry["attempt"] == 2, retry
    for _ in range(20):
        system.recovery.run_once()
    assert system.state["calls"] == [("stop", NAME), ("unload", NAME), ("unload", NAME)]
    events = [e for e in system.scheduler.events_since(0) if e.kind == "wake_failure_unload_retry"]
    assert len(events) == 1


def test_unload_retries_stop_after_the_attempt_budget(system):
    from llmsvc.wake_failure import MAX_UNLOAD_ATTEMPTS
    system.state["unload_status"] = [500] * 10
    arm(system)
    for _ in range(300):
        system.recovery.run_once()
        if not system.recovery.unloads:
            break
        system.scheduler.request_sample()
        with system.scheduler.changed:
            system.scheduler.changed.wait(0.01)
    assert not system.recovery.unloads
    assert system.state["calls"].count(("unload", NAME)) == MAX_UNLOAD_ATTEMPTS


def test_unload_has_its_own_budget_beyond_the_stop_deadline(system):
    transport = system.scheduler.model_actions.transport
    seen = []
    original = transport.http_request

    def record(method, path, *, deadline):
        seen.append(deadline - system.scheduler.monotonic())
        return original(method, path, deadline=deadline)

    transport.http_request = record
    assert arm(system)["status"] == "complete"
    config = system.scheduler.config
    assert seen and seen[0] > config.free_timeout_seconds
    assert seen[0] <= config.request_timeout_seconds + config.wake_failure_unload_allowance_seconds


def test_unload_allowance_is_bounded():
    assert SchedulerConfig("127.0.0.1", 8011).wake_failure_unload_allowance_seconds == 90
    for value in (-1, 601, float("nan"), True):
        with pytest.raises(ValueError):
            SchedulerConfig("127.0.0.1", 8011, wake_failure_unload_allowance_seconds=value)
