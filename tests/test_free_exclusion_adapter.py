# Generated-By: Codex / gpt-6-astra
"""Free eligibility/intent separation through the actual controller and planner."""

import time
from dataclasses import replace

import pytest

from llmsvc.state import Lease, Pin
from test_model_action_layer import Backend, setup


def free_case(tmp_path, *, ram=False, guard=None):
    backend = Backend(names=("a", "b", "c"))
    backend.models["b"] = replace(backend.models["b"], gpu=1)
    backend.free[1] = 100
    if ram:
        backend.models = {name: replace(model, state="sleeping", is_sleeping=True, resident_gb=2)
                          for name, model in backend.models.items()}
    service, controller, backend = setup(tmp_path, backend)
    if guard == "operation_in_progress":
        controller.pending.add("a")
    elif guard == "unmanaged_model":
        del controller.transport.models["a"]
    elif guard == "configured_unit_mismatch":
        backend.models["a"] = replace(backend.models["a"], unit="vllm-reconfigured.service")
        service.sample_once()
    return service, controller, backend


@pytest.mark.parametrize("ram,payload,names,estimate", [
    (False, {}, "abc", 234), (False, {"gpu": 0}, "ac", 156), (False, {"gpu": 1}, "b", 78),
    (False, {"need_gb": 25}, "a", 78), (False, {"need_gb": 0}, "", 0),
    (True, {}, "abc", 120), (True, {"gpu": 1}, "b", 40), (True, {"need_gb": 25}, "a", 40),
])
def test_free_selection_ranking_and_estimate_baseline(tmp_path, ram, payload, names, estimate):
    service, controller, backend = free_case(tmp_path, ram=ram)
    before = service.snapshot().to_dict()
    decision = controller.plan_free(service.snapshot(), ram=ram, **payload)
    assert [(a.kind, a.model) for a in decision.actions] == [("stop" if ram else "sleep", name) for name in names]
    assert decision.estimated_freed_gb == estimate and not decision.blocked_by
    assert service.snapshot().to_dict() == before and not backend.calls


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("guard", ["operation_in_progress", "unmanaged_model", "configured_unit_mismatch"])
def test_guard_only_context_and_eligible_peer_order_baseline(tmp_path, ram, guard):
    service, controller, backend = free_case(tmp_path, ram=ram, guard=guard)
    decision = controller.plan_free(service.snapshot(), ram=ram)
    assert [a.model for a in decision.actions] == ["b", "c"]
    assert [(b.model, b.reason, b.gpu, b.user, b.in_flight) for b in decision.blocked_by] == [
        ("a", guard, 0, "container", 0)]
    assert service.snapshot().models[0].budget_gb == 80 and not backend.calls


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_genuine_pin_expiry_keeps_existing_actions(tmp_path, ram, expired):
    service, controller, backend = free_case(tmp_path, ram=ram)
    pin = Pin("a", time.time()+(-10 if expired else 100), "real-owner")
    backend.pins = (pin,)
    service.sample_once()
    snapshot = service.snapshot()
    decision = controller.plan_free(snapshot, ram=ram)
    assert [a.model for a in decision.actions] == (list("abc") if expired else list("bc"))
    assert snapshot.pins == (pin,) and snapshot.pins[0] is pin
    assert [b.reason for b in decision.blocked_by] == ([] if expired else ["pinned_until"])


@pytest.mark.parametrize("guard", ["operation_in_progress", "unmanaged_model", "configured_unit_mismatch"])
def test_nested_admission_cannot_spend_guarded_sleeper_budget(tmp_path, guard):
    backend = Backend()
    backend.models["a"] = replace(backend.models["a"], state="sleeping", is_sleeping=True, resident_gb=2, gpu=1)
    backend.models["b"] = replace(backend.models["b"], is_default=True)
    backend.free[1] = 100
    backend.available = 160  # b needs 40; stopping a would be its only RAM admission source.
    service, controller, backend = setup(tmp_path, backend)
    if guard == "operation_in_progress":
        controller.pending.add("a")
    elif guard == "unmanaged_model":
        del controller.transport.models["a"]
    else:
        backend.models["a"] = replace(backend.models["a"], unit="vllm-other.service")
        service.sample_once()
    snapshot = replace(service.snapshot(), leases=(Lease("retained", "a", 1, .4, 20000, 80, "stale"),))
    before = snapshot.to_dict()
    decision = controller.plan_free(snapshot, gpu=0)
    assert not decision.actions
    assert {(b.model, b.reason) for b in decision.blocked_by} == {("a", guard), ("b", "memory_budget")}
    assert snapshot.to_dict() == before and snapshot.leases[0].budget_gb == 80


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("guard", ["operation_in_progress", "unmanaged_model", "configured_unit_mismatch"])
@pytest.mark.parametrize("expiry", ["active", "expired", "unknown"])
def test_real_pin_and_guard_overlap_preserves_separate_provenance(tmp_path, ram, guard, expiry):
    service, controller, backend = free_case(tmp_path, ram=ram, guard=guard)
    until = float("nan") if expiry == "unknown" else time.time()+(-10 if expiry == "expired" else 100)
    pin = Pin("a", until, "real-owner")
    if expiry == "unknown":
        # Exercise the pure adapter's conservative unknown-expiry contract.
        # The scheduler separately rejects non-finite collector JSON as an
        # unknown whole snapshot; do not weaken that publication boundary.
        snapshot = replace(service.snapshot(), pins=(pin,))
    else:
        backend.pins = (pin,)
        service.sample_once()
        snapshot = service.snapshot()
    decision = controller.plan_free(snapshot, ram=ram)
    assert [a.model for a in decision.actions] == ["b", "c"]
    blockers = [b for b in decision.blocked_by if b.model == "a"]
    assert [b.reason for b in blockers] == ([guard] if expiry == "expired" else ["pinned_until", guard])
    assert all((b.gpu, b.user, b.in_flight) == (0, "container", 0) for b in blockers)
    assert snapshot.pins == (pin,) and snapshot.pins[0] is pin and pin.by == "real-owner"


