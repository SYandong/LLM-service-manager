# Generated-By: Codex / gpt-6-astra
"""Real queue/core/SQLite lifecycle; proof and units are explicit fixtures only."""

import hashlib
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

import pytest
import yaml

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.catalog import CatalogRuntime
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.reload import QuietPeriod, RecoveryProof, ReloadError, ReloadQueue
from llmsvc.reload_witness import CandidateBinding, InstanceIdentity
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, StateSnapshot
from llmsvc.store import IntentStore
from test_registry_http_preview import request
from test_reload import Clock, make_quiet
from test_registry_api import registry as registry_fixture


def profile(name, port):
    return {"unit": "vllm-"+name+".service", "daemon_url": "http://127.0.0.1:"+str(port),
            "port": port, "util": .4, "budget_gb": 40, "weights_gb": 10, "is_default": False}


@pytest.fixture
def catalog(tmp_path):
    clock = Clock()
    world = {"units": {}, "calls": [], "collectors": [], "fail_proof": False}
    original_models = {"base": profile("base", 8101)}
    path = tmp_path/"swap.yaml"
    path.write_text(yaml.safe_dump({"models": {"base": {}}}))
    cfg = SchedulerConfig("127.0.0.1", 8011, read_only=False, catalog_enabled=True,
        model_actions_enabled=True, placement_enabled=True, placement_wait_seconds=2,
        collectors={"swap_url":"http://127.0.0.1:1", "systemctl":"unused", "models":original_models},
        state_db_path=str(tmp_path/"state.sqlite"))
    store = IntentStore(cfg.state_db_path, action_lock=threading.RLock())
    class Collector:
        def __init__(self, config):
            self.models = config.collectors["models"]
            self.closed = False
            self.hold = None
            world["collectors"].append(self)
        def __call__(self):
            if self.hold: self.hold()
            models = []
            for name, meta in self.models.items():
                exists = world["units"].get(name, UnitObservation(False, True))
                models.append(ModelState(name, state="awake" if exists.active else "stopped", gpu=0 if exists.active else None,
                    unit=meta["unit"], unit_active=exists.active, health_ok=bool(exists.active),
                    is_sleeping=False, swap_state="ready" if exists.active else "stopped",
                    util=meta["util"], budget_gb=meta["budget_gb"], weights_gb=meta["weights_gb"], is_default=False))
            return StateSnapshot(sampled_at=clock(), models=tuple(models),
                activity=tuple(Activity(name, last_request_at=clock()-10, requests_last_hour=0, in_flight=0) for name in self.models),
                gpus=(GPUState(0,total_gb=100,free_gb=100,external_gb=0),), memory=MemoryState(500,0))
        def close(self):
            self.closed = True
    def transport(config, models):
        def forbidden(*args, **kwargs): pytest.fail("catalog invoked a model transport")
        return ManagedModelTransport(swap_url=config.collectors["swap_url"],models=models,
                                     systemctl="unused",run=forbidden)
    collector = Collector(cfg)
    scheduler = Scheduler(cfg,collector,store=store,clock=clock)
    t = transport(cfg,original_models)
    scheduler.model_actions = ModelActionController(scheduler,t)
    scheduler.placement = PlacementController(scheduler,t,probe=lambda name,**kw: world["units"].get(name,UnitObservation(False,True)))
    scheduler.sample_once()
    quiet = QuietPeriod(clock)
    queue = ReloadQueue(path,action_lock=scheduler.action_lock,quiet=quiet,snapshot=scheduler.snapshot,
        validate=lambda path: world["calls"].append("validate"),notify_reload=lambda **kw: world["calls"].append("adopt"),
        log=lambda event: None,clock=clock,wall_clock=clock)
    def verify(record, *, deadline):
        world["calls"].append("verify")
        return RecoveryProof(hashlib.sha256(json.dumps(record,allow_nan=False).encode()).hexdigest(),
            True,True,not world["fail_proof"],True,InstanceIdentity(123,"456"))
    runtime = CatalogRuntime(scheduler,queue,verifier=verify,collector_factory=Collector,
                             relay_factory=lambda config:None,transport_factory=transport)
    models={**original_models,"new":profile("new",21001)}
    candidate=yaml.safe_dump({"macros":{"llmsvc_reload_generation":"gen_"+"1"*32},
                              "models":{name:{} for name in models}}).encode()
    binding=CandidateBinding("http://127.0.0.1:1/api/mcp", "gen_"+"1"*32,InstanceIdentity(123,"456"),hashlib.sha256(candidate).hexdigest())
    server=SchedulerHTTPServer(("127.0.0.1",0),scheduler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));thread.start()
    try:
        yield SimpleNamespace(clock=clock,world=world,path=path,cfg=cfg,store=store,s=scheduler,q=queue,
            runtime=runtime,models=models,candidate=candidate,binding=binding,collector=collector,
            collector_factory=Collector,transport_factory=transport,verify=verify,address=server.server_address)
    finally:
        scheduler.stop();server.shutdown();server.server_close();thread.join(3);scheduler.store.close()


