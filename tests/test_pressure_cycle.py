# Generated-By: Codex / gpt-6-astra
"""Opt-in per-GPU cycles with real loopback actions and simulated observations."""

from dataclasses import replace
import threading

import pytest

import test_automatic_cycle as fixed
from test_automatic_cycle import system
from llmsvc.config import SchedulerConfig, load_config
from llmsvc.policy import PolicySettings, plan_placement
from llmsvc.state import GPUProcess, ModelState, Pin, Reserve


@pytest.fixture
def pressure(system):
    scheduler, cycle, state, models, transport, observations = system
    scheduler.config = replace(scheduler.config, automation_policy="gpu_pressure")
    state.update(external=0, free=100, processes=())
    collect = scheduler.collect
    def observed():
        snapshot = collect()
        return replace(snapshot, gpus=tuple(replace(gpu, free_gb=state["free"],
            external_gb=state["external"], external_processes=state["processes"]) for gpu in snapshot.gpus))
    scheduler.collect = observed
    scheduler.sample_once()
    return system


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
@pytest.mark.parametrize("signal", ["process", "external-threshold", "low-free"])
def test_shared_pressure_reacts_at_next_simulated_sample_and_keeps_account(pressure, signal):
    scheduler, cycle, state, models, _, _ = pressure
    state["idle"]["a"] = 31
    scheduler.sample_once()
    assert not cycle.plan(scheduler.snapshot()).actions
    if signal == "process":
        state["processes"] = (GPUProcess(999, used_gb=0.01, name="fixture-external"),)
    elif signal == "external-threshold":
        state["external"] = scheduler.config.automation_shared_external_threshold_gb
    else:
        state["free"] = scheduler.config.automation_shared_free_threshold_gb - 0.1
    # Simulate one configured sampler interval, not elapsed GPU response time.
    signal_at = state["now"]
    state["now"] += scheduler.config.sample_interval_seconds
    fixed.start(pressure)
    result = cycle.run()
    assert result["status"] == "complete", result
    assert state["calls"] == [("sleep", "a")]
    assert result["actions"][0]["reason"] == "shared_gpu_pressure"
    assert state["now"] - signal_at < scheduler.config.sample_interval_seconds + 1
    assert models["a"].state == "sleeping"
    assert scheduler.store.lease("lease-a")[0].budget_gb == 60
    assert scheduler.store.lease("lease-a")[0].status == "confirmed"
    assert "freed_gb" not in result


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
def test_driver_residue_alone_does_not_trigger_sleep(pressure):
    scheduler, cycle, state, _, _, _ = pressure
    state["idle"]["a"] = 100
    state["external"] = 0.1
    fixed.start(pressure)
    result = cycle.run()
    assert result["status"] == "complete" and not result["actions"]
    assert not state["calls"]


@pytest.mark.parametrize("system,idle,acts", [
    ({"gpu": 0}, 600, False), ({"gpu": 0}, 3599, False), ({"gpu": 0}, 3600, True),
    ({"gpu": 1}, 299, False), ({"gpu": 1}, 300, True),
], indirect=["system"])
def test_per_gpu_exact_ttl_boundaries(pressure, idle, acts):
    _, cycle, state, _, _, _ = pressure
    state["idle"]["a"] = idle
    fixed.start(pressure)
    result = cycle.run()
    assert result["status"] == "complete", result
    assert state["calls"] == ([("sleep", "a")] if acts else [])
    if acts:
        assert result["actions"][0]["reason"] == "gpu_idle_ttl"


