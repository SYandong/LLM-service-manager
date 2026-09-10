# Generated-By: Codex / gpt-6-astra
"""Real queue/config/SQLite/HTTP with only owned proxy/helper subprocesses.

Process grouping, profiles and request counts here are explicit test fixtures;
this does not establish a production exclusion or helper-attribution source.
"""

import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from llmsvc.catalog import CatalogRuntime
from llmsvc.maintenance import MaintenanceController, MaintenanceError, fingerprint
from llmsvc.reload import ReloadError
from llmsvc.reload_witness import InstanceIdentity
from test_catalog_lifecycle import catalog
from test_registry_http_preview import request


SERVICE = '''import hashlib,json,os,sys,time,yaml
from http.server import BaseHTTPRequestHandler,HTTPServer
raw=open(sys.argv[1],'rb').read()
config=yaml.safe_load(raw)
info={'pid':os.getpid(),'config_sha256':hashlib.sha256(raw).hexdigest(),
      'generation':config.get('macros',{}).get('llmsvc_reload_generation'), 'transaction_id':sys.argv[4]}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  body=json.dumps(info).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=HTTPServer(('127.0.0.1',int(sys.argv[2])),Handler)
with open(sys.argv[3]+'.tmp','w') as ready: ready.write(json.dumps({'port':server.server_port,**info}))
os.replace(sys.argv[3]+'.tmp',sys.argv[3])
server.serve_forever()
'''


def ticks(pid):
    raw=Path('/proc/'+str(pid)+'/stat').read_text()
    return raw[raw.rfind(')')+2:].split()[19]


