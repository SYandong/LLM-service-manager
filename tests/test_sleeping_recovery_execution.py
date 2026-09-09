# Generated-By: Codex / gpt-6-astra
"""Actual ordinary stop/unload/launcher reentry using only temporary fixtures."""

import json
import sqlite3
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import AutomaticPolicyController, ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.recovery import SleepingRecoveryController
from llmsvc.server import SchedulerHTTPServer
from llmsvc.scheduler import Scheduler
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, Reserve
from llmsvc.store import IntentStore
from test_registry_http_preview import request as http_request


@pytest.fixture
def execution(tmp_path, request):
    options = getattr(request, "param", {})
    source_gpu = options.get("source_gpu", 0)
    destination_total = options.get("destination_total", 100)
    lock = threading.RLock()
    state = {"now":10000., "offset":0., "calls":[], "recent":3, "inflight":0, "available":500.,
             "source_gpu":source_gpu, "extra_models":(), "other_observations":{}, "source_time":True, "stop_mode":"exit", "ram_gain":40, "unload_status":200, "get_status":404, "confirm":True,
             "collect_hook":None, "probe_hook":None, "after_grant":None, "after_unload":None,
             "stop_seen":threading.Event(), "place_results":[], "probes":0, "held":False}
    model = ModelState("source", state="sleeping", gpu=source_gpu, unit="vllm-source.service", unit_active=True,
                       health_ok=True, is_sleeping=True, swap_state="stopped", util=options.get("observed_util", .8), budget_gb=80,
                       resident_gb=2, weights_gb=40, cold_start_seconds=2)
    state["model"] = model
    state["observation"] = UnitObservation(True, False, True, "source-lease", "1"*32)
    state["gpus"] = tuple(GPUState(index, total_gb=100 if index==source_gpu else destination_total,
        external_gb=30 if index==source_gpu else 0, free_gb=68 if index==source_gpu else destination_total) for index in (0,1))
    monotonic = lambda: time.monotonic()+state["offset"]
    core = None
    scheduler = None
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def do_POST(self):
            assert self.path == "/api/models/unload/source"
            with lock:
                state["calls"].append(("unload", "source"))
                if state["unload_status"] == 200:
                    state["model"] = replace(state["model"], swap_state="stopped")
                hook = state["after_unload"]
            if hook: hook()
            self.respond(state["unload_status"])
        def do_GET(self):
            assert self.path == "/upstream/source/"
            with lock:
                state["calls"].append(("wake", "source"))
            if state.get("before_place"): state["before_place"]()
            code, result = http_request(core.server_address, "POST", "/v1/place", {"model":"source", "util":.4})
            state["place_results"].append((code, result))
            if code != 200:
                self.respond(500)
                return
            lease_id, gpu = result["lease_id"], result["gpu"]
            if state["after_grant"]: state["after_grant"](lease_id)
            with lock:
                state["calls"].append(("launch", gpu, lease_id))
                state["observation"] = UnitObservation(True, False, True, lease_id, "2"*32)
                state["model"] = replace(state["model"], state="awake", gpu=gpu, unit_active=True,
                    health_ok=True, is_sleeping=False, swap_state="ready", util=.4, budget_gb=40, resident_gb=40)
                state["available"] -= 40
            if state["confirm"]:
                status, confirmed = http_request(core.server_address, "POST", "/v1/place/"+lease_id+"/confirm")
                state["confirmation"] = status, confirmed
            self.respond(state["get_status"])
    upstream = ThreadingHTTPServer(("127.0.0.1",0), Upstream)
    upstream_thread = threading.Thread(target=lambda: upstream.serve_forever(poll_interval=.01))
    upstream_thread.start()
    cfg = SchedulerConfig("127.0.0.1",8011,read_only=False,model_actions_enabled=True,placement_enabled=True,
        automation_enabled=True,sleeping_recovery_enabled=True,state_db_path=str(tmp_path/"state.sqlite"),
        sleeping_recovery_timeout_seconds=5,wake_timeout_seconds=5,automation_cycle_timeout_seconds=2,
        request_timeout_seconds=1,placement_wait_seconds=2,lease_probe_seconds=.3,
        action_observe_seconds=2,action_poll_seconds=.01,sample_interval_seconds=.02)
    store = IntentStore(cfg.state_db_path, action_lock=threading.RLock())
    store.create_lease(Lease("source-lease","source",source_gpu,.8,20000,80),model.unit)
    store.transition_lease("source-lease","confirmed")
    def collect():
        hook = state["collect_hook"]
        if hook: hook()
        with lock:
            state["now"] += .01
            current = state["model"]
            from llmsvc.state import StateSnapshot
            return StateSnapshot(sampled_at=state["now"] if state["source_time"] else None, models=(current,)+state["extra_models"],gpus=state["gpus"],
                memory=MemoryState(state["available"],40 if current.state=="sleeping" else 0),
                activity=(Activity("source",state["now"]-1000,state["recent"],0,state["inflight"]),))
    scheduler = Scheduler(cfg,collect,store=store,clock=lambda:state["now"])
    def run(argv, **kwargs):
        assert argv == ["fake-systemctl", "stop", "vllm-source.service"] and kwargs["timeout"] > 0
        with lock:
            state["calls"].append(("stop","source"))
            if state["stop_mode"] in ("exit", "error-after-exit"):
                state["observation"] = UnitObservation(False, True)
                state["model"] = replace(state["model"], state="stopped",unit_active=False,resident_gb=0)
                state["available"] += state["ram_gain"]
                # Pressure may clear after source exit. The private reentry
                # exclusion must still prevent selecting the former source0.
                state["gpus"] = tuple(replace(g,external_gb=0,free_gb=g.total_gb) if g.index==source_gpu else g for g in state["gpus"])
        state["stop_seen"].set()
        return SimpleNamespace(returncode=1 if state["stop_mode"].startswith("error") else 0)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:"+str(upstream.server_port),
        models={"source":{"unit":model.unit,"util":.4,"weights_gb":40}},systemctl="fake-systemctl",run=run,
        monotonic=monotonic)
    scheduler.model_actions = ModelActionController(scheduler,transport,monotonic=monotonic)
    def probe(name, *, deadline):
        if name != "source":
            return state["other_observations"].get(name, UnitObservation(False, True))
        with lock:
            state["probes"] += 1
            count = state["probes"]
        if state["probe_hook"]: state["probe_hook"](count)
        with lock: return state["observation"]
    scheduler.placement = PlacementController(scheduler,transport,probe=probe,monotonic=monotonic)
    recovery = SleepingRecoveryController(scheduler,monotonic=monotonic)
    scheduler.sleeping_recovery = recovery
    if getattr(request, "param", {}).get("automated"):
        scheduler.automation = AutomaticPolicyController(scheduler, accounting=scheduler.placement, monotonic=monotonic)
    state["result_seen"] = threading.Event()
    original_emit = scheduler.emit
    def emit(kind, **kwargs):
        event = original_emit(kind, **kwargs)
        if kind == "sleeping_recovery_result":
            state["last_result"] = event.detail
            state["result_seen"].set()
        return event
    scheduler.emit = emit
    scheduler.sample_once()
    core = SchedulerHTTPServer(("127.0.0.1",0),scheduler)
    core_thread = threading.Thread(target=lambda: core.serve_forever(poll_interval=.01))
    core_thread.start()
    # Start only the existing sampler here; explicit tests own recovery calls.
    scheduler.start()
    try:
        yield SimpleNamespace(scheduler=scheduler,controller=recovery,state=state,lock=lock,store=store,
            address=core.server_address,transport=transport,monotonic=monotonic,probe=probe)
    finally:
        state["collect_hook"] = None
        scheduler.stop()
        core.shutdown();core.server_close();core_thread.join(3)
        upstream.shutdown();upstream.server_close();upstream_thread.join(3)
        store.close()