def prepare(c):
    return c.runtime.prepare(c.candidate,c.models,binding=c.binding)


def install(c):
    job=c.runtime.enqueue(prepare(c))
    make_quiet(c.q.quiet,c.clock)
    return c.runtime.process_once(), job


def test_real_queue_installs_catalog_then_existing_http_can_place_and_confirm(catalog):
    c=catalog
    result,job=install(c)
    assert result["status"]=="applied",result
    assert c.store.catalog_checkpoint()["phase"]=="released" and not c.s.catalog_fenced
    assert c.collector.closed and "new" in c.s.model_actions.transport.active_models
    c.s.sample_once()
    status,placed=request(c.address,"POST","/v1/place",{"model":"new","util":.2})
    assert status==200,placed
    assert c.store.lease(placed["lease_id"])[0].budget_gb==40
    c.world["units"]["new"]=UnitObservation(True,False,True,placed["lease_id"],"1"*32)
    c.s.sample_once()
    status,confirmed=request(c.address,"POST","/v1/place/"+placed["lease_id"]+"/confirm")
    assert status==200 and confirmed["status"]=="confirmed"


def test_prepare_and_dryrun_leave_all_objects_files_and_schema_unchanged(catalog):
    c=catalog
    before=c.path.read_bytes(),c.store.path.read_bytes() if hasattr(c.store,"path") else None,len(c.world["collectors"])
    prepared=prepare(c)
    assert c.runtime.enqueue(prepared,dry_run=True)["would"]
    assert c.world["calls"]==[] and not c.q._pending and c.store.catalog_checkpoint() is None
    assert c.store._db.execute("PRAGMA user_version").fetchone()[0]==2
    assert c.path.read_bytes()==before[0] and len(c.world["collectors"])==before[2]


def test_unknown_settlement_keeps_old_catalog_and_durable_global_gate(catalog):
    c=catalog;c.world["fail_proof"]=True
    result,_=install(c)
    assert result["status"]=="reconciliation_required"
    assert c.s.catalog_fenced and c.store.catalog_pending()
    assert c.s.collect is c.collector and "new" not in c.s.placement.transport.active_models
    with pytest.raises(ValueError,match="catalog"):
        c.store.create_lease(Lease("bypass","base",0,.4,20000,40),"vllm-base.service")
    status,body=request(c.address,"POST","/v1/place",{"model":"base","util":.4})
    assert status==503 and body["error"]=="catalog_reconciliation_required"
    c.store.put_pin(Pin("base",20000,"owner"))  # Protection writes are still permitted.


