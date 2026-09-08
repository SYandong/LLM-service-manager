# Generated-By: Codex / gpt-6-astra
"""Deterministic M2 cycle replay with fake units, SQLite and loopback sleep."""

import http.client
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import AutomaticPolicyController, ManagedModelTransport, ModelActionController
from llmsvc.collectors.relay import DataPlaneEventRelay
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.scheduler import DataPlaneBridge, Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, StateSnapshot
from llmsvc.store import IntentStore


@pytest.fixture
def system(tmp_path):
    state = {"now":10000.0, "available":500.0, "calls":[], "mode":"apply", "gain":40,
             "idle":{"a":1000,"b":1000}, "inflight":{"a":0,"b":0}, "errors":(),
             "after_action":None, "collect_hook":None, "probe_hook":None,
             "transport_entered":threading.Event(), "transport_release":threading.Event(),
             "events":queue.Queue(), "source_done":threading.Event()}
    state["transport_release"].set()
    models = {name:ModelState(name, state="awake" if name=="a" else "sleeping", gpu=0, util=0.3,
        budget_gb=60, weights_gb=40, resident_gb=60 if name=="a" else 2,
        unit="vllm-"+name+".service", unit_active=True, health_ok=True,
        is_sleeping=name!="a", cold_start_seconds=1) for name in ("a","b")}
    observations = {name:UnitObservation(True, False, True, "lease-"+name) for name in models}
    def apply(kind, name):
        state["calls"].append((kind,name))
        state["transport_entered"].set()
        assert state["transport_release"].wait(2)
        if state["mode"] not in ("no-effect", "error"):
            model = models[name]
            models[name] = replace(model, state="sleeping" if kind=="sleep" else "stopped",
                is_sleeping=True, unit_active=kind=="sleep", resident_gb=2 if kind=="sleep" else 0)
            if kind=="sleep":
                state["available"] -= model.weights_gb
            else:
                state["available"] += state["gain"]
                observations[name] = UnitObservation(False, True) if state["mode"]!="unconfirmed-exit" else UnitObservation(True,False)
        if state["after_action"]:
            state["after_action"](kind,name)
        return state["mode"] not in ("error", "error-after-effect")
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            assert self.path.startswith("/api/models/unload/")
            name = self.path.rsplit("/",1)[1]
            self.send_response(200 if apply("sleep",name) else 500)
            self.send_header("Content-Length","0")
            self.end_headers()
        def do_GET(self):
            assert self.path == "/api/events"
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            while not state["source_done"].is_set():
                try:
                    item=state["events"].get(timeout=0.02)
                    data=("data: "+json.dumps(item)+"\n\n").encode()
                except queue.Empty:
                    data=b": heartbeat\n\n"
                try:
                    self.wfile.write(data); self.wfile.flush()
                except OSError:
                    break
    upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
    thread=threading.Thread(target=lambda:upstream.serve_forever(poll_interval=0.01))
    thread.start()
    config=SchedulerConfig("127.0.0.1",8011,read_only=False,model_actions_enabled=True,automation_enabled=True,
        state_db_path=str(tmp_path/"state.sqlite"),automation_cycle_timeout_seconds=2,
        automation_interval_seconds=30,action_observe_seconds=0.8,action_poll_seconds=0.005,
        request_timeout_seconds=1,lease_probe_seconds=0.1)
    store=IntentStore(config.state_db_path, action_lock=threading.RLock())
    for name in models:
        store.create_lease(Lease("lease-"+name,name,0,0.3,20000,60),"vllm-"+name+".service")
        store.transition_lease("lease-"+name,"confirmed")
    def collect():
        if state["collect_hook"]:
            state["collect_hook"]()
        state["now"] += 0.01
        sleeping=sum(m.weights_gb for m in models.values() if m.state=="sleeping")
        return StateSnapshot(sampled_at=state["now"],models=tuple(models.values()),errors=state["errors"],
            gpus=(GPUState(0,total_gb=200,free_gb=100,external_gb=0),),
            memory=MemoryState(state["available"],state.get("sleeping",sleeping)),
            activity=tuple(Activity(name,state["now"]-state["idle"][name],0,0,state["inflight"][name]) for name in models))
    scheduler=Scheduler(config,collect,store=store,clock=lambda:state["now"])
    def run(argv,**kwargs):
        assert argv[:2] == ["fake-systemctl","stop"] and kwargs["timeout"]>0
        name=argv[2][len("vllm-"):-len(".service")]
        return SimpleNamespace(returncode=0 if apply("stop",name) else 1)
    transport=ManagedModelTransport(swap_url="http://127.0.0.1:"+str(upstream.server_port),
        models={name:{"unit":m.unit} for name,m in models.items()},systemctl="fake-systemctl",run=run)
    scheduler.model_actions=ModelActionController(scheduler,transport)
    def probe(name,*,deadline):
        if state["probe_hook"]:
            state["probe_hook"](name)
        return observations[name]
    accounting=PlacementController(scheduler,transport,probe=probe)
    cycle=AutomaticPolicyController(scheduler,accounting=accounting)
    scheduler.sample_once()
    try:
        yield scheduler,cycle,state,models,transport,observations
    finally:
        state["transport_release"].set()
        state["source_done"].set()
        scheduler.stop()
        store.close()
        upstream.shutdown(); upstream.server_close(); thread.join(2)


