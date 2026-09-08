# Generated-By: Codex / gpt-6-astra
"""Pressure-specific #120 exclusion compatibility and admission regressions."""

from dataclasses import replace

import pytest

from llmsvc.policy import plan_pressure_sleep
from llmsvc.state import Lease, MemoryState, Pin
from test_policy_exclusions import legacy_plan
from test_policy_pressure import model, snapshot


@pytest.mark.parametrize("external, idle", [(0, 400), (2, 5)])
def test_pressure_exclusion_preserves_peer_ranking_and_legacy_blocker_context(external, idle):
    s = snapshot(model("cold"), model("hot", cold_start_seconds=300), idle=idle)
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], external_gb=external)))
    guards = {"cold": "unleased_model"}
    before = s.to_dict()
    expected = legacy_plan(plan_pressure_sleep, s, guards)
    assert [(a.kind, a.model) for a in expected.actions] == [("sleep", "hot")]
    assert [(b.model,b.reason,b.gpu,b.in_flight) for b in expected.blocked_by] == [
        ("cold","unleased_model",1,0)]
    assert plan_pressure_sleep(s, exclusions=guards) == expected
    assert s.to_dict() == before and guards == {"cold":"unleased_model"}


def test_pressure_default_call_none_and_empty_exclusions_are_equivalent():
    s = snapshot(model())
    assert plan_pressure_sleep(s) == plan_pressure_sleep(s, exclusions=None)
    assert plan_pressure_sleep(s) == plan_pressure_sleep(s, exclusions={})


@pytest.mark.parametrize("until", [9999, 11000, float("nan")])
def test_pressure_real_pin_and_independent_exclusion_retain_provenance(until):
    real_pin = Pin("a",until,"actual-user")
    s = snapshot(model(), pins=(real_pin,))
    original_pins = s.pins
    d = plan_pressure_sleep(s, exclusions={"a":"operation_in_progress"})
    assert not d.actions
    reasons = ["operation_in_progress"] if until == 9999 else ["pinned_until","operation_in_progress"]
    assert [b.reason for b in d.blocked_by] == reasons
    assert s.pins is original_pins and s.pins[0] is real_pin
    assert real_pin.by == "actual-user" and real_pin.until is until


@pytest.mark.parametrize("default", [False, True])
def test_pressure_excluded_sleeper_cannot_fund_sleep_admission(default):
    target = model("target", is_default=default)
    sleeper = replace(model("sleeper"),state="sleeping",resident_gb=2)
    s = snapshot(target,sleeper,memory=MemoryState(170,40),
                 leases=(Lease("L","sleeper",1,.4,1,40,"stale"),))
    before = s.to_dict()
    guards = {"sleeper":"unleased_model"}
    expected = legacy_plan(plan_pressure_sleep,s,guards)
    d = plan_pressure_sleep(s,exclusions=guards)
    assert d == expected
    assert [(a.kind,a.model) for a in d.actions] == ([] if default else [("stop","target")])
    assert any(b.model == "sleeper" and b.reason == "unleased_model" for b in d.blocked_by)
    assert s.to_dict() == before
    assert s.models[1].budget_gb == 40 and s.leases[0].budget_gb == 40


@pytest.mark.parametrize("field", ["free_gb", "external_gb", "total_gb"])
def test_pressure_observation_unknowns_keep_precedence(field):
    s = snapshot(model())
    s = replace(s,gpus=(s.gpus[0],replace(s.gpus[1],**{field:None})))
    guards = {"a":"operation_in_progress"}
    d = plan_pressure_sleep(s,exclusions=guards)
    assert d == legacy_plan(plan_pressure_sleep,s,guards)
    assert not d.actions and [b.reason for b in d.blocked_by] == ["unknown_gpu_pressure"]


@pytest.mark.parametrize("inflight", [None, 1])
def test_excluded_peer_does_not_weaken_inflight_or_unknown_protection(inflight):
    s = snapshot(model("excluded"),model("protected"))
    s = replace(s,activity=(s.activity[0],replace(s.activity[1],in_flight=inflight)))
    d = plan_pressure_sleep(s,exclusions={"excluded":"unmanaged_or_changed_unit"})
    assert not d.actions
    assert [(b.model,b.reason) for b in d.blocked_by] == [
        ("excluded","unmanaged_or_changed_unit"),
        ("protected","unknown_in_flight" if inflight is None else "in_flight")]
