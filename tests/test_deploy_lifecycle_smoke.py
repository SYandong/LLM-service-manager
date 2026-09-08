# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
from pathlib import Path
import subprocess

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
