# Generated-By: Codex / gpt-6-astra
"""Synthetic pressure/recovery replay, separate from real calibration evidence."""

import json
from pathlib import Path
from time import perf_counter

import pytest

from llmsvc.policy import plan_pressure_sleep, plan_relocation
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Reserve, StateSnapshot

CASES = json.loads((Path(__file__).parent / "fixtures/policy/pressure_recovery.json").read_text())["cases"]


def replay(case):
    recovery = case["operation"] == "recovery"
    gpu = case.get("gpu", 1)
    idle = case.get("idle", 60 if case.get("recent", True) else 5000)
    source = ModelState("source", state="sleeping" if recovery else "awake", gpu=gpu,
                        budget_gb=80, weights_gb=40, resident_gb=2 if recovery else 80,
                        is_default=case.get("is_default", False), cold_start_seconds=120)
    reserves = []
    if case.get("reserved"):
        reserves.append(Reserve("R", gpu, 80, 20000, "synthetic-owner"))
    if case.get("destination_reserved"):
        reserves.append(Reserve("destination", 0, 80, 20000, "synthetic-owner"))
    snapshot = StateSnapshot(sampled_at=10000, models=(source,),
                             activity=(Activity("source", 10000-idle, 1 if case.get("recent", True) else 0,
                                                in_flight=0),),
                             gpus=(GPUState(0, total_gb=100, free_gb=100, external_gb=0),
                                   GPUState(1, total_gb=100, free_gb=50,
                                            external_gb=case.get("external_gb", 0))),
                             memory=MemoryState(500, 40 if recovery else 0), reserves=tuple(reserves))
    before = snapshot.to_dict()
    decision = (plan_relocation(snapshot, model="source", reason=case["reason"])
                if recovery else plan_pressure_sleep(snapshot))
    assert [a.kind for a in decision.actions] == case["expected"]
    if "destination" in case:
        assert decision.gpu == case["destination"]
    assert snapshot.to_dict() == before


@pytest.mark.parametrize("case", CASES, ids=lambda c:c["id"])
def test_pressure_recovery_replay(case):
    assert case["provenance"]["kind"] == "synthetic_regression"
    replay(case)


def test_replays_complete_within_five_seconds():
    start = perf_counter()
    for case in CASES:
        replay(case)
    assert perf_counter()-start < 5