def start(system):
    scheduler,cycle,*_=system
    scheduler.start()  # Only sampler; manual tests do not attach the auto worker.
    return scheduler,cycle


def test_idle_cycle_uses_real_loopback_sleep_and_retains_full_account(system):
    scheduler,cycle,state,models,_,_=system
    start(system)
    result=cycle.run()
    assert result["status"]=="complete",result
    assert state["calls"]==[("sleep","a")]
    assert models["a"].state=="sleeping"
    assert scheduler.store.lease("lease-a")[0].status=="confirmed"
    assert scheduler.store.lease("lease-a")[0].budget_gb==60
    assert result["actions"][0]["reason"]=="idle_ttl" and "freed_gb" not in result


@pytest.mark.parametrize("reason",["pin","inflight","unknown-inflight","unknown-snapshot","unleased","recent"])
def test_protection_and_unknown_never_act(system,reason):
    scheduler,cycle,state,models,_,_=system
    if reason=="pin":scheduler.store.put_pin(Pin("a",20000,"owner"))
    elif reason=="inflight":state["inflight"]["a"]=1
    elif reason=="unknown-inflight":state["inflight"]["a"]=None
    elif reason=="unknown-snapshot":state["errors"]=("fixture unknown",)
    elif reason=="unleased":scheduler.store.transition_lease("lease-a","released")
    else:scheduler.config=replace(scheduler.config,automation_idle_seconds=1200)
    start(system)
    result=cycle.run()
    assert result["status"]=="blocked",result
    assert state["calls"]==[] and models["a"].state=="awake"


def test_memory_pressure_replans_from_real_gain_not_policy_estimate(system):
    scheduler,cycle,state,models,_,_=system
    models["a"]=replace(models["a"],state="sleeping",is_sleeping=True)
    state.update(available=140,gain=5)
    start(system)
    result=cycle.run()
    assert result["status"]=="complete",result
    assert state["calls"]==[("stop","a"),("stop","b")]
    assert state["available"]==150
    assert all(lease.status=="released" for lease,_ in scheduler.store.leases(include_released=True))


def test_budget_pressure_stops_only_until_observed_membership_fits(system):
    scheduler,cycle,state,models,_,_=system
    models["a"]=replace(models["a"],state="sleeping",is_sleeping=True)
    scheduler.config=replace(scheduler.config,memory_budget_gb=50)
    start(system)
    result=cycle.run()
    assert result["status"]=="complete",result
    assert state["calls"]==[("stop","a")]
    assert models["b"].state=="sleeping" and scheduler.store.lease("lease-b")[0].status=="confirmed"


def test_default_is_never_hard_stopped_and_failed_sleep_admission_holds_awake(system):
    scheduler,cycle,state,models,transport,_=system
    transport.models["a"]["is_default"]=True
    transport.models["b"]["is_default"]=True
    state["available"]=160
    start(system)
    result=cycle.run()
    assert result["status"]=="blocked",result
    assert state["calls"]==[] and models["a"].state=="awake"
    state["available"]=140
    assert cycle.run()["status"]=="blocked"
    assert state["calls"]==[]


def test_sleep_admission_reclaims_and_reobserves_before_sleep(system):
    scheduler,cycle,state,models,_,_=system
    state["available"]=160
    start(system)
    result=cycle.run()
    assert result["status"]=="complete",result
    assert state["calls"]==[("stop","b"),("sleep","a")]
    assert state["available"]==160


