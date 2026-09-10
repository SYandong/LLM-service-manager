#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Versioned, offline read-only upgrades preserving site configuration and clients."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import manage

Error = manage.DeploymentError


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.upgrade-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def json_write(path, value):
    atomic(path, (json.dumps(value, indent=2, sort_keys=True)+'\n').encode())


def snap(path):
    if path.is_symlink():
        raise Error('unexpected symlink: '+str(path))
    if not path.exists():
        return {'exists': False}
    if not path.is_file() or path.stat().st_size > 4*1024*1024:
        raise Error('not a bounded regular managed file: '+str(path))
    data = path.read_bytes(); st = path.stat()
    return {'exists': True, 'sha256': sha(data), 'data': base64.b64encode(data).decode(),
            'mode': st.st_mode & 0o777, 'uid': st.st_uid, 'gid': st.st_gid}


def same(path, record):
    now = snap(path)
    return now.get('sha256') == record.get('sha256') and now['exists'] == record['exists']


def link_value(path):
    if path.is_symlink():
        value = os.readlink(path)
        if not re.fullmatch(r'releases/[A-Za-z0-9_.+-]+', value):
            raise Error('current pointer escapes owned generations')
        return value
    if path.exists():
        raise Error('current pointer must be a symlink or absent')
    return None


def point(path, value):
    if value is None:
        path.unlink(missing_ok=True); return
    temporary = path.with_name('.current-'+uuid.uuid4().hex)
    os.symlink(value, temporary)
    try: os.replace(temporary, path)
    finally: temporary.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def relative_file(root, name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_./+-]+', name):
        raise Error('unsafe bundle filename')
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or name in ('', '.'):
        raise Error('unsafe bundle path')
    target = root/p
    for part in [target, *target.parents]:
        if part.is_symlink(): raise Error('bundle symlink rejected')
        if part == root: break
    if not target.is_file(): raise Error('missing bundle file: '+name)
    return target



def version_key(value):
    match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?',value)
    if not match:raise Error('unsupported deployment version; use semantic major.minor.patch prerelease')
    return tuple(int(match[i]) for i in (1,2,3))+({'a':0,'b':1,'rc':2,None:3}[match[4]],int(match[5] or 0))


def bundle(path):
    if path.is_symlink() or not path.is_dir(): raise Error('bundle must be a real directory')
    raw = relative_file(path, 'deployment.json').read_bytes()
    value = json.loads(raw)
    if value.get('schema_version') != 1 or value.get('scope') != 'read_only':
        raise Error('manual approval required: bundle is not read_only schema1')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', value.get('version','')):
        raise Error('invalid bundle version')
    if not re.fullmatch(r'[0-9a-f]{40}', value.get('commit','')):
        raise Error('bundle needs exact source commit')
    if not re.fullmatch(r'v[0-9][A-Za-z0-9.+_-]{0,79}', value.get('tag','')):
        raise Error('invalid release tag')
    files = value.get('files', {})
    if not isinstance(files, dict) or not 1 <= len(files) <= 100:
        raise Error('invalid bundle file manifest')
    for name, digest in files.items():
        p = relative_file(path, name)
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or p.stat().st_size > 128*1024*1024 or sha(p.read_bytes()) != digest:
            raise Error('bundle hash/size mismatch: '+name)
    wheels = value.get('install_wheels', [])
    if not isinstance(wheels,list) or not wheels or len(set(wheels)) != len(wheels):
        raise Error('explicit complete wheel list required')
    for name in [value.get('app_wheel'), value.get('cli'), value.get('bootstrap_pip'), *wheels]:
        if name not in files: raise Error('unhashed bundle input')
    if value['app_wheel'] not in wheels or not all(x.endswith('.whl') for x in wheels):
        raise Error('only wheels may be installed')
    if not Path(value['bootstrap_pip']).name.startswith('pip-') or not value['bootstrap_pip'].endswith('.whl'):
        raise Error('pinned bootstrap pip wheel required')
    with zipfile.ZipFile(path/value['app_wheel']) as archive:
        metadata = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(metadata) != 1: raise Error('invalid application wheel')
        text = archive.read(metadata[0]).decode()
        if '\nName: llmsvc\n' not in '\n'+text or '\nVersion: '+value['version']+'\n' not in '\n'+text:
            raise Error('application wheel/version mismatch')
        scripts=[n for n in archive.namelist() if n.endswith('.data/scripts/llm')]
        if len(scripts)!=1:raise Error('wheel must carry the standalone llm script')
        packaged=archive.read(scripts[0]);standalone=(path/value['cli']).read_bytes()
        # Wheel script packaging normalizes only the Python shebang.
        if packaged.partition(b'\n')[0] != b'#!python' or packaged.partition(b'\n')[2] != standalone.partition(b'\n')[2]:
            raise Error('standalone CLI differs from wheel implementation')
    value['bundle_sha256'] = sha(raw)
    value['generation'] = value['version']+'-'+sha(raw)[:12]
    return value


