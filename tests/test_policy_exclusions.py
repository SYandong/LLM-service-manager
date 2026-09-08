# Generated-By: Codex / gpt-6-astra
"""Compatibility cases for replacing the retired controller Pin adapters."""

from dataclasses import replace

import pytest

from llmsvc.policy import plan_idle_sleep, plan_memory_pressure, plan_placement, plan_reserve
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot


def scene(*, state="sleeping", available=500, budget=200):
    models = tuple(ModelState(name, state=state, gpu=0, budget_gb=30, weights_gb=30,
                              resident_gb=2 if state == "sleeping" else 30,
                              cold_start_seconds=score * 11) for name, score in (("cold", 1), ("hot", 10)))
    return StateSnapshot(sampled_at=10000, models=models,
                         activity=tuple(Activity(m.name, 9000, 0, in_flight=0, by=("reader",)) for m in models),
                         gpus=(GPUState(0, total_gb=100, external_gb=0, free_gb=40),),
                         memory=MemoryState(available, 60 if state == "sleeping" else 0, budget))


def legacy_plan(planner, snapshot, guards, **kwargs):
    """Frozen pre-#120 adapter behavior, only used as a compatibility oracle.

    Expected victims/reasons below are authored explicitly, not obtained from
    this adapter or from the new exclusion implementation.
    """
    pins = snapshot.pins + tuple(Pin(name, snapshot.sampled_at + 1000, "retired_test_guard") for name in guards)
    result = planner(replace(snapshot, pins=pins), **kwargs)
    return replace(result, blocked_by=tuple(
        replace(b, reason=guards[b.model]) if b.model in guards and b.reason == "pinned_until" else b
        for b in result.blocked_by))


def guarded_case(name):
    guards = {"cold": "unleased_model"}
    if name == "placement":
        return (plan_placement, scene(), {"request": ModelState("new", state="stopped", budget_gb=70)},
                guards, [("stop", "hot"), ("place", "new")], [])
    if name == "impossible":
        s = replace(scene(), pins=(Pin("hot", 11000, "real-owner"),))
        return (plan_placement, s, {"request": ModelState("new", state="stopped", budget_gb=70)},
                guards, [], [("cold", "unleased_model"), ("hot", "pinned_until"),
                             ("cold", "occupied_budget"), ("hot", "occupied_budget"), ("new", "no_feasible_gpu")])
    if name == "reserve":
        return plan_reserve, scene(), {"gpu": 0}, guards, [("stop", "hot")], [("cold", "unleased_model")]
    if name == "memory":
        return plan_memory_pressure, scene(budget=40), {}, guards, [("stop", "hot")], [("cold", "unleased_model")]
    if name == "idle":
        return plan_idle_sleep, scene(state="awake"), {}, guards, [("sleep", "hot")], [("cold", "unleased_model")]
    s = scene(state="awake", available=170)
    s = replace(s, models=(s.models[0], replace(s.models[1], state="sleeping")),
                memory=replace(s.memory, sleeping_weights_gb=30))
    guards = {"hot": "operation_in_progress"}
    if name == "admission":
        return plan_idle_sleep, s, {}, guards, [("stop", "cold")], [("hot", "operation_in_progress")]
    s = replace(s, models=(replace(s.models[0], is_default=True), s.models[1]))
    return plan_idle_sleep, s, {}, guards, [], [("hot", "operation_in_progress"), ("cold", "memory_budget")]


CASES = ("placement", "impossible", "reserve", "memory", "idle", "admission", "default_admission")


@pytest.mark.parametrize("name", CASES)
def test_existing_guarded_decisions_are_locked(name):
    planner, snapshot, kwargs, guards, expected_actions, expected_blockers = guarded_case(name)
    before = snapshot.to_dict()
    decision = legacy_plan(planner, snapshot, guards, **kwargs)
    assert [(a.kind, a.model) for a in decision.actions] == expected_actions
    assert [(b.model, b.reason) for b in decision.blocked_by] == expected_blockers
    for blocker in decision.blocked_by:
        if blocker.model in guards:
            assert (blocker.gpu, blocker.user, blocker.in_flight) == (0, "reader", 0)
    assert snapshot.to_dict() == before


@pytest.mark.parametrize("name", CASES)
def test_explicit_exclusions_match_locked_guard_decisions_without_mutation(name):
    planner, snapshot, kwargs, guards, _, _ = guarded_case(name)
    before = snapshot.to_dict()
    expected = legacy_plan(planner, snapshot, guards, **kwargs)
    actual = planner(snapshot, exclusions=guards, **kwargs)
    assert actual == expected
    assert snapshot.to_dict() == before
    assert snapshot.pins == tuple(Pin(**p) for p in before["pins"])
    assert guards == guarded_case(name)[3]


