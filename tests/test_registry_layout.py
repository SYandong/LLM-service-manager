# Generated-By: Codex / gpt-6-astra
"""Byte-preserving registry edits of administrator-maintained YAML."""
from dataclasses import replace
import json

import pytest
import yaml

from llmsvc.registry import RegistryError
from llmsvc.state import Activity, ModelState
from test_registry_api import registry as registry


def admin_config(queue, *, flow_members=True, newline='\n'):
    old = yaml.safe_load(queue.path.read_bytes())
    base = old['models']['base']
    members = '    members: [base] # membership note\n' if flow_members else '    members: # membership note\n      - base # resident membership\n'
    text = (
        '# 管理员配置 — preserve spelling and whitespace\n'
        'macros: &launch_macros\n'
        + ''.join('  ' + k + ': ' + json.dumps(v) + '\n' for k, v in old['macros'].items())
        + '\n# model catalogue\n'
        'models: # keep this header\n'
        '  # the resident model documentation\n'
        '  base:\n'
        '    aliases: [default] # default alias\n'
        '    macros: {util: ".3"}\n'
        '    useModelName: base\n'
        '    cmd: >-\n      ' + base['cmd'] + '\n'
        '    cmdStop: ' + json.dumps(base['cmdStop']) + '\n'
        '\n# group documentation\n'
        'groups:\n'
        '  pool:\n'
        '    swap: false\n'
        '    exclusive: false\n' + members
        + '\n# unrelated aliases, quotes, flow layout and end comments\n'
        'extra: &layout {first: "001", second: [1, 2]} # keep spacing\n'
        'extra_copy: *layout\n'
        'macro_copy: *launch_macros\n'
        '# final comment\n'
    )
    data = text.replace('\n', newline).encode()
    queue.path.write_bytes(data)
    return data


def add_payload(weights):
    return {'name': 'fine', 'path': str(weights), 'base': 'base'}


def add_stopped_state(state, clock):
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='stopped'),),
                       activity=state[0].activity + (Activity('fine', last_request_at=clock[0], in_flight=0),))


@pytest.mark.parametrize('flow_members', [True, False])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_add_then_remove_preserves_admin_bytes(registry, flow_members, newline):
    api, queue, weights, state, clock, _, _, drain = registry
    original = admin_config(queue, flow_members=flow_members, newline=newline)
    api.add(add_payload(weights))
    assert drain()['status'] == 'applied'
    changed = queue.path.read_bytes()
    prefix = original.split(b'models:')[0]
    resident = original.split(b'  # the resident')[1].split(b'groups:')[0]
    tail = original.split(b'# unrelated')[1]
    assert changed.startswith(prefix)
    assert b'  # the resident' + resident in changed
    assert changed.endswith(b'# unrelated' + tail)
    assert yaml.safe_load(changed)['groups']['pool']['members'] == ['base', 'fine']
    if newline == '\r\n':
        assert b'\n' not in changed.replace(b'\r\n', b'')
    add_stopped_state(state, clock)
    api.remove('fine')
    assert drain()['status'] == 'applied'
    assert queue.path.read_bytes() == original


