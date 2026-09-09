# Generated-By: Codex / gpt-6-astra
"""Destination profile/cold-RAM preflight must precede any source effect."""

from dataclasses import replace

import pytest

from llmsvc.policy import plan_relocation, plan_sleeping_recovery
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin
from test_policy_recovery import snapshot


def profile(**changes):
    return replace(ModelState("source",state="stopped",util=.8,budget_gb=80,weights_gb=40),**changes)


def run(planner,s,profiles=None,**kwargs):
    if planner is plan_relocation:
        kwargs.update(model="source",reason="cannot_wake")
    return planner(s,replacement_requests=profiles,**kwargs)


def profile_gap_snapshot():
    s=snapshot()
    source=replace(s.models[0],util=.4)
    resident=ModelState("protected",state="sleeping",gpu=0,budget_gb=100,weights_gb=20,
                        is_default=True,cold_start_seconds=120)
    return replace(s,models=(source,resident),gpus=(GPUState(0,total_gb=200,external_gb=0,free_gb=100),s.gpus[1]),
                   memory=MemoryState(500,60),activity=s.activity+(Activity("protected",9000,0,in_flight=0),),
                   leases=(Lease("source-lease","source",1,.8,20000,80,"confirmed"),))


@pytest.mark.parametrize("planner", [plan_relocation,plan_sleeping_recovery])
def test_real_replacement_floor_prevents_source_stop_when_observed_profile_would_fit(planner):
    s=profile_gap_snapshot();before=s.to_dict()
    legacy = (planner(s,model="source",reason="cannot_wake") if planner is plan_relocation else planner(s))
    assert legacy.actions and legacy.budget_gb==80
    d=run(planner,s,{"source":profile()})
    assert not d.actions and d.gpu is None
    assert any(b.reason=="relocation_unavailable" for b in d.blocked_by)
    assert s.to_dict()==before


@pytest.mark.parametrize("planner", [plan_relocation,plan_sleeping_recovery])
def test_post_source_stop_cold_ram_failure_has_zero_source_actions(planner):
    s=snapshot(memory=MemoryState(100,40));before=s.to_dict()
    legacy = (planner(s,model="source",reason="cannot_wake") if planner is plan_relocation else planner(s))
    assert legacy.actions
    d=run(planner,s,{"source":profile()})
    assert not d.actions and any(b.reason=="host_memory_floor" for b in d.blocked_by)
    assert s.to_dict()==before


def test_replacement_floor_never_invents_a_source_trigger():
    s=profile_gap_snapshot()
    # Observed source uses80 of200 GiB;100 external leaves100. Applying .8 to
    # this source would invent160 GiB demand. Only the destination uses that floor.
    s=replace(s,gpus=(s.gpus[0],replace(s.gpus[1],total_gb=200,external_gb=100)))
    d=plan_relocation(s,model="source",reason="cannot_wake",replacement_requests={"source":profile()})
    assert not d.actions and [b.reason for b in d.blocked_by]==["wake_budget_available"]


def test_destination_uses_real_floor_without_replacing_observed_source_metadata(monkeypatch):
    import llmsvc.policy.recovery as recovery
    s=profile_gap_snapshot()
    s=replace(s,models=(s.models[0],replace(s.models[1],budget_gb=30)))
    before=s.to_dict();seen=[];original=recovery.plan_placement
    def observe(hypothetical,request,**kwargs):
        stopped_source=next(m for m in hypothetical.models if m.name=="source")
        assert (stopped_source.state,stopped_source.util,stopped_source.budget_gb,stopped_source.weights_gb)==("stopped",.4,80,40)
        assert (request.util,request.budget_gb,request.weights_gb)==(.8,80,40)
        assert hypothetical.gpus is s.gpus and hypothetical.leases is s.leases
        seen.append(request)
        return original(hypothetical,request,**kwargs)
    monkeypatch.setattr(recovery,"plan_placement",observe)
    d=recovery.plan_relocation(s,model="source",reason="cannot_wake",replacement_requests={"source":profile()})
    assert seen and d.gpu==0 and d.budget_gb==160
    assert [(a.kind,a.model) for a in d.actions]==[("stop","source"),("place","source")]
    assert s.to_dict()==before


