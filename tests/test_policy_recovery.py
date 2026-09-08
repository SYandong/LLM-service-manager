# Generated-By: Codex / gpt-6-astra
"""Synthetic reserve/cannot-wake recovery preflight using real placement code."""

from dataclasses import replace

import pytest

from llmsvc.policy import PolicySettings
from llmsvc.policy.recovery import plan_relocation, plan_sleeping_recovery
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, Reserve, StateSnapshot


def snapshot(*, recent=True, **kwargs):
    source = ModelState("source", state="sleeping", gpu=1, budget_gb=80,
                        weights_gb=40, resident_gb=2, cold_start_seconds=120)
    data = dict(sampled_at=10000, models=(source,),
                activity=(Activity("source", 9940 if recent else 5000, 3 if recent else 0, in_flight=0),),
                gpus=(GPUState(0, total_gb=100, external_gb=0, free_gb=100),
                      GPUState(1, total_gb=100, external_gb=30, free_gb=68)),
                memory=MemoryState(500, 40))
    return StateSnapshot(**{**data, **kwargs})


def pairs(decision):
    return [(a.kind, a.model, a.gpu) for a in decision.actions]


@pytest.mark.parametrize("reason", ["reserve", "cannot_wake"])
def test_recent_source_gets_preflighted_different_gpu_placement(reason):
    s = snapshot()
    if reason == "reserve":
        s = replace(s, reserves=(Reserve("R", 1, 80, 20000, "owner"),))
    d = plan_relocation(s, model="source", reason=reason)
    assert pairs(d) == [("stop", "source", 1), ("place", "source", 0)]
    assert d.gpu == 0 and d.budget_gb == 80


@pytest.mark.parametrize("reason", ["reserve", "cannot_wake"])
def test_unused_source_stops_without_replacement(reason):
    s = snapshot(recent=False)
    if reason == "reserve":
        s = replace(s, reserves=(Reserve("R", 1, 80, 20000, "owner"),))
    d = plan_relocation(s, model="source", reason=reason)
    assert pairs(d) == [("stop", "source", 1)]
    assert d.gpu is None and d.budget_gb is None


def test_impossible_relocation_causes_zero_source_stop_and_zero_destination_eviction():
    s = snapshot()
    small = replace(s.models[0], name="small", gpu=0, budget_gb=20)
    default = replace(s.models[0], name="default", gpu=0, budget_gb=50, is_default=True)
    s = replace(s, models=s.models+(small,default),
                activity=s.activity+(replace(s.activity[0],model="small"), replace(s.activity[0],model="default")))
    d = plan_relocation(s, model="source", reason="cannot_wake")
    assert not d.actions and d.gpu is None
    assert any(b.reason == "relocation_unavailable" for b in d.blocked_by)


def test_destination_uses_cheapest_feasible_protected_placement_policy():
    s = snapshot()
    victim = replace(s.models[0], name="victim", gpu=0, budget_gb=60)
    s = replace(s, models=s.models+(victim,), activity=s.activity+(replace(s.activity[0], model="victim"),),
                memory=MemoryState(500, 80))
    d = plan_relocation(s, model="source", reason="cannot_wake")
    assert pairs(d) == [("stop", "source", 1), ("stop", "victim", 0), ("place", "source", 0)]


@pytest.mark.parametrize("protection", ["default", "pin", "inflight", "unknown_inflight"])
@pytest.mark.parametrize("reason", ["reserve", "cannot_wake"])
def test_protected_source_never_stops_or_relocates(protection,reason):
    s = snapshot(reserves=(Reserve("R", 1, 80, 20000, "owner"),))
    if protection == "default":
        s = replace(s, models=(replace(s.models[0], is_default=True),))
    elif protection == "pin":
        s = replace(s, pins=(Pin("source", 20000, "owner"),))
    else:
        s = replace(s, activity=(replace(s.activity[0], in_flight=1 if protection == "inflight" else None),))
    assert not plan_relocation(s, model="source", reason=reason).actions


def test_default_on_exclusive_gpu_is_not_stopped_to_move_to_empty_shared_gpu():
    s = snapshot()
    s = replace(s, models=(replace(s.models[0], gpu=0, is_default=True),),
                reserves=(Reserve("R", 0, 80, 20000, "owner"),))
    assert not plan_relocation(s, model="source", reason="reserve").actions


def test_no_wake_deficit_does_nothing_including_exact_budget_boundary():
    s = snapshot()
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], external_gb=20)))
    d = plan_relocation(s, model="source", reason="cannot_wake")
    assert not d.actions and d.blocked_by[0].reason == "wake_budget_available"


