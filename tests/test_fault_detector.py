# Generated-By: Codex / gpt-6-astra
"""Proven-fault temporal/lifecycle replay; no sleeping or runtime resources."""

from dataclasses import replace

import pytest

from llmsvc.faults import FaultDetector
from llmsvc.leases import UnitObservation
from llmsvc.state import Lease, ModelState, StateSnapshot


LEASE = Lease("lease", "model", 0, .4, 10000, 80, "confirmed")
UNIT = "vllm-model.service"
IDENTITY = UnitObservation(True, False, True, "lease", "a" * 32)
MODEL = ModelState("model", state="awake", gpu=0, unit=UNIT, unit_active=True,
                   health_ok=True, is_sleeping=False, swap_state="ready", budget_gb=80)


def observe(detector, second, *, model=MODEL, lease=LEASE, identity=IDENTITY,
            generation=None, skew=0, received=None, **kwargs):
    snapshot = StateSnapshot(sampled_at=100+second, models=(model,))
    return detector.observe(snapshot, lease, UNIT, identity,
        generation=int(second*100)+1 if generation is None else generation,
        received_at=100+second+skew if received is None else received,
        wall_now=100+second+skew, started_at=100+second, finished_at=100+second+skew, **kwargs)


def test_unexpected_exit_requires_current_previously_serving_identity():
    detector = FaultDetector()
    gone = replace(MODEL, state="stopped", unit_active=False, gpu=None)
    absent = UnitObservation(False, True)
    assert observe(detector, 0, model=gone, identity=absent) is None
    assert observe(detector, 1) is None
    proof = observe(detector, 2, model=gone, identity=absent)
    assert proof.reason == "unexpected_unit_exit" and proof.invocation_id == IDENTITY.invocation_id
    assert detector.current(proof, wall_now=102, now=102)
    assert not detector.current(proof, wall_now=105, now=105)


def test_ten_seconds_requires_repeated_bounded_evidence_and_healthy_does_not_clear_it():
    detector = FaultDetector()
    observe(detector, 0)
    sleeping = replace(MODEL, state="sleeping", is_sleeping=True)
    for second in range(1, 11):
        assert observe(detector, second, model=sleeping) is None
    proof = observe(detector, 11, model=sleeping)
    assert proof.reason == "ready_still_sleeping" and proof.samples == 11
    assert proof.window_lower_bound_seconds >= 10
    assert sleeping.health_ok is True


def test_round_skew_uses_first_receipt_to_last_round_start_not_two_start_times():
    detector = FaultDetector()
    observe(detector, 0, skew=.6)
    sleeping = replace(MODEL, state="sleeping", is_sleeping=True)
    for second in range(1, 12):
        assert observe(detector, second, model=sleeping, skew=.6) is None
    assert observe(detector, 12, model=sleeping, skew=.6).reason == "ready_still_sleeping"


@pytest.mark.parametrize("mode", ["15s-gap", "receipt-gap", "round-skew", "duplicate-generation", "reordered", "unknown-sleep", "recovered"])
def test_gaps_unknown_recovery_and_reused_publications_never_bridge_mismatch(mode):
    detector = FaultDetector()
    sleeping = replace(MODEL, state="sleeping", is_sleeping=True)
    observe(detector, 0)
    for second in range(1, 8):
        assert observe(detector, second, model=sleeping) is None
    if mode == "15s-gap":
        assert observe(detector, 22, model=sleeping) is None
        return
    if mode == "receipt-gap":
        assert observe(detector, 8, model=sleeping, received=120) is None
    elif mode == "round-skew":
        assert observe(detector, 8, model=sleeping, skew=1.5) is None
    elif mode == "duplicate-generation":
        assert observe(detector, 8, model=sleeping, generation=701) is None
    elif mode == "reordered":
        assert observe(detector, 6, model=sleeping) is None
    elif mode == "unknown-sleep":
        assert observe(detector, 8, model=replace(sleeping, is_sleeping=None)) is None
    else:
        assert observe(detector, 8) is None
    for second in range(9, 14):
        assert observe(detector, second, model=sleeping) is None


def test_explicit_actual_health_failure_count_and_independent_reset():
    detector = FaultDetector(health_failures=3)
    bad = replace(MODEL, state="unknown", health_ok=False)
    observe(detector, 0)
    assert observe(detector, 1, model=bad) is None
    assert observe(detector, 2, model=bad) is None
    assert observe(detector, 3, model=replace(bad, health_ok=None)) is None
    assert observe(detector, 4, model=bad) is None
    assert observe(detector, 5, model=bad) is None
    proof = observe(detector, 6, model=bad)
    assert proof.reason == "consecutive_health_failures" and proof.samples == 3
    observe(detector, 7)
    assert not detector.current(proof, wall_now=107, now=107)
    assert observe(detector, 8, model=bad) is None


