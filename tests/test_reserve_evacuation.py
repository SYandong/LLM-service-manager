# Generated-By: Codex / gpt-6-astra
"""Internal reservation action and exit/accounting proofs."""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llmsvc.actions import ActionDispatchError, ModelActionController, ReservationController
from llmsvc.policy import plan_placement
from llmsvc.state import ModelState, Pin
from llmsvc.leases import UnitObservation
from test_placement_leases import system, grant, ready
from test_reserve_intents import payload


@pytest.fixture
def evacuation(system):
    scheduler, state, transport = system
    leases = {}
    for name in ("a", "b"):
        state["models"][name] = replace(state["models"][name], util=0.4)
        transport.models[name]["util"] = 0.4
        lease_id = grant(scheduler, name, util=0.4)["lease_id"]
        ready(scheduler, state, lease_id)
        state["models"][name] = replace(state["models"][name], state="sleeping", is_sleeping=True)
        scheduler.sample_once()
        leases[name] = lease_id
    accounting = scheduler.placement
    # Reserve evacuation must not silently enable or require the placement API.
    scheduler.placement = None
    scheduler.config = replace(scheduler.config, model_actions_enabled=True, placement_enabled=False,
        reserve_timeout_seconds=2, action_observe_seconds=0.8, action_poll_seconds=0.005)
    state.update(calls=[], after_stop=None, mode="exit", counts={"a":0,"b":0})
    collect = scheduler.collect
    def observed():
        snapshot = collect()
        return replace(snapshot, activity=tuple(replace(item, in_flight=state["counts"][item.model]) for item in snapshot.activity))
    scheduler.collect = observed
    def run(argv, **kwargs):
        assert argv[0:2] == ["fake-systemctl", "stop"]
        name = next(name for name, unit in transport.units.items() if unit == argv[2])
        state["calls"].append(name)
        if state["mode"] in ("exit", "exit-error", "unknown-exit"):
            state["models"][name] = replace(state["models"][name], state="stopped", unit_active=False)
            state["observations"][name] = UnitObservation(False, True) if state["mode"] != "unknown-exit" else UnitObservation(True, False)
        if state["after_stop"]:
            state["after_stop"](name)
        return SimpleNamespace(returncode=1 if state["mode"] in ("error", "exit-error") else 0)
    transport.run = run
    scheduler.model_actions = ModelActionController(scheduler, transport)
    controller = ReservationController(scheduler, accounting=accounting)
    scheduler.sample_once()
    scheduler.start()
    yield scheduler, controller, state, transport, leases


def reserve(scheduler):
    return scheduler._save_reserve(payload(size_gb=20), source_ip="127.0.0.1")


def test_saved_intent_blocks_whole_gpu_and_sleepers_exit_without_place_api(evacuation):
    scheduler, controller, state, _, leases = evacuation
    record = reserve(scheduler)
    decision = plan_placement(scheduler.snapshot(), ModelState("new", state="stopped", util=0.1))
    assert not decision.actions and any(b.reason == "reserved" for b in decision.blocked_by)
    result = controller.evacuate(record)
    assert result == {"status":"complete", "stopped":["a","b"], "skipped":[]}
    assert state["calls"] == ["a","b"]
    assert all(scheduler.store.lease(value)[0].status == "released" for value in leases.values())
    assert scheduler.store.reserve(record.id) == record
    assert scheduler.placement is None and not scheduler.config.placement_enabled


@pytest.mark.parametrize("protection,reason", [("pin","pinned_until"), ("default","default_model"),
    ("inflight","in_flight"), ("unknown-inflight","unknown_in_flight"), ("awake","awake_model_untouched"),
    ("unleased","unleased_model")])