def test_recent_sleeper_stops_reconciles_and_reenters_real_place_confirm_before_ready(execution):
    e=execution
    result=e.controller.run_once()
    assert result["status"] == "relocated" and result["ready"] is True, result
    assert result["source_gpu"] == 0 and result["destination_gpu"] == 1 and result["source_released"] is True
    assert e.state["calls"][:3] == [("stop","source"),("unload","source"),("wake","source")]
    assert e.state["confirmation"][0] == 200 and e.state["place_results"][0][0] == 200
    lease=e.store.lease(result["lease_id"])[0]
    assert lease.status == "confirmed" and lease.gpu == 1 and lease.budget_gb == 80 and lease.util == .4
    assert e.store.lease("source-lease")[0].status == "released"
    assert len(e.store.leases()) == 1 and e.store.recoveries() == ()
    assert e.store.active(e.state["now"])[1] == ()  # No synthetic source reserve persisted.


def test_unused_sleeper_retires_without_cold_start(execution):
    e=execution;e.state["recent"]=0
    result=e.controller.run_once()
    assert result["status"] == "retired" and result["ready"] is False, result
    assert e.state["calls"] == [("stop","source"),("unload","source")]
    assert result["source_released"] is True and e.store.leases() == () and e.store.recoveries() == ()


@pytest.mark.parametrize("option", ["sleeping_recovery_enabled", "automation_enabled", "model_actions_enabled", "read_only"])
def test_existing_optins_do_not_implicitly_enable_recovery(execution, option):
    e=execution
    e.scheduler.config=replace(e.scheduler.config, **{option: option=="read_only"})
    before=e.store.leases()
    assert e.controller.run_once()["status"] == "disabled"
    assert e.state["calls"] == [] and e.store.recoveries() == () and e.store.leases() == before
    assert e.store._db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_dryrun_is_only_existing_policy_with_no_sampling_ids_schema_or_effects(execution, monkeypatch):
    e=execution;e.scheduler.stop()
    before=e.store.leases(),e.scheduler.events_since(0)
    def forbidden(*args, **kwargs): raise AssertionError("dryrun performed work")
    monkeypatch.setattr(e.scheduler, "sample_once", forbidden)
    monkeypatch.setattr(e.scheduler, "request_sample", forbidden)
    monkeypatch.setattr(e.controller.accounting, "_inspect", forbidden)
    monkeypatch.setattr("llmsvc.recovery.uuid.uuid4", forbidden)
    result=e.controller.run_once(dry_run=True)
    assert [a["kind"] for a in result["would"]] == ["stop","place"]
    assert e.state["calls"] == [] and e.store.recoveries() == ()
    assert e.store._db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert (e.store.leases(),e.scheduler.events_since(0)) == before


