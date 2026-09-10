# Generated-By: Codex / gpt-6-astra
"""CPU process evidence; temporary cgroup/systemd fixtures are not site proof."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from deploy.maintenance_executor import (ExecutorError, ScopeInspector, digest,
                                        handle, run_bounded)


class Fixture:
    def __init__(self, root, pid, proc_root=Path('/proc')):
        self.fragment = root/'fixture.service'
        self.fragment.write_text('[Service]\nExecStart=/fixture-only\n')
        self.cg = root/'cgroups/system.slice/fixture.service'
        self.cg.mkdir(parents=True)
        (self.cg/'cgroup.procs').write_text(str(pid)+'\n')
        self.props = {'Id':'fixture.service', 'LoadState':'loaded', 'ActiveState':'active',
                      'SubState':'running', 'MainPID':str(pid),
                      'ControlGroup':'/system.slice/fixture.service', 'InvocationID':'a'*32,
                      'FragmentPath':str(self.fragment), 'KillMode':'control-group', 'Delegate':'no'}
        self.calls=[]
        self.profile={'unit':'fixture.service', 'fragment_sha256':hashlib.sha256(self.fragment.read_bytes()).hexdigest()}
        self.inspector=ScopeInspector(self.profile, proc_root=proc_root,
                                      cgroup_root=root/'cgroups', runner=self.show)

    def show(self, argv, deadline):
        self.calls.append(argv)
        assert argv[1:3] == ['show','fixture.service']
        return ''.join(k+'='+v+'\n' for k,v in self.props.items())


def child(code=0):
    # A pipe byte is the deterministic exit barrier; no sleep/scheduling window.
    process=subprocess.Popen([sys.executable,'-c',
        'import os,sys;print("ready",flush=True);os.read(0,1);sys.exit(int(sys.argv[1]))',str(code)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    assert process.stdout.readline()==b'ready\n'
    return process


def finish(process):
    if process.poll() is None:
        process.stdin.write(b'x');process.stdin.flush()
    result=process.wait(timeout=5)
    process.stdin.close();process.stdout.close()
    return result


def envelope(operation, context=None):
    value={'operation':operation,'context':context or {'transaction_id':'fixture'},'timeout_seconds':5}
    return {**value,'request_id':digest(value)}


def test_real_process_identity_and_attributed_delayed_helper_block_settlement(tmp_path):
    proxy, helper=child(),child()
    try:
        f=Fixture(tmp_path,proxy.pid)
        (f.cg/'cgroup.procs').write_text(f'{proxy.pid}\n{helper.pid}\n')
        inspected=f.inspector.inspect(time.monotonic()+5)
        assert {a['pid'] for a in inspected['actors']}=={proxy.pid,helper.pid}
        assert finish(proxy)==0
        f.props['MainPID']='0'
        (f.cg/'cgroup.procs').write_text(f'{helper.pid}\n')
        result=f.inspector.observe_absence(inspected['identity'],inspected['scope'],inspected['actors'],time.monotonic()+5)
        assert result['old_process_absent'] is True
        assert result['observed_actors_absent'] is False
        assert result['scope_empty'] is False
        assert result['settlement_confirmed'] is False
        assert finish(helper)==0
        (f.cg/'cgroup.procs').write_text('')
        result=f.inspector.observe_absence(inspected['identity'],inspected['scope'],inspected['actors'],time.monotonic()+5)
        assert result['observed_actors_absent'] is True and result['scope_empty'] is True
        assert result['settlement_confirmed'] is False  # scope exit alone is insufficient
        assert result['backends_confirmed'] is False
    finally:
        for p in (proxy,helper):
            if p.poll() is None:finish(p)


def test_failed_helper_exit_does_not_become_success_or_cleanup(tmp_path):
    proxy, helper=child(),child(7)
    try:
        f=Fixture(tmp_path,proxy.pid)
        (f.cg/'cgroup.procs').write_text(f'{proxy.pid}\n{helper.pid}\n')
        seen=f.inspector.inspect(time.monotonic()+5)
        finish(proxy); assert finish(helper)==7
        f.props.update(MainPID='0',ActiveState='failed',SubState='failed')
        (f.cg/'cgroup.procs').write_text('')
        result=f.inspector.observe_absence(seen['identity'],seen['scope'],seen['actors'],time.monotonic()+5)
        assert result['observed_actors_absent'] and result['scope_empty']
        assert not result['cleanup_confirmed'] and not result['settlement_confirmed']
    finally:
        for p in (proxy,helper):
            if p.poll() is None:finish(p)


def fake_stat(root,pid,ticks,state='S'):
    path=root/str(pid);path.mkdir(exist_ok=True)
    fields=[state,'1']+['0']*17+[str(ticks)]
    (path/'stat').write_text(f'{pid} (fixture with ) name) '+' '.join(fields))


def test_pid_reuse_is_absent_old_identity_not_authority_to_signal_new(tmp_path):
    proc=tmp_path/'proc';proc.mkdir();fake_stat(proc,31,200)
    f=Fixture(tmp_path,31,proc)
    assert f.inspector.compare({'pid':31,'start_ticks':'100','scope_sha256':'a'*64}) == {
        'old_identity_present':False,'disposition':'pid_reused'}
    assert not f.calls


def test_zombie_and_unreadable_are_not_absence(tmp_path):
    proc=tmp_path/'proc';proc.mkdir();fake_stat(proc,31,100,'Z');f=Fixture(tmp_path,31,proc)
    expected={'pid':31,'start_ticks':'100','scope_sha256':'a'*64}
    assert f.inspector.compare(expected)['old_identity_present']
    (proc/'31/stat').write_text('malformed')
    with pytest.raises(ExecutorError,match='unreadable'):f.inspector.compare(expected)


@pytest.mark.parametrize('change',[{'InvocationID':'0'*32},{'MainPID':'0'},
                                  {'ControlGroup':'/../../escape'},{'LoadState':'not-found'}])
def test_unknown_or_unsafe_unit_fails_closed(tmp_path,change):
    import os
    f=Fixture(tmp_path,os.getpid());f.props.update(change)
    with pytest.raises(ExecutorError):f.inspector.inspect(time.monotonic()+5)


def test_changed_fragment_and_unreadable_descendant_are_not_ignored(tmp_path):
    import os
    f=Fixture(tmp_path,os.getpid());f.fragment.write_text('operator change')
    with pytest.raises(ExecutorError,match='fragment_changed'):f.inspector.inspect(time.monotonic()+5)
    f.profile['fragment_sha256']=hashlib.sha256(f.fragment.read_bytes()).hexdigest()
    (f.cg/'helper').mkdir()
    with pytest.raises(ExecutorError,match='members_unreadable'):f.inspector.inspect(time.monotonic()+5)


def test_inspection_detects_restart_between_samples(tmp_path):
    import os
    f=Fixture(tmp_path,os.getpid());original=f.show
    def restart(argv,deadline):
        if f.calls:f.props['InvocationID']='b'*32
        return original(argv,deadline)
    f.inspector.runner=restart
    with pytest.raises(ExecutorError,match='changed_during'):f.inspector.inspect(time.monotonic()+5)


def test_scope_hash_and_actor_inventory_required(tmp_path):
    import os
    f=Fixture(tmp_path,os.getpid());seen=f.inspector.inspect(time.monotonic()+5)
    with pytest.raises(ExecutorError,match='scope_binding'):
        f.inspector.observe_absence(seen['identity'],{},seen['actors'],time.monotonic()+5)
    with pytest.raises(ExecutorError,match='actor_inventory_missing'):
        f.inspector.observe_absence(seen['identity'],seen['scope'],[],time.monotonic()+5)


def test_dry_run_performs_no_command_or_write(tmp_path):
    class NoIO:
        def inspect(self,*args):raise AssertionError('unexpected inspect')
    before=list(tmp_path.iterdir())
    result=handle(envelope('stop_old'),'stop_old',NoIO(),dry_run=True)
    assert result['dry_run'] and result['accepted'] is False
    assert list(tmp_path.iterdir())==before
    with pytest.raises(ExecutorError,match='not_configured'):
        handle(envelope('stop_old'),'stop_old',NoIO())


def test_request_binding_and_remaining_deadline_checked_before_io():
    e=envelope('inspect');e['context']['transaction_id']='forged'
    with pytest.raises(ExecutorError,match='digest_mismatch'):handle(e,'inspect',None)
    for value in (True,0,-1,901,float('inf')):
        e={'operation':'inspect','context':{},'timeout_seconds':value}
        if value==float('inf'):continue  # canonical rejects nonfinite before invocation
        e['request_id']=digest(e)
        with pytest.raises(ExecutorError,match='deadline'):handle(e,'inspect',None)


def test_bounded_command_nonzero_and_output_limit():
    with pytest.raises(ExecutorError,match='command_failed'):
        run_bounded([sys.executable,'-c','raise SystemExit(7)'],time.monotonic()+5)
    with pytest.raises(ExecutorError,match='output_limit'):
        run_bounded([sys.executable,'-c','print("x"*70000)'],time.monotonic()+5)


def test_command_absolute_deadline_does_not_renew_on_output(tmp_path):
    pidfile=tmp_path/'pid'
    command=[sys.executable,'-u','-c',
             'import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));'
             '\nwhile True: print("tick",flush=True);time.sleep(.1)',str(pidfile)]
    with pytest.raises(ExecutorError,match='deadline'):
        run_bounded(command,time.monotonic()+1)
    assert pidfile.exists()
    assert not Path('/proc',pidfile.read_text()).exists()  # only owned command reaped


def test_missing_cgroup_cannot_turn_into_positive_settlement(tmp_path):
    import os
    f=Fixture(tmp_path,os.getpid())
    seen=f.inspector.inspect(time.monotonic()+5)
    (f.cg/'cgroup.procs').unlink();f.cg.rmdir()
    with pytest.raises(ExecutorError,match='absent_not_settlement'):
        f.inspector.observe_absence(seen['identity'],seen['scope'],seen['actors'],time.monotonic()+5)


def test_real_cli_dry_run_envelope_and_no_effect(tmp_path):
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'unit':'fixture.service','fragment_sha256':'a'*64,
                                   'systemctl':'/must-not-be-executed'}))
    request=envelope('stop_old')
    argv=[sys.executable,'-B','deploy/maintenance_executor.py','--profile',str(profile),
          '--dry-run','stop_old']
    result=subprocess.run(argv,input=json.dumps(request),text=True,capture_output=True,timeout=5)
    assert result.returncode==0, result.stderr
    value=json.loads(result.stdout)
    assert value['request_id']==request['request_id']
    assert value['transaction_id']=='fixture' and value['accepted'] is False
    assert sorted(p.name for p in tmp_path.iterdir())==['profile.json']


def test_current_abi_preflight_unknowns_block_without_fabricating_zero(tmp_path):
    import os
    f=Fixture(tmp_path,os.getpid())
    before=time.monotonic()
    result=handle(envelope('preflight'),'preflight',f.inspector)
    after=time.monotonic()
    assert before <= result['observed_at'] <= after
    assert result['ready'] is False and result['actors_known'] is False
    assert result['in_flight'] is None and result['ingress_state']=='unknown'


def test_proxy_settled_field_does_not_certify_helpers_or_complete_transition(tmp_path):
    proxy=child()
    try:
        f=Fixture(tmp_path,proxy.pid);seen=f.inspector.inspect(time.monotonic()+5)
        finish(proxy);f.props['MainPID']='0';(f.cg/'cgroup.procs').write_text('')
        context={'transaction_id':'fixture','old_identity':seen['identity'],
                 'observed_scope':seen['scope'],'observed_actors':seen['actors']}
        result=handle(envelope('observe_old',context),'observe_old',f.inspector)
        assert result['identity'] is None and result['old_identity']==seen['identity']
        assert result['old_settled'] is True
        assert result['helpers_settled'] is False and result['settlement_confirmed'] is False
    finally:
        if proxy.poll() is None:finish(proxy)
