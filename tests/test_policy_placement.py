# Generated-By: Codex / gpt-6-astra
"""Synthetic placement regressions against the shared core state contract."""

from dataclasses import replace

import pytest

from llmsvc.policy import PolicySettings
from llmsvc.policy.placement import plan_placement
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, Reserve, StateSnapshot


def resident(name, gpu=0, budget=40, score=1, **kwargs):
    return ModelState(name, state=kwargs.pop("state", "sleeping"), gpu=gpu, budget_gb=budget,
                      weights_gb=40, cold_start_seconds=score * 11, **kwargs)


def state(*models, **kwargs):
    defaults = dict(sampled_at=10000, models=models,
                    gpus=(GPUState(0, total_gb=100, external_gb=0), GPUState(1, total_gb=100, external_gb=0)),
                    activity=tuple(Activity(m.name, last_request_at=9400, requests_last_hour=0, in_flight=0,
                                            by=("synthetic-owner",)) for m in models),
                    memory=MemoryState(500, sum(m.weights_gb or 0 for m in models if m.state == "sleeping")))
    return StateSnapshot(**{**defaults, **kwargs})


def request(budget=60, **kwargs):
    return ModelState("incoming", state="stopped", budget_gb=budget, **kwargs)


def stopped(decision):
    return [a.model for a in decision.actions if a.kind == "stop"]


def test_free_fit_wins_over_zero_cost_eviction():
    d = plan_placement(state(resident("a", budget=80, score=0)), request())
    assert d.gpu == 1
    assert stopped(d) == []


def test_impossible_request_causes_zero_eviction_on_any_card():
    s = state(resident("small", budget=20), resident("default", budget=50, is_default=True),
              gpus=(GPUState(0, total_gb=100, external_gb=0), GPUState(1, total_gb=100, external_gb=60)))
    d = plan_placement(s, request(80))
    assert d.gpu is None and d.actions == ()


def test_feasible_card_chosen_without_evicting_small_model_elsewhere():
    d = plan_placement(state(resident("small", budget=20), resident("default", budget=50, is_default=True),
                             resident("large", gpu=1, budget=60, score=20)), request(80))
    assert d.gpu == 1 and stopped(d) == ["large"]


def test_enumerates_cheapest_set_instead_of_greedy_individual_scores():
    s = state(resident("cheap-small", budget=20, score=1), resident("expensive-small", budget=20, score=9),
              resident("medium-large", budget=60, score=5),
              gpus=(GPUState(0, total_gb=100, external_gb=0),))
    d = plan_placement(s, request(60))
    assert stopped(d) == ["medium-large"]
    assert d.eviction_cost == 5


def test_compares_cost_across_gpus_and_can_choose_multiple_victims():
    s = state(resident("costly", budget=80, score=20), resident("b", gpu=1, budget=40, score=2),
              resident("c", gpu=1, budget=40, score=3))
    d = plan_placement(s, request(90))
    assert d.gpu == 1 and stopped(d) == ["b", "c"] and d.eviction_cost == 5


def test_default_placement_exclusive_even_when_shared_is_empty_and_cheaper():
    d = plan_placement(state(resident("ordinary", budget=80, score=100)), request(is_default=True))
    assert d.gpu == 0 and stopped(d) == ["ordinary"]


def test_default_never_falls_back_to_shared_when_exclusive_is_blocked():
    s = state(resident("pinned", budget=80), pins=(Pin("pinned", 20000, "owner"),))
    assert plan_placement(s, request(is_default=True)).actions == ()


def test_exclusive_gpu_is_configurable_and_snapshot_default_cannot_be_masked():
    s = state(replace(request(), is_default=True), resident("other", gpu=1, budget=80))
    d = plan_placement(s, request(), settings=PolicySettings(exclusive_gpu=1))
    assert d.gpu == 1 and stopped(d) == ["other"]


@pytest.mark.parametrize("protection", ["pin", "default", "inflight", "unknown_inflight"])
def test_protected_models_are_never_placement_evicted(protection):
    m = resident("protected", budget=80, is_default=protection == "default")
    s = state(m, gpus=(GPUState(0, total_gb=100, external_gb=0),))
    if protection == "pin":
        s = replace(s, pins=(Pin(m.name, 20000, "owner"),))
    elif protection in ("inflight", "unknown_inflight"):
        s = replace(s, activity=(replace(s.activity[0], in_flight=1 if protection == "inflight" else None),))
    assert plan_placement(s, request()).actions == ()


@pytest.mark.parametrize("status", ["pending", "stale"])
def test_expired_outstanding_leases_still_reserve_full_budget(status):
    s = state(resident("default", budget=60, is_default=True),
              gpus=(GPUState(0, total_gb=100, external_gb=0),),
              leases=(Lease("L", "loading", 0, 0.3, 1, 30, status),))
    assert plan_placement(s, request(20)).actions == ()


