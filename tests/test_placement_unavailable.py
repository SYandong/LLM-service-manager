# Generated-By: Codex / gpt-6.1-sol
"""Offline fast failure with simulated time and the existing sampler protocol."""

import http.client
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport
from llmsvc.config import SchedulerConfig
from llmsvc.leases import LeaseError, PlacementController, UnitObservation
from llmsvc.policy import PolicySettings
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import GPUState, MemoryState, ModelState, Reserve, StateSnapshot
from llmsvc.store import IntentStore


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def wall(self):
        return 1000 + self.now


@pytest.fixture
def blocked_system(tmp_path):
    clock = Clock()
    state = {"external": (60, 60), "errors": (), "consume_requests": True,
             "sample_time": lambda: 1000 + state.get("round_started", clock.now),
             "sample_hook": lambda: None, "probes": [],
             "sample_requests": [], "next_periodic": 15.0, "pending_sample": None,
             "collector_delay": 0.25, "periodic_interval": 15.0,
             "bounds_transform": lambda bounds: bounds}

    def collect():
        state["sample_hook"]()
        return StateSnapshot(sampled_at=state["sample_time"](), errors=state["errors"],
            gpus=tuple(GPUState(index, total_gb=100, free_gb=100-external, external_gb=external)
                       for index, external in enumerate(state["external"])),
            models=(ModelState("incoming", state="stopped", weights_gb=10, unit_active=False),),
            memory=MemoryState(500, 0))

    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, placement_enabled=True,
        state_db_path=str(tmp_path / "state.sqlite"), shared_external_threshold_gb=30,
        action_poll_seconds=0.25)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    scheduler = Scheduler(config, collect, store=store, clock=clock.wall, monotonic=clock.monotonic)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:1", systemctl="fixture-systemctl",
        models={"incoming": {"util": 0.6, "weights_gb": 10}},
        run=lambda *args, **kwargs: pytest.fail("no placement actuator invocation"))

    def probe(model, *, deadline):
        state["probes"].append(model)
        return UnitObservation(False, True)

    scheduler.placement = PlacementController(scheduler, transport, probe=probe, monotonic=clock.monotonic)
    clock.now = -0.01
    scheduler.sample_once()
    clock.now = 0

    # Simulate only the sampler's event consumption and publication. The place
    # handler cannot invoke collect; default periodic sampling remains 15s.
    original_request = scheduler.request_sample

    def request_sample():
        state["sample_requests"].append(clock.now)
        original_request()

    scheduler.request_sample = request_sample

    def wait(timeout=None):
        if state["consume_requests"] and scheduler.sample_requested.is_set():
            scheduler.sample_requested.clear()
            if state["pending_sample"] is None:
                state["pending_sample"] = clock.now + state["collector_delay"]
                state["pending_started"] = clock.now
        due = state["next_periodic"]
        if state["pending_sample"] is not None:
            due = min(due, state["pending_sample"])
        clock.now = min(clock.now + timeout, due)
        if clock.now >= due:
            started = state["pending_started"] if due == state["pending_sample"] else clock.now
            state["pending_sample"] = None
            state["next_periodic"] = clock.now + state["periodic_interval"]
            state["round_started"] = started
            scheduler.sample_once()
            # Model the complete read-only interval, including collector delay;
            # source sampled_at is the start, not the publication/completion.
            scheduler._sample_bounds = state["bounds_transform"]((scheduler._sample_published, started, clock.now))

    scheduler.changed.wait = wait
    try:
        yield scheduler, state, clock
    finally:
        scheduler.stop()
        store.close()


def place(scheduler):
    return scheduler.placement.place({"model": "incoming", "util": 0.6})


