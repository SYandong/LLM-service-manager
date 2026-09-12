# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""CPU contract tests: real scheduler/lease HTTP and launcher, fixture hardware."""
import importlib.util
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError

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


@pytest.mark.parametrize('response,expect_success', [
    ({'model':'model','status':'ready','ready':True,'cold_start':True,'elapsed_seconds':1.2}, True),
    ({'model':'other','status':'ready','ready':True}, False),
    ({'model':'model','status':'failed','ready':False,'error':'wake_failed'}, False),
    (['not','an','object'], False),
])
def test_scheduler_wake_cold_route_owns_one_post_and_validates_final_response(response,expect_success,tmp_path,monkeypatch):
    post_seen=threading.Event();baseline_get_seen=threading.Event();progress_sent=threading.Event();done=threading.Event();calls=[]
    state={'read_only':False,'sampled_at':time.time(),'errors':[],
           'models':[{'name':'model','state':'awake','gpu':0,'unit':'vllm-model.service','resident_gb':20}],
           'leases':[{'model':'model','gpu':0,'unit':'vllm-model.service','lease_id':'lease-1','status':'confirmed','budget_gb':20}]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            if self.path.startswith('/v1/events?since='):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(b': connected\n\n');self.wfile.flush();assert baseline_get_seen.wait(1)
                old={'id':1,'timestamp':time.time(),'kind':'wake_progress','model':'model','detail':{'stage':'health_wait','source':'llama-swap','source_model':'model','progress_source':'per_model_log','log_epoch':'old','sequence':1,'received_at':time.time(),'trusted_for_quiet':False}}
                raw=json.dumps(old).encode();self.wfile.write(b'id: 1\ndata: '+raw+b'\n\n');self.wfile.flush()
                baseline={'id':2,'timestamp':time.time(),'kind':'state','model':'model','detail':{'sampled_at':time.time(),'errors':[]}}
                raw=json.dumps(baseline).encode();self.wfile.write(b'id: 2\ndata: '+raw+b'\n\n');self.wfile.flush();post_seen.wait(1)
                item={'id':3,'timestamp':time.time(),'kind':'wake_progress','model':'model','detail':{
                'stage':'health_wait','source':'llama-swap','source_model':'model','progress_source':'per_model_log',
                'log_epoch':'epoch','sequence':1,'received_at':time.time(),'trusted_for_quiet':False}}
                raw=json.dumps(item).encode();self.wfile.write(b'id: 3\ndata: '+raw+b'\n\n');self.wfile.flush();progress_sent.set();done.wait(1);return
            assert self.path=='/v1/state';state['sampled_at']=time.time();baseline_get_seen.set();payload=json.dumps(state).encode();self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def do_POST(self):
            assert self.path=='/v1/wake/model';calls.append(self.path);post_seen.set();assert progress_sent.wait(1);time.sleep(.05)
            payload=json.dumps(response).encode() if isinstance(response,dict) else json.dumps(response).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);done.set()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        profile={'scheduler_url':'http://127.0.0.1:'+str(server.server_port),'model':'model','unit':'vllm-model.service','token':'token','gpu':0,'request_id':'cold','work_deadline':time.monotonic()+5}
        identity=lambda unit,token,**kwargs:{'unit':unit,'pid':1,'start_ticks':'1','invocation_id':'a'*32}
        if expect_success:
            result=smoke.scheduler_wake_request(API,profile,time.monotonic()+3,identity_reader=identity)
            assert result['response']==response and result['progress_before_response'] is True
            assert result['lease']['lease_id']=='lease-1'
            assert result['progress'][0]['event_id']==3 and result['progress'][0]['model']=='model'
            assert isinstance(result['progress'][0]['source_event_timestamp'],float)
            assert isinstance(result['progress'][0]['local_observed_monotonic'],float)
            root=tmp_path/'run';root.mkdir();(root/'owner').write_text('token')
            baseline_get_seen.clear();post_seen.clear();progress_sent.clear();done.clear()
            request_id='a'*32;deadline=time.monotonic()+3
            (root/'request.json').write_text(json.dumps({'id':request_id,'operation':'cold','output':'result-'+request_id+'.json','deadline':deadline}))
            helper_profile={**profile,'root':str(root),'cli':str(ROOT/'cli/llm'),'control_instances':{},'cold_route':'scheduler_wake','work_deadline':deadline+2}
            profile_path=root/'profile.json';profile_path.write_text(json.dumps(helper_profile));monkeypatch.setattr(smoke,'unit_identity',identity)
            assert smoke.helper_main(['request',str(profile_path),'request.json'])==0
            receipt=json.loads((root/('result-'+request_id+'.json')).read_text())
            assert receipt['status']=='passed' and receipt['cold_start'] is True and receipt['lease']['lease_id']=='lease-1'
            evidence=receipt['evidence']
            assert all(field in evidence for field in ('started_wall','started_monotonic','post_returned_monotonic','progress_truncated','identity_checks'))
            assert evidence['progress_truncated'] is False and evidence['identity_checks']['account'] is True
            run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.model='model';run.unit='vllm-model.service';run.temp=str(root);run.deadline=time.monotonic()+70;run.work_deadline=run.deadline-5;run.measured_phases=set();run.phase_measurements={};run.units=[];run.config={'scheduler_python':sys.executable};run.log=lambda *a,**k:None;run.inventory=lambda **_:None;run.control=lambda *a,**k:None
            monkeypatch.setattr(smoke.uuid,'uuid4',lambda:SimpleNamespace(hex=request_id))
            run.python=lambda code,data,**kwargs: (Path(data['root'],data['name']).write_text(json.dumps(data['data'])) or {}) if 'write_text' in code else json.loads(Path(data['root'],data['name']).read_text())
            consumed=run.phase('cold',1)
            assert consumed['lease']['lease_id']=='lease-1' and run.measured_phases=={'cold'}
        else:
            with pytest.raises(smoke.EvidenceError):smoke.scheduler_wake_request(API,profile,time.monotonic()+3,identity_reader=identity)
        assert calls==(['/v1/wake/model','/v1/wake/model'] if expect_success else ['/v1/wake/model'])
    finally:
        done.set();server.shutdown();server.server_close();thread.join(2)


def test_scheduler_wake_post_uses_full_isolated_cold_budget_without_waiting_25_seconds():
    timeouts=[];posts=[]
    class Client:
        def __init__(self,url,timeout=10):timeouts.append(timeout)
        def request(self,method,path,payload=None,timeout=None):
            if method=='POST':posts.append(path);return {'model':'model','status':'ready','ready':True,'cold_start':True,'elapsed_seconds':40}
            return {'read_only':False,'sampled_at':time.time(),'errors':[],
                    'models':[{'name':'model','state':'awake','gpu':0,'unit':'vllm-model.service','resident_gb':20}],
                    'leases':[{'model':'model','gpu':0,'unit':'vllm-model.service','lease_id':'lease-1','status':'confirmed','budget_gb':20}]}
    class Reader:
        def __init__(self,*a,**k):self.calls=0
        def start(self):pass
        def drain(self):
            self.calls+=1
            if self.calls==1:return {'generation':0,'events':[{'id':1,'timestamp':time.time(),'kind':'state','model':'model','detail':{'sampled_at':time.time()+1,'errors':[]}}]}
            return {'generation':0,'events':[]}
        def close(self,timeout=None):return True
    api={'SchedulerClient':Client,'EventReader':Reader,'parse_wake_progress':lambda *a,**k:None,'accept_wake_progress':lambda *a,**k:False}
    profile={'scheduler_url':'http://fixture','model':'model','unit':'vllm-model.service','token':'token','gpu':0,'request_id':'cold'}
    result=smoke.scheduler_wake_request(api,profile,time.monotonic()+90,identity_reader=lambda *a,**k:{'unit':'vllm-model.service'})
    assert result['response']['cold_start'] is True and posts==['/v1/wake/model'] and timeouts[0]>25


