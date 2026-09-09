# Generated-By: Codex / gpt-6-astra
"""Protected single-victim execution with real scheduler/HTTP concurrency."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from llmsvc.actions import ModelActionController
from llmsvc.leases import LeaseError, UnitObservation
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Lease, ModelState, Pin, Reserve
from test_placement_leases import grant, ready, request, system


def initialize_victim(scheduler, state):
    # Keep functional headroom explicit BEFORE the first grant. Dedicated
    # negative deadline cases override this later, after successful setup.
    scheduler.config = replace(scheduler.config, placement_wait_seconds=2.0)
    snapshot = scheduler.snapshot()
    started = scheduler.placement.monotonic()
    proof = {"phase": "initial_victim_grant", "budget_seconds": scheduler.config.placement_wait_seconds,
             "sampled_at": snapshot.sampled_at, "sample_age_seconds": scheduler.clock()-snapshot.sampled_at,
             "snapshot_errors": list(snapshot.errors), "models": {m.name: m.state for m in snapshot.models},
             "leases_before": [asdict(lease) for lease in snapshot.leases]}
    state["setup_proof"] = proof
    error = None
    try:
        lease_id = grant(scheduler)["lease_id"]
    except LeaseError as exc:
        error = exc
        proof.update(error=exc.error, blockers=[asdict(b) for b in exc.blockers])
    finally:
        proof.update(elapsed_seconds=scheduler.placement.monotonic()-started,
                     probes=list(state["probes"]),
                     leases_after=[asdict(lease) for lease, _ in scheduler.store.leases()])
    if error is not None:
        raise AssertionError(f"Victim setup grant failed: {proof}") from error
    ready(scheduler, state, lease_id)
    scheduler.sample_once()
    assert scheduler.store.lease(lease_id)[0].status == "confirmed", proof
    return lease_id


@pytest.fixture
def victims(system):
    scheduler, state, transport = system
    lease_id = initialize_victim(scheduler, state)
    state.update(calls=[], mode="exit", inflight=0, idle=1000, after_stop=None, timeline=[])
    started = time.monotonic()
    def record(phase, **detail):
        state["timeline"].append({"ms": round((time.monotonic()-started)*1000, 3), "phase": phase, **detail})
    state["record"] = record
    original_emit = scheduler.emit
    def emit(kind, **kwargs):
        event = original_emit(kind, **kwargs)
        if kind in ("state", "lease_released", "placement_action_result", "place"):
            record("event", kind=kind, detail=event.detail)
        return event
    scheduler.emit = emit
    original_observe = scheduler.placement._observe_victim
    def observe(action, deadline):
        record("observation_begin", remaining_ms=round((deadline-time.monotonic())*1000, 3))
        result = original_observe(action, deadline)
        record("observation_end", confirmed=result)
        return result
    scheduler.placement._observe_victim = observe
    original_collect = scheduler.collect
    def collect():
        snapshot = original_collect()
        record("collect", sampled_at=snapshot.sampled_at, model_states={m.name: m.state for m in snapshot.models})
        return replace(snapshot, activity=tuple(replace(item, in_flight=state["inflight"],
            last_request_at=time.time()-state["idle"]) if item.model == "a" else item for item in snapshot.activity))
    scheduler.collect = collect
    def run(argv, **kwargs):
        assert argv == ["fake-systemctl", "stop", "vllm-a.service"]
        assert kwargs["timeout"] > 0
        state["calls"].append(argv)
        record("stop_submit")
        if state["mode"] in ("exit", "exit-error", "uncertain-exit"):
            state["models"]["a"] = replace(state["models"]["a"], state="stopped", unit_active=False)
            state["observations"]["a"] = UnitObservation(False, True) if state["mode"] != "uncertain-exit" else UnitObservation(True, False)
        if state["after_stop"]:
            state["after_stop"]()
        code = 1 if state["mode"] in ("error", "exit-error") else 0
        record("stop_return", code=code)
        return SimpleNamespace(returncode=code)
    transport.run = run
    # Functional results require two asynchronous rounds and durable SQLite
    # reconciliation, not a 50ms host scheduling/IO performance guarantee.
    # Dedicated negative tests override their short deadlines explicitly.
    scheduler.config = replace(scheduler.config, model_actions_enabled=True,
        placement_wait_seconds=2.0, action_observe_seconds=1.0, action_poll_seconds=0.002)
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.sample_once()
    scheduler.start()
    yield scheduler, state, transport, lease_id


def diagnostic(scheduler, state, response=None):
    """Keep CI's actual response and last observation/account steps on failure."""
    return {"response": response, "setup": state["setup_proof"], "timeline": state["timeline"][-40:],
            "leases": [asdict(lease) for lease, _ in scheduler.store.leases(include_released=True)],
            "action_results": [event.detail for event in scheduler.events_since(0) if event.kind == "placement_action_result"]}


