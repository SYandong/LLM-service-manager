#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Isolated scheduler-action mode for lifecycle_smoke, not a production driver."""
import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import math
import os
import signal
from pathlib import Path
import runpy
import re
import shlex
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlsplit
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import lifecycle_smoke as lifecycle


class EvidenceError(RuntimeError):
    def __init__(self, message, *, evidence=None):
        super().__init__(message)
        self.evidence = evidence


class _CleanupObservationUnknown(RuntimeError):
    """A bounded post-exit read is inconclusive and may be retried read-only."""

    def __init__(self, message, *, sampled_at=None, requires_publication=False):
        super().__init__(message)
        self.sampled_at=sampled_at
        self.requires_publication=requires_publication


def validate(config):
    for key in ('native_binary', 'wrapper_binary', 'scheduler_python', 'host_meminfo_path', 'nvidia_smi'):
        value=config.get(key)
        if not isinstance(value,str) or not Path(value).is_absolute() or any(c.isspace() for c in value):
            raise lifecycle.SmokeError('scheduler action mode needs absolute '+key)
    if config['host_meminfo_path'] in ('/proc/meminfo','/sys/fs/cgroup/memory.stat'):
        raise lifecycle.SmokeError('independently verified live host meminfo source required')
    for key in ('native_binary_sha256','wrapper_sha256'):
        value=config.get(key,'')
        if len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise lifecycle.SmokeError('missing reviewed '+key)
    startup=config.get('startup_seconds',150)
    cost=config.get('cold_start_cost_seconds',120)
    if type(startup) not in (int,float) or not math.isfinite(startup) or not 1<=startup<=150:
        raise lifecycle.SmokeError('startup_seconds must be finite and1..150')
    if type(cost) not in (int,float) or not math.isfinite(cost) or cost<=0:
        raise lifecycle.SmokeError('cold_start_cost_seconds is a positive policy input')
    root=Path(config['source'])
    for name in ('cli/llm','deploy/vllm-launch','deploy/maintenance_native.py','deploy/maintenance_executor.py'):
        if not (root/name).is_file():raise lifecycle.SmokeError('reviewed source missing '+name)
    route=config.get('cold_route','native_chat')
    if route not in ('native_chat','scheduler_wake'):
        raise lifecycle.SmokeError('cold_route must be native_chat or scheduler_wake')


def _parse_environment(raw):
    pairs = [item.split('=', 1) for item in shlex.split(raw or '') if '=' in item]
    if len(pairs) != len({name for name, _ in pairs}):
        raise EvidenceError('ambiguous unit environment')
    return dict(pairs)


def _parse_process_environment(raw):
    pairs=[]
    for item in raw.split(b'\0'):
        if not item:continue
        try:name,value=item.split(b'=',1)
        except ValueError:raise EvidenceError('process environment malformed')
        pairs.append((name.decode(),value.decode()))
    if len(pairs)!=len({name for name,_ in pairs}):raise EvidenceError('ambiguous process environment')
    return dict(pairs)


def _cgroup_matches(expected, proc_cgroup):
    expected=expected.rstrip('/')
    paths=[]
    for line in proc_cgroup.splitlines():
        parts=line.split(':',2)
        if len(parts)==3:paths.append(parts[2].rstrip('/'))
    return any(path==expected or path.startswith(expected+'/') for path in paths)


def validate_unit_observation(unit, token, values, proc_env, proc_start_ticks,
                              proc_cgroup, *, lease=None, model=None,
                              expected=None, control=None):
    """Validate a live unit instance; EnvironmentFile text is never authority."""
    control = unit.startswith('llmsvc-ops-action-') if control is None else control
    if values.get('Id') != unit:
        raise EvidenceError('unit identity mismatch')
    try:
        pid = int(values.get('MainPID', '0'))
    except (TypeError, ValueError) as exc:
        raise EvidenceError('unit PID invalid') from exc
    invocation = values.get('InvocationID', '')
    cgroup = values.get('ControlGroup', '')
    if pid <= 0:
        raise EvidenceError('unit exited before identity proof')
    if not re.fullmatch('[0-9a-f]{32}', invocation) or int(invocation, 16) == 0:
        raise EvidenceError('unit instance unknown')
    if not cgroup or not _cgroup_matches(cgroup,proc_cgroup):
        raise EvidenceError('unit cgroup mismatch')
    if not isinstance(proc_start_ticks, str) or not re.fullmatch('[0-9]+', proc_start_ticks):
        raise EvidenceError('unit start identity unknown')
    if control:
        if proc_env.get('LLMSVC_OPS_RUN_ID') != token:
            raise EvidenceError('control unit owner mismatch')
    else:
        expected_model = model or (unit[:-8] if unit.endswith('.service') else unit)
        if model is None and not expected_model.startswith('vllm-'):
            raise EvidenceError('daemon model binding missing')
        if model is None:
            expected_model = expected_model[5:]
        if proc_env.get('LLMSVC_MODEL') != expected_model:
            raise EvidenceError('daemon model environment mismatch')
        if lease is not None and proc_env.get('LLMSVC_LEASE_ID') != lease:
            raise EvidenceError('unit lease mismatch')
        if not proc_env.get('CUDA_VISIBLE_DEVICES'):
            raise EvidenceError('daemon GPU environment missing')
    declared = _parse_environment(values.get('Environment', ''))
    for key in ('LLMSVC_OPS_RUN_ID', 'LLMSVC_LEASE_ID', 'LLMSVC_MODEL', 'CUDA_VISIBLE_DEVICES'):
        if key in declared and declared.get(key) != proc_env.get(key):
            raise EvidenceError('unit environment differs from process environment')
    identity = {'unit': unit, 'pid': pid, 'start_ticks': proc_start_ticks,
                'invocation_id': invocation}
    if expected is not None and identity != expected:
        raise EvidenceError('unit instance changed')
    return identity


def unit_identity(unit, token, *, lease=None, deadline=None, origin=None,
                  model=None, expected=None, control=None, proc_root=Path('/proc')):
    limit=lifecycle.remaining(deadline or time.monotonic()+3,3)
    command=['systemctl','show',unit,'--property=Id,MainPID,InvocationID,Environment,ControlGroup']
    result=subprocess.run(command,capture_output=True,text=True,timeout=limit,check=True)
    values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    pid=int(values.get('MainPID','0'))
    if pid <= 0:raise EvidenceError('unit exited before identity proof')
    process=Path(proc_root)/str(pid)
    try:
        proc_env=_parse_process_environment(process.joinpath('environ').read_bytes())
        ticks=process.joinpath('stat').read_text().rsplit(') ',1)[1].split()[19]
        proc_cgroup=process.joinpath('cgroup').read_text()
    except (OSError, UnicodeError, IndexError) as exc:
        raise EvidenceError('process identity unavailable') from exc
    identity=validate_unit_observation(unit,token,values,proc_env,ticks,proc_cgroup,
                                       lease=lease,model=model,expected=expected,control=control)
    remaining=lifecycle.remaining(deadline or time.monotonic()+3,3)
    result=subprocess.run(command,capture_output=True,text=True,timeout=remaining,check=True)
    current=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    for key in ('Id','MainPID','InvocationID','ControlGroup'):
        if current.get(key)!=values.get(key):raise EvidenceError('unit instance changed')
    pid2=int(current.get('MainPID','0'))
    if pid2!=pid:raise EvidenceError('unit instance changed')
    process2=Path(proc_root)/str(pid2)
    try:
        proc_env2=_parse_process_environment(process2.joinpath('environ').read_bytes())
        ticks2=process2.joinpath('stat').read_text().rsplit(') ',1)[1].split()[19]
        proc_cgroup2=process2.joinpath('cgroup').read_text()
    except (OSError, UnicodeError, IndexError) as exc:
        raise EvidenceError('process identity unavailable') from exc
    if (ticks2!=ticks or proc_cgroup2!=proc_cgroup
            or any(proc_env2.get(key)!=proc_env.get(key) for key in ('LLMSVC_MODEL','LLMSVC_LEASE_ID','LLMSVC_OPS_RUN_ID','CUDA_VISIBLE_DEVICES'))):
        raise EvidenceError('unit instance changed')
    validate_unit_observation(unit,token,current,proc_env,ticks2,proc_cgroup2,
                              lease=lease,model=model,expected=identity,control=control)
    if origin is not None:
        from deploy.maintenance_executor import ScopeInspector
        from deploy.maintenance_native import NativeAdapter
        inspector=ScopeInspector({'unit':unit})
        if not NativeAdapter.listener_owned(inspector,origin,values['ControlGroup'],deadline or time.monotonic()+3):
            raise EvidenceError('daemon listener is not owned by bound unit')
    return identity


def account(snapshot, profile, *, state=None):
    if not isinstance(snapshot,dict):
        raise EvidenceError('unknown scheduler observation: state is not an object')
    if snapshot.get('errors'):
        reasons=';'.join(str(item)[:160] for item in snapshot['errors'][:4])
        raise EvidenceError('unknown scheduler observation: '+reasons)
    if snapshot.get('read_only') is not False:raise EvidenceError('action runtime is not enabled')
    at=snapshot.get('sampled_at')
    if type(at) not in (int,float) or not math.isfinite(at) or not 0<=time.time()-at<=5:
        raise EvidenceError('stale scheduler observation')
    rows=[r for r in snapshot.get('models',[]) if r.get('name')==profile['model']]
    leases=[r for r in snapshot.get('leases',[]) if r.get('model')==profile['model'] and r.get('status')!='released']
    if (len(rows)!=1 or len(snapshot.get('models',[]))!=1 or len(leases)!=1
            or len([r for r in snapshot.get('leases',[]) if r.get('status')!='released'])!=1):
        raise EvidenceError('unique isolated model/account not observed')
    model,lease=rows[0],leases[0]
    if (lease.get('status')!='confirmed' or lease.get('gpu')!=profile['gpu']
            or model.get('gpu')!=profile['gpu'] or model.get('unit')!=profile['unit']
            or not isinstance(lease.get('lease_id'),str) or not lease.get('lease_id')):
        raise EvidenceError('model or confirmed account binding mismatch')
    for value in (lease.get('budget_gb'),model.get('resident_gb')):
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise EvidenceError('memory/account observation unknown')
    if state is not None and model.get('state')!=state:raise EvidenceError('model state mismatch')
    return model,lease