class ProcessBackend:
    def __init__(self,c,root):
        self.c=c;self.root=root;self.processes=[];self.helpers=[];self.calls=[];self.port=0
        self.helper_mode='none';self.start_failure=False;self.bad_new_identity=False
        self.model_processes={};self.after_start=None
        self.script=root/'proxy.py';self.script.write_text(SERVICE)
        try:
            self.old,self.old_identity,self.old_scope=self.start('initial')
        except BaseException:
            self.close()
            raise
        self.new=None;self.new_identity=None;self.new_scope=None
        self.current=self.old
        self.restored=None;self.restored_identity=None;self.restored_scope=None

    def start(self,tx):
        number=len(self.processes)
        ready=self.root/('ready-'+str(number)+'.json')
        process=subprocess.Popen([sys.executable,str(self.script),str(self.c.path),str(self.port),str(ready),tx],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline=time.monotonic()+4
        while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.01)
        assert ready.exists(), 'owned fixture proxy did not start'
        data=json.loads(ready.read_text());self.port=data['port']
        scope={'unit':'fixture-proxy','incarnation':str(number),'fixture':True}
        identity={'pid':process.pid,'start_ticks':ticks(process.pid),'scope_sha256':fingerprint(scope)}
        return process,identity,scope

    def probe(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        try:
            connection.request('GET','/config');response=connection.getresponse()
            assert response.status==200
            return json.loads(response.read())
        finally: connection.close()

    def request(self,operation,context,*,deadline):
        self.calls.append(operation)
        c=self.c
        identity_now=(self.old_identity if self.current is self.old else
                      self.restored_identity if self.current is self.restored else self.new_identity)
        base={'transaction_id':context.get('transaction_id'),'observed_at':time.monotonic()}
        if operation in ('inspect','preflight'):
            observed=self.probe()
            assert observed['pid']==identity_now['pid']
            scope=(self.old_scope if self.current is self.old else
                   self.restored_scope if self.current is self.restored else self.new_scope)
            bindings=[]
            for lease,unit in context.get('accounts',[]):
                process=self.model_processes.get(lease['model'])
                observation=c.world['units'].get(lease['model'])
                if process is None or process.poll() is not None or observation is None:
                    continue
                environment=Path('/proc/'+str(process.pid)+'/environ').read_bytes().split(b'\0')
                assert ('LLMSVC_LEASE_ID='+lease['lease_id']).encode() in environment
                bindings.append({'model':lease['model'],'unit':unit,'lease_id':lease['lease_id'],
                                 'gpu':lease['gpu'],'invocation_id':observation.invocation_id})
            return {**base,'identity':identity_now,'scope':scope,'actors':[identity_now],
                    'backend_bindings':bindings,'exclusion_method':'stop_instance',
                    'config_sha256':observed['config_sha256'],'configuration_confirmed':True,
                    'ready':True,'actors_known':True,'in_flight':0,'ingress_state':'open'}
        if operation=='validate':
            raw=Path(context['candidate_path']).read_bytes()
            return {**base,'accepted':hashlib.sha256(raw).hexdigest()==context['candidate_sha256'] and isinstance(yaml.safe_load(raw),dict)}
        checkpoint=c.store.maintenance_checkpoint(context['transaction_id'])
        assert checkpoint is not None and c.s.catalog_fenced
        if operation=='stop_old':
            assert checkpoint['effects']['stop_old']['submitted'] and context['old_identity']==self.old_identity
            assert hashlib.sha256(c.path.read_bytes()).hexdigest()==context['base_sha256']
            self.old.terminate();self.old.wait(timeout=3)
            if self.helper_mode!='none':
                delay='.12' if self.helper_mode=='delayed' else '0'
                code='1' if self.helper_mode=='failed' else '0'
                self.helpers.append(subprocess.Popen([sys.executable,'-c',f'import time;time.sleep({delay});raise SystemExit({code})']))
            return {**base,'observed_at':time.monotonic(),'accepted':True}
        if operation=='observe_old':
            return {**base,'identity':None if self.old.poll() is not None else self.old_identity,
                    'old_identity':self.old_identity,'old_settled':self.old.poll() is not None,
                    'helpers_settled':all(p.poll()==0 for p in self.helpers),
                    'backends_confirmed':True,'ingress_state':'excluded'}
        if operation=='start_candidate':
            assert self.old.poll() is not None and all(p.poll()==0 for p in self.helpers)
            assert checkpoint['effects']['start_candidate']['submitted']
            assert hashlib.sha256(c.path.read_bytes()).hexdigest()==context['candidate_sha256']
            if self.start_failure:
                return {**base,'accepted':False}
            self.new,self.new_identity,self.new_scope=self.start(context['transaction_id']);self.current=self.new
            if self.after_start: self.after_start()
            return {**base,'observed_at':time.monotonic(),'accepted':True,'identity':self.new_identity}
        if operation=='stop_model':
            from llmsvc.leases import UnitObservation
            name=context['model'];process=self.model_processes[name]
            assert name in context['removed_models'] and checkpoint['effects']['stop_model:'+name]['submitted']
            observation=c.world['units'][name]
            assert observation.lease_id==context['lease_id'] and observation.invocation_id==context['invocation_id']
            process.terminate();process.wait(timeout=3)
            c.world['units'][name]=UnitObservation(False,True)
            c.s.sample_once()
            return {**base,'observed_at':time.monotonic(),'accepted':True}
        if operation=='stop_candidate':
            assert self.new is not None and context['new_identity']==self.new_identity
            assert checkpoint['effects']['stop_candidate']['submitted']
            self.new.terminate();self.new.wait(timeout=3)
            return {**base,'observed_at':time.monotonic(),'accepted':True}
        if operation=='observe_candidate_absent':
            absent=self.new is None or self.new.poll() is not None
            return {**base,'identity':None if absent else self.new_identity,
                'old_identity':self.old_identity,'old_settled':self.old.poll() is not None,
                'helpers_settled':all(p.poll()==0 for p in self.helpers),
                'attempt_identity':self.new_identity,'attempt_settled':absent,'attempt_bound':True,
                'operation_id':context['operation_id'],'backends_confirmed':True,
                'ingress_state':'excluded' if absent else 'open'}
        if operation=='start_base':
            assert self.old.poll() is not None and (self.new is None or self.new.poll() is not None)
            assert hashlib.sha256(c.path.read_bytes()).hexdigest()==context['base_sha256']
            assert checkpoint['effects']['start_base']['submitted']
            self.restored,self.restored_identity,self.restored_scope=self.start(context['transaction_id']);self.current=self.restored
            return {**base,'observed_at':time.monotonic(),'accepted':True,'identity':self.restored_identity}
        if operation=='observe_base':
            observed=self.probe()
            return {**base,'observed_at':time.monotonic(),'identity':self.restored_identity,
                'old_identity':self.old_identity,'old_settled':self.old.poll() is not None,
                'helpers_settled':all(p.poll()==0 for p in self.helpers),
                'attempt_identity':self.new_identity,'attempt_settled':self.new is None or self.new.poll() is not None,
                'ingress_state':'open','backends_confirmed':True,'cleanup_confirmed':True,'configuration_confirmed':True,
                'config_sha256':observed['config_sha256'],'generation':observed['generation'],
                'attempt_bound':observed['transaction_id']==context['transaction_id'],'operation_id':context['operation_id']}
        if operation=='observe_candidate':
            observed=self.probe()
            return {**base,'observed_at':time.monotonic(),
                    'identity':self.old_identity if self.bad_new_identity else self.new_identity,
                    'old_identity':self.old_identity,'old_settled':self.old.poll() is not None,
                    'helpers_settled':all(p.poll()==0 for p in self.helpers),'ingress_state':'open',
                    'config_sha256':observed['config_sha256'],'generation':observed['generation'],'configuration_confirmed':True,
                    'backends_confirmed':True,'cleanup_confirmed':True,
                    'attempt_bound':observed['transaction_id']==context['transaction_id'],
                    'operation_id':context['operation_id']}
        raise AssertionError('unimplemented fixture operation '+operation)

    def close(self):
        for process in self.processes+self.helpers+list(self.model_processes.values()):
            if process.poll() is None: process.terminate()
            process.wait(timeout=3)


@pytest.fixture
def maintenance(catalog,tmp_path):
    c=catalog;c.q.clock=time.monotonic
    backend=ProcessBackend(c,tmp_path)
    c.s.config=replace(c.s.config,catalog_mode='maintenance',catalog_profiles=c.models,
        maintenance_command=[sys.executable,'fixture'],action_poll_seconds=.01,
        collectors={**c.s.config.collectors,'swap_url':'http://127.0.0.1:'+str(backend.port)})
    controller=MaintenanceController(c.s,c.q,backend)
    c.q.validate=controller.validate
    runtime=CatalogRuntime(c.s,c.q,transition=controller,verifier=controller.verify,
        profile_provider=lambda raw:c.models,instance_provider=controller.inspect_instance,
        collector_factory=c.collector_factory,relay_factory=lambda cfg:None,transport_factory=c.transport_factory)
    c.runtime=runtime;c.controller=controller;c.backend=backend
    c.binding=replace(c.binding,endpoint='http://127.0.0.1:'+str(backend.port)+'/api/mcp',
                      instance=InstanceIdentity(backend.old.pid,ticks(backend.old.pid)))
    try: yield c
    finally: backend.close()


def enqueue(c):
    prepared=c.runtime.prepare(c.candidate,c.models,binding=c.binding)
    return c.runtime.enqueue(prepared)


def test_real_old_new_process_transition_precedes_catalog_admission(maintenance):
    c=maintenance;job=enqueue(c)
    assert c.q.quiet.blockers()==[{'reason':'inflight_stream_unknown'}]
    result=c.runtime.process_once()
    assert result['status']=='applied',result
    assert c.backend.old.poll() is not None and c.backend.new.poll() is None
    assert c.backend.new.pid!=c.backend.old.pid
    assert not c.s.catalog_fenced and not c.store.catalog_pending()
    assert c.store._db.execute('PRAGMA user_version').fetchone()[0]==6
    record=c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    assert record['stage']=='released' and record['old_identity']!=record['new_identity']
    assert c.q.quiet.blockers()==[{'reason':'inflight_stream_unknown'}]  # No manufactured quiet.
    c.s.sample_once()
    status,placed=request(c.address,'POST','/v1/place',{'model':'new','util':.2})
    assert status==200 and c.store.lease(placed['lease_id'])[0].budget_gb==40
    assert c.backend.calls.count('stop_old')==1 and c.backend.calls.count('start_candidate')==1


def test_delayed_helper_must_finish_before_configuration_replace(maintenance):
    c=maintenance;c.backend.helper_mode='delayed';enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='applied'
    assert c.backend.calls.count('observe_old')>1 and all(p.poll()==0 for p in c.backend.helpers)


def test_failed_helper_retains_old_config_and_durable_claim(maintenance):
    c=maintenance;c.backend.helper_mode='failed';c.q.operation_timeout=.5
    original=c.path.read_bytes();enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required' and result['config_committed'] is False
    assert c.path.read_bytes()==original and c.s.catalog_fenced and c.store.catalog_pending()
    assert 'start_candidate' not in c.backend.calls
    assert c.q.fenced and c.backend.calls.count('stop_old')==1


def test_failed_candidate_start_is_not_replayed(maintenance):
    c=maintenance;c.backend.start_failure=True;enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required' and result['config_committed'] is True
    assert c.s.catalog_fenced and c.store.catalog_pending()
    before=list(c.backend.calls)
    with pytest.raises((MaintenanceError,ReloadError)):
        c.runtime.process_once()
    assert c.backend.calls==before


def test_explicit_rollback_after_failed_start_restores_exact_base_without_ledger_restore(maintenance):
    c=maintenance;c.backend.start_failure=True
    original=c.path.read_bytes();enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required'
    rolled=c.controller.rollback()
    assert rolled['status']=='rolled_back' and c.path.read_bytes()==original
    assert c.backend.restored.poll() is None and c.backend.restored.pid!=c.backend.old.pid
    assert c.store.catalog_checkpoint()['phase']=='rolled_back' and not c.s.catalog_fenced
    assert not c.store.catalog_pending() and c.store.leases()==()
    assert 'new' not in c.runtime.manifest['active']
    assert c.backend.calls.count('start_candidate')==1 and c.backend.calls.count('start_base')==1
    assert c.controller.reconcile()['status']=='rolled_back'
    assert c.backend.calls.count('start_base')==1


def test_rollback_settles_the_started_candidate_before_restoring_base(maintenance):
    c=maintenance;c.backend.bad_new_identity=True;c.q.operation_timeout=.8
    original=c.path.read_bytes();enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required' and c.backend.new.poll() is None
    c.backend.bad_new_identity=False;c.q.operation_timeout=5
    assert c.controller.rollback()['status']=='rolled_back'
    assert c.backend.new.poll() is not None and c.backend.restored.poll() is None
    assert c.path.read_bytes()==original and c.backend.calls.count('stop_candidate')==1
    assert c.backend.calls.index('stop_candidate') < c.backend.calls.index('start_base')


def test_failed_helper_cannot_be_hidden_by_rollback(maintenance):
    c=maintenance;c.backend.helper_mode='failed';c.q.operation_timeout=.4
    original=c.path.read_bytes();enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    with pytest.raises(MaintenanceError): c.controller.rollback()
    assert c.path.read_bytes()==original and c.s.catalog_fenced and c.store.catalog_pending()
    assert 'start_base' not in c.backend.calls


def test_restart_reconciles_known_transition_without_stop_or_start_replay(maintenance,monkeypatch):
    c=maintenance;save=c.store.save_catalog
    def fail_release(expected,record,**kwargs):
        if record['phase']=='released': raise OSError('fixture final checkpoint unavailable')
        return save(expected,record,**kwargs)
    monkeypatch.setattr(c.store,'save_catalog',fail_release);enqueue(c)
    with pytest.raises(OSError): c.runtime.process_once()
    assert c.s.catalog_fenced and c.backend.new.poll() is None
    monkeypatch.setattr(c.store,'save_catalog',save)
    from llmsvc.store import IntentStore
    c.store.close();c.store=IntentStore(c.cfg.state_db_path,action_lock=c.s.action_lock);c.s.store=c.store
    controller=MaintenanceController(c.s,c.q,c.backend)
    runtime=CatalogRuntime(c.s,c.q,transition=controller,verifier=controller.verify,
        profile_provider=lambda raw:c.models,instance_provider=controller.inspect_instance,
        collector_factory=c.collector_factory,relay_factory=lambda cfg:None,transport_factory=c.transport_factory)
    c.controller=controller;c.runtime=runtime
    assert c.s.catalog_fenced
    assert runtime.reconcile()['status']=='reconciled'
    assert not c.s.catalog_fenced and c.backend.calls.count('stop_old')==1 and c.backend.calls.count('start_candidate')==1


def test_maintenance_preview_and_readonly_have_no_adapter_or_store_effect(maintenance):
    c=maintenance;before=c.path.read_bytes(),c.store._db.execute('PRAGMA user_version').fetchone()[0]
    calls=list(c.backend.calls)
    prepared=c.runtime.prepare(c.candidate,c.models,binding=c.binding)
    assert c.runtime.enqueue(prepared,dry_run=True)['would']
    assert c.controller.rollback(dry_run=True)['would']
    assert c.backend.calls==calls and before==(c.path.read_bytes(),c.store._db.execute('PRAGMA user_version').fetchone()[0])
    c.s.config=replace(c.s.config,read_only=True)
    with pytest.raises(ReloadError): c.runtime.enqueue(prepared)
    assert c.backend.calls==calls and c.store.catalog_checkpoint() is None


def test_old_scope_change_before_stop_never_signals_replacement(maintenance):
    c=maintenance;enqueue(c)
    original=c.backend.request
    def changed(operation,context,*,deadline):
        result=original(operation,context,deadline=deadline)
        if operation=='inspect' and c.store.catalog_checkpoint() is not None:
            result['identity']={**result['identity'],'scope_sha256':'f'*64}
        return result
    c.backend.request=changed
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required'
    assert c.backend.old.poll() is None and 'stop_old' not in c.backend.calls
    assert c.s.catalog_fenced
    assert c.controller.reconcile()=={'status':'aborted','native_effects':False}
    assert not c.s.catalog_fenced and not c.store.catalog_pending()
    assert c.backend.old.poll() is None and 'stop_old' not in c.backend.calls


def test_bootstrap_mounts_configured_controller_without_source_calls(maintenance,monkeypatch):
    import llmsvc.__main__ as entry
    from llmsvc.registry import ModelRegistry
    c=maintenance;c.s.registry=ModelRegistry(c.q,shared_roots=(),daemon_port_range=(21000,21010))
    before=list(c.backend.calls)
    monkeypatch.setattr('llmsvc.maintenance.CommandBackend',lambda *args,**kwargs:c.backend)
    monkeypatch.setattr(entry,'build_collector',c.collector_factory)
    monkeypatch.setattr(entry,'build_event_relay',lambda cfg:None)
    runtime=entry.build_catalog(c.s.config,c.s)
    assert runtime.can_submit() and c.s.registry.submit_change==runtime.submit_change
    assert c.backend.calls==before
    c.runtime=runtime;c.controller=runtime.transition
    enqueue(c)
    assert runtime.process_once()['status']=='applied'


def test_preflight_must_match_the_old_loaded_configuration(maintenance):
    c=maintenance;original=c.backend.request
    def wrong_config(operation,context,*,deadline):
        result=original(operation,context,deadline=deadline)
        if operation=='preflight': result['config_sha256']='0'*64
        return result
    c.backend.request=wrong_config;enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='queued' and result['blocked_by']
    assert 'stop_old' not in c.backend.calls and c.store.catalog_checkpoint() is None


def add_owned_model(c):
    from llmsvc.state import Lease
    from llmsvc.leases import UnitObservation
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
        env={'LLMSVC_LEASE_ID':'unit-lease'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    c.backend.model_processes['base']=process
    end=time.monotonic()+3
    while b'LLMSVC_LEASE_ID=unit-lease' not in Path('/proc/'+str(process.pid)+'/environ').read_bytes() and time.monotonic()<end:
        time.sleep(.01)
    invocation=hashlib.sha256((str(process.pid)+':'+ticks(process.pid)).encode()).hexdigest()[:32]
    c.world['units']['base']=UnitObservation(True,False,True,'unit-lease',invocation)
    c.store.create_lease(Lease('unit-lease','base',0,.4,20000,40),'vllm-base.service')
    c.store.transition_lease('unit-lease','confirmed');c.s.sample_once()
    candidate=yaml.safe_dump({'macros':{'llmsvc_reload_generation':'gen_'+'1'*32},'models':{'new':{}}}).encode()
    binding=replace(c.binding,candidate_sha256=hashlib.sha256(candidate).hexdigest())
    prepared=c.runtime.prepare(candidate,{'new':c.models['new']},binding=binding)
    return process,prepared


def test_running_removed_model_uses_guarded_stop_and_observed_account_release(maintenance):
    c=maintenance;process,prepared=add_owned_model(c)
    c.runtime.enqueue(prepared,cleanup=lambda **kw:c.controller.stop_model('base',**kw))
    result=c.runtime.process_once()
    assert result['status']=='applied',result
    assert process.poll() is not None and c.store.lease('unit-lease')[0].status=='released'
    assert c.backend.calls.count('stop_model')==1
    assert c.backend.calls.index('start_candidate')<c.backend.calls.index('stop_model')


def test_late_pin_blocks_running_target_cleanup(maintenance):
    from llmsvc.state import Pin
    c=maintenance;process,prepared=add_owned_model(c)
    c.backend.after_start=lambda:c.store.put_pin(Pin('base',20000,'fixture-owner'))
    c.runtime.enqueue(prepared,cleanup=lambda **kw:c.controller.stop_model('base',**kw))
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required'
    assert process.poll() is None and 'stop_model' not in c.backend.calls
    assert c.store.lease('unit-lease')[0].status=='confirmed' and c.store.lease('unit-lease')[0].budget_gb==40
    assert c.s.catalog_fenced