def test_scheduler_wake_controlled_baseline_consumes_budget_and_posts_once(monkeypatch):
    clock=[100.0];timeouts=[];posts=[];post_timeouts=[]
    monkeypatch.setattr(smoke.time,'monotonic',lambda:clock[0])
    state={'read_only':False,'sampled_at':time.time(),'errors':[],
           'models':[{'name':'model','state':'awake','gpu':0,'unit':'vllm-model.service','resident_gb':20}],
           'leases':[{'model':'model','gpu':0,'unit':'vllm-model.service','lease_id':'lease-1','status':'confirmed','budget_gb':20}]}
    class Client:
        def __init__(self,url,timeout=10):timeouts.append(timeout)
        def request(self,method,path,payload=None,timeout=None):
            if method=='GET' and path=='/v1/state' and not posts:clock[0]+=10
            if method=='POST':
                posts.append(path);post_timeouts.append(timeout)
                return {'model':'model','status':'ready','ready':True,'cold_start':True,'elapsed_seconds':40}
            return state
    class Reader:
        def __init__(self,*a,**k):self.closed=False;self.calls=0
        def start(self):pass
        def drain(self):
            self.calls+=1
            if self.calls==1:return {'generation':0,'events':[{'id':1,'timestamp':time.time(),'kind':'state','model':'model','detail':{'sampled_at':time.time()+1,'errors':[]}}]}
            return {'generation':0,'events':[]}
        def close(self,timeout=None):self.closed=True;return True
    api={'SchedulerClient':Client,'EventReader':Reader,'parse_wake_progress':lambda *a,**k:None,'accept_wake_progress':lambda *a,**k:False}
    profile={'scheduler_url':'http://fixture','model':'model','unit':'vllm-model.service','token':'token','gpu':0}
    result=smoke.scheduler_wake_request(api,profile,190,identity_reader=lambda *a,**k:{'unit':'vllm-model.service'})
    assert result['response']['cold_start'] is True and posts==['/v1/wake/model'] and timeouts[0]==90
    assert post_timeouts==[80.0]


def test_scheduler_wake_expired_before_worker_invoke_does_not_post(monkeypatch):
    clock=[100.0];posts=[];closed=[]
    monkeypatch.setattr(smoke.time,'monotonic',lambda:clock[0])
    state={'read_only':False,'sampled_at':time.time(),'errors':[],
           'models':[{'name':'model','state':'awake','gpu':0,'unit':'vllm-model.service','resident_gb':20}],
           'leases':[{'model':'model','gpu':0,'unit':'vllm-model.service','lease_id':'lease-1','status':'confirmed','budget_gb':20}]}
    class Client:
        def __init__(self,url,timeout=10):pass
        def request(self,method,path,payload=None,timeout=None):
            if method=='POST':posts.append(path)
            return state if method=='GET' else {'model':'model','status':'ready','ready':True,'cold_start':True,'elapsed_seconds':40}
    class Reader:
        def __init__(self,*a,**k):self.calls=0
        def start(self):pass
        def drain(self):
            self.calls+=1
            return {'generation':0,'events':[{'id':1,'timestamp':time.time(),'kind':'state','model':'model','detail':{'sampled_at':time.time()+1,'errors':[]}}]} if self.calls==1 else {'generation':0,'events':[]}
        def close(self,timeout=None):closed.append(timeout);return True
    class DelayedThread:
        def __init__(self,target,**kwargs):self.target=target
        def start(self):clock[0]=190;self.target()
        def is_alive(self):return False
        def join(self,timeout=None):pass
    monkeypatch.setattr(smoke.threading,'Thread',DelayedThread)
    api={'SchedulerClient':Client,'EventReader':Reader,'parse_wake_progress':lambda *a,**k:None,'accept_wake_progress':lambda *a,**k:False}
    profile={'scheduler_url':'http://fixture','model':'model','unit':'vllm-model.service','token':'token','gpu':0}
    with pytest.raises((smoke.EvidenceError,life.SmokeError)):
        smoke.scheduler_wake_request(api,profile,190,identity_reader=lambda *a,**k:{'unit':'vllm-model.service'})
    assert posts==[] and closed and closed[0]>0


def _resident_boundary_run(monkeypatch, *, stale=False, active_baseline=False, own=False):
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    pid=os.getpid();proc=Path('/proc')/str(pid)
    ticks=proc.joinpath('stat').read_text().rsplit(') ',1)[1].split()[19]
    cgroup=proc.joinpath('cgroup').read_text().split(':',2)[-1].strip().rstrip('/')
    unit=cgroup.rsplit('/',1)[-1] or 'fixture.service'
    run.config={'mode':'scheduler-actions','gpu':0,'util':.07,'nvidia_smi':'nvidia-smi',
                'idle_resident':{'enabled':True,'margin_gb':4,'idle_util_percent':1,
                    'candidate_full_budget_gb':99,'external_baseline_gb':99,
                    'inflight':99,'protected_processes':[],
                    'baseline_processes':[] if own else [{'gpu_uuid':'GPU0','pid':pid,
                        'start_ticks':ticks,'cgroup':cgroup}]}}
    run.token='token';run.model='candidate';run.unit=unit;run.deadline=time.monotonic()+10
    run.work_deadline=run.deadline-1;run.profile={'scheduler_url':'http://scheduler'};run.records=[]
    run._idle_owned_processes=[];run.port=None
    current=[] if own else [{'gpu_uuid':'GPU0','pid':pid,'start_ticks':ticks,'cgroup':cgroup,
                             'used_memory_mib':'128'}]
    state={'sampled_at':time.time()-(10 if stale else 0),'read_only':False,'errors':[],
           'inflight':0,'models':[],'leases':[]}
    def command(argv, **kwargs):
        if any(item.startswith('--query-gpu=') for item in argv):
            return subprocess.CompletedProcess(argv,0,'0, GPU0, 143360.0, 1024.0, 142336.0, 0.0\n','')
        if any(item.startswith('--query-compute-apps=') for item in argv):
            text=f'GPU0, {pid}, owned, 128\n'
            return subprocess.CompletedProcess(argv,0,text,'')
        if argv[1:2]==['pmon']:
            util='2.0' if active_baseline else '0.0'
            return subprocess.CompletedProcess(argv,0,f'# gpu pid type sm mem enc dec command\n0 {pid} C {util} 0 0 0 python\n','')
        raise AssertionError(argv)
    run.command=command
    run.json_at=lambda url,path,*args,**kwargs: state
    run.python=lambda code,data,**kwargs: {'events':[{'type':'modelStatus','data':'[]'},
        {'type':'inflight','data':'{"operation":"snapshot","requests":[]}'}],
        'cached_weights_complete':True,'weight_bytes':1000000,'port':12345}
    monkeypatch.setattr(smoke.lifecycle,'check_host_capacity',lambda *a,**k:None)
    return run


@pytest.mark.parametrize('case', ['stale_static','active_baseline','own_activity'])
def test_action_run_idle_resident_uses_fresh_boundary_observation(monkeypatch, case):
    run=_resident_boundary_run(monkeypatch, stale=case=='stale_static',
                               active_baseline=case=='active_baseline', own=case=='own_activity')
    if case=='own_activity':
        assert run.inventory(allow_own=True,allow_resident=True)[1]=='GPU0'
    else:
        with pytest.raises(life.SmokeError):run.inventory(allow_resident=True)


