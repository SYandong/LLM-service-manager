# Generated-By: Codex / gpt-6-astra
"""CPU-only synthetic placement fixtures, never claimed as historical traces."""

import json
from pathlib import Path
from time import perf_counter

import pytest

from llmsvc.policy import PolicySettings, plan_placement
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Reserve, StateSnapshot

CASES = json.loads((Path(__file__).parent / "fixtures/policy/placement.json").read_text())["cases"]


def replay(case):
    gpus = tuple(GPUState(g["index"], total_gb=g["total_gb"], external_gb=g.get("external_gb", 0))
                 for g in case["gpus"])
    models = tuple(ModelState(m["name"], state=m.get("state", "sleeping"), gpu=m.get("gpu", 0),
                              budget_gb=m.get("budget_gb", 40), weights_gb=40,
                              cold_start_seconds=m.get("keep_value", 1)*11,
                              is_default=m.get("is_default", False)) for m in case["models"])
    activity = tuple(Activity(m.name, 9400, 0, in_flight=0) for m in models)
    leases = tuple(Lease(str(i), l["model"], l["gpu"], l["budget_gb"]/next(g.total_gb for g in gpus if g.index==l["gpu"]),
                         1, l["budget_gb"], l.get("state", "pending")) for i,l in enumerate(case.get("leases", [])))
    reserves = tuple(Reserve(str(i), r["gpu"], r["size_gb"], 20000, "synthetic-owner")
                     for i,r in enumerate(case.get("reserves", [])))
    snapshot = StateSnapshot(sampled_at=10000, gpus=gpus, models=models, activity=activity,
                             leases=leases, reserves=reserves,
                             memory=MemoryState(500, sum(m.weights_gb for m in models if m.state=="sleeping")))
    r=case["request"]
    request=ModelState(r["model"], state="stopped", budget_gb=r["budget_gb"], is_default=r.get("is_default", False))
    exclusive=next(g["index"] for g in case["gpus"] if g.get("exclusive"))
    decision=plan_placement(snapshot, request, settings=PolicySettings(exclusive_gpu=exclusive))
    expected=case["expect"]
    assert decision.gpu == expected["gpu"]
    if "actions" in expected:
        assert [(a.kind,a.model) for a in decision.actions] == expected["actions"]
    if "stopped" in expected:
        assert [a.model for a in decision.actions if a.kind=="stop"] == expected["stopped"]
    if request.is_default and decision.gpu is not None:
        assert decision.gpu == exclusive


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_placement_replay(case):
    assert case["provenance"]["kind"] == "synthetic_regression"
    replay(case)


def test_six_placement_replays_finish_below_five_seconds():
    assert len(CASES) >= 6
    start = perf_counter()
    for case in CASES:
        replay(case)
    assert perf_counter()-start < 5