def action_probe(api, profile, operation, request_id, *, deadline, identity_reader=unit_identity):
    """Actual API/SSE exercise; local correlation is explicitly not a server ID."""
    if operation not in ('free','wake'):raise EvidenceError('unsupported action')
    evidence={'operation':operation,'local_request_id':request_id,'model':profile['model'],
              'response':None,'client_started_monotonic':None,'client_returned_monotonic':None,
              'deadline':deadline,'identity_checks':{}}
    client=api['SchedulerClient'](profile['scheduler_url'],timeout=lifecycle.remaining(deadline,25))
    event_client=api['SchedulerClient'](profile['scheduler_url'],timeout=1)
    reader=api['EventReader'](event_client,stream_timeout=lifecycle.remaining(deadline,30),retry_delay=.1,queue_size=512)
    events=[];metadata=[];lock=threading.Lock();notified=threading.Event()
    def receive():
        data=reader.drain();now=time.monotonic()
        with lock:
            metadata.append({k:data[k] for k in ('status','generation','dropped','missed','cursor')})
            events.extend((item,now) for item in data['events'])
        notified.set()
    reader.set_notify(receive);started=None;cursor=None
    def healthy_stream():
        with lock:
            if len(events)>4096 or any(m['generation'] or m['dropped'] or m['missed'] for m in metadata):
                raise EvidenceError('SSE loss or cursor reset')
            connected=[i for i,m in enumerate(metadata) if m['status']=='SSE connected']
            if connected and any(m['status']!='SSE connected' for m in metadata[connected[0]:]):
                raise EvidenceError('SSE disconnected during measurement')
            if connected:evidence['identity_checks']['sse_connected']=True
            return bool(connected)
    try:
        reader.start()
        while not healthy_stream():
            notified.wait(lifecycle.remaining(deadline,.1));notified.clear()
        before=client.request('GET','/v1/state')
        old_model,old_lease=account(before,profile,state='awake' if operation=='free' else 'sleeping')
        evidence['identity_checks']['before_account']=True
        unit_before=identity_reader(profile['unit'],profile['token'],lease=old_lease['lease_id'],deadline=deadline,origin=profile.get('backend_url'))
        evidence['identity_checks']['before_unit']=unit_before
        with lock:cursor=max((item['id'] for item,_ in events),default=0)
        evidence['identity_checks']['cursor_before']=cursor
        path='/v1/free' if operation=='free' else '/v1/wake/'+quote(profile['model'],safe='')
        payload={'gpu':profile['gpu'],'ram':False,'need_gb':1} if operation=='free' else {}
        started=time.monotonic()
        evidence['client_started_monotonic']=started
        try:
            response=client.request('POST',path,payload)
            http_done=time.monotonic()
        except Exception as exc:
            returned=time.monotonic()
            evidence['client_returned_monotonic']=returned
            payload_marker=object()
            payload=getattr(exc,'payload',payload_marker)
            status=getattr(exc,'status',None)
            if payload is not payload_marker and payload is not None:
                evidence['response']=payload
                evidence['response_parsed']=True
                evidence['response_payload_available']=True
            elif status is not None:
                evidence['response_parsed']=None
                evidence['response_payload_available']=False
            if status is not None:
                evidence['http_status']=status
                evidence['response_received']=True
                evidence['request_error_kind']='http'
            elif payload is not payload_marker and payload is not None:
                evidence['response_received']=True
                evidence['request_error_kind']='client'
            else:
                evidence['response_received']=None
                evidence['request_error_kind']='transport'
            evidence['transport_error_type']=type(exc).__name__
            evidence['transport_error_message']=str(exc)
            message='action request HTTP error' if status is not None else 'action request transport error'
            raise EvidenceError(message, evidence=evidence) from exc
        evidence['client_returned_monotonic']=http_done
        evidence['response']=response
        evidence['response_received']=True
        evidence['response_parsed']=True
        if operation=='free':
            if (response.get('status')!='complete' or response.get('measurement_complete') is not True
                    or response.get('slept')!=[profile['model']] or response.get('stopped')):
                raise EvidenceError('free refused, partial or unmeasured', evidence=evidence)
            value=response.get('freed_gb')
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise EvidenceError('free has no positive measured release', evidence=evidence)
        elif response.get('status')!='ready' or response.get('ready') is not True or response.get('model')!=profile['model']:
            raise EvidenceError('wake did not reach ready', evidence=evidence)
        kind=operation+'_result'
        while True:
            healthy_stream()
            with lock:
                matching=[(item,at) for item,at in events if item['id']>cursor and item['kind']==kind
                          and (operation=='free' or item.get('model')==profile['model'])
                          and all(item.get('detail',{}).get(k)==v for k,v in response.items())]
            if len(matching)>1:raise EvidenceError('ambiguous result-event correlation')
            if matching:
                evidence['identity_checks']['result_event']=matching[0][0]['id']
                break
            notified.wait(lifecycle.remaining(deadline,.1));notified.clear()
        after=client.request('GET','/v1/state')
        new_model,new_lease=account(after,profile,state='sleeping' if operation=='free' else 'awake')
        evidence['identity_checks']['after_account']=True
        if new_lease!=old_lease:raise EvidenceError('lease/account changed across action')
        evidence['identity_checks']['lease_unchanged']=True
        unit_after=identity_reader(profile['unit'],profile['token'],lease=new_lease['lease_id'],deadline=deadline,origin=profile.get('backend_url'))
        evidence['identity_checks']['after_unit']=unit_after
        if unit_after!=unit_before:raise EvidenceError('daemon instance changed across action')
        evidence['identity_checks']['unit_unchanged']=True
        healthy_stream()
        with lock:
            final_matches=[(item,at) for item,at in events if item['id']>cursor and item['kind']==kind
                           and all(item.get('detail',{}).get(k)==v for k,v in response.items())]
        if len(final_matches)!=1:raise EvidenceError('ambiguous result-event correlation')
        evidence['identity_checks']['final_result_event']=final_matches[0][0]['id']
        event,arrived=final_matches[0]
        return {'operation':operation,'local_request_id':request_id,'server_request_id':None,
                'correlation':'isolated model/account + cursor + exact response fields; no server request ID',
                'model':profile['model'],'lease':new_lease,'unit_identity':unit_after,'cursor_before':cursor,
                'event_id':event['id'],'event_kind':event['kind'],'http_seconds':http_done-started,
                'event_received_after_request_seconds':arrived-started,
                'event_received_after_http_seconds':arrived-http_done,
                'before_resident_gb':old_model['resident_gb'],'after_resident_gb':new_model['resident_gb'],
                'response':response,'source_event_timestamp':event.get('timestamp'),
                'long_term_stability_calibration':'NOT MEASURED'}
    except EvidenceError as exc:
        if exc.evidence is None:exc.evidence=evidence
        raise
    except Exception as exc:
        if evidence['client_started_monotonic'] is not None:
            evidence['validation_error_type']=type(exc).__name__
            try:exc.evidence=evidence
            except Exception:pass
        raise
    finally:
        active=sys.exc_info()[1]
        try:
            closed=reader.close()
        except Exception as exc:
            if active is None:raise EvidenceError('SSE reader did not stop',evidence=evidence) from exc
            evidence['reader_close_error_type']=type(exc).__name__
        else:
            if not closed:
                if active is None:raise EvidenceError('SSE reader did not stop',evidence=evidence)
                evidence['reader_close_error']='reader did not stop'
            else:evidence['identity_checks']['reader_closed']=True


def load_launcher(path):
    loader=importlib.machinery.SourceFileLoader('ops_existing_lease_launcher',str(path))
    spec=importlib.util.spec_from_loader(loader.name,loader);module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module;loader.exec_module(module);return module


def guarded_launch(profile, argv):
    """Keep the existing place/health/confirm path; reject wrong scope before start."""
    module=load_launcher(profile['launcher']);original=module.start_unit
    def start(config,args,model,placement):
        if (model!=profile['model'] or args.unit not in (profile['unit'],profile['unit'].removesuffix('.service'))
                or placement.gpu!=str(profile['gpu'])):
            return False  # Existing launcher releases only after its proven-absence check.
        marker=Path(profile['root'])/'launch-lease.json'
        with marker.open('x') as stream:json.dump({'lease_id':placement.lease_id,'gpu':placement.gpu,'model':model},stream)
        marker.chmod(0o600)
        remaining=profile['work_deadline']-time.monotonic()
        if remaining<=0:return False
        config={**config,'systemd_run':{**config['systemd_run'],'properties':{**config['systemd_run'].get('properties',{}),
                'RuntimeMaxSec':max(1,int(remaining)),'TimeoutStopSec':15}}}
        return original(config,args,model,placement)
    module.start_unit=start
    if module.unit_exists(profile['unit']):
        marker=json.loads((Path(profile['root'])/'launch-lease.json').read_text())
        unit_identity(profile['unit'],profile['token'],lease=marker['lease_id'],model=profile['model'])
    return module.main(argv)


def stop_wrapper(profile, pid):
    """Use the existing wrapper sleep command, then signal only a bound pidfd."""
    marker=json.loads((Path(profile['root'])/'launch-lease.json').read_text())
    before=unit_identity(profile['unit'],profile['token'],lease=marker['lease_id'],model=profile.get('model'),origin=profile['backend_url'])
    process=Path('/proc')/str(pid)
    if not lifecycle.cgroup_owned((process/'cgroup').read_text(),profile['source_unit']):
        raise EvidenceError('wrapper is outside owned source unit')
    argv=[x.decode() for x in (process/'cmdline').read_bytes().split(b'\0') if x]
    if argv!=profile['wrapper_argv']:raise EvidenceError('wrapper argv changed')
    ticks=(process/'stat').read_text().rsplit(') ',1)[1].split()[19]
    if hashlib.sha256(Path(profile['wrapper_binary']).read_bytes()).hexdigest()!=profile['wrapper_sha256']:
        raise EvidenceError('wrapper binary changed')
    fd=os.pidfd_open(pid)
    try:
        if (process/'stat').read_text().rsplit(') ',1)[1].split()[19]!=ticks:raise EvidenceError('wrapper PID reused')
        if unit_identity(profile['unit'],profile['token'],lease=marker['lease_id'],model=profile.get('model'),origin=profile['backend_url'])!=before:
            raise EvidenceError('daemon changed before sleep')
        # Never let an external stop-pid argument target a reused process ID.
        result=subprocess.run([profile['wrapper_binary'],'sleep','--vllm-url',profile['backend_url']],
                              timeout=lifecycle.remaining(profile['work_deadline'],15),check=False)
        if result.returncode:raise EvidenceError('owned wrapper sleep failed')
        from deploy.maintenance_native import NativeHTTP
        sleeping=NativeHTTP(profile['backend_url']).json('GET','/is_sleeping',min(profile['work_deadline'],time.monotonic()+2))
        if sleeping.get('is_sleeping') is not True:raise EvidenceError('daemon sleep not confirmed')
        if unit_identity(profile['unit'],profile['token'],lease=marker['lease_id'],model=profile.get('model'),origin=profile['backend_url'])!=before:
            raise EvidenceError('daemon changed during sleep')
        signal.pidfd_send_signal(fd,signal.SIGTERM)
    finally:os.close(fd)
    return 0