@pytest.mark.parametrize("mode",["error","no-effect","unconfirmed-exit"])
def test_failed_or_unknown_effect_retains_budget_and_stops_cycle(system,mode):
    scheduler,cycle,state,models,_,_=system
    models["a"]=replace(models["a"],state="sleeping",is_sleeping=True)
    state.update(available=140,mode=mode)
    scheduler.config=replace(scheduler.config,action_observe_seconds=0.05)
    start(system)
    result=cycle.run()
    assert result["status"] in ("failed","no_progress"),result
    assert state["calls"]==[("stop","a")]
    assert scheduler.store.lease("lease-a")[0].status=="confirmed"
    assert not cycle.active and not scheduler.model_actions.pending


def test_error_after_observed_exit_keeps_partial_result_and_stops(system):
    scheduler,cycle,state,models,_,_=system
    models["a"]=replace(models["a"],state="sleeping",is_sleeping=True)
    state.update(available=140,mode="error-after-effect")
    start(system)
    result=cycle.run()
    assert result["status"]=="partial" and len(result["actions"])==1,result
    assert state["calls"]==[("stop","a")]
    assert scheduler.store.lease("lease-a")[0].status=="released"


def test_no_measured_memory_progress_prevents_second_victim(system):
    scheduler,cycle,state,models,_,_=system
    models["a"]=replace(models["a"],state="sleeping",is_sleeping=True)
    state.update(available=140,gain=0)
    start(system)
    result=cycle.run()
    assert result["status"]=="no_progress",result
    assert state["calls"]==[("stop","a")]
    assert scheduler.store.lease("lease-b")[0].status=="confirmed"


def test_new_pin_during_final_identity_probe_is_revalidated(system):
    scheduler,cycle,state,models,_,_=system
    state["probe_hook"]=lambda name:scheduler.store.put_pin(Pin(name,20000,"late-owner"))
    start(system)
    result=cycle.run()
    assert result["status"]=="blocked",result
    assert state["calls"]==[]


@pytest.mark.parametrize("flag,value",[("automation_enabled",False),("model_actions_enabled",False),("read_only",True)])
def test_three_optins_and_dry_run_are_zero_effects(system,flag,value,monkeypatch):
    scheduler,cycle,state,_,transport,_=system
    scheduler.config=replace(scheduler.config,**{flag:value})
    before=scheduler.events_since(0),open(scheduler.config.state_db_path,"rb").read()
    scheduler.collect=lambda:pytest.fail("disabled/preview collected")
    transport.run=lambda *a,**k:pytest.fail("disabled/preview acted")
    cycle.accounting.probe=lambda *a,**k:pytest.fail("disabled/preview probed")
    assert cycle.run()["status"]=="disabled"
    preview=cycle.run(dry_run=True)
    assert preview["would"][0]["kind"]=="sleep"
    assert (scheduler.events_since(0),open(scheduler.config.state_db_path,"rb").read())==before
    assert not cycle.active and state["calls"]==[]


def test_no_overlapping_cycles_with_deterministic_barrier(system):
    scheduler,cycle,state,_,_,_=system
    entered,release=threading.Event(),threading.Event()
    active=threading.Event()
    def block():
        entered.set(); release.wait(2)
    state["collect_hook"]=block
    fresh=cycle._fresh_round
    def announce(deadline):
        active.set()
        return fresh(deadline)
    cycle._fresh_round=announce
    start(system)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(cycle.run)
        try:
            assert entered.wait(1) and active.wait(1)
            result=cycle.run()
            assert result["blocked_by"]==[{"model":None,"reason":"cycle_in_progress"}]
            release.set()
            assert pending.result(timeout=2)["status"]=="complete"
            assert state["calls"]==[("sleep","a")]
        finally:release.set()


def test_stalled_sampler_cannot_extend_cycle_deadline(system):
    scheduler,cycle,state,_,_,_=system
    release=threading.Event()
    state["collect_hook"]=lambda:release.wait(1)
    scheduler.config=replace(scheduler.config,automation_cycle_timeout_seconds=0.08)
    start(system)
    started=time.monotonic()
    try:
        result=cycle.run()
        assert result["status"]=="timeout" and time.monotonic()-started<0.3
        assert state["calls"]==[] and not cycle.active
    finally:release.set()


