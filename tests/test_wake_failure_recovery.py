# Generated-By: Claude Code / claude-opus-5-5
"""Wedged-wake recovery (#300) with fake units, SQLite and a loopback llama-swap."""

import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.scheduler import Scheduler
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, StateSnapshot
from llmsvc.store import IntentStore
from llmsvc.wake_failure import WakeFailureRecoveryController

NAME = "qwen3.8-27b"
UNIT = "vllm-" + NAME + ".service"


@pytest.fixture
def system(tmp_path):
    state = {"now": 1791104461.0, "step": 15.0, "calls": [], "inflight": 5, "unload_status": 200, "errors": ()}
    models = {NAME: ModelState(NAME, state="sleeping", gpu=0, util=0.7, budget_gb=98, weights_gb=55,
                               resident_gb=88, unit=UNIT, unit_active=True, health_ok=True,
                               is_sleeping=True, swap_state="ready", cold_start_seconds=120)}
    observations = {NAME: UnitObservation(True, False, True, "lease-q", "a"*32)}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/api/models/unload/" + NAME
            state["calls"].append(("unload", NAME))
            models[NAME] = replace(models[NAME], swap_state="stopped")
            state["inflight"] = 0
            self.send_response(state["unload_status"])
            self.send_header("Content-Length", "0")
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))
    thread.start()
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
        wake_failure_recovery_enabled=True, wake_failure_grace_seconds=60,
        state_db_path=str(tmp_path / "state.sqlite"), free_timeout_seconds=3,
        action_poll_seconds=0.005, request_timeout_seconds=1, lease_probe_seconds=0.1)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    store.create_lease(Lease("lease-q", NAME, 0, 0.7, state["now"] + 86400, 98), UNIT)
    store.transition_lease("lease-q", "confirmed")

    def collect():
        state["now"] += state["step"]
        sleeping = sum(m.weights_gb for m in models.values() if m.state == "sleeping")
        return StateSnapshot(sampled_at=state["now"], models=tuple(models.values()), errors=state["errors"],
            gpus=(GPUState(0, total_gb=140, free_gb=10, external_gb=40),),
            memory=MemoryState(400, sleeping),
            activity=(Activity(NAME, state["now"] - 1, 900, 80, state["inflight"], ("client",)),))

    scheduler = Scheduler(config, collect, store=store, clock=lambda: state["now"])

    def run(argv, **kwargs):
        assert argv == ["fake-systemctl", "stop", UNIT] and kwargs["timeout"] > 0
        state["calls"].append(("stop", NAME))
        # The backend dies; llama-swap still believes the model is ready.
        models[NAME] = replace(models[NAME], state="stopped", unit_active=False, health_ok=None,
                               is_sleeping=None, resident_gb=0)
        observations[NAME] = UnitObservation(False, True)
        return SimpleNamespace(returncode=0)

    transport = ManagedModelTransport(swap_url="http://127.0.0.1:" + str(upstream.server_port),
        models={NAME: {"unit": UNIT}}, systemctl="fake-systemctl", run=run)
    scheduler.model_actions = ModelActionController(scheduler, transport)
    accounting = PlacementController(scheduler, transport, probe=lambda name, *, deadline: observations[name])
    recovery = WakeFailureRecoveryController(scheduler, accounting=accounting)
    try:
        yield SimpleNamespace(scheduler=scheduler, recovery=recovery, state=state, models=models,
                              transport=transport, store=store)
    finally:
        scheduler.stop()
        store.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(2)


def observe(system, rounds):
    results = []
    for _ in range(rounds):
        system.scheduler.sample_once()
        results.append(system.recovery.run_once())
    return results


def recover(system):
    """Cross the grace period at the 15 s cadence, then let the sampler confirm."""
    statuses = observe(system, 5)  # 0, 15, 30, 45, 60 s: the fifth round is past grace.
    assert [r["status"] for r in statuses[:4]] == ["observing"] * 4, statuses
    return statuses[4]


def arm(system):
    """Reach the grace period manually, then let the running sampler confirm effects."""
    observe(system, 4)
    system.scheduler.sample_once()  # Fifth round: 60 s after the first mismatch.
    system.state["step"] = 0.01
    system.scheduler.start()  # Sampler only; confirmation needs fresh rounds.
    return system.recovery.run_once()


