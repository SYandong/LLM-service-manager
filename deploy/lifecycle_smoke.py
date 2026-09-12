#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Bounded, ops-owned cached-base lifecycle smoke; never LoRA acceptance."""
import argparse
import csv
import datetime
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid

import yaml


class SmokeError(RuntimeError):
    pass


def identity():
    token = uuid.uuid4().hex
    return token, 'ops-life-' + token, 'vllm-ops-life-' + token + '.service'


def remaining(deadline, limit):
    value = min(limit, deadline - time.monotonic())
    if value <= 0:
        raise SmokeError('test deadline expired')
    return value


def validate(config):
    for key in ('container', 'lock_path', 'model_path', 'vllm_binary', 'source', 'output_dir'):
        if not isinstance(config.get(key), str) or not config[key]:
            raise SmokeError('missing setting: ' + key)
    cap = config.get('wall_seconds', 300)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or not 60 <= cap <= 300:
        raise SmokeError('wall_seconds must be finite and between 60 and 300')
    if type(config.get('gpu')) is not int or config['gpu'] < 0:
        raise SmokeError('gpu must be a nonnegative integer')
    source = Path(config['source'])
    if not (source/'llmsvc/collectors/__init__.py').is_file() or not (source/'llmsvc/state.py').is_file():
        raise SmokeError('source must contain the reviewed collector and state modules')
    if not 0 < config.get('util', 0.2) < 1:
        raise SmokeError('util must be between zero and one')
    if config.get('mode','direct') not in ('direct','scheduler-actions','idle_resident'):
        raise SmokeError('unknown lifecycle mode')
    if config.get('mode') in ('scheduler-actions','idle_resident'):
        from deploy.scheduler_action_smoke import validate as validate_actions
        validate_actions(config)
    if config.get('mode')=='idle_resident' or 'idle_resident' in config:
        value=config.get('idle_resident')
        if not isinstance(value,dict):
            raise SmokeError('idle_resident settings are required')
        if value.get('enabled') is not True:
            raise SmokeError('idle_resident requires explicit enabled=true')
        for key in ('candidate_full_budget_gb','external_baseline_gb','margin_gb','idle_util_percent'):
            number=value.get(key)
            if isinstance(number,bool) or type(number) not in (int,float) or not math.isfinite(number) or number<0:
                raise SmokeError('idle_resident '+key+' must be finite and nonnegative')
        if type(value.get('inflight')) is not int or value['inflight']<0:
            raise SmokeError('idle_resident inflight must be a nonnegative integer')
        if value['idle_util_percent']>100:
            raise SmokeError('idle_resident idle_util_percent must be <=100')
        if value['margin_gb'] < 4:
            raise SmokeError('idle_resident margin must be at least 4 GiB')
        if type(value.get('candidate_full_budget_gb')) not in (int,float):
            raise SmokeError('idle_resident candidate budget is an expected value only')
        for key in ('protected_processes','baseline_processes'):
            if not isinstance(value.get(key),list):raise SmokeError('idle_resident '+key+' is required')


def owned(environment, token):
    try:
        return 'LLMSVC_OPS_RUN_ID=' + token in shlex.split(environment)
    except ValueError:
        return False


def cgroup_owned(contents, unit):
    return any(line.split(':', 2)[-1].rstrip('/').endswith('/' + unit)
               for line in contents.splitlines())


def sources(root):
    result = {str(p.relative_to(root)): p.read_text() for p in (root / 'llmsvc').rglob('*.py')}
    yaml_root = Path(yaml.__file__).parent
    result.update({'yaml/' + str(p.relative_to(yaml_root)): p.read_text() for p in yaml_root.rglob('*.py')})
    return result


COLLECT = r'''
import sys,json,importlib.abc,importlib.util
x=json.load(sys.stdin)
class Loader(importlib.abc.MetaPathFinder,importlib.abc.Loader):
 def find_spec(self,name,path=None,target=None):
  base=name.replace('.','/')
  for key,package in ((base+'/__init__.py',True),(base+'.py',False)):
   if key in x['sources']:
    spec=importlib.util.spec_from_loader(name,self,is_package=package);spec.loader_state=key;return spec
 def create_module(self,spec):return None
 def exec_module(self,module):
  key=module.__spec__.loader_state;module.__file__='<ops-source>/'+key
  exec(compile(x['sources'][key],module.__file__,'exec'),module.__dict__)
sys.meta_path.insert(0,Loader())
from llmsvc.collectors import build_collector
c=build_collector({'swap_url':x['swap_url'],'models':{x['model']:{'unit':x['unit'],'daemon_url':x['url'],'port':x['port'],'util':x['util']}},'deadline':1.8,'probe_timeout':.8,'host_meminfo_path':None})
try:print(json.dumps(c.collect().to_dict()))
finally:c.close()
'''