def test_confirmed_lease_deduplicates_daemon_even_after_replacement_on_other_gpu():
    s = state(resident("a", budget=40, is_default=True),
              gpus=(GPUState(0, total_gb=100, external_gb=0),),
              leases=(Lease("L", "a", 1, 0.4, 1, 40, "confirmed"),))
    assert plan_placement(s, request(60)).gpu == 0


def test_pending_daemon_overlap_counts_once_and_is_not_evictable():
    s = state(resident("a", budget=40), gpus=(GPUState(0, total_gb=100, external_gb=0),),
              leases=(Lease("L", "a", 0, 0.4, 1, 40, "stale"),))
    assert plan_placement(s, request(60)).gpu == 0
    assert not plan_placement(s, request(70)).actions


def test_conflicting_lease_gpu_assignments_block_both_cards():
    s = state(resident("a", budget=40), leases=(Lease("L", "a", 1, 0.4, 1, 40, "stale"),))
    assert not plan_placement(s, request(20)).actions


def test_confirmed_lease_without_daemon_requires_reconciliation():
    s = state(gpus=(GPUState(0, total_gb=100, external_gb=0),),
              leases=(Lease("L", "a", 0, 0.4, 1, 40, "confirmed"),))
    assert not plan_placement(s, request(20)).actions


def test_sleeping_full_budget_not_physical_resident_is_accounted():
    m = replace(resident("default", budget=80, is_default=True), resident_gb=2)
    s = state(m, gpus=(GPUState(0, total_gb=100, external_gb=0, free_gb=98),))
    assert not plan_placement(s, request(30)).actions


def test_active_reserve_excludes_gpu_and_expired_reserve_does_not():
    s = state(resident("default", budget=90, is_default=True),
              reserves=(Reserve("R", 1, 1, 20000, "owner"),))
    assert not plan_placement(s, request(20)).actions
    assert plan_placement(replace(s, sampled_at=20000), request(20)).gpu == 1


def test_unknown_gpu_or_budget_blocks_only_affected_gpu():
    s = state(replace(resident("unknown"), budget_gb=None))
    assert plan_placement(s, request()).gpu == 1
    s = replace(s, gpus=(s.gpus[0], replace(s.gpus[1], external_gb=None)))
    assert not plan_placement(s, request()).actions


def test_unknown_model_gpu_blocks_all_placement():
    assert not plan_placement(state(replace(resident("unknown"), gpu=None)), request()).actions


def test_util_request_uses_selected_gpu_capacity_and_reports_lease_budget():
    s = state(gpus=(GPUState(0, total_gb=200, external_gb=0),))
    d = plan_placement(s, request(None, util=0.6))
    assert d.gpu == 0 and d.budget_gb == 120


def test_external_usage_subtracts_from_exclusive_capacity_and_shared_threshold_is_strict():
    s = state(gpus=(GPUState(0, total_gb=100, external_gb=50),
                    GPUState(1, total_gb=100, external_gb=1)))
    assert not plan_placement(s, request(60)).actions


def test_waiting_requires_more_than_thirty_seconds_idle_and_reports_owner():
    s = state(resident("a", budget=80), gpus=(GPUState(0, total_gb=100, external_gb=0),),
              activity=(Activity("a", 9970, 0, in_flight=0, by=("owner",)),))
    d = plan_placement(s, request(), waiting=True)
    assert not d.actions
    assert any(b.reason == "recently_active" and b.user == "owner" for b in d.blocked_by)
    assert plan_placement(replace(s, sampled_at=10001), request(), waiting=True).gpu == 0


def test_awake_eviction_sleep_is_admitted_or_direct_stop_with_known_ram_pressure():
    s = state(resident("a", budget=80, state="awake"), gpus=(GPUState(0, total_gb=100, external_gb=0),))
    assert [a.kind for a in plan_placement(s, request()).actions] == ["sleep", "stop", "place"]
    s = replace(s, memory=MemoryState(160, 0))
    assert [a.kind for a in plan_placement(s, request()).actions] == ["stop", "place"]
    assert not plan_placement(replace(s, memory=MemoryState(None, 0)), request()).actions


def test_placement_never_reclaims_unpriced_sleepers_for_ram_admission():
    s = state(resident("awake", budget=80, state="awake"), resident("protected", gpu=1, is_default=True),
              memory=MemoryState(160, 40))
    d = plan_placement(s, request(90))
    assert stopped(d) == ["awake"]
    assert [a.kind for a in d.actions] == ["stop", "place"]


def test_existing_target_or_outstanding_target_lease_blocks_new_placement():
    assert not plan_placement(state(resident("incoming")), request()).actions
    s = state(leases=(Lease("L", "incoming", 0, 0.6, 20000, 60),))
    assert not plan_placement(s, request()).actions


def test_input_is_unchanged_and_plan_repeatable():
    s = state(resident("a", budget=80, state="awake"))
    before = s.to_dict()
    assert plan_placement(s, request(is_default=True)) == plan_placement(s, request(is_default=True))
    assert s.to_dict() == before
