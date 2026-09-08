# Generated-By: Codex / gpt-6-astra
"""Synthetic pressure observations, not one-week production calibration."""

from dataclasses import replace

import pytest

from llmsvc.policy import PolicySettings
from llmsvc.policy.pressure import plan_pressure_sleep
from llmsvc.state import Activity, GPUProcess, GPUState, MemoryState, ModelState, Pin, StateSnapshot


def model(name="a", gpu=1, **kwargs):
    return ModelState(name, state="awake", gpu=gpu, budget_gb=40, weights_gb=40,
                      resident_gb=40, cold_start_seconds=kwargs.pop("cold_start_seconds", 120), **kwargs)


def snapshot(*models, idle=400, **kwargs):
    data = dict(sampled_at=10000, models=models,
                activity=tuple(Activity(m.name, 10000-idle, 1, in_flight=0) for m in models),
                gpus=(GPUState(0, total_gb=100, free_gb=50, external_gb=0),
                      GPUState(1, total_gb=100, free_gb=50, external_gb=0)),
                memory=MemoryState(500, 0))
    return StateSnapshot(**{**data, **kwargs})


def pairs(decision):
    return [(a.kind, a.model) for a in decision.actions]


def test_shared_five_minute_ttl_and_exclusive_sixty_minute_ttl():
    assert pairs(plan_pressure_sleep(snapshot(model(), idle=300))) == [("sleep", "a")]
    assert not plan_pressure_sleep(snapshot(model(gpu=0), idle=600)).actions
    assert pairs(plan_pressure_sleep(snapshot(model(gpu=0), idle=3600))) == [("sleep", "a")]


def test_per_gpu_ttl_and_exclusive_index_are_configurable():
    config = PolicySettings(exclusive_gpu=1, exclusive_ttl_seconds=1000, shared_ttl_seconds=100)
    assert not plan_pressure_sleep(snapshot(model(), idle=500), settings=config).actions
    assert plan_pressure_sleep(snapshot(model(gpu=0), idle=100), settings=config).actions


@pytest.mark.parametrize("changes", [{"external_gb": 0.1}, {"free_gb": 9},
                                     {"external_processes": (GPUProcess(101, 0),)}])
def test_any_external_process_or_low_free_triggers_before_ttl(changes):
    s = snapshot(model(), idle=5)
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], **changes)))
    assert pairs(plan_pressure_sleep(s)) == [("sleep", "a")]
    assert plan_pressure_sleep(s).actions[0].reason == "shared_gpu_pressure"


def test_pressure_threshold_is_configurable_and_exact_boundary_is_not_low():
    s = snapshot(model(), idle=100)
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], free_gb=20)))
    assert not plan_pressure_sleep(s, settings=PolicySettings(shared_free_threshold_gb=20)).actions
    assert plan_pressure_sleep(s, settings=PolicySettings(shared_free_threshold_gb=21)).actions


def test_exclusive_card_ignores_shared_pressure_signal_before_its_ttl():
    s = snapshot(model(gpu=0), idle=600)
    s = replace(s, gpus=(replace(s.gpus[0], free_gb=1, external_gb=50), s.gpus[1]))
    assert not plan_pressure_sleep(s).actions


def test_one_cold_candidate_then_reobserve_and_stop_when_pressure_clears():
    s = snapshot(model("cold"), model("hot", cold_start_seconds=300), idle=100)
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], free_gb=1)))
    assert pairs(plan_pressure_sleep(s)) == [("sleep", "cold")]
    updated = replace(s, models=(replace(s.models[0], state="sleeping"), s.models[1]),
                      memory=MemoryState(460, 40),
                      gpus=(s.gpus[0], replace(s.gpus[1], free_gb=39)))
    assert not plan_pressure_sleep(updated).actions


def test_default_is_last_even_when_retention_score_is_lower():
    s = snapshot(model("default", is_default=True, cold_start_seconds=1),
                 model("ordinary", cold_start_seconds=100))
    assert pairs(plan_pressure_sleep(s)) == [("sleep", "ordinary")]


@pytest.mark.parametrize("protection", ["pin", "inflight", "unknown_inflight"])
def test_protection_survives_pressure_and_ttl(protection):
    s = snapshot(model())
    if protection == "pin":
        s = replace(s, pins=(Pin("a", 20000, "owner"),))
    else:
        s = replace(s, activity=(replace(s.activity[0], in_flight=1 if protection == "inflight" else None),))
    assert not plan_pressure_sleep(s).actions


@pytest.mark.parametrize("field", ["total_gb", "external_gb", "free_gb"])
def test_unknown_gpu_pressure_observation_blocks(field):
    s = snapshot(model())
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], **{field: None})))
    d = plan_pressure_sleep(s)
    assert not d.actions and any(b.reason == "unknown_gpu_pressure" for b in d.blocked_by)


def test_unknown_memory_blocks_pressure_instead_of_guessing_admission():
    s = snapshot(model(), memory=MemoryState(None, 0))
    assert not plan_pressure_sleep(s).actions


def test_memory_admission_precedes_sleep_and_default_remains_awake_on_failure():
    s = snapshot(model(), memory=MemoryState(160, 0))
    assert pairs(plan_pressure_sleep(s)) == [("stop", "a")]
    s = replace(s, models=(replace(s.models[0], is_default=True),))
    d = plan_pressure_sleep(s)
    assert not d.actions and any(b.reason == "memory_budget" for b in d.blocked_by)


def test_synthetic_next_sample_selects_pressure_action_within_thirty_seconds():
    # Simulates a 15-second snapshot interval; not a live action latency claim.
    before = snapshot(model(), idle=100)
    assert not plan_pressure_sleep(before).actions
    after = replace(before, sampled_at=before.sampled_at+15,
                    gpus=(before.gpus[0], replace(before.gpus[1], external_gb=20)))
    assert pairs(plan_pressure_sleep(after)) == [("sleep", "a")]
    assert after.sampled_at-before.sampled_at < 30


def test_snapshot_is_unchanged_and_repeatable():
    s = snapshot(model()); before = s.to_dict()
    assert plan_pressure_sleep(s) == plan_pressure_sleep(s)
    assert s.to_dict() == before