def scheduler_wake_request(api, profile, deadline, *, identity_reader=unit_identity):
    """Own one scheduler wake POST while a separate reader observes SSE only."""
    reader=None;request=None;active_error=None
    evidence={'operation':'cold','local_request_id':profile.get('request_id'),'model':profile['model'],
              'response':None,'progress':[],'progress_truncated':False,'progress_before_response':False,
              'progress_attribution_unavailable':False,'identity_checks':{},
              'started_monotonic':None,'started_wall':None,'post_started_monotonic':None,
              'post_started_wall':None,'post_returned_monotonic':None}
    outcome={};started=time.monotonic();started_wall=time.time()
    evidence['started_monotonic']=started;evidence['started_wall']=started_wall
    generation=None;after_id=0;baseline_ready=False;baseline_sampled_at=None
    progress_state={'log_epoch':None,'sequence':0,'retired_epochs':set()}
    progress_observed=[];progress_bytes=0;max_progress_entries=64;max_progress_bytes=32768
    def invoke():
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                outcome['error']=lifecycle.SmokeError('test deadline expired')
                return
            evidence['post_started_monotonic']=time.monotonic();evidence['post_started_wall']=time.time()
            outcome['response']=request_client.request('POST','/v1/wake/'+quote(profile['model'],safe=''),{},timeout=remaining)
        except Exception as exc:outcome['error']=exc
        finally:outcome['returned_monotonic']=time.monotonic()
    def collect(update,observed_at,*,allow_progress):
        nonlocal generation,after_id,baseline_ready,baseline_sampled_at,progress_state,progress_bytes
        incoming_generation=update.get('generation')
        if generation is None:generation=incoming_generation
        elif incoming_generation!=generation:
            generation=incoming_generation;progress_state={'log_epoch':None,'sequence':0,'retired_epochs':set()}
            evidence['progress_attribution_unavailable']=True;baseline_ready=False;allow_progress=False
        for item in update.get('events',[]):
            detail=item.get('detail') if isinstance(item,dict) else None
            sample=detail.get('sampled_at') if isinstance(detail,dict) else None
            if not baseline_ready:
                if (item.get('kind')=='state' and type(sample) in (int,float) and math.isfinite(sample)
                        and sample>baseline_sampled_at and type(item.get('timestamp')) in (int,float)
                        and item['timestamp']>=evidence['post_started_wall']):
                    baseline_sampled_at=sample;baseline_ready=True;after_id=item['id']
                    progress_state={'log_epoch':None,'sequence':0,'retired_epochs':set()};evidence['baseline_event_id']=item['id'];evidence['baseline_generation']=generation;allow_progress=False
                else:allow_progress=False
                continue
            if not allow_progress or evidence['post_started_wall'] is None or not isinstance(item,dict):
                continue
            progress=api['parse_wake_progress'](item,profile['model'],after_id=after_id,since=evidence['post_started_wall'])
            if progress is not None and api['accept_wake_progress'](progress,progress_state):
                safe={**progress,'event_id':item['id'],'source_event_timestamp':item['timestamp'],
                      'model':item['model'],'local_observed_monotonic':observed_at}
                size=len(json.dumps(safe,separators=(',',':')))
                if len(evidence['progress'])<max_progress_entries and progress_bytes+size<=max_progress_bytes:
                    evidence['progress'].append(safe);progress_observed.append(observed_at);progress_bytes+=size
                else:evidence['progress_truncated']=True
                after_id=max(after_id,item['id'])
    def baseline_state_event(update,observed_at):
        nonlocal generation,after_id,progress_state
        if generation is None:generation=update.get('generation')
        for item in update.get('events',[]):
            detail=item.get('detail') if isinstance(item,dict) else None
            sample=detail.get('sampled_at') if isinstance(detail,dict) else None
            if (item.get('kind')=='state' and type(sample) in (int,float) and math.isfinite(sample)
                    and sample>baseline_sampled_at and type(item.get('timestamp')) in (int,float)
                    and item['timestamp']>=started_wall):
                generation=update.get('generation');after_id=item['id'];progress_state={'log_epoch':None,'sequence':0,'retired_epochs':set()}
                evidence['baseline_event_id']=item['id'];evidence['baseline_generation']=generation;evidence['baseline_sampled_at']=sample
                return True
        return False
    try:
        request_timeout=deadline-time.monotonic()
        if request_timeout<=0:raise EvidenceError('scheduler wake deadline',evidence=evidence)
        request_client=api['SchedulerClient'](profile['scheduler_url'],timeout=request_timeout)
        event_client=api['SchedulerClient'](profile['scheduler_url'],timeout=1)
        reader=api['EventReader'](event_client,stream_timeout=.5,retry_delay=.1,max_retry_delay=1,queue_size=64)
        reader.start();baseline=request_client.request('GET','/v1/state',timeout=lifecycle.remaining(deadline,2))
        baseline_sampled_at=baseline.get('sampled_at') if isinstance(baseline,dict) else None
        if (type(baseline_sampled_at) not in (int,float) or not math.isfinite(baseline_sampled_at)
                or baseline.get('errors')):raise EvidenceError('scheduler state baseline unavailable',evidence=evidence)
        baseline_deadline=min(deadline,time.monotonic()+2);baseline_ready=False
        while not baseline_ready and time.monotonic()<baseline_deadline:
            baseline_ready=baseline_state_event(reader.drain(),time.monotonic())
            if not baseline_ready:time.sleep(.01)
        if not baseline_ready:raise EvidenceError('scheduler state baseline unavailable',evidence=evidence)
        if deadline-time.monotonic()<=0:raise EvidenceError('scheduler wake deadline',evidence=evidence)
        request=threading.Thread(target=invoke,name='ops-cold-scheduler-wake',daemon=True);request.start()
        while request.is_alive():
            collect(reader.drain(),time.monotonic(),allow_progress=True)
            if time.monotonic()>=deadline:break
            time.sleep(.01)
        request.join(timeout=max(0,deadline-time.monotonic()));collect(reader.drain(),time.monotonic(),allow_progress=True)
        if request.is_alive():raise EvidenceError('scheduler wake deadline',evidence=evidence)
        evidence['post_returned_monotonic']=outcome.get('returned_monotonic')
        returned=evidence['post_returned_monotonic'];evidence['progress_before_response']=bool(returned is not None and any(value<=returned for value in progress_observed))
        if 'error' in outcome:
            error=outcome['error'];details={**evidence,'error':type(error).__name__+': '+str(error)};status=getattr(error,'status',None);payload=getattr(error,'payload',None)
            if isinstance(error,lifecycle.SmokeError):
                error.evidence=evidence
                raise error
            if status is not None:details.update(http_status=status,response_received=True,request_error_kind='http')
            if payload is not None:details.update(response=payload,response_parsed=True,response_payload_available=True)
            raise EvidenceError('scheduler wake request failed',evidence=details)
        response=outcome.get('response');evidence['response']=response
        if (not isinstance(response,dict) or response.get('status')!='ready' or response.get('ready') is not True
                or response.get('model')!=profile['model'] or response.get('cold_start') is not True
                or type(response.get('elapsed_seconds')) not in (int,float) or not math.isfinite(response['elapsed_seconds'])
                or response['elapsed_seconds']<0):raise EvidenceError('scheduler wake did not reach ready',evidence=evidence)
        state=request_client.request('GET','/v1/state',timeout=lifecycle.remaining(deadline,2));model,lease=account(state,profile,state='awake');evidence['identity_checks']['account']=True
        evidence['identity_checks']['unit']=identity_reader(profile['unit'],profile['token'],lease=lease['lease_id'],model=profile['model'],deadline=deadline)
        return {**evidence,'lease':lease,'unit_identity':evidence['identity_checks']['unit'],'seconds':time.monotonic()-started}
    except EvidenceError as exc:
        active_error=exc
        if exc.evidence is None:exc.evidence=evidence
        raise
    except Exception as exc:
        active_error=exc
        try:
            if getattr(exc,'evidence',None) is None:exc.evidence=evidence
        except Exception:pass
        raise
    finally:
        cleanup_timeout=max(.1,min(2,max(.1,deadline-time.monotonic())));cleanup_error=None
        if reader is not None:
            try:
                if not reader.close(timeout=cleanup_timeout):cleanup_error='event reader did not stop'
            except Exception as exc:cleanup_error=type(exc).__name__+': '+str(exc)
        if request is not None and request.is_alive():
            request.join(timeout=cleanup_timeout)
            if request.is_alive():cleanup_error='wake request worker did not stop'
        if cleanup_error:
            if active_error is not None:
                try:active_error.evidence=(getattr(active_error,'evidence',None) or evidence);active_error.evidence['observer_cleanup_error']=cleanup_error
                except Exception:pass
            else:raise EvidenceError('scheduler wake observer cleanup incomplete',evidence={**evidence,'observer_cleanup_error':cleanup_error})