def test_marker_unlink_and_repair_failure_still_fences_restart(catalog,monkeypatch):
    c=catalog
    real_sync=c.q._sync_directory
    def sync():
        if c.store.catalog_checkpoint() and c.store.catalog_checkpoint()["phase"]=="published":
            raise OSError("fixture directory unavailable")
        real_sync()
    monkeypatch.setattr(c.q,"_sync_directory",sync)
    result,_=install(c)
    assert result["status"]=="reconciliation_required" and c.store.catalog_checkpoint()["phase"]=="published"
    assert c.s.catalog_fenced and c.q.fenced
    # Simulate complete receipt loss independently of the durable core ledger.
    c.q.marker.unlink(missing_ok=True)
    c.store.close()
    c.store=IntentStore(c.cfg.state_db_path,action_lock=c.s.action_lock)
    c.s.store=c.store
    restored=CatalogRuntime(c.s,c.q,verifier=c.verify,collector_factory=c.collector_factory,
        relay_factory=lambda config:None,transport_factory=c.transport_factory)
    assert c.s.catalog_fenced and c.store.catalog_pending()
    assert "new" in c.s.collect.models
    status,_=request(c.address,"POST","/v1/place",{"model":"new","util":.4})
    assert status==503


@pytest.mark.parametrize("bad", ["weights", "budget", "role", "port", "endpoint", "default"])
def test_untrusted_or_incomplete_profile_never_stages_or_allocates(catalog,bad):
    c=catalog;meta=c.models["new"]
    if bad=="weights": meta["weights_gb"]=None
    elif bad=="budget": meta["budget_gb"]=float("nan")
    elif bad=="role": meta["is_default"]=None
    elif bad=="port": meta["port"]=21000
    elif bad=="endpoint": meta["daemon_url"]="http://localhost:21001"
    elif bad=="default": meta["is_default"]=True
    with pytest.raises(ValueError): prepare(c)
    assert not c.world["calls"] and c.store.catalog_checkpoint() is None


@pytest.mark.parametrize("option", ["read_only","catalog_enabled"])
def test_default_or_readonly_option_does_not_enable_a_catalog_writer(catalog,option):
    c=catalog;c.s.config=replace(c.s.config,**{option:option=="read_only"})
    with pytest.raises(ReloadError,match="disabled"):
        c.runtime.enqueue(prepare(c))
    assert c.store.catalog_checkpoint() is None and c.world["calls"]==[]


def test_old_collection_and_old_transport_cannot_publish_or_act_after_install(catalog):
    c=catalog;entered=threading.Event();release=threading.Event()
    old_transport=c.s.model_actions.transport
    def hold():
        entered.set();assert release.wait(3)
    c.collector.hold=hold
    t=threading.Thread(target=c.s.sample_once);t.start()
    try:
        assert entered.wait(3)
        result,_=install(c)
        assert result["status"]=="applied"
        current=c.s.sample_once()
        release.set();t.join(3)
        assert not t.is_alive() and c.s.snapshot().models==current.models
        assert "new" in {m.name for m in current.models}
        from llmsvc.actions import ActionDispatchError
        with pytest.raises(ActionDispatchError,match="catalog_generation_changed"):
            old_transport.stop_unit("vllm-base.service",deadline=10**12)
    finally:
        release.set();t.join(3)


def test_final_checkpoint_failure_keeps_published_catalog_fenced(catalog,monkeypatch):
    c=catalog;real=c.store.save_catalog
    def save(expected,record,**kwargs):
        if record["phase"]=="released": raise OSError("fixture checkpoint failure")
        return real(expected,record,**kwargs)
    monkeypatch.setattr(c.store,"save_catalog",save)
    with pytest.raises(OSError): install(c)
    assert c.store.catalog_checkpoint()["phase"]=="published" and c.s.catalog_fenced
    assert not c.q.fenced and "new" in c.s.collect.models
    status,_=request(c.address,"POST","/v1/place",{"model":"new","util":.4})
    assert status==503


def test_readonly_recovery_preview_has_no_verifier_or_store_effect(catalog):
    c=catalog;c.world["fail_proof"]=True
    install(c)
    before=c.store.catalog_checkpoint(),list(c.world["calls"]),len(c.world["collectors"])
    c.s.config=replace(c.s.config,read_only=True)
    assert c.runtime.reconcile(dry_run=True)["would"]
    assert before==(c.store.catalog_checkpoint(),c.world["calls"],len(c.world["collectors"]))


def test_missing_settlement_can_later_reconcile_present_marker_without_reloading(catalog):
    c=catalog;c.world["fail_proof"]=True
    install(c)
    c.world["fail_proof"]=False
    result=c.runtime.reconcile()
    assert result["status"]=="reconciled" and not c.s.catalog_fenced
    assert c.world["calls"].count("adopt")==1
    assert c.store.catalog_checkpoint()["phase"]=="released"


