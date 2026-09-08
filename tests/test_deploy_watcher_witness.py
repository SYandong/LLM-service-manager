# Generated-By: Codex / gpt-6-astra
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest
import yaml

from deploy import watcher_witness as witness

GENERATION = 'gen_' + 'a' * 32


def envelope(generation=GENERATION):
    return {'jsonrpc': '2.0', 'id': 'request', 'result': {'content': [{'type': 'text', 'text':
        'Current llama-swap configuration at "macros.llmsvc_reload_generation" (credentials redacted, values resolved):\n\n```yaml\n' + generation + '\n```\n'}]}}


def test_exact_native_scalar_is_accepted():
    assert witness.parse_generation(envelope(), 'request') == GENERATION


@pytest.mark.parametrize('mutate', [
    lambda e: e.update(id='replayed-id'),
    lambda e: e.update(error={'code': -32022}),
    lambda e: e['result'].update(isError=True),
    lambda e: e['result'].update(isError=1),
    lambda e: e['result'].update(content=[]),
    lambda e: e['result']['content'].append(copy.deepcopy(e['result']['content'][0])),
    lambda e: e['result']['content'][0].update(type='resource'),
    lambda e: e['result']['content'][0].update(text='```yaml\n' + GENERATION + '\n```'),
    lambda e: e['result']['content'][0].update(text=e['result']['content'][0]['text'][:-4]),
])
def test_http_200_errors_replay_and_partial_envelopes_are_not_witnesses(mutate):
    value = envelope()
    mutate(value)
    with pytest.raises(witness.HarnessError):
        witness.parse_generation(value, 'request')


@pytest.mark.parametrize('value', ['null', '"[REDACTED]"', '123', '{key: value}', '!!python/object/apply:os.system ["false"]'])
def test_missing_redacted_nonscalar_or_unsafe_yaml_is_rejected(value):
    with pytest.raises((witness.HarnessError, yaml.YAMLError)):
        witness.parse_generation(envelope(value), 'request')


def test_generation_visibility_cannot_clear_settlement_barrier():
    process = {'pid': 1, 'start_ticks': '2'}
    result = witness.binding_state(process, process, GENERATION, {'generation': GENERATION}, 'hash', 'hash')
    assert result['candidate_generation_visible']
    assert result['barrier_must_remain'] and result['reconciliation_required']
    assert not result['settlement_confirmed']


@pytest.mark.parametrize('current,digest,expired,reason', [
    ({'pid': 2, 'start_ticks': '2'}, 'hash', False, 'service_identity_changed'),
    ({'pid': 1, 'start_ticks': '3'}, 'hash', False, 'service_identity_changed'),
    ({'pid': 1, 'start_ticks': '2'}, 'other', False, 'candidate_file_digest_changed'),
    ({'pid': 1, 'start_ticks': '2'}, 'hash', True, 'deadline_expired'),
])
def test_restart_disk_mismatch_and_deadline_fail_closed(current, digest, expired, reason):
    result = witness.binding_state({'pid': 1, 'start_ticks': '2'}, current, GENERATION,
                                   {'generation': GENERATION}, 'hash', digest, expired=expired)
    assert not result['candidate_generation_visible']
    assert reason in result['reasons'] and result['barrier_must_remain']


def test_same_stat_replacement_exposes_watcher_blind_spot(tmp_path):
    config = tmp_path / 'config'
    config.write_bytes(b'old-config')
    before = config.stat()
    event = witness.replace_once(config, b'new-config', preserve_stat=True)
    after = config.stat()
    assert before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
    assert event['prior_size'] == event['candidate_size'] and config.read_bytes() == b'new-config'


def test_bad_same_stat_fixture_does_not_replace_original(tmp_path):
    config = tmp_path / 'config'
    config.write_bytes(b'original')
    with pytest.raises(witness.HarnessError):
        witness.replace_once(config, b'other-size', preserve_stat=True)
    assert config.read_bytes() == b'original'


