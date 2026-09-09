# Generated-By: Codex / gpt-6-astra
"""Nonmutating inventory and projected registration previews."""
from dataclasses import replace
import hashlib

from llmsvc.state import Activity, ModelState, Pin
from test_registry_api import registry as registry


def payload(weights, name='fine'):
    return {'name': name, 'path': str(weights), 'base': 'base'}


def test_add_preview_respects_pending_ports_without_reservation(registry):
    api, queue, weights, _, _, calls, _, drain = registry
    original = queue.path.read_bytes()
    first = api.add(payload(weights, 'pending'))
    before_calls = list(calls)
    preview = api.preview_add(payload(weights))
    assert preview['would'][0]['model'] == 'fine'
    assert preview['model']['daemon_port'] == 8103
    assert preview['model']['base'] == 'base'
    assert preview['model']['util_macro'] == '.3'
    assert preview['port_reserved'] is False and preview['config_written'] is False
    assert preview['candidate_sha256'] != hashlib.sha256(original).hexdigest()
    assert queue.path.read_bytes() == original and calls == before_calls
    assert queue.queue_snapshot()['pending_ids'] == [first['id']]
    assert api.preview_add(payload(weights))['model']['daemon_port'] == 8103


def test_inventory_separates_file_configuration_runtime_and_pending(registry):
    api, queue, weights, _, _, calls, _, _ = registry
    api.add(payload(weights))
    before = queue.path.read_bytes(), list(calls)
    view = api.inventory()
    assert [row['name'] for row in view['models']] == ['base']
    assert view['models'][0]['source'] == 'config'
    assert view['models'][0]['temporary'] is False
    assert view['models'][0]['runtime_state'] == 'stopped'
    assert view['pending_changes'][0]['description']['model'] == 'fine'
    assert view['pending_changes'][0]['status'] == 'blocked'
    assert view['fenced'] is False
    assert (queue.path.read_bytes(), calls) == before
    view['models'][0]['name'] = 'tampered'
    assert api.inventory()['models'][0]['name'] == 'base'


def test_temporary_inventory_expiry_and_protection(registry):
    api, queue, weights, state, clock, calls, _, drain = registry
    api.add(payload(weights))
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='stopped'),),
                       activity=state[0].activity + (Activity('fine', last_request_at=clock[0], in_flight=0),))
    item = next(row for row in api.inventory()['models'] if row['name'] == 'fine')
    assert item['expires_at'] == clock[0] + 7 * 86400
    assert item['removable'] is True and item['temporary']
    state[0] = replace(state[0], pins=(Pin('fine', clock[0]+3600, 'fixture'),))
    item = next(row for row in api.inventory()['models'] if row['name'] == 'fine')
    assert not item['removable'] and item['blocked_by'][0]['reason'] == 'pinned'
    assert 'cmd' not in item and 'cmdStop' not in item


def test_remove_preview_reports_protection_and_never_cleans_units(registry):
    api, queue, weights, state, clock, calls, units, drain = registry
    api.add(payload(weights))
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='sleeping', weights_gb=10),),
                       activity=state[0].activity + (Activity('fine', last_request_at=clock[0], in_flight=0),))
    units.add('fine')
    original, old_calls = queue.path.read_bytes(), list(calls)
    preview = api.preview_remove('fine')
    assert preview['would'][0]['kind'] == 'remove_model'
    assert preview['candidate_sha256'] != hashlib.sha256(original).hexdigest()
    assert preview['blocked_by'] == [] and preview['config_written'] is False
    assert queue.path.read_bytes() == original and calls == old_calls and units == {'fine'}
    assert queue.queue_snapshot()['pending_ids'] == []
    state[0] = replace(state[0], pins=(Pin('fine', clock[0]+3600, 'fixture'),))
    blocked = api.preview_remove('fine')
    assert blocked['would'] == [] and blocked['candidate_sha256'] is None
    assert blocked['blocked_by'][0]['reason'] == 'pinned'
    assert queue.path.read_bytes() == original and calls == old_calls


def test_inventory_unknown_activity_does_not_fabricate_expiry(registry):
    api, _, weights, state, _, _, _, drain = registry
    api.add(payload(weights))
    drain()
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='unknown'),))
    item = next(row for row in api.inventory()['models'] if row['name'] == 'fine')
    assert item['runtime_state'] == 'unknown' and item['expires_at'] is None
    assert item['removable'] is False


def test_inventory_still_shows_recovery_fence(registry):
    api, queue, weights, _, _, _, _, drain = registry
    queue.notify_reload = lambda **kw: (_ for _ in ()).throw(RuntimeError('unknown settlement'))
    api.add(payload(weights))
    assert drain()['status'] == 'reconciliation_required'
    view = api.inventory()
    assert view['fenced'] and view['recovery']['status'] == 'reconciliation_required'
    fine = next(row for row in view['models'] if row['name'] == 'fine')
    assert fine['source'] == 'config' and fine['runtime_state'] == 'unknown'


def test_unsupported_layout_preview_rejects_without_mutation(registry):
    import json
    import pytest
    import yaml
    from llmsvc.registry import RegistryError
    api, queue, weights, _, _, calls, _, _ = registry
    queue.path.write_text(json.dumps(yaml.safe_load(queue.path.read_bytes())) + '\n')
    original = queue.path.read_bytes()
    with pytest.raises(RegistryError, match='layout'):
        api.preview_add(payload(weights))
    assert queue.path.read_bytes() == original and calls == []
    assert queue.queue_snapshot()['pending_ids'] == []


def test_stale_inventory_keeps_runtime_unknown(registry):
    api, queue, _, state, clock, _, _, _ = registry
    queue.snapshot = lambda: replace(state[0], sampled_at=clock[0]-31)
    assert api.inventory()['models'][0]['runtime_state'] == 'unknown'


def test_recovery_fence_refuses_add_preview_without_more_work(registry):
    import pytest
    from llmsvc.reload import ReloadError
    api, queue, weights, _, _, calls, _, drain = registry
    queue.notify_reload = lambda **kw: (_ for _ in ()).throw(RuntimeError('unknown'))
    api.add(payload(weights))
    drain()
    before = queue.path.read_bytes(), queue.marker.read_bytes(), list(calls)
    with pytest.raises(ReloadError, match='reconciliation'):
        api.preview_add(payload(weights, 'other'))
    assert (queue.path.read_bytes(), queue.marker.read_bytes(), calls) == before
