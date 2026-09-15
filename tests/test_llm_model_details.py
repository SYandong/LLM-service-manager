# Generated-By: Codex / gpt-6-astra
# Generated-By: OpenCode / deepseek-v4.1-flash
"""#152 client metadata consumption uses the real registry owner, never a planner clone."""
import copy
import json
from dataclasses import replace
import pytest
from llmsvc.state import Activity, ModelState, Pin
from test_registry_api import registry
from test_llm_events import api


def args(api, *words):
    return api['build_parser']().parse_args(list(words))


def inventory_reply(owner):
    return {'records': owner.records(), 'writes_enabled': False,
            'blocked_by': [{'reason': 'registry_writes_disabled'}, {'reason': 'inflight_stream_unknown'}],
            'inventory': owner.inventory()}


def test_owner_inventory_distinguishes_permanent_configuration_and_unknown_runtime(api, registry):
    owner, queue, _, state, clock, calls, _, _ = registry
    queue.snapshot = lambda: replace(state[0], sampled_at=clock[0]-31)
    before = queue.path.read_bytes(), list(calls), queue.queue_snapshot()
    result = inventory_reply(owner)
    original = copy.deepcopy(result)
    parsed = args(api, 'models')
    api['validate_registry_result'](parsed, result)
    text = api['format_result'](parsed, result)
    assert '[permanent; source=config]' in text and 'observed=unknown' in text
    assert 'model eligibility is not global commit readiness' in text
    assert result['inventory']['models'][0]['temporary'] is False and result['records'] == {}
    assert result == original and (queue.path.read_bytes(), calls, queue.queue_snapshot()) == before
    parsed.json = True
    assert json.loads(api['format_result'](parsed, result)) == original


def test_temporary_inventory_and_pin_protection_preserve_global_blockers(api, registry):
    owner, queue, weights, state, clock, calls, _, drain = registry
    owner.add({'name': 'ft', 'path': str(weights), 'base': 'base'})
    drain()  # Fixture-only setup; all callbacks are synthetic.
    state[0] = replace(state[0], models=state[0].models + (ModelState('ft', state='stopped'),),
                       activity=state[0].activity + (Activity('ft', last_request_at=clock[0], in_flight=0),),
                       pins=(Pin('ft', clock[0]+3600, 'owner'),))
    before = queue.path.read_bytes(), list(calls)
    result = inventory_reply(owner)
    parsed = args(api, 'models')
    api['validate_registry_result'](parsed, result)
    row = next(row for row in result['inventory']['models'] if row['name'] == 'ft')
    assert row['removable'] is False
    text = api['format_result'](parsed, result)
    assert '[temporary; source=config]' in text and 'protection=pinned' in text
    assert 'inflight_stream_unknown' in text
    assert (queue.path.read_bytes(), calls) == before


def test_basic_list_responses_remain_compatible_without_optional_details(api):
    listed = {'records': {}, 'writes_enabled': False, 'blocked_by': []}
    assert api['validate_registry_result'](args(api, 'models'), listed) == listed


@pytest.mark.parametrize('change', [{'inventory': None}, {'inventory': {}}, {'inventory': {'models': 'bad'}}])
def test_malformed_supplied_inventory_is_explicit_not_an_empty_list(api, change):
    result = {'records': {}, 'writes_enabled': False, 'blocked_by': [], **change}
    with pytest.raises(api['ClientError'], match='optional registry inventory'):
        api['validate_registry_result'](args(api, 'models'), result)


@pytest.mark.parametrize('change', [{'created_at': True}, {'temporary': None},
    {'removable': 'yes'}, {'source': 'runtime'}, {'runtime_state': None}, {'cmd': 'PRIVATE_MARKER'}])
def test_invalid_inventory_row_is_not_given_configuration_or_runtime_authority(api, registry, change):
    result = inventory_reply(registry[0])
    result['inventory']['models'][0].update(change)
    with pytest.raises(api['ClientError'], match='optional registry inventory'):
        api['validate_registry_result'](args(api, 'models'), result)