@pytest.mark.parametrize("change", ["lease", "invocation", "unknown-invocation", "unit", "gpu", "pending-startup"])
def test_identity_changes_and_unconfirmed_startup_reset_health_evidence(change):
    detector = FaultDetector()
    bad = replace(MODEL, state="unknown", health_ok=False)
    observe(detector, 0)
    observe(detector, 1, model=bad)
    observe(detector, 2, model=bad)
    kwargs = {"model": bad}
    if change == "lease":
        kwargs.update(lease=replace(LEASE, lease_id="new"), identity=replace(IDENTITY, lease_id="new"))
    elif change == "invocation":
        kwargs["identity"] = replace(IDENTITY, invocation_id="b"*32)
    elif change == "unknown-invocation":
        kwargs["identity"] = replace(IDENTITY, invocation_id="")
    elif change == "unit":
        kwargs["model"] = replace(bad, unit="vllm-other.service")
    elif change == "gpu":
        kwargs["lease"] = replace(LEASE, gpu=1)
    else:
        kwargs["lease"] = replace(LEASE, status="pending")
    assert observe(detector, 3, **kwargs) is None
    for second in range(4, 8):
        assert observe(detector, second, model=bad) is None


@pytest.mark.parametrize("stopped", [False, True])
def test_expected_sleep_or_stop_is_not_failed_wake(stopped):
    detector = FaultDetector()
    observe(detector, 0)
    detector.invalidate("model", stopped=stopped)
    state = replace(MODEL, state="stopped" if stopped else "sleeping", unit_active=not stopped,
                    is_sleeping=True)
    identity = UnitObservation(False, True) if stopped else IDENTITY
    for second in range(1, 15):
        assert observe(detector, second, model=state, identity=identity) is None


def test_known_confirmed_sleeping_daemon_can_begin_explicit_warm_wake_window():
    detector = FaultDetector()
    sleeping = replace(MODEL, state="sleeping", is_sleeping=True, swap_state="stopped")
    observe(detector, 0, model=sleeping)
    assert detector.warm_wake("model", LEASE.lease_id, now=100)
    sleeping = replace(sleeping, swap_state="ready")
    for second in range(1, 11):
        assert observe(detector, second, model=sleeping) is None
    assert observe(detector, 11, model=sleeping).reason == "ready_still_sleeping"


def test_no_prior_service_or_known_wake_means_no_startup_fault():
    detector = FaultDetector()
    starting = replace(MODEL, state="unknown", health_ok=False, swap_state="starting")
    for second in range(15):
        assert observe(detector, second, model=starting) is None
    assert not detector.warm_wake("model", LEASE.lease_id, now=114)


@pytest.mark.parametrize("value", [0, 1, 2, True, 3.5])
def test_health_threshold_cannot_silently_weaken_three_failure_minimum(value):
    with pytest.raises(ValueError):
        FaultDetector(health_failures=value)


def test_failed_unit_can_require_cleanup_while_cgroup_resources_still_exist():
    detector = FaultDetector()
    observe(detector, 0)
    failed = replace(MODEL, state="unknown", unit_active=False, health_ok=None)
    residual = replace(IDENTITY, active=False, inactive=True, exited=False)
    proof = observe(detector, 1, model=failed, identity=residual)
    assert proof.reason == "unexpected_unit_exit"
    assert not residual.exited  # Classification does not authorize release.


@pytest.mark.parametrize("invocation", [None, "", "0"*32, "n/a", "not-an-invocation-id"])
def test_unknown_invocation_never_arms_a_lifecycle(invocation):
    detector = FaultDetector()
    identity = replace(IDENTITY, invocation_id=invocation)
    observe(detector, 0, identity=identity)
    bad = replace(MODEL, state="unknown", health_ok=False)
    for second in range(1, 5):
        assert observe(detector, second, model=bad, identity=identity) is None


def test_health_configuration_can_require_more_actual_failures():
    detector = FaultDetector(health_failures=5)
    observe(detector, 0)
    bad = replace(MODEL, state="unknown", health_ok=False)
    for second in range(1, 5):
        assert observe(detector, second, model=bad) is None
    assert observe(detector, 5, model=bad).samples == 5


def test_epoch_wall_time_and_monotonic_collection_bounds_are_never_subtracted_together():
    detector = FaultDetector()
    for second in range(13):
        model = MODEL if second == 0 else replace(MODEL, state="sleeping", is_sleeping=True)
        snapshot = StateSnapshot(sampled_at=1800000000.+second, models=(model,))
        proof = detector.observe(snapshot, LEASE, UNIT, IDENTITY, generation=second+1,
            wall_now=1800000000.+second+.1, started_at=50.+second,
            finished_at=50.+second+.1, received_at=50.+second+.1)
        if second < 12:
            assert proof is None
    assert proof.reason == "ready_still_sleeping" and proof.window_lower_bound_seconds >= 10
    assert proof.round_started_at == 62


def test_wall_step_outside_monotonic_interval_bounds_resets_health_window():
    detector = FaultDetector()
    for second in range(4):
        model = MODEL if second == 0 else replace(MODEL, state="unknown", health_ok=False)
        jump = 1 if second == 3 else 0
        snapshot = StateSnapshot(sampled_at=1800000000.+second+jump, models=(model,))
        assert detector.observe(snapshot, LEASE, UNIT, IDENTITY, generation=second+1,
            wall_now=1800000000.+second+jump+.1, started_at=50.+second,
            finished_at=50.+second+.1, received_at=50.+second+.1) is None