def test_default_grace_with_15_second_sampler_fails_below_twenty_seconds(blocked_system):
    scheduler, state, clock = blocked_system
    # The first observation can be nearly a periodic interval old; the grace
    # still starts at this request, then accelerated requests get fresh rounds.
    scheduler._sample_bounds = (1, -14, -14)
    scheduler._snapshot = replace(scheduler._snapshot, sampled_at=986)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    error = caught.value
    assert 10 <= clock.now < 20
    assert (error.status, error.error, error.retry_after) == (503, "no_feasible_gpu", 60)
    assert error.message == "All placement GPUs are occupied by workloads outside llmsvc"
    assert error.gpus == ({"index": 0, "free_gb": 40, "external_gb": 60},
                          {"index": 1, "free_gb": 40, "external_gb": 60})
    assert not scheduler.store.leases() and not state["probes"]
    assert scheduler.config.sample_interval_seconds == 15
    assert scheduler.config.fault_interval_seconds == 1 and not scheduler.config.fault_recovery_enabled
    assert state["sample_requests"][0] == 0
    assert all(b-a >= 1 for a, b in zip(state["sample_requests"], state["sample_requests"][1:]))
    events = [e for e in scheduler.events_since(0) if e.kind == "placement_unplaceable"]
    assert len(events) == 1 and events[0].model == "incoming"
    assert events[0].detail["retry_after_seconds"] == 60
    assert [b["external_gb"] for b in events[0].detail["blockers"] if b["reason"] == "external_pressure"] == [60, 60]


@pytest.mark.parametrize("delay_at", ["classification", "freshness_check", "error_preparation"])
@pytest.mark.parametrize("delay_seconds", [0.75, 2.0])
def test_original_deadline_wins_when_unplaceable_proof_finishes_late(blocked_system, monkeypatch, delay_at, delay_seconds):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=4, placement_unplaceable_grace_seconds=3)
    method = {"classification": "_decision", "freshness_check": "_unplaceable_sample",
              "error_preparation": "_unplaceable_error"}[delay_at]
    original = getattr(scheduler.placement, method)
    delayed = False

    def pause_on_grace_reaching_round():
        nonlocal delayed
        # Delaying at wall time 3 before the finished round would miss the bug:
        # the actual final observation must first reach the grace endpoint.
        if not delayed and scheduler._sample_bounds[2] >= 3:
            delayed = True
            clock.now += delay_seconds

    def slow_proof(*args, **kwargs):
        if delay_at != "freshness_check":
            pause_on_grace_reaching_round()
        result = original(*args, **kwargs)
        if delay_at == "freshness_check":
            pause_on_grace_reaching_round()
        return result

    monkeypatch.setattr(scheduler.placement, method, slow_proof)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert delayed and clock.now == 3.25 + delay_seconds
    assert scheduler._sample_bounds == (5, 3, 3.25)
    assert (caught.value.status, caught.value.error) == (409, "placement_timeout")
    assert caught.value.retry_after is None
    assert {b.gpu: b.external_gb for b in caught.value.blockers if b.reason == "external_pressure"} == {0: 60, 1: 60}
    assert not scheduler.store.leases() and not state["probes"]
    assert all(requested < 4 for requested in state["sample_requests"])
    assert not any(event.kind == "placement_unplaceable" for event in scheduler.events_since(0))


@pytest.mark.parametrize("duration", [3, 7])
def test_normal_multi_second_collector_rounds_remain_fresh_and_finish_below_twenty(blocked_system, duration):
    scheduler, state, clock = blocked_system
    state["collector_delay"] = duration
    scheduler._sample_bounds = (1, -14, -14)
    scheduler._snapshot = replace(scheduler._snapshot, sampled_at=986)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.status == 503 and 10 <= clock.now < 20
    assert scheduler._sample_bounds[2] - scheduler._sample_bounds[1] == duration
    assert scheduler.snapshot().sampled_at == 1000 + scheduler._sample_bounds[1]


@pytest.mark.parametrize("grace", [0, 10])
def test_notifications_and_reused_snapshot_never_supply_second_sample(blocked_system, grace):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=12, placement_unplaceable_grace_seconds=grace)
    state["consume_requests"] = False
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert (caught.value.status, caught.value.error) == (409, "placement_timeout")
    assert clock.now == 12 and scheduler._sample_published == 1
    assert not scheduler.store.leases()


def test_new_generation_with_duplicate_wall_time_is_not_fresh_evidence(blocked_system):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=12)
    state["sample_time"] = lambda: 1000
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert (caught.value.status, caught.value.error) == (409, "placement_timeout")
    assert scheduler._sample_published > 2 and clock.now == 12
    assert not any(e.kind == "placement_unplaceable" for e in scheduler.events_since(0))