@pytest.mark.parametrize("ram", [False, True])
def test_actual_free_planner_receives_unchanged_intent_snapshot(tmp_path, ram, monkeypatch):
    import llmsvc.policy
    service, controller, backend = free_case(tmp_path, ram=ram, guard="operation_in_progress")
    backend.pins = (Pin("b", time.time()+100, "real-owner"),)
    service.sample_once()
    snapshot = service.snapshot()
    original = llmsvc.policy.plan_free
    calls = []
    def observe(value, **kwargs):
        assert value is snapshot and value.pins is snapshot.pins
        assert kwargs["exclusions"] == {"a": "operation_in_progress"}
        calls.append(value)
        return original(value, **kwargs)
    monkeypatch.setattr(llmsvc.policy, "plan_free", observe)
    decision = controller.plan_free(snapshot, ram=ram)
    assert [a.model for a in decision.actions] == ["c"] and len(calls) == 1


@pytest.mark.parametrize("ram", [False, True])
@pytest.mark.parametrize("guard", ["operation_in_progress", "unmanaged_model", "configured_unit_mismatch"])
def test_free_execution_acts_only_on_peer_and_reports_observed_release(tmp_path, ram, guard):
    service, controller, backend = free_case(tmp_path, ram=ram, guard=guard)
    backend.pins = (Pin("a", time.time()+100, "real-owner"),)
    original_a = backend.models["a"]
    result = controller.free({"ram": ram, "need_gb": 25}, by="caller")
    assert backend.calls == [("stop" if ram else "sleep", "b")]
    assert result["freed_gb"] == (40 if ram else 30) and result["measurement_complete"] is True
    assert result["status"] == "complete"
    assert [b["reason"] for b in result["skipped"] if b["model"] == "a"] == ["pinned_until", guard]
    assert backend.models["a"] == original_a and backend.models["a"].budget_gb == 80
    assert service.snapshot().pins == backend.pins


def test_pending_pin_overlap_preview_has_no_writer_collector_transport_or_event(tmp_path, monkeypatch):
    service, controller, backend = free_case(tmp_path, guard="operation_in_progress")
    backend.pins = (Pin("a", time.time()+100, "real-owner"),)
    service.sample_once()
    before = service.snapshot().to_dict(), service.events_since(0), backend.samples
    def forbidden(*args, **kwargs):
        pytest.fail("Preview invoked collector or transport")
    monkeypatch.setattr(service, "collect", forbidden)
    monkeypatch.setattr(controller.transport, "http_request", forbidden)
    monkeypatch.setattr(controller.transport, "stop_unit", forbidden)
    result = controller.free({"gpu": 0}, by="caller", dry_run=True)
    assert [a["model"] for a in result["would"]] == ["c"]
    assert [b["reason"] for b in result["blocked_by"] if b["model"] == "a"] == ["pinned_until", "operation_in_progress"]
    assert (service.snapshot().to_dict(), service.events_since(0), backend.samples) == before
    assert not (tmp_path / "unused.sqlite").exists() and not backend.calls


def test_invalid_pin_observation_blocks_actual_free_at_publication_boundary(tmp_path):
    service, controller, backend = free_case(tmp_path, guard="operation_in_progress")
    backend.pins = (Pin("a", float("nan"), "unknown-expiry"),)
    result = controller.free({}, by="caller")
    assert result["status"] == "blocked" and not backend.calls
    assert any(b["reason"] == "unknown_or_stale_snapshot" for b in result["skipped"])
    assert result["slept"] == [] and result["stopped"] == []