def test_expired_or_absent_reserve_does_not_trigger_relocation():
    for reserves in ((), (Reserve("R", 1, 80, 10000, "owner"),)):
        assert not plan_relocation(snapshot(reserves=reserves), model="source", reason="reserve").actions


def test_wake_feasibility_counts_other_sleepers_and_stale_leases():
    s = snapshot()
    s = replace(s, models=(replace(s.models[0], budget_gb=40),),
                leases=(Lease("L", "loading", 1, .4, 1, 40, "stale"),))
    assert plan_relocation(s, model="source", reason="cannot_wake").gpu == 0


def test_confirmed_source_lease_is_not_double_counted_and_can_be_reconciled_after_source_stop():
    s = snapshot(leases=(Lease("L", "source", 1, .8, 1, 80, "confirmed"),))
    assert plan_relocation(s, model="source", reason="cannot_wake").gpu == 0
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], external_gb=20)))
    assert not plan_relocation(s, model="source", reason="cannot_wake").actions


def test_source_with_stale_lease_is_not_relocated_before_core_reconciles():
    s = snapshot(leases=(Lease("L", "source", 1, .8, 1, 80, "stale"),))
    assert not plan_relocation(s, model="source", reason="cannot_wake").actions


def test_destination_reserve_or_unknown_accounting_blocks_move_without_source_stop():
    s = snapshot(reserves=(Reserve("R", 0, 1, 20000, "owner"),))
    assert not plan_relocation(s, model="source", reason="cannot_wake").actions
    s = snapshot()
    s = replace(s, gpus=(replace(s.gpus[0], external_gb=None),s.gpus[1]))
    assert not plan_relocation(s, model="source", reason="cannot_wake").actions


@pytest.mark.parametrize("field", ["last_request_at", "requests_last_hour", "in_flight"])
def test_unknown_source_activity_blocks_recovery(field):
    s = snapshot()
    s = replace(s, activity=(replace(s.activity[0], **{field:None}),))
    assert not plan_relocation(s, model="source", reason="cannot_wake").actions


def test_unknown_source_gpu_or_memory_blocks_recent_relocation():
    s = snapshot()
    assert not plan_relocation(replace(s,memory=MemoryState(None,40)),model="source",reason="cannot_wake").actions
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], external_gb=None)))
    assert not plan_relocation(s, model="source", reason="cannot_wake").actions


def test_unknown_reserve_expiry_is_not_treated_as_proven_eviction_intent():
    s = snapshot(reserves=(Reserve("R",1,80,float("nan"),"owner"),))
    assert not plan_relocation(s,model="source",reason="reserve").actions


def test_sweep_chooses_one_lowest_value_model_and_does_not_double_allocate_destination():
    s = snapshot()
    another = replace(s.models[0], name="hot", cold_start_seconds=300)
    s = replace(s, models=s.models+(another,), memory=MemoryState(500,80),
                activity=s.activity+(replace(s.activity[0],model="hot"),))
    d = plan_sleeping_recovery(s)
    assert pairs(d) == [("stop","source",1),("place","source",0)]


def test_sweep_returns_no_actions_when_all_sleepers_can_wake():
    s = snapshot();s=replace(s,gpus=(s.gpus[0],replace(s.gpus[1],external_gb=0)))
    d = plan_sleeping_recovery(s)
    assert not d.actions and not d.blocked_by


def test_fault_cleanup_is_not_available_to_bypass_default_protection():
    s = snapshot();s=replace(s,models=(replace(s.models[0],is_default=True,health_ok=False),))
    assert not plan_sleeping_recovery(s).actions
    with pytest.raises(ValueError):
        plan_relocation(s,model="source",reason="fault_cleanup")


def test_configured_exclusive_gpu_controls_destination_candidate_rules():
    s=snapshot()
    s=replace(s,gpus=(replace(s.gpus[0],external_gb=1),s.gpus[1],GPUState(2,total_gb=100,external_gb=0)))
    assert plan_relocation(s,model="source",reason="cannot_wake",settings=PolicySettings(exclusive_gpu=2)).gpu == 2


def test_inputs_and_persistent_intents_are_unchanged():
    s=snapshot(reserves=(Reserve("R",1,80,20000,"owner"),));before=s.to_dict()
    assert plan_sleeping_recovery(s) == plan_sleeping_recovery(s)
    assert s.to_dict() == before
