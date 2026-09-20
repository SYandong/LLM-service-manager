"""Placement pool and packing order (DESIGN §4.2 steps 1–2)."""

import pytest

from llmsvc.policy import PolicySettings
from llmsvc.policy.placement import plan_placement
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, StateSnapshot


def resident(name, gpu=0, budget=40, score=1, **kwargs):
    return ModelState(name, state=kwargs.pop("state", "sleeping"), gpu=gpu, budget_gb=budget,
                      weights_gb=40, cold_start_seconds=score * 11, **kwargs)


def gpus(*external):
    return tuple(GPUState(index, total_gb=100, external_gb=value) for index, value in enumerate(external))


def state(*models, cards=gpus(0, 0, 0), **kwargs):
    defaults = dict(sampled_at=10000, models=models, gpus=cards,
                    activity=tuple(Activity(m.name, last_request_at=9400, requests_last_hour=0, in_flight=0,
                                            by=("synthetic-owner",)) for m in models),
                    memory=MemoryState(500, sum(m.weights_gb or 0 for m in models if m.state == "sleeping")))
    return StateSnapshot(**{**defaults, **kwargs})


def request(budget=60, **kwargs):
    return ModelState("incoming", state="stopped", budget_gb=budget, **kwargs)


def stopped(decision):
    return [a.model for a in decision.actions if a.kind == "stop"]


def test_pool_restricts_destinations_but_keeps_accounting_everywhere():
    pool = PolicySettings(placement_gpus=(0, 1))
    d = plan_placement(state(resident("a", gpu=1, budget=80)), request(60), settings=pool)
    assert d.gpu == 0 and stopped(d) == []
    # GPU2 is empty but outside the pool, so the request evicts inside the pool instead.
    s = state(resident("a", budget=80), resident("b", gpu=1, budget=80, score=3))
    d = plan_placement(s, request(60), settings=pool)
    assert d.gpu == 0 and stopped(d) == ["a"]
    # External usage leaves 80 on each pooled card even after eviction; the empty
    # GPU2 would fit but is outside the pool, so nothing is placed or evicted.
    s = state(resident("a", budget=80), resident("b", gpu=1, budget=80, score=3), cards=gpus(20, 20, 0))
    d = plan_placement(s, request(90), settings=pool)
    assert d.gpu is None and d.actions == ()
    assert ("outside_placement_pool", 2) in [(b.reason, b.gpu) for b in d.blocked_by]
    assert plan_placement(s, request(90)).gpu == 2


def test_pool_of_none_keeps_every_observed_gpu():
    d = plan_placement(state(resident("a", budget=80), resident("b", gpu=1, budget=80)), request(60))
    assert d.gpu == 2 and stopped(d) == []


def test_best_fit_prefers_the_tightest_hole_and_first_fit_the_lowest_index():
    s = state(resident("a", gpu=1, budget=30), resident("b", gpu=2, budget=10))
    assert plan_placement(s, request(60)).gpu == 0
    assert plan_placement(s, request(60), settings=PolicySettings(placement_fit="best_fit")).gpu == 1


def test_best_fit_ties_fall_back_to_the_lowest_index():
    s = state(resident("a", gpu=1, budget=30), resident("b", gpu=2, budget=30))
    assert plan_placement(s, request(60), settings=PolicySettings(placement_fit="best_fit")).gpu == 1


def test_best_fit_counts_external_usage_as_part_of_the_hole():
    s = state(resident("a", gpu=1, budget=30), cards=gpus(0, 0, 35))
    settings = PolicySettings(placement_fit="best_fit", shared_external_threshold_gb=50)
    assert plan_placement(s, request(60), settings=settings).gpu == 2
    # Below the threshold the same card is a legitimate target; at it, it is not.
    settings = PolicySettings(placement_fit="best_fit", shared_external_threshold_gb=35)
    assert plan_placement(s, request(60), settings=settings).gpu == 1


def test_best_fit_never_evicts_for_packing_and_default_stays_exclusive():
    settings = PolicySettings(placement_fit="best_fit", exclusive_gpu=0)
    d = plan_placement(state(resident("a", budget=80, score=0), cards=gpus(0, 0)), request(), settings=settings)
    assert d.gpu == 1 and stopped(d) == []
    d = plan_placement(state(resident("a", gpu=1, budget=10), cards=gpus(0, 0)), request(is_default=True),
                       settings=settings)
    assert d.gpu == 0


def test_exclusive_gpu_moves_with_the_pool():
    settings = PolicySettings(exclusive_gpu=2, placement_gpus=(2, 3))
    s = state(resident("a", budget=10), cards=gpus(0, 0, 0, 0))
    assert plan_placement(s, request(is_default=True), settings=settings).gpu == 2
    assert plan_placement(s, request(60), settings=settings).gpu == 2


@pytest.mark.parametrize("kwargs", [
    {"placement_gpus": ()}, {"placement_gpus": [0, 1]}, {"placement_gpus": (0, 0)}, {"placement_gpus": (0, True)},
    # The pool must still contain the exclusive GPU where one is configured.
    {"placement_gpus": (1, 2), "exclusive_gpu": 0},
    {"placement_gpus": (0, -1)}, {"placement_fit": "worst_fit"}, {"placement_fit": None},
])
def test_invalid_pool_or_fit_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PolicySettings(**kwargs)
