#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Explicit native maintenance adapter and attributable oneshot helper jobs.

No service is provisioned by import, validation, or observation. A site must
configure/pin this adapter, its source unit and managed helper command first.
"""
import argparse
import contextlib
import errno
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.maintenance_executor import (ExecutorError, ScopeInspector, canonical,
                                        digest, run_bounded, validate_identity)

UNIT_KEYS = ('Id','LoadState','ActiveState','SubState','MainPID','ControlGroup',
             'InvocationID','FragmentPath','DropInPaths','Environment','Job','Restart')
JOB_KEYS = ('Result','ExecMainCode','ExecMainStatus','ExecMainStartTimestampMonotonic',
            'ExecMainExitTimestampMonotonic','ExecStart','RemainAfterExit','Transient')
TAG_KEYS = ('LLMSVC_MAINT_TRANSACTION','LLMSVC_MAINT_OPERATION','LLMSVC_MAINT_PHASE')


def file_bytes(path, limit=4*1024*1024):
    p = Path(path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ExecutorError('bounded_regular_file_required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit+1)
        if len(data)>limit:
            raise ExecutorError('file_limit')
        return data
    finally:
        os.close(fd)


def private_json(path):
    p=Path(path);info=p.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ExecutorError('private_owner_only_file_required')
    return json.loads(file_bytes(p))


def private_create(path, value):
    data=(canonical(value)+'\n').encode();p=Path(path)
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    directory=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)



def private_update(path, expected, changes):
    if private_json(path)!=expected:raise ExecutorError('native_record_changed')
    value={**expected,**changes};temporary=Path(path).with_name('.record-'+uuid.uuid4().hex)
    private_create(temporary,value)
    try:
        if private_json(path)!=expected:raise ExecutorError('native_record_changed')
        os.replace(temporary,path)
        directory=os.open(Path(path).parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)
    return value


def private_directory(path):
    p=Path(path)
    if not p.is_absolute() or p.resolve()!=p:
        raise ExecutorError('canonical_private_directory_required')
    info=p.stat()
    if not p.is_dir() or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
        raise ExecutorError('private_directory_ownership_required')
    return p


def environment(value):
    result={}
    for item in shlex.split(value):
        name,sep,val=item.partition('=')
        if not sep or name in result:raise ExecutorError('ambiguous_unit_environment')
        result[name]=val
    return result


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',value):
        raise ExecutorError('invalid_operation_identifier')
    return value


def helper_unit(scope_hash, model):
    return 'llmsvc-maint-helper-'+digest([scope_hash,model])[:32]+'.service'


class NativeHTTP:
    """Literal HTTP IP only, with one absolute socket watchdog and no redirect."""
    def __init__(self, origin):
        try:
            parsed=urlsplit(origin);address=ipaddress.ip_address(parsed.hostname)
            port=parsed.port or 80
        except (ValueError,TypeError) as exc:raise ExecutorError('literal_http_origin_required') from exc
        if (parsed.scheme!='http' or parsed.username or parsed.password or parsed.path not in ('','/')
                or parsed.query or parsed.fragment or getattr(address,'scope_id',None) or not 0<port<65536):
            raise ExecutorError('literal_http_origin_required')
        self.host,self.port,self.family=str(address),port,socket.AF_INET6 if address.version==6 else socket.AF_INET

    @contextlib.contextmanager
    def open(self,method,path,deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ExecutorError('http_deadline')
        sock=socket.socket(self.family,socket.SOCK_STREAM);connection=http.client.HTTPConnection(self.host,self.port)
        expired=threading.Event()
        def expire():
            expired.set()
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            sock.close()
        timer=threading.Timer(remaining,expire);timer.daemon=True;timer.start()
        try:
            sock.settimeout(remaining);sock.connect((self.host,self.port));connection.sock=sock
            if expired.is_set():raise ExecutorError('http_deadline')
            connection.request(method,path);response=connection.getresponse()
            if not 200<=response.status<300:raise ExecutorError('native_http_rejected')
            yield response
            if expired.is_set() or time.monotonic()>=deadline:raise ExecutorError('http_deadline')
        finally:
            timer.cancel();connection.close();sock.close();timer.join(timeout=1)

    def json(self,method,path,deadline):
        with self.open(method,path,deadline) as response:
            raw=response.read(1048577)
            if len(raw)>1048576:raise ExecutorError('http_body_limit')
        return json.loads(raw)

    def snapshot(self,deadline):
        from llmsvc.collectors.events import EventSnapshot
        value=EventSnapshot();frame=[];consumed=0
        with self.open('GET','/api/events',deadline) as response:
            while time.monotonic()<deadline:
                line=response.readline(262145);consumed+=len(line)
                if not line or len(line)>262144 or consumed>4194304:raise ExecutorError('incomplete_native_snapshot')
                if not line.strip():
                    if frame:value.feed(json.loads(b'\n'.join(frame)));frame=[]
                    if value.complete:return value
                elif line.startswith(b'data:'):frame.append(line[5:].strip())
        raise ExecutorError('native_snapshot_deadline')


class NativeAdapter(ScopeInspector):
    def __init__(self, profile, profile_path, **kwargs):
        super().__init__(profile,**kwargs)
        self.profile_path=Path(profile_path)
        self.profile_hash=hashlib.sha256(file_bytes(self.profile_path,65536)).hexdigest()
        self.config=Path(profile['native_config_path'])
        if self.config.parent!=Path(profile['native_config_dir']):raise ExecutorError('native_config_directory_mismatch')
        self.http=NativeHTTP(profile['native_origin'])
        host=profile['listen_host'];port=profile['listen_port']
        if not isinstance(host,str):raise ExecutorError('literal_or_empty_listen_host_required')
        try:listen=None if host=='' else ipaddress.ip_address(host)
        except (ValueError,TypeError) as exc:
            raise ExecutorError('literal_or_empty_listen_host_required') from exc
        if type(port) is not int or not 0<port<65536 or port!=self.http.port:
            raise ExecutorError('native_listen_port_mismatch')
        # Empty host preserves the native :PORT spelling. It is a bind identity,
        # never an HTTP destination, and needs an explicit reachability contract.
        origins=profile.get('native_probe_origins')
        if origins is None:
            if listen is None:raise ExecutorError('wildcard_probe_origins_required')
            origins=[profile['native_origin']]
        if (not isinstance(origins,list) or not 1<=len(origins)<=4
                or profile['native_origin'] not in origins):
            raise ExecutorError('invalid_native_probe_origins')
        endpoints=set()
        for origin in origins:
            if not isinstance(origin,str):raise ExecutorError('invalid_native_probe_origins')
            probe=NativeHTTP(origin);address=ipaddress.ip_address(probe.host)
            if probe.port!=port:raise ExecutorError('native_probe_port_mismatch')
            if listen is None or listen.is_unspecified:
                if not address.is_loopback:raise ExecutorError('native_origin_not_local_listener')
                if listen is not None and listen.version==4 and address.version!=4:
                    raise ExecutorError('native_probe_address_family_mismatch')
            elif address!=listen:
                raise ExecutorError('native_origin_not_local_listener')
            endpoint=(probe.host,probe.port)
            if endpoint in endpoints:raise ExecutorError('duplicate_native_probe_origin')
            endpoints.add(endpoint)
        self.native_probe_origins=tuple(origins)
        self.state=private_directory(profile['state_dir'])
        self.envfile=Path(profile['launch_environment_file'])
        if self.envfile.parent!=self.state:raise ExecutorError('launch_environment_outside_state')
        fragment=file_bytes(profile['fragment_path'],65536).decode()
        unsupported=('ExecStartPre=','ExecStartPost=','ExecStop=','ExecStopPost=','ExecCondition=')
        if any(line.strip().startswith(unsupported) and line.split('=',1)[1].strip() for line in fragment.splitlines()):
            raise ExecutorError('untracked_source_unit_lifecycle_hook')
        if self.envfile.is_symlink():raise ExecutorError('launch_environment_symlink')
        if self.envfile.exists():
            info=self.envfile.stat()
            if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ExecutorError('launch_environment_owner_required')
            for line in file_bytes(self.envfile,65536).decode().splitlines():
                if line.strip() and (line.split('=',1)[0] not in TAG_KEYS or '=' not in line):raise ExecutorError('unexpected_launch_environment')
        self.program=Path(__file__).resolve()
        if hashlib.sha256(file_bytes(self.program)).hexdigest()!=profile['helper_program_sha256']:
            raise ExecutorError('helper_program_changed')
        self.python=profile['helper_python']
        if any(not re.fullmatch(r'/[A-Za-z0-9_./+-]+',str(path)) for path in (self.python,self.program,self.state,self.profile_path)):
            raise ExecutorError('unambiguous_absolute_helper_paths_required')
        timeout=profile.get('helper_timeout_seconds',120)
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=900:
            raise ExecutorError('invalid_helper_timeout')
        auxiliaries=profile.get('source_auxiliaries',[])
        if not isinstance(auxiliaries,list) or len(auxiliaries)>16:raise ExecutorError('invalid_source_auxiliaries')
        for auxiliary in auxiliaries:
            if (not isinstance(auxiliary,dict) or not isinstance(auxiliary.get('argv'),list) or not auxiliary['argv']
                    or any(not isinstance(arg,str) or not arg or '\x00' in arg for arg in auxiliary['argv'])
                    or not re.fullmatch('[0-9a-f]{64}',auxiliary.get('sha256',''))):raise ExecutorError('invalid_source_auxiliary')
        self.models=profile.get('models',{})
        if not isinstance(self.models,dict) or len(self.models)>128:raise ExecutorError('invalid_native_profiles')
        for name,row in self.models.items():
            identifier(name)
            if not isinstance(row,dict) or not re.fullmatch(r'vllm-[A-Za-z0-9_.-]+\.service',row.get('unit','')):
                raise ExecutorError('invalid_backend_unit_profile')
            NativeHTTP(row['backend_origin'])
            if (not isinstance(row.get('process_argv'),list) or not row['process_argv']
                    or any(not isinstance(arg,str) or not arg or '\x00' in arg for arg in row['process_argv'])):
                raise ExecutorError('model_process_argv_required')
        if len({row['unit'] for row in self.models.values()})!=len(self.models):raise ExecutorError('duplicate_backend_unit_profile')

    def show(self,unit,deadline,job=False):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',unit):raise ExecutorError('invalid_unit')
        keys=UNIT_KEYS+(JOB_KEYS if job else ())
        raw=self.runner([self.command,'show',unit,'--no-pager','--property='+','.join(keys)],deadline)
        values={}
        for line in raw.splitlines():
            key,sep,value=line.partition('=')
            if not sep or key in values:raise ExecutorError('invalid_systemd_observation')
            values[key]=value
        if any(k not in values for k in keys) or values['Id']!=unit or not values['MainPID'].isdecimal():
            raise ExecutorError('incomplete_systemd_observation')
        if unit==self.profile['unit'] and values['LoadState']=='loaded':
            if values['DropInPaths'] or values['FragmentPath']!=self.profile['fragment_path']:
                raise ExecutorError('native_unit_definition_changed')
        return values

    def unit_exited(self,unit,deadline):
        p=self.show(unit,deadline)
        return (p['LoadState'] in ('loaded','not-found') and p['ActiveState'] in ('inactive','failed')
                and p['MainPID']=='0' and not p['ControlGroup'] and p['Job'] in ('','0'))

    def listener_owned(self,origin,group,deadline):
        target=NativeHTTP(origin);address=ipaddress.ip_address(target.host)
        if not group or '..' in Path(group).parts:return False
        members=self.members(self.cgroups/group.lstrip('/'));inodes=set();count=0
        for pid in members:
            for fd in (self.proc/str(pid)/'fd').iterdir():
                if time.monotonic()>=deadline:raise ExecutorError('listener_observation_deadline')
                count+=1
                if count>8192:raise ExecutorError('listener_descriptor_limit')
                try:value=os.readlink(fd)
                except FileNotFoundError:continue
                if value.startswith('socket:[') and value.endswith(']'):inodes.add(value[8:-1])
        matches=[]
        for name,version in [('tcp',4),('tcp6',6)]:
            for line in file_bytes(self.proc/'net'/name,4*1024*1024).decode().splitlines()[1:]:
                fields=line.split()
                if len(fields)<10 or fields[3]!='0A':continue
                raw,port=fields[1].split(':')
                if int(port,16)!=target.port:continue
                data=bytes.fromhex(raw)
                data=data[::-1] if version==4 else b''.join(data[i:i+4][::-1] for i in range(0,16,4))
                local=ipaddress.ip_address(data)
                local=getattr(local,'ipv4_mapped',None) or local
                if local==address or local.is_unspecified and (local.version==address.version or local.version==6 and address.version==4):
                    matches.append(fields[9])
        return bool(matches) and all(inode in inodes for inode in matches)

    def _native_snapshot(self,group,deadline):
        # Probe all configured families through their literal destinations. An
        # IPv6 wildcard inode alone cannot prove IPv4 reachability (V6ONLY).
        # Read the primary snapshot last so its in-flight sample is the newest.
        origins=[o for o in self.native_probe_origins if o!=self.profile['native_origin']]
        origins.append(self.profile['native_origin'])
        snapshot=None
        for origin in origins:
            if not self.listener_owned(origin,group,deadline):
                raise ExecutorError('native_listener_not_owned')
            http=self.http if origin==self.profile['native_origin'] else NativeHTTP(origin)
            snapshot=http.snapshot(deadline)
        for origin in origins:
            if not self.listener_owned(origin,group,deadline):
                raise ExecutorError('native_listener_changed_during_probe')
        return snapshot

    def backend(self,name,deadline,expected=None):
        row=self.models.get(name)
        if row is None:raise ExecutorError('backend_profile_missing')
        p=self.show(row['unit'],deadline);env=environment(p['Environment'])
        if (p['LoadState']!='loaded' or p['ActiveState']!='active' or int(p['MainPID'])<=0
                or not re.fullmatch('[0-9a-f]{32}',p['InvocationID']) or int(p['InvocationID'],16)==0
                or p['Job'] not in ('','0') or p['DropInPaths'] or p['Restart'] not in ('no','on-failure','on-abnormal')):
            raise ExecutorError('backend_instance_unconfirmed')
        lease=env.get('LLMSVC_LEASE_ID');gpu=env.get('CUDA_VISIBLE_DEVICES','')
        if not lease or not gpu.isdecimal():raise ExecutorError('backend_lease_or_gpu_unknown')
        process=self.process(int(p['MainPID']))
        if process is None:raise ExecutorError('backend_pid_changed')
        if not self.listener_owned(row['backend_origin'],p['ControlGroup'],deadline):
            raise ExecutorError('backend_listener_not_owned')
        observed={'model':name,'unit':row['unit'],'lease_id':lease,'gpu':int(gpu),
                  'invocation_id':p['InvocationID'],'pid':process['pid'],'start_ticks':process['start_ticks']}
        if expected is not None and any(observed.get(k)!=expected.get(k) for k in expected):
            raise ExecutorError('backend_binding_changed')
        return observed

    def config_data(self):
        import yaml
        raw=file_bytes(self.config);data=yaml.safe_load(raw)
        if not isinstance(data,dict) or not isinstance(data.get('models'),dict):raise ExecutorError('invalid_native_config')
        return raw,data

    def validate(self,path,expected_sha256,deadline,*,dry_run=False):
        import yaml
        raw=file_bytes(path);cfg=yaml.safe_load(raw)
        if not isinstance(cfg,dict) or not isinstance(cfg.get('models'),dict):
            raise ExecutorError('invalid_native_model_configuration')
        for name,model in cfg['models'].items():
            if name not in self.models or not isinstance(model,dict):
                raise ExecutorError('native_candidate_profile_missing')
            if shlex.split(model.get('cmdStop',''))!=self._helper_argv(name):
                raise ExecutorError('native_candidate_helper_untracked')
        hooks=cfg.get('hooks',{}).get('on_startup',{})
        if hooks.get('profile'):
            raise ExecutorError('startup_profile_requires_explicit_managed_preload')
        preload=hooks.get('preload',[])
        if not isinstance(preload,list) or any(name not in self.models for name in preload):
            raise ExecutorError('native_preload_profile_missing')
        return super().validate(path,expected_sha256,deadline,dry_run=dry_run)

    def _helper_argv(self,name):
        return [self.python,'-B',str(self.program),'helper','--profile',str(self.profile_path),
                '--model',name,'--pid','${PID}']

    def native_image(self,pid):
        # /proc/PID/exe is intentionally followed: it identifies the running
        # image, not just a same-named replacement on disk.
        hasher=hashlib.sha256();count=0
        with (self.proc/str(pid)/'exe').open('rb') as stream:
            for block in iter(lambda:stream.read(1048576),b''):
                count+=len(block)
                if count>256*1048576:raise ExecutorError('native_image_limit')
                hasher.update(block)
        if hasher.hexdigest()!=self.profile['native_binary_sha256']:
            raise ExecutorError('running_native_image_unpinned')
        argv=[p.decode() for p in file_bytes(self.proc/str(pid)/'cmdline',65536).split(b'\0') if p]
        configs=[]
        for index,arg in enumerate(argv):
            if arg=='-config' and index+1<len(argv):configs.append(argv[index+1])
            elif arg.startswith('-config='):configs.append(arg.split('=',1)[1])
        if configs!=[str(self.config)]:raise ExecutorError('native_config_argument_unbound')
        listens=[]
        for index,arg in enumerate(argv):
            if arg=='-listen' and index+1<len(argv):listens.append(argv[index+1])
            elif arg.startswith('-listen='):listens.append(arg.split('=',1)[1])
        host=self.profile['listen_host'];expected=('['+host+']' if ':' in host else host)+':'+str(self.profile['listen_port'])
        if listens!=[expected]:raise ExecutorError('native_listener_argument_unbound')

    def inspect_native(self,context,deadline):
        raw,cfg=self.config_data();base=super().inspect(deadline)
        source=self.show(self.profile['unit'],deadline)
        if source['Restart']!='no':raise ExecutorError('native_source_requires_no_automatic_restart')
        self.native_image(base['identity']['pid'])
        snapshot=self._native_snapshot(base['scope']['control_group'],deadline)
        active=[name for name,state in snapshot.states.items() if state!='stopped']
        if any(snapshot.states[n]!='ready' for n in active):raise ExecutorError('native_model_transition_in_progress')
        account_map=None
        accounts=context.get('current_accounts',context.get('accounts'))
        if accounts is not None:
            account_map={}
            for lease,unit in accounts:
                if lease['model'] in account_map:raise ExecutorError('duplicate_backend_account')
                account_map[lease['model']]=(lease,unit)
        hooks=cfg.get('hooks',{}).get('on_startup',{})
        if hooks.get('profile'):raise ExecutorError('startup_profile_requires_explicit_managed_preload')
        preload=hooks.get('preload',[])
        if not isinstance(preload,list):raise ExecutorError('invalid_native_preload')
        needed=set(active)|set(preload)
        bindings=[]
        for name in (account_map if account_map is not None else sorted(needed)):
            b=self.backend(name,deadline)
            if account_map is not None:
                lease,unit=account_map[name]
                if (b['unit']!=unit or b['lease_id']!=lease['lease_id'] or b['gpu']!=lease['gpu']
                        or lease['status']!='confirmed'):raise ExecutorError('backend_account_mismatch')
            bindings.append(b)
        if account_map is not None and not needed<=account_map.keys():raise ExecutorError('active_native_model_unleased')
        actors=base['actors'];actor_rows={p['pid']:self.process(p['pid']) for p in actors}
        helpers={};roots={};auxiliary=set()
        for actor in actors:
            if actor['pid']==base['identity']['pid']:continue
            args=[x.decode() for x in file_bytes(self.proc/str(actor['pid'])/'cmdline',65536).split(b'\0') if x]
            parent=int((self.proc/str(actor['pid'])/'stat').read_text().rsplit(') ',1)[1].split()[1])
            for allowed in self.profile.get('source_auxiliaries',[]):
                if args==allowed.get('argv') and parent==base['identity']['pid']:
                    hasher=hashlib.sha256();count=0
                    with (self.proc/str(actor['pid'])/'exe').open('rb') as stream:
                        for block in iter(lambda:stream.read(1048576),b''):
                            count+=len(block)
                            if count>256*1048576:raise ExecutorError('native_auxiliary_image_limit')
                            hasher.update(block)
                    if hasher.hexdigest()!=allowed.get('sha256'):
                        raise ExecutorError('native_auxiliary_image_changed')
                    auxiliary.add(actor['pid'])
        for name in active:
            profile=self.models.get(name);model=cfg['models'].get(name)
            if profile is None or not isinstance(model,dict):raise ExecutorError('native_model_profile_missing')
            if shlex.split(model.get('cmdStop',''))!=self._helper_argv(name):raise ExecutorError('untracked_native_stop_helper')
            if shlex.split(model.get('cmd',''))!=profile['process_argv']:raise ExecutorError('native_model_command_profile_changed')
            found=[]
            for actor in actors:
                args=[x.decode() for x in file_bytes(self.proc/str(actor['pid'])/'cmdline',65536).split(b'\0') if x]
                if args==profile['process_argv']:found.append(actor)
            if len(found)!=1:raise ExecutorError('native_model_actor_ambiguous')
            roots[found[0]['pid']]=name;helpers[name]={'wrapper':found[0],'backend':next(b for b in bindings if b['model']==name)}
        for actor in actors:
            pid=actor['pid'];seen=set()
            while pid!=base['identity']['pid'] and pid not in roots and pid not in auxiliary:
                if pid in seen or pid not in actor_rows:raise ExecutorError('native_actor_unattributed')
                seen.add(pid)
                fields=(self.proc/str(pid)/'stat').read_text().rsplit(') ',1)[1].split();pid=int(fields[1])
            # Direct children of the proxy must themselves match a model root.
            if actor['pid']!=base['identity']['pid'] and pid==base['identity']['pid']:
                raise ExecutorError('native_actor_unattributed')
        scope={**base['scope'],'profile_sha256':self.profile_hash,'config_sha256':hashlib.sha256(raw).hexdigest()}
        helper_models={n:{'wrapper_pid':v['wrapper']['pid'],'wrapper_start_ticks':v['wrapper']['start_ticks'],
                          'backend':v['backend']} for n,v in helpers.items()}
        scope_hash=digest(scope)
        identity={**base['identity'],'scope_sha256':scope_hash}
        actors=[{**p,'scope_sha256':scope_hash} for p in actors]
        self.native_image(base['identity']['pid'])
        if file_bytes(self.config)!=raw or super().inspect(deadline)['identity']!=base['identity']:
            raise ExecutorError('native_source_changed_during_observation')
        return {'identity':identity,'scope':scope,'actors':actors,'backend_bindings':bindings,'helper_models':helper_models,
                'exclusion_method':'stop_instance','in_flight':len(snapshot.requests),
                'actors_known':True,'config_sha256':scope['config_sha256'],
                'configuration_confirmed':True,'observed_at':time.monotonic()}

    def _scope_guard(self,scope,identity):
        validate_identity(identity)
        if (not isinstance(scope,dict) or digest(scope)!=identity['scope_sha256']
                or scope.get('unit')!=self.profile['unit'] or scope.get('profile_sha256')!=self.profile_hash
                or scope.get('fragment_sha256')!=self.profile['fragment_sha256']
                or scope.get('boot_id')!=(self.proc/'sys/kernel/random/boot_id').read_text().strip()):
            raise ExecutorError('native_scope_binding_changed')
        if hashlib.sha256(file_bytes(Path(self.profile['fragment_path']))).hexdigest()!=scope['fragment_sha256']:
            raise ExecutorError('native_unit_fragment_changed')

    def _claim_path(self,scope):
        invocation=scope.get('invocation_id','')
        if not re.fullmatch('[0-9a-f]{32}',invocation):raise ExecutorError('invalid_scope_invocation')
        return self.state/('stop-'+invocation+'.json')

    def _job(self,scope,name,models=None):
        if models is None:
            models=private_json(self.state/('stop-scope-'+digest(scope)+'.json'))['helper_models']
        model=models[name];key=digest([digest(scope),name])
        return self.state/('helper-'+key+'.json'),helper_unit(digest(scope),name),model

    def _helper_settled(self,scope,context,deadline):
        details=[]
        try:
            claim=private_json(self.state/('stop-scope-'+digest(scope)+'.json'))
            if claim['scope']!=scope or claim['transaction_id']!=context['transaction_id'] or claim['operation_id']!=context['operation_id']:
                raise ExecutorError('helper_scope_claim_mismatch')
            models=claim['helper_models']
        except (OSError,ValueError,KeyError,ExecutorError):
            return False,[{'settled':False,'reason':'helper_scope_claim_unknown'}]
        for name in models:
            path,unit,_=self._job(scope,name,models)
            try:
                desc=private_json(path);p=self.show(unit,deadline,job=True);env=environment(p['Environment'])
                started=private_json(path.with_suffix('.started.json'))
                expected={'LLMSVC_MAINT_TRANSACTION':context['transaction_id'],'LLMSVC_MAINT_OPERATION':context['operation_id'],
                          'LLMSVC_MAINT_SCOPE':digest(scope)}
                argv=[self.python,'-B',str(self.program),'job','--profile',str(self.profile_path),'--record',str(path)]
                match=re.search(r'^\{ path=([^;]+) ; argv\[\]=([^;]+) ;',p['ExecStart'])
                command_ok=bool(match and match[1].strip()==self.python and match[2].strip()==' '.join(argv))
                no_members=not p['ControlGroup'] or not self.members(self.cgroups/p['ControlGroup'].lstrip('/'))
                ok=(desc['scope']==scope and desc['transaction_id']==context['transaction_id']
                    and desc['operation_id']==context['operation_id'] and all(env.get(k)==v for k,v in expected.items())
                    and p['LoadState']=='loaded' and p['ActiveState']=='active' and p['SubState']=='exited'
                    and p['Result']=='success' and p['ExecMainCode']=='1' and p['ExecMainStatus']=='0'
                    and p['MainPID']=='0' and p['RemainAfterExit']=='yes' and p['Transient']=='yes'
                    and started['invocation_id']==p['InvocationID']
                    and started['record_sha256']==hashlib.sha256(file_bytes(path)).hexdigest()
                    and p['Job'] in ('','0') and int(p['ExecMainStartTimestampMonotonic'])>0
                    and int(p['ExecMainExitTimestampMonotonic'])>=int(p['ExecMainStartTimestampMonotonic'])
                    and not p['DropInPaths'] and command_ok and no_members
                    and not path.with_suffix('.duplicate').exists())
                details.append({'model':name,'unit':unit,'settled':ok,'result':p['Result'],'exit_status':p['ExecMainStatus']})
            except (OSError,ValueError,KeyError,ExecutorError):details.append({'model':name,'unit':unit,'settled':False,'reason':'helper_outcome_unknown'})
        return all(x['settled'] for x in details),details

    def _backends(self,context,deadline,cleanup=False):
        current={lease['model']:(lease,unit) for lease,unit in context.get('current_accounts',context.get('accounts',[]))}
        bindings=context.get('backend_bindings')
        if not isinstance(bindings,list):return False
        released=set()
        for binding in bindings:
            name=binding['model']
            submitted=context.get('effects',{}).get('stop_model:'+name,{}).get('submitted') is True
            if name not in current and submitted and self.unit_exited(binding['unit'],deadline):
                released.add(name);continue
            if cleanup and name in context.get('removed_models',[]):return False
            if name not in current:return False
            lease,unit=current[name]
            if lease['lease_id']!=binding['lease_id'] or unit!=binding['unit'] or lease['status']!='confirmed':return False
            try:self.backend(name,deadline,binding)
            except ExecutorError:return False
        return set(current)=={b['model'] for b in bindings}-released

    def _retired(self,identity,scope,actors,context,deadline):
        self._scope_guard(scope,identity)
        if not isinstance(actors,list) or identity not in actors:raise ExecutorError('native_actor_inventory_missing')
        if any(validate_identity(actor)['scope_sha256']!=identity['scope_sha256'] for actor in actors):
            raise ExecutorError('native_actor_scope_mismatch')
        claim=private_json(self.state/('stop-scope-'+identity['scope_sha256']+'.json'))
        if claim['identity']!=identity or claim['transaction_id']!=context['transaction_id']:
            raise ExecutorError('native_retirement_claim_mismatch')
        actors=actors+[actor for actor in claim['actors'] if actor not in actors]
        absent=all(not self.compare(a)['old_identity_present'] for a in actors)
        p=self.show(self.profile['unit'],deadline)
        group=self.cgroups/scope['control_group'].lstrip('/')
        empty=(not group.exists() or not self.members(group))
        # A new invocation may now own the same cgroup path. It is not an old actor.
        if p['InvocationID']!=scope['invocation_id'] and int(p['MainPID'])>0:
            empty=all(not self.compare(a)['old_identity_present'] for a in actors)
        else:
            empty=empty and p['ActiveState'] in ('inactive','failed') and p['MainPID']=='0' and not p['ControlGroup'] and p['Job'] in ('','0')
        helpers,details=self._helper_settled(scope,context,deadline)
        return absent and empty,helpers,details

    def current_identity(self,deadline):
        # A stopping process may have closed HTTP already. Observe its kernel
        # identity without requiring the endpoint to remain available.
        p=self.show(self.profile['unit'],deadline);pid=int(p['MainPID'])
        if pid==0:return None
        current=self.process(pid)
        if current is None:return None
        _,scope,_=self.scope(p)
        raw,_=self.config_data()
        scope.update(profile_sha256=self.profile_hash,config_sha256=hashlib.sha256(raw).hexdigest())
        return {'pid':pid,'start_ticks':current['start_ticks'],'scope_sha256':digest(scope)}

    def _address_available(self):
        host=self.profile['listen_host'];port=self.profile['listen_port']
        if host=='':
            # Reserve-check both wildcard families simultaneously. V6ONLY here
            # prevents our own temporary IPv6 socket conflicting with our IPv4
            # check; it never changes the native listener or host networking.
            required={ipaddress.ip_address(NativeHTTP(o).host).version for o in self.native_probe_origins}
            sockets=[]
            try:
                for family,address,version in ((socket.AF_INET,'0.0.0.0',4),(socket.AF_INET6,'::',6)):
                    try:
                        sock=socket.socket(family,socket.SOCK_STREAM);sockets.append(sock)
                        if version==6:sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        sock.bind((address,port))
                    except OSError as exc:
                        if version not in required and exc.errno in (errno.EAFNOSUPPORT,errno.EPROTONOSUPPORT,errno.EADDRNOTAVAIL):
                            continue
                        return False
                return True
            finally:
                for sock in sockets:sock.close()
        address=ipaddress.ip_address(host)
        sock=socket.socket(socket.AF_INET6 if address.version==6 else socket.AF_INET,socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind((str(address),port));return True
        except OSError:return False
        finally:sock.close()

    def _attempt_binding(self,identity,context,phase):
        env={}
        for item in file_bytes(self.proc/str(identity['pid'])/'environ',1048576).split(b'\0'):
            if b'=' in item:
                key,value=item.split(b'=',1)
                if key.decode(errors='replace') in TAG_KEYS:env[key.decode()]=value.decode()
        expected=dict(zip(TAG_KEYS,(context['transaction_id'],context['operation_id'],phase)))
        try:
            record=private_json(self._start_record(context,phase))
        except (OSError,ValueError,ExecutorError):return False
        return (env==expected and record['transaction_id']==context['transaction_id']
                and record['operation_id']==context['operation_id'] and record['phase']==phase
                and record['profile_sha256']==self.profile_hash and record['stage'] in ('submitted','started'))

    def _start_record(self,context,phase):
        return self.state/('start-'+digest([context['transaction_id'],context['operation_id'],phase])+'.json')

    def _known_unsubmitted_start(self,context):
        try:
            value=private_json(self._start_record(context,'candidate'))
            return (value['stage']=='aborted_before_command' and value['native_command_submitted'] is False
                    and value['transaction_id']==context['transaction_id'] and value['operation_id']==context['operation_id']
                    and value['profile_sha256']==self.profile_hash and value['config_sha256']==context['candidate_sha256'])
        except (OSError,ValueError,KeyError,ExecutorError):return False

    def _ensure_effect(self,operation,context):
        identifier(context['transaction_id']);identifier(context['operation_id'])
        key=operation+(':'+context['model'] if operation=='stop_model' else '')
        mark=context.get('effects',{}).get(key,{})
        if mark.get('submitted') is not True or mark.get('acknowledged') is not False:
            raise ExecutorError('durable_unacknowledged_effect_required')

    def _stop_source(self,operation,context,deadline):
        expected=context['old_identity'] if operation=='stop_old' else context['new_identity']
        observed=self.inspect_native(context,deadline)
        if observed['identity']!=expected or observed['in_flight']!=0:raise ExecutorError('source_changed_or_busy')
        scope=observed['scope'];self._scope_guard(scope,expected)
        p=self.show(self.profile['unit'],deadline)
        if p['Restart']!='no':raise ExecutorError('source_auto_restart_not_supported')
        actors=list(context.get('observed_actors') or [])
        actors.extend(a for a in observed['actors'] if a not in actors)
        claim={'scope':scope,'identity':expected,'actors':actors,'helper_models':observed['helper_models'],'transaction_id':context['transaction_id'],
               'operation_id':context['operation_id'],'deadline':deadline,'profile_sha256':self.profile_hash}
        # This immutable claim lets late helpers outlive the native process while
        # retaining an independently observable oneshot job result.
        private_create(self.state/('stop-scope-'+digest(scope)+'.json'),claim)
        for name in observed['helper_models']:
            path,unit,model=self._job(scope,name,observed['helper_models'])
            private_create(path,{**claim,'model':name,'unit':unit,'wrapper':{'pid':model['wrapper_pid'],
                'start_ticks':model['wrapper_start_ticks'],'scope_sha256':digest(scope)},'backend':model['backend']})
        private_create(self._claim_path(scope),claim)
        from deploy.maintenance_executor import stop_bound_process
        class Bound:
            def inspect(_,end):return self.inspect_native(context,end)
        return stop_bound_process(expected,Bound(),deadline)

    def original_scope(self,context):
        identity=context['old_identity']
        scope=context.get('observed_scope');actors=context.get('observed_actors')
        if isinstance(scope,dict) and digest(scope)==identity['scope_sha256']:
            return scope,actors
        record=private_json(self.state/('stop-scope-'+identity['scope_sha256']+'.json'))
        if record['identity']!=identity or record['transaction_id']!=context['transaction_id']:
            raise ExecutorError('original_stop_scope_unbound')
        return record['scope'],record['actors']

    def _start(self,operation,context,deadline):
        phase='candidate' if operation=='start_candidate' else 'base'
        path=self._start_record(context,phase)
        record={'transaction_id':context['transaction_id'],'operation_id':context['operation_id'],'phase':phase,
                'config_sha256':context['candidate_sha256'] if phase=='candidate' else context['base_sha256'],
                'profile_sha256':self.profile_hash,'stage':'preparing','native_command_submitted':False,
                'writer':self.process(os.getpid())}
        private_create(path,record)
        try:return self._start_impl(operation,context,deadline,path,record)
        except BaseException:
            current=private_json(path)
            if current['stage']=='preparing':
                private_update(path,current,{'stage':'aborted_before_command','native_command_submitted':False})
            raise

    def _start_impl(self,operation,context,deadline,record_path,record):
        phase='candidate' if operation=='start_candidate' else 'base'
        expected_sha=context['candidate_sha256'] if phase=='candidate' else context['base_sha256']
        old_scope,old_actors=self.original_scope(context)
        old,helpers,_=self._retired(context['old_identity'],old_scope,old_actors,context,deadline)
        if not old or not helpers or not self.unit_exited(self.profile['unit'],deadline) or not self._address_available():
            raise ExecutorError('old_source_not_settled_before_start')
        if phase=='base' and context.get('new_identity'):
            gone,jobs,_=self._retired(context['new_identity'],context['new_scope'],context['new_actors'],context,deadline)
            if not gone or not jobs:raise ExecutorError('attempt_not_settled_before_base_start')
        self.validate(str(self.config),expected_sha,deadline)
        # The pinned source unit must read this exact private EnvironmentFile.
        fragment=file_bytes(self.profile['fragment_path'],65536).decode()
        if ('EnvironmentFile='+str(self.envfile)) not in fragment.splitlines():raise ExecutorError('source_unit_missing_attempt_environment')
        data=''.join(k+'='+v+'\n' for k,v in zip(TAG_KEYS,(context['transaction_id'],context['operation_id'],phase)))
        temporary=self.state/('env-'+digest(record)+'.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.envfile)
        directory=os.open(self.state,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        record=private_update(record_path,record,{'stage':'submitted','native_command_submitted':True})
        self.runner([self.command,'start',self.profile['unit']],deadline)
        while time.monotonic()<deadline:
            try:
                current=self.inspect_native(context,deadline)
                break
            except (OSError,ValueError,ExecutorError):
                time.sleep(min(.05,max(0,deadline-time.monotonic())))
        else:raise ExecutorError('new_instance_observation_deadline')
        if current['identity']==context['old_identity'] or not self._attempt_binding(current['identity'],context,phase):
            raise ExecutorError('new_instance_attempt_binding_unconfirmed')
        private_update(record_path,record,{'stage':'started','identity':current['identity'],'scope':current['scope'],'actors':current['actors']})
        return {'accepted':True,'identity':current['identity'],'observed_at':time.monotonic()}

    def _bound_file_mode(self, context):
        """Validate the immutable core provenance without submitting a native RPC.

        This adapter has one pinned executable and no image installer. Core's
        image allowlist cannot authorize switching that executable between phases.
        inspect_native separately verifies the current process image/listeners.
        """
        if 'native_provenance' not in context:
            return False
        from llmsvc.native_binding import validate_settings
        from llmsvc.reload_witness import CandidateBinding, InstanceIdentity, NativeGenerationReader
        proof = context['native_provenance']
        try:
            if not isinstance(proof, dict) or set(proof) != {'endpoint', 'settings', 'base_generation'}:
                raise ValueError('invalid provenance fields')
            pins = validate_settings(proof['settings'])
            image = self.profile['native_binary_sha256']
            phases = proof['settings'].get('phase_images')
            if (not pins or phases != dict.fromkeys(('old', 'candidate', 'restored'), image)
                    or image not in {pin.executable_sha256 for pin in pins}):
                raise ValueError('single-image adapter cannot switch phase images')
            endpoint = NativeGenerationReader(self.profile['native_origin']).endpoint
            if proof['endpoint'] != endpoint:
                raise ValueError('native endpoint differs from pinned profile')
            old = validate_identity(context['old_identity'])
            CandidateBinding(endpoint=endpoint, generation=proof['base_generation'],
                instance=InstanceIdentity(old['pid'], old['start_ticks']),
                candidate_sha256=context['base_sha256']).to_dict()
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutorError('native_provenance_unbound') from exc
        return True

    def operation(self,operation,context,deadline,dry_run=False):
        if time.monotonic()>=deadline:raise ExecutorError('native_operation_deadline')
        bound_file_mode = self._bound_file_mode(context)
        if operation=='validate':return self.validate(context['candidate_path'],context['candidate_sha256'],deadline,dry_run=dry_run)
        effects={'stop_old','start_candidate','stop_model','stop_candidate','start_base'}
        if operation in effects:
            if dry_run:return {'accepted':False,'dry_run':True,'planned_operation':operation,'observed_at':time.monotonic()}
            self._ensure_effect(operation,context)
            if operation in ('stop_old','stop_candidate'):return self._stop_source(operation,context,deadline)
            if operation in ('start_candidate','start_base'):return self._start(operation,context,deadline)
            model=context['model'];b=next((b for b in context['backend_bindings'] if b['model']==model),None)
            if (b is None or model not in context['removed_models'] or b['unit']!=context['unit']
                    or b['lease_id']!=context['lease_id'] or b['invocation_id']!=context['invocation_id']):raise ExecutorError('cleanup_target_unbound')
            actual=self.backend(model,deadline,b);fd=os.pidfd_open(actual['pid'],0)
            try:
                self.backend(model,deadline,b)
                if time.monotonic()>=deadline:raise ExecutorError('model_stop_deadline')
                signal.pidfd_send_signal(fd,signal.SIGTERM)
            finally:os.close(fd)
            return {'accepted':True,'observed_at':time.monotonic()}
        if operation in ('inspect','preflight'):
            result=self.inspect_native(context,deadline)
            if operation=='preflight':result['ready']=result['in_flight']==0 and result['config_sha256']==context.get('base_sha256') and result['identity']==context.get('old_identity',result['identity'])
            return result
        if operation=='observe_unit':
            b=next((b for b in context['backend_bindings'] if b['model']==context['model']),None)
            if b is None:raise ExecutorError('unit_binding_missing')
            return {'exited':self.unit_exited(b['unit'],deadline),'observed_at':time.monotonic()}
        if operation not in ('observe_old','observe_candidate','observe_candidate_absent','observe_base'):
            raise ExecutorError('unsupported_native_operation')
        old_scope,old_actors=self.original_scope(context)
        old,helpers,detail=self._retired(context['old_identity'],old_scope,old_actors,context,deadline)
        result={'old_identity':context['old_identity'],'old_settled':old,'helpers_settled':helpers,'helper_observations':detail,
                'backends_confirmed':self._backends(context,deadline,cleanup=False),
                'observed_at':time.monotonic()}
        if operation in ('observe_old','observe_candidate_absent'):
            if operation=='observe_candidate_absent':
                expected=context.get('new_identity')
                if expected:
                    gone,jobs,rows=self._retired(expected,context.get('new_scope') or context['observed_scope'],context.get('new_actors') or context['observed_actors'],context,deadline)
                    result.update(attempt_settled=gone and jobs,attempt_identity=expected,helpers_settled=helpers and jobs)
                else:
                    known=self._known_unsubmitted_start(context)
                    result.update(attempt_settled=known,attempt_bound=known,operation_id=context['operation_id'] if known else None)
            result.update(identity=self.current_identity(deadline),
                          ingress_state='excluded' if old and helpers and self._address_available() else 'unknown')
            result['observed_at']=time.monotonic()
            return result
        current=self.inspect_native(context,deadline);phase='candidate' if operation=='observe_candidate' else 'base'
        wanted=context['candidate_sha256'] if phase=='candidate' else context['base_sha256']
        file_ok=(current['config_sha256']==wanted
                 and hashlib.sha256(file_bytes(self.config)).hexdigest()==current['config_sha256'])
        if bound_file_mode:
            # Only the independently observed file bytes. The controller supplies
            # generation visibility via its image/instance/listener-bound reader.
            configuration={'configuration_file_confirmed':file_ok}
        else:
            from llmsvc.reload_witness import NativeGenerationReader
            generation=NativeGenerationReader(self.profile['native_origin']).read(deadline=deadline)
            config_ok=file_ok and generation.error is None
            if phase=='candidate':config_ok=config_ok and generation.generation==context['generation']
            else:
                _,cfg=self.config_data()
                config_ok=config_ok and generation.generation==cfg.get('macros',{}).get('llmsvc_reload_generation')
            config_ok=config_ok and hashlib.sha256(file_bytes(self.config)).hexdigest()==current['config_sha256']
            configuration={'configuration_confirmed':config_ok,'generation':generation.generation}
        bound=self._attempt_binding(current['identity'],context,phase)
        # Candidate adoption precedes removed-model cleanup. Restoring the base
        # cancels that removal intent, but still requires current account integrity;
        # released accounts retain their submitted-stop/positive-exit requirement.
        cleanup=(self._backends(context,deadline,cleanup=True) if phase=='candidate'
                 else result['backends_confirmed'])
        result.update(configuration)
        result.update(identity=current['identity'],config_sha256=current['config_sha256'],cleanup_confirmed=cleanup,
                      ingress_state='open',attempt_bound=bound,operation_id=context['operation_id'] if bound else None,
                      observed_at=time.monotonic())
        if phase=='base':
            attempt=context.get('new_identity')
            if attempt:
                gone,jobs,_=self._retired(attempt,context['new_scope'],context['new_actors'],context,deadline)
                result.update(attempt_identity=attempt,attempt_settled=gone and jobs,helpers_settled=helpers and jobs)
            else:result['attempt_settled']=self._known_unsubmitted_start(context)
        return result


def execute_backend_job(adapter, record):
    """Actual helper work: exact backend sleep confirmation then pidfd wrapper stop."""
    if record.get('profile_sha256')!=adapter.profile_hash:raise ExecutorError('helper_profile_changed')
    wrapper=validate_identity(record['wrapper']);binding=record['backend'];name=record['model']
    deadline=record['deadline']
    if type(deadline) not in (int,float) or not time.monotonic()<deadline<=time.monotonic()+900:
        raise ExecutorError('helper_deadline_expired')
    observed=adapter.process(wrapper['pid'])
    if observed is None or observed['start_ticks']!=wrapper['start_ticks']:
        raise ExecutorError('helper_wrapper_identity_changed')
    fd=os.pidfd_open(wrapper['pid'],0)
    try:
        adapter.backend(name,deadline,binding)
        direct=NativeHTTP(adapter.models[name]['backend_origin'])
        # Keep the full lease. HTTP200 is only an ACK; require the real sleep state.
        with direct.open('POST','/sleep?level=1',deadline) as response:
            if len(response.read(65537))>65536:raise ExecutorError('sleep_response_limit')
        while time.monotonic()<deadline:
            adapter.backend(name,deadline,binding)
            state=direct.json('GET','/is_sleeping',deadline)
            if isinstance(state,dict) and state.get('is_sleeping') is True:break
            time.sleep(min(.05,max(0,deadline-time.monotonic())))
        else:raise ExecutorError('backend_sleep_unconfirmed')
        adapter.backend(name,deadline,binding)
        current=adapter.process(wrapper['pid'])
        if current is not None and current['start_ticks']==wrapper['start_ticks']:
            signal.pidfd_send_signal(fd,signal.SIGTERM)
        if time.monotonic()>=deadline:raise ExecutorError('helper_completed_after_deadline')
    finally:os.close(fd)


def helper_main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['helper','job'])
    parser.add_argument('--profile',required=True)
    parser.add_argument('--model');parser.add_argument('--pid',type=int)
    parser.add_argument('--record');parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    profile=private_json(args.profile);adapter=NativeAdapter(profile,args.profile)
    if args.mode=='job':
        path=Path(args.record)
        if path.parent!=adapter.state:raise ExecutorError('helper_record_outside_owned_state')
        record=private_json(path)
        expected={'LLMSVC_MAINT_TRANSACTION':record['transaction_id'],
                  'LLMSVC_MAINT_OPERATION':record['operation_id'],'LLMSVC_MAINT_SCOPE':digest(record['scope'])}
        if any(os.environ.get(k)!=v for k,v in expected.items()):raise ExecutorError('helper_job_environment_unbound')
        unit=helper_unit(digest(record['scope']),record['model']);p=adapter.show(unit,record['deadline'],job=True)
        if p['MainPID']!=str(os.getpid()) or p['InvocationID']!=os.environ.get('INVOCATION_ID'):
            raise ExecutorError('helper_job_instance_unbound')
        if args.dry_run:return 0
        own=adapter.process(os.getpid())
        private_create(path.with_suffix('.started.json'),{'invocation_id':p['InvocationID'],'pid':own['pid'],
            'start_ticks':own['start_ticks'],'record_sha256':hashlib.sha256(file_bytes(path)).hexdigest()})
        execute_backend_job(adapter,record)
        return 0
    identifier(args.model)
    if args.pid is None or args.pid<=0:raise ExecutorError('helper_wrapper_pid_required')
    invocation=os.environ.get('INVOCATION_ID','')
    if not re.fullmatch('[0-9a-f]{32}',invocation):raise ExecutorError('helper_source_invocation_missing')
    claim_path=adapter.state/('stop-'+invocation+'.json')
    if args.dry_run:
        print(canonical({'dry_run':True,'model':args.model,'effects':False}));return 0
    if not claim_path.exists():
        # Ordinary native unload has the same sleep/stop behavior, with no new
        # maintenance claim or oneshot. Source unit/actor identity is still checked.
        source=adapter.show(profile['unit'],time.monotonic()+2)
        group=(adapter.proc/'self/cgroup').read_text()
        if source['InvocationID']!=invocation or (':'+source['ControlGroup']) not in group:
            raise ExecutorError('ordinary_helper_source_scope_unbound')
        process=adapter.process(args.pid)
        if process is None:raise ExecutorError('ordinary_helper_wrapper_missing')
        binding=adapter.backend(args.model,time.monotonic()+2)
        record={'profile_sha256':adapter.profile_hash,'model':args.model,'backend':binding,
                'wrapper':{'pid':args.pid,'start_ticks':process['start_ticks'],'scope_sha256':'0'*64},
                'deadline':time.monotonic()+min(float(profile.get('helper_timeout_seconds',120)),900)}
        execute_backend_job(adapter,record);return 0
    claim=private_json(claim_path);scope=claim['scope'];adapter._scope_guard(scope,claim['identity'])
    path,unit,model=adapter._job(scope,args.model);record=private_json(path)
    if (model['wrapper_pid']!=args.pid or record['transaction_id']!=claim['transaction_id']
            or record['operation_id']!=claim['operation_id'] or record['scope']!=scope):
        raise ExecutorError('native_helper_claim_mismatch')
    p=adapter.show(unit,claim['deadline'])
    if p['LoadState']!='not-found':
        marker=path.with_suffix('.duplicate')
        if not marker.exists():private_create(marker,{'reason':'duplicate_native_helper_submission'})
        raise ExecutorError('native_helper_job_already_exists')
    remaining=claim['deadline']-time.monotonic()
    if not 0<remaining<=900:raise ExecutorError('helper_deadline_expired')
    systemd_run=profile.get('systemd_run','/usr/bin/systemd-run')
    if not Path(systemd_run).is_absolute():raise ExecutorError('absolute_systemd_run_required')
    argv=[systemd_run,'--quiet','--wait','--unit='+unit,'--property=Type=oneshot',
          '--property=RemainAfterExit=yes','--property=Restart=no',
          '--property=TimeoutStartSec='+str(remaining),'--property=KillMode=control-group',
          '--setenv=LLMSVC_MAINT_TRANSACTION='+claim['transaction_id'],
          '--setenv=LLMSVC_MAINT_OPERATION='+claim['operation_id'],
          '--setenv=LLMSVC_MAINT_SCOPE='+digest(scope),'--',adapter.python,'-B',str(adapter.program),
          'job','--profile',str(adapter.profile_path),'--record',str(path)]
    adapter.runner(argv,claim['deadline'])
    return 0


if __name__=='__main__':
    try:raise SystemExit(helper_main())
    except (ExecutorError,OSError,ValueError,KeyError) as exc:
        print(canonical({'error':str(exc) if isinstance(exc,ExecutorError) else 'native_helper_unavailable'}),file=sys.stderr)
        raise SystemExit(1)
