# Generated-By: OpenCode / deepseek-v4.1-flash
"""Placement eviction with keep_value cold-start and never-used fallbacks."""

from llmsvc.policy.placement import plan_placement
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, StateSnapshot


def resident(name, *, budget=40, cold=None, state="sleeping"):
    return ModelState(name, state=state, gpu=0, budget_gb=budget, weights_gb=40,
                      cold_start_seconds=cold)


def snapshot(models, activity):
    return StateSnapshot(
        sampled_at=10000, models=tuple(models), activity=tuple(activity),
        gpus=(GPUState(0, total_gb=100, external_gb=0),),
        memory=MemoryState(500, sum(m.weights_gb for m in models if m.state == "sleeping")))


def request(budget=60):
    return ModelState("incoming", state="stopped", budget_gb=budget)


def stopped(decision):
    return [action.model for action in decision.actions if action.kind == "stop"]


def test_default_cold_start_fallback_evicts_cheapest_known_activity_sleeper():
    cheap = resident("cheap", cold=None)
    busy = resident("busy", cold=None)
    s = snapshot((cheap, busy), (Activity("cheap", 9400, 0, 0, 0),
                                 Activity("busy", 9400, 20, 0, 0)))
    d = plan_placement(s, request())
    assert d.gpu == 0 and stopped(d) == ["cheap"]


def test_never_used_sleeper_is_evicted_before_a_recently_used_one():
    fresh = resident("fresh", cold=None)
    hot = resident("hot", cold=None)
    s = snapshot((fresh, hot), (Activity("fresh", None, 0, 0, 0),
                                Activity("hot", 9900, 0, 0, 0)))
    d = plan_placement(s, request())
    assert d.gpu == 0 and stopped(d) == ["fresh"]


def test_unknown_history_still_blocks_never_used_eviction():
    blocked = resident("blocked", budget=60, cold=None)
    s = snapshot((blocked,), (Activity("blocked", None, None, None, 0),))
    d = plan_placement(s, request())
    assert d.actions == ()
    assert any(blocker.reason == "unknown_activity" for blocker in d.blocked_by)


def test_unknown_aggregates_with_known_timestamp_also_block():
    blocked = resident("blocked", budget=60, cold=None)
    s = snapshot((blocked,), (Activity("blocked", 9400, None, 0, 0),))
    d = plan_placement(s, request())
    assert d.actions == ()
    assert any(blocker.reason == "unknown_activity" for blocker in d.blocked_by)


def test_measured_high_cold_start_is_kept_longer_than_the_default():
    measured = resident("measured", cold=600)
    fallback = resident("fallback", cold=None)
    s = snapshot((measured, fallback), (Activity("measured", 9400, 0, 0, 0),
                                        Activity("fallback", 9400, 0, 0, 0)))
    d = plan_placement(s, request())
    assert stopped(d) == ["fallback"]


def test_measured_low_cold_start_is_evicted_before_the_default():
    measured = resident("measured", cold=10)
    fallback = resident("fallback", cold=None)
    s = snapshot((measured, fallback), (Activity("measured", 9400, 0, 0, 0),
                                        Activity("fallback", 9400, 0, 0, 0)))
    d = plan_placement(s, request())
    assert stopped(d) == ["measured"]
