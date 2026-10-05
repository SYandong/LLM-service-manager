# Generated-By: Codex / gpt-6.1-sol
"""Synthetic hopeless/transient classification and preserved placement plans."""

from dataclasses import replace

import pytest

from llmsvc.policy import PolicySettings, plan_placement
from llmsvc.policy.feasibility import HOPELESS_REASONS, placement_unplaceable
from llmsvc.policy.placement import PlacementDecision
from llmsvc.state import Action, Activity, Blocker, GPUState, Lease, MemoryState, ModelState, StateSnapshot


def snapshot(**kwargs):
    defaults = dict(sampled_at=1000,
                    gpus=(GPUState(0, total_gb=100, external_gb=60),
                          GPUState(1, total_gb=100, external_gb=60)),
                    memory=MemoryState(500, 0))
    return StateSnapshot(**{**defaults, **kwargs})


def blocked(*blockers):
    return PlacementDecision(blocked_by=blockers + (Blocker("incoming", "no_feasible_gpu"),))


@pytest.mark.parametrize("reason", sorted(HOPELESS_REASONS))
def test_each_hopeless_reason_requires_evidence_on_every_candidate(reason):
    assert placement_unplaceable(snapshot(), blocked(Blocker(None, reason, 0), Blocker(None, reason, 1)))
    assert not placement_unplaceable(snapshot(), blocked(Blocker(None, reason, 0)))


@pytest.mark.parametrize("reason", [
    "reserved", "occupied_budget", "outstanding_lease", "in_flight", "pinned_until",
    "default_model", "unknown_gpu_capacity", "unknown_memory", "unknown_or_stale_snapshot",
    "fault_recovery_pending", "unknown_budget", "conflicting_accounting", "eviction_required",
    "unrecognised_future_reason",
])
def test_one_transient_or_unknown_candidate_keeps_waiting(reason):
    assert not placement_unplaceable(snapshot(), blocked(
        Blocker(None, "external_pressure", 0), Blocker(None, reason, 1)))
    # A hopeless blocker must not hide a second transient blocker on that card.
    assert not placement_unplaceable(snapshot(), blocked(
        Blocker(None, "external_pressure", 0), Blocker(None, "external_pressure", 1), Blocker(None, reason, 1)))


@pytest.mark.parametrize("reason", ["unknown_accounting_gpu", "unknown_memory", "fault_recovery_pending"])
def test_global_uncertainty_blocks_hopeless_classification(reason):
    decision = blocked(Blocker(None, "external_pressure", 0), Blocker(None, "external_pressure", 1))
    assert not placement_unplaceable(snapshot(), replace(decision, blocked_by=decision.blocked_by + (Blocker(None, reason),)))
    assert not placement_unplaceable(snapshot(blocked_by=(Blocker(None, reason),)), decision)


def test_missing_pool_member_empty_pool_and_summary_alone_are_transient():
    assert not placement_unplaceable(snapshot(), blocked())
    decision = blocked(Blocker(None, "external_pressure", 0), Blocker(None, "external_pressure", 1))
    assert not placement_unplaceable(snapshot(gpus=()), decision)
    assert not placement_unplaceable(snapshot(), decision, settings=PolicySettings(placement_gpus=(0, 1, 2)))
    assert not placement_unplaceable(snapshot(errors=("probe unavailable",)), decision)
    assert not placement_unplaceable(snapshot(sampled_at=None), decision)


def test_cards_outside_pool_do_not_hide_a_transient_candidate():
    settings = PolicySettings(placement_gpus=(0,))
    decision = blocked(Blocker(None, "unknown_gpu_capacity", 0), Blocker(None, "outside_placement_pool", 1))
    assert not placement_unplaceable(snapshot(), decision, settings=settings)
    decision = blocked(Blocker(None, "external_pressure", 0), Blocker(None, "unknown_gpu_capacity", 1))
    assert placement_unplaceable(snapshot(), decision, settings=settings)


@pytest.mark.parametrize("kind", ["place", "sleep", "stop"])
def test_an_actionable_plan_is_never_hopeless(kind):
    decision = blocked(Blocker(None, "external_pressure", 0), Blocker(None, "external_pressure", 1))
    assert not placement_unplaceable(snapshot(), replace(decision, actions=(Action(kind, "incoming", "placement", 0),)))


def test_custom_gpu_exclusions_only_apply_to_the_bound_card():
    decision = blocked(Blocker(None, "relocation_source", 0), Blocker(None, "external_pressure", 1))
    assert placement_unplaceable(snapshot(), decision, gpu_exclusions={0: "relocation_source"})
    assert not placement_unplaceable(snapshot(), decision)
    assert not placement_unplaceable(snapshot(), decision, gpu_exclusions={1: "relocation_source"})


def test_real_plan_all_external_occupancy_preserves_threshold_and_has_amounts():
    request = ModelState("incoming", state="stopped", budget_gb=60)
    settings = PolicySettings(shared_external_threshold_gb=30)
    decision = plan_placement(snapshot(), request, settings=settings)
    assert decision.unplaceable and not decision.actions
    assert [b.external_gb for b in decision.blocked_by if b.reason == "external_pressure"] == [60, 60]
    below = snapshot(gpus=(GPUState(0, total_gb=100, external_gb=29),))
    decision = plan_placement(below, request, settings=settings)
    assert decision.gpu == 0 and not decision.unplaceable


def test_oversize_pool_has_explicit_blockers_and_never_evicts():
    request = ModelState("incoming", state="stopped", budget_gb=150)
    decision = plan_placement(snapshot(gpus=(GPUState(0, total_gb=100, external_gb=0),
                                            GPUState(1, total_gb=140, external_gb=0))), request)
    assert decision.unplaceable and not decision.actions
    assert {(b.reason, b.gpu) for b in decision.blocked_by if b.gpu is not None} == {
        ("request_exceeds_gpu_capacity", 0), ("request_exceeds_gpu_capacity", 1)}
    unknown = snapshot(gpus=(GPUState(0, total_gb=100, external_gb=0), GPUState(1, external_gb=0)))
    assert not plan_placement(unknown, request).unplaceable


def test_unknown_accounting_on_an_external_card_remains_transient():
    unknown = ModelState("resident", state="awake", gpu=0, budget_gb=None)
    assert not plan_placement(snapshot(models=(unknown,)), ModelState("incoming", state="stopped", budget_gb=60)).unplaceable
    unknown = replace(unknown, gpu=None)
    assert not plan_placement(snapshot(models=(unknown,)), ModelState("incoming", state="stopped", budget_gb=60)).unplaceable
    lease = Lease("pending", "loading", 0, 0.5, 2000, 50)
    assert not plan_placement(snapshot(leases=(replace(lease, status="unrecognised"),)),
                              ModelState("incoming", state="stopped", budget_gb=60)).unplaceable


def test_external_card_and_feasible_eviction_card_keep_the_existing_victim_plan():
    resident = ModelState("resident", state="sleeping", gpu=1, budget_gb=80, weights_gb=10, cold_start_seconds=1)
    observed = snapshot(gpus=(GPUState(0, total_gb=100, external_gb=60), GPUState(1, total_gb=100, external_gb=0)),
                        models=(resident,), activity=(Activity("resident", 900, 0, 0, 0),), memory=MemoryState(500, 10))
    decision = plan_placement(observed, ModelState("incoming", state="stopped", budget_gb=60))
    assert not decision.unplaceable
    assert [(a.kind, a.model) for a in decision.actions] == [("stop", "resident"), ("place", "incoming")]