@pytest.mark.parametrize("protection", ["pin","default","inflight","unknown","reserve","unleased"])
def test_protection_or_impossible_destination_never_stops_source(execution, protection):
    e=execution
    with e.lock:
        if protection=="pin": e.store.put_pin(Pin("source",20000,"owner"))
        elif protection=="default": e.transport.models["source"]["is_default"]=True
        elif protection=="inflight": e.state["inflight"]=1
        elif protection=="unknown": e.state["model"]=replace(e.state["model"],state="unknown",health_ok=None)
        elif protection=="reserve": e.store.put_reserve(Reserve("dest",1,1,20000,"owner"))
        else: e.store.transition_lease("source-lease","released")
    before=e.store.leases()
    result=e.controller.run_once()
    assert result["ready"] is False and result["status"] == "blocked", result
    assert e.state["calls"] == [] and e.store.recoveries() == () and e.store.leases() == before
    assert e.store._db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_new_pin_after_claim_is_rechecked_before_stop_submission(execution, monkeypatch):
    e=execution
    original=e.store.claim_recovery
    def claim(value, **kwargs):
        result=original(value, **kwargs)
        e.store.put_pin(Pin("source",20000,"new-owner"))
        return result
    monkeypatch.setattr(e.store, "claim_recovery", claim)
    result=e.controller.run_once()
    assert result["ready"] is False and e.state["calls"] == []
    assert e.store.recoveries() == () and e.store.lease("source-lease")[0].status == "confirmed"
    assert e.store.active(e.state["now"])[0][0].by == "new-owner"


