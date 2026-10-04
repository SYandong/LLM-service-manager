# Generated-By: Claude Code / claude-opus-5-5
"""Replay and unit tests for the pure wedged-wake detector (#300)."""

import json
import threading
from pathlib import Path

import pytest

from llmsvc.actions import ActionDispatchError, ModelActionDispatcher
from llmsvc.policy import plan_wake_failure
from llmsvc.state import Action, Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot

FIXTURE = json.loads((Path(__file__).parent / "fixtures/policy/wake_failure.json").read_text())
CASES = FIXTURE["cases"]
NAME = FIXTURE["model"]
GRACE = FIXTURE["grace_seconds"]
BASE = 1791104461.0


def snapshot(at, *, swap_state="ready", is_sleeping=True, in_flight=3, awake=False,
             pinned=False, is_default=False, errors=()):
    model = ModelState(NAME, state="awake" if awake else "sleeping", gpu=0, util=0.7, budget_gb=98,
                       weights_gb=55, unit="vllm-" + NAME + ".service", unit_active=True, health_ok=True,
                       is_sleeping=False if awake else is_sleeping,
                       swap_state=swap_state, is_default=is_default)
    pins = (Pin(NAME, at + 3600, "synthetic-owner"),) if pinned else ()
    return StateSnapshot(sampled_at=at, models=(model,), pins=pins, errors=tuple(errors),
                         activity=(Activity(NAME, at - 1, 900, 80, in_flight, ("client",)),),
                         gpus=(GPUState(0, total_gb=140, free_gb=10, external_gb=40),),
                         memory=MemoryState(500, 55, 300, 150), read_only=False)


def replay(case):
    rounds = case["rounds"]
    windows = {}
    decision = None
    first_action = None
    for index in range(rounds["count"]):
        at = BASE + rounds["start"] + index * rounds["step"]
        observed = dict(case["observed"])
        current = snapshot(at, awake=index >= case.get("awake_from_round", rounds["count"]),
                           pinned=case.get("pinned", False), is_default=case.get("is_default", False),
                           errors=("gpus: deadline exceeded",) if index in case.get("error_rounds", ()) else (),
                           **observed)
        before = current.to_dict()
        decision = plan_wake_failure(current, windows, grace_seconds=GRACE)
        assert current.to_dict() == before
        windows = decision.windows
        if decision.actions and first_action is None:
            first_action = at - BASE
    return decision, first_action


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_wake_failure_replay(case):
    assert case["provenance"]["kind"] == "synthetic_regression"
    decision, first_action = replay(case)
    expect = case["expect"]
    assert [[action.kind, action.reason] for action in decision.actions] == expect["actions"]
    assert all(action.model == NAME for action in decision.actions)
    assert [blocker.reason for blocker in decision.blocked_by] == expect["reasons"]
    if "first_action_at" in case:
        assert first_action == case["first_action_at"]


def test_incident_blocker_keeps_in_flight_context_for_protected_models():
    decision, _ = replay(next(case for case in CASES if case.get("pinned")))
    (blocker,) = decision.blocked_by
    assert (blocker.model, blocker.gpu, blocker.in_flight, blocker.user) == (NAME, 0, 3, "client")


def test_reevaluating_the_same_snapshot_is_idempotent():
    windows = {}
    for index in range(3):
        decision = plan_wake_failure(snapshot(BASE + index * 15), windows, grace_seconds=GRACE)
        windows = decision.windows
    again = plan_wake_failure(snapshot(BASE + 30), windows, grace_seconds=GRACE)
    assert again.windows == windows == {NAME: (BASE, BASE + 30, 3)}


def test_controller_guards_block_instead_of_acting():
    windows = {NAME: (BASE, BASE + 75, 6)}
    decision = plan_wake_failure(snapshot(BASE + 90), windows, grace_seconds=GRACE,
                                 exclusions={NAME: "unleased_model"})
    assert decision.actions == ()
    assert [(b.model, b.reason) for b in decision.blocked_by] == [(NAME, "unleased_model")]


def test_unknown_health_or_unit_is_not_evidence():
    windows = {NAME: (BASE, BASE + 75, 6)}
    from dataclasses import replace
    for change in ({"health_ok": None, "state": "unknown"}, {"unit_active": None, "state": "unknown"}):
        current = snapshot(BASE + 90)
        current = replace(current, models=(replace(current.models[0], **change),))
        decision = plan_wake_failure(current, windows, grace_seconds=GRACE)
        assert decision.actions == () and decision.windows == {}


def test_invalid_grace_is_rejected():
    for value in (0, -1, float("nan"), True):
        with pytest.raises(ValueError):
            plan_wake_failure(snapshot(BASE), {}, grace_seconds=value)


def dispatcher(current, calls):
    return ModelActionDispatcher(
        action_lock=threading.RLock(), snapshot=lambda: current,
        http_request=lambda *args, **kwargs: calls.append(("http", args)) or 200,
        stop_unit=lambda unit, **kwargs: calls.append(("stop", unit)) or 0,
        timeout_seconds=5, max_snapshot_age_seconds=30, enabled=True, wall_clock=lambda: current.sampled_at)


def test_dispatcher_lets_only_wake_failed_stop_past_in_flight():
    calls = []
    current = snapshot(BASE)
    with pytest.raises(ActionDispatchError) as exc:
        dispatcher(current, calls).execute(Action("stop", NAME, "user_stop", 0), dry_run=False)
    assert exc.value.reason == "in_flight" and calls == []
    with pytest.raises(ActionDispatchError) as exc:
        dispatcher(current, calls).execute(Action("sleep", NAME, "wake_failed", 0), dry_run=False)
    assert exc.value.reason == "in_flight" and calls == []
    result = dispatcher(current, calls).execute(Action("stop", NAME, "wake_failed", 0), dry_run=False)
    assert result["status"] == "submitted" and calls == [("stop", "vllm-" + NAME + ".service")]


@pytest.mark.parametrize("protection,reason", [("pinned", "pinned"), ("is_default", "default_or_unknown_role")])
def test_dispatcher_keeps_pin_and_default_protection_for_wake_failed(protection, reason):
    calls = []
    current = snapshot(BASE, **{protection: True})
    with pytest.raises(ActionDispatchError) as exc:
        dispatcher(current, calls).execute(Action("stop", NAME, "wake_failed", 0), dry_run=False)
    assert exc.value.reason == reason and calls == []