def helper_main(argv):
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('request','launch','stop'));parser.add_argument('profile')
    parser.add_argument('rest',nargs=argparse.REMAINDER);args=parser.parse_args(argv)
    profile=json.loads(Path(args.profile).read_text());root=Path(profile['root'])
    if root.is_symlink() or (root/'owner').read_text()!=profile['token']:raise EvidenceError('profile owner mismatch')
    if args.mode=='stop':return stop_wrapper(profile,int(args.rest[0]))
    if args.mode=='launch':return guarded_launch(profile,args.rest[1:] if args.rest[:1]==['--'] else args.rest)
    if Path(args.rest[0]).name!=args.rest[0]:raise EvidenceError('request file is outside run')
    request=json.loads((root/args.rest[0]).read_text());deadline=request['deadline']
    if (type(deadline) not in (int,float) or not math.isfinite(deadline)
            or not time.monotonic()<deadline<=profile['work_deadline']):
        raise EvidenceError('request deadline is invalid')
    if Path(request['output']).name!=request['output']:raise EvidenceError('result file is outside run')
    output=root/request['output'];result={'local_request_id':request['id'],'operation':request['operation']}
    try:
        for key,expected in profile['control_instances'].items():
            if unit_identity(key,profile['token'],deadline=deadline)!=expected:raise EvidenceError('control instance changed')
        api=runpy.run_path(profile['cli'])
        if request['operation']=='cold' and profile.get('cold_route','native_chat')=='scheduler_wake':
            profile['request_id']=request['id']
            value=scheduler_wake_request(api,profile,deadline,identity_reader=unit_identity)
            result.update(status='passed',evidence=value,lease=value['lease'],seconds=value['seconds'],cold_start=True)
        elif request['operation']=='cold':
            from urllib.request import Request,build_opener,ProxyHandler
            from urllib.error import HTTPError
            started=time.monotonic()
            try:
                payload={'model':profile['model'],'messages':[{'role':'user','content':'Reply OK.'}],
                         'max_tokens':8,'temperature':0}
                response=build_opener(ProxyHandler({})).open(Request(profile['native_url']+'/v1/chat/completions',
                    data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),
                    timeout=lifecycle.remaining(deadline,150))
                content=response.read(65537);status=response.status;response.close()
                if len(content)>65536:raise EvidenceError('cold response limit')
                answer=json.loads(content)
                if not answer.get('choices'):raise EvidenceError('cold inference has no choices')
            except HTTPError as exc:status=exc.code;exc.close()
            if status!=200:raise EvidenceError('cold request failed')
            state=api['SchedulerClient'](profile['scheduler_url'],timeout=2).request('GET','/v1/state')
            model,lease=account(state,profile,state='awake');unit_identity(profile['unit'],profile['token'],lease=lease['lease_id'],model=profile['model'])
            result.update(status='passed',seconds=time.monotonic()-started,lease=lease)
        else:
            value=action_probe(api,profile,request['operation'],request['id'],deadline=deadline)
            result.update(status='passed',evidence=value)
        for key,expected in profile['control_instances'].items():
            if unit_identity(key,profile['token'],deadline=deadline)!=expected:raise EvidenceError('control instance changed')
    except EvidenceError as exc:
        result.update(status='failed',error=type(exc).__name__+': '+str(exc))
        if exc.evidence is not None:
            result['evidence']=exc.evidence
    except Exception as exc:
        result.update(status='failed',error=type(exc).__name__+': '+str(exc))
        if getattr(exc,'evidence',None) is not None:result['evidence']=exc.evidence
    temporary=output.with_name('.result-'+uuid.uuid4().hex)
    with temporary.open('x') as stream:
        json.dump(result,stream);temporary.chmod(0o600);stream.flush();os.fsync(stream.fileno())
    try:os.link(temporary,output)  # Atomic, exclusive publication; parent never reads partial JSON.
    finally:temporary.unlink(missing_ok=True)
    return 0 if result['status']=='passed' else 1




def artifacts(run, ports, weights_bytes):
    """Render only isolated files; no deployment or process side effects here."""
    c=run.config;root=run.temp;runtime=root+'/runtime';python=c['scheduler_python']
    scheduler_port,native_port,wrapper_port=ports
    scheduler_url='http://127.0.0.1:'+str(scheduler_port);native_url='http://127.0.0.1:'+str(native_port)
    helper=runtime+'/deploy/scheduler_action_smoke.py';profile_path=root+'/profile.json'
    backend_url='http://127.0.0.1:'+str(run.port)
    daemon=[c['vllm_binary'],'serve',c['model_path'],'--host','127.0.0.1','--port',str(run.port),
            '--served-model-name',run.model,'--gpu-memory-utilization',str(c.get('util',.2)),
            '--dtype','bfloat16','--max-model-len','512','--max-num-seqs','1','--enforce-eager','--enable-sleep-mode']
    launch=[python,'-B',helper,'launch',profile_path,'--',str(c.get('util',.2)),run.unit,
            '--config',root+'/launcher.json','--',*daemon]
    wrapper=[c['wrapper_binary'],'serve','--vllm-url',backend_url,'--listen','127.0.0.1:'+str(wrapper_port),
             '--wait-timeout','150s','--',*launch]
    stop=[python,'-B',helper,'stop',profile_path,'${PID}']
    environment={'LLMSVC_OPS_RUN_ID':run.token,'VLLM_SERVER_DEV_MODE':'1','HF_HUB_OFFLINE':'1',
                 'TRANSFORMERS_OFFLINE':'1','VLLM_NO_USAGE_STATS':'1','DO_NOT_TRACK':'1',
                 'VLLM_USE_FLASHINFER_SAMPLER':'0','HF_HOME':root+'/hf','VLLM_CACHE_ROOT':root+'/vllm-cache',
                 'TRITON_CACHE_DIR':root+'/triton','TORCHINDUCTOR_CACHE_DIR':root+'/inductor','CUDA_CACHE_PATH':root+'/cuda'}
    native={'globalTTL':0,'performance':{'disabled':True},'store':{'path':root+'/activity.sqlite'},
            'models':{run.model:{'cmd':shlex.join(wrapper),'cmdStop':shlex.join(stop),
                                 'proxy':'http://127.0.0.1:'+str(wrapper_port)}}}
    launcher={'scheduler_url':scheduler_url,'lock_dir':root+'/locks','request_timeout_seconds':25,
              'startup_timeout_seconds':min(150,c.get('startup_seconds',150)),'health_timeout_seconds':1,
              'health_poll_seconds':.5,'command_timeout_seconds':5,'lock_timeout_seconds':5,
              'systemd_run':{'environment_file':root+'/daemon.env','collect':True,'properties':{
                  'Restart':'no','RuntimeMaxSec':max(1,int(run.work_deadline-time.monotonic())),
                  'TimeoutStopSec':15}}}
    cold_route=c.get('cold_route','native_chat');cold_budget=c.get('startup_seconds',150)
    scheduler={'listen_host':'127.0.0.1','listen_port':scheduler_port,'read_only':False,
               'state_db_path':root+'/ledger.sqlite','placement_enabled':True,'model_actions_enabled':True,
               'automation_enabled':False,'fault_recovery_enabled':False,'catalog_enabled':False,
               'sample_interval_seconds':.25,'event_history_size':4096,'event_heartbeat_seconds':.25,
               'max_snapshot_age_seconds':5,'placement_wait_seconds':20,'lease_timeout_seconds':240,
               'free_timeout_seconds':20,'wake_timeout_seconds':cold_budget if cold_route=='scheduler_wake' else 20,'action_observe_seconds':8,'action_poll_seconds':.1,
               'collectors':{'swap_url':native_url,'activity_path':root+'/activity.sqlite',
                   'host_meminfo_path':c['host_meminfo_path'],'nvidia_smi':root+'/nvidia-smi',
                   'systemctl':root+'/systemctl-read',
                   'deadline':1.8,'probe_timeout':.8,
                   'models':{run.model:{'unit':run.unit,'daemon_url':backend_url,'port':run.port,
                                      'weights_gb':weights_bytes/1024**3,'util':c.get('util',.2),
                                      'cold_start_seconds':c.get('cold_start_cost_seconds',120)}}}}
    nvidia='''#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
import csv,io,subprocess,sys
args=sys.argv[1:]
allowed=("--query-gpu=index,uuid,memory.total,memory.used,memory.free,utilization.gpu", "--query-compute-apps=gpu_uuid,pid,used_gpu_memory,process_name")
if len(args)!=2 or args[0] not in allowed or args[1]!="--format=csv,noheader,nounits":raise SystemExit(64)
r=subprocess.run([BINARY,"--id",str(GPU),*args],text=True,capture_output=True,check=True,timeout=.65)
rows=list(csv.reader(io.StringIO(r.stdout)))
if args[0]==allowed[0]:
 if len(rows)!=1 or int(rows[0][0])!=GPU or rows[0][1].strip()!=UUID:raise SystemExit(65)
else:
 if any(row and row[0].strip()!=UUID for row in rows):raise SystemExit(65)
print(r.stdout,end="")
'''.replace('BINARY',repr(c['nvidia_smi'])).replace('GPU',str(c['gpu'])).replace('UUID',repr(run.gpu_uuid))
    systemctl_read='''#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
import subprocess,sys
args=sys.argv[1:]
if len(args)!=3 or args[0]!="show" or args[1] not in (UNIT,"vllm-*.service") or not args[2].startswith("--property="):raise SystemExit(64)
r=subprocess.run([BINARY,"show",UNIT,args[2]],text=True,capture_output=True,timeout=.65)
values=dict(line.split("=",1) for line in r.stdout.splitlines() if "=" in line)
absent=(values.get("LoadState")=="not-found" and values.get("ActiveState")=="inactive" and values.get("MainPID")=="0" and values.get("ControlGroup")=="")
if r.returncode and not absent:raise SystemExit(r.returncode)
print(r.stdout,end="")
'''.replace('BINARY',repr(c.get('systemctl','/usr/bin/systemctl'))).replace('UNIT',repr(run.unit))
    profile={'root':root,'token':run.token,'model':run.model,'unit':run.unit,'gpu':c['gpu'],
             'scheduler_url':scheduler_url,'native_url':native_url,'cli':runtime+'/cli/llm',
             'launcher':runtime+'/deploy/vllm-launch','control_instances':{},'work_deadline':run.work_deadline,
             'cold_route':cold_route,'cold_budget_seconds':cold_budget,
             'source_unit':run.source_unit,'wrapper_argv':wrapper,'wrapper_binary':c['wrapper_binary'],
             'wrapper_sha256':c['wrapper_sha256'],'backend_url':backend_url}
    files={root+'/native.json':json.dumps(native),root+'/scheduler.json':json.dumps(scheduler),
           root+'/launcher.json':json.dumps(launcher),profile_path:json.dumps(profile),
           root+'/daemon.env':''.join(k+'='+v+'\n' for k,v in environment.items()),root+'/nvidia-smi':nvidia,
           root+'/systemctl-read':systemctl_read}
    for name,source in run.source.items():files[runtime+'/'+name]=source
    for name in ('cli/llm','deploy/vllm-launch','deploy/maintenance_native.py','deploy/maintenance_executor.py'):
        files[runtime+'/'+name]=(Path(c['source'])/name).read_text()
    files[runtime+'/deploy/lifecycle_smoke.py']=Path(lifecycle.__file__).read_text()
    files[helper]=Path(__file__).read_text();files[runtime+'/deploy/__init__.py']=''
    return files,profile