def test_changed_source_invocation_after_claim_cannot_stop_replacement(execution, monkeypatch):
    e=execution
    original=e.store.claim_recovery
    def claim(value, **kwargs):
        result=original(value, **kwargs)
        with e.lock: e.state["observation"]=replace(e.state["observation"],invocation_id="3"*32)
        return result
    monkeypatch.setattr(e.store,"claim_recovery",claim)
    result=e.controller.run_once()
    assert result["ready"] is False and e.state["calls"] == []
    assert e.store.lease("source-lease")[0].status == "confirmed" and e.store.recoveries() == ()


def test_stop_error_after_proven_exit_is_partial_and_does_not_continue(execution):
    e=execution;e.state["stop_mode"]="error-after-exit"
    result=e.controller.run_once()
    assert result["status"] == "partial" and result["source_released"] is True and result["ready"] is False, result
    assert e.state["calls"] == [("stop","source")]
    claim=e.store.recovery("source")
    assert claim.stage == "released" and claim.stop_submitted and not claim.stop_acknowledged
    assert e.store.leases() == ()
    e.controller.run_once()
    assert e.state["calls"] == [("stop","source")]


def test_no_observed_ram_gain_never_uses_hypothetical_ram_to_cold_start(execution):
    e=execution;e.state.update(available=180,ram_gain=0)
    result=e.controller.run_once()
    assert result["status"] == "partial" and result["ready"] is False and result["error"] == "memory_budget", result
    assert e.state["calls"] == [("stop","source"),("unload","source")]
    assert e.store.leases() == () and e.store.recovery("source").stage == "settled"


@pytest.mark.parametrize("mode", ["rejected", "late", "origin_changed"])
def test_ambiguous_proxy_cleanup_is_not_resent_or_cleared_after_restart(execution, mode):
    e=execution
    if mode=="rejected": e.state["unload_status"]=500
    elif mode=="late": e.state["after_unload"]=lambda: e.state.update(offset=10.)
    else: e.state["after_unload"]=lambda: setattr(e.transport,"swap_url","http://127.0.0.1:1")
    result=e.controller.run_once()
    assert result["status"] == "partial" and result["ready"] is False
    claim=e.store.recovery("source")
    assert claim.proxy_submitted and not claim.proxy_acknowledged and claim.stage == "released"
    before=list(e.state["calls"])
    e.controller.run_once()
    restarted=SleepingRecoveryController(e.scheduler,monotonic=e.monotonic)
    e.scheduler.sleeping_recovery=restarted
    assert restarted.run_once()["error"] == ("recovery_profile_changed" if mode=="origin_changed" else "recovery_requires_settlement")
    assert e.state["calls"] == before and e.store.recovery("source") == claim
    assert not any(item[0]=="wake" for item in before)


def test_destination_identity_change_after_acknowledgment_cannot_complete(execution, monkeypatch):
    e=execution;original=e.store.advance_recovery
    def advance(claim, **kwargs):
        result=original(claim, **kwargs)
        if kwargs.get("wake_acknowledged"):
            with e.lock: e.state["observation"]=replace(e.state["observation"],invocation_id="3"*32)
        return result
    monkeypatch.setattr(e.store,"advance_recovery",advance)
    result=e.controller.run_once()
    assert result["status"] == "partial" and result["ready"] is False, result
    claim=e.store.recovery("source")
    assert claim.stage == "destination" and claim.destination_invocation_id == "2"*32
    assert e.store.lease(claim.destination_lease_id)[0].status == "confirmed"