def test_incident_replay_stops_releases_and_unloads_despite_in_flight(system, caplog):
    caplog.set_level(logging.INFO)
    result = arm(system)
    assert result["status"] == "complete", result
    assert result["stop_confirmed"] and result["proxy_unloaded"] and result["in_flight"] == 5
    assert system.state["calls"] == [("stop", NAME), ("unload", NAME)]
    assert system.store.lease("lease-q")[0].status == "released"
    kinds = [event.kind for event in system.scheduler.events_since(0)]
    assert kinds.index("wake_failure_detected") < kinds.index("lease_released") < kinds.index("wake_failure_result")
    dispatch = [json.loads(r.message) for r in caplog.records if r.name == "llmsvc.actions"]
    assert dispatch[-1]["kind"] == "action_dispatch" and dispatch[-1]["action"]["reason"] == "wake_failed"
    assert NAME not in system.scheduler.model_actions.pending


def test_ordinary_stop_is_still_blocked_by_in_flight(system):
    system.scheduler.sample_once()
    result = system.scheduler.model_actions.stop_model(NAME, by="test")
    assert result["status"] == "blocked" and result["error"] == "in_flight"
    assert system.state["calls"] == []


def test_healthy_sleeper_with_stopped_data_plane_is_left_alone(system):
    system.models[NAME] = replace(system.models[NAME], swap_state="stopped")
    system.state["inflight"] = 0
    assert {r["status"] for r in observe(system, 10)} == {"observing"}
    assert system.state["calls"] == []


@pytest.mark.parametrize("protection", ["pin", "default"])
def test_pinned_or_default_wedge_is_reported_never_touched(system, protection):
    if protection == "pin":
        system.store.put_pin(Pin(NAME, system.state["now"] + 86400, "owner"))
        reason = "pinned_until"
    else:
        system.transport.models[NAME]["is_default"] = True
        reason = "default_model"
    results = observe(system, 8)
    assert results[4]["status"] == "blocked" and results[4]["blocked_by"][0]["reason"] == reason
    assert system.state["calls"] == []
    blocked = [e for e in system.scheduler.events_since(0) if e.kind == "wake_failure_blocked"]
    assert len(blocked) == 1 and blocked[0].detail["in_flight"] == 5  # Reported once per window.
    assert system.store.lease("lease-q")[0].status == "confirmed"


def test_unconfirmed_identity_blocks_before_any_stop(system):
    system.recovery.accounting.probe = lambda name, *, deadline: UnitObservation(True, False, True, "other")
    result = recover(system)
    assert result["status"] == "blocked" and result["error"] == "unit_identity_unconfirmed"
    assert system.state["calls"] == []
    # The window restarts: no immediate retry on the next round.
    assert observe(system, 1)[0]["status"] == "observing"


def test_rejected_unload_is_partial_and_keeps_released_account(system):
    system.state["unload_status"] = 500
    result = arm(system)
    assert result["status"] == "partial" and result["error"] == "proxy_unload_rejected"
    assert result["stop_confirmed"] and not result["proxy_unloaded"]
    assert system.store.lease("lease-q")[0].status == "released"


def test_dry_run_previews_without_events_probes_or_transport(system, caplog):
    caplog.set_level(logging.INFO)
    system.scheduler.config = replace(system.scheduler.config, read_only=True)
    results = observe(system, 6)
    assert results[4]["status"] == "preview" and results[4]["would"][0]["reason"] == "wake_failed"
    assert system.state["calls"] == []
    assert not [e for e in system.scheduler.events_since(0) if e.kind.startswith("wake_failure")]
    previews = [json.loads(r.message) for r in caplog.records if r.name == "llmsvc.wake_failure"]
    assert previews and all(p["kind"] == "wake_failure_preview" and p["dry_run"] for p in previews)
    assert system.store.lease("lease-q")[0].status == "confirmed"


def test_worker_thread_runs_only_when_configured(system):
    system.scheduler.wake_failures = system.recovery
    system.state["step"] = 0.01
    system.scheduler.config = replace(system.scheduler.config, read_only=True, sample_interval_seconds=0.05)
    system.scheduler.start()
    assert system.scheduler._wake_failure_thread is not None and system.scheduler._wake_failure_thread.is_alive()
    system.scheduler.stop()
    assert not system.scheduler._wake_failure_thread.is_alive()
    assert system.state["calls"] == []


def test_config_flag_is_off_by_default_and_grace_is_bounded():
    config = SchedulerConfig("127.0.0.1", 8011)
    assert config.wake_failure_recovery_enabled is False and config.wake_failure_grace_seconds == 90
    for value in (10, 7200, float("inf"), True):
        with pytest.raises(ValueError):
            SchedulerConfig("127.0.0.1", 8011, wake_failure_grace_seconds=value)
