# Generated-By: Codex / gpt-6-astra
"""Deterministic systemd observation fixtures; no real service changes here."""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from deploy.maintenance_executor import ExecutorError, digest
from deploy.maintenance_native import NativeAdapter, helper_unit, private_create


@pytest.fixture
def native(tmp_path):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    fragment=tmp_path/'native.service';fragment.write_text('EnvironmentFile='+str(state/'launch.env')+'\n')
    config=tmp_path/'config.yaml';config.write_text('models: {}\n')
    import deploy.maintenance_native as module
    profile={'unit':'native.service','fragment_path':str(fragment),'fragment_sha256':hashlib.sha256(fragment.read_bytes()).hexdigest(),
             'native_config_path':str(config),'native_config_dir':str(tmp_path),'native_origin':'http://127.0.0.1:54321',
             'state_dir':str(state),'launch_environment_file':str(state/'launch.env'),
             'helper_python':sys.executable,'helper_program_sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
             'native_binary':sys.executable,'native_binary_sha256':'0'*64,'listen_host':'127.0.0.1','listen_port':54321,
             'models':{'m':{'unit':'vllm-m.service','backend_origin':'http://127.0.0.1:54322','process_argv':['/fixture/wrapper']}}}
    path=tmp_path/'profile.json';private_create(path,profile)
    adapter=NativeAdapter(profile,path)
    scope={'boot_id':(Path('/proc/sys/kernel/random/boot_id')).read_text().strip(),'unit':'native.service','invocation_id':'a'*32,
           'control_group':'/fixture/native','fragment_sha256':profile['fragment_sha256'],'profile_sha256':adapter.profile_hash,
           'config_sha256':'1'*64}
    adapter._fixture_helper_models={'m':{'wrapper_pid':31,'wrapper_start_ticks':'12','backend':{}}}
    context={'transaction_id':'tx','operation_id':'op'}
    return adapter,scope,context


