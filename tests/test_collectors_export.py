# Generated-By: Codex / gpt-6-astra
import json

import pytest

from llmsvc.collectors.export import export_snapshot, main
from llmsvc.state import Activity, GPUProcess, GPUState, ModelState, Pin, StateSnapshot


def source_snapshot():
    return StateSnapshot(sampled_at=100,
        models=(ModelState('/private/model-name', state='sleeping', unit='private-unit', gpu=0, budget_gb=50, is_default=True),),
        gpus=(GPUState(0, uuid='sensitive-uuid', total_gb=100, used_gb=40, external_gb=20,
                       external_processes=(GPUProcess(9191, used_gb=20, name='/home/private/executable', user='192.0.2.99'),)),),
        activity=(Activity('/private/model-name', requests_last_hour=4, in_flight=0, by=('192.0.2.99',)),),
        pins=(Pin('/private/model-name', 500, '192.0.2.99'),),
        errors=('database /private/path unavailable token=secret',)).to_dict()


def test_sanitizer_preserves_policy_inputs_and_referential_identity_without_secrets():
    source = source_snapshot()
    source['unknown_future_field'] = {'token': 'secret'}
    data = export_snapshot(source)
    text = json.dumps(data)
    for secret in ('private', '192.0.2.99', 'secret', '9191', 'sensitive-uuid'):
        assert secret not in text
    s = data['snapshot']
    assert s['models'][0]['name'] == s['activity'][0]['model'] == s['pins'][0]['model']
    assert s['pins'][0]['by'] == s['activity'][0]['by'][0] == s['gpus'][0]['external_processes'][0]['user']
    assert s['models'][0]['is_default'] is True
    assert s['models'][0]['budget_gb'] == 50
    assert s['gpus'][0]['external_gb'] == 20
    assert s['activity'][0]['in_flight'] == 0
    assert s['models'][0]['health_ok'] is None
    assert data['expected_actions'] is None
    assert source['models'][0]['name'] == '/private/model-name'


def test_journal_exports_only_known_action_fields_and_counts_dropped_records():
    lines = [
        json.dumps({'__REALTIME_TIMESTAMP': '2000000', 'MESSAGE': json.dumps({'kind': 'sleep', 'model': '/private/model-name', 'gpu': 0, 'token': 'secret'})}),
        json.dumps({'__REALTIME_TIMESTAMP': '3000000', 'MESSAGE': 'start /private/model-name on GPU 0 token=secret'}),
        json.dumps({'MESSAGE': 'Authorization: secret private request text'}),
        '{invalid',
    ]
    result = export_snapshot(source_snapshot(), lines)
    assert result['journal_dropped'] == 2
    assert [e['kind'] for e in result['journal_events']] == ['sleep', 'start']
    assert result['journal_events'][0] == {'timestamp': 2, 'kind': 'sleep', 'model': 'model-1', 'gpu': 0}
    assert 'secret' not in json.dumps(result)


def test_dry_run_makes_no_output_or_temporary_file_changes(tmp_path):
    source = tmp_path / 'state.json'
    source.write_text(json.dumps(source_snapshot()))
    output = tmp_path / 'output.json'
    output.write_text('existing')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert main(['--state', str(source), '--output', str(output), '--dry-run']) == 0
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert main(['--state', str(source), '--output', str(output)]) == 0
    assert json.loads(output.read_text())['expected_actions'] is None
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_atomic_replace_preserves_existing_output_and_cleans_temp(tmp_path, monkeypatch):
    source = tmp_path / 'state.json'
    source.write_text(json.dumps(source_snapshot()))
    output = tmp_path / 'output.json'
    output.write_text('existing')
    def failed_replace(*args):
        raise PermissionError('test')
    monkeypatch.setattr('llmsvc.collectors.export.os.replace', failed_replace)
    with pytest.raises(PermissionError):
        main(['--state', str(source), '--output', str(output)])
    assert output.read_text() == 'existing'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['output.json', 'state.json']


def test_bad_schema_is_rejected():
    with pytest.raises(ValueError):
        export_snapshot({'schema_version': 2})


def test_real_sanitized_capture_retains_provenance_and_dropped_line_count():
    from pathlib import Path
    path = Path(__file__).parent / 'fixtures/telemetry/snapshot-real.json'
    data = json.loads(path.read_text())
    assert len(data['snapshot']['models']) == 7
    assert len(data['journal_events']) + data['journal_dropped'] == 100
    assert data['capture']['not_contemporaneous'] is True
    assert data['expected_actions'] is None
    assert data['snapshot']['memory']['host_available_gb'] is None
