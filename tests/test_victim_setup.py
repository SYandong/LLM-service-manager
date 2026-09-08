# Generated-By: Codex / gpt-6-astra
"""Deterministic #127 setup-budget and diagnostic regressions; no live I/O."""

import time

import pytest

from test_placement_leases import system
from test_placement_victims import initialize_victim


def delayed_reconciliation(scheduler, monkeypatch, *, seconds, every=False):
    """Charge virtual scheduler/IO latency without sleeping or altering wall time."""
    offset = [0.0]
    original = scheduler.placement.reconcile
    monkeypatch.setattr(scheduler.placement, "monotonic", lambda: time.monotonic()+offset[0])
    def reconcile(**kwargs):
        result = original(**kwargs)
        if every or not offset[0]:
            offset[0] += seconds
        return result
    monkeypatch.setattr(scheduler.placement, "reconcile", reconcile)


def test_initial_victim_setup_tolerates_delay_beyond_shared_negative_budget(system, monkeypatch):
    scheduler, state, _ = system
    assert scheduler.config.placement_wait_seconds == 0.08
    delayed_reconciliation(scheduler, monkeypatch, seconds=0.1)
    lease_id = initialize_victim(scheduler, state)
    proof = state["setup_proof"]
    assert proof["phase"] == "initial_victim_grant" and proof["elapsed_seconds"] >= 0.1
    assert proof["sample_age_seconds"] < scheduler.config.max_snapshot_age_seconds
    assert proof["snapshot_errors"] == [] and proof["leases_before"] == []
    assert "a" in proof["probes"] and "error" not in proof
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"
    assert len(scheduler.store.leases()) == 1 and scheduler.store.lease(lease_id)[0].budget_gb == 60


def test_initial_setup_deadline_failure_records_phase_without_false_freshness_reason(system, monkeypatch):
    scheduler, state, _ = system
    delayed_reconciliation(scheduler, monkeypatch, seconds=3)
    with pytest.raises(AssertionError, match="initial_victim_grant"):
        initialize_victim(scheduler, state)
    proof = state["setup_proof"]
    assert proof["error"] == "placement_timeout" and proof["blockers"] == []
    assert proof["snapshot_errors"] == [] and proof["probes"] == []
    assert proof["elapsed_seconds"] >= 3 and proof["leases_after"] == []
    assert not scheduler.store.leases()


def test_initial_setup_reports_stale_snapshot_as_blocker_not_generic_timing(system, monkeypatch):
    scheduler, state, _ = system
    monkeypatch.setattr(scheduler, "clock", lambda: time.time()+60)
    delayed_reconciliation(scheduler, monkeypatch, seconds=0.1, every=True)
    with pytest.raises(AssertionError, match="unknown_or_stale_snapshot"):
        initialize_victim(scheduler, state)
    proof = state["setup_proof"]
    assert proof["sample_age_seconds"] > scheduler.config.max_snapshot_age_seconds
    assert [b["reason"] for b in proof["blockers"]] == ["unknown_or_stale_snapshot"]
    assert not proof["probes"] and not proof["leases_after"]