def test_action_run_idle_resident_does_not_accept_static_sleeping_booleans(monkeypatch):
    run=_resident_boundary_run(monkeypatch)
    run.config['idle_resident']['protected_processes']=[{
        'model':'protected','gpu_uuid':'GPU0','pid':os.getpid(),'start_ticks':'1',
        'cgroup':'/system.slice/protected.service','sleeping_proof':True,
        'health_proof':True,'full_budget_gb':10}]
    with pytest.raises(life.SmokeError,match='protected'):
        run.inventory(allow_resident=True)


def test_scheduler_wake_postresponse_identity_failure_preserves_response_and_progress():
    class Client:
        def __init__(self,url,timeout=10):pass
        def request(self,method,path,payload=None,timeout=None):
            if method=='POST':return {'model':'model','status':'ready','ready':True,'cold_start':True,'elapsed_seconds':1}
            return {'read_only':False,'sampled_at':time.time(),'errors':[],
                    'models':[{'name':'model','state':'awake','gpu':0,'unit':'vllm-model.service','resident_gb':20}],
                    'leases':[{'model':'model','gpu':0,'unit':'vllm-model.service','lease_id':'lease-1','status':'confirmed','budget_gb':20}]}
    class Reader:
        def __init__(self,*a,**k):self.done=False;self.calls=0
        def start(self):pass
        def drain(self):
            self.calls+=1
            if self.calls==1:return {'generation':0,'events':[{'id':1,'timestamp':time.time(),'kind':'state','model':'model','detail':{'sampled_at':time.time()+1,'errors':[]}}]}
            if self.calls==2:return {'generation':0,'events':[{'id':2,'timestamp':time.time(),'kind':'wake_progress','model':'model','detail':{'stage':'health_wait','source':'llama-swap','source_model':'model','progress_source':'per_model_log','log_epoch':'e','sequence':1,'received_at':time.time(),'trusted_for_quiet':False}}]}
            return {'generation':0,'events':[]}
        def close(self,timeout=None):return True
    api={'SchedulerClient':Client,'EventReader':Reader,'parse_wake_progress':API['parse_wake_progress'],'accept_wake_progress':API['accept_wake_progress']}
    profile={'scheduler_url':'http://fixture','model':'model','unit':'vllm-model.service','token':'token','gpu':0,'request_id':'cold'}
    def fail_identity(*a,**k):raise RuntimeError('identity failed after response')
    with pytest.raises(RuntimeError) as caught:
        smoke.scheduler_wake_request(api,profile,time.monotonic()+5,identity_reader=fail_identity)
    evidence=caught.value.evidence
    assert evidence['response']['status']=='ready' and evidence['progress'] and evidence['identity_checks']['account'] is True


def test_scheduler_wake_route_checks_warm_latency_independently(tmp_path):
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.model='model';run.unit='vllm-model.service';run.temp=str(tmp_path);run.deadline=time.monotonic()+70;run.work_deadline=run.deadline-5;run.measured_phases=set();run.phase_measurements={};run.units=[];run.config={'scheduler_python':sys.executable,'cold_route':'scheduler_wake'};run.log=lambda *a,**k:None;run.inventory=lambda **_:None;run.control=lambda *a,**k:None
    def python(code,data,**kwargs):
        if 'write_text' in code:
            receipt={'local_request_id':data['data']['id'],'operation':'wake','status':'passed','evidence':{'http_seconds':4.0}}
            (tmp_path/data['data']['output']).write_text(json.dumps(receipt));return {}
        return json.loads((tmp_path/data['name']).read_text())
    run.python=python
    with pytest.raises(life.SmokeError,match='warm wake exceeded'):
        run.phase('wake',1)
    assert run.phase_measurements['wake']=={'quality':'measured_over_target','measured':True}


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
    with pytest.raises(life.SmokeError,match='deadline') as exc:
        smoke.action_probe(API,chain.profile,'free','missing-event',deadline=time.monotonic()+1,identity_reader=chain.identity_reader)
    assert exc.value.evidence['response']['status'] in ('complete', 'partial', 'blocked')
    assert exc.value.evidence['identity_checks']['before_account'] is True
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


def test_action_run_inventory_uses_primary_catalog_without_generated_model_collision(tmp_path, monkeypatch):
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.config={'gpu':1,'util':.2,'model_path':'/cache','host_meminfo_path':'/verified-host'}
    run.model='ops-life-generated';run.unit='vllm-ops-life-generated.service';run.gpu_uuid='GPU-1';run.port=None;run.records=[]
    meminfo=tmp_path/'meminfo';meminfo.write_text('MemAvailable:       1048576000 kB\n')
    real_path=life.Path
    monkeypatch.setattr(life, 'Path', lambda value: meminfo if str(value)=='/proc/meminfo' else real_path(value))
    run.command=lambda argv,**kwargs: subprocess.CompletedProcess(
        argv,0,
        '1, GPU-1, 100000, 5, 99995, 0\n' if '--query-gpu=' in argv[1]
        else '', '')
    run.python=lambda code,data,**kwargs: {
        'events':[{'type':'modelStatus','data':'[{"id":"production-default","state":"ready"}]'},
                      {'type':'inflight','data':'{"operation":"snapshot","requests":[]}'}],
        'cached_weights_complete':True,'weight_bytes':1024,'port':9001,
    }
    observed=run.inventory()
    assert observed[0]=='1' and run.observation_quiet(run.python(None,None))


@pytest.mark.parametrize('events', [
    '[{"id":"ops-life-generated","state":"stopped"},{"id":"production-default","state":"ready"}]',
    '[{"id":"production-default"}]',
    '[{"id":"production-default","state":"unknown"}]',
    '[{"id":"production-default","state":"starting"}]',
    '[{"id":"production-default","state":"ready"},{"id":"production-default","state":"stopped"}]',
])
def test_action_run_inventory_rejects_collision_or_malformed_primary_catalog(tmp_path, events):
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.config={'gpu':1,'util':.2,'model_path':'/cache','host_meminfo_path':'/verified-host'}
    run.model='ops-life-generated';run.unit='vllm-ops-life-generated.service';run.gpu_uuid='GPU-1';run.port=None;run.records=[]
    run.command=lambda argv,**kwargs: subprocess.CompletedProcess(argv,0,
        '1, GPU-1, 100000, 5, 99995, 0\n' if '--query-gpu=' in argv[1] else '', '')
    run.python=lambda code,data,**kwargs: {'events':[{'type':'modelStatus','data':events},
        {'type':'inflight','data':'{"operation":"snapshot","requests":[]}'}],
        'cached_weights_complete':True,'weight_bytes':1024,'port':9001}
    with pytest.raises(life.SmokeError, match='unknown/busy'):
        run.inventory()
    assert not run.observation_quiet(run.python(None,None))


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
        {'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]}
        if path=='/v1/state' else {'status':'released','lease_id':'lease-1'})
    run.log=lambda *a,**k:None
    run.cleanup_actions()


