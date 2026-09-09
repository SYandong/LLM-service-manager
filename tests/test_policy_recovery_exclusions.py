# Generated-By: Codex / gpt-6-astra
"""Ordinary recovery eligibility remains distinct from intent/fault authority."""

from dataclasses import replace

import pytest

from llmsvc.policy import plan_relocation, plan_sleeping_recovery
from llmsvc.state import Activity, MemoryState, Pin
from test_policy_exclusions import legacy_plan
from test_policy_recovery import snapshot


def run(planner, s, **kwargs):
    if planner is plan_relocation:
        kwargs.update(model="source",reason="cannot_wake")
    return planner(s,**kwargs)


@pytest.mark.parametrize("planner", [plan_relocation, plan_sleeping_recovery])
@pytest.mark.parametrize("recent", [False, True])
def test_excluded_source_cannot_retire_or_relocate(planner,recent):
    s=snapshot(recent=recent);before=s.to_dict()
    guards={"source":"fault_fenced"}
    d=run(planner,s,exclusions=guards)
    assert not d.actions and any(b.model=="source" and b.reason=="fault_fenced" for b in d.blocked_by)
    assert s.to_dict()==before and guards=={"source":"fault_fenced"}


@pytest.mark.parametrize("planner", [plan_relocation, plan_sleeping_recovery])
@pytest.mark.parametrize("recent", [False, True])
def test_recovery_optional_input_defaults_preserve_original_decisions(planner,recent):
    s=snapshot(recent=recent)
    assert run(planner,s)==run(planner,s,exclusions=None)==run(planner,s,exclusions={})


@pytest.mark.parametrize("planner", [plan_relocation, plan_sleeping_recovery])
@pytest.mark.parametrize("until", [9999,11000,float("nan")])
def test_recovery_pin_overlap_keeps_real_intent_and_operational_reason(planner,until):
    pin=Pin("source",until,"actual-owner")
    s=snapshot(pins=(pin,));original=s.pins
    d=run(planner,s,exclusions={"source":"unconfirmed_account"})
    reasons=[b.reason for b in d.blocked_by if b.model=="source"]
    assert reasons==(["unconfirmed_account"] if until==9999 else ["pinned_until","unconfirmed_account"])
    assert not d.actions and s.pins is original and s.pins[0] is pin and pin.by=="actual-owner"


def destination_scene():
    s=snapshot()
    source=replace(s.models[0],budget_gb=40)
    cold=replace(source,name="cold",gpu=0,budget_gb=40,cold_start_seconds=11)
    hot=replace(source,name="hot",gpu=0,budget_gb=60,cold_start_seconds=110)
    return replace(s,models=(source,cold,hot),memory=MemoryState(500,120),
                   activity=s.activity+(Activity("cold",9400,0,in_flight=0),Activity("hot",9400,0,in_flight=0)),
                   gpus=(s.gpus[0],replace(s.gpus[1],external_gb=70)))


@pytest.mark.parametrize("planner", [plan_relocation, plan_sleeping_recovery])
def test_recovery_propagates_victim_exclusions_through_nested_placement(planner):
    s=destination_scene();before=s.to_dict();guards={"cold":"unconfigured_model"}
    kwargs={"model":"source","reason":"cannot_wake"} if planner is plan_relocation else {}
    expected=legacy_plan(planner,s,guards,**kwargs)
    d=run(planner,s,exclusions=guards)
    assert [(a.kind,a.model,a.gpu) for a in d.actions]==[("stop","source",1),("stop","hot",0),("place","source",0)]
    assert d==expected and d.budget_gb==40
    assert s.to_dict()==before


@pytest.mark.parametrize("planner", [plan_relocation, plan_sleeping_recovery])
def test_all_destination_victims_excluded_means_no_source_stop(planner):
    s=destination_scene();before=s.to_dict()
    d=run(planner,s,exclusions={"cold":"unconfirmed_account","hot":"operation_in_progress"})
    assert not d.actions and d.gpu is None
    assert any(b.reason=="relocation_unavailable" for b in d.blocked_by)
    assert s.to_dict()==before


@pytest.mark.parametrize("protection", ["default","inflight","unknown_inflight"])
def test_recovery_exclusions_do_not_bypass_normal_source_protection(protection):
    s=snapshot()
    if protection=="default":
        s=replace(s,models=(replace(s.models[0],is_default=True),))
    else:
        s=replace(s,activity=(replace(s.activity[0],in_flight=1 if protection=="inflight" else None),))
    assert not plan_relocation(s,model="source",reason="cannot_wake",exclusions={"absent":"fault_fenced"}).actions