@pytest.mark.parametrize("system,mode,idle,acts", [
    ({"gpu": 0}, "fixed_idle", 600, False), ({"gpu": 0}, "fixed_idle", 601, True),
    ({"gpu": 1}, "fixed_idle", 300, False),
    ({"gpu": 0}, "gpu_pressure", 600, False), ({"gpu": 1}, "gpu_pressure", 300, True),
], indirect=["system"])
def test_only_selected_ttl_planner_runs(system, monkeypatch, mode, idle, acts):
    import llmsvc.policy
    scheduler, cycle, state, _, _, _ = system
    scheduler.config = replace(scheduler.config, automation_policy=mode)
    state["idle"]["a"] = idle
    scheduler.sample_once()
    def forbidden(*args, **kwargs):
        pytest.fail("The unselected TTL planner ran")
    monkeypatch.setattr(llmsvc.policy, "plan_idle_sleep" if mode == "gpu_pressure" else "plan_pressure_sleep", forbidden)
    decision = cycle.plan(scheduler.snapshot())
    assert bool(decision.actions) is acts


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
@pytest.mark.parametrize("protection", ["pin", "inflight", "unknown-inflight", "unknown-gpu", "unleased", "unit-mismatch"])
def test_pressure_keeps_protected_unknown_and_unmanaged_models(pressure, protection):
    scheduler, cycle, state, models, transport, _ = pressure
    state["idle"]["a"] = 31
    state["external"] = 2
    if protection == "pin":
        scheduler.store.put_pin(Pin("a", 20000, "owner"))
    elif protection == "inflight":
        state["inflight"]["a"] = 1
    elif protection == "unknown-inflight":
        state["inflight"]["a"] = None
    elif protection == "unknown-gpu":
        state["free"] = None
    elif protection == "unleased":
        scheduler.store.transition_lease("lease-a", "released")
    else:
        transport.units["a"] = "vllm-other.service"
    fixed.start(pressure)
    result = cycle.run()
    assert result["status"] == "blocked", result
    assert not state["calls"] and models["a"].state == "awake"


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
def test_pressure_clearing_during_probe_invalidates_stale_action(pressure):
    scheduler, cycle, state, _, _, _ = pressure
    state["idle"]["a"] = 31
    state["external"] = 2
    def clear(name):
        state["probe_hook"] = None
        state["external"] = 0
        scheduler.sample_once()
    state["probe_hook"] = clear
    fixed.start(pressure)
    result = cycle.run()
    assert not state["calls"] and not result["actions"], result


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
def test_pressure_worker_replans_after_effect_and_preserves_new_reservation(pressure):
    scheduler, cycle, state, models, _, _ = pressure
    models["b"] = replace(models["b"], state="awake", is_sleeping=False)
    state["idle"] = {"a": 31, "b": 31}
    state["external"] = 2
    saved = Reserve("concurrent", 1, 10, 20000, "owner")
    emit = scheduler.emit
    def after(kind, **kwargs):
        event = emit(kind, **kwargs)
        if kind == "automation_action_result" and event.detail["confirmed"]:
            scheduler.store.put_reserve(saved)
            state["external"] = 0
        return event
    scheduler.emit = after
    fixed.start(pressure)
    result = cycle.run()
    assert state["calls"] == [("sleep", "a")], result
    assert models["b"].state == "awake" and scheduler.store.reserve(saved.id) == saved
    assert all(scheduler.store.lease("lease-" + n)[0].status == "confirmed" for n in "ab")
    decision = plan_placement(scheduler.snapshot(), ModelState("new", state="stopped", util=.1))
    assert not decision.actions and any(b.reason == "reserved" for b in decision.blocked_by)


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
@pytest.mark.parametrize("case", [
    fixed.test_no_overlapping_cycles_with_deterministic_barrier,
    fixed.test_new_pin_during_final_identity_probe_is_revalidated,
    fixed.test_no_measured_memory_progress_prevents_second_victim,
    fixed.test_default_is_never_hard_stopped_and_failed_sleep_admission_holds_awake,
    fixed.test_memory_pressure_replans_from_real_gain_not_policy_estimate,
    fixed.test_budget_pressure_stops_only_until_observed_membership_fits,
    fixed.test_sleep_admission_reclaims_and_reobserves_before_sleep,
    fixed.test_new_unknown_observation_blocks_next_action_after_confirmed_progress,
    fixed.test_shutdown_during_observation_stops_future_actions_and_keeps_unconfirmed_account,
    fixed.test_source_reader_proceeds_while_automatic_transport_holds_action_lock,
])
def test_existing_execution_and_lifecycle_proofs_also_hold_in_pressure_mode(pressure, case):
    case(pressure)


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
@pytest.mark.parametrize("flag,value", [("automation_enabled", False), ("model_actions_enabled", False), ("read_only", True)])
def test_pressure_mode_keeps_optins_and_zero_effect_preview(pressure, monkeypatch, flag, value):
    fixed.test_three_optins_and_dry_run_are_zero_effects(pressure, flag, value, monkeypatch)