def test_cleanup_stop_timeout_observes_exit_then_releases_once():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+5
    run.profile={'scheduler_url':'http://fixture'};readings=iter(({'absent':False,'lease_id':'lease-1'},{'absent':True,'lease_id':'lease-1'}));run.daemon_binding=lambda **_:next(readings);run.log=lambda *a,**k:None
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,1)
        return subprocess.CompletedProcess(argv,0,'LoadState=not-found\nActiveState=inactive\nMainPID=0\n','')
    run.container=container;released=[]
    def json_at(url,path,body=None,**kwargs):
        if path=='/v1/state':return {'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]}
        released.append(path);return {'status':'released','lease_id':'lease-1'}
    run.json_at=json_at;run.cleanup_actions()
    assert len([x for x in calls if x[1]=='stop'])==1 and released==['/v1/place/lease-1/release']


def test_cleanup_transient_snapshot_error_then_fresh_valid_releases_once():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+3
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    binding={'absent':False,'lease_id':'lease-1','identity':{'unit':run.unit,'pid':42,'start_ticks':'1','invocation_id':'a'*32}}
    readings=[0]
    def daemon_binding(**_):
        readings[0]+=1;return {'absent':readings[0]>1,'lease_id':'lease-1'}
    run.daemon_binding=daemon_binding
    calls=[];run.container=lambda argv,**kwargs:(calls.append(argv) or subprocess.CompletedProcess(argv,0,'LoadState=not-found\nActiveState=inactive\nMainPID=0\n',''))
    base=time.time();snapshots=iter(({'schema_version':1,'sampled_at':base,'errors':['collector unavailable'],'leases':[]},
                    {'schema_version':1,'sampled_at':base+.01,'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1','unit':run.unit}]}))
    released=[]
    run.json_at=lambda url,path,body=None,**kwargs: next(snapshots) if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'})
    run.cleanup_actions()
    assert len([x for x in calls if x[1]=='stop'])==1 and released==['/v1/place/lease-1/release']


def test_cleanup_persistent_unknown_preserves_ledger_without_second_stop():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.25
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    readings=[0]
    def daemon_binding(**_):
        readings[0]+=1;return {'absent':readings[0]>1,'lease_id':'lease-1'}
    run.daemon_binding=daemon_binding
    calls=[];run.container=lambda argv,**kwargs:(calls.append(argv) or subprocess.CompletedProcess(argv,0,'LoadState=not-found\nActiveState=inactive\nMainPID=0\n',''))
    reads=[];run.json_at=lambda *a,**k:(reads.append(a[1]) or {'schema_version':1,'sampled_at':time.time(),'errors':['still unknown'],'leases':[]})
    with pytest.raises(life.SmokeError,match='account cleanup snapshot unknown'):
        run.cleanup_actions()
    assert run.preserve and len([x for x in calls if x[1]=='stop'])==1 and reads.count('/v1/state')>1


def test_cleanup_malformed_lease_row_is_unknown_and_preserved():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+1
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'}
    run.json_at=lambda *a,**k:{'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[None]}
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve


def test_cleanup_stale_or_nonadvancing_snapshot_never_releases():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.25
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'}
    base=time.time()-10;released=[]
    def json_at(url,path,body=None,**kwargs):
        if path=='/v1/state':
            return {'schema_version':1,'sampled_at':base,'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]}
        released.append(path);return {'status':'released','lease_id':'lease-1'}
    run.json_at=json_at
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve and released==[]


def test_cleanup_invalid_timestamp_preserves_without_release():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.2
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'};released=[]
    run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':1,'sampled_at':'unknown','errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve and released==[]


def test_cleanup_invalid_timestamp_builds_baseline_then_requires_new_clean_publication():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+2
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'}
    base=time.time();released=[]
    states=iter((
        {'schema_version':1,'sampled_at':'bad','errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]},
        {'schema_version':1,'sampled_at':base,'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]},
        {'schema_version':1,'sampled_at':base+.01,'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]},
    ))
    run.json_at=lambda url,path,body=None,**kwargs: next(states) if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'})
    run.cleanup_actions()
    assert released==['/v1/place/lease-1/release']


def test_cleanup_malformed_errors_are_unknown_and_preserved():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.2
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'};released=[]
    run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':1,'sampled_at':time.time(),'errors':{},'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve and released==[]


def test_cleanup_boolean_schema_version_is_unknown_and_preserved():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.2
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'};released=[]
    run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':True,'sampled_at':time.time(),'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve and released==[]


@pytest.mark.parametrize('rows', [
    [{'lease_id':'lease-1','model':'fixture','status':'confirmed'},
     {'lease_id':'lease-1','model':'fixture','status':'stale'}],
    [{'lease_id':'lease-1','model':'other','status':'confirmed'}],
])
def test_cleanup_duplicate_or_foreign_matching_lease_preserves(rows):
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+1
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'};released=[]
    run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':rows} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):
        run.cleanup_actions()
    assert run.preserve and released==[]


def test_cleanup_replacement_after_exit_preserves_without_release():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+3
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    initial={'absent':False,'lease_id':'lease-1','identity':{'unit':run.unit,'pid':42,'start_ticks':'1','invocation_id':'a'*32}}
    replacement={'absent':False,'lease_id':'lease-1','identity':{'unit':run.unit,'pid':43,'start_ticks':'2','invocation_id':'b'*32}}
    readings=iter((initial,replacement));run.daemon_binding=lambda **_:next(readings)
    calls=[];run.container=lambda argv,**kwargs:(calls.append(argv) or subprocess.CompletedProcess(argv,0,'LoadState=not-found\nActiveState=inactive\nMainPID=0\n',''))
    run.json_at=lambda *a,**k:pytest.fail('replacement must not be released')
    with pytest.raises(life.SmokeError,match='daemon identity changed'):
        run.cleanup_actions()
    assert run.preserve and len([x for x in calls if x[1]=='stop'])==1


def test_cleanup_already_released_reconciles_without_second_release():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+3
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None;run.token='token';run.temp='/tmp/unused'
    readings=iter(({'absent':False,'lease_id':'lease-1'},{'absent':True,'lease_id':'lease-1'}));run.daemon_binding=lambda **_:next(readings)
    calls=[];run.container=lambda argv,**kwargs:(calls.append(argv) or subprocess.CompletedProcess(argv,0,'LoadState=not-found\nActiveState=inactive\nMainPID=0\n',''))
    released=[]
    run._ledger_release_witness=lambda binding:True
    run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[]} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    run.cleanup_actions()
    assert len([x for x in calls if x[1]=='stop'])==1 and released==[]


@pytest.mark.parametrize('tombstone', [
    None,
    ('fixture','confirmed','vllm-fixture.service'),
    ('fixture','released','other-unit.service'),
    ('fixture','released','vllm-fixture.service'),
])
def test_cleanup_empty_public_leases_requires_private_released_witness(tmp_path,tombstone):
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.token='token';run.temp=str(tmp_path);run.deadline=time.monotonic()+.2
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    (tmp_path/'owner').write_text('token');(tmp_path/'launch-lease.json').write_text(json.dumps({'lease_id':'lease-1'}))
    db=sqlite3.connect(tmp_path/'ledger.sqlite');db.execute('CREATE TABLE llmsvc_leases (lease_id TEXT PRIMARY KEY, model TEXT, gpu INTEGER, util REAL, expires_at REAL, budget_gb REAL, status TEXT, unit TEXT)')
    if tombstone is not None:db.execute('INSERT INTO llmsvc_leases VALUES (?,?,?,?,?,?,?,?)',('lease-1',tombstone[0],0,0,0,1,tombstone[1],tombstone[2]))
    db.commit();db.close()
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease-1'};run.container=lambda *a,**k:subprocess.CompletedProcess([],0,'not-found\n','')
    released=[];run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[]} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    def remote_python(code,data,**kwargs):
        result=subprocess.run([sys.executable,'-B','-c',code],input=json.dumps(data),text=True,capture_output=True,check=True)
        return json.loads(result.stdout)
    run.python=remote_python
    if tombstone==('fixture','released','vllm-fixture.service'):
        run.cleanup_actions();assert released==[] and not run.preserve
    else:
        with pytest.raises(life.SmokeError,match='cleanup incomplete'):run.cleanup_actions()
        assert run.preserve and released==[]


def test_completion_keeps_measured_phases_when_cleanup_fails():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.scope='fixture';run.measured_phases={'cold','free','wake'};run.phase_measurements={name:{'quality':'validated','measured':True} for name in run.measured_phases}
    value=run._completion_fields('failed',False)
    assert value['result']=='failed' and value['cleanup_succeeded'] is False
    assert value['live_chain_measured'] is True and value['measured_phases']==['cold','free','wake']


def test_completion_reports_no_measurements_after_preflight_failure():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.scope='fixture';run.measured_phases=set();run.phase_measurements={}
    value=run._completion_fields('failed',False)
    assert value['result']=='failed' and value['cleanup_succeeded'] is False
    assert value['live_chain_measured'] is False and value['measured_phases']==[]


def test_phase_records_measured_receipt_for_completion_reporting(tmp_path):
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.model='fixture';run.unit='vllm-fixture.service';run.temp=str(tmp_path);run.deadline=time.monotonic()+70;run.work_deadline=run.deadline-5;run.measured_phases=set();run.phase_measurements={};run.units=[];run.config={'scheduler_python':sys.executable};run.log=lambda *a,**k:None
    run.inventory=lambda **_:None;run.control=lambda *a,**k:None
    captured={}
    def python(code,data,**kwargs):
        if 'write_text' in code:
            captured.update(data['data']);(tmp_path/data['data']['output']).write_text(json.dumps({'status':'passed','local_request_id':data['data']['id'],'operation':data['data']['operation']}));return {}
        return json.loads((tmp_path/data['name']).read_text())
    run.python=python
    value=run.phase('free',1)
    assert value['status']=='passed' and run.measured_phases=={'free'}
    assert run._completion_fields('failed',False)['live_chain_measured'] is True


def test_partial_measured_phase_is_reported_without_claiming_full_success():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.model='fixture';run.measured_phases=set();run.phase_measurements={}
    run._record_phase_result('free',{'status':'failed','evidence':{'model':'fixture','response':{
        'status':'partial','measurement_complete':True,'freed_gb':3.5,'slept':['fixture'],'stopped':[]}}})
    value=run._completion_fields('failed',False)
    assert value['live_chain_measured'] is True and value['measured_phases']==['free']
    assert value['phase_measurements']['free']['quality']=='partial_measured'
    run._record_phase_result('wake',{'status':'failed','evidence':{'response':None}})
    assert value['phase_measurements']['free']['measured'] is True and run.phase_measurements['wake']['measured'] is False


@pytest.mark.parametrize('evidence', [
    {'model':'different','response':{'status':'partial','measurement_complete':True,'freed_gb':3.5,'slept':['fixture'],'stopped':[]}},
    {'response':{'status':'partial','measurement_complete':True,'freed_gb':3.5,'slept':['fixture'],'stopped':[]}},
    {'model':'fixture','response':{'status':'partial','measurement_complete':True,'freed_gb':3.5,'slept':['different'],'stopped':[]}},
])
def test_partial_free_wrong_or_missing_association_is_not_measured(evidence):
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.model='fixture';run.measured_phases=set();run.phase_measurements={}
    run._record_phase_result('free',{'status':'failed','evidence':evidence})
    assert run.phase_measurements['free']['measured'] is False and run.measured_phases==set()


def test_cleanup_stop_timeout_without_exit_preserves_ledger_and_does_not_release():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+.2
    run.profile={'scheduler_url':'http://fixture'};run.daemon_binding=lambda **_: {'absent':False,'lease_id':'lease-1'};run.log=lambda *a,**k:None
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,1)
        return subprocess.CompletedProcess(argv,0,'LoadState=loaded\nActiveState=active\nMainPID=42\n','')
    run.container=container;run.json_at=lambda *a,**k:pytest.fail('must not release uncertain lease')
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):run.cleanup_actions()
    assert run.preserve and len([x for x in calls if x[1]=='stop'])==1


def test_cleanup_stop_timeout_replacement_identity_preserves_ledger_and_no_repeat_stop():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+5
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    first={'unit':run.unit,'pid':42,'start_ticks':'100','invocation_id':'a'*32}
    replacement={'unit':run.unit,'pid':43,'start_ticks':'200','invocation_id':'b'*32}
    readings=iter(({'absent':False,'lease_id':'lease-1','identity':first},
                   {'absent':False,'lease_id':'lease-1','identity':replacement}))
    run.daemon_binding=lambda **_:next(readings)
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,1)
        return subprocess.CompletedProcess(argv,0,'LoadState=loaded\nActiveState=active\nMainPID=42\n','')
    run.container=container;run.json_at=lambda *a,**k:pytest.fail('replacement must not release')
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):run.cleanup_actions()
    assert run.preserve and len([x for x in calls if x[1]=='stop'])==1


