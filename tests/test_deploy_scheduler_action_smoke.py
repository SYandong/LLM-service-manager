# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""CPU contract tests: real scheduler/lease HTTP and launcher, fixture hardware."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from deploy import lifecycle_smoke as life
from deploy import scheduler_action_smoke as smoke
from llmsvc.actions import ManagedModelTransport,ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController,UnitObservation
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import StateSnapshot,GPUState,MemoryState,ModelState,Activity
from llmsvc.store import IntentStore

ROOT=Path(__file__).parents[1]
API=runpy.run_path(str(ROOT/'cli/llm'))


@pytest.fixture
def chain(tmp_path,request):
    name='smoke';unit='vllm-smoke.service';world={'process':None,'state':'stopped','calls':[],'errors':(), 'resident':20.0}
    with socket.socket() as s:s.bind(('127.0.0.1',0));backend_port=s.getsockname()[1]
    class Source(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            assert self.path=='/api/models/unload/smoke'
            world['calls'].append(('POST',self.path));world['state']='sleeping'
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        def do_GET(self):
            assert self.path=='/upstream/smoke/'
            world['calls'].append(('GET',self.path));world['state']='awake'
            self.send_response(404);self.send_header('Content-Length','0');self.end_headers()
    source=ThreadingHTTPServer(('127.0.0.1',0),Source)
    def collect():
        process=world['process'];active=process is not None and process.poll() is None
        resident=(1.0 if world['state']=='sleeping' else world['resident']) if active else 0.0
        return StateSnapshot(sampled_at=time.time(),read_only=False,
            gpus=(GPUState(0,total_gb=100,used_gb=resident,free_gb=None if resident is None else 100-resident,
                           managed_gb=resident,external_gb=0),),errors=world['errors'],
            models=(ModelState(name,state=world['state'],gpu=0 if active else None,unit=unit,unit_active=active,
                    health_ok=active,is_sleeping=world['state']=='sleeping',swap_state='ready' if world['state']=='awake' else 'stopped',
                    resident_gb=resident,weights_gb=10,util=.2,cold_start_seconds=1),),
            activity=(Activity(name,time.time()-1000,1,1,0),),memory=MemoryState(500,10 if world['state']=='sleeping' else 0))
    config=SchedulerConfig('127.0.0.1',8011,read_only=False,model_actions_enabled=True,placement_enabled=True,
        state_db_path=str(tmp_path/'ledger.sqlite'),sample_interval_seconds=.02,event_heartbeat_seconds=.1,
        placement_wait_seconds=2,lease_probe_seconds=.1,free_timeout_seconds=2,wake_timeout_seconds=2,
        action_observe_seconds=1,action_poll_seconds=.02)
    store=IntentStore(config.state_db_path,action_lock=threading.RLock());scheduler=Scheduler(config,collect,store=store)
    transport=ManagedModelTransport(swap_url='http://127.0.0.1:'+str(source.server_port),
        models={name:{'unit':unit,'util':.2,'weights_gb':10,'port':backend_port}},
        systemctl='unused',run=lambda *a,**k:pytest.fail('fixture must not call real systemctl'))
    def environment():
        p=world['process']
        return dict(x.split(b'=',1) for x in Path('/proc/'+str(p.pid)+'/environ').read_bytes().split(b'\0') if b'=' in x)
    def probe(model,*,deadline):
        p=world['process']
        if p is None or p.poll() is not None:return UnitObservation(False,True)
        return UnitObservation(True,False,True,environment()[b'LLMSVC_LEASE_ID'].decode(),'a'*32)
    scheduler.model_actions=ModelActionController(scheduler,transport)
    scheduler.placement=PlacementController(scheduler,transport,probe=probe)
    server=SchedulerHTTPServer(('127.0.0.1',0),scheduler)
    threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.02}) for s in (server,source)]
    def cleanup():
        scheduler.stop()
        for srv,thread in zip((server,source),threads):
            if thread.is_alive():srv.shutdown()
            srv.server_close()
        for t in threads:
            if t.ident is not None:t.join(2)
        process=world['process']
        if process is not None:
            if process.poll() is None:process.terminate()
            process.wait(timeout=3);process.stderr.close()
        store.close()
    request.addfinalizer(cleanup)
    for t in threads:t.start()
    scheduler.start();scheduler.emit('started',detail={'read_only':False})
    launcher=smoke.load_launcher(ROOT/'deploy/vllm-launch')
    def checked(argv,timeout=None):
        if argv[0]=='systemd-run':
            assert not world['process']
            env={arg.split('=',1)[1].split('=',1)[0]:arg.split('=',2)[2] for arg in argv if arg.startswith('--setenv=')}
            assert env['LLMSVC_MODEL']==name
            script='''import http.server,sys
class H(http.server.BaseHTTPRequestHandler):
 def do_GET(self):self.send_response(200);self.end_headers()
 def log_message(self,*args):pass
http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
'''
            world['process']=subprocess.Popen([sys.executable,'-B','-c',script,str(backend_port)],env={**os.environ,**env},
                                               stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            world['state']='awake'
            return subprocess.CompletedProcess(argv,0,'','')
        active=world['process'] is not None and world['process'].poll() is None
        if argv[:2]==['systemctl','is-active']:
            return subprocess.CompletedProcess(argv,0 if active else 3,'active\n' if active else 'inactive\n','')
        assert argv[:2]==['systemctl','show']
        key=argv[argv.index('-p')+1] if '-p' in argv else None
        if argv.count('-p')>1:
            text='LoadState='+('loaded' if active else 'not-found')+'\nActiveState='+('active' if active else 'inactive')+'\nMainPID='+('1' if active else '0')+'\nControlGroup='+('/fixture' if active else '')
        elif key=='LoadState':text='loaded' if active else 'not-found'
        elif key=='ActiveState':text='active' if active else 'inactive'
        elif key=='Environment':text='LLMSVC_LEASE_ID='+environment()[b'LLMSVC_LEASE_ID'].decode()
        elif any(a.startswith('--property=') for a in argv):
            text='LoadState='+('loaded' if active else 'not-found')+'\nActiveState='+('active' if active else 'inactive')+'\nMainPID='+('1' if active else '0')+'\nControlGroup='+('/fixture' if active else '')
        else:raise AssertionError(argv)
        return subprocess.CompletedProcess(argv,0,text+'\n','')
    launcher.run_checked=checked
    config_path=tmp_path/'launcher.json';config_path.write_text(json.dumps({'scheduler_url':'http://127.0.0.1:'+str(server.server_port),
        'lock_dir':str(tmp_path/'locks'),'request_timeout_seconds':2,'health_timeout_seconds':.5,
        'startup_timeout_seconds':3,'health_poll_seconds':.02,'systemd_run':{'environment_file':None}}))
    assert not store.leases()
    launch_args=['.2',unit,'--config',str(config_path),'--',sys.executable,'--port',str(backend_port)]
    if getattr(request,'param',None)!='unstarted':
        code=launcher.main(launch_args)
        assert code==0
        lease,_=store.leases()[0];assert lease.status=='confirmed' and lease.budget_gb==20
    profile={'scheduler_url':'http://127.0.0.1:'+str(server.server_port),'model':name,'unit':unit,'gpu':0,'token':'fixture'}
    def identity_reader(unit_,token,*,lease=None,deadline=None,origin=None):
        assert unit_==unit and token=='fixture' and lease==environment()[b'LLMSVC_LEASE_ID'].decode()
        process=world['process'];ticks=Path('/proc/'+str(process.pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
        return {'unit':unit,'pid':process.pid,'start_ticks':ticks,'invocation_id':'a'*32}
    yield SimpleNamespace(scheduler=scheduler,profile=profile,world=world,identity_reader=identity_reader,store=store,launcher=launcher,launch_args=launch_args,root=tmp_path)


def test_actual_scheduler_free_wake_events_preserve_real_launcher_account(chain):
    first=smoke.action_probe(API,chain.profile,'free','local-free',deadline=time.monotonic()+4,identity_reader=chain.identity_reader)
    second=smoke.action_probe(API,chain.profile,'wake','local-wake',deadline=time.monotonic()+4,identity_reader=chain.identity_reader)
    assert first['after_resident_gb']==1 and second['after_resident_gb']==20
    assert first['lease']==second['lease'] and first['lease']['status']=='confirmed'
    assert first['lease']['budget_gb']==20
    assert first['event_id']>first['cursor_before'] and second['event_id']>second['cursor_before']
    assert first['server_request_id'] is None and first['local_request_id']=='local-free'
    assert chain.world['calls']==[('POST','/api/models/unload/smoke'),('GET','/upstream/smoke/')]


def test_pin_refusal_does_not_dispatch_or_forge_measurement(chain):
    client=API['SchedulerClient'](chain.profile['scheduler_url'])
    client.request('POST','/v1/pin',{'model':'smoke','until':time.time()+60,'by':'fixture'})
    with pytest.raises(smoke.EvidenceError,match='refused'):
        smoke.action_probe(API,chain.profile,'free','pinned',deadline=time.monotonic()+3,identity_reader=chain.identity_reader)
    assert not chain.world['calls'] and chain.store.leases()[0][0].status=='confirmed'


def test_unknown_resident_memory_blocks_before_action(chain):
    chain.world['resident']=None;chain.scheduler.sample_once()
    with pytest.raises(smoke.EvidenceError,match='unknown'):
        smoke.action_probe(API,chain.profile,'free','unknown',deadline=time.monotonic()+3,identity_reader=chain.identity_reader)
    assert not chain.world['calls']


def test_lease_or_unit_mismatch_blocks_before_action(chain):
    def wrong(*a,**k):raise smoke.EvidenceError('unit lease mismatch')
    with pytest.raises(smoke.EvidenceError,match='lease mismatch'):
        smoke.action_probe(API,chain.profile,'free','wrong',deadline=time.monotonic()+3,identity_reader=wrong)
    assert not chain.world['calls']


def test_dry_run_action_mode_creates_nothing(tmp_path,monkeypatch):
    root=tmp_path/'source'
    for name in ['llmsvc/collectors/__init__.py','llmsvc/state.py','cli/llm','deploy/vllm-launch','deploy/maintenance_native.py','deploy/maintenance_executor.py']:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')
    value={'mode':'scheduler-actions','container':'fixture','lock_path':str(tmp_path/'lock'),'model_path':'/cache/base',
           'vllm_binary':'/runtime/vllm','source':str(root),'output_dir':str(tmp_path/'evidence'),'gpu':0,
           'native_binary':'/native','wrapper_binary':'/wrapper','scheduler_python':'/python','host_meminfo_path':'/approved/host/meminfo',
           'nvidia_smi':'/usr/bin/nvidia-smi','native_binary_sha256':'a'*64,'wrapper_sha256':'b'*64}
    config=tmp_path/'config.json';config.write_text(json.dumps(value))
    before=sorted(str(p) for p in tmp_path.rglob('*'))
    monkeypatch.setattr(smoke,'ActionRun',lambda *a:pytest.fail('dry-run must not create a runner'))
    assert life.main(['--config',str(config),'--dry-run'])==0
    assert before==sorted(str(p) for p in tmp_path.rglob('*'))


def test_cleanup_does_not_stop_foreign_daemon_or_delete_ledger():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.temp_created=True;run.preserve=False;run.units=[]
    run.verify_owner=lambda **k:False;run.log=lambda *a,**k:None
    run.python=lambda *a,**k:pytest.fail('ledger must remain')
    run.container=lambda argv,**k: subprocess.CompletedProcess(argv,0,'loaded\n','') if argv[1]=='show' else pytest.fail('no foreign stop')
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):run.cleanup_actions()
    assert run.preserve


def test_missing_result_event_never_proves_latency(chain,monkeypatch):
    original=chain.scheduler.emit
    def emit(kind,*args,**kwargs):
        if kind=='free_result':return None  # Explicit missing-event fault injection.
        return original(kind,*args,**kwargs)
    monkeypatch.setattr(chain.scheduler,'emit',emit)
    with pytest.raises(life.SmokeError,match='deadline'):
        smoke.action_probe(API,chain.profile,'free','missing-event',deadline=time.monotonic()+1,identity_reader=chain.identity_reader)
    assert chain.world['calls']==[('POST','/api/models/unload/smoke')]
    assert chain.store.leases()[0][0].status=='confirmed'


def test_reported_event_gap_blocks_before_action(chain):
    base=API['EventReader'];readers=[]
    class Gap(base):
        def __init__(self,*a,**k):super().__init__(*a,**k);readers.append(self)
        def drain(self):
            value=super().drain();value['missed']=1;return value
    with pytest.raises(smoke.EvidenceError,match='SSE loss'):
        smoke.action_probe({**API,'EventReader':Gap},chain.profile,'free','gap',deadline=time.monotonic()+3,identity_reader=chain.identity_reader)
    assert not chain.world['calls']
    assert all(not reader.thread.is_alive() for reader in readers)


def test_foreign_control_unit_is_not_killed_and_files_are_retained():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=False;run.temp_created=True;run.preserve=False
    run.units=['llmsvc-foreign.service'];run.token='ours';run.log=lambda *a,**k:None
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv);return subprocess.CompletedProcess(argv,0,'LoadState=loaded\nEnvironment=LLMSVC_OPS_RUN_ID=other\n','')
    run.container=container;run.python=lambda *a,**k:pytest.fail('preserve files')
    with pytest.raises(life.SmokeError):run.cleanup_actions()
    assert all(call[1]=='show' for call in calls) and run.preserve


def _identity_observation():
    unit='vllm-fixture.service'
    return unit, 'run-token', {
        'Id': unit, 'MainPID': '42', 'InvocationID': 'a'*32,
        'Environment': 'CUDA_VISIBLE_DEVICES=0',
        'ControlGroup': '/system.slice/'+unit,
    }, {
        'CUDA_VISIBLE_DEVICES': '0', 'LLMSVC_MODEL': 'fixture',
        'LLMSVC_LEASE_ID': 'lease-1',
    }, '0::/system.slice/'+unit+'\n'


def test_daemon_identity_uses_proc_environment_and_exact_instance_contract():
    unit, token, values, proc_env, cgroup = _identity_observation()
    identity=smoke.validate_unit_observation(unit,token,values,proc_env,'123',cgroup,
        lease='lease-1',model='fixture')
    assert identity == {'unit':unit,'pid':42,'start_ticks':'123','invocation_id':'a'*32}
    with pytest.raises(smoke.EvidenceError,match='unit lease mismatch'):
        smoke.validate_unit_observation(unit,token,values,{**proc_env,'LLMSVC_LEASE_ID':'forged'},'123',cgroup,lease='lease-1',model='fixture')
    with pytest.raises(smoke.EvidenceError,match='daemon model environment mismatch'):
        smoke.validate_unit_observation(unit,token,values,{**proc_env,'LLMSVC_MODEL':'other'},'123',cgroup,lease='lease-1',model='fixture')
    with pytest.raises(smoke.EvidenceError,match='unit instance changed'):
        smoke.validate_unit_observation(unit,token,values,proc_env,'123',cgroup,lease='lease-1',model='fixture',expected={**identity,'pid':43})
    with pytest.raises(smoke.EvidenceError,match='unit exited'):
        smoke.validate_unit_observation(unit,token,{**values,'MainPID':'0'},proc_env,'123',cgroup,lease='lease-1',model='fixture')
    with pytest.raises(smoke.EvidenceError,match='daemon model environment mismatch'):
        smoke.validate_unit_observation(unit,token,values,{'CUDA_VISIBLE_DEVICES':'0','LLMSVC_LEASE_ID':'lease-1'},'123',cgroup,lease='lease-1',model='fixture')


def test_unit_identity_rechecks_systemd_and_process_instance(tmp_path, monkeypatch):
    proc=tmp_path/'proc';entry=proc/'42';entry.mkdir(parents=True)
    (entry/'stat').write_text('42 (fixture) '+' '.join(['S']+['0']*18+['123']))
    (entry/'environ').write_bytes(b'CUDA_VISIBLE_DEVICES=0\0LLMSVC_MODEL=fixture\0LLMSVC_LEASE_ID=lease-1\0')
    (entry/'cgroup').write_text('0::/system.slice/vllm-fixture.service\n')
    unit='vllm-fixture.service';base='Id='+unit+'\nMainPID=42\nInvocationID='+'a'*32+'\nEnvironment=CUDA_VISIBLE_DEVICES=0\nControlGroup=/system.slice/'+unit+'\n'
    calls=[]
    def runner(argv,**kwargs):
        calls.append(1);return subprocess.CompletedProcess(argv,0,base,'')
    monkeypatch.setattr(smoke.subprocess,'run',runner)
    result=smoke.unit_identity(unit,'ignored',lease='lease-1',model='fixture',proc_root=proc)
    assert result['start_ticks']=='123' and len(calls)==2
    replacement='Id='+unit+'\nMainPID=43\nInvocationID='+'b'*32+'\nEnvironment=CUDA_VISIBLE_DEVICES=0\nControlGroup=/system.slice/'+unit+'\n'
    calls.clear()
    def changing_runner(argv,**kwargs):
        calls.append(1);return subprocess.CompletedProcess(argv,0,base if len(calls)==1 else replacement,'')
    monkeypatch.setattr(smoke.subprocess,'run',changing_runner)
    with pytest.raises(smoke.EvidenceError,match='unit instance changed'):
        smoke.unit_identity(unit,'ignored',lease='lease-1',model='fixture',proc_root=proc)
    assert len(calls)==2


def test_control_unit_keeps_explicit_run_id_contract():
    unit='llmsvc-ops-action-source-run.service';values={
        'Id':unit,'MainPID':'7','InvocationID':'b'*32,
        'Environment':'LLMSVC_OPS_RUN_ID=run-token','ControlGroup':'/system.slice/'+unit}
    identity=smoke.validate_unit_observation(unit,'run-token',values,
        {'LLMSVC_OPS_RUN_ID':'run-token'},'456','0::/system.slice/'+unit+'\n',control=True)
    assert identity['pid']==7
    with pytest.raises(smoke.EvidenceError,match='control unit owner mismatch'):
        smoke.validate_unit_observation(unit,'run-token',values,
            {'LLMSVC_OPS_RUN_ID':'other'},'456','0::/system.slice/'+unit+'\n',control=True)


def test_cold_error_reports_collector_reason():
    with pytest.raises(smoke.EvidenceError,match='collector: concurrent round'):
        smoke.account({'errors':['collector: concurrent round']},{'model':'fixture','gpu':0,'unit':'vllm-fixture.service'})


def test_cleanup_releases_only_proven_owned_lease_after_absent_daemon():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+5
    run.profile={'scheduler_url':'http://fixture'}
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'}
    run.json_at=lambda url,path,body=None,**kwargs: (
        {'schema_version':1,'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]}
        if path=='/v1/state' else {'status':'released','lease_id':'lease-1'})
    run.log=lambda *a,**k:None
    run.cleanup_actions()


def test_daemon_binding_runs_through_remote_runtime_entrypoint(tmp_path):
    root=tmp_path/'run';(root/'runtime').mkdir(parents=True);(root/'owner').write_text('token')
    (root/'launch-lease.json').write_text(json.dumps({'lease_id':'lease-1'}))
    shutil.copytree(ROOT/'deploy',root/'runtime'/'deploy',dirs_exist_ok=True)
    fakebin=root/'bin';fakebin.mkdir();systemctl=fakebin/'systemctl'
    systemctl.write_text('#!/usr/bin/env python3\nprint("Id=vllm-fixture.service\\nLoadState=loaded\\nActiveState=inactive\\nMainPID=0\\nInvocationID=\\nEnvironment=\\nControlGroup=/system.slice/vllm-fixture.service")\n')
    systemctl.chmod(0o700)
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.temp=str(root);run.unit='vllm-fixture.service';run.model='fixture';run.token='token'
    def remote_python(code,data,**kwargs):
        env={**os.environ,'PATH':str(fakebin)+':'+os.environ.get('PATH',''),
             'PYTHONPATH':str(root/'runtime')+':'+str(ROOT)}
        value=subprocess.run([sys.executable,'-B','-c',code],input=json.dumps(data),text=True,
                             capture_output=True,env=env,check=True,timeout=5)
        return json.loads(value.stdout)
    run.python=remote_python
    result=run.daemon_binding(cleanup=True)
    assert result=={'absent':True,'exit_proven':True,'lease_id':'lease-1'}


def test_cleanup_preserves_files_when_exit_observation_deadline_expires():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=True;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.2
    run.daemon_binding=lambda **_: {'absent':False,'lease_id':'lease-1'}
    run.container=lambda *a,**k: subprocess.CompletedProcess(a,0,'LoadState=loaded\nActiveState=active\nMainPID=42\n','')
    run.log=lambda *a,**k:None
    run.json_at=lambda *a,**k:pytest.fail('must not release before proven exit')
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve


def test_generated_profile_has_empty_ledger_and_scoped_hardware(tmp_path):
    run=SimpleNamespace(config={'source':str(ROOT),'scheduler_python':'/python','native_binary':'/native','wrapper_binary':'/wrapper',
        'vllm_binary':'/vllm','model_path':'/cache','wrapper_sha256':'a'*64,'gpu':2,'nvidia_smi':'/usr/bin/nvidia-smi','host_meminfo_path':'/verified/host'},
        temp='/tmp/ops-life-token',token='token',model='ops-life-token',unit='vllm-ops-life-token.service',port=9001,
        source={},source_unit='llmsvc-ops-source-token.service',gpu_uuid='GPU-actual-2',work_deadline=time.monotonic()+200)
    files,profile=smoke.artifacts(run,[9002,9003,9004],10*1024**3)
    scheduler=json.loads(files[run.temp+'/scheduler.json'])
    assert scheduler['state_db_path']==run.temp+'/ledger.sqlite'
    assert not any('INSERT' in text or 'create_lease' in text for name,text in files.items() if not name.endswith('.py') and '/deploy/' not in name)
    assert scheduler['placement_enabled'] and scheduler['model_actions_enabled'] and not scheduler['automation_enabled']
    assert set(scheduler['collectors']['models'])=={run.model}
    assert 'ledger.sqlite' not in files
    compile(files[run.temp+'/nvidia-smi'],'<scoped nvidia wrapper>','exec')
    assert profile['gpu']==2 and profile['control_instances']=={}


@pytest.mark.parametrize('chain',['unstarted'],indirect=True)
def test_wrong_gpu_grant_is_released_without_starting_backend(chain,monkeypatch):
    monkeypatch.setattr(smoke,'load_launcher',lambda path:chain.launcher)
    profile={**chain.profile,'gpu':1,'root':str(chain.root),'launcher':'existing',
             'work_deadline':time.monotonic()+5}
    assert smoke.guarded_launch(profile,chain.launch_args)==1
    leases=chain.store.leases(include_released=True)
    assert len(leases)==1 and leases[0][0].status=='released'
    assert chain.world['process'] is None and not (chain.root/'launch-lease.json').exists()


def test_stop_helper_rejects_unowned_daemon_before_any_sleep(tmp_path,monkeypatch):
    (tmp_path/'launch-lease.json').write_text(json.dumps({'lease_id':'existing-real-protocol-id'}))
    profile={'root':str(tmp_path),'unit':'vllm-own.service','token':'own','backend_url':'http://127.0.0.1:9999'}
    def refuse(*a,**k):raise smoke.EvidenceError('unit ownership mismatch')
    monkeypatch.setattr(smoke,'unit_identity',refuse)
    monkeypatch.setattr(smoke.subprocess,'run',lambda *a,**k:pytest.fail('must not issue sleep'))
    with pytest.raises(smoke.EvidenceError,match='ownership'):
        smoke.stop_wrapper(profile,123)


@pytest.mark.parametrize('key,value',[('startup_seconds',0),('startup_seconds',float('inf')),
                                      ('cold_start_cost_seconds',True),('host_meminfo_path','/proc/meminfo')])
def test_invalid_action_mode_inputs_are_rejected_without_runner(tmp_path,key,value):
    config={'source':str(ROOT),'native_binary':'/native','wrapper_binary':'/wrapper','scheduler_python':'/python',
            'nvidia_smi':'/nvidia','host_meminfo_path':'/verified-host','native_binary_sha256':'a'*64,'wrapper_sha256':'b'*64}
    config[key]=value
    with pytest.raises(life.SmokeError):smoke.validate(config)


def test_http200_unknown_release_preserves_ledger():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.temp_created=True;run.preserve=False;run.units=[]
    run.unit='vllm-own.service';run.model='own';run.profile={'scheduler_url':'http://127.0.0.1:1'};run.log=lambda *a,**k:None
    run.container=lambda *a,**k:subprocess.CompletedProcess([],0,'not-found\n','')
    run.json_at=lambda url,path,*a,**k:({'schema_version':1,'leases':[{'model':'own','lease_id':'lease','status':'pending'}]} if path=='/v1/state' else {'status':'unknown'})
    run.python=lambda *a,**k:pytest.fail('unknown lease must be retained')
    with pytest.raises(life.SmokeError):run.cleanup_actions()
    assert run.preserve


def test_scoped_systemctl_preserves_positive_absence_and_rejects_mutations(tmp_path):
    tool=tmp_path/'systemctl';calls=tmp_path/'calls.json'
    tool.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\n'
                    +'Path('+repr(str(calls))+').write_text(json.dumps(sys.argv[1:]))\n'
                    +'print("Id=vllm-ops-life-token.service\\nLoadState=not-found\\nActiveState=inactive\\nMainPID=0\\nControlGroup=")\nraise SystemExit(1)\n')
    tool.chmod(0o700)
    run=SimpleNamespace(config={'source':str(ROOT),'scheduler_python':'/python','native_binary':'/native','wrapper_binary':'/wrapper',
        'wrapper_sha256':'a'*64,'vllm_binary':'/vllm','model_path':'/cache','gpu':0,'nvidia_smi':'/nvidia',
        'systemctl':str(tool),'host_meminfo_path':'/verified'},temp='/tmp/ops-life-token',token='token',model='ops-life-token',
        unit='vllm-ops-life-token.service',port=9001,source={},source_unit='source.service',gpu_uuid='GPU-test',work_deadline=time.monotonic()+200)
    files,_=smoke.artifacts(run,[9002,9003,9004],1024)
    wrapper=tmp_path/'read-only';wrapper.write_text(files[run.temp+'/systemctl-read'])
    result=subprocess.run([sys.executable,str(wrapper),'show','vllm-*.service','--property=Id,LoadState,ActiveState,MainPID,ControlGroup'],capture_output=True,text=True)
    assert result.returncode==0 and 'LoadState=not-found' in result.stdout
    assert json.loads(calls.read_text())[1]==run.unit
    before=calls.read_bytes()
    result=subprocess.run([sys.executable,str(wrapper),'stop',run.unit],capture_output=True,text=True)
    assert result.returncode!=0 and calls.read_bytes()==before


def test_intent_store_error_keeps_ledger_even_if_snapshot_lease_list_is_empty():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.temp_created=True;run.preserve=False;run.units=[]
    run.unit='vllm-own.service';run.model='own';run.profile={'scheduler_url':'http://127.0.0.1:1'};run.log=lambda *a,**k:None
    run.container=lambda *a,**k:subprocess.CompletedProcess([],0,'not-found\n','')
    run.json_at=lambda *a,**k:{'schema_version':1,'leases':[],'errors':['intent_store_unavailable']}
    run.python=lambda *a,**k:pytest.fail('must retain unknown ledger')
    with pytest.raises(life.SmokeError):run.cleanup_actions()
    assert run.preserve
