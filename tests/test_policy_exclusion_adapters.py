# Generated-By: Codex / gpt-6-astra
"""Controller decision regressions: eligibility must not masquerade as intent."""

from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

import pytest

from llmsvc.actions import AutomaticPolicyController, ModelActionController, ReservationController
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, Reserve, StateSnapshot


LANES = ("placement", "reserve", "idle", "pressure")


@pytest.fixture
def adapter():
    def build(lane):
        def forbidden(*args, **kwargs):
            pytest.fail("Planning invoked a transport or writer")

        transport = SimpleNamespace(
            units={name: f"vllm-{name}.service" for name in "abc"},
            models={name: {"weights_gb": 10} for name in "abc"},
            http_request=forbidden, stop_unit=forbidden,
        )
        scheduler = SimpleNamespace(
            config=SchedulerConfig("127.0.0.1", 19001, model_actions_enabled=True),
            clock=lambda: 10000, store=None, action_lock=RLock(),
        )
        scheduler.model_actions = ModelActionController(scheduler, transport)
        placement = PlacementController(scheduler, transport)
        reserve = ReservationController(scheduler, accounting=placement)
        automatic = AutomaticPolicyController(scheduler, accounting=placement)
        sleeping = lane != "idle"
        snapshot = StateSnapshot(
            sampled_at=10000,
            gpus=(GPUState(0, total_gb=100, free_gb=94 if sleeping else 10, external_gb=0),),
            models=tuple(ModelState(name, state="sleeping" if sleeping else "awake", gpu=0,
                budget_gb=30, weights_gb=10, resident_gb=2 if sleeping else 30,
                unit=transport.units[name], unit_active=True, cold_start_seconds=10) for name in "abc"),
            activity=tuple(Activity(name, last_request_at=9000, requests_last_hour=i,
                requests_last_10m=i, in_flight=0, by=(f"tenant-{name}",)) for i, name in enumerate("abc")),
            leases=tuple(Lease(f"lease-{name}", name, 0, .3, 11000, 30, "confirmed") for name in "abc"),
            memory=MemoryState(300, 30 if sleeping else 0, 10 if lane == "pressure" else 200, 150),
        )
        request = ModelState("new", state="stopped", budget_gb=40, weights_gb=10)
        def plan(value):
            if lane == "placement":
                decision, blockers = placement._decision(value, request, waiting=False)
                assert decision is not None
                return replace(decision, blocked_by=blockers)
            if lane == "reserve":
                return reserve._plan(value, Reserve("reservation", 0, 20, 11000, "owner"))
            return automatic.plan(value)
        return scheduler, transport, snapshot, plan
    return build


def effects(decision):
    return [(action.kind, action.model) for action in decision.actions]


@pytest.mark.parametrize("lane", LANES)
def test_eligible_peer_order_and_full_placement_budget(adapter, lane):
    _, _, snapshot, plan = adapter(lane)
    expected = {
        "placement": [("stop", "a"), ("place", "new")],
        "reserve": [("stop", name) for name in "abc"],
        "idle": [("sleep", name) for name in "abc"],
        "pressure": [("stop", name) for name in "ab"],
    }
    decision = plan(snapshot)
    assert effects(decision) == expected[lane]
    assert not decision.blocked_by
    if lane == "placement":
        assert decision.budget_gb == 40
    assert all(lease.budget_gb == 30 and lease.status == "confirmed" for lease in snapshot.leases)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("guard", ["unleased_model", "unmanaged_or_changed_unit", "operation_in_progress"])