def test_cleanup_stop_timeout_cleared_terminal_identity_releases_once():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+5
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    binding={'absent':False,'lease_id':'lease-1','identity':{'unit':run.unit,'pid':42,'start_ticks':'100','invocation_id':'a'*32},'control_group':'/system.slice/'+run.unit}
    readings=iter((binding,{'absent':True,'lease_id':'lease-1'}));run.daemon_binding=lambda **_:next(readings)
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,1)
        return subprocess.CompletedProcess(argv,0,'Id='+run.unit+'\nLoadState=loaded\nActiveState=inactive\nMainPID=0\nInvocationID=\nControlGroup=\n','')
    run.container=container;released=[]
    run.json_at=lambda url,path,body=None,**kwargs: ({'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[{'model':'fixture','status':'confirmed','lease_id':'lease-1'}]} if path=='/v1/state' else (released.append(path) or {'status':'released','lease_id':'lease-1'}))
    run.cleanup_actions()
    assert len([x for x in calls if x[1]=='stop'])==1 and released==['/v1/place/lease-1/release']


def test_cleanup_stop_timeout_terminal_replacement_invocation_preserves_files():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=True;run.temp_created=False;run.units=[];run.preserve=False;run.model='fixture';run.unit='vllm-fixture.service';run.deadline=time.monotonic()+5
    run.profile={'scheduler_url':'http://fixture'};run.log=lambda *a,**k:None
    first={'unit':run.unit,'pid':42,'start_ticks':'100','invocation_id':'a'*32}
    run.daemon_binding=lambda **_: {'absent':False,'lease_id':'lease-1','identity':first,'control_group':'/system.slice/'+run.unit}
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,1)
        return subprocess.CompletedProcess(argv,0,'Id='+run.unit+'\nLoadState=loaded\nActiveState=failed\nMainPID=0\nInvocationID='+'b'*32+'\nControlGroup=/system.slice/'+run.unit+'\n','')
    run.container=container;run.json_at=lambda *a,**k:pytest.fail('replacement must not release')
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):run.cleanup_actions()
    assert run.preserve and len([x for x in calls if x[1]=='stop'])==1