def test_old_buffered_relay_events_keep_old_generation_provenance(catalog):
    from llmsvc.scheduler import DataPlaneBridge
    from test_core_event_bridge import BufferedRelay, model_frame
    c=catalog;relay=BufferedRelay()
    c.s.event_bridge=DataPlaneBridge(c.s,relay)
    relay.buffer.observe(model_frame())
    result,_=install(c)
    assert result["status"]=="applied" and relay.closes==1
    events=c.s.events_since(0)
    assert any(e.kind=="catalog_events_discarded" and e.detail["catalog_epoch"]=="0"*32 for e in events)
    assert not any(e.model=="m" for e in events)


def test_removed_account_keeps_observation_profile_without_readmission(catalog):
    c=catalog
    c.store.create_lease(Lease("old","base",0,.4,20000,40),"vllm-base.service")
    c.store.transition_lease("old","confirmed")
    c.world["units"]["base"]=UnitObservation(True,False,True,"old","2"*32)
    c.s.sample_once()
    c.models={"new":c.models["new"]}
    c.candidate=yaml.safe_dump({"macros":{"llmsvc_reload_generation":"gen_"+"1"*32},"models":{"new":{}}}).encode()
    c.binding=replace(c.binding,candidate_sha256=hashlib.sha256(c.candidate).hexdigest())
    result,_=install(c)
    assert result["status"]=="applied"
    assert "base" in c.s.collect.models and "base" not in c.s.placement.transport.active_models
    assert c.store.lease("old")[0].budget_gb==40
    status,body=request(c.address,"POST","/v1/place",{"model":"base","util":.4})
    assert status==404 and body["error"]=="unknown_model"
    c.s.sample_once()
    assert c.store.lease("old")[0].status=="confirmed"
    from llmsvc.actions import ActionDispatchError
    with pytest.raises(ActionDispatchError):
        c.s.model_actions.transport.stop_unit("vllm-base.service",deadline=10**12)


def test_new_pin_after_enqueue_blocks_removal_before_any_config_change(catalog):
    c=catalog;c.models={"new":c.models["new"]}
    c.candidate=yaml.safe_dump({"macros":{"llmsvc_reload_generation":"gen_"+"1"*32},"models":{"new":{}}}).encode()
    c.binding=replace(c.binding,candidate_sha256=hashlib.sha256(c.candidate).hexdigest())
    c.runtime.enqueue(prepare(c));before=c.path.read_bytes()
    c.store.put_pin(Pin("base",20000,"owner"))
    make_quiet(c.q.quiet,c.clock)
    result=c.runtime.process_once()
    assert result["status"]=="queued" and result["blocked_by"]==[{"reason":"catalog_removal_protected"}]
    assert c.path.read_bytes()==before and c.store.catalog_checkpoint() is None


def test_concurrent_cycle_cannot_retire_or_clear_another_cycle_owner(catalog,monkeypatch):
    c=catalog;entered=threading.Event();release=threading.Event();results=[]
    original=c.runtime.retire
    owner=[]
    def retire():
        if not owner: owner.append(threading.get_ident())
        assert threading.get_ident()==owner[0], "second cycle retired first cycle objects"
        entered.set();assert release.wait(3)
        original()
    monkeypatch.setattr(c.runtime,"retire",retire)
    c.runtime.enqueue(prepare(c));make_quiet(c.q.quiet,c.clock)
    t=threading.Thread(target=lambda:results.append(c.runtime.process_once()));t.start()
    try:
        assert entered.wait(3)
        with pytest.raises(ReloadError): c.runtime.process_once()
        assert c.runtime.busy and c.s.catalog_fenced
    finally:
        release.set();t.join(3)
    assert not t.is_alive() and results[0]["status"]=="applied"


