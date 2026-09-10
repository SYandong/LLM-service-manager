# Generated-By: Codex / gpt-6-astra
"""Bootstrap file/process evidence fixtures; no production or real systemd calls."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

from deploy.bootstrap_native import BootstrapAdapter, checksum, snapshot
from deploy.maintenance_executor import ExecutorError, digest, handle
from deploy.maintenance_native import NativeAdapter, NativeHTTP, private_create


def write(path, data, mode=0o600):
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(mode)


@pytest.fixture
def site(tmp_path, monkeypatch):
    import deploy.bootstrap_native as module
    import deploy.maintenance_native as native_module
    artifacts=tmp_path/'artifacts'; artifacts.mkdir()
    live=tmp_path/'live'; live.mkdir()
    state=tmp_path/'bootstrap'; state.mkdir(mode=0o700)
    native_state=tmp_path/'native-state'; native_state.mkdir(mode=0o700)
    proc=tmp_path/'proc'; (proc/'42').mkdir(parents=True); (proc/'sys/kernel/random').mkdir(parents=True)
    write(proc/'sys/kernel/random/boot_id', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
    write(proc/'42/stat', '42 (source) '+' '.join(['S','1']+['0']*17+['123']))
    write(proc/'42/exe', b'pinned-native')
    config=live/'native.yaml'; fragment=live/'source.service'; env=native_state/'launch.env'
    binary=tmp_path/'native'; write(binary,b'pinned-native',0o755)
    argv=[str(binary),'-config',str(config),'-listen','127.0.0.1:54321']
    write(proc/'42/cmdline', b'\0'.join(v.encode() for v in argv)+b'\0')
    group=tmp_path/'cgroups/source'; group.mkdir(parents=True); write(group/'cgroup.procs','42\n')
    target_profile_path=live/'native-profile.json'
    before_config={'models':{'m':{'cmd':'old-wrapper','cmdStop':'old-stop','aliases':['alias']}},
                   'hooks':{'on_startup':{'preload':['m']}},'globalTTL':0}
    write(config,json.dumps(before_config)); write(fragment,'[Service]\nRestart=always\nKillMode=control-group\n')
    write(live/'launcher',b'old-launcher',0o755)
    source={'unit':'source.service','fragment_path':str(fragment),'fragment_sha256':checksum(fragment.read_bytes()),
            'native_binary':str(binary),'native_binary_sha256':checksum(b'pinned-native'),
            'native_config_path':str(config),'native_config_dir':str(live),'native_origin':'http://127.0.0.1:54321',
            'listen_host':'127.0.0.1','listen_port':54321,'native_probe_origins':['http://127.0.0.1:54321']}
    new_fragment='[Service]\nRestart=no\nEnvironmentFile='+str(env)+'\nKillMode=control-group\n'
    daemon_env=live/'daemon.env';write(daemon_env,'DAEMON_ONLY=yes\n')
    target={**source,'native_adapter':True,'fragment_sha256':checksum(new_fragment.encode()),
            'state_dir':str(native_state),'launch_environment_file':str(env),'helper_python':sys.executable,
            'helper_program_sha256':checksum(Path(native_module.__file__).read_bytes()),'models':{'m':{
                'unit':'vllm-m.service','backend_origin':'http://127.0.0.1:54322','process_argv':['/fixture/wrapper']}}}
    after_config=json.loads(json.dumps(before_config)); after_config['models']['m'].update(
        cmd='/fixture/wrapper',cmdStop=shlex.join([sys.executable,'-B',str(Path(native_module.__file__).resolve()),
        'helper','--profile',str(target_profile_path),'--model','m','--pid','${PID}']))
    payloads={'unit_fragment':(fragment,new_fragment,0o600),'native_config':(config,json.dumps(after_config),0o600),
              'launcher':(live/'launcher','new-reviewed-launcher',0o755),
              'launcher_config':(live/'launcher.json',json.dumps({'systemd_run':{'environment_file':str(daemon_env)}}),0o600),
              'native_profile':(target_profile_path,json.dumps(target),0o600),
              'attempt_environment':(env,'',0o600)}
    rows={}
    for kind,(destination,data,mode) in payloads.items():
        content=data.encode(); src=artifacts/kind; write(src,content)
        rows[kind]={'source':str(src),'target':str(destination),'before':snapshot(destination),
                    'after':{'sha256':checksum(content),'mode':mode,'uid':os.geteuid(),'gid':os.getegid()}}
    manifest={'schema_version':1,'state_dir':str(state),'artifact_root':str(artifacts),
              'default_model':'m','default_unit':'vllm-m.service','source_profile':source,'files':rows,
              'native_environment_files':[],'daemon_environment_file':{'path':str(daemon_env),'sha256':checksum(daemon_env.read_bytes())}}
    manifest_path=tmp_path/'manifest.json'; private_create(manifest_path,manifest)
    profile={'bootstrap_adapter':True,'manifest_path':str(manifest_path),'manifest_sha256':checksum(manifest_path.read_bytes())}
    profile_path=tmp_path/'bootstrap-profile.json'; private_create(profile_path,profile)
    properties={'Id':'source.service','LoadState':'loaded','ActiveState':'active','SubState':'running','MainPID':'42',
                'ControlGroup':'/source','InvocationID':'a'*32,'FragmentPath':str(fragment),'DropInPaths':'',
                'Restart':'always','KillMode':'control-group','Job':'','NeedDaemonReload':'no'}
    calls=[]; backend_live=[False]
    def runner(argv,deadline):
        calls.append(list(argv))
        if argv[1]=='show':
            unit=argv[2]; props=dict(properties)
            if unit!='source.service':
                props.update(Id=unit,LoadState='loaded' if backend_live[0] else 'not-found',
                    ActiveState='active' if backend_live[0] else 'inactive',MainPID='99' if backend_live[0] else '0',
                    ControlGroup='/backend' if backend_live[0] else '',FragmentPath='',Restart='no',
                    Environment='LLMSVC_LEASE_ID=real-fixture-lease CUDA_VISIBLE_DEVICES=0' if backend_live[0] else '')
            keys=next(x.split('=',1)[1] for x in argv if x.startswith('--property='))
            return '\n'.join(k+'='+props.get(k,'') for k in keys.split(','))
        if argv[0]==str(binary): return 'config is valid'
        if argv[1]=='start':
            assert argv[2]=='source.service'
            for pid,parent,cmd in [(43,1,[str(binary),'-config',str(config),'-listen','127.0.0.1:54321']),
                                   (44,43,['/fixture/wrapper'])]:
                (proc/str(pid)).mkdir()
                write(proc/str(pid)/'stat',str(pid)+' (source) '+' '.join(['S',str(parent)]+['0']*17+[str(200+pid)]))
                write(proc/str(pid)/'cmdline',b'\0'.join(x.encode() for x in cmd)+b'\0')
                write(proc/str(pid)/'exe',b'pinned-native')
            write(proc/'43/environ',env.read_bytes().replace(b'\n',b'\0'))
            write(group/'cgroup.procs','43\n44\n')
            properties.update(MainPID='43',ActiveState='active',SubState='running',ControlGroup='/source',InvocationID='b'*32)
            snapshot_value.states={'m':'ready'}
            return ''
        if argv[1]=='daemon-reload':
            properties['Restart']='no' if 'Restart=no' in fragment.read_text() else 'always';return ''
        raise AssertionError('unexpected command '+repr(argv))
    adapter=BootstrapAdapter(profile,profile_path,proc_root=proc,cgroup_root=tmp_path/'cgroups',runner=runner)
    monkeypatch.setattr(NativeAdapter,'listener_owned',lambda *a:True)
    monkeypatch.setattr(NativeAdapter,'_address_available',lambda *a:True)
    snapshot_value=SimpleNamespace(states={'m':'stopped'},requests={})
    monkeypatch.setattr(NativeHTTP,'snapshot',lambda *a:snapshot_value)
    def stop(expected,inspector,deadline):
        assert inspector.inspect(deadline)['identity']==expected
        # Only the low-level effect is simulated; absence is re-derived from
        # changed fake /proc/cgroup/systemd observations by actual executor code.
        shutil.rmtree(proc/'42'); write(group/'cgroup.procs','')
        properties.update(MainPID='0',ActiveState='inactive',SubState='dead',ControlGroup='')
        return {'accepted':True}
    monkeypatch.setattr(module,'stop_bound_process',stop)
    context={'bootstrap_id':'test','transaction_id':'test','manifest_sha256':profile['manifest_sha256'],
             'default_model':'m','default_unit':'vllm-m.service','effects':{},'account':None,'launch_submitted':False}
    return SimpleNamespace(adapter=adapter,context=context,root=state/'test',rows=rows,calls=calls,
        properties=properties,backend_live=backend_live,snapshot=snapshot_value,proc=proc,group=group,
        manifest=manifest,manifest_path=manifest_path,profile_path=profile_path,profile=profile)


def stage(site):
    first=site.adapter.operation('bootstrap_preflight',site.context,time.monotonic()+3)
    site.context['source_identity']=first['identity']
    site.context['effects']['bootstrap_stage']={'submitted':True,'acknowledged':False}
    return site.adapter.operation('bootstrap_stage',site.context,time.monotonic()+3)


def test_preflight_is_read_only_and_preserves_unleased_default(site):
    before={str(p):p.read_bytes() for p in site.profile_path.parent.rglob('*') if p.is_file()}
    value=site.adapter.operation('bootstrap_preflight',site.context,time.monotonic()+3)
    assert value['ready'] and value['in_flight']==0 and value['default_preload_preserved']
    assert value['legacy_backends_absent'] and not site.root.exists()
    assert before=={str(p):p.read_bytes() for p in site.profile_path.parent.rglob('*') if p.is_file()}
    assert all(call[1]=='show' for call in site.calls)
    assert site.context['account'] is None


@pytest.mark.parametrize('case',['inflight','model','backend','argv','pin'])
def test_preflight_refuses_unsafe_or_unbound_baseline_without_effects(site,case):
    if case=='inflight':site.snapshot.requests={'r':'m'}
    if case=='model':site.snapshot.states={'m':'ready'}
    if case=='backend':site.backend_live[0]=True
    if case=='argv':write(site.proc/'42/cmdline',b'changed\0')
    if case=='pin':site.context['manifest_sha256']='0'*64
    with pytest.raises(ExecutorError):site.adapter.operation('bootstrap_preflight',site.context,time.monotonic()+2)
    assert not site.root.exists() and all(call[1]=='show' for call in site.calls)


def test_stage_receipt_requires_actual_absence_and_exact_installed_bytes(site):
    result=stage(site)
    assert result['source_absent'] and result['helpers_settled'] and result['staged']
    assert result['legacy_backends_absent'] and result['in_flight'] is None
    assert not result['default_confirmed'] and not result['active_ready']
    assert site.adapter._files_match('after')
    assert json.loads((site.root/'record.json').read_text())['stop_submitted']
    assert sum(call[1]=='daemon-reload' for call in site.calls)==1
    assert not any(call[1] in ('start','stop') for call in site.calls)
    with pytest.raises(ExecutorError):
        site.adapter.operation('bootstrap_stage',site.context,time.monotonic()+2)


def test_ack_or_crash_after_stop_submission_does_not_certify_staged(site,monkeypatch):
    import deploy.bootstrap_native as module
    def unknown(*a):raise OSError('lost result')
    monkeypatch.setattr(module,'stop_bound_process',unknown)
    with pytest.raises(OSError):stage(site)
    result=site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert not result['source_absent'] and not result['helpers_settled'] and not result['staged']
    assert json.loads((site.root/'record.json').read_text())['stop_submitted']
    assert Path(site.rows['launcher']['target']).read_bytes()==b'old-launcher'


def test_partial_file_install_stays_observe_only_until_safe_rollback(site,monkeypatch):
    original=site.adapter._install
    def fail(root,record,kind):
        if kind=='launcher_config':raise OSError('injected disk failure')
        return original(root,record,kind)
    monkeypatch.setattr(site.adapter,'_install',fail)
    with pytest.raises(OSError):stage(site)
    observed=site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert observed['source_absent'] and not observed['staged']
    before_calls=len(site.calls)
    with pytest.raises(ExecutorError):site.adapter.operation('bootstrap_stage',site.context,time.monotonic()+2)
    assert not any(c[1]=='start' for c in site.calls[before_calls:])
    site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    result=site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert result['rolled_back'] and not result['old_source_restarted'] and not result['ledger_restored']
    assert site.adapter._files_match('before')


@pytest.mark.parametrize('change',['live_default','launch_submitted','account','foreign_file'])
def test_rollback_never_stops_default_or_overwrites_foreign_state(site,change):
    stage(site)
    site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    if change=='live_default':site.backend_live[0]=True
    if change=='launch_submitted':site.context['launch_submitted']=True
    if change=='account':site.context['account']={'status':'confirmed'}
    if change=='foreign_file':write(Path(site.rows['native_profile']['target']),'foreign')
    with pytest.raises(ExecutorError):site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert not any(c[1] in ('start','stop') for c in site.calls)


def test_activate_needs_actual_confirmed_default_before_any_start(site):
    stage(site)
    site.context['effects']['bootstrap_activate']={'submitted':True,'acknowledged':False}
    with pytest.raises(ExecutorError,match='confirmed_default'):
        site.adapter.operation('bootstrap_activate',site.context,time.monotonic()+2)
    assert not json.loads((site.root/'record.json').read_text())['activation_submitted']
    assert not any(c[1]=='start' for c in site.calls)


@pytest.mark.parametrize('operation',['bootstrap_stage','bootstrap_activate','bootstrap_rollback'])
def test_dry_run_has_no_commands_lock_files_or_submission_requirement(site,operation):
    before=list(site.calls)
    result=site.adapter.operation(operation,site.context,time.monotonic()+1,dry_run=True)
    assert result['dry_run'] and not result['accepted']
    assert site.calls==before and not site.root.exists()
    with pytest.raises(ExecutorError,match='durable_submission'):
        site.adapter.operation(operation,site.context,time.monotonic()+1)


def test_artifact_changed_after_preflight_is_rejected_before_replacement(site,monkeypatch):
    original=site.adapter._install
    def tamper(root,record,kind):
        if kind=='unit_fragment':write(Path(site.rows[kind]['source']),'unreviewed')
        return original(root,record,kind)
    monkeypatch.setattr(site.adapter,'_install',tamper)
    with pytest.raises(ExecutorError,match='replacement_digest'):stage(site)
    assert snapshot(site.rows['unit_fragment']['target'])==site.rows['unit_fragment']['before']
    assert not any(c[1]=='daemon-reload' for c in site.calls)


def test_command_backend_envelope_keeps_correlation(site):
    envelope={'operation':'bootstrap_preflight','context':site.context,'timeout_seconds':2}
    envelope['request_id']=digest(envelope)
    result=handle(envelope,'bootstrap_preflight',site.adapter)
    assert result['request_id']==envelope['request_id'] and result['transaction_id']=='test'


def confirm_default_fixture(site):
    site.backend_live[0]=True
    path=site.proc/'99';path.mkdir()
    write(path/'stat','99 (backend) '+' '.join(['S','1']+['0']*17+['300']))
    site.context['account']={'model':'m','unit':'vllm-m.service','lease_id':'real-fixture-lease',
                             'gpu':0,'status':'confirmed','budget_gb':12}
    site.context['launch_submitted']=True
    site.context['effects']['bootstrap_activate']={'submitted':True,'acknowledged':False}


def test_actual_adapter_checks_new_source_and_existing_confirmed_account(site):
    stage(site);confirm_default_fixture(site)
    result=site.adapter.operation('bootstrap_activate',site.context,time.monotonic()+3)
    assert result['active_ready'] and result['default_confirmed'] and not result['source_absent']
    assert result['identity']['pid']==43 and result['identity']['start_ticks']=='243'
    assert result['helpers_settled'] and result['in_flight']==0
    assert len([c for c in site.calls if c[1]=='start'])==1
    site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    with pytest.raises(ExecutorError,match='live_or_unknown'):
        site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert site.context['account']['budget_gb']==12
    assert not any(c[1]=='stop' for c in site.calls)


def test_lost_start_reply_is_reconciled_from_instance_without_replaying_start(site):
    stage(site);confirm_default_fixture(site)
    original=site.adapter.runner
    def lost(argv,deadline):
        value=original(argv,deadline)
        if argv[1]=='start':raise OSError('reply lost after real simulated effect')
        return value
    site.adapter.runner=lost
    with pytest.raises(OSError):
        site.adapter.operation('bootstrap_activate',site.context,time.monotonic()+3)
    observed=site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert observed['active_ready'] and observed['default_confirmed']
    with pytest.raises(ExecutorError,match='activation_state_unknown'):
        site.adapter.operation('bootstrap_activate',site.context,time.monotonic()+2)
    assert len([c for c in site.calls if c[1]=='start'])==1


@pytest.mark.parametrize('key,value',[('lease_id','foreign-lease'),('gpu',1)])
def test_confirmed_row_without_current_unit_binding_cannot_activate(site,key,value):
    stage(site);confirm_default_fixture(site);site.context['account'][key]=value
    with pytest.raises(ExecutorError,match='account_mismatch'):
        site.adapter.operation('bootstrap_activate',site.context,time.monotonic()+2)
    assert not any(c[1]=='start' for c in site.calls)


def test_unknown_new_instance_environment_never_claims_ready(site):
    stage(site);confirm_default_fixture(site)
    result=site.adapter.operation('bootstrap_activate',site.context,time.monotonic()+3)
    assert result['active_ready']
    write(site.proc/'43/environ',b'LLMSVC_MAINT_TRANSACTION=foreign\0')
    with pytest.raises(ExecutorError,match='attempt_unbound'):
        site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)


def test_restart_reconciles_completed_file_stage_without_callback_flag(site,monkeypatch):
    import deploy.bootstrap_native as module
    original=module.private_update
    def crash(path,expected,changes):
        if changes=={'staged':True}:raise OSError('crash before callback status')
        return original(path,expected,changes)
    monkeypatch.setattr(module,'private_update',crash)
    with pytest.raises(OSError):stage(site)
    restarted=BootstrapAdapter(site.profile,site.profile_path,**site.adapter.options)
    observed=restarted.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert observed['staged'] and observed['source_absent'] and observed['helpers_settled']
    confirm_default_fixture(site)
    result=restarted.operation('bootstrap_activate',site.context,time.monotonic()+3)
    assert result['active_ready'] and result['default_binding']['lease_id']=='real-fixture-lease'


def test_before_stop_failure_can_restore_fragment_without_restarting_source(site,monkeypatch):
    original=site.adapter._capture
    def fail(deadline,*,staged_fragment=False):
        if staged_fragment:raise ExecutorError('unit check rejected before stop')
        return original(deadline)
    monkeypatch.setattr(site.adapter,'_capture',fail)
    with pytest.raises(ExecutorError):stage(site)
    monkeypatch.setattr(site.adapter,'_capture',original)
    site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    result=site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert result['original_source_retained'] and not result['old_source_restarted']
    assert not result['source_absent'] and site.properties['MainPID']=='42'
    assert site.adapter._files_match('before')
    assert not any(c[1] in ('start','stop') for c in site.calls)


def test_missing_native_model_rows_are_unknown_not_stopped(site):
    site.snapshot.states={}
    with pytest.raises(ExecutorError,match='busy_or_model_present'):
        site.adapter.operation('bootstrap_preflight',site.context,time.monotonic()+2)
    assert not site.root.exists()


def test_rollback_prechecks_every_file_before_deleting_any_owned_target(site):
    stage(site)
    write(Path(site.rows['native_profile']['target']),'foreign')
    before={kind:snapshot(row['target']) for kind,row in site.rows.items()}
    site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    with pytest.raises(ExecutorError,match='foreign_file'):
        site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert before=={kind:snapshot(row['target']) for kind,row in site.rows.items()}


def test_explicit_daemon_environment_change_blocks_without_export_or_effect(site):
    path=Path(site.manifest['daemon_environment_file']['path']);write(path,'CHANGED=yes\n')
    with pytest.raises(ExecutorError,match='environment_file_changed'):
        site.adapter.operation('bootstrap_preflight',site.context,time.monotonic()+2)
    assert not site.root.exists() and not site.calls


def test_readonly_observation_never_creates_missing_state(site):
    with pytest.raises(FileNotFoundError):
        site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+1)
    assert not site.root.exists()


def test_bootstrap_cli_invalid_manifest_is_redacted_and_zero_effect(site):
    import subprocess
    profile=dict(site.profile,manifest_sha256='0'*64)
    site.profile_path.write_text(json.dumps(profile))
    request={'operation':'bootstrap_preflight','context':site.context,'timeout_seconds':1}
    request['request_id']=digest(request)
    result=subprocess.run([sys.executable,'-B','deploy/maintenance_executor.py','--profile',str(site.profile_path),'bootstrap_preflight'],
                          input=json.dumps(request),text=True,capture_output=True,timeout=3)
    assert result.returncode==1 and not result.stdout
    assert json.loads(result.stderr)=={'error':'bootstrap_manifest_pin_changed'}
    assert not site.root.exists()


@pytest.mark.parametrize('name',['.','..'])
def test_bootstrap_identifier_cannot_select_a_parent_directory(site,name):
    site.context.update(bootstrap_id=name,transaction_id=name)
    with pytest.raises(ExecutorError,match='identifier_path'):
        site.adapter.operation('bootstrap_preflight',site.context,time.monotonic()+2)
    assert not site.calls


def test_bootstrap_existing_state_directory_cannot_be_a_symlink(site):
    site.root.symlink_to(site.profile_path.parent,target_is_directory=True)
    with pytest.raises(ExecutorError,match='directory_symlink'):
        site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert not site.calls


def test_rollback_flag_does_not_certify_changed_files_or_unloaded_definition(site):
    stage(site);site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    observed=site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert observed['rolled_back']
    site.properties['NeedDaemonReload']='yes'
    observed=site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    assert not observed['rolled_back']
    site.properties['NeedDaemonReload']='no'
    write(Path(site.rows['launcher']['target']),'foreign')
    assert not site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)['rolled_back']


def test_completed_rollback_after_lost_callback_is_observed_not_repeated(site,monkeypatch):
    import deploy.bootstrap_native as module
    stage(site);site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    original=module.private_update
    def lost(path,expected,changes):
        if changes=={'rolled_back':True}:raise OSError('callback flag lost')
        return original(path,expected,changes)
    monkeypatch.setattr(module,'private_update',lost)
    with pytest.raises(OSError):site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)['rolled_back']
    with pytest.raises(ExecutorError):site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)


def test_reboot_cannot_reuse_old_attempt_tags_or_file_receipts(site):
    stage(site)
    write(site.proc/'sys/kernel/random/boot_id','11111111-2222-3333-4444-555555555555')
    before=len(site.calls)
    with pytest.raises(ExecutorError,match='boot_identity_changed'):
        site.adapter.operation('bootstrap_observe',site.context,time.monotonic()+2)
    site.context['effects']['bootstrap_rollback']={'submitted':True,'acknowledged':False}
    with pytest.raises(ExecutorError,match='boot_identity_changed'):
        site.adapter.operation('bootstrap_rollback',site.context,time.monotonic()+2)
    assert len(site.calls)==before
