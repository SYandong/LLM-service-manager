# Generated-By: Codex / gpt-6-astra
"""Protected single-victim execution with real scheduler/HTTP concurrency."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llmsvc.actions import ModelActionController
from llmsvc.leases import LeaseError, UnitObservation
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Lease, ModelState, Pin, Reserve
from test_placement_leases import grant, ready, request, system


@pytest.fixture
def victims(system):
    scheduler, state, transport = system
    lease_id = grant(scheduler)["lease_id"]
    ready(scheduler, state, lease_id)
    scheduler.sample_once()
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"
    state.update(calls=[], mode="exit", inflight=0, idle=1000, after_stop=None)
    original_collect = scheduler.collect
    def collect():
        snapshot = original_collect()
        return replace(snapshot, activity=tuple(replace(item, in_flight=state["inflight"],
            last_request_at=time.time()-state["idle"]) if item.model == "a" else item for item in snapshot.activity))
    scheduler.collect = collect
    def run(argv, **kwargs):
        assert argv == ["fake-systemctl", "stop", "vllm-a.service"]
        assert kwargs["timeout"] > 0
        state["calls"].append(argv)
        if state["mode"] in ("exit", "exit-error", "uncertain-exit"):
            state["models"]["a"] = replace(state["models"]["a"], state="stopped", unit_active=False)
            state["observations"]["a"] = UnitObservation(False, True) if state["mode"] != "uncertain-exit" else UnitObservation(True, False)
        if state["after_stop"]:
            state["after_stop"]()
        return SimpleNamespace(returncode=1 if state["mode"] in ("error", "exit-error") else 0)
    transport.run = run
    scheduler.config = replace(scheduler.config, model_actions_enabled=True,
        placement_wait_seconds=0.3, action_observe_seconds=0.05, action_poll_seconds=0.002)
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.sample_once()
    scheduler.start()
    yield scheduler, state, transport, lease_id


def test_http_place_stops_one_confirmed_victim_then_grants_after_observed_exit(victims):
    scheduler, state, _, lease_id = victims
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        status, result = request(server.server_address, "/v1/place", {"model": "b", "util": 0.6})
        assert status == 200 and result["gpu"] == 0
        assert len(state["calls"]) == 1
        assert scheduler.store.lease(lease_id)[0].status == "released"
        assert scheduler.store.lease(result["lease_id"])[0].budget_gb == 60
        events = [e for e in scheduler.events_since(0) if e.kind in ("placement_action_result", "place")]
        assert events[-2].kind == "placement_action_result" and events[-2].detail["confirmed"] is True
        assert events[-1].model == "b"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("protection", ["pin", "default", "inflight", "unknown-inflight", "reserved", "unleased"])
def test_no_protected_reserved_or_unleased_victim_actions(victims, protection):
    scheduler, state, transport, lease_id = victims
    if protection == "pin":
        scheduler.store.put_pin(Pin("a", time.time()+100, "owner"))
    elif protection == "default":
        transport.models["a"]["is_default"] = True
    elif protection == "inflight":
        state["inflight"] = 1
    elif protection == "unknown-inflight":
        state["inflight"] = None
    elif protection == "reserved":
        scheduler.store.put_reserve(Reserve("held", 0, 1, time.time()+100, "owner"))
    else:
        # A legacy observed daemon is not automatically adopted into a fabricated lease.
        scheduler.store.transition_lease(lease_id, "released")
    scheduler.sample_once()
    with pytest.raises(LeaseError):
        grant(scheduler, "b")
    assert state["calls"] == []
    assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())


@pytest.mark.parametrize("mode,error,released", [("error", "placement_action_failed", False),
    ("exit-error", "placement_action_failed", True), ("no-effect", "placement_no_progress", False),
    ("uncertain-exit", "placement_no_progress", False)])
def test_failed_or_unconfirmed_effect_stops_request_without_budget_estimates(victims, mode, error, released):
    scheduler, state, _, lease_id = victims
    state["mode"] = mode
    with pytest.raises(LeaseError, match=error):
        grant(scheduler, "b")
    assert len(state["calls"]) == 1
    assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())
    assert (scheduler.store.lease(lease_id)[0].status == "released") is released
    assert not scheduler.model_actions.pending
    if mode == "exit-error":
        events = [event for event in scheduler.events_since(0) if event.kind == "placement_action_result"]
        assert events[-1].detail["confirmed"] is True
        assert events[-1].detail["error"] == "transport_rejected"


def test_replans_after_exit_and_does_not_grant_through_new_reservation(victims):
    scheduler, state, _, lease_id = victims
    def reserve():
        scheduler.store.put_reserve(Reserve("new", 0, 100, time.time()+100, "owner"))
    state["after_stop"] = reserve
    with pytest.raises(LeaseError, match="placement_timeout") as caught:
        grant(scheduler, "b")
    assert len(state["calls"]) == 1
    assert scheduler.store.lease(lease_id)[0].status == "released"
    assert "reserved" in [blocker.reason for blocker in caught.value.blockers]
    assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())


def test_dispatcher_revalidates_pin_added_after_policy_decision(victims):
    scheduler, state, _, _ = victims
    execute = scheduler.model_actions.dispatcher.execute
    def protected(action, **kwargs):
        scheduler.store.put_pin(Pin(action.model, time.time()+100, "late-owner"))
        return execute(action, **kwargs)
    scheduler.model_actions.dispatcher.execute = protected
    with pytest.raises(LeaseError, match="placement_action_failed") as caught:
        grant(scheduler, "b")
    assert caught.value.blockers[0].reason == "pinned"
    assert state["calls"] == []


def test_waiting_busy_victim_becomes_idle_before_action(victims):
    scheduler, state, _, _ = victims
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.7)
    state["inflight"] = 1
    scheduler.sample_once()
    waited = threading.Event()
    old_wait = scheduler.changed.wait
    def wait(timeout=None):
        waited.set()
        return old_wait(timeout)
    scheduler.changed.wait = wait
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(grant, scheduler, "b")
        assert waited.wait(0.3)
        with scheduler.changed:
            state["inflight"] = 0
            state["idle"] = 5
        scheduler.sample_once()
        time.sleep(0.03)
        assert state["calls"] == []  # waiting policy requires idle >30s
        with scheduler.changed:
            state["idle"] = 31
        scheduler.sample_once()
        assert pending.result(timeout=1)["gpu"] == 0
    assert len(state["calls"]) == 1


def test_dry_run_reports_policy_actions_without_executor_or_sampling(victims):
    scheduler, state, _, _ = victims
    scheduler.stop()
    before = open(scheduler.config.state_db_path, "rb").read()
    scheduler.collect = lambda: pytest.fail("dry-run collected")
    result = scheduler.preview("place", {"model": "b", "util": 0.6})
    assert [action["kind"] for action in result["would"]] == ["stop", "place"]
    assert state["calls"] == []
    assert open(scheduler.config.state_db_path, "rb").read() == before


def test_slow_sampler_cannot_extend_placement_resource_wait(victims):
    scheduler, state, _, _ = victims
    blocked = threading.Event()
    release = threading.Event()
    collect = scheduler.collect
    def delayed_collect():
        if state["calls"]:
            blocked.set()
            release.wait(1)
        return collect()
    scheduler.collect = delayed_collect
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08, action_observe_seconds=0.06)
    started = time.monotonic()
    try:
        with pytest.raises(LeaseError):
            grant(scheduler, "b")
        assert blocked.is_set()
        assert time.monotonic()-started < 0.25
        assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())
    finally:
        release.set()


def test_no_stop_when_configured_unit_now_belongs_to_another_lease(victims):
    scheduler, state, _, lease_id = victims
    state["observations"]["a"] = UnitObservation(True, False, True, "another-owner")
    with pytest.raises(LeaseError, match="placement_action_blocked") as caught:
        grant(scheduler, "b")
    assert caught.value.blockers[0].reason == "unit_identity_unconfirmed"
    assert state["calls"] == []
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"


def test_placement_optin_alone_does_not_enable_victim_actions(victims):
    scheduler, state, _, _ = victims
    scheduler.config = replace(scheduler.config, model_actions_enabled=False)
    with pytest.raises(LeaseError, match="placement_timeout") as caught:
        grant(scheduler, "b")
    assert "eviction_required" in [blocker.reason for blocker in caught.value.blockers]
    assert state["calls"] == []


def test_concurrent_placements_do_not_stop_same_victim_twice_or_double_grant(victims):
    scheduler, state, transport, _ = victims
    state["models"]["c"] = ModelState("c", state="stopped", unit="vllm-c.service", unit_active=False,
                                       weights_gb=10, util=0.6)
    transport.models["c"] = {"util": 0.6, "weights_gb": 10}
    transport.units["c"] = "vllm-c.service"
    scheduler.sample_once()
    def place(name):
        try:
            return grant(scheduler, name)
        except LeaseError as exc:
            return exc
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(place, ("b", "c")))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert len(state["calls"]) == 1
    assert sum(lease.budget_gb for lease, _ in scheduler.store.leases()) == 60
    assert not scheduler.model_actions.pending


def test_replans_each_victim_after_new_pin_instead_of_using_old_list(victims):
    scheduler, state, transport, _ = victims
    # Two 30 GiB daemons must both exit for an 80 GiB request on a 100 GiB card.
    scheduler.store.transition_lease(scheduler.store.leases()[0][0].lease_id, "released")
    scheduler.store.create_lease(Lease("small-a", "a", 0, 0.3, time.time()+100, 30), "vllm-a.service")
    ready(scheduler, state, "small-a")
    state["models"]["a"] = replace(state["models"]["a"], util=0.3)
    state["models"]["c"] = ModelState("c", state="awake", unit="vllm-c.service", unit_active=True,
        health_ok=True, is_sleeping=False, gpu=0, weights_gb=10, util=0.3, budget_gb=30, cold_start_seconds=2)
    transport.models["c"] = {"util": 0.3, "weights_gb": 10}
    transport.units["c"] = "vllm-c.service"
    transport.models["b"]["util"] = 0.8
    scheduler.store.create_lease(Lease("small-c", "c", 0, 0.3, time.time()+100, 30), "vllm-c.service")
    state["observations"]["c"] = UnitObservation(True, False, True, "small-c")
    def pin_second():
        scheduler.store.put_pin(Pin("c", time.time()+100, "new-owner"))
    state["after_stop"] = pin_second
    scheduler.sample_once()
    with pytest.raises(LeaseError, match="placement_timeout") as caught:
        grant(scheduler, "b", util=0.8)
    assert len(state["calls"]) == 1
    assert "pinned_until" in [blocker.reason for blocker in caught.value.blockers]
    assert state["models"]["c"].unit_active is True


def test_uncertain_late_submission_keeps_budget_and_reports_timeout_blocker(victims):
    scheduler, state, transport, lease_id = victims
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.04)
    def run(argv, **kwargs):
        state["calls"].append(argv)
        time.sleep(kwargs["timeout"]+0.005)
        return SimpleNamespace(returncode=0)
    transport.run = run
    with pytest.raises(LeaseError, match="placement_timeout") as caught:
        grant(scheduler, "b")
    assert caught.value.blockers[0].reason == "deadline_exceeded"
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"
    assert not scheduler.model_actions.pending
    events = [event for event in scheduler.events_since(0) if event.kind == "placement_action_result"]
    assert events[-1].detail["confirmed"] is False