def test_guarded_candidate_keeps_budget_and_peer_ranking(adapter, lane, guard):
    scheduler, transport, snapshot, plan = adapter(lane)
    if guard == "unleased_model":
        snapshot = replace(snapshot, leases=snapshot.leases[1:])
    elif guard == "unmanaged_or_changed_unit":
        transport.units["a"] = "vllm-reconfigured.service"
    else:
        scheduler.model_actions.pending.add("a")
    before = snapshot.to_dict()
    decision = plan(snapshot)
    expected = [("stop", "b"), ("place", "new")] if lane == "placement" else [
        ("sleep" if lane == "idle" else "stop", name) for name in "bc"]
    assert effects(decision) == expected
    # Successful placement omits unrelated victim blockers, as before.
    if lane != "placement":
        assert [(b.model, b.reason, b.gpu, b.user, b.in_flight) for b in decision.blocked_by] == [
            ("a", guard, 0, "tenant-a", 0)]
    assert snapshot.to_dict() == before


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("protection", ["pin", "expired-pin", "default", "inflight", "unknown-inflight"])
def test_intrinsic_protection_and_pin_expiry(adapter, lane, protection):
    _, transport, snapshot, plan = adapter(lane)
    if protection in ("pin", "expired-pin"):
        snapshot = replace(snapshot, pins=(Pin("a", 9999 if protection == "expired-pin" else 11000, "real-owner"),))
    elif protection == "default":
        transport.models["a"]["is_default"] = True
    else:
        activity = replace(snapshot.activity[0], in_flight=1 if protection == "inflight" else None)
        snapshot = replace(snapshot, activity=(activity,) + snapshot.activity[1:])
    before = snapshot.to_dict()
    decision = plan(snapshot)
    allowed = protection == "expired-pin" or (protection == "default" and lane == "idle")
    assert any(a.model == "a" for a in decision.actions) is allowed
    if not allowed and lane != "placement":
        reason = {"pin": "pinned_until", "default": "default_model", "inflight": "in_flight",
                  "unknown-inflight": "unknown_in_flight"}[protection]
        assert any(b.model == "a" and b.reason == reason and b.user == "tenant-a" for b in decision.blocked_by)
    assert snapshot.to_dict() == before


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("until", [9999, 11000, float("nan")])
def test_real_pin_and_independent_guard_keep_separate_provenance(adapter, lane, until):
    _, _, snapshot, plan = adapter(lane)
    real_pin = Pin("a", until, "real-owner")
    # Placement needs an infeasible GPU to expose its complete blocker set.
    snapshot = replace(snapshot, leases=snapshot.leases[1:],
        pins=(real_pin, Pin("b", 11000, "owner-b"), Pin("c", 11000, "owner-c")))
    decision = plan(snapshot)
    assert not decision.actions
    blockers = [b for b in decision.blocked_by if b.model == "a"]
    reasons = [b.reason for b in blockers]
    expected = ["unleased_model"] if until == 9999 else ["pinned_until", "unleased_model"]
    if lane == "placement":
        # An excluded daemon still occupies its full budget on an infeasible
        # GPU, independently of its pin and operational eligibility blockers.
        expected.append("occupied_budget")
    assert reasons == expected
    assert all((b.gpu, b.user, b.in_flight) == (0, "tenant-a", 0) for b in blockers)
    assert snapshot.pins[0] is real_pin and real_pin.by == "real-owner"


@pytest.mark.parametrize("lane", LANES)
def test_real_planners_receive_only_original_user_pins(adapter, lane, monkeypatch):
    import llmsvc.leases
    import llmsvc.policy

    _, _, snapshot, plan = adapter(lane)
    snapshot = replace(snapshot, leases=snapshot.leases[1:], pins=(Pin("b", 9999, "expired-owner"),))
    calls = []
    def observe(original):
        def checked(value, *args, **kwargs):
            assert value.pins is snapshot.pins
            assert value.leases is snapshot.leases
            assert kwargs["exclusions"] == {"a": "unleased_model"}
            calls.append(original.__name__)
            return original(value, *args, **kwargs)
        return checked
    if lane == "placement":
        monkeypatch.setattr(llmsvc.leases, "plan_placement", observe(llmsvc.leases.plan_placement))
    else:
        for name in ("plan_reserve", "plan_memory_pressure", "plan_idle_sleep"):
            monkeypatch.setattr(llmsvc.policy, name, observe(getattr(llmsvc.policy, name)))
    decision = plan(snapshot)
    assert decision.actions and all(a.model != "a" for a in decision.actions)
    assert calls == {
        "placement": ["plan_placement"], "reserve": ["plan_reserve"],
        "pressure": ["plan_memory_pressure"], "idle": ["plan_memory_pressure", "plan_idle_sleep"],
    }[lane]


@pytest.mark.parametrize("lane", LANES)
def test_concurrent_free_window_cannot_supply_any_victim_budget(adapter, lane):
    scheduler, _, snapshot, plan = adapter(lane)
    scheduler.model_actions.free_active = True
    before = snapshot.to_dict()
    decision = plan(snapshot)
    assert not decision.actions
    assert {b.model for b in decision.blocked_by if b.reason == "operation_in_progress"} == set("abc")
    assert snapshot.to_dict() == before


def test_idle_sleep_admission_cannot_stop_an_unleased_sleeper(adapter):
    _, _, snapshot, plan = adapter("idle")
    snapshot = replace(snapshot,
        models=(replace(snapshot.models[0], state="sleeping", resident_gb=2),) + snapshot.models[1:],
        leases=snapshot.leases[1:],
        # a already consumes all sleeping allowance. b is default and cannot
        # fall back to stop; c is pinned. No candidate may borrow a's budget.
        memory=MemoryState(300, 10, 10, 150),
        pins=(Pin("c", 11000, "owner-c"),))
    snapshot = replace(snapshot, models=(snapshot.models[0], replace(snapshot.models[1], is_default=True),
                                        snapshot.models[2]))
    decision = plan(snapshot)
    assert not decision.actions
    assert {(b.model, b.reason) for b in decision.blocked_by} >= {
        ("a", "unleased_model"), ("b", "memory_budget"), ("c", "pinned_until")}
    assert snapshot.models[0].budget_gb == 30 and snapshot.memory.sleeping_weights_gb == 10