def test_destination_binding_failure_rolls_back_lease_and_keeps_fence(execution):
    e=execution
    original=e.store.claim_recovery
    def claim(value, **kwargs):
        result=original(value, **kwargs)
        e.store._db.execute("CREATE TRIGGER fail_destination BEFORE UPDATE ON llmsvc_recoveries WHEN NEW.stage='destination' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        e.store._db.commit()
        return result
    e.store.claim_recovery=claim
    result=e.controller.run_once()
    assert result["status"] == "partial" and result["ready"] is False
    assert e.state["place_results"][0][0] == 503
    assert e.store.leases() == () and e.store.recovery("source").destination_lease_id is None
    assert not any(item[0]=="launch" for item in e.state["calls"])


def test_same_model_concurrent_cycle_does_not_reset_deadline_or_duplicate_stop(execution):
    from concurrent.futures import ThreadPoolExecutor
    e=execution;entered=threading.Event();release=threading.Event()
    def collect():
        if e.state["stop_seen"].is_set():
            entered.set()
            assert release.wait(3)
    e.state["collect_hook"]=collect
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(e.controller.run_once)
        try:
            assert entered.wait(3)
            deadline=e.controller.deadline
            result=e.controller.run_once()
            assert result["error"] == "cycle_in_progress" and e.controller.active
            assert e.controller.deadline == deadline
        finally:
            release.set()
        assert pending.result(timeout=4)["status"] == "relocated"
    assert [call[0] for call in e.state["calls"]].count("stop") == 1


def test_pin_http_progresses_during_source_observation_and_blocks_later_actions(execution):
    from concurrent.futures import ThreadPoolExecutor
    e=execution;entered=threading.Event();release=threading.Event()
    def collect():
        if e.state["stop_seen"].is_set():
            entered.set()
            assert release.wait(3)
    e.state["collect_hook"]=collect
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(e.controller.run_once)
        try:
            assert entered.wait(3)
            status,_=http_request(e.address,"POST","/v1/pin",{"model":"source","until":20000,"by":"fixture"})
            assert status == 200
        finally: release.set()
        result=pending.result(timeout=4)
    assert result["status"] == "partial" and result["error"] == "pinned"
    assert e.state["calls"] == [("stop","source")]
    assert e.store.active(e.state["now"])[0][0].model == "source"


@pytest.mark.parametrize("execution", [{"automated":True}], indirect=True)
def test_existing_automation_worker_mounts_recovery_and_stops_cleanly(execution):
    e=execution
    assert e.state["result_seen"].wait(8)
    assert e.state["last_result"]["status"] == "relocated", e.state["last_result"]
    e.scheduler.stop()
    before=list(e.state["calls"])
    assert e.scheduler._automation_thread is not None and not e.scheduler._automation_thread.is_alive()
    assert not e.scheduler._thread.is_alive()
    assert e.state["calls"] == before and e.store.recoveries() == ()


def test_cold_ram_preflight_impossible_even_after_source_release_has_zero_effects(execution):
    e=execution;e.state["available"]=100
    result=e.controller.run_once()
    assert result["ready"] is False
    assert e.state["calls"] == [], result
    assert e.store.lease("source-lease")[0].status == "confirmed" and e.store.recoveries() == ()


@pytest.mark.parametrize("execution", [{"source_gpu":1,"destination_total":200,"observed_util":.4}], indirect=True)
def test_destination_profile_floor_is_preflighted_without_inflating_source_accounting(execution):
    e=execution
    with e.lock:
        e.state["extra_models"]=(ModelState("protected",state="awake",gpu=0,unit="vllm-protected.service",
            unit_active=True,health_ok=True,is_sleeping=False,is_default=True,budget_gb=100,weights_gb=40),)
        e.state["gpus"]=tuple(replace(g,free_gb=100) if g.index==0 else g for g in e.state["gpus"])
    result=e.controller.run_once()
    assert result["ready"] is False
    assert e.state["calls"] == [], result
    assert e.store.lease("source-lease")[0].status == "confirmed" and e.store.recoveries() == ()


def test_higher_future_profile_does_not_manufacture_current_wake_pressure(execution):
    e=execution
    with e.lock:
        e.transport.models["source"]["util"]=1.0
        e.state["gpus"]=tuple(replace(g,external_gb=10,free_gb=88) if g.index==0 else g for g in e.state["gpus"])
    result=e.controller.run_once()
    assert result["status"] == "idle" and e.state["calls"] == []
    assert e.store.recoveries() == () and e.store.lease("source-lease")[0].budget_gb == 80


@pytest.mark.parametrize("execution", [{"source_gpu":1,"destination_total":200,"observed_util":.4}], indirect=True)
def test_real_reentry_charges_full_profile_floor_on_larger_destination(execution):
    e=execution
    result=e.controller.run_once()
    assert result["status"] == "relocated" and result["destination_gpu"] == 0, result
    lease=e.store.lease(result["lease_id"])[0]
    assert lease.budget_gb == 160 and lease.util == .4 and lease.status == "confirmed"
    assert len(e.store.leases()) == 1


def reopen_recovery(e):
    with e.scheduler.action_lock:
        old = e.store
        old.close()
        e.store = IntentStore(e.scheduler.config.state_db_path, action_lock=e.scheduler.action_lock)
        e.scheduler.store = e.store
        e.scheduler.model_actions = ModelActionController(e.scheduler,e.transport,monotonic=e.monotonic)
        e.scheduler.placement = PlacementController(e.scheduler,e.transport,probe=e.probe,monotonic=e.monotonic)
        e.scheduler.sleeping_recovery = SleepingRecoveryController(e.scheduler,monotonic=e.monotonic)
    return e.scheduler.sleeping_recovery


def test_acknowledged_destination_can_finish_observation_only_after_reopen(execution, monkeypatch):
    e=execution;original=e.store.advance_recovery
    def interrupted_complete(claim, **kwargs):
        if kwargs.get("stage")=="complete" and claim.stage=="destination":
            raise sqlite3.OperationalError("fixture interrupted completion")
        return original(claim,**kwargs)
    monkeypatch.setattr(e.store,"advance_recovery",interrupted_complete)
    result=e.controller.run_once()
    assert result["status"]=="partial" and e.store.recovery("source").wake_acknowledged
    calls=list(e.state["calls"])
    restarted=reopen_recovery(e)
    try:
        result=restarted.run_once()
        assert result["status"]=="relocated" and result["ready"] is True and result["observations_only"]
        assert e.state["calls"]==calls and e.store.recoveries()==()
    finally:
        e.scheduler.stop();e.store.close()


def test_late_old_wake_reentry_after_controller_reopen_cannot_allocate(execution):
    from concurrent.futures import ThreadPoolExecutor
    e=execution;entered=threading.Event();release=threading.Event()
    def before_place():
        entered.set(); assert release.wait(3)
    e.state["before_place"]=before_place
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(e.controller.run_once)
        try:
            assert entered.wait(3)
            restarted=reopen_recovery(e)
        finally:
            release.set()
        result=pending.result(timeout=4)
    try:
        assert result["status"]=="partial" and result["ready"] is False
        assert e.state["place_results"][0][0]==503
        assert e.store.leases()==() and not any(call[0]=="launch" for call in e.state["calls"])
        before=list(e.state["calls"])
        assert restarted.run_once()["error"]=="recovery_requires_settlement"
        assert e.state["calls"]==before and e.store.recovery("source").wake_submitted
    finally:
        e.scheduler.stop();e.store.close()


def test_missing_source_timestamp_never_creates_recovery_claim(execution):
    e=execution;e.state["source_time"]=False
    result=e.controller.run_once()
    assert result["error"]=="unknown_collection_provenance" and e.state["calls"]==[]
    assert e.store.recoveries()==() and e.store._db.execute("PRAGMA user_version").fetchone()[0]==2


def test_no_progress_stop_keeps_full_budget_and_is_not_replayed(execution):
    e=execution;e.state["stop_mode"]="no-effect"
    e.state["collect_hook"]=lambda: e.state.update(offset=10.) if e.state["stop_seen"].is_set() else None
    result=e.controller.run_once()
    assert result["status"]=="partial" and result["source_released"] is False
    assert e.state["calls"]==[("stop","source")]
    assert e.store.lease("source-lease")[0].budget_gb==80
    e.state["collect_hook"]=None
    e.controller.run_once()
    assert e.state["calls"]==[("stop","source")] and e.store.recovery("source") is not None


def test_new_destination_reserve_during_probe_prevents_stale_lease_grant(execution, monkeypatch):
    e=execution;changed=[]
    def probe_hook(count):
        claim=e.store.recovery("source")
        if claim is not None and claim.stage=="waking" and not changed:
            changed.append(True)
            e.store.put_reserve(Reserve("arrived",1,1,20000,"new-owner"))
    e.state["probe_hook"]=probe_hook
    original=e.scheduler.placement._decision
    def decision(*args,**kwargs):
        result=original(*args,**kwargs)
        if changed and any(b.reason=="reserved" for b in result[1]):
            e.state["offset"]=10.  # Deterministic expiry after the actual blocker was observed.
        return result
    monkeypatch.setattr(e.scheduler.placement,"_decision",decision)
    result=e.controller.run_once()
    assert result["status"]=="partial" and result["ready"] is False
    assert changed and e.store.leases()==()
    assert not any(call[0]=="launch" for call in e.state["calls"])
    assert e.store.active(e.state["now"])[1][0].id=="arrived"


def test_late_accepted_cold_response_cannot_complete_or_be_resent(execution):
    e=execution;e.state["after_grant"]=lambda lease_id:e.state.update(offset=10.)
    result=e.controller.run_once()
    assert result["status"]=="partial" and result["ready"] is False
    claim=e.store.recovery("source")
    assert claim.stage=="destination" and claim.wake_submitted and not claim.wake_acknowledged
    assert e.store.lease(claim.destination_lease_id)[0].budget_gb==80
    before=list(e.state["calls"])
    e.controller.run_once()
    assert e.state["calls"]==before and e.store.recovery("source") is not None


def test_shutdown_during_source_observation_retains_claim_and_starts_no_later_action(execution):
    from concurrent.futures import ThreadPoolExecutor
    e=execution;entered=threading.Event();release=threading.Event()
    def collect():
        if e.state["stop_seen"].is_set():
            entered.set(); assert release.wait(3)
    e.state["collect_hook"]=collect
    with ThreadPoolExecutor(2) as pool:
        pending=pool.submit(e.controller.run_once)
        try:
            assert entered.wait(3)
            shutdown=pool.submit(e.scheduler.stop)
            assert e.scheduler.stopping.wait(3)
        finally: release.set()
        result=pending.result(timeout=4)
        shutdown.result(timeout=4)
    assert result["status"]=="partial" and result["error"]=="scheduler_stopping"
    assert e.state["calls"]==[("stop","source")]
    assert e.store.recovery("source") is not None
    assert e.store.lease("source-lease")[0].status=="confirmed"


@pytest.mark.parametrize("pending_weight", [100,None])
def test_pending_cold_start_weight_is_reserved_in_preflight(execution,pending_weight):
    e=execution
    with e.scheduler.action_lock, e.lock:
        e.state["available"]=200
        e.state["extra_models"]=(ModelState("pending",state="stopped",unit="vllm-pending.service",unit_active=False,weights_gb=pending_weight),)
        e.transport.models["pending"]={"unit":"vllm-pending.service","util":.1,"weights_gb":pending_weight}
        e.transport.units["pending"]="vllm-pending.service"
        e.transport.paths.update((("POST","/api/models/unload/pending"),("GET","/upstream/pending/")))
        e.store.create_lease(Lease("pending-lease","pending",0,.1,20000,10),"vllm-pending.service")
    before=e.store.leases()
    result=e.controller.run_once()
    assert result["status"]=="blocked" and result["ready"] is False
    assert e.state["calls"]==[] and e.store.recoveries()==() and e.store.leases()==before


def test_old_source_tombstone_still_rejects_late_confirm_during_destination_start(execution):
    e=execution;late=[]
    e.state["after_grant"]=lambda lease_id:late.append(http_request(e.address,"POST","/v1/place/source-lease/confirm"))
    result=e.controller.run_once()
    assert result["status"]=="relocated"
    assert late and late[0][0]==409 and late[0][1]["error"]=="lease_revoked"
    assert len(e.store.leases())==1 and e.store.lease(result["lease_id"])[0].status=="confirmed"


def test_hostname_only_recovery_origin_is_blocked_before_source_stop(execution):
    e=execution;e.transport.swap_url=e.transport.swap_url.replace("127.0.0.1","localhost")
    result=e.controller.run_once()
    assert result["status"]=="blocked" and e.state["calls"]==[] and e.store.recoveries()==()
    assert result["blocked_by"][0]["reason"]=="unsupported_recovery_origin"