@pytest.mark.parametrize("available,weight,allowed", [(150,40,True),(149,40,False),(170,60,True),(169,60,False)])
def test_cold_ram_uses_observed_source_release_and_replacement_weight(available,weight,allowed):
    s=snapshot(memory=MemoryState(available,40))
    d=run(plan_relocation,s,{"source":profile(weights_gb=weight)})
    assert bool(d.actions) is allowed
    if not allowed:
        assert any(b.reason=="host_memory_floor" for b in d.blocked_by)


@pytest.mark.parametrize("status", ["pending","stale"])
@pytest.mark.parametrize("weight,allowed", [(40,True),(50,False)])
def test_pending_start_weights_share_cold_host_headroom(status,weight,allowed):
    s=snapshot(memory=MemoryState(190,40),leases=(Lease("L","loading",1,.05,1,5,status),))
    before=s.to_dict()
    d=run(plan_relocation,s,{"source":profile(),"loading":ModelState("loading",weights_gb=weight)})
    assert bool(d.actions) is allowed
    if not allowed:assert any(b.reason=="host_memory_floor" for b in d.blocked_by)
    assert s.to_dict()==before


@pytest.mark.parametrize("entry", [None,ModelState("loading"),ModelState("wrong",weights_gb=1),ModelState("loading",weights_gb=-1)])
def test_unknown_pending_weight_never_releases_source(entry):
    s=snapshot(leases=(Lease("L","loading",1,.05,1,5,"stale"),))
    profiles={"source":profile()}
    if entry is not None:profiles["loading"]=entry
    d=run(plan_relocation,s,profiles)
    assert not d.actions and any(b.model=="loading" and b.reason=="unknown_memory" for b in d.blocked_by)


def test_each_pending_or_stale_record_matches_existing_runtime_headroom_charge():
    s=snapshot(memory=MemoryState(180,40),leases=(Lease("L1","loading",1,.05,1,5,"pending"),
                                                Lease("L2","loading",1,.05,1,5,"stale")))
    d=run(plan_relocation,s,{"source":profile(),"loading":ModelState("loading",weights_gb=20)})
    assert not d.actions and any(b.reason=="host_memory_floor" for b in d.blocked_by)


@pytest.mark.parametrize("status", ["released","confirmed"])
def test_inactive_lease_history_does_not_need_pending_weight_profile(status):
    s=snapshot(memory=MemoryState(150,40),leases=(Lease("L","old",1,.05,1,5,status),))
    s=replace(s,models=s.models+(ModelState("old",state="stopped",unit_active=False),))
    assert run(plan_relocation,s,{"source":profile()}).actions


@pytest.mark.parametrize("bad", [None,{},profile(name="other"),profile(state="awake"),
    profile(util=0),profile(util=1.1),profile(util=float("nan")),profile(budget_gb=-1),
    profile(util=None,budget_gb=None),profile(weights_gb=None),profile(weights_gb=-1),
    profile(weights_gb=float("inf")),profile(is_default=True),profile(unit_active=True)])
def test_invalid_supplied_destination_profile_never_stops_source(bad):
    s=snapshot();before=s.to_dict()
    d=run(plan_relocation,s,{"source":bad})
    assert not d.actions and any(b.reason=="invalid_replacement_request" for b in d.blocked_by)
    assert s.to_dict()==before


def test_missing_recent_profile_blocks_but_unused_retirement_needs_none():
    recent=run(plan_relocation,snapshot(),{})
    assert not recent.actions and any(b.reason=="missing_replacement_request" for b in recent.blocked_by)
    old=run(plan_relocation,snapshot(recent=False),{})
    assert [(a.kind,a.model) for a in old.actions]==[("stop","source")]


@pytest.mark.parametrize("planner", [plan_relocation,plan_sleeping_recovery])
def test_none_keeps_legacy_callers_compatible(planner):
    for s in (profile_gap_snapshot(),snapshot(memory=MemoryState(100,40))):
        old=planner(s,model="source",reason="cannot_wake") if planner is plan_relocation else planner(s)
        assert run(planner,s,None)==old


@pytest.mark.parametrize("protection", ["pin","default","inflight"])
def test_replacement_profile_does_not_override_source_protection(protection):
    s=snapshot()
    if protection=="pin":s=replace(s,pins=(Pin("source",11000,"owner"),))
    elif protection=="default":s=replace(s,models=(replace(s.models[0],is_default=True),))
    else:s=replace(s,activity=(replace(s.activity[0],in_flight=1),))
    assert not run(plan_relocation,s,{"source":profile()}).actions
