# Generated-By: Codex / gpt-6-astra
"""Synthetic fixture and phase safety checks; no installed model/GPU required."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time

import pytest


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


synthetic = load('synthetic_lora')
# Match direct-script sibling resolution without changing production import paths.
sys.path.insert(0, str(DEPLOY))
try:
    smoke = load('lora_smoke')
finally:
    sys.path.remove(str(DEPLOY))


def base(tmp_path):
    p = tmp_path / 'base'; p.mkdir()
    (p / 'config.json').write_text(json.dumps({'model_type': 'qwen2', 'architectures': ['Qwen2ForCausalLM'],
                                              'hidden_size': 16, 'num_hidden_layers': 2, 'num_attention_heads': 2}))
    (p / 'weights').write_bytes(b'never change this base')
    return p


def test_fixture_has_zero_bf16_rank1_pairs_and_preserves_base(tmp_path):
    cached = base(tmp_path)
    before = {p.name: p.read_bytes() for p in cached.iterdir()}
    output = tmp_path / 'adapter'
    result = synthetic.generate(cached, output)
    raw = (output / 'adapter_model.safetensors').read_bytes()
    size = struct.unpack('<Q', raw[:8])[0]
    header = json.loads(raw[8:8+size]); tensor_data = raw[8+size:]
    assert header.pop('__metadata__')['fixture'] == synthetic.LABEL
    assert len(header) == 4 and len(tensor_data) == 128 and not any(tensor_data)
    assert [v['data_offsets'] for _, v in sorted(header.items())] == [[0,32],[32,64],[64,96],[96,128]]
    assert all(v['dtype'] == 'BF16' for v in header.values())
    assert {tuple(v['shape']) for v in header.values()} == {(1,16),(16,1)}
    assert result['rank'] == 1 and result['quality_evidence'] is False
    assert result['trained'] is False
    assert before == {p.name: p.read_bytes() for p in cached.iterdir()}
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir())
    with pytest.raises(ValueError, match='must not exist'):
        synthetic.generate(cached, output)


def test_fixture_dry_run_and_base_or_symlink_refusal(tmp_path):
    cached = base(tmp_path)
    assert synthetic.generate(cached, tmp_path/'adapter', dry_run=True)['dry_run']
    assert not (tmp_path/'adapter').exists()
    with pytest.raises(ValueError, match='outside'):
        synthetic.generate(cached, cached/'adapter')
    (tmp_path/'alias').symlink_to(cached, target_is_directory=True)
    with pytest.raises(ValueError):
        synthetic.generate(cached, tmp_path/'alias')


@pytest.mark.parametrize('change', [{'model_type':'other'}, {'hidden_size':True}, {'num_hidden_layers':10000},
                                   {'head_dim':1}, {'num_attention_heads':3}])
def test_unsupported_fixture_refuses_before_output(tmp_path, change):
    cached = base(tmp_path); p = cached/'config.json'
    config = json.loads(p.read_text()); config.update(change); p.write_text(json.dumps(config))
    with pytest.raises(ValueError): synthetic.generate(cached, tmp_path/'adapter')
    assert not (tmp_path/'adapter').exists()


def runner():
    run = smoke.LoRARun.__new__(smoke.LoRARun)
    run.model = 'ops-life-test'; run.adapter_name = 'ops-life-test-synthetic-zero'
    run.adapter_path = '/tmp/ops-life-test/adapter'
    run.deadline = time.monotonic()+300
    run.records = []; run.log = lambda kind, **kw: run.records.append((kind,kw))
    run.collect = lambda expected: None
    run.memory = lambda phase: None
    run.adapter_listed = lambda expected: None
    return run


def test_phase_sequence_requires_inference_after_wake_and_one_load():
    run = runner(); phases = []
    run.measured = lambda phase, path, payload, **kw: phases.append((phase,path,payload))
    def inference(phase, model):
        phases.append((phase,model))
        return 'same digest'
    run.inference = inference
    run.exercise()
    assert [p[0] for p in phases] == ['base_before_load','load_adapter','adapter_before_sleep',
                                      'sleep_level1','wake_level1','adapter_after_wake','unload_adapter']
    assert phases[-2][1] == run.adapter_name
    receipt = run.records[-1][1]
    assert receipt['adapter_request_after_wake'] and receipt['base_and_adapter_text_equal']
    assert not receipt['trained_quality_measured'] and not receipt['swap_routing_measured']


def test_list_only_retention_is_not_success():
    run = runner()
    run.measured = lambda *a, **kw: None
    def infer(phase, model):
        if phase == 'adapter_after_wake': raise smoke.lifecycle.SmokeError('request failed')
        return 'digest'
    run.inference = infer
    with pytest.raises(smoke.lifecycle.SmokeError): run.exercise()
    assert not any(kind == 'synthetic_retention' for kind, _ in run.records)


def test_budget_or_ownership_failure_prevents_phase_http():
    run = runner(); run.deadline = time.monotonic()+59
    run.http = lambda *a, **kw: pytest.fail('must not send')
    run.inventory = lambda **kw: pytest.fail('budget first')
    with pytest.raises(smoke.lifecycle.SmokeError, match='budget'): run.measured('load','/v1/load_lora_adapter',{})
    run.deadline = time.monotonic()+300
    run.inventory = lambda **kw: None
    def owner(): raise smoke.lifecycle.SmokeError('ownership lost')
    run.check_endpoint = owner
    with pytest.raises(smoke.lifecycle.SmokeError, match='ownership'): run.measured('load','/v1/load_lora_adapter',{})


def test_fixture_scope_and_lora_flags():
    run = runner(); run.temp='/tmp/ops-life-test';run.token='owned';run.config={'model_path':'/cached/base'}
    calls=[]
    run.python=lambda code,data,**kw: calls.append((code,data,kw)) or {'fixture':synthetic.LABEL}
    run.prepare_fixture()
    assert calls[0][1]['output'] == run.temp+'/adapter'
    assert calls[0][1]['root'] == run.temp and calls[0][1]['token'] == run.token
    assert run.extra_env() == {'VLLM_ALLOW_RUNTIME_LORA_UPDATING':'True'}
    assert run.extra_args() == ['--enable-lora','--max-loras','1','--max-cpu-loras','1','--max-lora-rank','8']


def test_harness_dry_run_creates_nothing(tmp_path, monkeypatch):
    cfg={'container':'test','lock_path':str(tmp_path/'lock'),'model_path':'/cached/base',
         'vllm_binary':'/test/vllm','source':str(DEPLOY.parent),'output_dir':str(tmp_path/'output'),
         'gpu':0,'wall_seconds':300}
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg))
    monkeypatch.setattr(smoke,'LoRARun',type('ForbiddenRun',(),{'scope':'test'}))
    assert smoke.main(['--config',str(path),'--dry-run']) == 0
    assert list(tmp_path.iterdir()) == [path]


def test_busy_preflight_never_prepares_or_starts_lora():
    run = runner(); run.source_hash='fixture';run.config={};run.attempted=False
    def busy(**kwargs): raise smoke.lifecycle.SmokeError('foreign process')
    run.inventory=busy
    run.prepare_fixture=lambda: pytest.fail('busy GPU must not create fixture')
    run.container=lambda *a, **kw: pytest.fail('busy GPU must not start a unit')
    cleaned=[];run.cleanup=lambda: cleaned.append(True)
    from types import SimpleNamespace
    run.command=lambda *a, **kw: SimpleNamespace(stdout='')
    run.python=lambda *a, **kw: {}
    assert run.execute() == 'failed'
    assert cleaned == [True]
    assert run.records[-1][1]['lora_measured'] is False


def test_adapter_list_checks_presence_and_inference_rejects_empty():
    run=runner()
    run.http=lambda *a, **kw: {'body':json.dumps({'data':[{'id':run.adapter_name}]})}
    run.adapter_listed = smoke.LoRARun.adapter_listed.__get__(run)
    run.adapter_listed(True)
    with pytest.raises(smoke.lifecycle.SmokeError): run.adapter_listed(False)
    run.measured=lambda *a, **kw: {'body':json.dumps({'choices':[{'message':{'content':''}}]})}
    with pytest.raises(smoke.lifecycle.SmokeError, match='empty'): run.inference('adapter_after_wake',run.adapter_name)
