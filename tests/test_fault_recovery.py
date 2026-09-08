# Generated-By: Codex / gpt-6-astra
"""Default-off proof→stop→exit/account→proxy integration, no live model process."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import ActionDispatchError, ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.faults import FaultRecoveryController
from llmsvc.leases import LeaseError, PlacementController, UnitObservation
from llmsvc.scheduler import Scheduler
from llmsvc.state import Activity, FaultClaim, GPUState, Lease, MemoryState, ModelState, Pin, StateSnapshot
from llmsvc.store import IntentStore


@pytest.fixture
def system(tmp_path):
    state = {"now": 10000., "calls": [], "timeline": [], "stop": "apply", "proxy": "apply",
             "generation_hook": None, "probe_hook": None, "after_stop": None, "after_proxy": None,
             "inflight": 1, "proxy_code": 200}
    unit = "vllm-model.service"
    state["model"] = ModelState("model", state="awake", gpu=0, unit=unit, unit_active=True,
        health_ok=True, is_sleeping=False, swap_state="ready", budget_gb=80, weights_gb=40, is_default=True)
    state["identity"] = UnitObservation(True, False, True, "original", "a"*32)
    other = ModelState("other", state="awake", gpu=1, unit="vllm-other.service", unit_active=True,
                       health_ok=True, is_sleeping=False, budget_gb=80, weights_gb=40)
    config = SchedulerConfig("127.0.0.1", 19001, read_only=False, placement_enabled=True,
        model_actions_enabled=True, fault_recovery_enabled=True, fault_timeout_seconds=2,
        request_timeout_seconds=1, lease_probe_seconds=1, action_poll_seconds=.005,
        placement_wait_seconds=2, state_db_path=str(tmp_path / "state.sqlite"))
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    store.create_lease(Lease("original", "model", 0, .4, 20000, 80), unit)
    store.transition_lease("original", "confirmed")
    store.put_pin(Pin("model", 20000, "pin-owner"))
    def collect():
        if state["generation_hook"]:
            state["generation_hook"]()
        state["now"] += .1
        return StateSnapshot(sampled_at=state["now"], models=(state["model"], other),
            gpus=(GPUState(0, total_gb=200, free_gb=120, external_gb=0),
                  GPUState(1, total_gb=200, free_gb=120, external_gb=0)),
            memory=MemoryState(500, 0),
            activity=(Activity("model", state["now"]-1000, 0, 0, state["inflight"], ("caller",)),
                      Activity("other", state["now"]-1000, 0, 0, 0)))
    scheduler = Scheduler(config, collect, store=store, clock=lambda: state["now"], monotonic=lambda: state["now"])
    emit = scheduler.emit
    def record(kind, **kwargs):
        state["timeline"].append(kind)
        return emit(kind, **kwargs)
    scheduler.emit = record
    def stop(argv, **kwargs):
        assert argv == ["fake-systemctl", "stop", unit] and kwargs["timeout"] > 0
        assert scheduler.store.fault("model").stage == "claimed"
        assert scheduler.store.lease("original")[0].status == "confirmed"
        state["calls"].append("stop")
        state["timeline"].append("stop")
        if state["stop"] == "apply":
            state["model"] = replace(state["model"], state="stopped", unit_active=False, health_ok=None,
                                     is_sleeping=None, gpu=None)
            state["identity"] = UnitObservation(False, True)
        if state["after_stop"]:
            state["after_stop"]()
        return SimpleNamespace(returncode=1 if state["stop"] == "reject" else 0)
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            assert self.path == "/api/models/unload/model"
            # The external proxy must not re-enter the scheduler's action
            # lock. A separate read-only connection proves release/fence were
            # committed durably before this HTTP request reached the proxy.
            with sqlite3.connect("file:"+config.state_db_path+"?mode=ro", uri=True) as observed_db:
                assert observed_db.execute("SELECT stage FROM llmsvc_faults WHERE lease_id='original'").fetchone()[0] == "released"
                assert observed_db.execute("SELECT status FROM llmsvc_leases WHERE lease_id='original'").fetchone()[0] == "released"
            state["calls"].append("proxy")
            state["timeline"].append("proxy")
            if state["proxy"] == "apply":
                state["model"] = replace(state["model"], swap_state="stopped")
            if state["after_proxy"]:
                state["after_proxy"]()
            self.send_response(state["proxy_code"])
            self.send_header("Content-Length", "0")
            self.end_headers()
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    proxy_thread = threading.Thread(target=lambda: proxy.serve_forever(poll_interval=.01))
    proxy_thread.start()
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:"+str(proxy.server_port),
        models={"model": {"unit": unit, "util": .4, "weights_gb": 40, "is_default": True}},
        systemctl="fake-systemctl", run=stop, monotonic=lambda: state["now"])
    scheduler.model_actions = ModelActionController(scheduler, transport, monotonic=lambda: state["now"])
    def probe(name, *, deadline):
        assert name == "model" and deadline > state["now"]
        if state["probe_hook"]:
            state["probe_hook"]()
        return state["identity"]
    scheduler.placement = PlacementController(scheduler, transport, probe=probe, monotonic=lambda: state["now"])
    recovery = FaultRecoveryController(scheduler, probe=probe, monotonic=lambda: state["now"])
    scheduler.sample_once()
    scheduler.start()  # Existing sampler only; manual replay controls fault ticks.
    scheduler.faults = recovery
    try:
        yield scheduler, recovery, state, transport, probe
    finally:
        scheduler.stop()
        scheduler.store.close()
        proxy.shutdown(); proxy.server_close(); proxy_thread.join(3)


def tick(system, seconds=1):
    _, recovery, state, _, _ = system
    state["now"] += seconds
    return recovery.run_once()


def arm(system):
    assert tick(system)["status"] == "observing"


def fail_health(system):
    _, _, state, _, _ = system
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    assert tick(system)["status"] == "observing"
    return tick(system)


def test_health_fault_uses_dedicated_path_and_pin_survives_normal_replacement(system):
    scheduler, _, state, _, _ = system
    arm(system)
    result = fail_health(system)
    assert result["status"] == "complete" and result["account_released"] and result["proxy_unloaded"], result
    assert state["calls"] == ["stop", "proxy"]
    timeline = state["timeline"]
    assert timeline.index("fault_detected") < timeline.index("stop") < timeline.index("fault_account_released") < timeline.index("proxy")
    assert scheduler.store.active(state["now"])[0] == (Pin("model", 20000, "pin-owner"),)
    assert not scheduler.store.faults()
    result = scheduler.placement.place({"model": "model", "util": .4})
    assert result["gpu"] == 0 and result["lease_id"] != "original"
    assert scheduler.store.lease(result["lease_id"])[0].budget_gb == 80
    state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                             is_sleeping=False, swap_state="ready", gpu=0)
    state["identity"] = UnitObservation(True, False, True, result["lease_id"], "b"*32)
    confirmed = scheduler.placement.finish("confirm", result["lease_id"])
    assert confirmed["status"] == "confirmed"
    assert scheduler.snapshot().pins[0].by == "pin-owner"
    assert scheduler.model_actions.free({"gpu": 0}, by="caller")["status"] == "blocked"
    assert state["calls"] == ["stop", "proxy"]


def test_unexpected_absence_reconciles_without_replaying_stop(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="stopped", unit_active=False, gpu=None, health_ok=None)
    state["identity"] = UnitObservation(False, True)
    result = tick(system)
    assert result["status"] == "complete" and result["reason"] == "unexpected_unit_exit", result
    assert state["calls"] == ["proxy"] and scheduler.store.lease("original")[0].status == "released"


def test_ten_second_discrepancy_is_replayed_without_real_wait(system):
    _, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="sleeping", is_sleeping=True)
    results = [tick(system) for _ in range(11)]
    assert all(result["status"] == "observing" for result in results[:-1]), results
    assert results[-1]["status"] == "complete" and results[-1]["reason"] == "ready_still_sleeping", results
    assert state["calls"] == ["stop", "proxy"]


@pytest.mark.parametrize("signal", ["unknown-health", "changed-incarnation", "gap", "expected-stop", "pending-startup"])
def test_transient_unknown_or_other_lifecycle_never_becomes_fault(system, signal):
    scheduler, recovery, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    if signal == "unknown-health":
        state["model"] = replace(state["model"], health_ok=None)
    elif signal == "changed-incarnation":
        state["identity"] = replace(state["identity"], invocation_id="b"*32)
    elif signal == "gap":
        state["now"] += 15
    elif signal == "expected-stop":
        from llmsvc.state import Action
        recovery.note_expected(Action("stop", "model", "ordinary", 0))
    else:
        scheduler.store.transition_lease("original", "stale")
    for _ in range(4):
        assert tick(system)["status"] == "observing"
    assert not state["calls"] and not scheduler.store.faults()


@pytest.mark.parametrize("mode", ["reject", "no-effect"])
def test_failed_or_unobserved_stop_retains_account_and_no_proxy(system, mode):
    scheduler, _, state, _, _ = system
    arm(system)
    state["stop"] = mode
    result = fail_health(system)
    assert result["status"] == "blocked" and not result["account_released"], result
    assert state["calls"] == ["stop"] and scheduler.store.lease("original")[0].status == "confirmed"
    assert scheduler.store.fault("model").stage == "claimed"
    assert scheduler.snapshot().pins[0].model == "model"


def test_http200_without_observed_proxy_stop_retains_released_fence(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["proxy"] = "no-effect"
    result = fail_health(system)
    assert result["status"] == "partial" and result["account_released"] and not result["proxy_unloaded"], result
    assert scheduler.store.fault("model").stage == "released"
    preview = scheduler.placement.preview("place", {"model": "model", "util": .4})
    assert not preview["would"] and preview["blocked_by"][0]["reason"] == "fault_recovery_pending"
    with pytest.raises(LeaseError, match="fault_recovery_pending"):
        scheduler.placement.finish("release", "original")


def test_released_fence_survives_restart_disable_and_then_finishes_without_stop(system):
    scheduler, _, state, _, probe = system
    arm(system)
    state["proxy"] = "no-effect"
    assert fail_health(system)["status"] == "partial"
    with scheduler.action_lock:
        old = scheduler.store
        scheduler.store = IntentStore(scheduler.config.state_db_path, action_lock=scheduler.action_lock)
        old.close()
        scheduler.config = replace(scheduler.config, fault_recovery_enabled=False)
        resumed = FaultRecoveryController(scheduler, probe=probe, monotonic=lambda: state["now"])
        scheduler.faults = resumed
    assert resumed.run_once()["status"] == "disabled"
    with pytest.raises(ActionDispatchError, match="fault_recovery_pending"):
        scheduler.model_actions.wake_model(scheduler.snapshot(), "model")
    with pytest.raises(ValueError, match="fault recovery pending"):
        scheduler.store.create_lease(Lease("new", "model", 0, .4, 20000, 80), "vllm-model.service")
    scheduler.config = replace(scheduler.config, fault_recovery_enabled=True)
    # The original acknowledged request finishes later. Resume observation,
    # never send another unload that could outlive this model incarnation.
    state["model"] = replace(state["model"], swap_state="stopped")
    result = tick((scheduler, resumed, state, None, probe))
    assert result["status"] == "complete" and result["proxy_unloaded"], result
    assert state["calls"] == ["stop", "proxy"]
    assert not scheduler.store.faults() and scheduler.snapshot().pins[0].by == "pin-owner"


def test_restart_claim_never_replays_stale_stop_on_active_unit(system):
    scheduler, recovery, state, _, probe = system
    claim = FaultClaim("original", "model", "vllm-model.service", "a"*32, 0, "unexpected_unit_exit", state["now"],
                       proxy_origin_hash=recovery._origin_hash())
    scheduler.store.claim_fault(claim)
    resumed = FaultRecoveryController(scheduler, probe=probe, monotonic=lambda: state["now"])
    scheduler.faults = resumed
    result = tick((scheduler, resumed, state, None, probe))
    assert result["status"] == "blocked" and state["calls"] == []
    assert scheduler.store.lease("original")[0].status == "confirmed"


def test_new_incarnation_after_proxy_submission_never_clears_old_claim_or_retries_target(system):
    scheduler, _, state, _, _ = system
    arm(system)
    def replace_unit():
        state["identity"] = UnitObservation(True, False, True, "other-lease", "b"*32)
        state["model"] = replace(state["model"], state="awake", unit_active=True, gpu=0,
                                 health_ok=True, is_sleeping=False, swap_state="ready")
    state["after_proxy"] = replace_unit
    result = fail_health(system)
    assert result["status"] == "partial" and not result["proxy_unloaded"], result
    assert scheduler.store.fault("model") is not None
    before = list(state["calls"])
    assert tick(system)["status"] == "partial"
    assert state["calls"] == before


def test_failed_unit_with_residual_resources_stops_before_releasing_account(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", unit_active=False, health_ok=None)
    state["identity"] = replace(state["identity"], active=False, inactive=True, exited=False)
    result = tick(system)
    assert result["status"] == "complete" and state["calls"] == ["stop", "proxy"], result
    assert scheduler.store.lease("original")[0].status == "released"


def test_incarnation_change_during_the_collection_interval_invalidates_health_reply(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    assert tick(system)["status"] == "observing"
    calls = [0]
    def restart_between_probes():
        calls[0] += 1
        if calls[0] == 2:
            state["identity"] = replace(state["identity"], invocation_id="b"*32)
    state["probe_hook"] = restart_between_probes
    assert tick(system)["status"] == "observing"
    assert not state["calls"] and not scheduler.store.faults()


def test_final_identity_change_before_claim_prevents_every_fault_mutation(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    assert tick(system)["status"] == "observing"
    calls = [0]
    def restart_at_final_probe():
        calls[0] += 1
        if calls[0] == 3:
            state["identity"] = replace(state["identity"], invocation_id="b"*32)
    state["probe_hook"] = restart_at_final_probe
    result = tick(system)
    assert result["status"] == "blocked" and result["error"] == "fault_unit_identity_changed", result
    assert not state["calls"] and not scheduler.store.faults()


def test_probe_timeout_is_unknown_and_does_not_preserve_consecutive_votes(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    assert tick(system)["status"] == "observing"
    def unknown():
        raise TimeoutError("fixture probe timeout")
    state["probe_hook"] = unknown
    assert tick(system)["status"] == "observing"
    state["probe_hook"] = None
    for _ in range(3):
        assert tick(system)["status"] == "observing"
    assert not state["calls"] and not scheduler.store.faults()


def test_concurrent_recovery_calls_have_only_one_owner(system):
    _, recovery, state, _, _ = system
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(3)
    state["generation_hook"] = hold
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(recovery.run_once)
        try:
            assert entered.wait(2)
            assert recovery.run_once() == {"status": "busy"}
            assert not state["calls"]
        finally:
            release.set()
        assert pending.result(timeout=3)["status"] == "observing"
    assert not recovery.active


@pytest.mark.parametrize("flag,value", [("fault_recovery_enabled", False), ("model_actions_enabled", False), ("read_only", True)])
def test_default_safe_gates_and_dry_preview_have_zero_additional_effects(system, flag, value, monkeypatch):
    scheduler, recovery, state, transport, _ = system
    scheduler.stop()
    scheduler.config = replace(scheduler.config, **{flag: value})
    before = open(scheduler.config.state_db_path, "rb").read(), list(state["calls"]), scheduler.events_since(0)
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled or dry-run fault path performed I/O")
    monkeypatch.setattr(recovery, "probe", forbidden)
    monkeypatch.setattr(scheduler, "collect", forbidden)
    monkeypatch.setattr(transport, "http_request", forbidden)
    monkeypatch.setattr(transport, "stop_unit", forbidden)
    assert recovery.run_once() == {"status": "disabled"}
    preview = recovery.run_once(dry_run=True)
    assert not preview["would"] and preview["blocked_by"][0]["reason"] == "fault_evidence_unavailable"
    assert (open(scheduler.config.state_db_path, "rb").read(), state["calls"], scheduler.events_since(0)) == before


def test_healthy_pin_default_inflight_still_block_ordinary_dispatch(system):
    scheduler, _, state, _, _ = system
    arm(system)
    result = scheduler.model_actions.free({"gpu": 0}, by="caller")
    assert result["status"] == "blocked" and state["calls"] == []
    assert not scheduler.store.faults() and scheduler.store.lease("original")[0].status == "confirmed"


def test_shutdown_after_stop_keeps_claim_and_unconfirmed_budget_and_never_unloads(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["after_stop"] = lambda: scheduler.stopping.set()
    result = fail_health(system)
    assert result["status"] == "blocked" and not result["account_released"], result
    assert state["calls"] == ["stop"] and scheduler.store.lease("original")[0].status == "confirmed"
    assert scheduler.store.fault("model").stage == "claimed"


def test_actual_worker_lifecycle_runs_proven_exit_and_joins_without_extra_cycles(system):
    scheduler, _, state, _, probe = system
    scheduler.stop()
    fresh = Scheduler(scheduler.config, scheduler.collect, store=scheduler.store, clock=lambda: state["now"], monotonic=lambda: state["now"])
    fresh.model_actions = ModelActionController(fresh, scheduler.model_actions.transport, monotonic=lambda: state["now"])
    fresh.placement = PlacementController(fresh, fresh.model_actions.transport, probe=probe, monotonic=lambda: state["now"])
    recovery = FaultRecoveryController(fresh, probe=probe, monotonic=lambda: state["now"])
    fresh.faults = recovery
    armed, done = threading.Event(), threading.Event()
    observe = recovery.detector.observe
    def observed(*args, **kwargs):
        result = observe(*args, **kwargs)
        record = recovery.detector.records.get("model")
        if record is not None and record.served:
            armed.set()
        return result
    recovery.detector.observe = observed
    emit = fresh.emit
    def finished(kind, **kwargs):
        event = emit(kind, **kwargs)
        if kind == "fault_result":
            done.set()
        return event
    fresh.emit = finished
    fresh.start()
    try:
        assert armed.wait(3)
        with fresh.changed:
            state["model"] = replace(state["model"], state="stopped", unit_active=False, gpu=None)
            state["identity"] = UnitObservation(False, True)
            state["now"] += 1
        fresh.request_sample()
        assert done.wait(3)
        assert not fresh.store.faults() and state["calls"] == ["proxy"]
    finally:
        fresh.stop()
    assert not fresh._fault_thread.is_alive() and not recovery.active


def test_changed_proxy_origin_never_receives_an_old_released_claim(system):
    scheduler, _, state, transport, _ = system
    arm(system)
    state["proxy"] = "no-effect"
    assert fail_health(system)["status"] == "partial"
    before = list(state["calls"])
    transport.swap_url = "http://127.0.0.1:1"
    result = tick(system)
    assert result["status"] == "partial" and result["error"] == "fault_claim_or_configuration_changed", result
    assert state["calls"] == before and scheduler.store.fault("model") is not None
    assert any(b.reason == "fault_recovery_pending" for b in scheduler.snapshot().blocked_by)


def test_aliased_configured_unit_does_not_establish_fault_eligibility(system):
    scheduler, _, state, transport, _ = system
    arm(system)
    transport.units["alias"] = transport.units["model"]
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    for _ in range(3):
        assert tick(system)["status"] == "observing"
    assert not state["calls"] and not scheduler.store.faults()


def test_fault_once_entrypoint_is_readonly_and_does_not_forge_a_sequence(system, monkeypatch, caplog):
    import logging
    import llmsvc.__main__ as entry
    scheduler, _, state, _, _ = system
    scheduler.stop()
    config = replace(scheduler.config, collectors={"swap_url": "http://127.0.0.1:1",
        "models": {"model": {"unit": "vllm-model.service"}}})
    monkeypatch.setattr(entry, "load_config", lambda path: config)
    monkeypatch.setattr(entry, "build_collector", lambda value: scheduler.collect)
    monkeypatch.setattr(entry, "build_event_relay", lambda value: None)
    original = entry.Scheduler
    monkeypatch.setattr(entry, "Scheduler", lambda *args, **kwargs: original(*args, **kwargs, clock=lambda: state["now"], monotonic=lambda: state["now"]))
    def forbidden(*args, **kwargs):
        pytest.fail("--once started a fault probe or action")
    monkeypatch.setattr(FaultRecoveryController, "_probe", forbidden)
    monkeypatch.setattr("sys.argv", ["llmsvc", "--config", "fixture", "--once"])
    before = open(config.state_db_path, "rb").read()
    with caplog.at_level(logging.INFO, logger="llmsvc.faults"):
        assert entry.main() == 0
    preview = next(json.loads(record.message) for record in caplog.records if '"kind": "fault_preview"' in record.message)
    assert preview["would"] == [] and preview["blocked_by"][0]["reason"] == "fault_evidence_unavailable"
    assert open(config.state_db_path, "rb").read() == before and not state["calls"]


@pytest.mark.parametrize("key,value", [("fault_recovery_enabled", "yes"), ("fault_interval_seconds", 0),
    ("fault_interval_seconds", 2), ("fault_timeout_seconds", 121), ("fault_timeout_seconds", float("nan")),
    ("fault_health_failures", 2), ("fault_health_failures", True), ("fault_health_failures", 101)])
def test_invalid_fault_configuration_is_rejected(key, value):
    with pytest.raises(ValueError):
        SchedulerConfig("127.0.0.1", 19001, **{key: value})


def test_pin_write_proceeds_during_fault_observation_wait_and_is_retained(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    assert tick(system)["status"] == "observing"
    entered, release = threading.Event(), threading.Event()
    def hold_after_stop():
        if state["calls"]:
            entered.set()
            assert release.wait(3)
    state["generation_hook"] = hold_after_stop
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(tick, system)
        try:
            assert entered.wait(2)
            saved = scheduler.write_pin("pin", {"model": "model", "until": 30000, "by": "self-label"},
                                        source_ip="127.0.0.1")
            assert saved["by"] == "ip:127.0.0.1"
        finally:
            release.set()
        result = pending.result(timeout=3)
    assert result["status"] == "complete", result
    assert scheduler.store.active(state["now"])[0] == (Pin("model", 30000, "ip:127.0.0.1"),)


def test_readonly_transition_after_submission_does_not_write_release_or_proxy(system):
    scheduler, _, state, _, _ = system
    arm(system)
    before = []
    def disable():
        scheduler.config = replace(scheduler.config, read_only=True)
        before.append(open(scheduler.config.state_db_path, "rb").read())
    state["after_stop"] = disable
    result = fail_health(system)
    assert result["status"] == "blocked" and not result["account_released"], result
    assert state["calls"] == ["stop"] and scheduler.store.lease("original")[0].status == "confirmed"
    assert open(scheduler.config.state_db_path, "rb").read() == before[0]


@pytest.mark.parametrize("active,pid,group,exited", [("active", "123", "/fixture", False),
    ("failed", "0", "/residual", False), ("failed", "0", "", True)])
def test_systemctl_identity_probe_distinguishes_failed_unit_and_actual_resource_exit(active, pid, group, exited):
    from llmsvc.leases import LeaseUnitProbe
    def show(argv, **kwargs):
        assert argv[:3] == ["fake-systemctl", "show", "vllm-model.service"]
        assert "InvocationID" in argv[-1] and kwargs["timeout"] > 0
        return SimpleNamespace(returncode=0, stdout="\n".join([
            "LoadState=loaded", "ActiveState="+active, "MainPID="+pid, "ControlGroup="+group,
            "Environment=LLMSVC_LEASE_ID=original", "InvocationID="+"a"*32]))
    transport = SimpleNamespace(systemctl="fake-systemctl", run=show,
                                unit_for_model=lambda name: "vllm-model.service")
    observation = LeaseUnitProbe(transport)("model", deadline=time.monotonic()+2)
    assert observation.invocation_id == "a"*32 and observation.lease_id == "original"
    assert observation.exited is exited and observation.inactive is (active == "failed")


def test_unacknowledged_proxy_request_is_never_retried_or_cleared_from_later_snapshots(system):
    scheduler, _, state, _, _ = system
    arm(system)
    state["proxy_code"] = 503
    result = fail_health(system)
    assert result["status"] == "partial" and result["account_released"] and not result["proxy_unloaded"], result
    claim = scheduler.store.fault("model")
    assert claim.proxy_submitted and not claim.proxy_acknowledged
    # The test proxy already reported stopped, but a failed request is not a
    # settled old-target operation. Do not authorize a fresh model or resend.
    assert state["model"].swap_state == "stopped"
    before = list(state["calls"])
    result = tick(system)
    assert result["status"] == "partial" and result["error"] == "fault_proxy_outcome_unknown"
    assert state["calls"] == before and scheduler.store.fault("model") is not None
    preview = scheduler.faults.run_once(dry_run=True)
    assert preview["would"] == [] and preview["blocked_by"][0]["reason"] == "fault_proxy_outcome_unknown"


def test_scheduler_receipt_fallback_cannot_forge_missing_source_time(system, monkeypatch):
    scheduler, _, state, _, _ = system
    arm(system)
    state["model"] = replace(state["model"], state="unknown", health_ok=False)
    assert tick(system)["status"] == "observing"
    assert tick(system)["status"] == "observing"
    collect = scheduler.collect
    scheduler.collect = lambda: replace(collect(), sampled_at=None)
    monkeypatch.setattr("llmsvc.scheduler.time.time", lambda: state["now"])
    result = tick(system)
    assert result == {"status": "blocked", "error": "fault_source_timestamp_unknown"}
    assert not scheduler.store.faults() and not state["calls"]
    scheduler.collect = collect
    assert tick(system)["status"] == "observing"