def helper_fixture(adapter,scope,context):
    private_create(adapter.state/('stop-scope-'+digest(scope)+'.json'),{**context,'scope':scope,'helper_models':adapter._fixture_helper_models})
    path,unit,_=adapter._job(scope,'m')
    record={**context,'scope':scope,'model':'m'};private_create(path,record)
    private_create(path.with_suffix('.started.json'),{'invocation_id':'b'*32,'record_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    argv=[adapter.python,'-B',str(adapter.program),'job','--profile',str(adapter.profile_path),'--record',str(path)]
    properties={'Id':unit,'LoadState':'loaded','ActiveState':'active','SubState':'exited','Result':'success','ExecMainCode':'1',
                'ExecMainStatus':'0','MainPID':'0','RemainAfterExit':'yes','Transient':'yes','Job':'','ControlGroup':'',
                'ExecMainStartTimestampMonotonic':'100','ExecMainExitTimestampMonotonic':'200','InvocationID':'b'*32,
                'DropInPaths':'','ExecStart':'{ path='+adapter.python+' ; argv[]='+' '.join(argv)+' ; ignore_errors=no ; }',
                'Environment':'LLMSVC_MAINT_TRANSACTION=tx LLMSVC_MAINT_OPERATION=op LLMSVC_MAINT_SCOPE='+digest(scope)}
    adapter.show=lambda *a,**k:properties
    return path,properties


def test_helper_positive_evidence_uses_actual_result_and_bound_invocation(native):
    adapter,scope,context=native;path,p=helper_fixture(*native)
    assert adapter._helper_settled(scope,context,time.monotonic()+1)[0]
    p['InvocationID']='c'*32
    assert not adapter._helper_settled(scope,context,time.monotonic()+1)[0]


@pytest.mark.parametrize('changes',[{'ActiveState':'activating','SubState':'start'},
    {'Result':'exit-code','ExecMainStatus':'7'},{'ExecMainCode':'0'},
    {'MainPID':'123'},{'Job':'active-job'},{'ExecMainStartTimestampMonotonic':'0'},
    {'ExecStart':'{ path=/foreign ; argv[]=/foreign ; }'},{'Environment':'LLMSVC_MAINT_TRANSACTION=other'},
    {'Transient':'no'},{'DropInPaths':'/foreign.conf'}])
def test_failed_delayed_or_unbound_helper_is_never_settled(native,changes):
    adapter,scope,context=native;_,p=helper_fixture(*native);p.update(changes)
    assert not adapter._helper_settled(scope,context,time.monotonic()+1)[0]


def test_duplicate_and_missing_started_receipt_block(native):
    adapter,scope,context=native;path,_=helper_fixture(*native)
    path.with_suffix('.started.json').unlink()
    assert not adapter._helper_settled(scope,context,time.monotonic()+1)[0]
    private_create(path.with_suffix('.started.json'),{'invocation_id':'b'*32,'record_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    private_create(path.with_suffix('.duplicate'),{'duplicate':True})
    assert not adapter._helper_settled(scope,context,time.monotonic()+1)[0]


def test_effect_requires_distinct_core_submission_and_dryrun_never_dispatches(native):
    adapter,_,context=native
    for operation in ['stop_old','start_candidate','stop_model','stop_candidate','start_base']:
        result=adapter.operation(operation,context,time.monotonic()+1,dry_run=True)
        assert result['dry_run'] and not result['accepted']
    with pytest.raises(ExecutorError,match='durable_unacknowledged'):
        adapter._ensure_effect('stop_old',context)
    context['effects']={'stop_old':{'submitted':True,'acknowledged':False}}
    adapter._ensure_effect('stop_old',context)
    context['effects']['stop_old']['acknowledged']=True
    with pytest.raises(ExecutorError):adapter._ensure_effect('stop_old',context)


def test_candidate_scope_substitution_cannot_erase_original_stop_binding(native):
    adapter,scope,context=native;old={'pid':42,'start_ticks':'13','scope_sha256':digest(scope)}
    private_create(adapter.state/('stop-scope-'+digest(scope)+'.json'),{**context,'identity':old,'scope':scope,'actors':[old]})
    supplied={**context,'old_identity':old,'observed_scope':{'candidate':'scope'},'observed_actors':[]}
    restored,actors=adapter.original_scope(supplied)
    assert restored==scope and actors==[old]
    supplied['transaction_id']='another'
    with pytest.raises(ExecutorError,match='original_stop_scope_unbound'):adapter.original_scope(supplied)


def test_current_stopping_identity_does_not_need_http(native):
    adapter,_,_=native
    adapter.show=lambda *a,**k:{'MainPID':'0'}
    adapter.http.snapshot=lambda *a:pytest.fail('HTTP is already closed during stop')
    assert adapter.current_identity(time.monotonic()+1) is None


def test_released_model_account_is_not_resurrected_for_rollback(native):
    adapter,_,_=native
    binding={'model':'m','unit':'vllm-m.service','lease_id':'lease','gpu':0,'invocation_id':'a'*32}
    context={'backend_bindings':[binding],'current_accounts':[],'removed_models':['m'],
             'effects':{'stop_model:m':{'submitted':True,'acknowledged':True}}}
    adapter.unit_exited=lambda *a:True
    assert adapter._backends(context,time.monotonic()+1,cleanup=True)
    assert adapter._backends(context,time.monotonic()+1,cleanup=False)
    context['removed_models']=[]  # Restored configuration never restores its old lease.
    assert adapter._backends(context,time.monotonic()+1,cleanup=True)
    adapter.unit_exited=lambda *a:False
    assert not adapter._backends(context,time.monotonic()+1,cleanup=True)
    adapter.unit_exited=lambda *a:True;context['effects']={}
    assert not adapter._backends(context,time.monotonic()+1)


def test_only_observed_precommand_abort_is_a_known_absent_start(native):
    adapter,_,context=native;context['candidate_sha256']='c'*64
    assert not adapter._known_unsubmitted_start(context)
    path=adapter._start_record(context,'candidate')
    record={**context,'profile_sha256':adapter.profile_hash,'stage':'submitted','native_command_submitted':True,
            'config_sha256':context['candidate_sha256']}
    private_create(path,record)
    assert not adapter._known_unsubmitted_start(context)
    record.update(stage='aborted_before_command',native_command_submitted=False);path.write_text(json.dumps(record))
    assert adapter._known_unsubmitted_start(context)
    record['operation_id']='other';path.write_text(json.dumps(record))
    assert not adapter._known_unsubmitted_start(context)


def test_start_precondition_failure_records_no_native_submission_and_never_retries(native):
    adapter,_,context=native;context.update(candidate_sha256='c'*64,base_sha256='b'*64)
    calls=[]
    def refuse(*args):calls.append(1);raise ExecutorError('address_busy')
    adapter._start_impl=refuse
    with pytest.raises(ExecutorError,match='address_busy'):adapter._start('start_candidate',context,time.monotonic()+1)
    assert adapter._known_unsubmitted_start(context)
    with pytest.raises(FileExistsError):adapter._start('start_candidate',context,time.monotonic()+1)
    assert calls==[1]


def test_current_kernel_nonce_also_requires_the_bound_native_start_record(native,tmp_path):
    adapter,_,context=native;context['candidate_sha256']='c'*64
    proc=tmp_path/'proc';(proc/'99').mkdir(parents=True)
    (proc/'99/environ').write_bytes(b'LLMSVC_MAINT_TRANSACTION=tx\0LLMSVC_MAINT_OPERATION=op\0LLMSVC_MAINT_PHASE=candidate\0OTHER=ignored\0')
    adapter.proc=proc;identity={'pid':99,'start_ticks':'1','scope_sha256':'a'*64}
    assert not adapter._attempt_binding(identity,context,'candidate')
    path=adapter._start_record(context,'candidate')
    record={**context,'phase':'candidate','profile_sha256':adapter.profile_hash,'stage':'submitted'}
    private_create(path,record)
    assert adapter._attempt_binding(identity,context,'candidate')
    record['stage']='aborted_before_command';path.write_text(json.dumps(record))
    assert not adapter._attempt_binding(identity,context,'candidate')


def test_native_cli_catches_the_same_error_class_without_traceback(native,tmp_path):
    import subprocess
    from deploy.maintenance_executor import digest
    adapter,_,_=native;profile=dict(adapter.profile);profile['native_adapter']=True;profile['helper_program_sha256']='0'*64
    adapter.profile_path.write_text(json.dumps(profile))
    value={'operation':'inspect','context':{'transaction_id':None},'timeout_seconds':2};value['request_id']=digest(value)
    run=subprocess.run([sys.executable,'-B','deploy/maintenance_executor.py','--profile',str(adapter.profile_path),'inspect'],
                       input=json.dumps(value),text=True,capture_output=True,timeout=5)
    assert run.returncode==1 and not run.stdout
    assert json.loads(run.stderr)=={'error':'helper_program_changed'}
    assert 'Traceback' not in run.stderr


def test_scope_hash_is_stable_when_native_actor_inventory_grows(native,monkeypatch,tmp_path):
    from types import SimpleNamespace
    from deploy.maintenance_executor import ScopeInspector
    adapter,_,_=native
    proc=tmp_path/'proc';proc.mkdir()
    def proc_row(pid,parent,argv):
        directory=proc/str(pid);directory.mkdir()
        fields=['S',str(parent)]+['0']*17+['100']
        (directory/'stat').write_text(f'{pid} (fixture) '+' '.join(fields))
        (directory/'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv)+b'\0')
    proc_row(31,1,['native']);proc_row(32,31,['/fixture/wrapper']);adapter.proc=proc
    base_scope={'unit':'native.service','invocation_id':'a'*32,'control_group':'/fixture',
                'fragment_sha256':adapter.profile['fragment_sha256'],'boot_id':'test-boot'}
    base_identity={'pid':31,'start_ticks':'100','scope_sha256':digest(base_scope)}
    records=[base_identity]
    def inspect(_,deadline):return {'identity':base_identity,'scope':base_scope,'actors':list(records)}
    monkeypatch.setattr(ScopeInspector,'inspect',inspect)
    adapter.show=lambda *a,**k:{'Restart':'no'};adapter.native_image=lambda *a:None;adapter.listener_owned=lambda *a:True
    state={'m':'stopped'};adapter.http.snapshot=lambda *a:SimpleNamespace(states=dict(state),requests={})
    adapter.backend=lambda *a,**k:{'model':'m','unit':'vllm-m.service','lease_id':'lease','gpu':0,'invocation_id':'b'*32}
    import shlex
    cfg={'models':{'m':{'cmd':'/fixture/wrapper','cmdStop':shlex.join(adapter._helper_argv('m'))}}}
    adapter.config.write_text(json.dumps(cfg))
    context={'accounts':[[{'model':'m','lease_id':'lease','gpu':0,'status':'confirmed'},'vllm-m.service']]}
    before=adapter.inspect_native(context,time.monotonic()+1)
    state['m']='ready';records.append({'pid':32,'start_ticks':'100','scope_sha256':digest(base_scope)})
    after=adapter.inspect_native(context,time.monotonic()+1)
    assert before['scope']==after['scope'] and before['identity']==after['identity']
    assert len(after['actors'])==len(before['actors'])+1 and 'm' in after['helper_models']


def test_backend_listener_belongs_to_its_unit_not_another_port_holder(native,tmp_path):
    adapter,_,_=native;proc=tmp_path/'proc';(proc/'10/fd').mkdir(parents=True);(proc/'net').mkdir()
    (proc/'10/fd/3').symlink_to('socket:[111]')
    # /proc/net rows: local address/port, LISTEN, inode.
    header='header\n'
    row='0: 0100007F:D432 00000000:0000 0A 0 0 0 0 0 111\n'
    (proc/'net/tcp').write_text(header+row);(proc/'net/tcp6').write_text(header)
    adapter.proc=proc;adapter.members=lambda *a:[10]
    assert adapter.listener_owned('http://127.0.0.1:54322','/fixture',time.monotonic()+1)
    (proc/'net/tcp').write_text(header+row+row.replace('111','222'))
    assert not adapter.listener_owned('http://127.0.0.1:54322','/fixture',time.monotonic()+1)


def test_source_dropin_cannot_bypass_pinned_fragment(native):
    from deploy.maintenance_native import UNIT_KEYS
    adapter,_,_=native
    props={key:'' for key in UNIT_KEYS};props.update(Id=adapter.profile['unit'],LoadState='loaded',MainPID='0',
        FragmentPath=adapter.profile['fragment_path'],DropInPaths='/untracked.conf')
    adapter.runner=lambda *a:''.join(k+'='+v+'\n' for k,v in props.items())
    with pytest.raises(ExecutorError,match='definition_changed'):adapter.show(adapter.profile['unit'],time.monotonic()+1)


def test_indented_lifecycle_hook_is_not_hidden_by_whitespace(native):
    adapter,_,_=native;fragment=Path(adapter.profile['fragment_path'])
    fragment.write_text(fragment.read_text()+'  ExecStopPost=/untracked/helper\n')
    with pytest.raises(ExecutorError,match='lifecycle_hook'):
        NativeAdapter(adapter.profile,adapter.profile_path)
