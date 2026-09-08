# Generated-By: Codex / gpt-6-astra
"""Protection and cumulative admission regressions, using synthetic snapshots."""

from dataclasses import replace

import pytest

from llmsvc.policy import (
    PolicySettings, plan_free, plan_idle_sleep, plan_memory_pressure,
    plan_reserve, reload_admission,
)
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot


def model(name, **changes):
    return replace(ModelState(name, state="awake", gpu=0, budget_gb=40,
                              weights_gb=40, resident_gb=40, cold_start_seconds=120), **changes)


def snapshot(*models, **changes):
    return replace(StateSnapshot(
        sampled_at=10000, models=models,
        gpus=(GPUState(0, total_gb=100, free_gb=60, external_gb=0),),
        activity=tuple(Activity(m.name, last_request_at=9000, requests_last_hour=0, in_flight=0) for m in models),
        memory=MemoryState(host_available_gb=500,
                           sleeping_weights_gb=sum(m.weights_gb or 0 for m in models if m.state == "sleeping")),
    ), **changes)


def actions(decision):
    return [(a.kind, a.model) for a in decision.actions]


def test_default_low_score_still_sleeps_last():
    s = snapshot(model("default", is_default=True, cold_start_seconds=1),
                 model("ordinary", cold_start_seconds=100))
    assert actions(plan_free(s)) == [("sleep", "ordinary"), ("sleep", "default")]


def test_free_need_stops_when_estimated_release_met_and_filters_gpu():
    s = snapshot(model("a"), model("b"), model("other", gpu=1))
    d = plan_free(s, gpu=0, need_gb=35)
    assert actions(d) == [("sleep", "a")]
    assert d.estimated_freed_gb == 38


def test_free_requires_strictly_more_than_thirty_seconds_idle():
    s = snapshot(model("a"), activity=(Activity("a", 9970, 0, in_flight=0),))
    assert not plan_free(s).actions
    assert actions(plan_free(replace(s, sampled_at=10001))) == [("sleep", "a")]


@pytest.mark.parametrize("operation", [plan_free, plan_idle_sleep, plan_memory_pressure,
                                        lambda s: plan_reserve(s, gpu=0)])
@pytest.mark.parametrize("state", ["awake", "sleeping"])
@pytest.mark.parametrize("protection", ["pin", "inflight", "unknown_inflight"])
def test_protection_applies_to_all_ordinary_policy_branches(operation, state, protection):
    s = snapshot(model("a", state=state), memory=MemoryState(100, 250))
    if protection == "pin":
        s = replace(s, pins=(Pin("a", 11000, "test-owner"),))
    else:
        s = replace(s, activity=(replace(s.activity[0], in_flight=2 if protection == "inflight" else None),))
    assert not operation(s).actions


def test_pin_expires_at_snapshot_time():
    s = snapshot(model("a"), pins=(Pin("a", 10000, "test-owner"),))
    assert actions(plan_idle_sleep(s)) == [("sleep", "a")]
    assert not plan_idle_sleep(replace(s, pins=(Pin("a", 10001, "test-owner"),))).actions


def test_admission_reclaims_sleepers_before_sleep():
    s = snapshot(model("awake"), model("sleeper", state="sleeping"),
                 memory=MemoryState(170, 40))
    assert actions(plan_free(s)) == [("stop", "sleeper"), ("sleep", "awake")]


def test_known_ram_failure_stops_ordinary_without_sleep():
    s = snapshot(model("a"), memory=MemoryState(160, 0))
    assert actions(plan_free(s)) == [("stop", "a")]


def test_default_ram_failure_remains_awake_and_blocked():
    d = plan_free(snapshot(model("default", is_default=True), memory=MemoryState(160, 0)))
    assert not d.actions
    assert any(b.reason == "memory_budget" for b in d.blocked_by)


def test_protected_sleepers_are_not_reclaimed_for_admission():
    s = snapshot(model("a"), model("default", state="sleeping", is_default=True),
                 model("pinned", state="sleeping"), memory=MemoryState(160, 80),
                 pins=(Pin("pinned", 20000, "test-owner"),))
    assert actions(plan_free(s)) == [("stop", "a")]


def test_ram_free_only_stops_sleepers_and_reports_ram_estimate():
    d = plan_free(snapshot(model("awake"), model("sleeping", state="sleeping")), ram=True)
    assert actions(d) == [("stop", "sleeping")]
    assert d.estimated_freed_gb == 40


def test_memory_pressure_stops_lowest_score_until_both_limits_hold():
    s = snapshot(model("hot", state="sleeping", cold_start_seconds=500),
                 model("cold", state="sleeping"), memory=MemoryState(140, 80, budget_gb=60))
    assert actions(plan_memory_pressure(s)) == [("stop", "cold")]


def test_default_never_stopped_under_unresolved_memory_pressure():
    d = plan_memory_pressure(snapshot(model("default", state="sleeping", is_default=True),
                                      memory=MemoryState(100, 250)))
    assert not d.actions
    assert {b.reason for b in d.blocked_by} == {"default_model", "memory_budget"}


def test_reserve_only_clears_unprotected_sleepers_on_target_gpu():
    s = snapshot(model("awake"), model("sleep", state="sleeping"),
                 model("default", state="sleeping", is_default=True),
                 model("elsewhere", gpu=1, state="sleeping"))
    assert actions(plan_reserve(s, gpu=0)) == [("stop", "sleep")]


def test_reload_checks_batch_not_individual_admission_and_never_reclaims():
    s = snapshot(model("a"), model("b"), model("sleep", state="sleeping"),
                 memory=MemoryState(210, 40))
    d = reload_admission(s)
    assert not d.actions
    assert any(b.reason == "memory_budget" for b in d.blocked_by)


def test_reload_default_awake_allowed_but_pin_and_inflight_block():
    s = snapshot(model("default", is_default=True))
    assert not reload_admission(s).blocked_by
    assert reload_admission(replace(s, pins=(Pin("default", 20000, "owner"),))).blocked_by
    assert reload_admission(replace(s, activity=(replace(s.activity[0], in_flight=1),))).blocked_by


@pytest.mark.parametrize("memory", [MemoryState(None, 0), MemoryState(500, None)])
def test_unknown_memory_blocks_sleep_without_direct_stop_fallback(memory):
    d = plan_free(snapshot(model("a"), memory=memory))
    assert not d.actions
    assert any(b.reason == "unknown_memory" for b in d.blocked_by)


@pytest.mark.parametrize("changes", [{"sampled_at": None}, {"errors": ("collector unavailable",)}])
def test_incomplete_snapshot_blocks_mutating_decisions(changes):
    s = snapshot(model("a"), **changes)
    for operation in (plan_free, plan_idle_sleep, plan_memory_pressure, reload_admission):
        assert not operation(s).actions
        assert operation(s).blocked_by


def test_decisions_do_not_mutate_snapshot_and_are_repeatable():
    s = snapshot(model("a"), model("s", state="sleeping"), memory=MemoryState(170, 40))
    before = s.to_dict()
    assert plan_free(s) == plan_free(s)
    assert s.to_dict() == before


def test_cumulative_sleep_admission_does_not_oversubscribe_ram():
    s = snapshot(model("a"), model("b", is_default=True), memory=MemoryState(210, 0))
    assert actions(plan_free(s)) == [("sleep", "a"), ("stop", "a"), ("sleep", "b")]


@pytest.mark.parametrize("bad", [None, True, -1, float("inf")])
def test_invalid_settings_rejected(bad):
    with pytest.raises(ValueError):
        PolicySettings(sleeping_residual_gb=bad)
