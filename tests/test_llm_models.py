# Generated-By: Codex / gpt-6-astra
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Model listing client wire and discovery validation; the write CLI is gone."""
import io
import json
import pytest
from test_llm_events import api


def args(api, *words):
    return api['build_parser']().parse_args(list(words))


def test_models_wire_payload_and_json_shape(api):
    reply = {'records': {}, 'writes_enabled': False, 'blocked_by': [{'reason': 'registry_writes_disabled'}],
             'discovered': []}
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        return io.BytesIO(json.dumps(reply).encode())
    parsed = args(api, 'models', '--json')
    result = api['execute_command'](parsed, api['SchedulerClient']('http://fixture.invalid', opener=opener))
    assert result == reply and len(calls) == 1
    assert calls[0].full_url == 'http://fixture.invalid/v1/models'
    assert calls[0].get_method() == 'GET'
    assert calls[0].data is None


@pytest.mark.parametrize('words', [
    ['add', '/x', '--name', 'n', '--base', 'b'], ['rm', 'ft'], ['import', 'x']])
def test_removed_write_commands_rejected_without_http(api, words):
    with pytest.raises(SystemExit):
        args(api, *words)


def test_listing_keeps_temporary_records_and_readiness_separate(api):
    parsed = args(api, 'models')
    reply = {'records': {'ft': {'name': 'ft', 'base': 'base', 'path': '/shared/ft', 'kind': 'full_weight'}},
             'writes_enabled': False, 'blocked_by': [{'reason': 'registry_writes_disabled'},
                                                      {'reason': 'inflight_stream_unknown'}]}
    assert api['validate_registry_result'](parsed, reply) == reply
    assert api['result_exit_code'](parsed, reply) == 0  # A successful read is not write readiness.
    text = api['format_result'](parsed, reply)
    assert 'not active adoption' in text and 'writes enabled: no' in text
    assert 'inflight_stream_unknown' in text and '/shared/ft' in text


@pytest.mark.parametrize('status', ['pending', 'configured', 'invalid', 'orphaned'])
def test_discovered_statuses_are_accepted(api, status):
    parsed = args(api, 'models')
    reply = {'records': {}, 'writes_enabled': False, 'blocked_by': [],
             'discovered': [{'name': 'a', 'path': '/srv/a', 'base': None, 'util': None,
                             'weights_gb': None, 'status': status, 'reason': None}]}
    assert api['validate_registry_result'](parsed, reply) == reply


def test_discovered_concurrency_fields_are_optional(api):
    parsed = args(api, 'models')
    reply = {'records': {}, 'writes_enabled': False, 'blocked_by': [],
             'discovered': [
                 {'name': 'a', 'path': '/srv/a', 'base': None, 'util': None, 'weights_gb': None,
                  'concurrency_limit': 32, 'concurrency_queue': 8, 'status': 'pending', 'reason': None},
                 {'name': 'b', 'path': '/srv/b', 'base': None, 'util': None, 'weights_gb': None,
                  'status': 'pending', 'reason': None}]}
    assert api['validate_registry_result'](parsed, reply) == reply


def test_inventory_concurrency_fields_are_optional(api):
    parsed = args(api, 'models')
    row = {'name': 'a', 'source': 'config', 'temporary': False, 'base': None, 'daemon_port': None,
           'created_at': None, 'last_used_at': None, 'runtime_state': 'stopped', 'removable': False,
           'blocked_by': []}
    reply = {'records': {}, 'writes_enabled': False, 'blocked_by': [], 'discovered': None,
             'inventory': {'config_sha256': 'a' * 64, 'fenced': False, 'recovery': {},
                           'pending_changes': [],
                           'models': [dict(row, concurrency_limit=32, concurrency_queue=8),
                                      dict(row, name='b')]}}
    assert api['validate_registry_result'](parsed, reply) == reply


def test_unknown_discovered_status_is_rejected(api):
    parsed = args(api, 'models')
    reply = {'records': {}, 'writes_enabled': False, 'blocked_by': [],
             'discovered': [{'name': 'a', 'path': '/srv/a', 'base': None, 'util': None,
                             'weights_gb': None, 'status': 'maybe', 'reason': None}]}
    with pytest.raises(api['ClientError'], match='Invalid discovered model list'):
        api['validate_registry_result'](parsed, reply)


def test_empty_discovery_reports_nothing_new(api):
    parsed = args(api, 'models')
    reply = {'records': {}, 'writes_enabled': False, 'blocked_by': [], 'discovered': []}
    assert 'Nothing new under the shared roots' in api['format_result'](parsed, reply)


@pytest.mark.parametrize('code,error', [(400, 'registry_invalid_request'), (503, 'registry_not_configured'),
                                        (409, 'registry_reconciliation_required')])
def test_model_http_error_json_preserves_reason_and_message(api, monkeypatch, capsys, code, error):
    from urllib.error import HTTPError
    body = {'error': error, 'message': 'owner validation detail'}
    def opener(request, **kwargs):
        raise HTTPError(request.full_url, code, error, {}, io.BytesIO(json.dumps(body).encode()))
    client = api['SchedulerClient']('http://fixture.invalid', opener=opener)
    monkeypatch.setitem(api['main'].__globals__, 'SchedulerClient', lambda **kwargs: client)
    result = api['main'](['--url', 'http://fixture.invalid', 'models', '--json'])
    assert result == 1 and json.loads(capsys.readouterr().out) == body


from test_registry_api import registry


def test_canonical_models_read_reaches_real_registry_without_files_or_queue_changes(api, registry):
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    owner, queue, weights, _, _, calls, _, _ = registry
    before = (queue.path.read_bytes(), set(queue.path.parent.iterdir()), len(queue._pending),
              dict(queue._jobs), list(calls))
    def read(method, path, payload=None):
        assert method == 'GET' and urlsplit(path).path == '/v1/models'
        result = owner.inventory(include_records=True)
        records = result.pop('records')
        return {'records': records, 'inventory': result, 'writes_enabled': False,
                'discovered': owner.discovered(), 'blocked_by': [{'reason': 'registry_writes_disabled'}]}
    parsed = args(api, 'models')
    result = api['execute_command'](parsed, SimpleNamespace(request=read))
    assert result['records'] == {}
    assert (queue.path.read_bytes(), set(queue.path.parent.iterdir()), len(queue._pending),
            dict(queue._jobs), list(calls)) == before