class ActionRun(lifecycle.Run):
    scope='actual isolated scheduler free/wake/SSE chain; live timings only after GPU guards'

    def observation_quiet(self, preflight):
        return lifecycle.scheduler_actions_quiet(preflight, self.model)

    def _resident_enabled(self):
        return (self.config.get('mode') in ('scheduler-actions','idle_resident')
                and isinstance(self.config.get('idle_resident'), dict)
                and self.config['idle_resident'].get('enabled') is True)

    def _resident_pmon(self, gpu_index):
        """Read two bounded pmon samples; failures remain unknown."""
        binary=self.config.get('nvidia_smi','nvidia-smi')
        result=self.command([binary,'pmon','-i',str(gpu_index),'-c','2','-s','um'],limit=3)
        samples={}
        for line in result.stdout.splitlines():
            fields=line.split()
            if not fields or fields[0].startswith('#') or len(fields)<7:
                continue
            try:
                pid=int(fields[1]); sm=float(fields[3]); mem=float(fields[4])
            except (TypeError,ValueError):
                continue
            if pid>0 and math.isfinite(sm) and math.isfinite(mem):
                samples.setdefault(pid,[]).append({'sm_percent':sm,'mem_percent':mem})
        return samples

    def _primary_state(self):
        """Read the configured primary state endpoint before private setup."""
        reader=getattr(self,'_primary_state_reader',None)
        if callable(reader):
            return reader()
        spec=self.config.get('idle_resident',{})
        url=spec.get('primary_state_url')
        if not isinstance(url,str) or not url.startswith(('http://','https://')):
            raise lifecycle.SmokeError('idle_resident primary state target unavailable')
        code='''import json,sys,urllib.request
x=json.load(sys.stdin);req=urllib.request.Request(x["url"])
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
with opener.open(req,timeout=2) as r:
 if r.status!=200:raise RuntimeError("primary state status")
 raw=r.read(1048577)
 if len(raw)>1048576:raise RuntimeError("primary state too large")
 print(raw.decode())
'''
        result=self.container(['python3','-B','-c',code],input=json.dumps({'url':url}),limit=3)
        return json.loads(result.stdout)

    def _primary_ledger(self):
        """Read the existing IntentStore lease table through SQLite mode=ro."""
        reader=getattr(self,'_primary_ledger_reader',None)
        if callable(reader):
            return reader()
        spec=self.config.get('idle_resident',{});path=spec.get('primary_ledger_path')
        if not isinstance(path,str) or not path.startswith('/'):
            raise lifecycle.SmokeError('idle_resident primary ledger target unavailable')
        code='''import json,sqlite3,sys,time
x=json.load(sys.stdin);uri="file:"+x["path"]+"?mode=ro"
db=sqlite3.connect(uri,uri=True,timeout=2)
try:
 rows=db.execute("SELECT lease_id,model,gpu,util,expires_at,budget_gb,status,unit FROM llmsvc_leases ORDER BY model,lease_id").fetchall()
 leases=[dict(zip(("lease_id","model","gpu","util","expires_at","budget_gb","status","unit"),row)) for row in rows]
 blockers=[]
 tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if "llmsvc_reserves" in tables:
  blockers += [{"kind":"reserve","id":row[0]} for row in db.execute("SELECT id FROM llmsvc_reserves WHERE until > ?",(time.time(),))]
 if "llmsvc_faults" in tables:
  blockers += [{"kind":"fault","id":row[0]} for row in db.execute("SELECT lease_id FROM llmsvc_faults WHERE stage != 'complete'")]
 if "llmsvc_recoveries" in tables:
  blockers += [{"kind":"recovery","id":row[0]} for row in db.execute("SELECT id FROM llmsvc_recoveries WHERE stage NOT IN ('complete','aborted','rolled_back')")]
 print(json.dumps({"leases":leases,"blockers":blockers},allow_nan=False))
finally:db.close()
'''
        result=self.container(['python3','-B','-c',code],input=json.dumps({'path':path}),limit=3)
        rows=json.loads(result.stdout)
        if not isinstance(rows,dict) or not isinstance(rows.get('leases'),list) or not isinstance(rows.get('blockers'),list):
            raise lifecycle.SmokeError('primary ledger response malformed')
        return rows

    def _primary_probe(self, port):
        """Probe real protected daemon endpoints; health may have an empty body."""
        probe=getattr(self,'_primary_probe_reader',None)
        if callable(probe):
            return probe(port)
        if type(port) is not int or not 1<=port<=65535:
            raise lifecycle.SmokeError('protected model port unavailable')
        code='''import json,sys,urllib.request
x=json.load(sys.stdin)
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def check_health():
 with opener.open(urllib.request.Request(x["base"]+"/health"),timeout=1) as r:
  if r.status!=200:raise RuntimeError("/health status")
def check_sleeping():
 with opener.open(urllib.request.Request(x["base"]+"/is_sleeping"),timeout=1) as r:
  if r.status!=200:raise RuntimeError("/is_sleeping status")
  raw=r.read(65537)
  if len(raw)>65536:raise RuntimeError("/is_sleeping too large")
  value=json.loads(raw.decode())
  if not isinstance(value,dict) or value.get("is_sleeping") is not True:raise RuntimeError("sleeping proof")
check_health();check_sleeping();print(json.dumps({"health":True,"sleeping":True}))
'''
        result=self.container(['python3','-B','-c',code],input=json.dumps({'base':'http://127.0.0.1:'+str(port)}),limit=3)
        return {'health':True,'sleeping':True}

    def _primary_unit_identity(self, unit, model, lease_id, host_process):
        """Use the existing container/systemd namespace, bound to host cgroup identity."""
        reader=getattr(self,'_primary_unit_reader',None)
        if callable(reader):
            return reader(unit,model,lease_id,host_process)
        if not isinstance(host_process,dict) or not lifecycle.cgroup_owned(host_process.get('cgroup',''),unit):
            raise lifecycle.SmokeError('protected host GPU process is not bound to its unit')
        code='''import json,os,subprocess,sys
x=json.load(sys.stdin)
def read():
 show=subprocess.run(["systemctl","show",x["unit"],"-p","Id","-p","MainPID","-p","InvocationID","-p","ControlGroup","-p","Environment"],capture_output=True,text=True,check=True)
 v=dict(line.split("=",1) for line in show.stdout.splitlines() if "=" in line)
 pid=int(v.get("MainPID","0"));
 if (v.get("Id")!=x["unit"] or pid<=0 or not v.get("InvocationID")
     or not v.get("ControlGroup")):raise RuntimeError("protected unit binding")
 proc="/proc/"+str(pid);env={}
 for item in open(proc+"/environ","rb").read().split(b"\\0"):
  if b"=" in item:
   key,value=item.split(b"=",1);env[key.decode()]=value.decode()
 ticks=open(proc+"/stat").read().rsplit(") ",1)[1].split()[19];cg=open(proc+"/cgroup").read()
 if not any(line.rsplit(":",1)[-1].rstrip("/") == v["ControlGroup"].rstrip("/") for line in cg.splitlines()):raise RuntimeError("protected cgroup binding")
 if env.get("LLMSVC_MODEL")!=x["model"] or env.get("LLMSVC_LEASE_ID")!=x["lease"] or env.get("CUDA_VISIBLE_DEVICES")!=str(x["gpu"]):raise RuntimeError("protected process binding")
 return v,pid,ticks,cg
first=read();second=read()
if any(first[i]!=second[i] for i in (0,1,2,3)):raise RuntimeError("protected unit changed")
print(json.dumps({"unit":x["unit"],"pid":first[1],"start_ticks":first[2],"cgroup":first[3],"invocation_id":first[0].get("InvocationID","")}))
'''
        result=self.container(['python3','-B','-c',code],input=json.dumps({'unit':unit,'model':model,'lease':lease_id,'gpu':self.config['gpu']}),limit=3)
        return json.loads(result.stdout)

    def resident_observation(self, gpu, current_processes):
        """Build fresh proof from the independent primary state and ledger."""
        state=self._primary_state(); now=time.time()
        if (not isinstance(state,dict) or type(state.get('schema_version')) is not int
                or type(state.get('sampled_at')) not in (int,float)
                or not math.isfinite(state['sampled_at']) or state['sampled_at']>now
                or now-state['sampled_at']>5 or state.get('errors')!=[]):
            raise lifecycle.SmokeError('idle_resident primary state stale or malformed')
        models=state.get('models'); activities=state.get('activity'); gpus=state.get('gpus')
        if not isinstance(models,list) or not isinstance(activities,list) or not isinstance(gpus,list):
            raise lifecycle.SmokeError('idle_resident primary state shape unknown')
        model_names=[row.get('name') if isinstance(row,dict) else None for row in models]
        if (any(not isinstance(name,str) or not name for name in model_names)
                or len(model_names)!=len(set(model_names))):
            raise lifecycle.SmokeError('idle_resident primary model rows malformed or duplicated')
        gpu_rows=[row for row in gpus if isinstance(row,dict) and row.get('index')==self.config['gpu']]
        if len(gpu_rows)!=1 or gpu_rows[0].get('uuid')!=gpu['uuid']:
            raise lifecycle.SmokeError('idle_resident primary GPU binding mismatch')
        if not activities or any(not isinstance(row,dict) or not isinstance(row.get('model'),str)
                                 or type(row.get('in_flight')) is not int or row['in_flight']<0 for row in activities):
            raise lifecycle.SmokeError('idle_resident primary inflight observation unknown')
        inflight=sum(row['in_flight'] for row in activities)
        if state.get('blocked_by'):
            raise lifecycle.SmokeError('idle_resident primary state has pending blockers')
        ledger_result=self._primary_ledger()
        if not isinstance(ledger_result,dict):raise lifecycle.SmokeError('idle_resident primary ledger malformed')
        if ledger_result.get('blockers'):raise lifecycle.SmokeError('idle_resident primary ledger has pending fences')
        ledger=ledger_result.get('leases')
        if not isinstance(ledger,list) or not ledger:raise lifecycle.SmokeError('idle_resident primary ledger unavailable')
        lease_ids=[row.get('lease_id') if isinstance(row,dict) else None for row in ledger]
        if (any(not isinstance(row,dict) or not isinstance(row.get('lease_id'),str)
                or not isinstance(row.get('model'),str) or type(row.get('gpu')) is not int
                or not isinstance(row.get('unit'),str) or row.get('status') not in ('pending','stale','confirmed','released')
                or type(row.get('budget_gb')) not in (int,float)
                or not math.isfinite(row['budget_gb']) or row['budget_gb']<=0 for row in ledger)
                or len(lease_ids)!=len(set(lease_ids))):
            raise lifecycle.SmokeError('idle_resident primary lease rows malformed')
        resident=self.config.get('idle_resident',{});expected_protected=resident.get('protected_processes',[])
        expected_baseline=resident.get('baseline_processes',[])
        if not isinstance(expected_protected,list) or not expected_protected:
            raise lifecycle.SmokeError('idle_resident protected identity proof is required')
        if not isinstance(expected_baseline,list):raise lifecycle.SmokeError('idle_resident baseline identities malformed')
        protected_expected_names={row.get('model') for row in expected_protected if isinstance(row,dict)}
        if any(row['status'] in ('pending','stale') and (row['gpu']==self.config['gpu'] or row['model'] in protected_expected_names)
               for row in ledger):
            raise lifecycle.SmokeError('idle_resident primary lease is pending or stale')
        activity_models={row['model'] for row in activities}
        if not protected_expected_names.issubset(activity_models):
            raise lifecycle.SmokeError('idle_resident protected activity coverage unavailable')
        current_by_id={(row.get('gpu_uuid'),row.get('pid'),row.get('start_ticks'),row.get('cgroup')):row
                       for row in current_processes if isinstance(row,dict)}
        pmon=self._resident_pmon(self.config['gpu'])
        def idle_samples(samples):
            return (isinstance(samples,list) and len(samples)>=2 and all(
                isinstance(item,dict) and type(item.get('sm_percent')) in (int,float)
                and type(item.get('mem_percent')) in (int,float)
                and math.isfinite(item['sm_percent']) and math.isfinite(item['mem_percent'])
                and item['sm_percent']<=resident.get('idle_util_percent',1)
                and item['mem_percent']<=resident.get('idle_util_percent',1) for item in samples))
        protected=[];protected_names=set()
        for expected in expected_protected:
            if not isinstance(expected,dict) or not isinstance(expected.get('model'),str):
                raise lifecycle.SmokeError('idle_resident protected identity expectation malformed')
            name=expected['model'];
            if name in protected_names:raise lifecycle.SmokeError('idle_resident protected identities duplicated')
            protected_names.add(name)
            rows=[row for row in models if isinstance(row,dict) and row.get('name')==name]
            if len(rows)!=1:raise lifecycle.SmokeError('idle_resident protected model observation ambiguous')
            model=rows[0]
            if (model.get('gpu')!=self.config['gpu'] or model.get('state')!='sleeping'
                    or model.get('is_sleeping') is not True or model.get('health_ok') is not True
                    or type(model.get('port')) is not int):
                raise lifecycle.SmokeError('idle_resident protected model sleep/health proof unavailable')
            lease_rows=[row for row in ledger if row['model']==name and row['gpu']==self.config['gpu']
                        and row['unit']==model.get('unit') and row['status']=='confirmed']
            if len(lease_rows)!=1:raise lifecycle.SmokeError('idle_resident protected ledger/unit proof unavailable')
            lease=lease_rows[0]
            identity={k:expected.get(k) for k in ('gpu_uuid','pid','start_ticks','cgroup')}
            key=tuple(identity.get(k) for k in ('gpu_uuid','pid','start_ticks','cgroup'))
            if any(identity.get(k) in (None,'') for k in ('gpu_uuid','pid','start_ticks','cgroup')) or key not in current_by_id:
                raise lifecycle.SmokeError('idle_resident protected process identity unavailable')
            if not idle_samples(pmon.get(identity['pid'])):
                raise lifecycle.SmokeError('idle_resident protected process is not idle')
            probe=self._primary_probe(model['port'])
            if not isinstance(probe,dict) or probe.get('health') is not True or probe.get('sleeping') is not True:
                raise lifecycle.SmokeError('idle_resident protected HTTP proof unavailable')
            checked=self._primary_unit_identity(model.get('unit'),name,lease['lease_id'],current_by_id[key])
            protected.append({**identity,'model':name,'ledger_status':'confirmed','model_state':'sleeping',
                              'health_status':'sleeping','full_budget_gb':lease['budget_gb'],'unit_identity':checked})
        for model in models:
            if (isinstance(model,dict) and model.get('name')!=self.model
                    and model.get('gpu')==self.config['gpu']
                    and model.get('name') not in protected_names):
                raise lifecycle.SmokeError('idle_resident unclassified protected model')
        baseline=[]
        for expected in expected_baseline:
            if not isinstance(expected,dict):raise lifecycle.SmokeError('idle_resident baseline identity malformed')
            key=tuple(expected.get(k) for k in ('gpu_uuid','pid','start_ticks','cgroup'))
            row=current_by_id.get(key);samples=pmon.get(expected.get('pid'),[])
            if row is None or len(samples)<2:raise lifecycle.SmokeError('idle_resident baseline proof unavailable')
            baseline.append({**expected,'utilization_samples':samples})
        external_ids={tuple(row.get(k) for k in ('gpu_uuid','pid','start_ticks','cgroup'))
                      for row in current_processes if isinstance(row,dict) and not self._process_owned(row)}
        expected_ids={tuple(row.get(k) for k in ('gpu_uuid','pid','start_ticks','cgroup'))
                      for row in expected_baseline+expected_protected if isinstance(row,dict)}
        if external_ids-set(expected_ids):raise lifecycle.SmokeError('idle_resident unknown external process')
        external_baseline=sum(float(row.get('used_memory_mib',0))/1024 for row in current_processes
                              if isinstance(row,dict) and tuple(row.get(k) for k in ('gpu_uuid','pid','start_ticks','cgroup')) in external_ids)
        return {'source':'collector','ledger_source':'state+ledger','unit_source':'systemd+proc',
                'capacity_source':'nvidia-smi','sampled_at':state['sampled_at'],'inflight':inflight,
                'protected_processes':protected,'baseline_processes':baseline,
                'external_baseline_gb':external_baseline,'candidate_util':self.config.get('util')}

    def _process_owned(self, row):
        return lifecycle.cgroup_owned(row.get('cgroup',''),self.unit)

    def _resident_unit_identity(self, unit, model, lease):
        return unit_identity(unit,self.token,lease=lease,model=model,deadline=self.deadline)

    def inventory(self, *, allow_own=False, allow_resident=False):
        if allow_resident is False and self._resident_enabled() and getattr(self,'profile',None) is not None:
            allow_resident=True
        return super().inventory(allow_own=allow_own,allow_resident=allow_resident)

    def __init__(self,config):
        super().__init__(config)
        self.scheduler_unit='llmsvc-ops-action-scheduler-'+self.token+'.service'
        self.source_unit='llmsvc-ops-action-source-'+self.token+'.service'
        self.units=[];self.lease=None;self.preserve=False;self.measured_phases=set();self.phase_measurements={}

    def _ledger_release_witness(self, binding):
        code='''import json,sqlite3,sys
from pathlib import Path
x=json.load(sys.stdin);root=Path(x['root'])
if root.is_symlink() or not (root/'owner').is_file() or (root/'owner').read_text()!=x['token']:
 raise RuntimeError('run root ownership mismatch')
marker=root/'launch-lease.json'
if not marker.is_file() or json.loads(marker.read_text()).get('lease_id')!=x['lease_id']:
 raise RuntimeError('launch lease marker mismatch')
path=root/'ledger.sqlite'
if path.is_symlink() or not path.is_file(): print(json.dumps({'released':False}));raise SystemExit(0)
db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
try:
 row=db.execute('SELECT model,status,unit FROM llmsvc_leases WHERE lease_id=?',(x['lease_id'],)).fetchone()
 print(json.dumps({'released':bool(row and row[0]==x['model'] and row[1]=='released' and row[2]==x['unit'])}))
finally: db.close()
'''
        try:
            value=self.python(code,{'root':self.temp,'token':self.token,'lease_id':binding.get('lease_id'),
                                    'model':self.model,'unit':self.unit},cleanup=True,limit=3)
        except Exception:
            return False
        return value.get('released') is True

    def _cleanup_state(self, binding, *, newer_than=None, require_publication=False, allow_baseline=False):
        try:
            observed=self.daemon_binding(cleanup=True)
        except lifecycle.SmokeError as exc:
            raise _CleanupObservationUnknown('daemon exit observation unavailable') from exc
        if observed.get('lease_id')!=binding.get('lease_id'):
            raise lifecycle.SmokeError('account cleanup lease identity mismatch')
        if observed.get('absent') is not True:
            expected=binding.get('identity'); current=observed.get('identity')
            if expected is None or current!=expected:
                raise lifecycle.SmokeError('daemon identity changed during cleanup')
            raise _CleanupObservationUnknown('daemon reappeared during cleanup')
        try:
            state=self.json_at(self.profile['scheduler_url'],'/v1/state',cleanup=True)
        except (lifecycle.SmokeError, OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
            raise _CleanupObservationUnknown('account cleanup snapshot unavailable') from exc
        sampled_at=state.get('sampled_at') if isinstance(state,dict) else None
        now=time.time()
        configured_age=getattr(self,'config',{}).get('max_snapshot_age_seconds',5)
        if (isinstance(configured_age,bool) or type(configured_age) not in (int,float)
                or not math.isfinite(configured_age) or configured_age<=0):
            raise _CleanupObservationUnknown('account cleanup freshness configuration unknown',requires_publication=True)
        valid_timestamp=(isinstance(state,dict) and type(sampled_at) in (int,float)
                         and math.isfinite(sampled_at) and 0<=sampled_at<=now
                         and now-sampled_at<=configured_age)
        if not valid_timestamp:
            detail=(str(state.get('errors'))[:200] if isinstance(state,dict) and 'errors' in state else '')
            suffix=': '+detail if detail else ''
            raise _CleanupObservationUnknown('account cleanup snapshot timestamp unknown'+suffix,
                                              requires_publication=True)
        if require_publication and newer_than is None and not allow_baseline:
            raise _CleanupObservationUnknown('account cleanup requires a fresh publication',
                                              sampled_at=sampled_at,requires_publication=True)
        if newer_than is not None and sampled_at<=newer_than:
            raise _CleanupObservationUnknown('account cleanup publication did not advance',sampled_at=sampled_at)
        schema=state.get('schema_version');errors=state.get('errors')
        if type(schema) is not int or schema!=1:
            raise _CleanupObservationUnknown('account cleanup schema unknown',sampled_at=sampled_at)
        if not isinstance(errors,list) or any(not isinstance(item,str) for item in errors):
            raise _CleanupObservationUnknown('account cleanup errors malformed: '+str(errors)[:200],sampled_at=sampled_at)
        if errors:
            raise _CleanupObservationUnknown('account cleanup reported errors: '+';'.join(errors[:4]),sampled_at=sampled_at)
        models=state.get('models')
        if models is not None:
            if not isinstance(models,list):
                raise _CleanupObservationUnknown('account cleanup model snapshot unknown',sampled_at=sampled_at)
            names=[]
            for row in models:
                if not isinstance(row,dict):
                    raise _CleanupObservationUnknown('account cleanup model snapshot malformed',sampled_at=sampled_at)
                name=row.get('name') or row.get('model')
                if not isinstance(name,str) or not name or name in names:
                    raise _CleanupObservationUnknown('account cleanup model snapshot malformed',sampled_at=sampled_at)
                names.append(name)
            matching=[row for row in models if (row.get('name') or row.get('model'))==self.model]
            if len(matching)>1 or (matching and matching[0].get('unit') not in (None,self.unit)):
                raise lifecycle.SmokeError('account cleanup model identity mismatch')
        rows=[];lease_ids=[]
        valid_status={'pending','stale','confirmed','released'}
        for row in state['leases']:
            if not isinstance(row,dict) or not isinstance(row.get('lease_id'),str) or not row.get('lease_id'):
                raise _CleanupObservationUnknown('account cleanup lease snapshot malformed',sampled_at=sampled_at)
            if not isinstance(row.get('model'),str) or not row.get('model') or row.get('status') not in valid_status:
                raise _CleanupObservationUnknown('account cleanup lease snapshot malformed',sampled_at=sampled_at)
            if row.get('unit') is not None and (not isinstance(row.get('unit'),str) or not row.get('unit')):
                raise _CleanupObservationUnknown('account cleanup lease snapshot malformed',sampled_at=sampled_at)
            if row['lease_id'] in lease_ids:
                raise lifecycle.SmokeError('account cleanup lease identity mismatch')
            lease_ids.append(row['lease_id']);rows.append(row)
        for row in rows:
            if row.get('lease_id')==binding.get('lease_id') and (row.get('model')!=self.model or row.get('unit') not in (None,self.unit)):
                raise lifecycle.SmokeError('account cleanup lease identity mismatch')
        active=[row for row in rows if row.get('status')!='released']
        leases=[row for row in active if row.get('model')==self.model]
        if len(leases)>1:
            raise lifecycle.SmokeError('account cleanup lease identity mismatch')
        for lease in leases:
            if lease.get('lease_id')!=binding.get('lease_id') or lease.get('unit') not in (None,self.unit):
                raise lifecycle.SmokeError('account cleanup lease identity mismatch')
        return leases,sampled_at

    def _completion_fields(self, result, cleanup_succeeded):
        return {'result':result,'scope':self.scope,
                'live_chain_measured':bool(self.measured_phases),
                'measured_phases':sorted(self.measured_phases),
                'phase_measurements':self.phase_measurements,
                'cleanup_succeeded':cleanup_succeeded}

    def _record_phase_result(self, name, value):
        status=value.get('status') if isinstance(value,dict) else None
        measured=status=='passed';quality='validated' if measured else 'unmeasured'
        evidence=value.get('evidence') if isinstance(value,dict) else None
        evidence_model=evidence.get('model') if isinstance(evidence,dict) else None
        response=evidence.get('response') if isinstance(evidence,dict) else None
        if not measured and isinstance(response,dict):
            if (name=='free' and response.get('measurement_complete') is True
                    and evidence_model==self.model
                    and response.get('slept')==[self.model]
                    and response.get('stopped')==[]
                    and type(response.get('freed_gb')) in (int,float)
                    and math.isfinite(response['freed_gb']) and response['freed_gb']>0):
                measured=True;quality='partial_measured'
            elif (name=='wake' and response.get('ready') is True
                  and evidence_model==self.model
                  and response.get('model')==self.model):
                measured=True;quality='partial_measured'
        self.phase_measurements[name]={'quality':quality,'measured':measured}
        if measured:self.measured_phases.add(name)

    def control(self,unit,argv,limit):
        self.units.append(unit)
        duration=max(1,int(min(limit,self.work_deadline-time.monotonic())))
        return self.container(['systemd-run','--collect','--unit='+unit,'--property=Restart=no',
            '--property=RuntimeMaxSec='+str(duration),'--property=TimeoutStopSec=5',
            '--property=WorkingDirectory='+self.temp+'/runtime',
            '--setenv=LLMSVC_OPS_RUN_ID='+self.token,'--setenv=PYTHONDONTWRITEBYTECODE=1',
            '--setenv=PYTHONPATH='+self.temp+'/runtime','--setenv=HTTP_PROXY=','--setenv=HTTPS_PROXY=',
            '--setenv=ALL_PROXY=','--setenv=NO_PROXY=127.0.0.1,::1',
            '--setenv=http_proxy=','--setenv=https_proxy=','--setenv=all_proxy=',
            '--setenv=no_proxy=127.0.0.1,::1','--',*argv],limit=5)

    def identities(self):
        code='''import importlib.util,json,sys
x=json.load(sys.stdin);sys.path.insert(0,x['runtime'])
from deploy.scheduler_action_smoke import unit_identity
print(json.dumps({u:unit_identity(u,x['token']) for u in x['units']}))
'''
        return self.python(code,{'runtime':self.temp+'/runtime','token':self.token,
                                 'units':[self.scheduler_unit,self.source_unit]},limit=6)

    def json_at(self,url,path,body=None,*,cleanup=False):
        code='''import json,sys,urllib.request
x=json.load(sys.stdin);data=None if x['body'] is None else json.dumps(x['body']).encode()
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
r=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(urllib.request.Request(x['url']+x['path'],data=data,headers={'Content-Type':'application/json'}),timeout=2)
print(r.read().decode())
'''
        return self.python(code,{'url':url,'path':path,'body':body},limit=3,cleanup=cleanup)

    def prepare(self):
        code='''import hashlib,json,socket,sys
from pathlib import Path
x=json.load(sys.stdin)
host=Path(x['host_meminfo_path']).stat()
if [host.st_dev,host.st_ino]!=x['host_meminfo_identity']:raise RuntimeError('live host meminfo binding unverified')
for path,pin in x['pins'].items():
 if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=pin:raise RuntimeError('runtime binary changed')
root=Path(x['root']);root.mkdir(mode=0o700);(root/'owner').write_text(x['token'])
sockets=[socket.socket() for _ in range(3)]
try:
 for s in sockets:s.bind(('127.0.0.1',0))
 ports=[s.getsockname()[1] for s in sockets]
finally:
 for s in sockets:s.close()
model=Path(x['model']);idx=model/'model.safetensors.index.json'
files=set(json.loads(idx.read_text())['weight_map'].values()) if idx.exists() else {p.name for p in model.glob('*.safetensors')}
print(json.dumps({'ports':ports,'weight_bytes':sum((model/f).stat().st_size for f in files)}))
'''
        self.temp_created=True  # Even a lost preparation reply must clean only its owner-marked root.
        prepared=self.python(code,{'pins':{self.config['native_binary']:self.config['native_binary_sha256'],
                                           self.config['wrapper_binary']:self.config['wrapper_sha256']},
                                   'root':self.temp,'token':self.token,'model':self.config['model_path'],
                                   'host_meminfo_path':self.config['host_meminfo_path'],
                                   'host_meminfo_identity':[Path('/proc/meminfo').stat().st_dev,Path('/proc/meminfo').stat().st_ino]},limit=8)
        self.temp_created=True
        files,self.profile=artifacts(self,prepared['ports'],prepared['weight_bytes'])
        self.python('''import json,sys
from pathlib import Path
x=json.load(sys.stdin);root=Path(x['root'])
if (root/'owner').read_text()!=x['token']:raise RuntimeError('owner changed')
for name,text in x['files'].items():
 p=Path(name)
 if not p.is_relative_to(root) or '..' in p.parts:raise RuntimeError('path outside run')
 p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('x') as f:f.write(text)
 p.chmod(0o700 if p.name in ('nvidia-smi','systemctl-read') else 0o600)
print('{}')
''',{'root':self.temp,'token':self.token,'files':files},limit=8)
        self.log('isolated_profile',profile=self.profile,source_hash=self.source_hash,ports=prepared['ports'],
                 file_sha256={name:hashlib.sha256(text.encode()).hexdigest() for name,text in files.items()})

    def phase(self,name,seconds):
        if self.deadline-time.monotonic()<=60:raise lifecycle.SmokeError('insufficient remaining action/cleanup budget')
        self.inventory(allow_own=self.attempted and name!='cold', allow_resident=self._resident_enabled())
        if name=='cold':self.attempted=True
        request_id=uuid.uuid4().hex;input_name='request-'+request_id+'.json';output_name='result-'+request_id+'.json'
        request={'id':request_id,'operation':name,'output':output_name,
                 'deadline':min(self.work_deadline-5,time.monotonic()+seconds)}
        self.python("import json,sys;from pathlib import Path;x=json.load(sys.stdin);p=Path(x['root'])/x['name'];p.write_text(json.dumps(x['data']));p.chmod(0o600);print('{}')",
                    {'root':self.temp,'name':input_name,'data':request})
        unit='llmsvc-ops-action-request-'+request_id+'.service'
        self.control(unit,[self.config['scheduler_python'],'-B',self.temp+'/runtime/deploy/scheduler_action_smoke.py',
                           'request',self.temp+'/profile.json',input_name],seconds+3)
        while time.monotonic()<request['deadline']+3:
            self.inventory(allow_own=True)
            value=self.python("import json,sys;from pathlib import Path;x=json.load(sys.stdin);p=Path(x['root'])/x['name'];print(p.read_text() if p.exists() else '{}')",
                              {'root':self.temp,'name':output_name})
            if value:
                if value.get('local_request_id')!=request_id or value.get('operation')!=name:
                    raise lifecycle.SmokeError('request receipt identity mismatch')
                self._record_phase_result(name,value)
                self.log('scheduler_action_phase',phase=name,receipt=value)
                if value.get('status')!='passed':raise lifecycle.SmokeError('action phase failed: '+str(value.get('error')))
                if name=='wake' and self.config.get('cold_route','native_chat')=='scheduler_wake':
                    warm_seconds=(value.get('evidence') or {}).get('http_seconds')
                    if (type(warm_seconds) not in (int,float) or not math.isfinite(warm_seconds)
                            or warm_seconds<0):
                        self.phase_measurements[name]={'quality':'unmeasured','measured':False}
                        self.measured_phases.discard(name)
                        raise lifecycle.SmokeError('warm wake latency unavailable')
                    if warm_seconds>3:
                        self.phase_measurements[name]={'quality':'measured_over_target','measured':True}
                        self.measured_phases.add(name)
                        raise lifecycle.SmokeError('warm wake exceeded three-second target')
                if name=='cold':self.lease=value['lease']
                return value
            state=self.container(['systemctl','is-active',unit],check=False).stdout.strip()
            if state not in ('active','activating'):
                raise lifecycle.SmokeError('request unit exited without a complete receipt')
            time.sleep(.5)
        raise lifecycle.SmokeError('bounded action request did not complete')

    def daemon_binding(self, *, cleanup=False):
        code='''import json,sys,subprocess
from pathlib import Path
x=json.load(sys.stdin);root=Path(x['root'])
if root.is_symlink() or not (root/'owner').is_file() or (root/'owner').read_text()!=x['token']:raise RuntimeError('run root ownership mismatch')
sys.path.insert(0,str(root/'runtime'))
from deploy.scheduler_action_smoke import unit_identity
marker=root/'launch-lease.json'
if not marker.is_file():raise RuntimeError('launch lease marker missing')
lease=json.loads(marker.read_text()).get('lease_id')
if not isinstance(lease,str) or not lease:raise RuntimeError('launch lease marker invalid')
show=subprocess.run(['systemctl','show',x['unit'],'-p','Id','-p','LoadState','-p','ActiveState','-p','MainPID','-p','InvocationID','-p','Environment','-p','ControlGroup'],capture_output=True,text=True,check=False).stdout
values=dict(line.split('=',1) for line in show.splitlines() if '=' in line)
if values.get('Id')!=x['unit']:raise RuntimeError('daemon unit identity mismatch')
if values.get('LoadState')=='not-found' or (values.get('MainPID')=='0' and values.get('ActiveState') in ('inactive','dead','failed')):
 print(json.dumps({'absent':True,'exit_proven':True,'lease_id':lease}));raise SystemExit(0)
if values.get('MainPID')=='0':raise RuntimeError('daemon identity unknown')
identity=unit_identity(x['unit'],x['token'],lease=lease,model=x['model'])
print(json.dumps({'absent':False,'lease_id':lease,'identity':identity,'control_group':values.get('ControlGroup',''),'invocation_id':values.get('InvocationID','')}))
'''
        return self.python(code,{'root':self.temp,'unit':self.unit,'model':self.model,
                                 'token':self.token},limit=5,cleanup=cleanup)

    def wait_daemon_exit(self, binding):
        end=min(self.deadline,time.monotonic()+18)
        while time.monotonic()<end:
            value=self.container(['systemctl','show',self.unit,'-p','Id','-p','LoadState','-p','ActiveState','-p','MainPID','-p','InvocationID','-p','ControlGroup'],cleanup=True,check=False).stdout
            fields=dict(line.split('=',1) for line in value.splitlines() if '=' in line)
            if fields.get('LoadState')=='not-found':
                return
            if fields.get('ActiveState') in ('inactive','failed','dead') and fields.get('MainPID')=='0':
                if fields.get('Id')!=self.unit:raise lifecycle.SmokeError('daemon identity changed during cleanup')
                expected=binding.get('identity') or {}
                for key,old in (('InvocationID',expected.get('invocation_id') or binding.get('invocation_id','')),
                                ('ControlGroup',binding.get('control_group',''))):
                    current=fields.get(key,'')
                    if current and (not old or current!=old):
                        raise lifecycle.SmokeError('daemon identity changed during cleanup')
                return
            current=self.daemon_binding(cleanup=True)
            if current.get('absent'):
                if current.get('exit_proven') is not True:raise lifecycle.SmokeError('daemon exit proof unavailable')
                return
            if (current.get('lease_id')!=binding.get('lease_id')
                    or current.get('identity')!=binding.get('identity')):
                raise lifecycle.SmokeError('daemon identity changed during cleanup')
            lifecycle.remaining(end,2);time.sleep(.2)
        raise lifecycle.SmokeError('owned daemon did not exit before cleanup deadline')

    def wait_free_eligible(self):
        end=min(self.work_deadline-55,time.monotonic()+40)
        while time.monotonic()<end:
            self.inventory(allow_own=True)
            value=self.json_at(self.profile['scheduler_url'],'/v1/free?dry_run=1',
                               {'gpu':self.config['gpu'],'ram':False,'need_gb':1})
            actions=value.get('would',[])
            if len(actions)==1 and actions[0].get('kind')=='sleep' and actions[0].get('model')==self.model:
                self.log('free_preview',response=value);return
            reasons={row.get('reason') for row in value.get('blocked_by',[])}
            if reasons-{'recently_active','insufficient_reclaimable_memory'}:
                raise lifecycle.SmokeError('free preview blocked: '+str(value))
            time.sleep(1)
        raise lifecycle.SmokeError('free idle/protection eligibility not established in remaining budget')

    def cleanup_actions(self):
        errors=[]
        # Every stop/kill is conditional on the exact test token. Never let a
        # native unload callback touch a possibly replaced daemon during cleanup.
        if self.attempted or (self.temp_created and self.units):
            try:
                binding=self.daemon_binding(cleanup=True)
                if not binding.get('absent'):
                    try:
                        self.container(['systemctl','stop',self.unit],limit=12,cleanup=True)
                    except subprocess.TimeoutExpired as exc:
                        self.log('owned_stop_timeout',unit=self.unit,error=str(exc))
                        # The stop request was already submitted. Observe the
                        # same identity until the original deadline; never resend it.
                    self.wait_daemon_exit(binding)
                last_unknown_sampled_at=None;unknown_without_timestamp=False;baseline_pending=False
                while True:
                    try:
                        leases,sampled_at=self._cleanup_state(binding,newer_than=last_unknown_sampled_at,
                                                              require_publication=unknown_without_timestamp,
                                                              allow_baseline=baseline_pending)
                        if not leases and binding.get('lease_id') and not self._ledger_release_witness(binding):
                            raise _CleanupObservationUnknown('private ledger release witness unavailable',sampled_at=sampled_at)
                        if baseline_pending:
                            last_unknown_sampled_at=sampled_at;unknown_without_timestamp=False;baseline_pending=False
                            continue
                        break
                    except _CleanupObservationUnknown as exc:
                        if exc.sampled_at is not None:
                            last_unknown_sampled_at=(exc.sampled_at if last_unknown_sampled_at is None
                                                     else max(last_unknown_sampled_at,exc.sampled_at))
                        if exc.requires_publication and exc.sampled_at is None:
                            unknown_without_timestamp=True;baseline_pending=True
                        remaining=self.deadline-time.monotonic()
                        if remaining<=0:
                            raise lifecycle.SmokeError('account cleanup snapshot unknown: '+str(exc)) from exc
                        time.sleep(min(.2,remaining))
                for lease in leases:
                    released=self.json_at(self.profile['scheduler_url'],'/v1/place/'+quote(lease['lease_id'],safe='')+'/release',{},cleanup=True)
                    if released.get('status')!='released' or released.get('lease_id')!=lease['lease_id']:
                        raise lifecycle.SmokeError('lease release unconfirmed')
                self.log('account_cleanup',leases=leases,proven_exit_release_requested=True)
            except Exception as exc:errors.append(str(exc));self.preserve=True
        for unit in reversed(self.units):
            try:
                values=self.container(['systemctl','show',unit,'-p','Id','-p','LoadState','-p','Environment','-p','MainPID','-p','InvocationID','-p','ControlGroup'],cleanup=True).stdout
                if 'LoadState=not-found' in values:continue
                initial=dict(line.split('=',1) for line in values.splitlines() if '=' in line)
                env=next((line.split('=',1)[1] for line in values.splitlines() if line.startswith('Environment=')), '')
                if not lifecycle.owned(env,self.token):raise lifecycle.SmokeError('control unit ownership unknown')
                self.container(['systemctl','kill','--kill-whom=all','--signal=KILL',unit],cleanup=True,check=False)
                try:
                    self.container(['systemctl','stop',unit],cleanup=True,limit=5)
                except subprocess.TimeoutExpired as exc:
                    self.log('control_stop_timeout',unit=unit,error=str(exc))
                    end=min(self.deadline,time.monotonic()+5)
                    while time.monotonic()<end:
                        state=self.container(['systemctl','show',unit,'-p','Id','-p','LoadState','-p','ActiveState','-p','MainPID','-p','InvocationID','-p','ControlGroup'],cleanup=True,check=False).stdout
                        fields=dict(line.split('=',1) for line in state.splitlines() if '=' in line)
                        if fields.get('LoadState')=='not-found':
                            break
                        if fields.get('ActiveState') in ('inactive','failed','dead') and fields.get('MainPID')=='0':
                            if fields.get('Id')!=initial.get('Id'):raise lifecycle.SmokeError('control unit identity changed during cleanup')
                            for key in ('InvocationID','ControlGroup'):
                                current=fields.get(key,'');old=initial.get(key,'')
                                if current and (not old or current!=old):
                                    raise lifecycle.SmokeError('control unit identity changed during cleanup')
                            break
                        for key in ('Id','InvocationID','ControlGroup'):
                            if fields.get(key)!=initial.get(key):
                                raise lifecycle.SmokeError('control unit identity changed during cleanup')
                        if fields.get('MainPID')!=initial.get('MainPID'):
                            raise lifecycle.SmokeError('control unit PID changed during cleanup')
                        lifecycle.remaining(end,2);time.sleep(.2)
                    else:raise lifecycle.SmokeError('control unit did not exit before cleanup deadline')
            except Exception as exc:errors.append(str(exc));self.preserve=True
        if self.temp_created and not self.preserve:
            self.python("import json,sys,shutil;from pathlib import Path;x=json.load(sys.stdin);p=Path(x['root']);\nif p.exists():\n assert not p.is_symlink() and (p/'owner').read_text()==x['token'];shutil.rmtree(p)\nprint('{}')",
                        {'root':self.temp,'token':self.token},cleanup=True)
        self.log('action_cleanup',errors=errors,files_preserved=self.preserve)
        if errors:raise lifecycle.SmokeError('owned cleanup incomplete: '+ '; '.join(errors[:2]))

    def execute(self):
        result='failed'
        cleanup_succeeded=True
        try:
            gpu=self.inventory(allow_resident=self._resident_enabled());self.gpu_uuid=gpu[1]
            if self.container(['systemctl','show',self.unit,'-p','LoadState','--value']).stdout.strip()!='not-found':
                raise lifecycle.SmokeError('test daemon unit already exists')
            self.prepare();self.inventory()
            self.control(self.source_unit,[self.config['native_binary'],'-config',self.temp+'/native.json',
                                           '-listen',self.profile['native_url'].removeprefix('http://')],300)
            while True:
                try:self.json_at(self.profile['native_url'],'/v1/models');break
                except Exception:
                    lifecycle.remaining(self.work_deadline,1);time.sleep(.2)
            self.control(self.scheduler_unit,[self.config['scheduler_python'],'-B','-m','llmsvc','--config',self.temp+'/scheduler.json'],300)
            while True:
                try:
                    state=self.json_at(self.profile['scheduler_url'],'/v1/state')
                    if state.get('sampled_at') is not None and not state.get('errors'):break
                except Exception:pass
                lifecycle.remaining(self.work_deadline,1);time.sleep(.2)
            self.profile['control_instances']=self.identities()
            self.python("import json,sys;from pathlib import Path;x=json.load(sys.stdin);(Path(x['root'])/'profile.json').write_text(json.dumps(x));print('{}')",self.profile)
            if self._resident_enabled():
                self.inventory(allow_resident=True)
            self.phase('cold',self.config.get('startup_seconds',150))
            self.wait_free_eligible()
            self.phase('free',25);self.phase('wake',25)
            result='passed'
        except Exception as exc:self.log('failure',error=type(exc).__name__+': '+str(exc))
        finally:
            try:self.cleanup_actions()
            except Exception as exc:cleanup_succeeded=False;result='failed';self.log('cleanup_failure',error=str(exc))
        self.log('complete',**self._completion_fields(result,cleanup_succeeded))
        return result


if __name__=='__main__':
    raise SystemExit(helper_main(sys.argv[1:]))