PREFLIGHT = r'''
import json,socket,time,urllib.request
from pathlib import Path
x=json.load(__import__('sys').stdin);events=[];frame=[];deadline=time.monotonic()+2
with urllib.request.urlopen(x['swap_url']+'/api/events',timeout=1) as r:
 while time.monotonic()<deadline:
  line=r.readline(262144)
  if not line:break
  if line.strip()==b'':
   if frame:
    event=json.loads(b'\n'.join(frame));frame=[]
    if event.get('type') in ('modelStatus','inflight'):events.append(event)
    if any(e.get('type')=='modelStatus' for e in events) and any(e.get('type')=='inflight' for e in events):break
  elif line.startswith(b'data:'):frame.append(line[5:].strip())
port=None
if x.get('choose_port'):
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
model=Path(x['model_path']);index=model/'model.safetensors.index.json'
files=list(json.loads(index.read_text()).get('weight_map',{}).values()) if index.exists() else [p.name for p in model.glob('*.safetensors')]
complete=bool(files) and (model/'config.json').is_file() and all((model/f).is_file() and (model/f).stat().st_size>0 for f in set(files))
print(json.dumps({'events':events,'port':port,'cached_weights_complete':complete,'weight_bytes':sum((model/f).stat().st_size for f in set(files)) if complete else None}))
'''


def quiet(preflight):
    states = None
    inflight = None
    for event in preflight.get('events', []):
        data = event['data']
        if isinstance(data, str):
            data = json.loads(data)
        if event['type'] == 'modelStatus':
            states = data
        elif event['type'] == 'inflight' and data.get('operation') == 'snapshot':
            inflight = data.get('requests', [])
    return (states is not None and inflight == [] and
            all(m.get('state') == 'stopped' for m in states))


def scheduler_actions_quiet(preflight, selected_model):
    """Require an explicit primary catalog and no selected-model collision.

    The caller has already proven selected-card isolation with fresh GPU,
    process, RAM and ownership checks. The generated isolated model must be
    absent from the primary catalog; a collision cannot be treated as isolated.
    Other model states do not impose direct-mode's all-model stopped
    requirement, while malformed/unknown state or inflight remains fail-closed.
    """
    states = None
    inflight = None
    for event in preflight.get('events', []):
        data = event['data']
        if isinstance(data, str):
            data = json.loads(data)
        if event['type'] == 'modelStatus':
            states = data
        elif event['type'] == 'inflight' and data.get('operation') == 'snapshot':
            inflight = data.get('requests', [])
    if not isinstance(states, list) or inflight != []:
        return False
    known_states = {'stopped', 'ready', 'sleeping'}
    names = []
    for row in states:
        if not isinstance(row, dict):
            return False
        name = row.get('id') or row.get('model') or row.get('name')
        if not isinstance(name, str) or not name or row.get('state') not in known_states:
            return False
        if name in names:
            return False
        names.append(name)
    return selected_model not in names


def check_host_capacity(available_gb, weight_bytes):
    # Test-only conservative headroom; never exported as production admission.
    if not math.isfinite(available_gb) or weight_bytes <= 0 or weight_bytes > 20*1024**3:
        raise SmokeError('requires a small complete cache <=20GiB and known host RAM')
    if available_gb < max(64, 2*weight_bytes/1024**3):
        raise SmokeError('insufficient fresh host RAM for the isolated test')