def launcher(python, default_url, *, scheduler=False, shared=None, config=None):
    # Resolve the pointer once. A running client stays on its original environment.
    root = repr(shared) if scheduler else "str(Path(__file__).absolute().parent.parent if Path(__file__).name == 'llm-run' else Path(__file__).absolute().parent)"
    arguments = "['-m','llmsvc','--config',"+repr(config)+",'--dry-run']" if scheduler else "[str(generation/'llm'),*sys.argv[1:]]"
    return (f'''#!{python}
# Generated-By: Codex / gpt-6-astra
import os,re,sys
from pathlib import Path
from urllib.parse import urlsplit
root=Path({root})
try:
    target=os.readlink(root/'current')
    if not re.fullmatch(r'releases/[A-Za-z0-9_.+-]+',target):raise ValueError('invalid generation')
    generation=root/target
    if not (generation/'release.json').is_file():raise ValueError('generation unavailable')
    executable=str(generation/'venv/bin/python')
    if not os.environ.get('LLM_URL'):os.environ['LLM_URL']={default_url!r}
    os.execv(executable,[executable]+{arguments})
except (OSError,ValueError) as exc:
    print('llm: selected runtime unavailable: '+str(exc),file=sys.stderr)
    raise SystemExit(1)
''').encode()


CONFIG_CHECK = '''import json,sys
from llmsvc.config import load_config
c=load_config(sys.argv[1])
flags={k:bool(getattr(c,k,False)) for k in ('model_actions_enabled','placement_enabled','automation_enabled','fault_recovery_enabled','sleeping_recovery_enabled')}
print(json.dumps({'read_only':c.read_only,'flags':flags,'host_meminfo_path':c.collectors.get('host_meminfo_path'),'listen_host':c.listen_host,'listen_port':c.listen_port}))
'''