def test_http_place_stops_one_confirmed_victim_then_grants_after_observed_exit(victims):
    scheduler, state, _, lease_id = victims
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        status, result = request(server.server_address, "/v1/place", {"model": "b", "util": 0.6})
        assert status == 200 and result["gpu"] == 0, diagnostic(scheduler, state, (status, result))
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
    if mode != "exit-error":
        # Deliberately no exit: retain a short no-progress observation test.
        scheduler.config = replace(scheduler.config, action_observe_seconds=0.05)
    with pytest.raises(LeaseError, match=error):
        grant(scheduler, "b")
    assert len(state["calls"]) == 1
    assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())
    assert (scheduler.store.lease(lease_id)[0].status == "released") is released
    assert not scheduler.model_actions.pending
    if mode == "exit-error":
        events = [event for event in scheduler.events_since(0) if event.kind == "placement_action_result"]
        assert events[-1].detail["confirmed"] is True, diagnostic(scheduler, state)
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
    assert "reserved" in [blocker.reason for blocker in caught.value.blockers], diagnostic(
        scheduler, state, {"error": caught.value.error, "blockers": [asdict(b) for b in caught.value.blockers]})
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
    state["inflight"] = 1
    scheduler.sample_once()
    busy_seen, recent_seen = threading.Event(), threading.Event()
    decide = scheduler.placement._decision
    def observed_decision(snapshot, request, *, waiting):
        decision, blockers = decide(snapshot, request, waiting=waiting)
        if request.name == "b":
            reasons = [blocker.reason for blocker in blockers]
            state["record"]("waiting_decision", waiting=waiting, reasons=reasons)
            if "in_flight" in reasons:
                busy_seen.set()
            if waiting and "recently_active" in reasons:
                recent_seen.set()
        return decision, blockers
    scheduler.placement._decision = observed_decision
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(grant, scheduler, "b")
            assert busy_seen.wait(2), diagnostic(scheduler, state)
            with scheduler.changed:
                state["inflight"] = 0
                state["idle"] = 5
            scheduler.sample_once()
            # Wait for the actual waiting-policy rejection of the five-second
            # observation, not an assumed scheduling interval after publication.
            assert recent_seen.wait(2), diagnostic(scheduler, state)
            with scheduler.changed:
                assert state["calls"] == [] and not pending.done(), diagnostic(scheduler, state)
                state["idle"] = 31
            scheduler.sample_once()
            assert pending.result(timeout=3)["gpu"] == 0
    finally:
        scheduler.placement._decision = decide
    assert len(state["calls"]) == 1, diagnostic(scheduler, state)


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


@pytest.mark.parametrize("scenario,pause", [("http", 0.08), ("exit-error", 0.08), ("reservation", 0.35)])
def test_functional_observation_tolerates_controlled_lock_latency(victims, scenario, pause):
    """Reproduce #100's three shapes without assuming the historical CI timing.

    Delayed release notification/thread scheduling is not evidence of a failed
    stop. Functional fixtures must allow two newer rounds; dedicated deadline tests
    keep explicit short limits. The pause exists only in this mock test hook.
    """
    scheduler, state, _, lease_id = victims
    observation_started = threading.Event()
    observe = scheduler.placement._observe_victim
    def observe_started(action, deadline):
        observation_started.set()
        return observe(action, deadline)
    scheduler.placement._observe_victim = observe_started
    emit = scheduler.emit
    def delayed_notification(kind, **kwargs):
        event = emit(kind, **kwargs)
        if kind == "lease_released":
            assert observation_started.wait(1)
            state["record"]("controlled_lock_delay_begin", seconds=pause)
            time.sleep(pause)
            state["record"]("controlled_lock_delay_end")
        return event
    scheduler.emit = delayed_notification
    if scenario == "exit-error":
        state["mode"] = "exit-error"
    elif scenario == "reservation":
        state["after_stop"] = lambda: scheduler.store.put_reserve(Reserve("new", 0, 100, time.time()+100, "owner"))
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        status, body = request(server.server_address, "/v1/place", {"model": "b", "util": 0.6})
        proof = diagnostic(scheduler, state, (status, body))
        assert len(state["calls"]) == 1, proof
        assert scheduler.store.lease(lease_id)[0].status == "released", proof
        assert proof["action_results"][-1]["confirmed"] is True, proof
        if scenario == "http":
            assert status == 200 and body["gpu"] == 0, proof
            assert sum(lease.model == "b" for lease, _ in scheduler.store.leases()) == 1, proof
        else:
            assert not any(lease.model == "b" for lease, _ in scheduler.store.leases()), proof
            if scenario == "exit-error":
                assert status == 503 and body["error"] == "placement_action_failed", proof
                assert proof["action_results"][-1]["error"] == "transport_rejected", proof
            else:
                assert status == 409 and body["error"] == "placement_timeout", proof
                assert "reserved" in [item["reason"] for item in body["blockers"]], proof
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)



def test_grant_waits_for_exit_proof_and_a_second_published_round(victims):
    scheduler, state, _, lease_id = victims
    first_started, second_started = threading.Event(), threading.Event()
    allow_first, allow_second = threading.Event(), threading.Event()
    collect = scheduler.collect
    post_stop_rounds = [0]
    def gated_collect():
        snapshot = collect()
        if state["calls"]:
            post_stop_rounds[0] += 1
            if post_stop_rounds[0] == 1:
                first_started.set()
                assert allow_first.wait(1), "fixture did not release first observation"
            elif post_stop_rounds[0] == 2:
                second_started.set()
                assert allow_second.wait(1), "fixture did not release second observation"
        return snapshot
    scheduler.collect = gated_collect
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(grant, scheduler, "b")
        try:
            assert first_started.wait(1), diagnostic(scheduler, state)
            assert len(state["calls"]) == 1
            assert scheduler.store.lease(lease_id)[0].status == "confirmed"
            assert not pending.done(), diagnostic(scheduler, state)
            assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())
            allow_first.set()
            assert second_started.wait(1), diagnostic(scheduler, state)
            # First fresh stopped round + positive configured-unit exit proof
            # releases the old ledger row, but does not yet grant this request.
            assert scheduler.store.lease(lease_id)[0].status == "released"
            assert not pending.done(), diagnostic(scheduler, state)
            assert not any(lease.model == "b" for lease, _ in scheduler.store.leases())
            allow_second.set()
            result = pending.result(timeout=2)
            assert result["gpu"] == 0
            assert sum(lease.model == "b" for lease, _ in scheduler.store.leases()) == 1
            assert len(state["calls"]) == 1 and post_stop_rounds[0] >= 2
        finally:
            allow_first.set()
            allow_second.set()