def idle_resident_admission(*, gpu, protected_processes, baseline_processes,
                            current_processes, candidate_full_budget_gb=None,
                            external_baseline_gb=None, inflight=None, margin_gb=4.0,
                            idle_util_percent=1.0, owned_processes=(),
                            observation=None, now=None, max_age_seconds=5.0):
    """Pure opt-in admission over one fresh, collector-bound observation.

    The process/config rows are expected identities and thresholds.  Sleeping
    proof, budgets, inflight and external memory must come from ``observation``
    at this call; static affirmative config cannot satisfy this predicate.
    """
    reasons=[]
    def finite(value):return type(value) in (int,float) and math.isfinite(value) and value>=0
    now=time.time() if now is None else now
    if not finite(now): reasons.append('clock_unknown')
    if not isinstance(observation,dict):
        reasons.append('fresh_observation_unknown')
        observation={}
    sampled_at=observation.get('sampled_at')
    if (not finite(sampled_at) or not finite(now) or sampled_at>now
            or now-sampled_at>max_age_seconds):
        reasons.append('fresh_observation_stale_or_invalid')
    if observation.get('source') != 'collector':
        reasons.append('collector_source_unverified')
    if observation.get('ledger_source') != 'state+ledger':
        reasons.append('ledger_source_unverified')
    if observation.get('capacity_source') != 'nvidia-smi':
        reasons.append('capacity_source_unverified')
    if observation.get('unit_source') != 'systemd+proc':
        reasons.append('unit_source_unverified')
    if not isinstance(gpu,dict) or not isinstance(gpu.get('uuid'),str) or not gpu['uuid']:
        reasons.append('gpu_identity_unknown')
    for key in ('total_gb','free_gb','utilization_percent'):
        if not finite(gpu.get(key) if isinstance(gpu,dict) else None):reasons.append('gpu_'+key+'_unknown')
    observed_inflight=observation.get('inflight', inflight)
    if type(observed_inflight) is not int or observed_inflight != 0:reasons.append('inflight_unknown_or_nonzero')
    if not finite(margin_gb) or margin_gb < 4:reasons.append('margin_below_required_floor')
    if not finite(observation.get('external_baseline_gb')):
        reasons.append('external_capacity_unknown')
    candidate_util=observation.get('candidate_util')
    if not finite(candidate_util) or candidate_util<=0 or candidate_util>=1:
        reasons.append('candidate_budget_unknown')
    if not finite(idle_util_percent): reasons.append('idle_threshold_unknown')
    observed_external=observation.get('external_baseline_gb')
    if finite(observed_external) and isinstance(gpu,dict) and finite(gpu.get('total_gb')) and finite(candidate_util):
        candidate_full_budget_gb=float(gpu['total_gb'])*float(candidate_util)
        if not finite(candidate_full_budget_gb) or candidate_full_budget_gb<=0:
            reasons.append('candidate_budget_unknown')
    else:
        candidate_full_budget_gb=0.0
    external_baseline_gb=observed_external
    def identity(row):
        return (row.get('gpu_uuid'),row.get('pid'),row.get('start_ticks'),row.get('cgroup')) if isinstance(row,dict) else None
    def valid_identity(row):
        value=identity(row)
        gpu_uuid=gpu.get('uuid') if isinstance(gpu,dict) else None
        return (value is not None and value[0]==gpu_uuid and type(value[1]) is int and value[1]>0
                and isinstance(value[2],str) and value[2].isdigit() and isinstance(value[3],str) and bool(value[3]))
    protected_ids=[]
    protected_total=0.0
    if not isinstance(protected_processes,list) or not isinstance(baseline_processes,list):
        reasons.append('expected_identity_lists_malformed')
        protected_processes=baseline_processes=()
    observed_protected=observation.get('protected_processes')
    if not isinstance(observed_protected,list):
        reasons.append('protected_ledger_unknown')
        observed_protected=[]
    for expected in protected_processes:
        row=next((item for item in observed_protected
                  if isinstance(item,dict) and item.get('model')==expected.get('model')),None)
        if (not isinstance(expected,dict) or not isinstance(row,dict)
                or not valid_identity(row) or identity(row)!=identity(expected)
                or row.get('ledger_status')!='confirmed'
                or row.get('model_state')!='sleeping'
                or row.get('health_status')!='sleeping'
                or not finite(row.get('full_budget_gb'))):
            reasons.append('protected_identity_or_sleeping_unknown');continue
        if identity(row) in protected_ids: reasons.append('duplicate_protected_identity');continue
        protected_ids.append(identity(row));protected_total+=float(row['full_budget_gb'])
    if any(isinstance(row,dict) and row.get('model') not in {x.get('model') for x in protected_processes if isinstance(x,dict)}
           for row in observed_protected):
        reasons.append('unclassified_protected_model')
    baseline_ids=[]
    observed_baseline=observation.get('baseline_processes')
    if not isinstance(observed_baseline,list):
        reasons.append('baseline_observation_unknown');observed_baseline=[]
    for expected in baseline_processes:
        row=next((item for item in observed_baseline
                  if isinstance(item,dict) and identity(item)==identity(expected)),None)
        samples=row.get('utilization_samples') if isinstance(row,dict) else None
        def idle_sample(value):
            return (isinstance(value,dict) and finite(value.get('sm_percent'))
                    and finite(value.get('mem_percent'))
                    and value['sm_percent']<=idle_util_percent and value['mem_percent']<=idle_util_percent)
        if (not isinstance(expected,dict) or not isinstance(row,dict) or identity(row) in protected_ids
                or not valid_identity(row) or identity(row)!=identity(expected) or not isinstance(samples,list)
                or len(samples)<2 or any(not idle_sample(x) for x in samples)):
            reasons.append('baseline_identity_or_idle_unknown');continue
        baseline_ids.append(identity(row))
    owned_ids=[identity(row) for row in owned_processes if valid_identity(row)]
    allowed=set(protected_ids+baseline_ids+owned_ids)
    seen=[]
    if not isinstance(current_processes,list): reasons.append('current_processes_unknown');current_processes=[]
    for row in current_processes:
        current_id=identity(row)
        if not valid_identity(row) or current_id in seen or current_id not in allowed:
            reasons.append('new_changed_or_unknown_occupant');continue
        seen.append(current_id)
    if baseline_ids and not set(baseline_ids)<=set(seen):reasons.append('baseline_process_missing')
    required=protected_total+float(candidate_full_budget_gb if finite(candidate_full_budget_gb) else 0)+float(external_baseline_gb if finite(external_baseline_gb) else 0)+float(margin_gb if finite(margin_gb) else 0)
    if isinstance(gpu,dict) and finite(gpu.get('total_gb')) and required>gpu['total_gb']:
        reasons.append('full_budget_capacity_shortfall')
    if finite(candidate_full_budget_gb) and isinstance(gpu,dict) and finite(gpu.get('free_gb')) \
            and gpu['free_gb'] < candidate_full_budget_gb:
        reasons.append('current_free_capacity_shortfall')
    return {'eligible':not reasons,'reasons':sorted(set(reasons)),'required_gb':required,
            'protected_full_budget_gb':protected_total}


