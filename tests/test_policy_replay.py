# Generated-By: Codex / gpt-6-astra
"""Offline synthetic replay; historical provenance is deliberately separate."""

import json
from pathlib import Path
from time import perf_counter

import pytest

from llmsvc.policy import plan_free, plan_idle_sleep, plan_memory_pressure, plan_reserve, reload_admission
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot

CASES = json.loads((Path(__file__).parent / "fixtures/policy/scenarios.json").read_text())["cases"]


def replay(case):
    models, activity, pins = [], [], []
    for raw in case["models"]:
        name = raw["name"]
        idle = raw.get("idle_seconds", 1000)
        count = raw.get("requests_last_hour", 0)
        cold_start = (raw["keep_value"] * (1 + idle / 60) / (1 + count)
                      if "keep_value" in raw else 120)
        models.append(ModelState(name, state=raw.get("state", "awake"), gpu=raw.get("gpu", 0),
                                 budget_gb=raw.get("budget_gb", 40), weights_gb=raw.get("weight_gb", 40),
                                 resident_gb=raw.get("budget_gb", 40), is_default=raw.get("is_default", False),
                                 cold_start_seconds=cold_start))
        activity.append(Activity(name, last_request_at=10000-idle, requests_last_hour=count,
                                 in_flight=raw.get("in_flight", 0)))
        if raw.get("pinned"):
            pins.append(Pin(name, 11000, "synthetic-owner"))
    memory = case.get("memory", {})
    s = StateSnapshot(sampled_at=10000, models=tuple(models), activity=tuple(activity), pins=tuple(pins),
                      gpus=(GPUState(0, total_gb=100, free_gb=50, external_gb=0),),
                      memory=MemoryState(memory.get("available_gb", 500),
                                         sum(m.weights_gb for m in models if m.state=="sleeping"),
                                         memory.get("sleeping_budget_gb", 200),
                                         memory.get("minimum_available_gb", 150)))
    operation = {"free":plan_free, "idle":plan_idle_sleep, "memory":plan_memory_pressure,
                 "reserve":plan_reserve, "reload":reload_admission}[case["operation"]]
    before = s.to_dict()
    d = operation(s, **case.get("request", {}))
    assert s.to_dict() == before
    expected = case["expect"]
    if "actions" in expected:
        assert [(a.kind, a.model) for a in d.actions] == expected["actions"]
    for key, kind in (("slept", "sleep"), ("stopped", "stop")):
        if key in expected:
            assert [a.model for a in d.actions if a.kind==kind] == expected[key]
    assert set(expected.get("blocked", [])) <= {b.model for b in d.blocked_by}
    assert set(expected.get("reasons", [])) <= {b.reason for b in d.blocked_by}
    assert all(not next(m for m in models if m.name==a.model).is_default for a in d.actions if a.kind=="stop")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_synthetic_replay(case):
    assert case["provenance"]["kind"] == "synthetic_regression"
    replay(case)


def test_replay_suite_has_six_cases_and_finishes_within_five_seconds():
    assert len(CASES) >= 6
    start = perf_counter()
    for case in CASES:
        replay(case)
    assert perf_counter() - start < 5