def test_expiry_uses_local_deletion_and_preserves_comments(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    original = admin_config(queue, flow_members=False)
    api.add(add_payload(weights))
    drain()
    add_stopped_state(state, clock)
    clock[0] += 8 * 86400
    assert len(api.expire()) == 1
    assert drain()['status'] == 'applied'
    assert queue.path.read_bytes() == original


@pytest.mark.parametrize('layout', ['flow_models', 'flow_root', 'duplicate_models', 'merged_models', 'aliased_members', 'multiline_flow_members', 'no_final_newline'])
def test_unsupported_layout_rejected_before_validation_or_write(registry, layout):
    api, queue, weights, _, _, calls, _, _ = registry
    original = admin_config(queue)
    config = yaml.safe_load(original)
    if layout == 'flow_models':
        config['models'] = yaml.safe_load(original)['models']
        original = ('models: ' + json.dumps(config['models']) + '\nmacros: ' + json.dumps(config['macros']) + '\n').encode()
    elif layout == 'flow_root':
        original = (json.dumps(config) + '\n').encode()
    elif layout == 'duplicate_models':
        original = b'models: {}\n' + original
    elif layout == 'merged_models':
        original = original.replace(b'  base:\n', b'  <<: {}\n  base:\n')
    elif layout == 'aliased_members':
        original = b'pool_members: &members [base]\n' + original.replace(b'members: [base]', b'members: *members')
    elif layout == 'multiline_flow_members':
        original = original.replace(b'members: [base]', b'members: [\n      base\n    ]')
    elif layout == 'no_final_newline':
        original = original.rstrip(b'\n')
    queue.path.write_bytes(original)
    before = {p: p.read_bytes() for p in queue.path.parent.iterdir() if p.is_file()}
    queue.validate = lambda path: pytest.fail('unsupported layout reached validator')
    with pytest.raises(RegistryError, match='layout'):
        api.add(add_payload(weights))
    assert queue.path.read_bytes() == original and not queue._pending and not calls
    assert {p: p.read_bytes() for p in queue.path.parent.iterdir() if p.is_file()} == before


def test_anchored_temporary_model_referenced_elsewhere_cannot_be_removed(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    admin_config(queue)
    api.add(add_payload(weights))
    drain()
    add_stopped_state(state, clock)
    anchored = queue.path.read_bytes().replace(b'  fine:\n', b'  fine: &retained\n') + b'other_reference: *retained\n'
    queue.path.write_bytes(anchored)
    with pytest.raises(RegistryError, match='layout'):
        api.remove('fine')
    assert queue.path.read_bytes() == anchored
    assert not queue._pending


def test_resident_model_anchor_and_alias_survive_add_rm(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    original = admin_config(queue).replace(b'  base:\n', b'  base: &resident_config\n')
    original = original.replace(b'\n# group documentation', b'  mirror: *resident_config # preserved alias\n\n# group documentation')
    queue.path.write_bytes(original)
    api.add(add_payload(weights))
    assert drain()['status'] == 'applied'
    changed = queue.path.read_bytes()
    assert b'base: &resident_config' in changed
    assert b'mirror: *resident_config # preserved alias' in changed
    add_stopped_state(state, clock)
    state[0] = replace(state[0], models=state[0].models + (ModelState('mirror', state='stopped'),),
                       activity=state[0].activity + (Activity('mirror', last_request_at=0, in_flight=0),))
    api.remove('fine')
    assert drain()['status'] == 'applied'
    assert queue.path.read_bytes() == original


def test_supported_dry_run_keeps_file_and_queue_unchanged(registry):
    api, queue, weights, _, _, calls, _, _ = registry
    original = admin_config(queue)
    before = {p: p.read_bytes() for p in queue.path.parent.iterdir() if p.is_file()}
    assert api.add(add_payload(weights), dry_run=True)['would']
    assert queue.path.read_bytes() == original and not queue._pending and not calls
    assert {p: p.read_bytes() for p in queue.path.parent.iterdir() if p.is_file()} == before


def test_layout_revalidated_when_a_queued_change_is_applied(registry):
    api, queue, weights, _, _, calls, _, drain = registry
    admin_config(queue)
    api.add(add_payload(weights))
    external = (json.dumps(yaml.safe_load(queue.path.read_bytes())) + '\n').encode()
    queue.path.write_bytes(external)
    result = drain()
    assert result['status'] == 'failed' and not result['config_committed']
    assert queue.path.read_bytes() == external and not calls
    assert not queue.marker.exists()


def test_removal_keeps_adjacent_model_comment_lines(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    original = admin_config(queue)
    api.add(add_payload(weights))
    drain()
    # Comments inserted around a temporary block after registration belong to
    # the administrator and must not be consumed via MappingNode.end_mark.
    changed = queue.path.read_bytes().replace(b'  fine:\n', b'  # before target\n  fine:\n')
    changed = changed.replace(b'  # the resident', b'  # after target\n  # the resident')
    queue.path.write_bytes(changed)
    add_stopped_state(state, clock)
    api.remove('fine')
    assert drain()['status'] == 'applied'
    expected = original.replace(b'  # the resident', b'  # before target\n  # after target\n  # the resident')
    assert queue.path.read_bytes() == expected


def test_recursive_model_metadata_rejected_before_staging(registry):
    api, queue, weights, _, _, calls, _, _ = registry
    original = admin_config(queue).replace(b'    aliases:', b'    extra: &recursive [*recursive]\n    aliases:')
    queue.path.write_bytes(original)
    queue.validate = lambda path: pytest.fail('recursive layout reached validator')
    with pytest.raises(RegistryError, match='layout'):
        api.add(add_payload(weights))
    assert queue.path.read_bytes() == original and not queue._pending and not calls


def test_anchored_models_header_preserved(registry):
    api, queue, weights, state, clock, _, _, drain = registry
    original = admin_config(queue).replace(b'models: #', b'models: &catalogue #')
    queue.path.write_bytes(original)
    api.add(add_payload(weights))
    assert drain()['status'] == 'applied'
    assert b'models: &catalogue # keep this header' in queue.path.read_bytes()
    add_stopped_state(state, clock)
    api.remove('fine')
    assert drain()['status'] == 'applied'
    assert queue.path.read_bytes() == original