def test_protected_or_unsupported_models_stay_and_outcome_is_partial(evacuation, protection, reason):
    scheduler, controller, state, transport, leases = evacuation
    if protection == "pin":
        scheduler.store.put_pin(Pin("a", time.time()+100, "owner"))
    elif protection == "default":
        transport.models["a"]["is_default"] = True
    elif protection == "inflight":
        state["counts"]["a"] = 1
    elif protection == "unknown-inflight":
        state["counts"]["a"] = None
    elif protection == "awake":
        state["models"]["a"] = replace(state["models"]["a"], state="awake", is_sleeping=False)
    else:
        scheduler.store.transition_lease(leases["a"], "released")
    scheduler.sample_once()
    record = reserve(scheduler)
    result = controller.evacuate(record)
    assert result["status"] == "partial" and result["stopped"] == ["b"]
    assert reason in [row["reason"] for row in result["skipped"]]
    assert state["calls"] == ["b"] and state["models"]["a"].unit_active is True
    assert scheduler.store.reserve(record.id) == record


def test_new_pin_after_first_stop_is_revalidated_before_second(evacuation):
    scheduler, controller, state, _, leases = evacuation
    def protect(name):
        scheduler.store.put_pin(Pin("b", time.time()+100, "late-owner"))
    state["after_stop"] = protect
    result = controller.evacuate(reserve(scheduler))
    assert result["status"] == "partial" and result["stopped"] == ["a"]
    assert state["calls"] == ["a"] and scheduler.store.lease(leases["b"])[0].status == "confirmed"
    assert scheduler.snapshot().pins[0].model == "b"


@pytest.mark.parametrize("mode,stopped", [("error", []), ("no-effect", []), ("unknown-exit", []), ("exit-error", ["a"])])
def test_failure_or_unknown_exit_never_returns_fake_complete_or_releases_early(evacuation, mode, stopped):
    scheduler, controller, state, _, leases = evacuation
    state["mode"] = mode
    if mode != "exit-error":
        scheduler.config = replace(scheduler.config, action_observe_seconds=0.05)
    record = reserve(scheduler)
    result = controller.evacuate(record)
    assert result["status"] == ("partial" if stopped else "blocked")
    assert result["stopped"] == stopped and state["calls"] == ["a"]
    assert (scheduler.store.lease(leases["a"])[0].status == "released") is bool(stopped)
    assert scheduler.store.lease(leases["b"])[0].status == "confirmed"
    assert scheduler.store.reserve(record.id) == record
    assert not scheduler.model_actions.pending


@pytest.mark.parametrize("cancel", ["delete", "expiry"])
def test_delete_or_expiry_stops_future_steps_but_reports_submitted_effect(evacuation, cancel):
    scheduler, controller, state, _, leases = evacuation
    record = reserve(scheduler)
    def cancelled(name):
        if cancel == "delete":
            scheduler._delete_reserve(record.id, source_ip="127.0.0.1")
        else:
            scheduler.clock = lambda: record.until+1
            scheduler.config = replace(scheduler.config, max_snapshot_age_seconds=4000)
    state["after_stop"] = cancelled
    result = controller.evacuate(record)
    assert result["status"] == "partial" and result["stopped"] == ["a"]
    assert result["error"] == ("reservation_removed_or_changed" if cancel == "delete" else "reservation_expired")
    assert state["calls"] == ["a"] and scheduler.store.lease(leases["b"])[0].status == "confirmed"


def test_disabled_actuators_persist_intent_without_fake_evacuation(evacuation):
    scheduler, controller, state, _, _ = evacuation
    scheduler.config = replace(scheduler.config, model_actions_enabled=False)
    record = reserve(scheduler)
    result = controller.evacuate(record)
    assert result["status"] == "blocked" and result["stopped"] == []
    assert state["calls"] == [] and scheduler.store.reserve(record.id) == record


def test_dry_run_or_readonly_never_probes_transports_or_writes(evacuation, monkeypatch):
    scheduler, controller, state, transport, _ = evacuation
    scheduler.stop()
    record = scheduler._save_reserve(payload(), source_ip="127.0.0.1", dry_run=True)
    events = scheduler.events_since(0)
    before = open(scheduler.config.state_db_path, "rb").read()
    controller.accounting.probe = lambda *a, **k: pytest.fail("dry-run probed")
    transport.run = lambda *a, **k: pytest.fail("dry-run acted")
    scheduler.config = replace(scheduler.config, read_only=True)
    result = controller.evacuate(record, dry_run=True)
    assert [a["kind"] for a in result["would"]] == ["stop", "stop"]
    with pytest.raises(ActionDispatchError, match="read_only"):
        controller.evacuate(record)
    assert scheduler.events_since(0) == events
    assert open(scheduler.config.state_db_path, "rb").read() == before


