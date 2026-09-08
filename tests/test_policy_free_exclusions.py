# Generated-By: Codex / gpt-6-astra
"""Free-specific eligibility compatibility and protected admission regressions."""

from dataclasses import replace

import pytest

from llmsvc.policy import plan_free
from llmsvc.state import Lease, MemoryState, Pin
from test_policy_exclusions import legacy_plan
from test_policy_intents import actions, model, snapshot


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("reason", ["unmanaged_model", "configured_unit_mismatch", "operation_in_progress"])
def test_free_exclusions_preserve_gpu_need_and_peer_ranking(ram, reason):
    state = "sleeping" if ram else "awake"
    s = snapshot(model("cold", state=state), model("hot", state=state, cold_start_seconds=300),
                 model("other_gpu", state=state, gpu=1))
    guards = {"cold": reason}
    before = s.to_dict()
    expected = legacy_plan(plan_free, s, guards, gpu=0, ram=ram, need_gb=35)
    assert actions(expected) == [("stop" if ram else "sleep", "hot")]
    assert expected.estimated_freed_gb == (40 if ram else 38)
    assert [(b.model,b.reason,b.gpu,b.in_flight) for b in expected.blocked_by] == [("cold",reason,0,0)]
    assert plan_free(s, exclusions=guards, gpu=0, ram=ram, need_gb=35) == expected
    assert s.to_dict() == before and guards == {"cold":reason}


@pytest.mark.parametrize("kwargs", [{}, {"ram":True}, {"need_gb":0}, {"need_gb":100}, {"gpu":1}])
def test_free_none_and_empty_exclusions_keep_existing_callers(kwargs):
    s = snapshot(model("awake"), model("sleeping",state="sleeping"))
    assert plan_free(s, **kwargs) == plan_free(s, exclusions=None, **kwargs)
    assert plan_free(s, **kwargs) == plan_free(s, exclusions={}, **kwargs)


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("until", [9999, 11000, float("nan")])
def test_free_real_pin_and_exclusion_preserve_metadata_and_both_reasons(ram, until):
    pin = Pin("a",until,"actual-user")
    s = snapshot(model("a",state="sleeping" if ram else "awake"), pins=(pin,))
    original_pins = s.pins
    d = plan_free(s,ram=ram,exclusions={"a":"operation_in_progress"})
    assert not d.actions
    expected = ["operation_in_progress"] if until == 9999 else ["pinned_until","operation_in_progress"]
    assert [b.reason for b in d.blocked_by] == expected
    assert s.pins is original_pins and s.pins[0] is pin
    assert pin.by == "actual-user" and pin.until is until


@pytest.mark.parametrize("default", [False, True])
def test_free_nested_admission_cannot_borrow_excluded_sleeper_memory(default):
    s = snapshot(model("target",is_default=default), model("sleeper",state="sleeping"),
                 memory=MemoryState(170,40), leases=(Lease("L","sleeper",0,.4,1,40,"stale"),))
    before = s.to_dict()
    guards = {"sleeper":"configured_unit_mismatch"}
    d = plan_free(s,exclusions=guards)
    assert d == legacy_plan(plan_free,s,guards)
    assert actions(d) == ([] if default else [("stop","target")])
    assert any(b.model == "sleeper" and b.reason == "configured_unit_mismatch" for b in d.blocked_by)
    assert s.to_dict() == before and s.models[1].budget_gb == 40 and s.leases[0].budget_gb == 40


@pytest.mark.parametrize("protection", ["default", "pin", "inflight", "unknown_inflight", "unknown_state"])
def test_free_ram_guarded_peer_does_not_weaken_existing_protection(protection):
    protected = model("protected",state="sleeping",is_default=protection=="default")
    s = snapshot(model("guarded",state="sleeping"), protected)
    if protection == "pin":
        s = replace(s,pins=(Pin("protected",11000,"actual-user"),))
    elif protection in ("inflight","unknown_inflight"):
        s = replace(s,activity=(s.activity[0],replace(s.activity[1],in_flight=1 if protection=="inflight" else None)))
    elif protection == "unknown_state":
        s = replace(s,models=(s.models[0],replace(s.models[1],state="unknown")))
    guards={"guarded":"operation_in_progress"}
    d=plan_free(s,ram=True,exclusions=guards)
    assert not d.actions and d == legacy_plan(plan_free,s,guards,ram=True)


def test_free_unknown_host_ram_still_blocks_unexcluded_sleep():
    s = snapshot(model("guarded"),model("other"),memory=MemoryState(None,0))
    d = plan_free(s,exclusions={"guarded":"unmanaged_model"})
    assert not d.actions
    assert {(b.model,b.reason) for b in d.blocked_by} == {("guarded","unmanaged_model"),("other","unknown_memory")}


@pytest.mark.parametrize("need", [0, 60])
def test_free_guarded_need_zero_or_shortfall_preserves_release_estimate(need):
    s = snapshot(model("guarded"),model("other"))
    guards = {"guarded":"operation_in_progress"}
    d = plan_free(s,need_gb=need,exclusions=guards)
    assert d == legacy_plan(plan_free,s,guards,need_gb=need)
    assert actions(d) == ([] if need == 0 else [("sleep","other")])
    assert d.estimated_freed_gb == (0 if need == 0 else 38)
    if need:
        assert [b.reason for b in d.blocked_by] == ["operation_in_progress","insufficient_reclaimable_memory"]


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("until", [9999, 10000, 11000])
def test_free_genuine_pin_expiry_without_overlap_keeps_original_behavior(ram,until):
    pin = Pin("a",until,"actual-user")
    s = snapshot(model("a",state="sleeping" if ram else "awake"),pins=(pin,))
    d = plan_free(s,ram=ram,exclusions={"absent":"operation_in_progress"})
    assert d == plan_free(s,ram=ram)
    assert actions(d) == ([] if until > s.sampled_at else [("stop" if ram else "sleep","a")])
    assert s.pins[0] is pin
