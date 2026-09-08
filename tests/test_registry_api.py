# Generated-By: Codex / gpt-6-astra
"""Registry/queue contract with staged YAML and mocked adoption/unit callbacks."""
import json
from dataclasses import replace
from pathlib import Path
import threading

import pytest
import yaml

from llmsvc.registry import ModelRegistry, RegistryError
from llmsvc.reload import QuietPeriod, ReloadQueue
from llmsvc.state import Activity, MemoryState, ModelState, StateSnapshot


@pytest.fixture
def registry(tmp_path):
    clock = [1000.0]
    now = lambda: clock[0]
    shared = tmp_path / 'models'
    weights = shared / 'candidate'
    weights.mkdir(parents=True)
    (weights / 'config.json').write_text('{"architectures": ["Test"]}')
    (weights / 'model.safetensors').write_bytes(b'fake test weights')
    config = {
        'macros': {'wrapper': '/test/wrapper', 'launch': '/test/launch', 'vllm': '/test/vllm',
                   'common': '--served-model-name ${MODEL_ID} --enable-sleep-mode'},
        'models': {'base': {'macros': {'util': '.3'}, 'useModelName': 'base', 'aliases': ['default'],
                           'cmd': '${wrapper} serve --vllm-url http://localhost:8101 --listen :${PORT} --journal-unit vllm-${MODEL_ID}.service -- ${launch} ${util} vllm-${MODEL_ID} -- ${vllm} serve /test/base ${common} --port 8101',
                           'cmdStop': '${wrapper} sleep --vllm-url http://localhost:8101 --stop-pid ${PID}'}},
        'groups': {'pool': {'swap': False, 'exclusive': False, 'members': ['base']}},
    }
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    state = [StateSnapshot(sampled_at=now(), models=(ModelState('base', state='stopped', is_default=True),),
                           activity=(Activity('base', last_request_at=0, in_flight=0),),
                           memory=MemoryState(500, 0), read_only=False)]
    quiet = QuietPeriod(now)
    calls = []
    units = set()
    queue = ReloadQueue(path, action_lock=threading.RLock(), quiet=quiet, snapshot=lambda: replace(state[0], sampled_at=now()),
                        validate=lambda p: yaml.safe_load(p.read_text()), notify_reload=lambda **kwargs: calls.append('reload'),
                        log=lambda event: None, clock=now, wall_clock=now)
    def stop(name, *, deadline):
        calls.append(('stop', name))
        units.discard(name)
    api = ModelRegistry(queue, shared_roots=(shared,), daemon_port_range=(8101, 8110),
                        reserved_ports=lambda: (), stop_model=stop, unit_absent=lambda name, **kwargs: name not in units, now=now)
    def drain():
        quiet.observe(0)
        for _ in range(5):
            clock[0] += 1
            quiet.heartbeat()
        return queue.process_once()
    return api, queue, weights, state, clock, calls, units, drain


def test_add_dry_run_then_apply_persists_records_and_groups(registry):
    api, queue, weights, state, _, calls, _, drain = registry
    initial = queue.path.read_bytes()
    payload = {'name': 'fine', 'path': str(weights), 'base': 'base'}
    assert api.handle('POST', '/v1/models', payload, dry_run=True)['would']
    assert queue.path.read_bytes() == initial and calls == []
    job = api.handle('POST', '/v1/models', payload)
    assert job['status'] == 'queued'
    assert api.records() == {}
    assert drain()['status'] == 'applied'
    assert api.records()['fine']['base'] == 'base'
    saved = yaml.safe_load(queue.path.read_text())
    assert saved['groups']['pool']['members'] == ['base', 'fine']
    assert saved['models']['fine']['useModelName'] == 'fine'
    assert saved['models']['fine']['metadata']['llmsvc_registry']['daemon_port'] == 8102
    assert api.records() == ModelRegistry(queue, shared_roots=(), daemon_port_range=(8101, 8110)).records()


def test_remove_waits_for_adoption_then_cleans_only_target(registry):
    api, queue, weights, state, clock, calls, units, drain = registry
    api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='sleeping', weights_gb=10),),
                       activity=state[0].activity + (Activity('fine', last_request_at=clock[0], in_flight=0),))
    units.add('fine')
    calls.clear()
    assert api.remove('fine', dry_run=True)['would']
    assert units == {'fine'} and calls == []
    api.remove('fine')
    assert 'fine' in api.records()
    assert drain()['status'] == 'applied'
    assert calls == ['reload', ('stop', 'fine')]
    assert api.records() == {} and not units


def test_busy_remove_and_lora_are_rejected(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    with pytest.raises(RegistryError, match='LoRA'):
        api.add({'name': 'fine', 'path': str(weights), 'base': 'base', 'lora': True})
    api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='awake', weights_gb=10),),
                       activity=state[0].activity + (Activity('fine', last_request_at=clock[0], in_flight=1),))
    with pytest.raises(RegistryError, match='in_flight'):
        api.remove('fine')
    assert 'fine' in api.records()


def test_two_queued_adds_reserve_different_ports(registry):
    api, queue, weights, _, _, _, _, drain = registry
    for name in ('one', 'two'):
        api.add({'name': name, 'path': str(weights), 'base': 'base'})
    assert drain()['status'] == 'applied'
    assert drain()['status'] == 'applied'
    assert {r['daemon_port'] for r in api.records().values()} == {8102, 8103}


def test_expiry_rechecks_activity_before_apply(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='stopped'),),
                       activity=state[0].activity + (Activity('fine', last_request_at=1000, in_flight=0),))
    clock[0] += 8 * 86400
    assert len(api.expire()) == 1
    state[0] = replace(state[0], activity=(state[0].activity[0], Activity('fine', last_request_at=clock[0], in_flight=0)))
    result = drain()
    assert result['status'] == 'queued'
    assert any(item['reason'] == 'recent_activity' for item in result['blocked_by'])
    assert 'fine' in api.records()


def test_known_unused_model_expires_but_missing_history_does_not(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='stopped'),),
                       activity=state[0].activity + (Activity('fine', in_flight=0),))
    clock[0] += 8 * 86400
    assert api.expire() == []
    state[0] = replace(state[0], activity=(state[0].activity[0], Activity('fine', in_flight=0,
                                                requests_last_hour=0, requests_last_10m=0)))
    assert len(api.expire()) == 1
    assert drain()['status'] == 'applied'


def test_nested_filter_alias_collision_and_strip(registry):
    api, queue, weights, _, _, _, _, drain = registry
    config = yaml.safe_load(queue.path.read_text())
    config['models']['base']['filters'] = {'setParamsByID': {'variant': {'temperature': .2}}, 'setParams': {'temperature': .5}}
    queue.path.write_text(yaml.safe_dump(config))
    with pytest.raises(RegistryError, match='exists'):
        api.add({'name': 'variant', 'path': str(weights), 'base': 'base'})
    api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    drain()
    added = yaml.safe_load(queue.path.read_text())['models']['fine']
    assert added['filters'] == {'setParams': {'temperature': .5}}


def test_unknown_launcher_identity_cannot_reuse_existing_unit(registry):
    api, queue, weights, _, _, _, _, _ = registry
    config = yaml.safe_load(queue.path.read_text())
    config['models']['base']['cmd'] = config['models']['base']['cmd'].replace('${util} vllm-${MODEL_ID}', '${util} vllm-unrelated')
    queue.path.write_text(yaml.safe_dump(config))
    with pytest.raises(RegistryError, match='launcher'):
        api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    assert not queue._pending
