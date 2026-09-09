# Generated-By: Codex / gpt-6-astra
"""GPU candidacy exclusions preserve the complete accounting snapshot."""

from dataclasses import replace

import pytest

from llmsvc.policy import PolicySettings, plan_placement
from llmsvc.state import GPUState, Lease, ModelState, Reserve
from test_policy_placement import resident, request, state, stopped


def test_gpu_exclusion_prevents_reentry_to_now_empty_source():
    s=state();before=s.to_dict();guards={0:"relocation_source"}
    assert plan_placement(s,request()).gpu==0
    d=plan_placement(s,request(),gpu_exclusions=guards)
    assert d.gpu==1 and stopped(d)==[] and s.to_dict()==before
    assert guards=={0:"relocation_source"} and s.reserves==()


def test_gpu_exclusion_defaults_and_absent_gpu_keep_original_result():
    s=state(resident("a",budget=80))
    expected=plan_placement(s,request())
    assert expected==plan_placement(s,request(),gpu_exclusions=None)
    assert expected==plan_placement(s,request(),gpu_exclusions={})
    assert expected==plan_placement(s,request(),gpu_exclusions={99:"relocation_source"})


def test_allowed_gpu_eviction_set_retains_cheapest_feasible_ranking():
    s=state(resident("costly",gpu=1,budget=80,score=20),
            resident("cheap-a",gpu=2,budget=40,score=2),resident("cheap-b",gpu=2,budget=40,score=3),
            gpus=tuple(GPUState(i,total_gb=100,external_gb=0) for i in range(3)))
    d=plan_placement(s,request(90),gpu_exclusions={0:"relocation_source"})
    assert d.gpu==2 and stopped(d)==["cheap-a","cheap-b"] and d.eviction_cost==5


def test_all_gpu_candidates_excluded_produces_no_eviction():
    s=state(resident("a",budget=80),resident("b",gpu=1,budget=80))
    d=plan_placement(s,request(),gpu_exclusions={0:"relocation_source",1:"other_constraint"})
    assert not d.actions and d.gpu is None
    assert {(b.gpu,b.reason) for b in d.blocked_by if b.gpu is not None}=={
        (0,"relocation_source"),(1,"other_constraint")}


@pytest.mark.parametrize("exclusive", [0,1])
def test_default_never_uses_shared_gpu_when_exclusive_is_excluded(exclusive):
    d=plan_placement(state(),request(is_default=True),settings=PolicySettings(exclusive_gpu=exclusive),
                     gpu_exclusions={exclusive:"relocation_source"})
    assert not d.actions and d.gpu is None


def test_excluded_gpu_remains_in_cross_gpu_conflict_accounting():
    s=state(resident("a",gpu=0,budget=40),
            leases=(Lease("L","a",1,.4,1,40,"stale"),))
    before=s.to_dict()
    d=plan_placement(s,request(20),gpu_exclusions={0:"relocation_source"})
    assert not d.actions
    assert {b.gpu for b in d.blocked_by if b.reason=="conflicting_accounting"}=={0,1}
    assert s.to_dict()==before


def test_unknown_accounting_location_cannot_be_hidden_by_gpu_filter():
    s=state(replace(resident("unknown"),gpu=None))
    d=plan_placement(s,request(),gpu_exclusions={0:"relocation_source"})
    assert not d.actions and any(b.reason=="unknown_accounting_gpu" for b in d.blocked_by)


@pytest.mark.parametrize("status", ["pending","stale"])
def test_gpu_filter_does_not_bypass_outstanding_target_lease(status):
    s=state(leases=(Lease("L","incoming",0,.6,1,60,status),))
    d=plan_placement(s,request(),gpu_exclusions={0:"relocation_source"})
    assert not d.actions and [b.reason for b in d.blocked_by]==["outstanding_lease"]
    assert s.leases[0].budget_gb==60 and s.leases[0].status==status


def test_remaining_gpu_reserves_and_model_exclusions_still_apply():
    s=state(resident("a",gpu=1,budget=80))
    assert not plan_placement(s,request(),gpu_exclusions={0:"relocation_source"},
                              exclusions={"a":"fault_fenced"}).actions
    s=replace(s,reserves=(Reserve("R",1,20,20000,"actual-owner"),))
    d=plan_placement(s,request(),gpu_exclusions={0:"relocation_source"})
    assert not d.actions and any(b.reason=="reserved" and b.user=="actual-owner" for b in d.blocked_by)


@pytest.mark.parametrize("key", [True,False,-1,"0",0.0,None])
def test_gpu_exclusion_ids_must_be_nonnegative_integers(key):
    with pytest.raises(ValueError,match="gpu_exclusions"):
        plan_placement(state(),request(),gpu_exclusions={key:"relocation_source"})


@pytest.mark.parametrize("reason", [""," ",None,1])
def test_gpu_exclusion_reasons_cannot_silently_disable_a_guard(reason):
    with pytest.raises(ValueError,match="gpu_exclusions"):
        plan_placement(state(),request(),gpu_exclusions={0:reason})