def test_real_pin_http_progresses_during_observation_and_blocks_next_action(system):
    scheduler,cycle,state,models,_,_=system
    models["b"]=replace(models["b"],state="awake",is_sleeping=False)
    entered,release=threading.Event(),threading.Event()
    def block_after_action():
        if state["calls"]:entered.set(); release.wait(1)
    state["collect_hook"]=block_after_action
    start(system)
    server=SchedulerHTTPServer(("127.0.0.1",0),scheduler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01)); thread.start()
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(cycle.run)
        try:
            assert entered.wait(1)
            connection=http.client.HTTPConnection(*server.server_address,timeout=1)
            connection.request("POST","/v1/pin",json.dumps({"model":"b","until":20000,"by":"claim"}))
            response=connection.getresponse(); assert response.status==200; response.read(); connection.close()
            release.set()
            result=pending.result(timeout=2)
            assert result["status"]=="partial",result
            assert state["calls"]==[("sleep","a")] and models["b"].state=="awake"
            assert scheduler.snapshot().pins[0].model=="b"
        finally:
            release.set(); server.shutdown(); server.server_close(); thread.join(2)


def test_scheduled_worker_runs_only_when_opted_in_and_cleans_up(system):
    scheduler,cycle,state,_,_,_=system
    scheduler.automation=cycle
    scheduler.start()
    limit=time.monotonic()+2
    while not any(e.kind=="automation_result" for e in scheduler.events_since(0)) and time.monotonic()<limit:
        time.sleep(0.005)
    assert state["calls"]==[("sleep","a")]
    scheduler.stop()
    assert not scheduler._automation_thread.is_alive() and not cycle.active
    assert not scheduler.model_actions.pending


def test_source_reader_proceeds_while_automatic_transport_holds_action_lock(system):
    scheduler,cycle,state,_,transport,_=system
    scheduler.config=replace(scheduler.config,data_plane_events_enabled=True)
    relay=DataPlaneEventRelay(transport.swap_url,["a","b"],capacity=2,timeout=0.3)
    scheduler.event_bridge=DataPlaneBridge(scheduler,relay)
    state["transport_release"].clear()
    start(system)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(cycle.run)
        try:
            assert state["transport_entered"].wait(1)
            for index in range(40):
                state["events"].put({"type":"modelStatus","data":[{"id":"a","state":"ready" if index%2 else "starting"}]})
            limit=time.monotonic()+0.5
            while time.monotonic()<limit:
                with relay.buffer._lock:
                    dropped=relay.buffer._dropped.get("buffer_full",0)
                dropped+=(scheduler.event_bridge.pending or {}).get("dropped_by_reason",{}).get("buffer_full",0)
                if dropped>5:break
                time.sleep(0.005)
            assert dropped>5 and relay.subscription._thread.is_alive()
        finally:
            state["transport_release"].set(); pending.result(timeout=2)


@pytest.mark.parametrize("flag,value",[("automation_enabled",False),("model_actions_enabled",False),("read_only",True)])
def test_scheduler_never_starts_automation_thread_without_all_gates(system,flag,value):
    scheduler,cycle,state,_,_,_=system
    scheduler.config=replace(scheduler.config,**{flag:value})
    scheduler.automation=cycle
    scheduler.start()
    assert scheduler._automation_thread is None
    scheduler.stop()
    assert state["calls"]==[]


def test_shutdown_during_observation_stops_future_actions_and_keeps_unconfirmed_account(system):
    scheduler,cycle,state,models,_,_=system
    models["a"]=replace(models["a"],state="sleeping",is_sleeping=True)
    state["available"]=140
    blocked,release=threading.Event(),threading.Event()
    finished=threading.Event()
    emit=scheduler.emit
    def finished_cycle(kind,**kwargs):
        event=emit(kind,**kwargs)
        if kind=="automation_result":finished.set()
        return event
    scheduler.emit=finished_cycle
    def hold():
        if state["calls"]:
            blocked.set(); release.wait(2)
    state["collect_hook"]=hold
    scheduler.automation=cycle
    scheduler.start()
    assert blocked.wait(1)
    with ThreadPoolExecutor(1) as pool:
        stopping=pool.submit(scheduler.stop)
        assert scheduler.stopping.wait(1) and finished.wait(1)
        assert scheduler.store.lease("lease-a")[0].status=="confirmed"
        release.set()
        stopping.result(timeout=3)
    assert state["calls"]==[("stop","a")]
    assert not scheduler._automation_thread.is_alive() and not cycle.active
    assert not scheduler.model_actions.pending
    assert scheduler.store.lease("lease-b")[0].status=="confirmed"
    # Late sampler publication alone is not permission to release accounting.
    assert scheduler.store.lease("lease-a")[0].status=="confirmed"


