# Generated-By: Codex / gpt-6-astra
"""Ordinary recovery baseline: pure intent is not runtime action authority."""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.policy import plan_sleeping_recovery
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Lease, Reserve
from llmsvc.store import IntentStore
from test_policy_recovery import snapshot as recovery_snapshot
from test_registry_http_preview import request


@pytest.fixture
def recovery_system(tmp_path):
    state = recovery_snapshot()
    source = replace(state.models[0], unit="vllm-source.service", unit_active=True,
                     health_ok=True, is_sleeping=True, swap_state="ready", util=.8)
    values = {"snapshot": replace(state, models=(source,)), "now": state.sampled_at, "calls": []}
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
                             state_db_path=str(tmp_path / "recovery.sqlite"), wake_timeout_seconds=2)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    store.create_lease(Lease("source-lease", "source", 1, .8, 20000, 80), source.unit)
    store.transition_lease("source-lease", "confirmed")
    def collect():
        values["now"] += .01
        return replace(values["snapshot"], sampled_at=values["now"])
    scheduler = Scheduler(config, collect, store=store, clock=lambda: values["now"])
    def forbidden(*args, **kwargs):
        values["calls"].append((args, kwargs))
        raise AssertionError("Baseline invoked a model transport without recovery opt-in")
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:1",
        models={"source": {"unit": source.unit, "util": .8, "weights_gb": 40}},
        systemctl="unused-test-systemctl", run=forbidden)
    transport.http_request = forbidden
    transport.stop_unit = forbidden
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.sample_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    thread.start()
    try:
        yield SimpleNamespace(scheduler=scheduler, state=values, store=store, address=server.server_address)
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(3)
        store.close()


@pytest.mark.parametrize("recent", [False, True])
def test_recovery_policy_does_not_silently_turn_manual_wake_into_stop_or_relocation(recovery_system, recent):
    system = recovery_system
    state = system.state["snapshot"]
    system.state["snapshot"] = replace(state, activity=(replace(state.activity[0], requests_last_hour=3 if recent else 0),))
    system.scheduler.sample_once()
    decision = plan_sleeping_recovery(system.scheduler.snapshot())
    expected = [("stop", "source", 1)] + ([("place", "source", 0)] if recent else [])
    assert [(a.kind, a.model, a.gpu) for a in decision.actions] == expected
    before = system.store.leases()
    status, result = request(system.address, "POST", "/v1/wake/source")
    assert status == 200 and result["status"] == "blocked" and result["ready"] is False
    assert result["error"] == "insufficient_gpu_memory"
    assert system.state["calls"] == [] and system.store.leases() == before
    assert system.store.lease("source-lease")[0].budget_gb == 80
    assert system.scheduler.model_actions.pending == set()
    assert not any(event.kind in ("place", "lease_released", "placement_action_result")
                   for event in system.scheduler.events_since(0))


def test_impossible_relocation_keeps_source_account_and_causes_no_speculative_transport(recovery_system):
    system = recovery_system
    system.store.put_reserve(Reserve("destination-held", 0, 1, system.state["now"]+600, "fixture-owner"))
    system.scheduler.sample_once()
    decision = plan_sleeping_recovery(system.scheduler.snapshot())
    assert decision.actions == ()
    assert any(item.reason == "relocation_unavailable" for item in decision.blocked_by)
    before = system.store.leases(), system.store.active(system.state["now"])
    status, result = request(system.address, "POST", "/v1/wake/source")
    assert status == 200 and result["ready"] is False
    assert system.state["calls"] == []
    assert (system.store.leases(), system.store.active(system.state["now"])) == before


def finish_source_exit_fixture(system):
    from llmsvc.leases import PlacementController, UnitObservation
    scheduler = system.scheduler
    scheduler.config = replace(scheduler.config, placement_enabled=True, placement_wait_seconds=2)
    scheduler.placement = PlacementController(scheduler, scheduler.model_actions.transport,
        probe=lambda model, **kwargs: UnitObservation(False, True))
    state = system.state["snapshot"]
    system.state["snapshot"] = replace(state,
        models=(replace(state.models[0], state="stopped", unit_active=False, resident_gb=0),),
        memory=replace(state.memory, sleeping_weights_gb=0))
    scheduler.sample_once()
    # This fixture supplies positive absent-unit evidence through the actual
    # reconciler/finish path; no source budget is removed by a policy estimate.
    assert scheduler.placement.finish("release", "source-lease")["status"] == "released"
    assert not system.store.leases()


def test_precreating_destination_lease_would_reject_normal_launcher_reentry(recovery_system):
    system = recovery_system
    finish_source_exit_fixture(system)
    system.store.create_lease(Lease("destination-lease", "source", 0, .8, 20000, 80), "vllm-source.service")
    before = system.store.leases()
    status, result = request(system.address, "POST", "/v1/place", {"model": "source", "util": .8})
    assert status == 409 and result["error"] == "outstanding_lease"
    assert system.store.leases() == before and system.state["calls"] == []


def test_normal_unconstrained_reentry_can_choose_former_source_after_pressure_clears(recovery_system):
    system = recovery_system
    finish_source_exit_fixture(system)
    state = system.state["snapshot"]
    system.state["snapshot"] = replace(state, gpus=(state.gpus[0], replace(state.gpus[1], external_gb=0, free_gb=100)))
    system.store.put_reserve(Reserve("other-card-held", 0, 1, 20000, "fixture-owner"))
    system.scheduler.sample_once()
    status, result = request(system.address, "POST", "/v1/place", {"model": "source", "util": .8})
    assert status == 200 and result["gpu"] == 1  # Legal for an ordinary request; insufficient for relocation.
    assert system.store.lease(result["lease_id"])[0].budget_gb == 80
    assert system.state["calls"] == []


@pytest.mark.parametrize("field,value", [("sleeping_recovery_enabled",1),
    ("sleeping_recovery_timeout_seconds",0), ("sleeping_recovery_timeout_seconds",-1),
    ("sleeping_recovery_timeout_seconds",True), ("sleeping_recovery_timeout_seconds",float("nan")),
    ("sleeping_recovery_timeout_seconds",float("inf")), ("sleeping_recovery_timeout_seconds",901),
    ("sleeping_recovery_timeout_seconds","900")])
def test_recovery_options_are_explicit_and_finitely_bounded(field,value):
    with pytest.raises(ValueError,match=field):
        SchedulerConfig("127.0.0.1",8011,**{field:value})