@pytest.mark.parametrize("change", ["expired", "awake", "disabled"])
def test_final_probe_revalidation_prevents_invalidated_action(evacuation, change):
    scheduler, controller, state, _, _ = evacuation
    record = reserve(scheduler)
    probe = controller.accounting.probe
    def changed(model, *, deadline):
        value = probe(model, deadline=deadline)
        if change == "expired":
            scheduler.clock = lambda: record.until+1
        elif change == "awake":
            state["models"][model] = replace(state["models"][model], state="awake", is_sleeping=False)
            scheduler.sample_once()
        else:
            scheduler.config = replace(scheduler.config, model_actions_enabled=False)
        return value
    controller.accounting.probe = changed
    result = controller.evacuate(record)
    assert result["status"] == "blocked" and result["stopped"] == []
    assert state["calls"] == []
    assert result["error"] == {"expired":"reservation_expired", "awake":"model_no_longer_sleeping",
                               "disabled":"model_actions_not_enabled"}[change]


def test_changed_unit_identity_never_stops_a_bystander(evacuation):
    scheduler, controller, state, _, _ = evacuation
    record = reserve(scheduler)
    state["observations"]["a"] = UnitObservation(True, False, True, "other-lease")
    result = controller.evacuate(record)
    assert result["status"] == "blocked" and result["error"] == "unit_identity_unconfirmed"
    assert state["calls"] == [] and not scheduler.model_actions.pending


def test_store_failure_after_partial_progress_retains_observed_stops(evacuation):
    import sqlite3
    scheduler, controller, state, _, _ = evacuation
    record = reserve(scheduler)
    emit = scheduler.emit
    def fail_after_proof(kind, **kwargs):
        event = emit(kind, **kwargs)
        if kind == "reserve_action_result" and event.detail["confirmed"]:
            def failed_read(*args):
                raise sqlite3.OperationalError("fixture unavailable")
            scheduler.store.reserve = failed_read
        return event
    scheduler.emit = fail_after_proof
    result = controller.evacuate(record)
    assert result["status"] == "partial" and result["stopped"] == ["a"]
    assert result["error"] == "intent_store_unavailable" and state["calls"] == ["a"]
    assert not scheduler.model_actions.pending


def test_stalled_sampler_cannot_extend_one_reserve_deadline_or_release_budget(evacuation):
    import threading
    scheduler, controller, state, _, leases = evacuation
    record = reserve(scheduler)
    entered, release = threading.Event(), threading.Event()
    collect = scheduler.collect
    def stalled():
        if state["calls"]:
            entered.set()
            release.wait(1)
        return collect()
    scheduler.collect = stalled
    started = time.monotonic()
    try:
        result = controller.evacuate(record, deadline=started+0.08)
        assert time.monotonic()-started < 0.3 and entered.is_set()
        assert result["status"] == "blocked" and result["stopped"] == []
        assert state["calls"] == ["a"]
        assert scheduler.store.lease(leases["a"])[0].status == "confirmed"
    finally:
        release.set()


def test_delete_progresses_while_waiting_for_already_submitted_effect(evacuation):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    scheduler, controller, state, _, leases = evacuation
    record = reserve(scheduler)
    entered, release = threading.Event(), threading.Event()
    collect = scheduler.collect
    def waiting():
        if state["calls"]:
            entered.set()
            release.wait(1)
        return collect()
    scheduler.collect = waiting
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(controller.evacuate, record)
        try:
            assert entered.wait(1)
            scheduler._delete_reserve(record.id, source_ip="127.0.0.1")
            assert scheduler.store.reserve(record.id) is None
            release.set()
            result = pending.result(timeout=2)
            assert result["status"] == "partial" and result["stopped"] == ["a"]
            assert result["error"] == "reservation_removed_or_changed"
            assert state["calls"] == ["a"] and scheduler.store.lease(leases["b"])[0].status == "confirmed"
        finally:
            release.set()