def test_first_checkpoint_failure_is_atomic_and_precedes_config_effect(catalog):
    import sqlite3
    c=catalog;c.store.put_pin(Pin("base",20000,"owner"))
    c.runtime.enqueue(prepare(c));make_quiet(c.q.quiet,c.clock)
    before=c.path.read_bytes()
    c.store._db.set_authorizer(lambda action,table,*rest:
        sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and table=="llmsvc_catalog" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError): c.runtime.process_once()
    finally:
        c.store._db.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
    assert c.path.read_bytes()==before and "adopt" not in c.world["calls"]
    assert c.store._db.execute("PRAGMA user_version").fetchone()[0]==2
    assert c.store.catalog_checkpoint() is None and c.store.active(c.clock())[0]
    assert c.s.catalog_fenced


def test_late_proof_cannot_renew_the_operation_deadline(catalog):
    c=catalog;original=c.runtime.verifier
    def late(record,*,deadline):
        proof=original(record,deadline=deadline)
        c.clock.advance(c.q.operation_timeout)
        return proof
    c.runtime.verifier=late
    result,_=install(c)
    assert result["status"]=="reconciliation_required"
    assert c.s.catalog_fenced and c.store.catalog_checkpoint()["phase"]=="claimed"
    assert c.s.collect is c.collector


def test_uncommitted_abort_does_not_fence_static_restart(catalog,monkeypatch):
    c=catalog;c.runtime.enqueue(prepare(c));make_quiet(c.q.quiet,c.clock)
    monkeypatch.setattr(c.q,"validate",lambda path: (_ for _ in ()).throw(ValueError("fixture rejection")))
    result=c.runtime.process_once()
    assert result["status"]=="failed" and not result["config_committed"]
    assert c.store.catalog_checkpoint()["phase"]=="aborted" and not c.s.catalog_fenced
    restored=CatalogRuntime(c.s,c.q,verifier=c.verify,collector_factory=c.collector_factory,
        relay_factory=lambda config:None,transport_factory=c.transport_factory)
    assert not c.s.catalog_fenced and restored.epoch=="0"*32
    c.s.check_catalog()


def test_aborted_next_transaction_retains_last_released_restart_metadata(catalog,monkeypatch):
    c=catalog;install(c);released=c.store.catalog_checkpoint();epoch=c.runtime.epoch
    c.s.sample_once()
    c.candidate=c.candidate+b"# another candidate\n"
    c.binding=replace(c.binding,candidate_sha256=hashlib.sha256(c.candidate).hexdigest())
    c.runtime.enqueue(prepare(c))
    monkeypatch.setattr(c.q,"validate",lambda path: (_ for _ in ()).throw(ValueError("fixture rejection")))
    result=c.runtime.process_once()
    assert result["status"]=="failed" and c.store.catalog_checkpoint()["previous"]==released
    restored=CatalogRuntime(c.s,c.q,verifier=c.verify,collector_factory=c.collector_factory,
        relay_factory=lambda config:None,transport_factory=c.transport_factory)
    assert c.s.catalog_fenced and restored.epoch==epoch
    assert c.store.restore_catalog_abort(c.store.catalog_checkpoint())==released


def test_old_confirm_wait_cannot_cross_catalog_generation(catalog):
    from llmsvc.leases import LeaseError
    c=catalog
    c.store.create_lease(Lease("base-lease","base",0,.4,20000,40),"vllm-base.service")
    c.store.transition_lease("base-lease","confirmed")
    c.world["units"]["base"]=UnitObservation(True,False,True,"base-lease","1"*32)
    c.s.sample_once()
    old=c.s.placement;entered=threading.Event();release=threading.Event();results=[]
    def hold(): entered.set();assert release.wait(3)
    c.collector.hold=hold
    def finish():
        try: results.append(old.finish("confirm","base-lease"))
        except LeaseError as error: results.append(error.error)
    t=threading.Thread(target=finish);t.start()
    try:
        assert entered.wait(3)
        result,_=install(c)
        assert result["status"]=="applied"
    finally:
        release.set();t.join(3)
    assert results==["catalog_generation_changed"]
    assert c.store.lease("base-lease")[0].status=="confirmed"