def test_missing_collector_timestamp_cannot_use_synthesised_freshness(blocked_system, monkeypatch):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=12)
    monkeypatch.setattr("llmsvc.scheduler.time.time", clock.wall)
    state["sample_time"] = lambda: None
    scheduler.sample_once()
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert (caught.value.status, caught.value.error) == (409, "placement_timeout")
    assert 0 <= clock.wall() - scheduler.snapshot().sampled_at < scheduler.config.max_snapshot_age_seconds
    assert scheduler._sample_source_time_provided is False
    assert not scheduler.store.leases()


@pytest.mark.parametrize("invalid", ["future", "stale", "wrong_generation"])
def test_invalid_collection_bounds_do_not_accumulate_grace(blocked_system, invalid):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=12)

    def invalid_bounds(bounds):
        generation, started, finished = bounds
        if invalid == "future":
            return generation, finished + 1, finished + 2
        if invalid == "stale":
            return generation, finished - 31, finished
        return generation - 1, started, finished

    state["bounds_transform"] = invalid_bounds
    scheduler._sample_bounds = invalid_bounds(scheduler._sample_bounds)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert (caught.value.status, caught.value.error) == (409, "placement_timeout")
    assert scheduler._sample_published >= 2 and clock.now == 12
    assert not any(e.kind == "placement_unplaceable" for e in scheduler.events_since(0))


def test_backwards_source_timestamp_restarts_grace(blocked_system):
    scheduler, state, clock = blocked_system
    state["sample_time"] = lambda: clock.wall() - (2 if clock.now >= 3 else 0)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.status == 503 and 14 <= clock.now < 20


def test_zero_grace_still_waits_for_two_distinct_fresh_rounds(blocked_system):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_unplaceable_grace_seconds=0)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.status == 503
    assert scheduler._sample_published == 2 and clock.now == state["collector_delay"]


def test_external_pressure_disappearing_during_grace_places_normally(blocked_system):
    scheduler, state, clock = blocked_system

    def clear_external():
        if clock.now >= 3:
            state["external"] = (0, 0)

    state["sample_hook"] = clear_external
    result = place(scheduler)
    assert result["gpu"] == 0 and 3 <= clock.now < 10
    assert len(scheduler.store.leases()) == 1
    assert not any(e.kind == "placement_unplaceable" for e in scheduler.events_since(0))


def test_one_reserved_candidate_retains_original_409_deadline(blocked_system):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=12)
    scheduler.store.put_reserve(Reserve("hold", 1, 20, 2000, "synthetic-owner"))
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert (caught.value.status, caught.value.error) == (409, "placement_timeout")
    assert "reserved" in [b.reason for b in caught.value.blockers]
    assert clock.now == 12 and not state["sample_requests"]
    assert caught.value.retry_after is None and not scheduler.store.leases()


def test_transient_sample_resets_entire_grace_window(blocked_system):
    scheduler, state, clock = blocked_system
    state["next_periodic"] = state["periodic_interval"] = 1.0

    def transient_round():
        state["errors"] = ("fixture collector gap",) if 3 <= clock.now < 4 else ()

    state["sample_hook"] = transient_round
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.status == 503
    assert 14 <= clock.now < 20
    assert not scheduler.store.leases()


def test_oversized_budget_fails_with_capacity_message_and_zero_actions(blocked_system):
    scheduler, state, clock = blocked_system
    state["external"] = (0, 0)
    scheduler.placement.transport.models["incoming"]["budget_gb"] = 150
    scheduler.sample_once()
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.status == 503 and "total capacity" in caught.value.message
    assert [b.reason for b in caught.value.blockers if b.gpu is not None] == [
        "request_exceeds_gpu_capacity", "request_exceeds_gpu_capacity"]
    assert not state["probes"] and not scheduler.store.leases()


