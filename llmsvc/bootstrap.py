# Generated-By: Codex / gpt-6-astra
"""Explicit first-default bootstrap using the existing real lease/launcher path."""
import copy
import hashlib
import hmac
import ipaddress
import inspect
import json
import math
import os
import secrets
import sys
import time
import types
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from llmsvc import bootstrap_state
from llmsvc.maintenance import CommandBackend, identity
from llmsvc.reload import _read_regular_file
from llmsvc.scheduler import IntentWriteError

HEADER = 'X-LLMSVC-Bootstrap'


class BootstrapError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def actor():
    raw=Path('/proc/self/stat').read_text()
    return {'pid':os.getpid(),'start_ticks':raw[raw.rfind(')')+2:].split()[19]}


def validate_settings(settings):
    if not isinstance(settings,dict):raise ValueError('bootstrap must be a mapping')
    if not settings:return
    required={'model','util','command','launcher_path','launcher_sha256','launcher_config_path',
              'launcher_config_sha256','migration_command','manifest_sha256','base_config_sha256',
              'target_config_sha256','timeout_seconds'}
    if set(settings)!=required:raise ValueError('invalid bootstrap settings')
    if not isinstance(settings['model'],str) or not settings['model']:raise ValueError('bootstrap model missing')
    for key in ('launcher_sha256','launcher_config_sha256','manifest_sha256','base_config_sha256','target_config_sha256'):
        value=settings[key]
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('bootstrap digest missing or invalid')
    if type(settings['util']) not in (int,float) or not 0<settings['util']<=1:raise ValueError('invalid bootstrap util')
    for key in ('command','migration_command'):
        value=settings[key]
        if (not isinstance(value,list) or not 1<=len(value)<=256 or any(not isinstance(v,str) or not v or '\x00' in v for v in value)
                or not Path(value[0]).is_absolute()):raise ValueError('bootstrap requires explicit absolute command argv')
    for key in ('launcher_path','launcher_config_path'):
        if not isinstance(settings[key],str) or not Path(settings[key]).is_absolute():raise ValueError('bootstrap input path must be absolute')
    value=settings['timeout_seconds']
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<=900:raise ValueError('bootstrap timeout must be in (0,900]')


