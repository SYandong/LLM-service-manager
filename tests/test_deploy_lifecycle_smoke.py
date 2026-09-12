# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
from pathlib import Path
import subprocess
import time

import pytest

SPEC = importlib.util.spec_from_file_location('lifecycle_smoke', Path(__file__).parents[1] / 'deploy/lifecycle_smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def config(tmp_path):
    (tmp_path/'llmsvc/collectors').mkdir(parents=True,exist_ok=True)
    (tmp_path/'llmsvc/collectors/__init__.py').write_text('')
    (tmp_path/'llmsvc/state.py').write_text('')
    return {'container': 'test', 'lock_path': str(tmp_path / 'lock'), 'model_path': '/cached/model',
            'vllm_binary': '/test/vllm', 'source': str(tmp_path), 'output_dir': str(tmp_path / 'out'),
            'gpu': 0, 'wall_seconds': 300, 'swap_url': 'http://127.0.0.1:9999'}


def test_dry_run_does_not_create_lock_directory_or_run(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json';path.write_text(json.dumps(config(tmp_path)))
    before=sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    monkeypatch.setattr(smoke, 'Run', lambda _: pytest.fail('no runner in dry-run'))
    assert smoke.main(['--config', str(path), '--dry-run']) == 0
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('seconds', [301, float('nan'), float('inf'), -1, True])
def test_invalid_wall_budget_is_rejected(tmp_path, seconds):
    value = config(tmp_path);value['wall_seconds'] = seconds
    with pytest.raises(smoke.SmokeError):smoke.validate(value)


def test_uuid_scope_and_exact_ownership():
    a=smoke.identity();b=smoke.identity()
    assert a != b and a[2].startswith('vllm-ops-life-') and a[2].endswith('.service')
    assert smoke.owned('LLMSVC_OPS_RUN_ID=abc OTHER=value', 'abc')
    assert not smoke.owned('LLMSVC_OPS_RUN_ID=abcdef', 'abc')
    assert smoke.cgroup_owned('0::/lxc.payload.test/system.slice/'+a[2], a[2])
    assert not smoke.cgroup_owned('0::/lxc.payload.test/system.slice/'+a[2]+'-foreign', a[2])


def test_deadline_never_extends(monkeypatch):
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: 100)
    assert smoke.remaining(103, 20) == 3
    with pytest.raises(smoke.SmokeError):smoke.remaining(100, 20)


def test_quiet_requires_complete_zero_inflight_and_stopped_models():
    assert not smoke.quiet({})
    state={'events':[{'type':'modelStatus','data':'[{"state":"stopped"}]'},
                     {'type':'inflight','data':'{"operation":"snapshot"}'}]}
    assert smoke.quiet(state)
    state['events'][0]['data']='[{"state":"ready"}]'
    assert not smoke.quiet(state)


def test_scheduler_actions_allows_other_gpu_bystander_after_selected_isolation():
    state = {'events': [
        {'type': 'modelStatus', 'data': '[{"id":"default","state":"ready"}]'},
        {'type': 'inflight', 'data': '{"operation":"snapshot","requests":[]}'}]}
    assert smoke.scheduler_actions_quiet(state, 'selected')
    assert not smoke.quiet(state)


@pytest.mark.parametrize('states,requests', [
    ('[{"id":"selected","state":"ready"}]', []),
    ('[{"id":"other"}]', []),
    ('[{"id":"other","state":"unknown"}]', []),
    ('[{"id":"selected","state":"stopped"}]', [{'id': 'inflight'}]),
])
def test_scheduler_actions_rejects_selected_unknown_or_inflight(states, requests):
    state = {'events': [
        {'type': 'modelStatus', 'data': states},
        {'type': 'inflight', 'data': json.dumps({'operation': 'snapshot', 'requests': requests})}]}
    assert not smoke.scheduler_actions_quiet(state, 'selected')


def _resident_process(pid, start='10', cgroup='/system.slice/external.service', samples=None, **extra):
    return {'gpu_uuid':'GPU0','pid':pid,'start_ticks':start,'cgroup':cgroup,
            'utilization_samples':[0.0,0.0] if samples is None else samples, **extra}


def test_idle_resident_predicate_accepts_classified_baseline_and_full_budget_margin():
    protected=_resident_process(10,model='protected',full_budget_gb=42.12041015625,
                                sleeping_proof=True,health_proof=True,cgroup='/system.slice/vllm-protected.service')
    baseline=_resident_process(20,model='external',cgroup='/user.slice/external.service')
    protected_observed={**protected,'ledger_status':'confirmed','model_state':'sleeping',
                        'health_status':'sleeping'}
    baseline_observed={**baseline}
    observation={'sampled_at':time.time(),'source':'collector','ledger_source':'state+ledger',
                 'unit_source':'systemd+proc','capacity_source':'nvidia-smi','inflight':0,
                 'protected_processes':[protected_observed],'baseline_processes':[baseline_observed],
                 'external_baseline_gb':1.831,'candidate_util':9.828/140.4013671875}
    result=smoke.idle_resident_admission(
        gpu={'uuid':'GPU0','total_gb':140.4013671875,'free_gb':135.0,'utilization_percent':0.0},
        protected_processes=[protected],baseline_processes=[baseline],
        current_processes=[protected,baseline],candidate_full_budget_gb=9.828,
        external_baseline_gb=1.831,inflight=0,margin_gb=4,observation=observation)
    assert result['eligible'] is True and result['required_gb']==pytest.approx(57.77941015625)


@pytest.mark.parametrize('change', ['budget','inflight','active','changed','unknown'])
def test_idle_resident_predicate_fails_closed_on_capacity_or_identity(change):
    protected=_resident_process(10,model='protected',full_budget_gb=42.0,sleeping_proof=True,health_proof=True)
    baseline=_resident_process(20,model='external')
    current=[protected,baseline]; kwargs={'candidate_full_budget_gb':9.8,'external_baseline_gb':1.8,'inflight':0}
    gpu={'uuid':'GPU0','total_gb':140.0,'free_gb':120.0,'utilization_percent':0.0}
    if change=='budget':gpu['total_gb']=50
    if change=='inflight':kwargs['inflight']=1
    if change=='active':baseline['utilization_samples']=[0.0,2.0]
    if change=='changed':current=[protected,{**baseline,'start_ticks':'99'}]
    if change=='unknown':current=[protected,{**baseline,'gpu_uuid':'GPU1'}]
    observed_protected={**protected,'ledger_status':'confirmed','model_state':'sleeping',
                        'health_status':'sleeping'}
    observed_baseline={**baseline}
    if change=='active':observed_baseline['utilization_samples']=[0.0,2.0]
    observation={'sampled_at':time.time(),'source':'collector','ledger_source':'state+ledger',
                     'unit_source':'systemd+proc','capacity_source':'nvidia-smi',
                     'inflight':1 if change=='inflight' else 0,
                 'protected_processes':[observed_protected],'baseline_processes':[observed_baseline],
                 'external_baseline_gb':1.8,'candidate_util':9.8/140.0}
    result=smoke.idle_resident_admission(gpu=gpu,protected_processes=[protected],baseline_processes=[baseline],current_processes=current,**kwargs,observation=observation)
    assert result['eligible'] is False and result['reasons']


def test_idle_resident_mode_validation_is_opt_in_and_strict(tmp_path):
    value=config(tmp_path);value['mode']='scheduler-actions';value.update({
        'native_binary':'/native','wrapper_binary':'/wrapper','scheduler_python':'/python',
        'host_meminfo_path':'/approved/meminfo','nvidia_smi':'/usr/bin/nvidia-smi',
        'native_binary_sha256':'a'*64,'wrapper_sha256':'b'*64})
    for name in ('cli/llm','deploy/vllm-launch','deploy/maintenance_native.py','deploy/maintenance_executor.py'):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('')
    value['idle_resident']={
        'candidate_full_budget_gb':9.828,'external_baseline_gb':1.831,'margin_gb':4,
        'idle_util_percent':1,'inflight':0,'protected_processes':[],'baseline_processes':[],
        'enabled':True}
    value['mode']='idle_resident'
    smoke.validate(value)
    value['idle_resident']['candidate_full_budget_gb']=float('nan')
    with pytest.raises(smoke.SmokeError):smoke.validate(value)


def test_cleanup_never_stops_foreign_unit_or_removes_its_files():
    run=smoke.Run.__new__(smoke.Run);run.attempted=True;run.temp_created=True;run.unit='vllm-ops-life-test.service'
    run.verify_owner=lambda **_: False
    calls=[]
    def container(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'active\n', '')
    run.container=container
    run.python=lambda *_a, **_kw:pytest.fail('must preserve files while foreign/active unit exists')
    with pytest.raises(smoke.SmokeError):run.cleanup()
    assert calls == [['systemctl','is-active',run.unit]]


def test_cleanup_stops_only_owned_generated_unit():
    run=smoke.Run.__new__(smoke.Run);run.attempted=True;run.temp_created=False;run.unit='vllm-ops-life-test.service'
    run.verify_owner=lambda **_: True
    run.log=lambda *_a, **_kw:None
    calls=[]
    def container(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'inactive\n', '')
    run.container=container
    run.cleanup()
    assert calls == [['systemctl','stop',run.unit],['systemctl','is-active',run.unit]]


def test_host_capacity_guard_rejects_unknown_small_ram_and_large_weights():
    smoke.check_host_capacity(100,15*1024**3)
    for available, weights in ((float('nan'),15*1024**3),(32,15*1024**3),(1000,21*1024**3)):
        with pytest.raises(smoke.SmokeError):smoke.check_host_capacity(available,weights)


def test_default_hooks_are_inert():
    run = smoke.Run.__new__(smoke.Run)
    assert smoke.Run.scope == 'cached-base collector lifecycle only'
    assert smoke.Run.lora_measured is False
    assert run.prepare_fixture() is None
    assert run.extra_env() == {}
    assert run.extra_args() == []


def test_execute_calls_default_hooks_before_launch(monkeypatch):
    events = []

    class HookedRun(smoke.Run):
        def __init__(self):
            self.config = {'gpu': 0, 'vllm_binary': '/test/vllm', 'model_path': '/cached/model', 'wall_seconds': 300}
            self.token = 'tok'
            self.model = 'ops-life-test'
            self.unit = 'vllm-ops-life-test.service'
            self.deadline = 1000
            self.work_deadline = 900
            self.temp = '/tmp/ops-life-test'
            self.records = []
            self.attempted = False
            self.temp_created = False
            self.port = 12345
            self.source_hash = 'source'
            self.argv = None

        def log(self, kind, **detail):
            events.append(('log', kind, detail))

        def inventory(self, *, allow_own=False):
            events.append(('inventory', allow_own))

        def prepare_fixture(self):
            assert self.temp_created is True
            events.append(('prepare_fixture',))

        def extra_env(self):
            events.append(('extra_env',))
            return {'EXTRA_SMOKE_ENV': '1'}

        def extra_args(self):
            events.append(('extra_args',))
            return ['--extra-smoke-arg']

        def container(self, argv, **kwargs):
            events.append(('container', argv))
            if argv[:4] == ['systemctl', 'show', self.unit, '-p']:
                return subprocess.CompletedProcess(argv, 0, 'not-found\n', '')
            if argv and argv[0] == 'systemd-run':
                self.argv = argv
                raise smoke.SmokeError('stop before runtime launch')
            return subprocess.CompletedProcess(argv, 0, '{}', '')

        def python(self, code, data, **kwargs):
            events.append(('python', data))
            return {}

        def cleanup(self):
            events.append(('cleanup',))

    monkeypatch.setattr(smoke.time, 'monotonic', lambda: 100)
    run = HookedRun()
    assert run.execute() == 'failed'
    assert ('prepare_fixture',) in events
    assert events.index(('prepare_fixture',)) < events.index(('extra_env',))
    assert events.index(('extra_env',)) < events.index(('extra_args',))
    assert any(item == '--setenv=EXTRA_SMOKE_ENV=1' for item in run.argv)
    assert run.argv[-1] == '--extra-smoke-arg'
    complete = [event for event in events if event[0] == 'log' and event[1] == 'complete'][0]
    assert complete[2]['scope'] == smoke.Run.scope
    assert complete[2]['lora_measured'] is False