def test_control_stop_timeout_terminal_replacement_preserves_files():
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.attempted=False;run.temp_created=False;run.units=['llmsvc-control.service'];run.preserve=False;run.token='token';run.model='fixture';run.unit='vllm-fixture.service';run.profile={'scheduler_url':'http://fixture'};run.deadline=time.monotonic()+5;run.log=lambda *a,**k:None
    run.daemon_binding=lambda **_: {'absent':True,'lease_id':'lease'}
    initial='Id=llmsvc-control.service\nLoadState=loaded\nActiveState=active\nMainPID=7\nInvocationID='+'a'*32+'\nControlGroup=/system.slice/llmsvc-control.service\nEnvironment=LLMSVC_OPS_RUN_ID=token\n'
    terminal='Id=llmsvc-control.service\nLoadState=loaded\nActiveState=failed\nMainPID=0\nInvocationID='+'b'*32+'\nControlGroup=/system.slice/llmsvc-control.service\n'
    calls=[]
    def container(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='kill':return subprocess.CompletedProcess(argv,0,'','')
        if argv[1]=='stop':raise subprocess.TimeoutExpired(argv,1)
        return subprocess.CompletedProcess(argv,0,initial if len([x for x in calls if x[1]=='show'])==1 else terminal,'')
    run.container=container;run.python=lambda *a,**k:pytest.fail('replacement must preserve files')
    with pytest.raises(life.SmokeError,match='cleanup incomplete'):run.cleanup_actions()
    assert run.preserve and len([x for x in calls if x[1]=='stop'])==1


def test_daemon_binding_runs_through_remote_runtime_entrypoint(tmp_path):
    root=tmp_path/'run';(root/'runtime').mkdir(parents=True);(root/'owner').write_text('token')
    (root/'launch-lease.json').write_text(json.dumps({'lease_id':'lease-1'}))
    shutil.copytree(ROOT/'deploy',root/'runtime'/'deploy',dirs_exist_ok=True)
    fakebin=root/'bin';fakebin.mkdir();systemctl=fakebin/'systemctl'
    systemctl.write_text('#!/usr/bin/env python3\nprint("Id=vllm-fixture.service\\nLoadState=loaded\\nActiveState=inactive\\nMainPID=0\\nInvocationID=\\nEnvironment=\\nControlGroup=/system.slice/vllm-fixture.service")\n')
    systemctl.chmod(0o700)
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.temp=str(root);run.unit='vllm-fixture.service';run.model='fixture';run.token='token'
    isolated_cwd=root/'cwd';isolated_cwd.mkdir()
    captured={}
    def remote_python(code,data,**kwargs):
        captured['code']=code
        env={**os.environ,'PATH':str(fakebin)+':'+os.environ.get('PATH','')}
        env.pop('PYTHONPATH',None)
        value=subprocess.run([sys.executable,'-I','-B','-c',code],input=json.dumps(data),text=True,
                             capture_output=True,env=env,cwd=isolated_cwd,check=True,timeout=5)
        return json.loads(value.stdout)
    run.python=remote_python
    result=run.daemon_binding(cleanup=True)
    assert result=={'absent':True,'exit_proven':True,'lease_id':'lease-1'}
    broken=captured['code'].replace("sys.path.insert(0,str(root/'runtime'))\n",'',1)
    env={**os.environ,'PATH':str(fakebin)+':'+os.environ.get('PATH','')}
    env.pop('PYTHONPATH',None)
    failed=subprocess.run([sys.executable,'-I','-B','-c',broken],input=json.dumps({'root':str(root),'unit':run.unit,'model':run.model,'token':run.token}),text=True,
                          capture_output=True,env=env,cwd=isolated_cwd,check=False,timeout=5)
    assert failed.returncode != 0


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
    native=json.loads(files[run.temp+'/native.json'])
    assert scheduler['state_db_path']==run.temp+'/ledger.sqlite'
    assert not any('INSERT' in text or 'create_lease' in text for name,text in files.items() if not name.endswith('.py') and '/deploy/' not in name)
    assert scheduler['placement_enabled'] and scheduler['model_actions_enabled'] and not scheduler['automation_enabled']
    assert set(scheduler['collectors']['models'])=={run.model}
    assert 'ledger.sqlite' not in files
    compile(files[run.temp+'/nvidia-smi'],'<scoped nvidia wrapper>','exec')
    assert profile['gpu']==2 and profile['control_instances']=={}
    wrapper=profile['wrapper_argv']
    assert '--journal-unit' not in wrapper
    assert wrapper[:2]==[run.config['wrapper_binary'],'serve']
    assert wrapper[wrapper.index('--')+1:wrapper.index('--')+4] == [run.config['scheduler_python'],'-B',run.temp+'/runtime/deploy/scheduler_action_smoke.py']
    assert shlex.split(native['models'][run.model]['cmd'])==wrapper
    assert shlex.split(native['models'][run.model]['cmdStop']) == [
        run.config['scheduler_python'], '-B', run.temp+'/runtime/deploy/scheduler_action_smoke.py',
        'stop', run.temp+'/profile.json', '${PID}',
    ]
    run.config.update(cold_route='scheduler_wake',startup_seconds=90)
    wake_files,wake_profile=smoke.artifacts(run,[9002,9003,9004],10*1024**3)
    wake_scheduler=json.loads(wake_files[run.temp+'/scheduler.json'])
    assert wake_scheduler['wake_timeout_seconds']==90
    assert wake_profile['cold_route']=='scheduler_wake' and wake_profile['cold_budget_seconds']==90


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


@pytest.mark.parametrize('scenario', ['success', 'wrapper_failure', 'sleep_unconfirmed', 'identity_changed'])
def test_stop_wrapper_uses_supported_sleep_and_proves_before_pidfd_signal(tmp_path, monkeypatch, scenario):
    pid=os.getpid(); process=Path('/proc')/str(pid)
    wrapper_binary=str((process/'exe').resolve())
    wrapper_argv=[value.decode() for value in (process/'cmdline').read_bytes().split(b'\0') if value]
    ticks=(process/'stat').read_text().rsplit(') ',1)[1].split()[19]
    marker=tmp_path/'launch-lease.json';marker.write_text(json.dumps({'lease_id':'lease-1'}))
    identity={'unit':'vllm-fixture.service','pid':pid,'start_ticks':ticks,'invocation_id':'a'*32}
    profile={'root':str(tmp_path),'token':'run-token','unit':'vllm-fixture.service',
             'model':'fixture','backend_url':'http://127.0.0.1:8101',
             'source_unit':'fixture-source.service','wrapper_binary':wrapper_binary,
             'wrapper_sha256':hashlib.sha256(Path(wrapper_binary).read_bytes()).hexdigest(),
             'wrapper_argv':wrapper_argv,'work_deadline':time.monotonic()+5}
    events=[]
    monkeypatch.setattr(smoke.lifecycle,'cgroup_owned',lambda *args:True)
    def observe_identity(*args, **kwargs):
        events.append('identity')
        if scenario == 'identity_changed' and 'sleep_confirm' in events:
            return {**identity, 'invocation_id': 'b'*32}
        return identity
    monkeypatch.setattr(smoke,'unit_identity',observe_identity)
    monkeypatch.setattr(smoke.os,'pidfd_open',lambda value:99)
    monkeypatch.setattr(smoke.os,'close',lambda value:events.append(('close',value)))
    def signal_bound(fd, sig):
        assert fd == 99 and sig == smoke.signal.SIGTERM
        events.append('pidfd')
    monkeypatch.setattr(smoke.signal,'pidfd_send_signal',signal_bound)
    class Native:
        def __init__(self,*args):pass
        def json(self,*args):
            events.append('sleep_confirm')
            return {'is_sleeping':scenario != 'sleep_unconfirmed'}
    import deploy.maintenance_native as native
    monkeypatch.setattr(native,'NativeHTTP',Native)
    def wrapper(argv,**kwargs):
        if argv[1]=='stop':raise AssertionError('unsupported stop command was used')
        assert argv[1:]==['sleep','--vllm-url',profile['backend_url']]
        events.append('sleep')
        return subprocess.CompletedProcess(argv,1 if scenario == 'wrapper_failure' else 0,'','')
    monkeypatch.setattr(smoke.subprocess,'run',wrapper)
    if scenario == 'success':
        assert smoke.stop_wrapper(profile,pid)==0
        assert events == ['identity', 'identity', 'sleep', 'sleep_confirm', 'identity', 'pidfd', ('close', 99)]
    else:
        message = {'wrapper_failure':'owned wrapper sleep failed',
                   'sleep_unconfirmed':'daemon sleep not confirmed',
                   'identity_changed':'daemon changed during sleep'}[scenario]
        with pytest.raises(smoke.EvidenceError, match=message):
            smoke.stop_wrapper(profile,pid)
        assert 'pidfd' not in events
        assert events[-1] == ('close', 99)


@pytest.mark.parametrize('key,value',[('startup_seconds',0),('startup_seconds',float('inf')),
                                      ('cold_start_cost_seconds',True),('cold_route','unsupported'),
                                      ('host_meminfo_path','/proc/meminfo')])
def test_invalid_action_mode_inputs_are_rejected_without_runner(tmp_path,key,value):
    config={'source':str(ROOT),'native_binary':'/native','wrapper_binary':'/wrapper','scheduler_python':'/python',
            'nvidia_smi':'/nvidia','host_meminfo_path':'/verified-host','native_binary_sha256':'a'*64,'wrapper_sha256':'b'*64}
    config[key]=value
    with pytest.raises(life.SmokeError):smoke.validate(config)


def test_http200_unknown_release_preserves_ledger():
    run=smoke.ActionRun.__new__(smoke.ActionRun);run.attempted=True;run.temp_created=True;run.preserve=False;run.units=[]
    run.unit='vllm-own.service';run.model='own';run.profile={'scheduler_url':'http://127.0.0.1:1'};run.log=lambda *a,**k:None
    run.container=lambda *a,**k:subprocess.CompletedProcess([],0,'not-found\n','')
    run.json_at=lambda url,path,*a,**k:({'schema_version':1,'sampled_at':time.time(),'errors':[],'leases':[{'model':'own','lease_id':'lease','status':'pending'}]} if path=='/v1/state' else {'status':'unknown'})
    run.python=lambda *a,**k:pytest.fail('unknown lease must be retained')
    with pytest.raises(life.SmokeError):run.cleanup_actions()
    assert run.preserve


class _EvidenceReader:
    def __init__(self, *args, **kwargs):
        self.callback = None
        self.pending = None

    def set_notify(self, callback):
        self.callback = callback

    def start(self):
        if self.callback:
            self.callback()

    def drain(self):
        if self.pending is not None:
            value, self.pending = self.pending, None
            return value
        return {'status': 'SSE connected', 'generation': 0, 'dropped': 0,
                'missed': 0, 'cursor': 0, 'events': []}

    def push(self, value):
        self.pending = value
        self.callback()

    def close(self):
        return True


def _action_evidence_fixture(response, *, operation='free', post_hook=None,
                             reader_type=_EvidenceReader):
    state = {'read_only': False, 'sampled_at': time.time(), 'errors': [],
             'models': [{'name': 'fixture', 'gpu': 0, 'unit': 'vllm-fixture.service',
                         'state': 'awake' if operation == 'free' else 'sleeping',
                         'resident_gb': 80}],
             'leases': [{'model': 'fixture', 'gpu': 0, 'unit': 'vllm-fixture.service',
                         'lease_id': 'lease-1', 'status': 'confirmed', 'budget_gb': 80}]}
    reader_box = {}
    class Reader(reader_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            reader_box['reader'] = self
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, payload=None):
            if method == 'GET':
                return state
            if isinstance(response, BaseException):
                raise response
            state['models'][0]['state'] = 'sleeping' if operation == 'free' else 'awake'
            if method == 'POST' and post_hook is not None:
                post_hook(reader_box['reader'], response)
            return response
    api = {'SchedulerClient': Client, 'EventReader': Reader}
    profile = {'scheduler_url': 'http://fixture', 'model': 'fixture', 'gpu': 0,
               'unit': 'vllm-fixture.service', 'token': 'token',
               'backend_url': 'http://127.0.0.1:8101'}
    return api, profile


@pytest.mark.parametrize('response,expected_response', [
    ({'status': 'partial', 'measurement_complete': True, 'freed_gb': 27.6,
      'slept': ['fixture'], 'stopped': [], 'error': 'transport_error'}, True),
    (TimeoutError('no response'), False),
])
def test_action_probe_failure_evidence_preserves_response_or_unknown(response, expected_response):
    api, profile = _action_evidence_fixture(response)
    with pytest.raises(smoke.EvidenceError) as exc:
        smoke.action_probe(api, profile, 'free', 'local-request',
                           deadline=time.monotonic() + 2,
                           identity_reader=lambda *args, **kwargs: {
                               'unit': 'vllm-fixture.service', 'pid': 1,
                               'start_ticks': '2', 'invocation_id': 'a' * 32})
    evidence = exc.value.evidence
    assert evidence['local_request_id'] == 'local-request'
    assert evidence['model'] == 'fixture'
    assert evidence['identity_checks']['before_account'] is True
    assert evidence['identity_checks']['before_unit']['invocation_id'] == 'a' * 32
    assert (evidence['response'] is not None) is expected_response
    assert evidence['client_returned_monotonic'] >= evidence['client_started_monotonic']


def test_wake_partial_response_preserves_failure_evidence():
    response = {'status': 'partial', 'ready': False, 'model': 'fixture',
                'error': 'transport_error'}
    api, profile = _action_evidence_fixture(response, operation='wake')
    with pytest.raises(smoke.EvidenceError, match='wake did not reach ready') as exc:
        smoke.action_probe(api, profile, 'wake', 'wake-partial',
                           deadline=time.monotonic() + 2,
                           identity_reader=lambda *args, **kwargs: {
                               'unit': 'vllm-fixture.service', 'pid': 1,
                               'start_ticks': '2', 'invocation_id': 'a' * 32})
    assert exc.value.evidence['response'] == response
    assert exc.value.evidence['identity_checks']['before_account'] is True
    assert exc.value.evidence['response_received'] is True


class _JSONResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


def _actual_client_http_error_fixture(body):
    state = {'read_only': False, 'sampled_at': time.time(), 'errors': [],
             'models': [{'name': 'fixture', 'gpu': 0, 'unit': 'vllm-fixture.service',
                         'state': 'awake', 'resident_gb': 80}],
             'leases': [{'model': 'fixture', 'gpu': 0, 'unit': 'vllm-fixture.service',
                         'lease_id': 'lease-1', 'status': 'confirmed', 'budget_gb': 80}]}
    def opener(request, timeout):
        if request.get_method() == 'GET':
            return _JSONResponse(json.dumps(state).encode())
        raise HTTPError(request.full_url, 409, 'Conflict', {}, BytesIO(body))
    class Client:
        def __new__(cls, url, timeout=10):
            return API['SchedulerClient'](url, timeout=timeout, opener=opener)
    return {'SchedulerClient': Client, 'EventReader': _EvidenceReader}, {
        'scheduler_url': 'http://fixture', 'model': 'fixture', 'gpu': 0,
        'unit': 'vllm-fixture.service', 'token': 'token',
        'backend_url': 'http://127.0.0.1:8101',
    }


@pytest.mark.parametrize('body,available,expected_response', [
    (b'{"error":"placement_busy"}', True, {'error': 'placement_busy'}),
    (b'["placement_busy"]', False, None),
    (b'7', False, None),
    (b'null', False, None),
    (b'not-json', False, None),
])
def test_real_scheduler_client_http_error_boundary(body, available, expected_response):
    api, profile = _actual_client_http_error_fixture(body)
    with pytest.raises(smoke.EvidenceError, match='HTTP error') as exc:
        smoke.action_probe(api, profile, 'free', 'real-http-error',
                           deadline=time.monotonic() + 2,
                           identity_reader=lambda *args, **kwargs: {
                               'unit': 'vllm-fixture.service', 'pid': 1,
                               'start_ticks': '2', 'invocation_id': 'a' * 32})
    evidence = exc.value.evidence
    assert evidence['http_status'] == 409
    assert evidence['response_received'] is True
    assert evidence['request_error_kind'] == 'http'
    assert evidence['response_payload_available'] is available
    assert evidence['response'] == expected_response
    assert evidence['response_parsed'] is (True if available else None)
    assert 'HTTP 409:' in evidence['transport_error_message']


def test_helper_receipt_serializes_real_client_http_error_evidence(tmp_path, monkeypatch):
    api, profile = _actual_client_http_error_fixture(b'["placement_busy"]')
    with pytest.raises(smoke.EvidenceError) as exc:
        smoke.action_probe(api, profile, 'free', 'real-http-receipt',
                           deadline=time.monotonic() + 2,
                           identity_reader=lambda *args, **kwargs: {
                               'unit': 'vllm-fixture.service', 'pid': 1,
                               'start_ticks': '2', 'invocation_id': 'a' * 32})
    evidence = exc.value.evidence
    root = tmp_path / 'run'; root.mkdir(); (root / 'owner').write_text('token')
    deadline = time.monotonic() + 2
    (root / 'request.json').write_text(json.dumps({
        'id': 'real-http-receipt', 'operation': 'free', 'output': 'result.json',
        'deadline': deadline}))
    cli = tmp_path / 'cli.py'; cli.write_text('')
    (root / 'profile.json').write_text(json.dumps({
        'root': str(root), 'token': 'token', 'control_instances': {},
        'cli': str(cli), 'model': 'fixture', 'work_deadline': deadline + 1}))
    monkeypatch.setattr(smoke, 'action_probe', lambda *args, **kwargs:
                        (_ for _ in ()).throw(smoke.EvidenceError(
                            'action request HTTP error', evidence=evidence)))
    assert smoke.helper_main(['request', str(root / 'profile.json'), 'request.json']) == 1
    receipt = json.loads((root / 'result.json').read_text())
    assert receipt['status'] == 'failed'
    assert receipt['evidence']['http_status'] == 409
    assert receipt['evidence']['response_payload_available'] is False
    assert receipt['evidence']['response_parsed'] is None
    assert receipt['evidence']['request_error_kind'] == 'http'


def test_post_response_stream_failure_preserves_response_and_passed_checks():
    response = {'status': 'complete', 'measurement_complete': True,
                'freed_gb': 27.6, 'slept': ['fixture'], 'stopped': []}
    api, profile = _action_evidence_fixture(
        response,
        post_hook=lambda reader, _response: reader.push({
            'status': 'SSE disconnected', 'generation': 0, 'dropped': 0,
            'missed': 0, 'cursor': 1, 'events': []}),
    )
    with pytest.raises(smoke.EvidenceError, match='SSE disconnected') as exc:
        smoke.action_probe(api, profile, 'free', 'stream-failure',
                           deadline=time.monotonic() + 2,
                           identity_reader=lambda *args, **kwargs: {
                               'unit': 'vllm-fixture.service', 'pid': 1,
                               'start_ticks': '2', 'invocation_id': 'a' * 32})
    evidence = exc.value.evidence
    assert evidence['response'] == response
    assert evidence['identity_checks']['sse_connected'] is True
    assert evidence['identity_checks'].get('result_event') is None
    assert evidence['identity_checks']['reader_closed'] is True


def test_post_response_identity_failure_preserves_response_and_checks():
    response = {'status': 'complete', 'measurement_complete': True,
                'freed_gb': 27.6, 'slept': ['fixture'], 'stopped': []}
    event = {'id': 1, 'kind': 'free_result', 'model': 'fixture',
             'detail': response}
    calls = []
    def identity(*args, **kwargs):
        calls.append(True)
        return {'unit': 'vllm-fixture.service', 'pid': 1,
                'start_ticks': 'changed' if len(calls) > 1 else '2',
                'invocation_id': 'a' * 32}
    api, profile = _action_evidence_fixture(
        response, post_hook=lambda reader, _response: reader.push({
            'status': 'SSE connected', 'generation': 0, 'dropped': 0,
            'missed': 0, 'cursor': 1, 'events': [event]}),
    )
    with pytest.raises(smoke.EvidenceError, match='daemon instance changed') as exc:
        smoke.action_probe(api, profile, 'free', 'identity-failure',
                           deadline=time.monotonic() + 2, identity_reader=identity)
    evidence = exc.value.evidence
    assert evidence['response'] == response
    assert evidence['identity_checks']['result_event'] == 1
    assert evidence['identity_checks']['after_account'] is True
    assert evidence['identity_checks'].get('unit_unchanged') is None


def test_reader_close_failure_does_not_mask_response_failure():
    class CloseFailureReader(_EvidenceReader):
        def close(self):
            raise RuntimeError('close failed')

    response = {'status': 'partial', 'measurement_complete': True,
                'freed_gb': 27.6, 'slept': ['fixture'], 'stopped': []}
    api, profile = _action_evidence_fixture(response, reader_type=CloseFailureReader)
    with pytest.raises(smoke.EvidenceError, match='free refused') as exc:
        smoke.action_probe(api, profile, 'free', 'close-failure',
                           deadline=time.monotonic() + 2,
                           identity_reader=lambda *args, **kwargs: {
                               'unit': 'vllm-fixture.service', 'pid': 1,
                               'start_ticks': '2', 'invocation_id': 'a' * 32})
    assert exc.value.evidence['response'] == response
    assert exc.value.evidence['reader_close_error_type'] == 'RuntimeError'


@pytest.mark.parametrize('evidence', [
    {'response': {'status': 'partial', 'measurement_complete': True,
                  'freed_gb': 27.6, 'slept': ['fixture'], 'stopped': [],
                  'error': 'transport_error'}, 'deadline': 10.0},
    {'response': None, 'deadline': 10.0, 'transport_error_type': 'TimeoutError'},
])
def test_helper_receipt_serializes_structured_action_failure(tmp_path, monkeypatch, evidence):
    root = tmp_path / 'run'; root.mkdir(); (root / 'owner').write_text('token')
    deadline = time.monotonic() + 2
    request = root / 'request.json'; request.write_text(json.dumps({
        'id': 'local-request', 'operation': 'free', 'output': 'result.json',
        'deadline': deadline}))
    cli = tmp_path / 'cli.py'; cli.write_text('')
    profile = root / 'profile.json'; profile.write_text(json.dumps({
        'root': str(root), 'token': 'token', 'control_instances': {},
        'cli': str(cli), 'model': 'fixture', 'work_deadline': deadline + 1}))
    monkeypatch.setattr(smoke, 'action_probe', lambda *args, **kwargs:
                        (_ for _ in ()).throw(smoke.EvidenceError(
                            'free refused, partial or unmeasured',
                            evidence={'operation': 'free', 'local_request_id': 'local-request',
                                      'model': 'fixture', **evidence})))
    assert smoke.helper_main(['request', str(profile), 'request.json']) == 1
    receipt = json.loads((root / 'result.json').read_text())
    assert receipt['status'] == 'failed'
    assert receipt['evidence']['local_request_id'] == 'local-request'
    assert receipt['evidence']['response'] == evidence['response']
    if evidence['response'] is None:
        assert receipt['evidence']['transport_error_type'] == 'TimeoutError'


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