@pytest.mark.parametrize("name", CASES)
def test_omitted_none_and_empty_exclusions_keep_existing_defaults(name):
    planner, snapshot, kwargs, _, _, _ = guarded_case(name)
    assert planner(snapshot, **kwargs) == planner(snapshot, exclusions=None, **kwargs)
    assert planner(snapshot, **kwargs) == planner(snapshot, exclusions={}, **kwargs)


@pytest.mark.parametrize("name", ["impossible", "reserve", "memory", "idle"])
@pytest.mark.parametrize("until", [11000, float("nan")])
def test_real_pin_and_exclusion_keep_both_provenances_and_pin_metadata(name, until):
    planner, snapshot, kwargs, guards, _, _ = guarded_case(name)
    real_pin = Pin("cold", until, "actual-user")
    snapshot = replace(snapshot, pins=snapshot.pins+(real_pin,))
    original_pins = snapshot.pins
    actual = planner(snapshot, exclusions=guards, **kwargs)
    assert not any(a.model == "cold" for a in actual.actions)
    blockers = [b for b in actual.blocked_by if b.model == "cold"]
    assert [b.reason for b in blockers][:2] == ["pinned_until", "unleased_model"]
    assert all((b.gpu,b.user,b.in_flight) == (0,"reader",0) for b in blockers)
    assert snapshot.pins is original_pins and snapshot.pins[-1] is real_pin
    assert real_pin.by == "actual-user" and real_pin.until is until
    expected = legacy_plan(planner, snapshot, guards, **kwargs)
    # The overlap provenance repair changes blockers, never victims or ranking.
    assert actual.actions == expected.actions


@pytest.mark.parametrize("name", ["reserve", "memory", "idle"])
def test_expired_real_pin_does_not_hide_active_exclusion(name):
    planner, snapshot, kwargs, guards, _, _ = guarded_case(name)
    snapshot = replace(snapshot, pins=(Pin("cold",10000,"actual-user"),))
    actual = planner(snapshot, exclusions=guards, **kwargs)
    assert [(b.model,b.reason) for b in actual.blocked_by] == [("cold","unleased_model")]
    assert actual == legacy_plan(planner,snapshot,guards,**kwargs)


@pytest.mark.parametrize("protection", ["default", "inflight", "unknown", "pin"])
def test_guarding_peer_does_not_weaken_real_protection(protection):
    s = scene()
    if protection == "default":
        s=replace(s,models=(s.models[0],replace(s.models[1],is_default=True)))
    elif protection == "pin":
        s=replace(s,pins=(Pin("hot",11000,"actual-user"),))
    else:
        s=replace(s,activity=(s.activity[0],replace(s.activity[1],in_flight=1 if protection=="inflight" else None)))
    request=ModelState("new",state="stopped",budget_gb=70)
    d=plan_placement(s,request,exclusions={"cold":"unleased_model"})
    assert not d.actions and d.gpu is None
    assert d == legacy_plan(plan_placement,s,{"cold":"unleased_model"},request=request)


def test_stale_lease_and_excluded_daemon_keep_their_entire_placement_budget():
    from llmsvc.state import Lease
    s=scene()
    s=replace(s,models=(s.models[0],),activity=(s.activity[0],),memory=MemoryState(500,30),
              leases=(Lease("L","loading",0,.6,1,60,"stale"),))
    d=plan_placement(s,ModelState("new",state="stopped",budget_gb=20),exclusions={"cold":"unleased_model"})
    assert not d.actions and d.gpu is None
    assert any(b.model == "cold" and b.reason == "unleased_model" for b in d.blocked_by)
    assert s.leases[0].status == "stale" and s.models[0].budget_gb == 30


def test_unknown_model_state_retains_precedence_over_exclusion():
    s=scene()
    s=replace(s,models=(replace(s.models[0],state="unknown"),s.models[1]))
    d=plan_reserve(s,gpu=0,exclusions={"cold":"operation_in_progress"})
    assert [(b.model,b.reason) for b in d.blocked_by] == [("cold","unknown_model_state")]


@pytest.mark.parametrize("guards", [{"cold":""},{"cold":None},{None:"unleased_model"},{" ":"unleased_model"}])
@pytest.mark.parametrize("name", ["placement","reserve","memory","idle"])
def test_invalid_exclusion_entries_never_silently_disable_a_guard(name,guards):
    planner,snapshot,kwargs,_,_,_=guarded_case(name)
    with pytest.raises(ValueError,match="exclusions"):
        planner(snapshot,exclusions=guards,**kwargs)


def test_absent_model_exclusion_does_not_change_current_model_accounting():
    s=scene()
    assert plan_reserve(s,gpu=0,exclusions={"absent":"unleased_model"}) == plan_reserve(s,gpu=0)
