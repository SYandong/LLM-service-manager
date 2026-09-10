#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Bounded native source stage/file rollback fixture; no GPU or lease creation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(args):
    from deploy.bootstrap_native import checksum, snapshot, unit_candidate
    from deploy.maintenance_native import NativeHTTP, private_create
    from deploy.maintenance_executor import digest
    if args.dry_run:
        print(json.dumps({'dry_run':True,'effects':False,'scope':'new owned native unit, false-preload fixture, staged files and rollback only'}));return
    if os.geteuid()!=0:raise RuntimeError('root required for explicitly isolated unit fixture')
    if not 20<=args.seconds<=120:raise ValueError('seconds must be20..120')
    binary=Path(args.binary).resolve(strict=True)
    if checksum(binary.read_bytes())!=args.binary_sha256:raise ValueError('native binary pin mismatch')
    output=Path(args.output);output.mkdir(mode=0o700,exist_ok=False)
    work=Path(tempfile.mkdtemp(prefix='llmsvc-bootstrap-cpu-'));artifacts=work/'artifacts';artifacts.mkdir(mode=0o700)
    state=work/'records';state.mkdir(mode=0o700);native_state=work/'native-state';native_state.mkdir(mode=0o700)
    token=uuid.uuid4().hex;model='bootstrap-'+token[:12];unit='llmsvc-bootstrap-fixture-'+token+'.service';backend='vllm-'+model+'.service'
    unit_path=Path(args.unit_dir)/unit;config=work/'native.json';native_profile_path=work/'native-profile.json';env=native_state/'launch.env'
    start=time.monotonic();hard=start+args.seconds;deadline=hard-min(15,args.seconds/3);records=[];created=False;error=None
    def command(argv,check=True,end=None):
        remaining=(end or deadline)-time.monotonic()
        if remaining<=0:raise TimeoutError('bootstrap fixture deadline')
        return subprocess.run(argv,text=True,capture_output=True,timeout=min(10,remaining),check=check)
    def put(path,data,mode=0o600):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin='http://127.0.0.1:'+str(port)
    # The preserved default deliberately fails without starting any backend.
    # This exercises first-use staging, not model inference or account acceptance.
    data={'globalTTL':0,'healthCheckTimeout':1,'performance':{'disabled':True},
          'store':{'path':str(work/'activity.sqlite')},'hooks':{'on_startup':{'preload':[model]}},
          'models':{model:{'cmd':'/bin/false','cmdStop':'/bin/true','proxy':'http://127.0.0.1:1','aliases':['synthetic-default']}}}
    put(config,json.dumps(data).encode());launcher=work/'launcher';put(launcher,b'original fixture launcher\n',0o755)
    daemon_env=work/'daemon.env';put(daemon_env,b'FIXTURE_ONLY=1\n')
    argv=[str(binary),'-config',str(config),'-listen','127.0.0.1:'+str(port)]
    definition=('[Unit]\nDescription=Owned bootstrap source staging fixture\nStartLimitIntervalSec=120\nStartLimitBurst=1\n[Service]\nType=simple\nRestart=always\nKillMode=control-group\n'
                +'RuntimeMaxSec='+str(args.seconds)+'\nTimeoutStopSec=2\nWorkingDirectory='+str(work)+'\nExecStart='+shlex.join(argv)+'\n').encode()
    executor=Path(__file__).with_name('maintenance_executor.py');helper=Path(__file__).with_name('maintenance_native.py')
    baseline={'unit':unit,'fragment_path':str(unit_path),'fragment_sha256':checksum(definition),
              'native_binary':str(binary),'native_binary_sha256':args.binary_sha256,'native_config_path':str(config),'native_config_dir':str(work),
              'native_origin':origin,'listen_host':'127.0.0.1','listen_port':port,'native_probe_origins':[origin],'source_auxiliaries':[]}
    new_definition=unit_candidate(definition,str(env))
    target={**baseline,'native_adapter':True,'fragment_sha256':checksum(new_definition),'state_dir':str(native_state),
            'launch_environment_file':str(env),'helper_python':args.python,'helper_program_sha256':checksum(helper.read_bytes()),
            'models':{model:{'unit':backend,'backend_origin':'http://127.0.0.1:1','process_argv':['/bin/false']}}}
    candidate=json.loads(json.dumps(data));candidate['models'][model]['cmdStop']=shlex.join(
        [args.python,'-B',str(helper),'helper','--profile',str(native_profile_path),'--model',model,'--pid','${PID}'])
    result={'generated_by':'Codex / gpt-6-astra','scope':'actual pinned native source/systemd stage plus file rollback; no default account/inference acceptance',
            'native_binary_sha256':args.binary_sha256,'unit':unit,'backend_unit_never_started':backend,'work':str(work),
            'records':records,'GPU_used':False,'leases_created':0,'core_claim':'synthetic request fence only; real core schema7 integration remains separate',
            'transport':'Actual correlated CLI envelope; old192 core operation allowlist has no bootstrap verbs, final core201 integration separate'}
    try:
        for name in (unit,backend):
            assert command(['systemctl','show',name,'-p','LoadState','--value']).stdout.strip()=='not-found'
        put(unit_path,definition);created=True
        command(['systemctl','daemon-reload']);command(['systemctl','start',unit])
        while time.monotonic()<deadline:
            try:
                observed=NativeHTTP(origin).snapshot(min(deadline,time.monotonic()+2))
                if observed.states=={model:'stopped'} and not observed.requests:break
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(.05)
        else:raise TimeoutError('native false-preload settlement')
        payloads={'unit_fragment':(unit_path,new_definition,0o600),'native_config':(config,json.dumps(candidate).encode(),0o600),
                  'launcher':(launcher,b'reviewed fixture launcher bytes\n',0o755),
                  'launcher_config':(work/'launcher.json',json.dumps({'systemd_run':{'environment_file':str(daemon_env)}}).encode(),0o600),
                  'native_profile':(native_profile_path,json.dumps(target).encode(),0o600),'attempt_environment':(env,b'',0o600)}
        rows={}
        for kind,(path,content,mode) in payloads.items():
            src=artifacts/kind;put(src,content)
            rows[kind]={'source':str(src),'target':str(path),'before':snapshot(path),
                        'after':{'sha256':checksum(content),'mode':mode,'uid':0,'gid':os.getegid()}}
        manifest={'schema_version':1,'state_dir':str(state),'artifact_root':str(artifacts),'default_model':model,'default_unit':backend,
                  'source_profile':baseline,'files':rows,'native_environment_files':[],
                  'daemon_environment_file':{'path':str(daemon_env),'sha256':checksum(daemon_env.read_bytes())}}
        manifest_path=work/'manifest.json';private_create(manifest_path,manifest)
        dispatch=work/'dispatch.json';pin=checksum(manifest_path.read_bytes());private_create(dispatch,{'bootstrap_adapter':True,'manifest_path':str(manifest_path),'manifest_sha256':pin})
        context={'bootstrap_id':token,'transaction_id':token,'manifest_sha256':pin,'default_model':model,'default_unit':backend,
                 'account':None,'launch_submitted':False,'effects':{}}
        def request(op):
            sent=time.monotonic()
            envelope={'operation':op,'context':context,'timeout_seconds':min(15,deadline-sent)}
            envelope['request_id']=digest(envelope)
            completed=subprocess.run([args.python,'-B',str(executor),'--profile',str(dispatch),op],
                input=json.dumps(envelope),text=True,capture_output=True,timeout=max(.01,deadline-time.monotonic()))
            (output/(op+'.stderr.private')).write_text(completed.stderr)
            (output/(op+'.stderr.private')).chmod(0o600)
            if completed.returncode:raise RuntimeError('bootstrap CLI failed: '+completed.stderr.strip())
            value=json.loads(completed.stdout)
            assert value['request_id']==envelope['request_id'] and value['transaction_id']==token
            assert sent<=value['observed_at']<=time.monotonic()
            records.append({'operation':op,'result':value});return value
        first=request('bootstrap_preflight');assert first['ready'] and first['default_preload_preserved']
        context['source_identity']=first['identity'];context['effects']['bootstrap_stage']={'submitted':True,'acknowledged':False}
        staged=request('bootstrap_stage');assert staged['source_absent'] and staged['helpers_settled'] and staged['staged']
        assert staged['in_flight'] is None and not staged['default_confirmed']
        observed=request('bootstrap_observe');assert observed['staged'] and observed['source_absent']
        context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
        restored=request('bootstrap_rollback');assert restored['rolled_back'] and restored['source_absent'] and not restored['old_source_restarted']
        assert all(snapshot(row['target'])==row['before'] for row in rows.values())
        result['status']='PASS'
    except BaseException as exc:
        result['status']='FAIL';error={'type':type(exc).__name__,'message':str(exc)}
    finally:
        if created:
            stop=command(['systemctl','stop',unit],check=False,end=hard)
            props=command(['systemctl','show',unit,'-p','MainPID','-p','ControlGroup','-p','Job'],end=hard).stdout
            assert 'MainPID=0' in props and 'ControlGroup=\n' in props and 'Job=\n' in props,props
            # Remove only the definition created by this run, after exact bytes.
            assert checksum(unit_path.read_bytes()) in (checksum(definition),checksum(new_definition))
            unit_path.unlink();command(['systemctl','daemon-reload'],end=hard)
            command(['systemctl','reset-failed',unit],check=False,end=hard)
            result['cleanup']={'source_stopped':stop.returncode==0,'no_main_pid_or_cgroup_or_job':True,'unit_file_removed':not unit_path.exists()}
        result['seconds']=time.monotonic()-start;result['error']=error
        private_create(output/'receipt.private.json',result)
    print(json.dumps({'status':result['status'],'seconds':result['seconds'],'receipt':str(output/'receipt.private.json'),'error':error}))
    if error:raise SystemExit(1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',required=True);parser.add_argument('--binary-sha256',required=True)
    parser.add_argument('--python',default=sys.executable);parser.add_argument('--unit-dir',default='/run/systemd/system')
    parser.add_argument('--output',required=True);parser.add_argument('--seconds',type=float,default=45)
    parser.add_argument('--dry-run',action='store_true')
    run(parser.parse_args())


if __name__=='__main__':main()
