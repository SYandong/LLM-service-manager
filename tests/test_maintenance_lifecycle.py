# Generated-By: Codex / gpt-6-astra
"""Real queue/config/SQLite/HTTP with only owned proxy/helper subprocesses.

Process grouping, profiles and request counts here are explicit test fixtures;
this does not establish a production exclusion or helper-attribution source.
"""

import hashlib
import http.client
import json
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
open(sys.argv[3],'w').write(json.dumps({'port':server.server_port,**info}))
server.serve_forever()
'''


def ticks(pid):
    raw=Path('/proc/'+str(pid)+'/stat').read_text()
    return raw[raw.rfind(')')+2:].split()[19]


class ProcessBackend:
    def __init__(self,c,root):
        self.c=c;self.root=root;self.processes=[];self.helpers=[];self.calls=[];self.port=0
        self.helper_mode='none';self.start_failure=False;self.bad_new_identity=False
        self.script=root/'proxy.py';self.script.write_text(SERVICE)
        self.old,self.old_identity,self.old_scope=self.start('initial')
        self.new=None;self.new_identity=None;self.new_scope=None
        self.current=self.old

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
        identity_now=self.old_identity if self.current is self.old else self.new_identity
        base={'transaction_id':context.get('transaction_id'),'observed_at':time.monotonic()}
        if operation in ('inspect','preflight'):
            observed=self.probe()
            assert observed['pid']==identity_now['pid']
            scope=self.old_scope if self.current is self.old else self.new_scope
            return {**base,'identity':identity_now,'scope':scope,'actors':[identity_now],
                    'backend_bindings':[],'exclusion_method':'stop_instance',
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
            return {**base,'observed_at':time.monotonic(),'accepted':True,'identity':self.new_identity}
        if operation=='observe_candidate':
            observed=self.probe()
            return {**base,'observed_at':time.monotonic(),
                    'identity':self.old_identity if self.bad_new_identity else self.new_identity,
                    'old_identity':self.old_identity,'old_settled':self.old.poll() is not None,
                    'helpers_settled':all(p.poll()==0 for p in self.helpers),'ingress_state':'open',
                    'config_sha256':observed['config_sha256'],'generation':observed['generation'],
                    'backends_confirmed':True,'cleanup_confirmed':True,
                    'attempt_bound':observed['transaction_id']==context['transaction_id'],
                    'operation_id':context['operation_id']}
        raise AssertionError('unimplemented fixture operation '+operation)

    def close(self):
        for process in self.processes+self.helpers:
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