def test_new_unknown_observation_blocks_next_action_after_confirmed_progress(system):
    scheduler,cycle,state,models,_,_=system
    models["b"]=replace(models["b"],state="awake",is_sleeping=False)
    emit=scheduler.emit
    def unknown_after(kind,**kwargs):
        event=emit(kind,**kwargs)
        if kind=="automation_action_result" and event.detail["confirmed"]:
            state["errors"]=("fixture source unavailable",)
        return event
    scheduler.emit=unknown_after
    start(system)
    result=cycle.run()
    assert result["status"]=="partial" and result["error"]=="unknown_or_stale_snapshot"
    assert state["calls"]==[("sleep","a")]


@pytest.mark.parametrize("change",["ram","unknown","inflight","readonly","automation"])
def test_final_probe_rechecks_current_admission_and_optins(system,change):
    scheduler,cycle,state,models,_,_=system
    def change_once(name):
        state["probe_hook"]=None
        if change=="ram":state["available"]=160
        elif change=="unknown":state["available"]=None
        elif change=="inflight":state["inflight"]["a"]=1
        elif change=="readonly":scheduler.config=replace(scheduler.config,read_only=True)
        else:scheduler.config=replace(scheduler.config,automation_enabled=False)
        scheduler.sample_once()
    state["probe_hook"]=change_once
    start(system)
    result=cycle.run()
    if change=="ram":
        assert state["calls"]==[("stop","b"),("sleep","a")],result
        assert state["available"]>=150
    else:
        assert state["calls"]==[] and result["status"]=="blocked",result


def test_unknown_or_changed_unit_lease_identity_blocks_automation(system):
    scheduler,cycle,state,_,_,observations=system
    observations["a"]=UnitObservation(True,False,True,"other-lease")
    start(system)
    result=cycle.run()
    assert result["error"]=="unit_identity_unconfirmed" and state["calls"]==[]


def test_entrypoint_once_logs_preview_without_starting_cycle(system,monkeypatch,caplog):
    import logging
    import llmsvc.__main__ as entry
    scheduler,cycle,state,_,_,_=system
    config=replace(scheduler.config,read_only=True)
    original_scheduler=entry.Scheduler
    monkeypatch.setattr(entry,"Scheduler",lambda *a,**k:original_scheduler(*a,**k,clock=lambda:state["now"]))
    monkeypatch.setattr(entry,"load_config",lambda path:config)
    monkeypatch.setattr(entry,"build_collector",lambda cfg:scheduler.collect)
    monkeypatch.setattr(entry,"build_event_relay",lambda cfg:None)
    # Main constructs a real read-only store and action metadata; no transport starts.
    config=replace(config,collectors={"swap_url":"http://127.0.0.1:1","models":{"a":{"unit":"vllm-a.service"},"b":{"unit":"vllm-b.service"}}})
    monkeypatch.setattr(entry,"load_config",lambda path:config)
    monkeypatch.setattr("sys.argv",["llmsvc","--config","unused","--once"])
    before=open(config.state_db_path,"rb").read()
    initial_time=state["now"]
    with caplog.at_level(logging.INFO,logger="llmsvc.actions"):
        assert entry.main()==0
    preview=next(json.loads(record.message) for record in caplog.records if '"kind": "automation_preview"' in record.message)
    assert preview["would"][0]["kind"]=="sleep"
    assert state["now"]-initial_time==pytest.approx(0.01)
    assert open(config.state_db_path,"rb").read()==before and state["calls"]==[]


def test_shutdown_at_final_plan_boundary_prevents_transport(system):
    scheduler,cycle,state,_,_,_=system
    plan=cycle.plan
    calls=[0]
    def stop_at_final_check(snapshot):
        decision=plan(snapshot)
        calls[0]+=1
        if calls[0]==2:scheduler.stopping.set()
        return decision
    cycle.plan=stop_at_final_check
    start(system)
    result=cycle.run()
    assert state["calls"]==[],result
    assert not cycle.active and not scheduler.model_actions.pending
