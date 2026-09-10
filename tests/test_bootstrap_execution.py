# Generated-By: Codex / gpt-6-astra
"""Real bootstrap HTTP/launcher/health/SQLite, owned CPU units and migration fixtures.

GPU/systemd/native-source facts are explicitly simulated. No site or model work.
"""
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport
from llmsvc.bootstrap import BootstrapController, BootstrapError
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, StateSnapshot
from llmsvc.store import IntentStore

BACKEND='''import json,os,socket
from http.server import HTTPServer,BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  body=b'{"is_sleeping": false}' if self.path=='/is_sleeping' else b'{}'
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=HTTPServer(('127.0.0.1',0),Handler,bind_and_activate=False)
server.socket.close();server.socket=socket.socket(fileno=int(os.environ['FIXTURE_LISTEN_FD']))
server.server_address=server.socket.getsockname();server.serve_forever()
'''


def ticks(pid):
    raw=Path('/proc/'+str(pid)+'/stat').read_text()
    return raw[raw.rfind(')')+2:].split()[19]


@pytest.fixture
def bootstrap_service(tmp_path):
    children=[];world={'unit':None,'lease':None,'source':None,'calls':[]}
    old=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);children.append(old)
    source_id=lambda p:{'pid':p.pid,'start_ticks':ticks(p.pid),'scope_sha256':hashlib.sha256(str(p.pid).encode()).hexdigest()}
    old_id=source_id(old)
    reserved=socket.socket();reserved.bind(('127.0.0.1',0));reserved.listen()
    backend_port=reserved.getsockname()[1]
    backend_script=tmp_path/'backend.py';backend_script.write_text(BACKEND)
    source=tmp_path/'source.yaml';base=b'models: {default: {}}\nhooks: {on_startup: {preload: [default]}}\n'
    target=base+b'# reviewed managed launcher fixture\n';source.write_bytes(base)
    store=IntentStore(tmp_path/'state.sqlite',action_lock=threading.RLock())
    metadata={'unit':'vllm-default.service','port':backend_port,'daemon_url':'http://127.0.0.1:'+str(backend_port),
              'util':.4,'budget_gb':40,'weights_gb':10,'is_default':True}
    def healthy():
        if world['unit'] is None or world['unit'].poll() is not None:return False
        connection=http.client.HTTPConnection('127.0.0.1',backend_port,timeout=.5)
        try:
            connection.request('GET','/health');response=connection.getresponse();response.read();return response.status==200
        except OSError:return False
        finally:connection.close()
    def collect():
        active=world['unit'] is not None and world['unit'].poll() is None
        health=healthy() if active else None
        gpu=None
        if health:
            environment=Path('/proc/'+str(world['unit'].pid)+'/environ').read_bytes().split(b'\0')
            gpu=int(next(value.split(b'=',1)[1] for value in environment if value.startswith(b'CUDA_VISIBLE_DEVICES=')))
        excluded=old.poll() is not None and world['source'] is None
        return StateSnapshot(sampled_at=time.time(),models=(ModelState('default',
            state='awake' if health else 'unknown' if active else 'stopped',gpu=gpu,
            unit=metadata['unit'],unit_active=active,health_ok=health,is_sleeping=False if health else None,
            swap_state=None if excluded else 'stopped',util=.4,budget_gb=40,weights_gb=10,is_default=True),),
            gpus=(GPUState(0,total_gb=100,free_gb=100,external_gb=0),),memory=MemoryState(500,0),
            activity=(Activity('default',in_flight=None),),
            errors=('running: URLError','events: URLError') if excluded else ())
    cfg=SchedulerConfig('127.0.0.1',8011,read_only=False,state_db_path=str(tmp_path/'state.sqlite'),
        model_actions_enabled=True,placement_enabled=True,catalog_enabled=True,catalog_mode='maintenance',
        bootstrap_enabled=True,collectors={'swap_url':'http://127.0.0.1:1','models':{'default':metadata}},
        registry={'config_path':str(source),'shared_roots':[str(tmp_path)],'daemon_port_range':[21000,21100]})
    scheduler=Scheduler(cfg,collect=collect,store=store)
    transport=ManagedModelTransport(swap_url='http://127.0.0.1:1',models={'default':metadata},systemctl='unused')
    def probe(name,**kwargs):
        process=world['unit']
        if process is None or process.poll() is not None:return UnitObservation(False,True)
        env=Path('/proc/'+str(process.pid)+'/environ').read_bytes().split(b'\0')
        lease=next(v.split(b'=',1)[1].decode() for v in env if v.startswith(b'LLMSVC_LEASE_ID='))
        return UnitObservation(True,False,True,lease,hashlib.sha256((str(process.pid)+ticks(process.pid)).encode()).hexdigest()[:32])
    scheduler.placement=PlacementController(scheduler,transport,probe=probe)
    server=SchedulerHTTPServer(('127.0.0.1',0),scheduler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01));thread.start()
    launcher=Path(__file__).resolve().parents[1]/'deploy/vllm-launch'
    launcher_cfg=tmp_path/'launcher.json';launcher_cfg.write_text(json.dumps({
        'scheduler_url':'http://127.0.0.1:'+str(server.server_port),'lock_dir':str(tmp_path/'locks'),
        'health_poll_seconds':.01,'startup_timeout_seconds':5,'request_timeout_seconds':5,
        'systemd_run':{'environment_file':None}}))
    spec={'model':'default','util':.4,'command':[sys.executable,str(backend_script),'--port',str(backend_port),'--gpu-memory-utilization','.4'],
          'launcher_path':str(launcher),'launcher_sha256':hashlib.sha256(launcher.read_bytes()).hexdigest(),
          'launcher_config_path':str(launcher_cfg),'launcher_config_sha256':hashlib.sha256(launcher_cfg.read_bytes()).hexdigest(),
          'migration_command':[sys.executable,'fixture'],'manifest_sha256':'a'*64,
          'base_config_sha256':hashlib.sha256(base).hexdigest(),'target_config_sha256':hashlib.sha256(target).hexdigest(),
          'timeout_seconds':15}
    scheduler.config=replace(cfg,listen_port=server.server_port,bootstrap=spec)
    class Migration:
        def request(self,operation,context,*,deadline):
            world['calls'].append(operation)
            result={'transaction_id':context['transaction_id'],'manifest_sha256':'a'*64,
                    'default_model':'default','default_unit':'vllm-default.service','source_origin':context['source_origin'],'default_preload_preserved':True,'observed_at':time.monotonic()}
            if operation=='bootstrap_preflight':
                return {**result,'identity':old_id,'source_config_sha256':spec['base_config_sha256'],
                        'legacy_backends_absent':world['unit'] is None,'ready':True,'preload':['default'],'in_flight':0}
            record=store.bootstrap_checkpoint();assert record is not None and record['id']==context['transaction_id']
            if operation=='bootstrap_stage':
                assert record['effects']['stage']=='submitted'
                old.terminate();old.wait(timeout=3);source.write_bytes(target)
            if operation=='bootstrap_activate':
                assert record['effects']['activate']=='submitted'
                assert store.lease(record['lease_id'])[0].status=='confirmed'
                world['source']=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);children.append(world['source'])
            if operation=='bootstrap_rollback':
                assert context['account'] is None and context['launch_submitted'] is False
                assert world['unit'] is None
                source.write_bytes(base);result['rolled_back']=True
            result.update(accepted=True,staged=source.read_bytes()==target,source_absent=old.poll() is not None and world['source'] is None,helpers_settled=old.poll() is not None,legacy_backends_absent=world['unit'] is None,
                source_config_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),launcher_sha256=spec['launcher_sha256'],
                launcher_config_sha256=spec['launcher_config_sha256'],observed_at=time.monotonic())
            if world['source'] is not None:
                result.update(active_ready=True,identity=source_id(world['source']),default_confirmed=True)
            return result
    controller=BootstrapController(scheduler,backend=Migration())
    load=controller._launcher
    def load_controlled():
        module=load()
        def command(argv,timeout=None):
            if argv[0]=='systemd-run':
                assert store.bootstrap_checkpoint()['effects']['launch']=='submitted'
                world['calls'].append('systemd-run')
                env=os.environ.copy()
                for arg in argv:
                    if arg.startswith('--setenv='):
                        key,value=arg[len('--setenv='):].split('=',1);env[key]=value
                if world.get('wrong_gpu') is not None:env['CUDA_VISIBLE_DEVICES']=str(world['wrong_gpu'])
                env['FIXTURE_LISTEN_FD']=str(reserved.fileno())
                process=subprocess.Popen(argv[argv.index('--')+1:],env=env,pass_fds=[reserved.fileno()],
                                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                world['unit']=process;children.append(process)
                return subprocess.CompletedProcess(argv,0,'','')
            if argv[:2]==['systemctl','is-active']:
                value='active' if world['unit'] is not None and world['unit'].poll() is None else 'inactive'
                return subprocess.CompletedProcess(argv,0,value+'\n','')
            if argv[:2]==['systemctl','show']:
                active=world['unit'] is not None and world['unit'].poll() is None
                return subprocess.CompletedProcess(argv,0,('loaded' if active else 'not-found')+'\n','')
            raise AssertionError(argv)
        module.run_checked=command
        return module
    controller._launcher=load_controlled
    try:yield SimpleNamespace(s=scheduler,store=store,c=controller,world=world,source=source,base=base,spec=spec,address=server.server_address)
    finally:
        server.shutdown();server.server_close();thread.join(3);scheduler.stop();store.close();reserved.close()
        for process in children:
            if process.poll() is None:process.terminate()
            process.wait(timeout=3)


def test_real_place_launch_health_confirm_creates_first_managed_default(bootstrap_service):
    c=bootstrap_service
    result=c.c.run()
    assert result['stage']=='complete'
    lease,unit=c.store.lease(result['lease_id'])
    assert lease.status=='confirmed' and lease.model=='default' and lease.budget_gb==40 and lease.gpu==0
    assert unit=='vllm-default.service' and not c.store.bootstrap_pending()
    assert c.world['calls'].count('systemd-run')==1
    assert c.world['calls'].index('bootstrap_stage')<c.world['calls'].index('systemd-run')<c.world['calls'].index('bootstrap_activate')
    assert c.s.config.collectors['models']['default']['is_default'] is True
    assert 'preload' in c.source.read_text()


def test_bootstrap_dryrun_never_calls_launcher_migration_or_store_writer(bootstrap_service):
    c=bootstrap_service;before=c.store._db.iterdump();before=list(before)
    assert c.c.run(dry_run=True)['would']==[{'kind':'bootstrap_default','model':'default'}]
    assert not c.world['calls'] and c.store.bootstrap_checkpoint() is None
    assert list(c.store._db.iterdump())==before and c.source.read_bytes()==c.base



def test_restart_before_start_reuses_only_its_real_prepared_lease(bootstrap_service,monkeypatch):
    c=bootstrap_service;save=c.c._save
    def interrupted(record,**changes):
        if changes.get('stage')=='start_submitted':raise OSError('fixture write failure before start')
        return save(record,**changes)
    monkeypatch.setattr(c.c,'_save',interrupted)
    with pytest.raises(BootstrapError):c.c.run()
    record=c.store.bootstrap_checkpoint();lease_id=record['lease_id']
    assert record['stage']=='placed' and 'launch' not in record['effects']
    assert c.world['unit'] is None and c.store.lease(lease_id)[0].budget_gb==40
    monkeypatch.setattr(c.c,'_save',save)
    assert c.c.recover('resume')['stage']=='complete'
    assert c.store.bootstrap_checkpoint()['lease_id']==lease_id
    assert len(c.store.leases(include_released=True))==1 and c.world['calls'].count('systemd-run')==1


def test_unknown_start_ack_is_observed_without_launch_replay(bootstrap_service,monkeypatch):
    c=bootstrap_service;load=c.c._launcher
    def interrupted_launcher():
        module=load();run=module.run_checked
        def interrupted(argv,timeout=None):
            result=run(argv,timeout)
            if argv[0]=='systemd-run':raise OSError('fixture lost start acknowledgement')
            return result
        module.run_checked=interrupted
        return module
    monkeypatch.setattr(c.c,'_launcher',interrupted_launcher)
    with pytest.raises(BootstrapError):c.c.run()
    record=c.store.bootstrap_checkpoint()
    assert record['effects']['launch']=='submitted' and c.store.lease(record['lease_id'])[0].budget_gb==40
    assert c.world['unit'].poll() is None
    with pytest.raises(BootstrapError,match='cannot stop'):
        c.c.recover('rollback')
    assert 'bootstrap_rollback' not in c.world['calls'] and c.world['unit'].poll() is None
    observed=c.c.recover('observe')
    assert observed['stage']=='default_confirmed' and observed['needs_resume']
    assert c.store.bootstrap_checkpoint()['effects']['launch']=='submitted'  # Never manufacture a timely ACK.
    assert c.c.recover('resume')['stage']=='complete'
    assert c.world['calls'].count('systemd-run')==1


def test_unknown_activation_ack_finishes_from_bound_observation_only(bootstrap_service,monkeypatch):
    c=bootstrap_service;request=c.c.backend.request
    def interrupted(operation,context,*,deadline):
        result=request(operation,context,deadline=deadline)
        if operation=='bootstrap_activate':raise OSError('fixture lost activation response')
        return result
    monkeypatch.setattr(c.c.backend,'request',interrupted)
    with pytest.raises(OSError):c.c.run()
    assert c.store.bootstrap_checkpoint()['effects']['activate']=='submitted'
    assert c.c.recover('observe')['stage']=='complete'
    assert c.store.bootstrap_checkpoint()['effects']['activate']=='submitted'
    assert c.world['calls'].count('bootstrap_activate')==1


def test_missing_or_wrong_capability_cannot_confirm_pending_bootstrap(bootstrap_service,monkeypatch):
    c=bootstrap_service;save=c.c._save
    def interrupted(record,**changes):
        if changes.get('stage')=='start_submitted':raise OSError('fixture write failure')
        return save(record,**changes)
    monkeypatch.setattr(c.c,'_save',interrupted)
    with pytest.raises(BootstrapError):c.c.run()
    record=c.store.bootstrap_checkpoint()
    connection=http.client.HTTPConnection(*c.address,timeout=2)
    try:
        connection.request('POST','/v1/place/'+record['lease_id']+'/confirm','{}',
                           {'Content-Type':'application/json','X-LLMSVC-Bootstrap':'0'*64})
        response=connection.getresponse();body=json.loads(response.read())
        assert response.status==503 and body['error']=='bootstrap_reconciliation_required'
    finally:connection.close()
    assert c.store.lease(record['lease_id'])[0].status=='pending' and c.world['unit'] is None


def test_unexpected_non_source_error_is_not_erased_for_bootstrap(bootstrap_service,monkeypatch):
    c=bootstrap_service;collect=c.s.collect
    def broken_memory():
        return replace(collect(),errors=('memory: OSError',))
    monkeypatch.setattr(c.s,'collect',broken_memory)
    with pytest.raises(BootstrapError,match='not admissible'):c.c.run()
    assert c.store.leases()==() and c.world['unit'] is None
    assert not c.world['calls'] and c.store.bootstrap_checkpoint() is None



def test_wrong_kernel_gpu_never_confirms_or_stops_default(bootstrap_service):
    c=bootstrap_service;c.world['wrong_gpu']=1
    with pytest.raises(BootstrapError):c.c.run()
    record=c.store.bootstrap_checkpoint();lease=c.store.lease(record['lease_id'])[0]
    assert lease.status=='pending' and lease.gpu==0 and lease.budget_gb==40
    assert c.world['unit'].poll() is None and 'bootstrap_activate' not in c.world['calls']
    with pytest.raises(BootstrapError,match='unconfirmed'):c.c.recover('observe')
    assert c.store.bootstrap_pending() and c.world['calls'].count('systemd-run')==1


def test_changed_staged_launcher_config_cannot_redirect_capability(bootstrap_service,monkeypatch):
    c=bootstrap_service;request=c.c.backend.request
    def changed(operation,context,*,deadline):
        result=request(operation,context,deadline=deadline)
        if operation=='bootstrap_stage':
            Path(c.spec['launcher_config_path']).write_text('{"scheduler_url":"http://invalid.example"}')
        return result
    monkeypatch.setattr(c.c.backend,'request',changed)
    with pytest.raises(BootstrapError,match='inputs changed'):c.c.run()
    assert c.world['unit'] is None and c.store.leases()==()
    assert c.store.bootstrap_pending()



def test_file_rollback_before_launch_releases_only_proven_absent_pending_lease(bootstrap_service,monkeypatch):
    c=bootstrap_service;save=c.c._save
    def interrupted(record,**changes):
        if changes.get('stage')=='start_submitted':raise OSError('fixture before launch')
        return save(record,**changes)
    monkeypatch.setattr(c.c,'_save',interrupted)
    with pytest.raises(BootstrapError):c.c.run()
    lease_id=c.store.bootstrap_checkpoint()['lease_id']
    assert c.world['unit'] is None and c.store.lease(lease_id)[0].status=='pending'
    monkeypatch.setattr(c.c,'_save',save)
    assert c.c.recover('rollback')['stage']=='aborted'
    assert c.store.lease(lease_id)[0].status=='released'
    assert c.source.read_bytes()==c.base and c.world['source'] is None
    assert c.world['calls'].count('bootstrap_rollback')==1 and 'systemd-run' not in c.world['calls']
    assert not c.store.bootstrap_pending()



def test_source_down_exemption_requires_the_bound_source_origin(bootstrap_service,monkeypatch):
    c=bootstrap_service;request=c.c.backend.request
    def wrong_origin(operation,context,*,deadline):
        result=request(operation,context,deadline=deadline)
        result['source_origin']='http://127.0.0.1:2'
        return result
    monkeypatch.setattr(c.c.backend,'request',wrong_origin)
    with pytest.raises(BootstrapError,match='unbound'):c.c.run()
    assert c.store.bootstrap_checkpoint() is None and c.source.read_bytes()==c.base
    assert c.world['calls']==['bootstrap_preflight'] and c.world['unit'] is None