def test_pending_start_prevents_catalog_commit(catalog):
    c=catalog
    c.store.create_lease(Lease("starting","base",0,.4,20000,40),"vllm-base.service")
    with pytest.raises(ReloadError,match="active operation"): c.runtime.enqueue(prepare(c))
    assert c.store.catalog_checkpoint() is None and "adopt" not in c.world["calls"]


@pytest.fixture
def registry_catalog(catalog,registry_fixture):
    from llmsvc.registry import ModelRegistry
    c=catalog;_,old_queue,weights,*_=registry_fixture
    c.path.write_bytes(old_queue.path.read_bytes())
    trusted={"base":profile("base",8101),"fine":profile("fine",8102)}
    c.runtime.profile_provider=lambda raw: {name:trusted[name] for name in ModelRegistry._decode(raw)[0]["models"]}
    c.runtime.instance_provider=lambda **kwargs: InstanceIdentity(123,"456")
    api=ModelRegistry(c.q,shared_roots=(weights.parent,),daemon_port_range=(8101,8110),now=c.clock,
        stop_model=lambda *a,**kw:pytest.fail("fixture removed model was already absent"),
        unit_absent=lambda name,**kw:not c.world["units"].get(name,UnitObservation(False,True)).exists)
    c.s.registry=api;c.runtime.connect_registry(api)
    return c,api,weights


def test_copied_cli_submits_real_registry_change_through_catalog_worker(registry_catalog,tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    c,api,weights=registry_catalog
    script=tmp_path/"llm-copy";script.write_bytes(Path("cli/llm").read_bytes())
    result=subprocess.run([sys.executable,"-I","-S",str(script),"--url","http://%s:%s"%c.address,
        "add",str(weights),"--name","fine","--base","base","--json"],capture_output=True,text=True,timeout=5)
    assert result.returncode==1 and not result.stderr  # Existing CLI treats queued as incomplete, not applied.
    queued=json.loads(result.stdout)
    assert queued["status"]=="queued" and queued["config_committed"] is False
    assert queued["description"]=={"kind":"add_model","model":"fine","base":"base"}
    installed=threading.Event();emit=c.s.emit
    def record(kind,**kwargs):
        event=emit(kind,**kwargs)
        if kind=="catalog_installed": installed.set()
        return event
    c.s.emit=record;make_quiet(c.q.quiet,c.clock);c.s.start()
    assert installed.wait(3)
    assert c.q.get(queued["id"])["status"]=="applied" and "fine" in api.records()
    c.s.sample_once()
    status,placed=request(c.address,"POST","/v1/place",{"model":"fine","util":.3})
    assert status==200 and c.store.lease(placed["lease_id"])[0].budget_gb==40
    status,listed=request(c.address,"GET","/v1/models")
    assert status==200 and listed["writes_enabled"] is True


def test_registry_remove_keeps_both_late_precheck_and_absent_cleanup(registry_catalog):
    c,api,weights=registry_catalog
    status,job=request(c.address,"POST","/v1/models",{"name":"fine","path":str(weights),"base":"base"})
    assert status==200,job
    make_quiet(c.q.quiet,c.clock);assert c.runtime.process_once()["status"]=="applied"
    c.s.sample_once()
    status,job=request(c.address,"DELETE","/v1/models/fine")
    assert status==200,job
    c.store.put_pin(Pin("fine",20000,"owner"))
    result=c.runtime.process_once()
    assert result["status"]=="queued" and result["blocked_by"]
    c.store.remove_pin("fine")
    result=c.runtime.process_once()
    assert result["status"]=="applied" and "fine" not in api.records()
    assert "fine" in c.s.collect.models and "fine" not in c.s.placement.transport.active_models


def test_temporary_metadata_port_mismatch_cannot_enter_runtime(registry_catalog):
    c,api,weights=registry_catalog
    c.runtime.profile_provider=lambda raw: {"base":profile("base",8101),"fine":profile("fine",8103)}
    before=c.path.read_bytes()
    status,body=request(c.address,"POST","/v1/models",{"name":"fine","path":str(weights),"base":"base"})
    assert status==503 and body["error"]=="registry_unavailable"
    assert c.path.read_bytes()==before and c.store.catalog_checkpoint() is None and not c.q._pending
