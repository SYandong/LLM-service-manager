#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
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


def check_host_capacity(available_gb, weight_bytes):
    # Test-only conservative headroom; never exported as production admission.
    if not math.isfinite(available_gb) or weight_bytes <= 0 or weight_bytes > 20*1024**3:
        raise SmokeError('requires a small complete cache <=20GiB and known host RAM')
    if available_gb < max(64, 2*weight_bytes/1024**3):
        raise SmokeError('insufficient fresh host RAM for the isolated test')


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

    def inventory(self, *, allow_own=False):
        g = self.command(['nvidia-smi', '--query-gpu=index,uuid,memory.total,memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits']).stdout
        p = self.command(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid,process_name,used_memory', '--format=csv,noheader,nounits']).stdout
        rows = [list(map(str.strip, r)) for r in csv.reader(io.StringIO(g))]
        gpu = next(r for r in rows if int(r[0]) == self.config['gpu'])
        if not all(math.isfinite(float(value)) for value in gpu[2:]):
            raise SmokeError('GPU observations are not finite')
        processes = [list(map(str.strip, r)) for r in csv.reader(io.StringIO(p)) if r and r[0].strip() == gpu[1]]
        for process in processes:
            try:
                ours = cgroup_owned((Path('/proc') / process[1] / 'cgroup').read_text(), self.unit)
            except OSError:
                ours = False
            if not allow_own or not ours:
                raise SmokeError('foreign or unclassified process on candidate GPU')
        if not allow_own:
            need = float(gpu[2]) * self.config.get('util', .2) + 4096
            if float(gpu[4]) < need or float(gpu[3]) > 256 or float(gpu[5]) > 0:
                raise SmokeError('candidate GPU is not idle with sufficient memory')
        state = self.python(PREFLIGHT, {**self.config, 'choose_port': self.port is None})
        if not quiet(state) or not state['cached_weights_complete']:
            raise SmokeError('unknown/busy serving or incomplete cached weights')
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
        host_available_gb = int(mem['MemAvailable'].split()[0]) / 1024**2
        check_host_capacity(host_available_gb, state['weight_bytes'])
        if self.port is None:
            self.port = state['port']
        self.log('preflight' if not allow_own else 'monitor', gpu=gpu, processes=processes, in_flight=0, host_available_gb=host_available_gb)
        return gpu

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
        print(json.dumps({'dry_run': True, 'scope': 'cached-base lifecycle only', 'config': config}))
        return 0
    output = Path(config['output_dir'])
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Path(config['lock_path']).open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = Run(config)
        result = run.execute()
        path = output / (run.model + '.json')
        path.write_text(json.dumps({'unit': run.unit, 'token': run.token, 'records': run.records}, indent=2)+'\n');path.chmod(0o600)
        print(json.dumps({'evidence': str(path)}))
    return 0 if result == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