def test_recovery_error_summary_uses_its_actual_destination_pool(blocked_system, monkeypatch):
    scheduler, state, _ = blocked_system
    claim = SimpleNamespace(source_gpu=0, util_floor=0.6, budget_floor_gb=60)
    settings = PolicySettings(placement_gpus=(1,), shared_external_threshold_gb=30)
    scheduler.sleeping_recovery = SimpleNamespace(deadline=120, controller=SimpleNamespace(settings=settings))
    monkeypatch.setattr(scheduler.placement, "_recovery_context", lambda model: claim)
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.status == 503
    assert caught.value.gpus == ({"index": 1, "free_gb": 40, "external_gb": 60},)
    assert caught.value.message == "All placement GPUs are occupied by workloads outside llmsvc"
    assert not state["probes"] and not scheduler.store.leases()


@pytest.mark.parametrize("status", [503, 409])
def test_http_unavailable_contract_and_unchanged_timeout_contract(blocked_system, status):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, placement_retry_after_seconds=37)
    if status == 409:
        scheduler.config = replace(scheduler.config, placement_wait_seconds=12)
        scheduler.store.put_reserve(Reserve("hold", 1, 20, 2000, "synthetic-owner"))
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("POST", "/v1/place", json.dumps({"model": "incoming", "util": 0.6}))
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == status
        if status == 503:
            assert response.getheader("Retry-After") == "37"
            assert payload["error"] == "no_feasible_gpu"
            assert payload["retryable"] is True and payload["retry_after_seconds"] == 37
            assert "outside llmsvc" in payload["message"]
            assert len(payload["gpus"]) == 2 and payload["gpus"][0]["external_gb"] == 60
            assert payload["blockers"][0]["external_gb"] == 60
        else:
            assert response.getheader("Retry-After") is None
            assert set(payload) == {"error", "blockers"} and payload["error"] == "placement_timeout"
        assert not scheduler.store.leases() and not state["probes"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_dry_run_does_not_request_sampling_emit_or_allocate(blocked_system):
    scheduler, state, _ = blocked_system
    before = scheduler.events_since(0)
    result = scheduler.placement.preview("place", {"model": "incoming", "util": 0.6})
    assert result["would"] == []
    assert "external_pressure" in [b["reason"] for b in result["blocked_by"]]
    assert scheduler.events_since(0) == before
    assert not state["sample_requests"] and not state["probes"] and not scheduler.store.leases()


def test_real_sampler_and_http_default_grace_finish_below_twenty_seconds(tmp_path, record_property):
    """Measured loopback path; fake GPUs, no subprocess or production service."""
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, placement_enabled=True,
        state_db_path=str(tmp_path / "real-sampler.sqlite"), shared_external_threshold_gb=30)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())

    def collect():
        sampled_at = time.time()
        time.sleep(0.05)  # Bounded synthetic collector; sampler remains real.
        return StateSnapshot(sampled_at=sampled_at,
            gpus=(GPUState(0, total_gb=100, free_gb=40, external_gb=60),),
            models=(ModelState("incoming", state="stopped", weights_gb=10, unit_active=False),),
            memory=MemoryState(500, 0))

    scheduler = Scheduler(config, collect, store=store)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:1", systemctl="fixture-systemctl",
        models={"incoming": {"util": 0.6, "weights_gb": 10}},
        run=lambda *args, **kwargs: pytest.fail("no actuator invocation"))
    scheduler.placement = PlacementController(scheduler, transport,
        probe=lambda *args, **kwargs: pytest.fail("no unit probe needed for external pressure"))
    scheduler.start(sampling_only=True)
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=25)
    try:
        scheduler.await_initial_sample(time.monotonic() + 2)
        started = time.monotonic()
        connection.request("POST", "/v1/place", json.dumps({"model": "incoming", "util": 0.6}))
        response = connection.getresponse()
        payload = json.loads(response.read())
        elapsed = time.monotonic() - started
        record_property("placement_http_elapsed_seconds", elapsed)
        assert response.status == 503 and response.getheader("Retry-After") == "60"
        assert payload["retryable"] is True and payload["error"] == "no_feasible_gpu"
        assert 10 <= elapsed < 20, elapsed
        assert scheduler._sample_published >= 2 and not store.leases()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
        scheduler.stop()
        store.close()