class Upgrade:
    def __init__(self, settings, root, *, command=None):
        self.cfg = settings; self.root = root
        if not root.is_absolute() or root.resolve() != root: raise Error('canonical explicit root required')
        for key in ('prefix','shared_dir','python','scheduler_url','swap_url','trampoline_path'):
            if not isinstance(settings.get(key),str) or not settings[key]: raise Error('missing upgrade setting: '+key)
        for key in ('prefix','shared_dir','python','trampoline_path'):
            if not re.fullmatch(r'/[A-Za-z0-9_./-]+', settings[key]) or '..' in Path(settings[key]).parts:
                raise Error('unsupported deployment path: '+key)
        for key in ('scheduler_url','swap_url'):
            parts=urlsplit(settings[key])
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.path not in ('','/') or parts.query or parts.fragment or parts.port==0:
                raise Error('service URL must be a plain HTTP(S) origin')
        self.prefix = manage.inside(root, settings['prefix'])
        self.shared = manage.inside(root, settings['shared_dir'])
        if self.prefix == self.shared or self.prefix in self.shared.parents or self.shared in self.prefix.parents:
            raise Error('private installation and public shared roots must not overlap')
        self.manifest_path = manage.inside(root, settings['prefix']+'/manifest.json')
        self.pointer = self.shared/'current'
        self.state_path = manage.inside(root,settings['prefix']+'/upgrade-state.json')
        self.command_override = command
        self.command_timeout = settings.get('command_timeout_seconds',120)
        self.health_timeout = settings.get('health_timeout_seconds',30)
        if type(self.command_timeout) not in (int,float) or not 1 <= self.command_timeout <= 300: raise Error('invalid command deadline')
        if type(self.health_timeout) not in (int,float) or not 1 <= self.health_timeout <= 60: raise Error('invalid health deadline')
        if not self.shared.is_dir() or not self.prefix.is_dir(): raise Error('existing installation/shared directory required')

    def run(self, argv, *, timeout=None, env=None):
        if self.command_override:
            return self.command_override(argv,timeout=timeout or self.command_timeout,env=env)
        result = subprocess.run(argv,capture_output=True,text=True,timeout=timeout or self.command_timeout,env=env,umask=0o022)
        if result.returncode:
            raise Error('command failed: '+Path(argv[0]).name+'; stderr_sha256='+sha(result.stderr.encode()))
        return result

    def load(self):
        data = json.loads(self.manifest_path.read_text())
        if data.get('schema_version') not in (1,2) or data.get('root') != str(self.root): raise Error('installation manifest/root mismatch')
        site = data['settings']
        if site.get('prefix') != self.cfg['prefix'] or site.get('cli_path') != self.cfg['shared_dir']+'/llm': raise Error('site/shared prefix mismatch')
        if Path(site['unit_path']).name != 'llmsvc-scheduler.service': raise Error('only scheduler unit may change')
        self.site = site; self.manifest = data
        self.unit = manage.inside(self.root,site['unit_path']); self.config = manage.inside(self.root,site['config_path'])
        self.cli = manage.inside(self.root,site['cli_path']); self.cli_run = manage.inside(self.root,self.cfg['shared_dir']+'/bin/llm-run')
        self.scheduler_run = manage.inside(self.root,self.cfg['prefix']+'/bin/scheduler-run')
        self.trampoline = manage.inside(self.root,self.cfg['trampoline_path'])
        self.allowed = {str(p) for p in (self.unit,self.config,self.cli,self.cli_run,self.scheduler_run,self.manifest_path)}
        expected = {site[k] for k in ('unit_path','config_path','cli_path')}
        if {x['path'] for x in data['files']} != expected: raise Error('manifest file list mismatch')
        for entry in data['files']:
            path = manage.inside(self.root,entry['path'])
            if entry['path'] != site['config_path'] and sha(path.read_bytes()) != entry['sha256']:
                raise Error('managed runtime/unit file changed; explicit reconciliation required')
        self.protected = {str(self.trampoline):snap(self.trampoline)}
        for entry in data['backups']:
            if entry['path'] not in site['backup_files'] or not re.fullmatch('[0-9]+',entry['file']): raise Error('invalid backup record')
            saved = manage.inside(self.root,site['prefix']+'/backup/'+entry['file'])
            if sha(saved.read_bytes()) != entry['sha256']: raise Error('original backup integrity mismatch')
            self.protected[str(saved)] = snap(saved)
            current = manage.inside(self.root,entry['path']); self.protected[str(current)] = snap(current)
        for path in (self.cli,self.cli_run,self.scheduler_run,self.unit):
            if path.exists() and os.path.samefile(path,self.trampoline): raise Error('fixed trampoline aliases a mutable target')
        if data['schema_version']==2:
            extras=data.get('managed_extra_files',{})
            if set(extras)!={str(self.cli_run),str(self.scheduler_run)} or any(sha(Path(p).read_bytes())!=digest for p,digest in extras.items()):
                raise Error('managed dispatcher integrity mismatch')
        self.before = {str(p):snap(p) for p in (self.unit,self.config,self.cli,self.cli_run,self.scheduler_run,self.manifest_path)}
        self.previous_pointer = link_value(self.pointer)
        if self.previous_pointer:
            self.verify_generation(self.shared/self.previous_pointer)
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.previous_state=state
        if state.get('status') not in (None,'committed','rolled_back','failed_before_switch'):
            raise Error('unfinished upgrade transaction; use explicit rollback '+state.get('transaction',''))

    def guard(self):
        if not all(same(Path(p),x) for p,x in self.before.items()): raise Error('site changed during candidate preparation')
        self.protection_guard()
        if link_value(self.pointer) != self.previous_pointer: raise Error('current generation changed concurrently')

    def protection_guard(self):
        if not all(same(Path(p),x) for p,x in self.protected.items()): raise Error('protected trampoline/data-plane/backup changed')

    def fingerprint(self, path):
        out = {}
        for item in sorted(path.rglob('*')):
            if '__pycache__' in item.parts or item.suffix=='.pyc' or item==path/'release.json': continue
            name = str(item.relative_to(path))
            if item.is_symlink(): out[name] = {'link':os.readlink(item)}
            elif item.is_file(): out[name] = {'sha256':sha(item.read_bytes())}
        return out

    def verify_generation(self, path):
        if path.is_symlink() or path.parent != self.shared/'releases' or path.parent.is_symlink(): raise Error('invalid generation path')
        record=json.loads((path/'release.json').read_text())
        if self.fingerprint(path) != record['runtime_files']: raise Error('retained generation was modified')
        return record

    def prepare(self, directory, payload):
        generation = manage.inside(self.root,self.cfg['shared_dir']+'/releases/'+payload['generation'])
        if generation.exists():
            record=self.verify_generation(generation)
            if record['bundle_sha256'] != payload['bundle_sha256']: raise Error('generation bundle mismatch')
            return generation
        if not generation.parent.exists():
            generation.parent.mkdir(mode=0o755);generation.parent.chmod(0o755)
        elif generation.parent.stat().st_mode & 0o055 != 0o055:
            raise Error('shared releases directory is not client-readable')
        generation.mkdir(mode=0o755);generation.chmod(0o755)
        # Retain failed generations as evidence; they cannot activate without release.json.
        self.run([self.cfg['python'],'-m','venv','--without-pip',str(generation/'venv')])
        python=str(generation/'venv/bin/python')
        env={**os.environ,'PYTHONPATH':str((directory/payload['bootstrap_pip']).resolve()),'PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1','PYTHONDONTWRITEBYTECODE':'1'}
        self.run([python,'-m','pip','install','--no-index','--no-deps','--no-cache-dir',*[str((directory/x).resolve()) for x in payload['install_wheels']]],env=env)
        self.run([python,'-m','pip','check'],env=env)
        self.run([python,'-c',"import llmsvc,textual; assert llmsvc.__version__ == "+repr(payload['version'])])
        atomic(generation/'llm',(directory/payload['cli']).read_bytes(),0o755)
        version=self.run([python,str(generation/'llm'),'--version']).stdout.strip()
        if version != payload['version']: raise Error('installed CLI version mismatch')
        record={k:payload[k] for k in ('tag','version','commit','bundle_sha256','generation')}
        record['runtime_files']=self.fingerprint(generation)
        json_write(generation/'release.json',record);(generation/'release.json').chmod(0o644)
        return generation

    def preflight(self, generation, transaction):
        python=str(generation/'venv/bin/python')
        config=json.loads(self.run([python,'-c',CONFIG_CHECK,str(self.config)]).stdout)
        if config['read_only'] is not True or any(config['flags'].values()):
            raise Error('manual approval required: automatic path accepts read-only config only')
        self.run([python,'-m','llmsvc','--config',str(self.config),'--check-config'])
        once=json.loads(self.run([python,'-m','llmsvc','--config',str(self.config),'--once'],timeout=min(30,self.command_timeout)).stdout)
        if once.get('read_only') is not True: raise Error('candidate once is not read-only')
        if config.get('host_meminfo_path') is not None and once.get('memory',{}).get('host_available_gb') is None:
            raise Error('configured host-memory source unavailable in candidate')
        json_write(transaction/'candidate-once.private.json',once)
        self.validated_config=config
        return config

    def idle(self):
        if self.root != Path('/'):
            manage.emit('upgrade_staging_idle_check',live=False);return
        code='''import json,sys,time,urllib.request
url=sys.argv[1];frames=[];data=[];end=time.monotonic()+3
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(url+'/api/events',timeout=2) as response:
 while time.monotonic()<end:
  line=response.readline(262144)
  if not line:break
  if line.strip()==b'':
   if data:
    frames.append(json.loads(b'\\n'.join(data)));data=[]
    if any(x.get('type')=='inflight' for x in frames) and any(x.get('type')=='modelStatus' for x in frames):break
  elif line.startswith(b'data:'):data.append(line[5:].strip())
print(json.dumps(frames))
'''
        frames=json.loads(self.run([self.cfg['python'],'-c',code,self.cfg['swap_url'].rstrip('/')],timeout=5).stdout)
        inflight=None
        for event in frames:
            data=event.get('data');data=json.loads(data) if isinstance(data,str) else data
            if event.get('type')=='inflight' and data.get('operation')=='snapshot':inflight=data.get('requests')
        if inflight != []: raise Error('fresh inference idle status unknown or busy')
        # Diagnostic scheduling guard for scheduler-only restart, not reload quiet proof.
        manage.emit('upgrade_idle_observed',continuous_quiet_proven=False)

    def live_unit_guard(self):
        if self.root!=Path('/'):return
        raw=self.run(['systemctl','show','llmsvc-scheduler.service','-p','ActiveState,MainPID,FragmentPath,DropInPaths'],timeout=5).stdout
        properties=dict(x.split('=',1) for x in raw.splitlines() if '=' in x)
        if properties.get('ActiveState')!='active' or properties.get('FragmentPath')!=self.site['unit_path'] or properties.get('DropInPaths'):
            raise Error('active scheduler unit/drop-in identity requires explicit reconciliation')

    def restart(self):
        if self.root == Path('/'):
            self.run(['systemctl','daemon-reload'],timeout=15)
            self.run(['systemctl','restart','llmsvc-scheduler.service'],timeout=45)
        else: manage.emit('upgrade_staging_restart',live=False)

    def health(self, version=None):
        if self.root == Path('/'):
            end=time.monotonic()+self.health_timeout
            while True:
                try:
                    left=end-time.monotonic()
                    if left<=0:raise Error('scheduler health deadline')
                    code="import json,sys,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({}),type('NoRedirect',(urllib.request.HTTPRedirectHandler,),{'redirect_request':lambda *a,**k:None})()); r=o.open(sys.argv[1],timeout=2); data=r.read(4194305); assert len(data)<=4194304; print(json.dumps(json.loads(data)))"
                    state=json.loads(self.run([self.cfg['python'],'-c',code,self.cfg['scheduler_url'].rstrip('/')+'/v1/state'],timeout=min(3,left)).stdout)
                    if state.get('read_only') is not True or state.get('sampled_at') is None: raise Error('active scheduler not ready/read-only')
                    if getattr(self,'validated_config',{}).get('host_meminfo_path') and state.get('memory',{}).get('host_available_gb') is None:raise Error('live host-memory source unavailable')
                    if version:
                        pid=self.run(['systemctl','show','llmsvc-scheduler.service','-p','MainPID','--value'],timeout=min(3,max(.1,end-time.monotonic()))).stdout.strip()
                        selected=link_value(self.pointer)
                        executable=(self.cfg['shared_dir']+'/'+selected+'/venv/bin/python') if selected else self.site['prefix']+'/venv/bin/python'
                        check="from pathlib import Path;import sys; a=(Path('/proc')/sys.argv[1]/'cmdline').read_bytes().split(bytes([0])); assert a[0].decode()==sys.argv[2] and b'--dry-run' in a"
                        self.run([self.cfg['python'],'-c',check,pid,executable],timeout=min(3,max(.1,end-time.monotonic())))
                    break
                except (OSError,ValueError,Error,subprocess.SubprocessError):
                    if time.monotonic()>=end: raise Error('scheduler health deadline')
                    time.sleep(.2)
            for name in ('llama-swap.service','vllm-reaper.timer'):
                if self.run(['systemctl','is-active',name],timeout=5).stdout.strip()!='active':raise Error('data-plane/reaper not active')
        result=self.run([self.cfg['python'],str(self.cli),'--version'],timeout=5).stdout.strip()
        if version and result!=version:raise Error('shared CLI health version mismatch')
        if self.root==Path('/'):
            self.run([str(self.cli_run),'status','--json'],timeout=15)
        return result

    def unit_candidate(self):
        old=self.unit.read_text();lines=old.splitlines();indexes=[i for i,x in enumerate(lines) if x.startswith('ExecStart=')]
        expected='ExecStart='+self.cfg['prefix']+'/bin/scheduler-run'
        if len(indexes)!=1 or ('--dry-run' not in lines[indexes[0]] and lines[indexes[0]]!=expected):raise Error('expected one explicitly read-only scheduler ExecStart')
        lines[indexes[0]]='ExecStart='+self.cfg['prefix']+'/bin/scheduler-run'
        return ('\n'.join(lines)+'\n').encode()

    def upgrade(self, directory, *, dry_run=False):
        payload=bundle(directory);self.load()
        manage.emit('upgrade_plan',dry_run=dry_run,version=payload['version'],tag=payload['tag'],site_config_preserved=True,read_only=True)
        if dry_run:return {'dry_run':True,'generation':payload['generation']}
        descriptor=os.open(self.prefix/'upgrade.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.load()
            if self.previous_pointer=='releases/'+payload['generation']:
                self.verify_generation(self.shared/self.previous_pointer);return {'status':'already_current','generation':payload['generation']}
            previous_version=self.manifest.get('upgrade',{}).get('version')
            if previous_version and version_key(payload['version'])<=version_key(previous_version):
                raise Error('automatic downgrade/same-version replacement refused; use saved transaction rollback')
            token=uuid.uuid4().hex;transaction=manage.inside(self.root,self.cfg['prefix']+'/transactions/'+token);transaction.mkdir(mode=0o700,parents=True)
            planned={str(self.unit):self.unit_candidate(),str(self.cli):launcher(self.cfg['python'],self.cfg['scheduler_url']),str(self.cli_run):launcher(self.cfg['python'],self.cfg['scheduler_url']),str(self.scheduler_run):launcher(self.cfg['python'],self.cfg['scheduler_url'],scheduler=True,shared=self.cfg['shared_dir'],config=self.site['config_path'])}
            record={'schema_version':1,'transaction':token,'status':'preparing','root':str(self.root),'upgrade_settings':self.cfg,'before':self.before,'previous_upgrade_state':self.previous_state,'previous_pointer':self.previous_pointer,'new_pointer':'releases/'+payload['generation'],'protected':self.protected,'planned_sha256':{p:sha(x) for p,x in planned.items()},'version':payload['version'],'tag':payload['tag'],'bundle_sha256':payload['bundle_sha256']}
            json_write(transaction/'transaction.json',record);json_write(self.state_path,{'transaction':token,'status':'preparing'})
            try:
                record['previous_cli_version']=self.run([self.cfg['python'],str(self.cli),'--version'],timeout=5).stdout.strip()
                generation=self.prepare(directory,payload);record['validated_config']=self.preflight(generation,transaction);self.guard();self.live_unit_guard();self.idle();self.guard()
                record['status']='switching';json_write(transaction/'transaction.json',record);json_write(self.state_path,{'transaction':token,'status':'switching'})
                # Pointer first supports the new directory launchers; old clients retain old envs.
                point(self.pointer,record['new_pointer'])
                for name,data in planned.items():atomic(Path(name),data,0o644 if Path(name)==self.unit else 0o755)
                self.restart();self.protection_guard()
                if not same(self.config,self.before[str(self.config)]):raise Error('operator config changed during switch')
                self.health(payload['version'])
                new=json.loads(json.dumps(self.manifest));new['schema_version']=2
                new['files']=[{'path':self.site[k],'sha256':sha(manage.inside(self.root,self.site[k]).read_bytes())} for k in ('unit_path','config_path','cli_path')]
                new['managed_extra_files']={str(p):sha(p.read_bytes()) for p in (self.cli_run,self.scheduler_run)}
                if self.root==Path('/'):new['activated']=True
                new['upgrade']={'transaction':token,'generation':payload['generation'],'version':payload['version'],'tag':payload['tag'],'bundle_sha256':payload['bundle_sha256'],'site_config_origin':'validated current operator bytes, prior manifest retained in transaction'}
                encoded=(json.dumps(new,indent=2,sort_keys=True)+'\n').encode();record['planned_sha256'][str(self.manifest_path)]=sha(encoded)
                json_write(transaction/'transaction.json',record);atomic(self.manifest_path,encoded)
                record['status']='committed';json_write(transaction/'transaction.json',record);json_write(self.state_path,{'transaction':token,'status':'committed'})
                manage.emit('upgrade_complete',transaction=token,version=payload['version'],old_environments_retained=True)
                return {'status':'committed','transaction':token,'generation':payload['generation']}
            except BaseException:
                if record['status']=='preparing':
                    record['status']='failed_before_switch';json_write(transaction/'transaction.json',record);json_write(self.state_path,record['previous_upgrade_state'] or {'transaction':token,'status':'failed_before_switch'})
                else:
                    self.restore(record,transaction)
                raise

    def restore(self, record, transaction):
        # Validate every affected byte/link before any rollback mutation.
        if record['root']!=str(self.root) or record['upgrade_settings']!=self.cfg or set(record['before'])!=self.allowed:
            raise Error('transaction scope mismatch')
        if link_value(self.pointer) not in (record['previous_pointer'],record['new_pointer']):raise Error('rollback pointer conflict')
        for name,before in record['before'].items():
            now=snap(Path(name))
            if now.get('sha256') not in (before.get('sha256'),record['planned_sha256'].get(name)):
                raise Error('rollback file conflict; preserve operator change')
        for name,before in record['protected'].items():
            if not same(Path(name),before):raise Error('rollback protected-file conflict')
        record['status']='rolling_back';json_write(transaction/'transaction.json',record);json_write(self.state_path,{'transaction':record['transaction'],'status':'rolling_back'})
        if record['previous_pointer'] is not None:point(self.pointer,record['previous_pointer'])
        for name,before in record['before'].items():
            path=Path(name)
            if same(path,before):continue
            if before['exists']:
                atomic(path,base64.b64decode(before['data']),before['mode'])
                if os.geteuid()==0:os.chown(path,before['uid'],before['gid'])
            else:path.unlink(missing_ok=True)
        if record['previous_pointer'] is None:point(self.pointer,None)
        self.validated_config=record.get('validated_config',{})
        self.restart();self.protection_guard();self.health(record.get('previous_cli_version'))
        record['status']='rolled_back';json_write(transaction/'transaction.json',record);json_write(self.state_path,record.get('previous_upgrade_state') or {'transaction':record['transaction'],'status':'rolled_back'})
        manage.emit('upgrade_rolled_back',transaction=record['transaction'],old_environments_retained=True)
        return {'status':'rolled_back','transaction':record['transaction']}

    def rollback(self, token, *, dry_run=False):
        if not re.fullmatch('[0-9a-f]{32}',token):raise Error('invalid transaction id')
        transaction=manage.inside(self.root,self.cfg['prefix']+'/transactions/'+token);record=json.loads(manage.inside(self.root,self.cfg['prefix']+'/transactions/'+token+'/transaction.json').read_text())
        self.site=json.loads(base64.b64decode(record['before'][str(self.manifest_path)]['data']))['settings']
        self.unit=manage.inside(self.root,self.site['unit_path']);self.config=manage.inside(self.root,self.site['config_path']);self.cli=manage.inside(self.root,self.site['cli_path']);self.cli_run=manage.inside(self.root,self.cfg['shared_dir']+'/bin/llm-run');self.scheduler_run=manage.inside(self.root,self.cfg['prefix']+'/bin/scheduler-run')
        self.allowed={str(p) for p in (self.unit,self.config,self.cli,self.cli_run,self.scheduler_run,self.manifest_path)};self.protected=record['protected']
        state=json.loads(self.state_path.read_text())
        if record.get('status')=='rolled_back' and link_value(self.pointer)==record['previous_pointer'] and all(same(Path(p),entry) for p,entry in record['before'].items()):
            return {'status':'already_rolled_back','transaction':token,'dry_run':dry_run}
        if state.get('transaction')!=token:raise Error('rollback is not the current transaction')
        manage.emit('upgrade_rollback_plan',transaction=token,dry_run=dry_run)
        if dry_run:return {'dry_run':True,'transaction':token}
        descriptor=os.open(self.prefix/'upgrade.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if json.loads(self.state_path.read_text()).get('transaction')!=token:raise Error('rollback transaction changed')
            self.idle()
            return self.restore(record,transaction)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('apply','rollback'))
    parser.add_argument('--settings',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--transaction')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    try:
        manage.JOURNAL=args.root==Path('/') and not args.dry_run
        upgrade=Upgrade(json.loads(args.settings.read_text()),args.root)
        if args.action=='apply':
            if not args.bundle:raise Error('apply requires --bundle')
            result=upgrade.upgrade(args.bundle,dry_run=args.dry_run)
        else:result=upgrade.rollback(args.transaction or '',dry_run=args.dry_run)
        print(json.dumps(result));return 0
    except (Error,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        manage.emit('upgrade_error',error=str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