def test_dry_run_opens_no_socket_process_or_output(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        pytest.fail('dry-run attempted mutation')
    monkeypatch.setattr(witness.subprocess, 'Popen', fail)
    monkeypatch.setattr(witness.socket, 'socket', fail)
    monkeypatch.setattr(witness.tempfile, 'mkdtemp', fail)
    out = tmp_path / 'out'
    assert witness.main(['--llama-swap-binary', str(tmp_path / 'absent'), '--output-dir', str(out), '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['dry_run']
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('deadline', ['nan', 'inf', '0', '301'])
def test_invalid_deadline_creates_no_resources(tmp_path, deadline):
    assert witness.main(['--llama-swap-binary', '/missing', '--deadline-seconds', deadline,
                         '--output-dir', str(tmp_path / 'out')]) == 2
    assert not (tmp_path / 'out').exists()


PIDFD = hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')


@pytest.mark.skipif(not PIDFD, reason='Linux pidfd-enabled Python required')
def test_failed_binary_startup_preserves_foreign_output_and_cleans_own_session(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    foreign = out / 'foreign'
    foreign.write_text('keep')
    result = witness.bounded_run({'binary': '/missing', 'scenario': 'adopt', 'deadline_seconds': 3}, out)
    assert result['status'] == 'worker_failed'
    assert result['cleanup'] == {'owned_session_empty': True, 'owned_temp_removed': True}
    assert foreign.read_text() == 'keep'


@pytest.mark.skipif(not PIDFD, reason='Linux pidfd-enabled Python required')
def test_hard_deadline_kills_only_owned_worker(tmp_path, monkeypatch):
    original = subprocess.Popen
    # Exercise this supervisor's hard backstop without requiring v252 or sockets.
    def slow_worker(argv, **kwargs):
        return original([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
    monkeypatch.setattr(witness.subprocess, 'Popen', slow_worker)
    result = witness.bounded_run({'binary': '/missing', 'scenario': 'adopt', 'deadline_seconds': .4}, tmp_path / 'out')
    assert result['status'] == 'deadline_exceeded'
    assert result['cleanup'] == {'owned_session_empty': True, 'owned_temp_removed': True}
    assert result['elapsed_seconds'] < 2


@pytest.mark.skipif(not PIDFD, reason='Linux pidfd-enabled Python required')
def test_stop_fixture_refuses_reused_process_identity(tmp_path, monkeypatch):
    (tmp_path / 'old-process.json').write_text(json.dumps({'pid': os.getpid(), 'start_ticks': 'wrong'}))
    monkeypatch.setattr(witness.signal, 'pidfd_send_signal', lambda *args: pytest.fail('signaled reused PID'))
    with pytest.raises(witness.HarnessError, match='reused fixture PID'):
        witness.stop_fixture(tmp_path, 'adopt', os.getpid(), 0)


def test_native_read_records_completion_and_caps_socket_by_phase_budget(monkeypatch):
    observed = {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            value = envelope()
            value['id'] = observed['request_id']
            return json.dumps(value).encode()
    class Opener:
        def open(self, request, timeout):
            observed['timeout'] = timeout
            observed['request_id'] = json.loads(request.data)['id']
            return Response()
    monkeypatch.setattr(witness.urllib.request, 'build_opener', lambda *args: Opener())
    record = witness.mcp_read('http://127.0.0.1:1', witness.Deadline(.02))
    assert record['generation'] == GENERATION
    assert 0 < observed['timeout'] <= .02
    assert record['response_received_mono'] >= record['monotonic']


def test_disabled_endpoint_preserves_status_and_body(monkeypatch):
    import io
    class Opener:
        def open(self, request, timeout):
            raise witness.urllib.error.HTTPError(request.full_url, 503, 'disabled', {}, io.BytesIO(b'{"enabled":false}'))
    monkeypatch.setattr(witness.urllib.request, 'build_opener', lambda *args: Opener())
    result = witness.mcp_read('http://127.0.0.1:1', witness.Deadline(1))
    assert result['http_status'] == 503 and result['error_body'] == '{"enabled":false}'
    assert 'generation' not in result and result['error']


def test_pending_requests_are_explicit_and_frozen():
    mutable = [{'id': 'req-0', 'status': 'completed'}]
    captured = witness.capture_requests(mutable)
    mutable.append({'id': 'req-1', 'status': 'completed'})
    assert witness.summarize(captured)['pending_at_capture'] == 1
    assert witness.summarize(captured)['completed'] == 1


def test_truncated_http_error_body_keeps_status_and_closes_response(monkeypatch):
    import io
    class BrokenBody(io.BytesIO):
        def read(self, *args): raise TimeoutError('truncated error body')
    body = BrokenBody()
    class Opener:
        def open(self, request, timeout):
            raise witness.urllib.error.HTTPError(request.full_url, 503, 'disabled', {}, body)
    monkeypatch.setattr(witness.urllib.request, 'build_opener', lambda *args: Opener())
    result = witness.mcp_read('http://127.0.0.1:1', witness.Deadline(1))
    assert result['http_status'] == 503 and 'truncated' in result['error_body_read_error']
    assert body.closed and 'response_received_mono' in result


def test_incomplete_success_body_cannot_witness_generation(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): raise witness.http.client.IncompleteRead(b'partial', 20)
    class Opener:
        def open(self, request, timeout): return Response()
    monkeypatch.setattr(witness.urllib.request, 'build_opener', lambda *args: Opener())
    result = witness.mcp_read('http://127.0.0.1:1', witness.Deadline(1))
    assert result['http_status'] == 200 and result['error'] and 'generation' not in result