class BootstrapController:
    def __init__(self,scheduler,*,backend=None,clock=time.monotonic):
        self.scheduler=scheduler;self.clock=clock;self.busy=False;self.token=None;self.deadline=0
        self.pending_id=None
        self.spec=copy.deepcopy(scheduler.config.bootstrap)
        validate_settings(self.spec)
        if not self.spec:raise ValueError('bootstrap settings are absent')
        self.model=self.spec['model'];self.unit='vllm-'+self.model+'.service'
        self.backend=backend or CommandBackend(self.spec['migration_command'],monotonic=clock)
        from llmsvc.reload_witness import NativeGenerationReader
        self.source_origin=NativeGenerationReader(scheduler.config.collectors.get('swap_url','')).endpoint[:-len('/api/mcp')]
        address=ipaddress.ip_address(urlsplit(self.source_origin).hostname)
        if address.is_unspecified or address.is_multicast:raise ValueError('bootstrap source needs a concrete HTTP destination')
        self.profile_hash=self._profile_hash()
        previous=getattr(scheduler,'bootstrap',None)
        if previous is not None and previous.busy:raise BootstrapError('bootstrap owner is active')
        scheduler.bootstrap=self

    def _profile_hash(self):
        cfg=self.scheduler.config
        return digest({'bootstrap':cfg.bootstrap,'collectors':cfg.collectors,
                       'registry':cfg.registry,'listen':[cfg.listen_host,cfg.listen_port],
                       'placement_limits':[cfg.memory_budget_gb,cfg.host_min_available_gb,
                                           cfg.max_snapshot_age_seconds,cfg.lease_timeout_seconds,cfg.placement_wait_seconds]})

    def _enabled(self):
        s=self.scheduler
        if (s.bootstrap is not self or not s.config.bootstrap_enabled or s.config.read_only
                or not s.config.placement_enabled or not s.config.catalog_enabled
                or not s.config.model_actions_enabled or s.config.catalog_mode!='maintenance'
                or s.store is None or s.store.read_only or s.placement is None or s.stopping.is_set()):
            raise BootstrapError('bootstrap is disabled or stopping')
        if self._profile_hash()!=self.profile_hash:raise BootstrapError('bootstrap configuration changed')
        metadata=s.config.collectors.get('models',{}).get(self.model,{})
        if metadata.get('is_default') is not True or metadata.get('unit',self.unit)!=self.unit:
            raise BootstrapError('bootstrap target must remain the configured default')
        weights=metadata.get('weights_gb')
        if type(weights) not in (int,float) or not math.isfinite(weights) or weights<0:
            raise BootstrapError('bootstrap default weights are unknown')
        if type(metadata.get('port')) is not int or not 1<=metadata['port']<=65535:
            raise BootstrapError('bootstrap default port is unknown')

    def _record(self):
        record=self.scheduler.store.bootstrap_checkpoint()
        if record is None or record['profile_sha256']!=self.profile_hash:raise BootstrapError('bootstrap claim missing or changed')
        return record

    def _save(self,record,**changes):
        s=self.scheduler
        with s.action_lock:
            stage=changes.get('stage',record['stage'])
            if stage in ('complete','aborted') and record['stage']!=stage:
                self._enabled()
                if not self.busy or self._record()!=record:
                    raise BootstrapError('bootstrap terminal owner or checkpoint changed')
                if stage=='complete':
                    # This probe can sample/wait. Everything that can invalidate
                    # completion must be checked again after it, under this lock.
                    observed=self._confirmed(record)
                    row=s.store.lease(record['lease_id'])
                    proof=changes.get('migration',record['migration']).get('activated',{})
                    binding=proof.get('default_binding',{})
                    if (row is None or binding.get('lease_id')!=record['lease_id']
                            or binding.get('model')!=self.model or binding.get('unit')!=self.unit
                            or binding.get('gpu')!=row[0].gpu
                            or binding.get('invocation_id')!=observed.invocation_id):
                        raise BootstrapError('bootstrap terminal default binding changed')
                expected=self.spec['target_config_sha256' if stage=='complete' else 'base_config_sha256']
                if self._source_state()!=(expected,True):
                    raise BootstrapError('bootstrap terminal source changed')
                # No external callback/probe follows these final checks. Store
                # validation and its compare-and-save remain inside action_lock.
                self._enabled()
                if not self.busy or self._record()!=record or self.clock()>=self.deadline:
                    raise BootstrapError('bootstrap terminal checkpoint changed or deadline exceeded')
            value=bootstrap_state.save(s.store,record,{**record,**changes})
        s.emit('bootstrap_stage',model=self.model,detail={'stage':value['stage'],'lease_id':value['lease_id']})
        return value

    def _source_state(self):
        import yaml
        path=Path(self.scheduler.config.registry['config_path'])
        raw,_=_read_regular_file(path,self.scheduler.config.registry.get('config_max_bytes',1048576))
        try:
            document=yaml.safe_load(raw)
            startup=document.get('hooks',{}).get('on_startup',{})
            preserved=startup.get('preload')==[self.model] and not startup.get('profile')
        except (AttributeError,TypeError,yaml.YAMLError) as exc:
            raise BootstrapError('bootstrap source preload is unreadable') from exc
        return hashlib.sha256(raw).hexdigest(),preserved

    def _source_hash(self):
        return self._source_state()[0]

    def _context(self,record=None):
        account=None
        if record is not None and record['lease_id'] is not None:
            row=self.scheduler.store.lease(record['lease_id'])
            if row is not None and row[0].status!='released':
                account={**asdict(row[0]),'unit':row[1]}
                observed=record['migration'].get('default_observed')
                if observed is not None:account['invocation_id']=observed['invocation_id']
        transaction=record['id'] if record else self.pending_id
        return {'transaction_id':transaction,'bootstrap_id':transaction,'manifest_sha256':self.spec['manifest_sha256'],
                'default_model':self.model,'default_unit':self.unit,'source_origin':self.source_origin,'launcher_sha256':self.spec['launcher_sha256'],
                'launcher_config_sha256':self.spec['launcher_config_sha256'],
                'base_config_sha256':self.spec['base_config_sha256'],'target_config_sha256':self.spec['target_config_sha256'],
                'source_identity':record['migration']['preflight']['identity'] if record else None,
                'effects':{'bootstrap_'+key:{'submitted':True,'acknowledged':value=='acknowledged'}
                           for key,value in (record['effects'] if record else {}).items() if key!='launch'},
                'account':account,'launch_submitted':record is not None and 'launch' in record['effects']}

    def _request(self,operation,record=None):
        self._enabled()
        started=self.clock()
        if started>=self.deadline:raise BootstrapError('bootstrap deadline exceeded')
        result=self.backend.request(operation,self._context(record),deadline=self.deadline)
        self._enabled()
        ended=self.clock()
        if (not isinstance(result,dict) or result.get('transaction_id')!=(record['id'] if record else self.pending_id)
                or result.get('manifest_sha256')!=self.spec['manifest_sha256']
                or result.get('default_model')!=self.model or result.get('default_unit')!=self.unit
                or result.get('source_origin')!=self.source_origin or ended>=self.deadline
                or type(result.get('observed_at')) not in (int,float) or not started<=result['observed_at']<=ended):
            raise BootstrapError('bootstrap observation is unbound or late')
        return result

    def _stage_proof(self,result):
        for path_key,hash_key,limit in (('launcher_path','launcher_sha256',262144),
                                        ('launcher_config_path','launcher_config_sha256',65536)):
            raw,_=_read_regular_file(Path(self.spec[path_key]),limit)
            if hashlib.sha256(raw).hexdigest()!=self.spec[hash_key]:
                raise BootstrapError('staged bootstrap launcher inputs changed')
        source_hash,preload_preserved=self._source_state()
        return (result.get('staged') is True and result.get('source_absent') is True
                and result.get('helpers_settled') is True and preload_preserved
                and result.get('launcher_sha256')==self.spec['launcher_sha256']
                and result.get('launcher_config_sha256')==self.spec['launcher_config_sha256']
                and result.get('source_config_sha256')==self.spec['target_config_sha256']
                and source_hash==self.spec['target_config_sha256'])

    def policy_snapshot(self,snapshot):
        """Preserve raw unknowns; scoped cold bootstrap may exclude expected source-down errors."""
        s=self.scheduler
        if not s.store.bootstrap_authorized():return snapshot
        record=self._record()
        snapshot=replace(snapshot,errors=tuple(e for e in snapshot.errors if e!='bootstrap_reconciliation_required'))
        errors=set(snapshot.errors)
        expected={'running: URLError','events: URLError','running: ConnectionRefusedError','events: ConnectionRefusedError'}
        if not errors & expected:return snapshot
        if record['stage'] not in ('placing','placed','start_submitted','start_acknowledged','health_observed','confirm_submitted','default_confirmed'):
            return snapshot
        proof=self._request('bootstrap_observe',record)
        if not self._stage_proof(proof):raise BootstrapError('bootstrap source exclusion is unconfirmed')
        return replace(snapshot,errors=tuple(e for e in snapshot.errors if e not in expected))

    @contextmanager
    def http_scope(self,operation,payload,token,source_ip):
        s=self.scheduler;self._enabled()
        with s.action_lock:
            if s.stopping.is_set():
                raise IntentWriteError(503,'scheduler_stopping')
            if not self.busy or self.clock()>=self.deadline:
                raise IntentWriteError(503,'bootstrap_reconciliation_required')
            record=self._record()
            try:
                peer=ipaddress.ip_address(source_ip);configured=ipaddress.ip_address(s.config.listen_host)
                peer=getattr(peer,'ipv4_mapped',None) or peer
                configured=getattr(configured,'ipv4_mapped',None) or configured
                local=peer.is_loopback or peer==configured
            except ValueError:local=False
            valid=(local and isinstance(token,str) and len(token)==64 and
                   hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),record['token_sha256']))
            if operation=='place':
                valid=valid and record['stage']=='placing' and payload=={'model':self.model,'util':self.spec['util']}
            elif operation in ('confirm','release'):
                valid=valid and record['lease_id'] is not None and payload=={'lease_id':record['lease_id']}
            else:valid=False
            if not valid:raise IntentWriteError(503,'bootstrap_reconciliation_required')
        with s.store.bootstrap_scope(record['id']):yield

    def _launcher(self):
        raw,_=_read_regular_file(Path(self.spec['launcher_path']),262144)
        settings,_=_read_regular_file(Path(self.spec['launcher_config_path']),65536)
        if (hashlib.sha256(raw).hexdigest()!=self.spec['launcher_sha256']
                or hashlib.sha256(settings).hexdigest()!=self.spec['launcher_config_sha256']):
            raise BootstrapError('bootstrap launcher inputs changed')
        config=json.loads(settings)
        host=self.scheduler.config.listen_host
        authority='['+host+']' if ':' in host else host
        if config.get('scheduler_url','').rstrip('/')!='http://'+authority+':'+str(self.scheduler.config.listen_port):
            raise BootstrapError('bootstrap launcher targets a different authority')
        name='_llmsvc_bootstrap_launcher_'+uuid.uuid4().hex
        module=types.ModuleType(name);module.__file__=self.spec['launcher_path'];sys.modules[name]=module
        try:exec(compile(raw,self.spec['launcher_path'],'exec'),module.__dict__)
        except BaseException:
            sys.modules.pop(name,None);raise
        try:
            required_hooks={'progress','deadline','headers','config','placement'}
            if not required_hooks<=set(inspect.signature(module.main).parameters):
                raise BootstrapError('bootstrap launcher lacks durable integration hooks')
            configured=module.deep_merge(module.DEFAULT_CONFIG,config)
            module.validate_config(configured)
            port=module.extract_port(self.spec['command'])
            metadata=self.scheduler.config.collectors['models'][self.model]
            if (port is None or int(port)!=metadata.get('port')
                    or configured['unit_suffix']!='.service'
                    or configured['place_path']!='/v1/place'
                    or configured['confirm_path_template']!='/v1/place/{lease_id}/confirm'
                    or configured['release_path_template']!='/v1/place/{lease_id}/release'
                    or configured['health_url_template'].format(port=port)!=metadata['daemon_url'].rstrip('/')+'/health'):
                sys.modules.pop(name,None)
                raise BootstrapError('bootstrap launcher protocol or model port differs')
            util_flags=[]
            for index,arg in enumerate(self.spec['command']):
                if arg=='--gpu-memory-utilization':
                    if index+1>=len(self.spec['command']):raise BootstrapError('bootstrap util argument missing')
                    util_flags.append(self.spec['command'][index+1])
                elif arg.startswith('--gpu-memory-utilization='):util_flags.append(arg.split('=',1)[1])
            if len(util_flags)!=1 or float(util_flags[0])!=self.spec['util']:
                sys.modules.pop(name,None)
                raise BootstrapError('bootstrap command util is not bound to its lease')
            module._bootstrap_config=configured  # Frozen checked bytes, never a later path reread.
        except BaseException:
            sys.modules.pop(name,None)
            raise
        return module

    def _progress(self,stage,fields):
        s=self.scheduler
        if stage=='start_submitted':s.sample_once()
        with s.action_lock:
            self._enabled();record=self._record()
            if self.clock()>=self.deadline:raise BootstrapError('bootstrap deadline exceeded')
            if stage=='placed':
                if record['stage']!='placed' or fields.get('lease_id')!=record['lease_id']:
                    raise BootstrapError('launcher lease was not atomically attached')
                row=s.store.lease(record['lease_id'])
                if (row is None or row[0].model!=self.model or row[1]!=self.unit or row[0].util!=self.spec['util']
                        or str(row[0].gpu)!=str(fields.get('gpu'))):raise BootstrapError('bootstrap lease changed')
                return
            mapped='default_confirmed' if stage=='confirmed' else stage
            if stage not in ('placing','start_submitted','start_acknowledged','health_observed','confirm_submitted','confirmed'):
                raise BootstrapError('unknown bootstrap launcher stage')
            if stage!='placing' and fields.get('lease_id')!=record['lease_id']:
                raise BootstrapError('launcher transition refers to another lease')
            effects=dict(record['effects'])
            if stage=='start_submitted':
                self._start_admission(record)
                effects['launch']='submitted'
            if stage=='start_acknowledged':effects['launch']='acknowledged'
            migration=record['migration']
            if stage=='confirmed':
                observed=self._confirmed(record)
                migration={**migration,'default_observed':asdict(observed)}
            self._save(record,stage=mapped,effects=effects,migration=migration)
        if stage=='placing':
            s.sample_once()  # Publish fresh physical observations before the HTTP lease request.

    def _start_admission(self,record):
        from llmsvc.policy import PolicySettings
        s=self.scheduler;row=s.store.lease(record['lease_id'])
        if row is None or row[0].status not in ('pending','stale') or row[0].expires_at<=s.clock():
            raise BootstrapError('bootstrap prepared lease is not current')
        lease,unit=row
        with s.store.bootstrap_scope(record['id']):
            snapshot=self.policy_snapshot(s.snapshot())
            observed=s.placement._inspect(self.model,self.deadline)
            if not s.placement._fresh(snapshot) or observed.exists is not False:
                raise BootstrapError('bootstrap start lacks fresh unit absence')
        if unit!=self.unit or lease.gpu!=PolicySettings().exclusive_gpu:
            raise BootstrapError('bootstrap default placement binding changed')
        gpu=next((value for value in snapshot.gpus if value.index==lease.gpu),None)
        available=snapshot.memory.host_available_gb
        weights=s.config.collectors['models'][self.model].get('weights_gb')
        values=(available,weights,gpu.free_gb if gpu else None)
        if any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in values):
            raise BootstrapError('bootstrap start memory is unknown')
        if gpu.free_gb<lease.budget_gb or available-weights<s.config.host_min_available_gb:
            raise BootstrapError('bootstrap start memory changed')

    def _confirmed(self,record):
        s=self.scheduler;row=s.store.lease(record['lease_id']) if record['lease_id'] else None
        if row is None or row[0].status!='confirmed' or row[0].model!=self.model or row[1]!=self.unit:
            raise BootstrapError('default lacks a confirmed real lease')
        with s.store.bootstrap_scope(record['id']):
            s.sample_once()
            observation=s.placement._inspect(self.model,self.deadline)
            if (not s.placement._healthy(row[0],row[1],observation) or not observation.invocation_id
                    or record['migration'].get('default_observed',{}).get('invocation_id',observation.invocation_id)!=observation.invocation_id):
                raise BootstrapError('default unit health or lease identity is unconfirmed')
        return observation

    def run(self,*,dry_run=False):
        if dry_run:return {'would':[{'kind':'bootstrap_default','model':self.model}],'dry_run':True}
        s=self.scheduler
        with s.action_lock:
            self._enabled()
            if self.busy or s.store.bootstrap_checkpoint() is not None:
                raise BootstrapError('bootstrap already exists; observation recovery required')
            if (s.model_actions is not None and (s.model_actions.pending or s.model_actions.free_active)
                    or s.catalog is not None and s.catalog.busy):
                raise BootstrapError('another lifecycle owner is active')
            self.busy=True;self.deadline=self.clock()+self.spec['timeout_seconds'];self.pending_id=uuid.uuid4().hex
        module=None
        try:
            module=self._launcher()
            s.await_initial_sample(self.deadline)
            with s.action_lock:
                self._enabled()
                if self.clock() >= self.deadline:
                    raise BootstrapError('bootstrap deadline exceeded')
                request=s.placement._request({'model':self.model,'util':self.spec['util']})
                decision,blocked=s.placement._decision(s.snapshot(),request,waiting=False)
                if decision is None or not decision.actions or any(action.kind!='place' for action in decision.actions):
                    raise BootstrapError('bootstrap initial placement is not admissible')
            before,preserved=self._source_state();proof=self._request('bootstrap_preflight')
            if (before!=self.spec['base_config_sha256'] or not preserved or self._source_state()!=(before,True)
                    or proof.get('source_config_sha256')!=before or proof.get('legacy_backends_absent') is not True or proof.get('ready') is not True
                    or proof.get('default_preload_preserved') is not True
                    or type(proof.get('in_flight')) is not int or proof['in_flight']!=0):
                raise BootstrapError('bootstrap preflight is unknown or busy')
            identity(proof.get('identity'))
            self.token=secrets.token_hex(32)
            record={'id':self.pending_id,'profile_sha256':self.profile_hash,'model':self.model,'unit':self.unit,
                    'util':self.spec['util'],'actor':actor(),'token_sha256':hashlib.sha256(self.token.encode()).hexdigest(),
                    'stage':'claimed','lease_id':None,'migration':{'preflight':proof},'effects':{},'error':None,'catalog_present':False}
            bootstrap_state.claim(s.store,record)
            record=self._save(record,stage='stage_submitted',effects={'stage':'submitted'})
            staged=self._request('bootstrap_stage',record)
            if staged.get('accepted') is not True or staged.get('legacy_backends_absent') is not True or not self._stage_proof(staged):
                raise BootstrapError('bootstrap stage is not confirmed')
            record=self._save(record,stage='staged',effects={'stage':'acknowledged'},migration={**record['migration'],'staged':staged})
            args=[str(self.spec['util']),self.unit,'--config',self.spec['launcher_config_path'],'--',*self.spec['command']]
            result=module.main(args,progress=self._progress,deadline=self.deadline,headers={HEADER:self.token},
                               config=module._bootstrap_config)
            record=self._record()
            if result!=0 or record['stage']!='default_confirmed':raise BootstrapError('bootstrap launcher did not confirm default')
            return self._activate(record)
        except Exception as exc:
            try:
                current=s.store.bootstrap_checkpoint()
                if current is not None:self._save(current,error=type(exc).__name__)
            except Exception:pass  # The last durable fence remains; never clear on logging failure.
            raise
        finally:
            if module is not None:sys.modules.pop(module.__name__,None)
            self.token=None;self.busy=False


    def _activate(self,record):
        self._confirmed(record)
        record=self._save(record,stage='activate_submitted',effects={**record['effects'],'activate':'submitted'})
        started=self._request('bootstrap_activate',record)
        if started.get('accepted') is not True:raise BootstrapError('bootstrap activation acknowledgement unavailable')
        record=self._save(record,effects={**record['effects'],'activate':'acknowledged'})
        return self._finish_source(record)

    def _finish_source(self,record):
        proof=self._request('bootstrap_observe',record)
        current=identity(proof.get('identity'));old=record['migration']['preflight']['identity']
        if ((current['pid'],current['start_ticks'])==(old['pid'],old['start_ticks']) or proof.get('active_ready') is not True
                or proof.get('default_confirmed') is not True
                or proof.get('source_config_sha256')!=self.spec['target_config_sha256']
                or self._source_state()!=(self.spec['target_config_sha256'],True)):
            raise BootstrapError('bootstrap source/default handoff is unconfirmed')
        observed=self._confirmed(record)
        binding=proof.get('default_binding');lease=self.scheduler.store.lease(record['lease_id'])[0]
        if (not isinstance(binding,dict) or binding.get('model')!=self.model or binding.get('unit')!=self.unit
                or binding.get('lease_id')!=record['lease_id'] or binding.get('invocation_id')!=observed.invocation_id
                or binding.get('gpu')!=lease.gpu or type(binding.get('pid')) is not int or binding['pid']<=0
                or not isinstance(binding.get('start_ticks'),str) or not binding['start_ticks'].isdigit()):
            raise BootstrapError('native default binding differs from confirmed account')
        self._save(record,stage='complete',
                   migration={**record['migration'],'activated':proof})
        return {'stage':'complete','model':self.model,'lease_id':record['lease_id']}

    def recover(self,mode='observe',*,dry_run=False):
        if mode not in ('observe','resume','rollback'):raise ValueError('invalid bootstrap recovery mode')
        if dry_run:return {'would':[{'kind':'bootstrap_'+mode,'model':self.model}],'dry_run':True}
        s=self.scheduler;module=None
        with s.action_lock:
            self._enabled();record=self._record()
            if self.busy:raise BootstrapError('bootstrap owner is active')
            previous=record['actor'];current=actor()
            if previous!=current:
                try:
                    raw=Path('/proc/'+str(previous['pid'])+'/stat').read_text()
                    active=raw[raw.rfind(')')+2:].split()[19]==previous['start_ticks']
                except FileNotFoundError:active=False
                except (OSError,IndexError):raise BootstrapError('previous bootstrap actor is unknown')
                if active:raise BootstrapError('previous bootstrap actor remains active')
            if record['stage'] in ('complete','aborted'):
                return {'stage':record['stage'],'model':self.model,'lease_id':record['lease_id']}
            self.token=secrets.token_hex(32)
            record=bootstrap_state.reclaim(s.store,record,current,hashlib.sha256(self.token.encode()).hexdigest())
            self.busy=True;self.deadline=self.clock()+self.spec['timeout_seconds']
        try:
            if mode=='rollback':
                if 'launch' in record['effects']:raise BootstrapError('bootstrap rollback cannot stop or repeat a submitted default launch')
                if record['lease_id'] is not None:
                    with s.store.bootstrap_scope(record['id']):
                        s.placement.finish('release',record['lease_id'])
                if 'rollback' in record['effects']:raise BootstrapError('unknown bootstrap rollback requires source settlement')
                record=self._save(record,effects={**record['effects'],'rollback':'submitted'})
                proof=self._request('bootstrap_rollback',record)
                old=record['migration']['preflight']['identity']
                absent=proof.get('source_absent') is True and proof.get('helpers_settled') is True
                retained=False
                if proof.get('original_source_retained') is True:
                    current=identity(proof.get('identity'))
                    retained=((current['pid'],current['start_ticks'])==(old['pid'],old['start_ticks'])
                              and type(proof.get('in_flight')) is int and proof['in_flight']==0)
                if (proof.get('rolled_back') is not True or not (absent or retained)
                        or proof.get('legacy_backends_absent') is not True
                        or self._source_state()!=(self.spec['base_config_sha256'],True)):
                    raise BootstrapError('bootstrap file rollback remains unconfirmed')
                self._save(record,stage='aborted',effects={**record['effects'],'rollback':'acknowledged'},
                           migration={**record['migration'],'rolled_back':proof})
                return {'stage':'aborted','model':self.model}
            if 'activate' in record['effects']:
                return self._finish_source(record)  # Observe only; never resend unknown start.
            proof=self._request('bootstrap_observe',record)
            if not self._stage_proof(proof):raise BootstrapError('staged migration is not presently confirmed')
            if record['stage']=='stage_submitted':
                record=self._save(record,stage='staged',migration={**record['migration'],'staged':proof})
            if 'launch' not in record['effects']:
                if mode!='resume':return {'stage':record['stage'],'model':self.model,'needs_resume':True}
                module=self._launcher();prepared=None
                if record['lease_id'] is not None:
                    row=s.store.lease(record['lease_id'])
                    if row is None or row[0].status not in ('pending','stale') or row[1]!=self.unit:
                        raise BootstrapError('bootstrap prepared allocation changed')
                    prepared=module.Placement(str(row[0].gpu),row[0].lease_id)
                args=[str(self.spec['util']),self.unit,'--config',self.spec['launcher_config_path'],'--',*self.spec['command']]
                result=module.main(args,progress=self._progress,deadline=self.deadline,headers={HEADER:self.token},
                                   config=module._bootstrap_config,placement=prepared)
                record=self._record()
                if result!=0 or record['stage']!='default_confirmed':raise BootstrapError('bootstrap resumed launch is not confirmed')
            elif record['stage']!='default_confirmed':
                with s.store.bootstrap_scope(record['id']):
                    s.sample_once();row=s.store.lease(record['lease_id'])
                    observed=s.placement._inspect(self.model,self.deadline)
                    if (row is None or not s.placement._healthy(row[0],row[1],observed) or not observed.invocation_id
                            or record['migration'].get('default_observed',{}).get('invocation_id',observed.invocation_id)!=observed.invocation_id):
                        raise BootstrapError('submitted bootstrap launch remains unconfirmed')
                    if 'default_observed' not in record['migration']:
                        record=self._save(record,migration={**record['migration'],'default_observed':asdict(observed)})
                    if record['stage'] in ('start_submitted','start_acknowledged'):
                        record=self._save(record,stage='health_observed')
                    if record['stage']=='health_observed':record=self._save(record,stage='confirm_submitted')
                    s.placement.finish('confirm',record['lease_id'])
                    record=self._save(record,stage='default_confirmed')
            if mode!='resume':return {'stage':record['stage'],'model':self.model,'needs_resume':True}
            return self._activate(record)
        finally:
            if module is not None:sys.modules.pop(module.__name__,None)
            self.token=None;self.busy=False
