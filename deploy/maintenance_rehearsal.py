#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Bounded native/systemd CPU rehearsal; all units/files/ports are unique and owned."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def backend(work,port):
    state={'sleeping':False}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,code,value):
            data=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            self.reply(200,{'is_sleeping':state['sleeping']} if self.path=='/is_sleeping' else {'ok':True})
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            if self.path.startswith('/sleep'):
                mode=(work/'mode').read_text().strip()
                if mode in ('delayed','failed'):(work/'wrapper.exit').touch()
                if mode=='failed':self.reply(500,{'error':'synthetic helper failure'});return
                if mode=='delayed':
                    (work/'sleep.entered').touch();deadline=time.monotonic()+60
                    while not (work/'sleep.release').exists() and time.monotonic()<deadline:time.sleep(.01)
                    if not (work/'sleep.release').exists():self.reply(500,{'error':'fixture deadline'});return
                state['sleeping']=True;self.reply(200,{'ok':True})
            else:self.reply(200,{'id':'fixture','choices':[{'message':{'role':'assistant','content':'CPU fixture'}}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
    http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()


def wrapper(work):
    while not (work/'wrapper.exit').exists():time.sleep(.02)


def rehearsal(args):
    from llmsvc.maintenance import CommandBackend
    from deploy.maintenance_native import NativeHTTP,private_create
    if args.dry_run:
        print(json.dumps({'dry_run':True,'scope':'unique CPU native/backend/helper units, temporary state/config and loopback only','maximum_seconds':args.seconds}));return
    if os.geteuid()!=0:raise RuntimeError('isolated systemd rehearsal requires root; no automatic privilege change')
    if not 20<=args.seconds<=180:raise ValueError('seconds must be20..180')
    binary=Path(args.binary).resolve(strict=True);pin=hashlib.sha256(binary.read_bytes()).hexdigest()
    if pin!=args.binary_sha256:raise ValueError('native binary pin mismatch')
    output=Path(args.output);output.mkdir(mode=0o700,parents=False,exist_ok=False)
    work=Path(tempfile.mkdtemp(prefix='llmsvc-native-cpu-'));state=work/'state';state.mkdir(mode=0o700)
    (state/'launch.env').write_text('');(state/'launch.env').chmod(0o600);(work/'mode').write_text(args.scenario)
    token=uuid.uuid4().hex;model='fixture'+token[:8];source_unit='llmsvc-ops-native-'+token+'.service';backend_unit='vllm-ops-backend-'+token+'.service'
    unit_path=Path(args.unit_dir)/source_unit;units=[source_unit,backend_unit];context={};records=[];start=time.monotonic();hard_deadline=start+args.seconds;deadline=hard_deadline-min(40,args.seconds/2)
    def run(argv,timeout=10,check=True):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('rehearsal deadline')
        return subprocess.run(argv,capture_output=True,text=True,timeout=min(timeout,remaining),check=check)
    def port():
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
    native_port,backend_port=port(),port();lease=uuid.uuid4().hex
    executor=Path(__file__).with_name('maintenance_executor.py');helper=Path(__file__).with_name('maintenance_native.py');profile_path=work/'profile.json';config=work/'native.yaml'
    source_argv=[str(binary),'-config',str(config),'-listen','127.0.0.1:'+str(native_port)]
    idle_argv=[args.python,'-B',str(Path(__file__).resolve()),'--wrapper','--work',str(work)]
    data={'globalTTL':0,'healthCheckTimeout':3,'performance':{'disabled':True},'macros':{'llmsvc_reload_generation':'gen_'+uuid.uuid4().hex},'store':{'path':str(work/'activity.sqlite')},
          'models':{model:{'cmd':shlex.join(idle_argv),'cmdStop':shlex.join([args.python,'-B',str(helper),'helper','--profile',str(profile_path),'--model',model,'--pid','${PID}']),
                           'proxy':'http://127.0.0.1:'+str(backend_port)}}}
    config.write_text(json.dumps(data,sort_keys=True));config.chmod(0o600);base=config.read_bytes()
    definition='[Unit]\nDescription=Owned isolated native maintenance CPU fixture\n[Service]\nType=simple\nRestart=no\nKillMode=control-group\nRuntimeMaxSec='+str(args.seconds)+'\nEnvironmentFile='+str(state/'launch.env')+'\nWorkingDirectory='+str(work)+'\nExecStart='+shlex.join(source_argv)+'\n'
    profile={'native_adapter':True,'unit':source_unit,'fragment_path':str(unit_path),'fragment_sha256':hashlib.sha256(definition.encode()).hexdigest(),
             'native_binary':str(binary),'native_binary_sha256':pin,'native_config_dir':str(work),'native_config_path':str(config),
             'native_origin':'http://127.0.0.1:'+str(native_port),'listen_host':'127.0.0.1','listen_port':native_port,
             'state_dir':str(state),'launch_environment_file':str(state/'launch.env'),'helper_python':args.python,
             'helper_program_sha256':hashlib.sha256(helper.read_bytes()).hexdigest(),'helper_timeout_seconds':20,
             'models':{model:{'unit':backend_unit,'backend_origin':'http://127.0.0.1:'+str(backend_port),'process_argv':idle_argv}}}
    private_create(profile_path,profile)
    diagnostic=work/'adapter-diagnostic.py'
    diagnostic.write_text("""# Generated-By: Codex / gpt-6-astra
import json,subprocess,sys,uuid
from pathlib import Path
raw=sys.stdin.buffer.read(4194305);value=json.loads(raw);out=Path(sys.argv[1]);name=value['operation']+'-'+uuid.uuid4().hex
run=subprocess.run(sys.argv[2:],input=raw,capture_output=True,timeout=min(180,value['timeout_seconds']+1))
(out/(name+'.stderr')).write_bytes(run.stderr)
(out/(name+'.stdout')).write_bytes(run.stdout)
sys.stdout.buffer.write(run.stdout);sys.stderr.buffer.write(run.stderr);raise SystemExit(run.returncode)
""")
    client=CommandBackend([args.python,'-B',str(diagnostic),str(output),args.python,'-B',str(executor),'--profile',str(profile_path)])
    def request(operation,extra=None):
        current=json.loads(json.dumps(context));current.update(extra or {})
        if operation in ('stop_candidate','observe_candidate_absent') and current.get('new_scope'):
            current['observed_scope']=current['new_scope'];current['observed_actors']=current['new_actors']
        before=time.monotonic();value=client.request(operation,current,deadline=deadline)
        assert before<=value['observed_at']<=time.monotonic()
        records.append({'operation':operation,'result':value,'at':time.monotonic()});return value
    def effect(operation):
        context['effects'][operation]={'submitted':True,'acknowledged':False};value=request(operation)
        assert value['accepted'] is True;context['effects'][operation]['acknowledged']=True;return value
    def wait(operation,predicate):
        while time.monotonic()<deadline:
            value=request(operation)
            if predicate(value):return value
            time.sleep(.05)
        raise TimeoutError(operation)
    result={'generated_by':'Codex / gpt-6-astra','scenario':args.scenario,'native_sha256':pin,'units':units,'work':str(work),'records':records,'GPU_used':False,'production_config_or_units_touched':False}
    try:
        assert not unit_path.exists()
        for unit in units:
            found=run(['systemctl','show',unit,'-p','LoadState','--value']).stdout.strip()
            assert found=='not-found',('preexisting unit',unit)
        fd=os.open(unit_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(definition)
        run(['systemctl','daemon-reload'])
        run(['systemd-run','--quiet','--unit='+backend_unit,'--property=Restart=no','--property=RuntimeMaxSec='+str(args.seconds),
             '--setenv=LLMSVC_LEASE_ID='+lease,'--setenv=CUDA_VISIBLE_DEVICES=0','--',args.python,'-B',str(Path(__file__).resolve()),'--backend','--work',str(work),'--port',str(backend_port)])
        run(['systemctl','start',source_unit])
        http=NativeHTTP(profile['native_origin'])
        while True:
            try:
                http.snapshot(min(deadline,time.monotonic()+3))
                break
            except (OSError,ValueError,RuntimeError):
                if time.monotonic()+.1>=deadline:raise
                time.sleep(.05)
        # An OpenAI request must select this fixture model; perform it explicitly.
        import urllib.request
        req=urllib.request.Request(profile['native_origin']+'/v1/chat/completions',data=json.dumps({'model':model,'messages':[{'role':'user','content':'CPU fixture'}]}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as response:assert response.status==200;response.read(65536)
        account={'lease_id':lease,'model':model,'gpu':0,'status':'confirmed'}
        context={'transaction_id':uuid.uuid4().hex,'operation_id':uuid.uuid4().hex,'accounts':[[account,backend_unit]],'current_accounts':[[account,backend_unit]],
                 'effects':{},'removed_models':[],'new_identity':None,'rollback_identity':None,'exclusion_method':'stop_instance'}
        observed=request('inspect');context.update(old_identity=observed['identity'],observed_scope=observed['scope'],observed_actors=observed['actors'],backend_bindings=observed['backend_bindings'],base_sha256=hashlib.sha256(base).hexdigest())
        assert request('preflight')['ready'] is True
        effect('stop_old')
        if args.scenario=='delayed':
            early=wait('observe_old',lambda x:x['old_settled'])
            assert early['helpers_settled'] is False
            (work/'sleep.release').touch()
        old=wait('observe_old',lambda x:x['old_settled'] and (x['helpers_settled'] or args.scenario=='failed' and any(r.get('result')=='exit-code' for r in x['helper_observations'])))
        if args.scenario=='failed':
            assert not old['helpers_settled'] and config.read_bytes()==base
            result['status']='expected_helper_failure_fenced';return
        assert old['identity'] is None and old['helpers_settled'] and old['backends_confirmed'] and old['ingress_state']=='excluded'
        data['macros']['llmsvc_reload_generation']='gen_'+uuid.uuid4().hex
        if args.scenario=='remove-model':data['models']={};context['removed_models']=[model]
        config.write_text(json.dumps(data,sort_keys=True))
        context.update(candidate_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),generation=data['macros']['llmsvc_reload_generation'])
        if args.scenario=='start-refused':
            holder=socket.socket();holder.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);holder.bind(('127.0.0.1',native_port));holder.listen()
            refused=False
            try:
                try:effect('start_candidate')
                except Exception:refused=True
            finally:holder.close()
            assert refused
            absent=request('observe_candidate_absent');assert absent['attempt_settled'] and absent['attempt_bound'] and absent['identity'] is None
            config.write_bytes(base);restored=effect('start_base');context['rollback_identity']=restored['identity'];observed=request('inspect');context.update(rollback_scope=observed['scope'],rollback_actors=observed['actors'])
            verified=request('observe_base');assert all(verified[k] is True for k in ('old_settled','helpers_settled','attempt_settled','configuration_confirmed','backends_confirmed','cleanup_confirmed'))
            result['status']='passed_refused_start_then_exact_base_rollback';return
        fresh=effect('start_candidate');context['new_identity']=fresh['identity'];observed=request('inspect');context.update(new_scope=observed['scope'],new_actors=observed['actors'])
        if args.scenario=='remove-model':
            binding=context['backend_bindings'][0];key='stop_model:'+model
            context['effects'][key]={'submitted':True,'acknowledged':False}
            value=request('stop_model',{'model':model,'unit':backend_unit,'lease_id':lease,'invocation_id':binding['invocation_id']})
            assert value['accepted'];context['effects'][key]['acknowledged']=True
            while not request('observe_unit',{'model':model})['exited']:time.sleep(.05)
            context['current_accounts']=[]  # Native proof precedes the fixture's ledger-view change.
            result['model_unit_exit_before_account_release']=True
        verified=request('observe_candidate');assert all(verified[k] is True for k in ('old_settled','helpers_settled','configuration_confirmed','backends_confirmed','cleanup_confirmed','attempt_bound'))
        assert verified['identity']!=context['old_identity'] and verified['generation']==context['generation']
        # Roll back this exact successful candidate, without replaying any effect.
        effect('stop_candidate');absent=wait('observe_candidate_absent',lambda x:x.get('attempt_settled'))
        assert absent['attempt_identity']==context['new_identity'] and absent['identity'] is None
        config.write_bytes(base);restored=effect('start_base');context['rollback_identity']=restored['identity'];observed=request('inspect');context.update(rollback_scope=observed['scope'],rollback_actors=observed['actors'])
        verified=request('observe_base');assert all(verified[k] is True for k in ('old_settled','helpers_settled','attempt_settled','configuration_confirmed','backends_confirmed','cleanup_confirmed'))
        assert restored['identity'] not in (context['old_identity'],context['new_identity']) and config.read_bytes()==base
        result['status']='passed_native_forward_and_exact_base_rollback'
    except BaseException as exc:
        result['status']='failed';result['error']={'type':type(exc).__name__,'message':str(exc)}
        raise
    finally:
        # Only names derived from this freshly owned private state can be removed.
        helpers=[]
        for record in state.glob('helper-*.json'):
            try:
                value=json.loads(record.read_text())
                if value.get('unit','').startswith('llmsvc-maint-helper-'):helpers.append(value['unit'])
            except (ValueError,OSError):pass
        cleanup=[]
        def clean(argv,limit=3):
            left=hard_deadline-time.monotonic()
            if left<=0:return None
            try:return subprocess.run(argv,capture_output=True,text=True,timeout=min(limit,left))
            except subprocess.TimeoutExpired:return None
        for unit in [source_unit,*helpers,backend_unit]:
            done=clean(['systemctl','stop',unit],5)
            code=done.returncode if done is not None else 'owned_cleanup_timeout'
            if done is None:clean(['systemctl','kill','--kill-who=all','--signal=KILL',unit])
            clean(['systemctl','reset-failed',unit])
            status=clean(['systemctl','show',unit,'-p','MainPID,ActiveState,ControlGroup'])
            text=status.stdout if status is not None else ''
            fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
            gone=fields.get('MainPID')=='0' and fields.get('ControlGroup')=='' and fields.get('ActiveState') in ('inactive','failed')
            cleanup.append({'unit':unit,'stop_returncode':code,'after':text,'no_process_or_cgroup':gone})
        if unit_path.exists() and hashlib.sha256(unit_path.read_bytes()).hexdigest()==profile['fragment_sha256']:unit_path.unlink()
        clean(['systemctl','daemon-reload'])
        result.update(elapsed_seconds=time.monotonic()-start,cleanup=cleanup,final_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),base_sha256=hashlib.sha256(base).hexdigest())
        logs={}
        for unit in [source_unit,*helpers,backend_unit]:
            got=clean(['journalctl','-u',unit,'--no-pager','-n','80','-o','cat']);logs[unit]=got.stdout if got is not None else 'deadline: journal not collected'
        (output/'journal.private.json').write_text(json.dumps(logs,indent=2));(output/'journal.private.json').chmod(0o600)
        if not all(row['no_process_or_cgroup'] for row in cleanup):
            result['status']='cleanup_unconfirmed'
        (output/'receipt.private.json').write_text(json.dumps(result,indent=2));(output/'receipt.private.json').chmod(0o600)
        print(json.dumps({'status':result['status'],'seconds':result['elapsed_seconds'],'receipt':str(output/'receipt.private.json'),'error':result.get('error')}))
        if result['status']=='cleanup_unconfirmed':raise RuntimeError('owned fixture cleanup unconfirmed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',action='store_true');parser.add_argument('--wrapper',action='store_true');parser.add_argument('--work');parser.add_argument('--port',type=int)
    parser.add_argument('--binary');parser.add_argument('--binary-sha256');parser.add_argument('--python',default=sys.executable);parser.add_argument('--output')
    parser.add_argument('--unit-dir',default='/run/systemd/system');parser.add_argument('--seconds',type=int,default=120);parser.add_argument('--scenario',choices=['success','delayed','failed','start-refused','remove-model'],default='success');parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    if args.backend:backend(Path(args.work),args.port)
    elif args.wrapper:wrapper(Path(args.work))
    else:rehearsal(args)


if __name__=='__main__':main()