class Run:
    scope = 'cached-base collector lifecycle only'
    lora_measured = False

    def __init__(self, config):
        self.config = config
        self.token, self.model, self.unit = identity()
        self.deadline = time.monotonic() + config.get('wall_seconds', 300)
        self.work_deadline = self.deadline - 45
        self.temp = '/tmp/' + self.model
        self.records = []
        self.attempted = False
        self.temp_created = False
        self.port = None
        self.source = sources(Path(config['source']))
        self.source_hash = hashlib.sha256(json.dumps(self.source, sort_keys=True).encode()).hexdigest()

    def prepare_fixture(self):
        return None

    def extra_env(self):
        return {}

    def extra_args(self):
        return []

    def resident_observation(self, gpu, current_processes):
        """Return one fresh collector-bound resident proof, or fail closed."""
        raise SmokeError('idle_resident requires a live collector-bound observation')

    def log(self, kind, **detail):
        record = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'kind': kind, **detail}
        self.records.append(record)
        print(json.dumps(record), flush=True)

    def command(self, argv, *, limit=8, cleanup=False, check=True, input=None):
        deadline = self.deadline if cleanup else self.work_deadline
        result = subprocess.run(argv, input=input, capture_output=True, text=True,
                                timeout=remaining(deadline, limit))
        if check and result.returncode:
            raise SmokeError('command failed: ' + argv[0] + ': ' + result.stderr[-300:])
        return result

    def container(self, argv, **kwargs):
        return self.command(self.config.get('lxc_prefix', ['sudo', '-n', 'lxc']) +
                            ['exec', self.config['container'], '--'] + argv, **kwargs)

    def python(self, code, data, **kwargs):
        result = self.container(['python3', '-B', '-c', code], input=json.dumps(data), **kwargs)
        return json.loads(result.stdout)

    def process_records(self, processes, *, allow_own=False, allow_resident=False):
        current=[]
        for process in processes:
            pid=None;ticks=None;cgroup=None
            try:
                pid=int(process[1]); proc=Path('/proc')/str(pid)
                ticks=proc.joinpath('stat').read_text().rsplit(') ',1)[1].split()[19]
                cgroup=proc.joinpath('cgroup').read_text()
            except (OSError,ValueError,IndexError):
                if allow_resident: raise SmokeError('resident process identity unknown')
            current.append({'gpu_uuid':process[0],'pid':pid,
                            'start_ticks':ticks,'cgroup':cgroup,'used_memory_mib':process[3]})
            ours=cgroup_owned(cgroup or '', self.unit)
            if not allow_resident and (not allow_own or not ours):
                raise SmokeError('foreign or unclassified process on candidate GPU')
        return current

    def inventory(self, *, allow_own=False, allow_resident=False):
        nvidia_smi=self.config.get('nvidia_smi','nvidia-smi')
        g = self.command([nvidia_smi, '--query-gpu=index,uuid,memory.total,memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits']).stdout
        p = self.command([nvidia_smi, '--query-compute-apps=gpu_uuid,pid,process_name,used_memory', '--format=csv,noheader,nounits']).stdout
        rows = [list(map(str.strip, r)) for r in csv.reader(io.StringIO(g))]
        gpu = next(r for r in rows if int(r[0]) == self.config['gpu'])
        if not all(math.isfinite(float(value)) for value in gpu[2:]):
            raise SmokeError('GPU observations are not finite')
        processes = [list(map(str.strip, r)) for r in csv.reader(io.StringIO(p)) if r and r[0].strip() == gpu[1]]
        current_processes=self.process_records(processes,allow_own=allow_own,allow_resident=allow_resident)
        if not allow_own and not allow_resident:
            need = float(gpu[2]) * self.config.get('util', .2) + 4096
            if float(gpu[4]) < need or float(gpu[3]) > 256 or float(gpu[5]) > 0:
                raise SmokeError('candidate GPU is not idle with sufficient memory')
        if allow_resident:
            resident=self.config.get('idle_resident',{})
            owned_processes=getattr(self,'_idle_owned_processes',[])
            if allow_own:
                current_owned=[row for row in current_processes if cgroup_owned(row.get('cgroup',''),self.unit)]
                if current_owned and not getattr(self,'_resident_worker_observed',False):
                    owned_processes=current_owned;self._idle_owned_processes=list(current_owned)
                    self._resident_worker_observed=True
                elif getattr(self,'_resident_worker_observed',False):
                    keys=('gpu_uuid','pid','start_ticks','cgroup')
                    expected={tuple(row.get(key) for key in keys) for row in owned_processes}
                    observed={tuple(row.get(key) for key in keys) for row in current_owned}
                    if not expected or observed!=expected or len(current_owned)!=len(owned_processes):
                        raise SmokeError('idle_resident owned daemon identity unknown or changed')
            observation=self.resident_observation(
                {'uuid':gpu[1],'total_gb':float(gpu[2])/1024,
                 'free_gb':float(gpu[4])/1024,'utilization_percent':float(gpu[5])},
                current_processes)
            result=idle_resident_admission(
                gpu={'uuid':gpu[1],'total_gb':float(gpu[2])/1024,
                     'free_gb':float(gpu[4])/1024,'utilization_percent':float(gpu[5])},
                protected_processes=resident.get('protected_processes',[]),
                baseline_processes=resident.get('baseline_processes',[]),
                current_processes=current_processes,
                owned_processes=owned_processes,
                candidate_full_budget_gb=observation.get('candidate_full_budget_gb'),
                external_baseline_gb=observation.get('external_baseline_gb'),
                inflight=observation.get('inflight'),margin_gb=resident.get('margin_gb',4),
                idle_util_percent=resident.get('idle_util_percent',1),
                observation=observation)
            if not result['eligible']:raise SmokeError('idle_resident blocked: '+','.join(result['reasons']))
        state = self.python(PREFLIGHT, {**self.config, 'choose_port': self.port is None})
        if not self.observation_quiet(state) or not state['cached_weights_complete']:
            raise SmokeError('unknown/busy serving or incomplete cached weights')
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
        host_available_gb = int(mem['MemAvailable'].split()[0]) / 1024**2
        check_host_capacity(host_available_gb, state['weight_bytes'])
        if self.port is None:
            self.port = state['port']
        self.log('preflight' if not allow_own else 'monitor', gpu=gpu, processes=processes, in_flight=0, host_available_gb=host_available_gb)
        return gpu

    def observation_quiet(self, preflight):
        return quiet(preflight)

    def verify_owner(self, *, cleanup=False):
        r = self.container(['systemctl', 'show', self.unit, '-p', 'Environment', '--value'],
                           cleanup=cleanup, check=False)
        return r.returncode == 0 and owned(r.stdout, self.token)

    def http(self, path, payload=None, *, limit=3):
        code = """import json,sys,urllib.request
x=json.load(sys.stdin);req=urllib.request.Request(x['url'],data=json.dumps(x['data']).encode() if x['data'] is not None else None,headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req,timeout=x['timeout']) as r: print(json.dumps({'status':r.status,'body':r.read(1048576).decode()}))
"""
        return self.python(code, {'url': f'http://127.0.0.1:{self.port}' + path,
                                 'data': payload, 'timeout': remaining(self.work_deadline, limit)}, limit=limit + 1)

    def check_endpoint(self):
        if not self.verify_owner():
            raise SmokeError('test unit ownership lost')
        models = json.loads(self.http('/v1/models')['body'])
        if self.model not in [m.get('id') for m in models.get('data', [])]:
            raise SmokeError('alternate endpoint does not serve this test model')

    def collect(self, expected, *, cleanup=False):
        state = self.python(COLLECT, {'sources': self.source, 'swap_url': self.config['swap_url'],
                                     'model': self.model, 'unit': self.unit, 'port': self.port,
                                     'url': f'http://127.0.0.1:{self.port}', 'util': self.config.get('util', .2)}, limit=5, cleanup=cleanup)
        row = next(m for m in state['models'] if m['name'] == self.model)
        self.log('collector', expected=expected, model=row, errors=state['errors'])
        if row['state'] != expected:
            raise SmokeError('actual collector lifecycle mismatch: ' + row['state'])

    def exercise(self):
        self.collect('awake')
        self.check_endpoint()
        answer = self.http('/v1/chat/completions', {'model': self.model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}], 'max_tokens': 8, 'temperature': 0}, limit=10)
        self.log('base_inference', status=answer['status'], response=answer['body'])
        self.inventory(allow_own=True); self.check_endpoint()
        self.http('/sleep?level=1&mode=wait', {}, limit=15)
        self.collect('sleeping'); self.inventory(allow_own=True)
        self.check_endpoint(); self.http('/wake_up', {}, limit=15)
        self.collect('awake')

    def cleanup(self):
        if self.attempted and self.verify_owner(cleanup=True):
            self.container(['systemctl', 'stop', self.unit], limit=25, cleanup=True)
            self.log('owned_unit_stopped', unit=self.unit)
        # Refuse file cleanup while a test-tagged unit is still active.
        r = self.container(['systemctl', 'is-active', self.unit], check=False, cleanup=True)
        if r.stdout.strip() in ('active', 'activating', 'deactivating'):
            raise SmokeError('unit still active; preserve test files for manual owned cleanup')
        if self.temp_created:
            code = """import json,sys,shutil
from pathlib import Path
x=json.load(sys.stdin);p=Path(x['path'])
if p.is_symlink() or (p/'owner').read_text()!=x['token']: raise RuntimeError('directory ownership mismatch')
shutil.rmtree(p);print('{}')
"""
            self.python(code, {'path': self.temp, 'token': self.token}, cleanup=True)
            self.log('owned_directory_removed', path=self.temp)

    def execute(self):
        result = 'failed'
        try:
            self.log('scope', scope=self.scope, lora_measured=False, source_hash=self.source_hash, flashinfer_sampler=self.config.get('flashinfer_sampler', False))
            self.inventory()
            absent = self.container(['systemctl', 'show', self.unit, '-p', 'LoadState', '--value'])
            if absent.stdout.strip() != 'not-found':
                raise SmokeError('generated unit name already exists')
            self.python("from pathlib import Path;import json,sys;x=json.load(sys.stdin);p=Path(x['path']);p.mkdir(mode=0o700);(p/'owner').write_text(x['token']);print('{}')", {'path': self.temp, 'token': self.token})
            self.temp_created = True
            self.prepare_fixture()
            self.inventory()  # Fresh immediately before startup.
            env = {'CUDA_VISIBLE_DEVICES': str(self.config['gpu']), 'LLMSVC_OPS_RUN_ID': self.token,
                   'VLLM_SERVER_DEV_MODE': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                   'VLLM_NO_USAGE_STATS': '1', 'DO_NOT_TRACK': '1',
                   'VLLM_USE_FLASHINFER_SAMPLER': '1' if self.config.get('flashinfer_sampler', False) else '0',
                   'HF_HOME': self.temp + '/hf', 'VLLM_CACHE_ROOT': self.temp + '/vllm-cache',
                   'TRITON_CACHE_DIR': self.temp + '/triton', 'TORCHINDUCTOR_CACHE_DIR': self.temp + '/inductor',
                   'CUDA_CACHE_PATH': self.temp + '/cuda', 'XDG_CACHE_HOME': self.temp + '/xdg'}
            env.update(self.extra_env())
            argv = ['systemd-run', '--unit=' + self.unit, '--collect', '--property=Restart=no',
                    '--property=RuntimeMaxSec=' + str(max(1, int(self.work_deadline-time.monotonic()))),
                    '--property=TimeoutStopSec=20']
            argv += ['--setenv=' + k + '=' + v for k, v in env.items()]
            argv += ['--', self.config['vllm_binary'], 'serve', self.config['model_path'],
                     '--host', '127.0.0.1', '--port', str(self.port), '--served-model-name', self.model,
                     '--gpu-memory-utilization', str(self.config.get('util', .2)), '--dtype', 'bfloat16',
                     '--max-model-len', '512', '--max-num-seqs', '1', '--enforce-eager', '--enable-sleep-mode']
            argv += self.extra_args()
            self.attempted = True
            began = time.monotonic()
            self.container(argv, limit=8)
            self.log('started', unit=self.unit, port=self.port, wall_cap=self.config.get('wall_seconds', 300))
            startup_end = min(self.work_deadline - 45, began + self.config.get('startup_seconds', 150))
            while time.monotonic() < startup_end:
                self.inventory(allow_own=True)
                state = self.container(['systemctl', 'is-active', self.unit], check=False).stdout.strip()
                if state not in ('active', 'activating'):
                    raise SmokeError('test daemon exited during startup')
                try:
                    self.http('/health'); self.check_endpoint(); break
                except (SmokeError, subprocess.TimeoutExpired):
                    time.sleep(min(5, remaining(startup_end, 5)))
            else:
                raise SmokeError('bounded startup deadline reached')
            self.log('ready', cold_start_seconds=time.monotonic()-began)
            self.exercise()
            result = 'passed'
        except Exception as exc:
            self.log('failure', error=type(exc).__name__ + ': ' + str(exc))
            if self.attempted:
                try:
                    journal = self.container(['journalctl', '-u', self.unit, '-n', '80', '--no-pager', '-o', 'cat'], limit=5, cleanup=True, check=False)
                    self.log('own_unit_journal', text=journal.stdout[-16000:])
                except Exception as error:
                    self.log('journal_unavailable', error=type(error).__name__)
        finally:
            try:
                self.cleanup()
                if self.attempted:
                    self.collect('stopped', cleanup=True)
                gpu = self.command(['nvidia-smi', '--query-gpu=index,memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits'], cleanup=True).stdout
                processes = self.command(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid,process_name,used_memory', '--format=csv,noheader,nounits'], cleanup=True).stdout
                final = self.python(PREFLIGHT, {**self.config, 'choose_port': False}, cleanup=True)
                self.log('post_test', gpus=gpu, processes=processes, production_quiet=quiet(final))
            except Exception as exc:
                result = 'failed'; self.log('cleanup_failure', error=str(exc))
        self.log('complete', result=result, scope=self.scope, lora_measured=result == 'passed' and self.lora_measured)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text()); validate(config)
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'scope': config.get('mode','direct')+' lifecycle validation', 'config': config}))
        return 0
    output = Path(config['output_dir'])
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Path(config['lock_path']).open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if config.get('mode') in ('scheduler-actions','idle_resident'):
            from deploy.scheduler_action_smoke import ActionRun
            run = ActionRun(config)
        else:
            run = Run(config)
        result = run.execute()
        path = output / (run.model + '.json')
        path.write_text(json.dumps({'unit': run.unit, 'token': run.token, 'records': run.records}, indent=2)+'\n');path.chmod(0o600)
        print(json.dumps({'evidence': str(path)}))
    return 0 if result == 'passed' else 1


if __name__ == '__main__':
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    sys.modules.setdefault('deploy.lifecycle_smoke',sys.modules[__name__])
    raise SystemExit(main())