def test_pressure_config_defaults_match_policy_and_load_explicit_values(tmp_path):
    cfg = SchedulerConfig("127.0.0.1", 19001)
    settings = PolicySettings()
    assert cfg.automation_policy == "fixed_idle" and not cfg.automation_enabled
    for suffix in ("exclusive_ttl_seconds", "shared_ttl_seconds", "shared_external_threshold_gb", "shared_free_threshold_gb"):
        assert getattr(cfg, "automation_" + suffix) == getattr(settings, suffix)
    path = tmp_path / "config.yaml"
    path.write_text("listen_host: 127.0.0.1\nlisten_port: 19001\nautomation_policy: gpu_pressure\n"
                    "automation_shared_ttl_seconds: 450\nautomation_exclusive_ttl_seconds: 4000\n"
                    "automation_shared_external_threshold_gb: 2\nautomation_shared_free_threshold_gb: 12\n")
    cfg = load_config(str(path))
    assert cfg.automation_policy == "gpu_pressure" and cfg.automation_shared_ttl_seconds == 450
    assert cfg.read_only and not cfg.model_actions_enabled


@pytest.mark.parametrize("key,value", [
    ("automation_policy", "both"), ("automation_policy", True), ("automation_policy", []),
    ("automation_shared_ttl_seconds", 0), ("automation_exclusive_ttl_seconds", float("inf")),
    ("automation_shared_external_threshold_gb", -1), ("automation_shared_free_threshold_gb", float("nan")),
    ("automation_shared_free_threshold_gb", True),
])
def test_invalid_pressure_configuration_is_rejected(key, value):
    with pytest.raises(ValueError, match=key):
        SchedulerConfig("127.0.0.1", 19001, **{key: value})


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
def test_actual_worker_uses_pressure_mode_and_joins_after_one_cycle(pressure):
    scheduler, cycle, state, _, _, _ = pressure
    state["idle"]["a"] = 31
    state["external"] = 2
    finished = threading.Event()
    emit = scheduler.emit
    def emitted(kind, **kwargs):
        event = emit(kind, **kwargs)
        if kind == "automation_result":
            finished.set()
        return event
    scheduler.emit = emitted
    scheduler.automation = cycle
    scheduler.start()
    assert finished.wait(3)
    scheduler.stop()
    assert state["calls"] == [("sleep", "a")]
    assert not scheduler._automation_thread.is_alive() and not cycle.active
    assert not scheduler.model_actions.pending


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
@pytest.mark.parametrize("mode", ["error", "no-effect", "unconfirmed-exit"])
def test_pressure_failure_preserves_account_and_stops(pressure, mode):
    fixed.test_failed_or_unknown_effect_retains_budget_and_stops_cycle(pressure, mode)


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
@pytest.mark.parametrize("settings,observed,acts", [
    ({"automation_shared_ttl_seconds": 450}, {"idle": 400}, False),
    ({"automation_shared_ttl_seconds": 450}, {"idle": 450}, True),
    ({"automation_shared_external_threshold_gb": 3}, {"idle": 31, "external": 2}, False),
    ({"automation_shared_external_threshold_gb": 3}, {"idle": 31, "external": 3}, True),
    ({"automation_shared_free_threshold_gb": 20}, {"idle": 31, "free": 19}, True),
])
def test_configured_policy_values_change_actual_pressure_eligibility(pressure, settings, observed, acts):
    scheduler, cycle, state, _, _, _ = pressure
    scheduler.config = replace(scheduler.config, **settings)
    state["idle"]["a"] = observed["idle"]
    state.update({key: value for key, value in observed.items() if key != "idle"})
    fixed.start(pressure)
    result = cycle.run()
    assert result["status"] == "complete", result
    assert state["calls"] == ([("sleep", "a")] if acts else [])


def test_custom_exclusive_ttl_is_used_without_fixed_idle_fallback(pressure):
    scheduler, cycle, state, _, _, _ = pressure
    scheduler.config = replace(scheduler.config, automation_exclusive_ttl_seconds=4000)
    state["idle"]["a"] = 3600
    scheduler.sample_once()
    assert not cycle.plan(scheduler.snapshot()).actions
    state["idle"]["a"] = 4000
    fixed.start(pressure)
    result = cycle.run()
    assert state["calls"] == [("sleep", "a")], result


@pytest.mark.parametrize("system", [{"gpu": 1}], indirect=True)
def test_pressure_entrypoint_once_is_readonly_and_uses_selected_preview(pressure, monkeypatch, caplog):
    fixed.test_entrypoint_once_logs_preview_without_starting_cycle(pressure, monkeypatch, caplog)
