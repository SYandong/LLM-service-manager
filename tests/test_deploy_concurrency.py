# Generated-By: Codex / gpt-6-astra
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import urllib.request

import pytest
import yaml

from deploy import concurrency_smoke as smoke

CONFIG = b'''# preserve top comment
globalTTL: 600
macros:
  greeting: "keep me"
models:
  first: # preserve model comment
    cmd: 'python3 -c "print(1)"'
    proxy: http://127.0.0.1:9001
    concurrencyLimit: 10 # preserve scalar comment
    ttl: 600
  second:
    cmd: echo second
    proxy: http://127.0.0.1:9002
  third:
    cmd: echo third
    proxy: http://127.0.0.1:9003
    concurrencyLimit: 128
'''


def test_localized_candidate_preserves_bytes_and_larger_cap():
    candidate, changes = smoke.candidate_config(CONFIG, 64)
    assert candidate == CONFIG.replace(b'concurrencyLimit: 10 #', b'concurrencyLimit: 64 #').replace(
        b'  second:\n', b'  second:\n    concurrencyLimit: 64\n')
    assert changes == [{'model': 'first', 'previous': 10, 'candidate': 64},
                       {'model': 'second', 'previous': None, 'candidate': 64}]
    parsed = yaml.safe_load(candidate)
    assert parsed['models']['third']['concurrencyLimit'] == 128
    assert parsed['globalTTL'] == 600 and parsed['models']['first']['ttl'] == 600


def test_crlf_and_non_ascii_comments_stay_unchanged():
    original = ('# 注释\n' + CONFIG.decode()).replace('\n', '\r\n').encode()
    candidate, _ = smoke.candidate_config(original, 64)
    assert candidate.startswith('# 注释\r\n'.encode())
    assert b'\n' not in candidate.replace(b'\r\n', b'')


@pytest.mark.parametrize('text', [b'models: {a: {cmd: x}}\n', b'models:\n  a: &anchor\n    cmd: x\n',
    b'models:\n  a:\n    cmd: x\n    cmd: y\n', b'models:\n  a:\n    concurrencyLimit: nope\n',
    b'models: {}\n', b'models:\n  a:\n    cmd: x', b'models:\n  a: null\n'])
def test_ambiguous_or_unsupported_config_fails_before_mutation(text):
    with pytest.raises((smoke.HarnessError, yaml.YAMLError)):
        smoke.candidate_config(text, 64)


def test_prepare_dry_run_makes_no_file_process_socket(tmp_path, monkeypatch):
    source = tmp_path / 'copy.yaml'; source.write_bytes(CONFIG)
    def forbidden(*args, **kwargs): pytest.fail('dry-run mutated state')
    monkeypatch.setattr(smoke.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(smoke.socket, 'socket', forbidden)
    monkeypatch.setattr(smoke, 'check_binary', forbidden)
    output = tmp_path / 'stage'
    plan = smoke.prepare(source, output, '/missing', 64, True)
    assert len(plan['changes']) == 2 and source.read_bytes() == CONFIG and not output.exists()


def test_staging_and_exact_rollback_candidate_preserve_source(tmp_path, monkeypatch):
    source = tmp_path / 'copy.yaml'; source.write_bytes(CONFIG)
    calls = []
    monkeypatch.setattr(smoke, 'check_binary', lambda binary: None)
    monkeypatch.setattr(smoke, 'CommandValidator', lambda *args, **kwargs: lambda path: calls.append(path.name))
    stage = tmp_path / 'stage'
    smoke.prepare(source, stage, '/fixture', 64, False)
    assert calls == ['original.yaml', 'candidate.yaml']
    assert (stage / 'original.yaml').read_bytes() == source.read_bytes() == CONFIG
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in stage.iterdir())
    rollback = tmp_path / 'rollback.yaml'
    smoke.rollback_candidate(stage, stage / 'candidate.yaml', rollback, True)
    assert not rollback.exists()
    smoke.rollback_candidate(stage, stage / 'candidate.yaml', rollback, False)
    assert rollback.read_bytes() == CONFIG and source.read_bytes() == CONFIG
    with pytest.raises(smoke.HarnessError):
        smoke.rollback_candidate(stage, stage / 'candidate.yaml', rollback, False)
    (stage / 'candidate.yaml').write_text('changed')
    with pytest.raises(smoke.HarnessError, match='differs'):
        smoke.rollback_candidate(stage, stage / 'candidate.yaml', tmp_path / 'another', False)


def test_failed_validation_removes_only_own_stage(tmp_path, monkeypatch):
    source = tmp_path / 'copy.yaml'; source.write_bytes(CONFIG)
    other = tmp_path / 'foreign'; other.write_text('keep')
    monkeypatch.setattr(smoke, 'check_binary', lambda binary: None)
    def fail(path): raise smoke.ValidationError('rejected')
    monkeypatch.setattr(smoke, 'CommandValidator', lambda *args, **kwargs: fail)
    with pytest.raises(smoke.ValidationError):
        smoke.prepare(source, tmp_path / 'stage', '/fixture', 64, False)
    assert not (tmp_path / 'stage').exists() and source.read_bytes() == CONFIG and other.read_text() == 'keep'


def test_gate_counts_distinct_adjudicated_requests_not_double_counted_overlap():
    gate = smoke.Gate(3)
    gate.attempted.update(['a', 'b', 'c'])
    gate.enter('a')
    gate.finish('a', {'id': 'a', 'status': 'timeout'})
    gate.finish('b', {'id': 'b', 'status': 'rejected'})
    assert len(gate.arrived | gate.results.keys()) == 2
    checked, finished = threading.Event(), threading.Event()
    first_result = []
    original_wait_for = gate.changed.wait_for
    def observed_wait_for(predicate, timeout):
        def observed_predicate():
            value = predicate()
            if not first_result:
                first_result.append(value)
                checked.set()
            return value
        return original_wait_for(observed_predicate, timeout)
    gate.changed.wait_for = observed_wait_for
    def waiter():
        assert gate.wait_adjudicated(smoke.Deadline(5))
        finished.set()
    thread = threading.Thread(target=waiter); thread.start()
    assert checked.wait(5) and first_result == [False]
    gate.finish('c', {'id': 'c', 'status': 'rejected'})
    assert finished.wait(5)
    thread.join(5)


def test_real_loopback_32_clients_are_held_before_release():
    gate = smoke.Gate(32); deadline = smoke.Deadline(20)
    server = smoke.Backend(('127.0.0.1', 0), smoke.backend_handler(gate, deadline))
    serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}); serving.start()
    start = threading.Barrier(33)
    threads = [threading.Thread(target=smoke.client, args=('http://127.0.0.1:' + str(server.server_port),
        'req-%03d' % index, start, gate, deadline)) for index in range(32)]
    try:
        for thread in threads: thread.start()
        start.wait(10)
        assert gate.wait_adjudicated(deadline)
        assert len(gate.active) == gate.peak == 32 and gate.results == {}
        gate.release.set()
        for thread in threads: thread.join(10)
        assert len(gate.results) == 32 and smoke.counts(list(gate.results.values()))['completed'] == 32
    finally:
        gate.release.set()
        server.shutdown(); server.server_close(); serving.join(5)
        for thread in threads: thread.join(5)


def test_outcomes_do_not_turn_failures_into_completed():
    result = smoke.counts([{'status': value} for value in ('completed', 'rejected', 'timeout', 'truncated', 'http_error', 'transport_error', 'stream_error', 'not_attempted')])
    assert all(value == 1 for value in result.values())


def test_measure_dry_run_and_invalid_parameters_create_no_workers(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(smoke, 'bounded_run', lambda *args: pytest.fail('worker created'))
    args = ['measure', '--llama-swap-binary', '/absent', '--output-dir', str(tmp_path / 'out')]
    assert smoke.main(args + ['--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['dry_run'] and not (tmp_path / 'out').exists()
    for more in (['--requests', '0'], ['--requests', '100'], ['--limit', '0'], ['--deadline-seconds', 'nan']):
        assert smoke.main(args + more) == 2


@pytest.mark.skipif(not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'), reason='Linux pidfd Python required')
def test_failed_binary_accounts_unattempted_clients_and_cleans(tmp_path):
    result = smoke.bounded_run({'binary': '/missing', 'limit': 64, 'requests': 32, 'deadline_seconds': 5}, tmp_path / 'output')
    assert result['status'] != 'ok'
    assert result['attempted'] == 0 and result['outcomes']['not_attempted'] == 32
    assert result['cleanup'] == {'owned_session_empty': True, 'owned_temp_removed': True}


@pytest.mark.skipif(not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'), reason='Linux pidfd Python required')
def test_hard_deadline_backstop_preserves_foreign_output(tmp_path, monkeypatch):
    output = tmp_path / 'output'; output.mkdir(); (output / 'foreign').write_text('keep')
    original = subprocess.Popen
    def slow(argv, **kwargs): return original([sys.executable, '-c', 'import time;time.sleep(10)'], **kwargs)
    monkeypatch.setattr(smoke.subprocess, 'Popen', slow)
    result = smoke.bounded_run({'binary': '/missing', 'limit': 64, 'requests': 32, 'deadline_seconds': 1}, output)
    assert result['status'] == 'deadline_exceeded' and result['elapsed_seconds'] < 5
    assert result['cleanup']['owned_session_empty'] and (output / 'foreign').read_text() == 'keep'


@pytest.mark.skipif(not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'), reason='Linux pidfd Python required')
def test_failed_worker_keeps_structured_diagnostic(tmp_path):
    result = smoke.bounded_run({'binary': '/missing', 'limit': 64, 'requests': 1, 'deadline_seconds': 5}, tmp_path / 'output')
    assert result['failure'] and 'FileNotFoundError' in result['failure']


@pytest.mark.skipif(not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'), reason='Linux pidfd Python required')
def test_timeout_recovers_partial_accounting_without_timing_race(tmp_path, monkeypatch):
    class Worker:
        pid = 987654321
        returncode = None
        def __init__(self, argv, **kwargs):
            self.directory = Path(argv[-1]).parent
        def communicate(self, timeout):
            (self.directory / 'result.json').write_text(json.dumps({'status': 'failed', 'attempted': 3,
                'outcomes': {'completed': 1, 'rejected': 2}, 'failure': 'partial before shutdown'}))
            raise subprocess.TimeoutExpired('fixture', timeout)
        def poll(self): return -9
    monkeypatch.setattr(smoke.subprocess, 'Popen', Worker)
    monkeypatch.setattr(smoke, 'kill_owned_session', lambda pid: None)
    monkeypatch.setattr(smoke, 'owned_session_members', lambda pid: [])
    result = smoke.bounded_run({'binary': '/missing', 'limit': 64, 'requests': 32, 'deadline_seconds': 5}, tmp_path / 'output')
    assert result['status'] == 'deadline_exceeded' and result['accounting_available'] is True
    assert result['attempted'] == 3 and result['outcomes']['rejected'] == 2
    assert result['failure'] == 'partial before shutdown'


def test_client_stream_error_with_done_is_not_success(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self): return iter([b'data: {"error":{"message":"fixture"}}\n', b'data: [DONE]\n'])
    class Opener:
        def open(self, *args, **kwargs): return Response()
    monkeypatch.setattr(smoke.urllib.request, 'build_opener', lambda *args: Opener())
    gate = smoke.Gate(1)
    smoke.client('http://127.0.0.1:1', 'one', threading.Barrier(1), gate, smoke.Deadline(5))
    assert gate.results['one']['status'] == 'stream_error'


def test_http429_body_timeout_stays_explicit_rejection(monkeypatch):
    import io
    class Body(io.BytesIO):
        def read(self, *args): raise TimeoutError('fixture')
    class Opener:
        def open(self, request, **kwargs):
            raise smoke.urllib.error.HTTPError(request.full_url, 429, 'limit', {}, Body())
    monkeypatch.setattr(smoke.urllib.request, 'build_opener', lambda *args: Opener())
    gate = smoke.Gate(1)
    smoke.client('http://127.0.0.1:1', 'one', threading.Barrier(1), gate, smoke.Deadline(5))
    assert gate.results['one']['status'] == 'rejected'
    assert gate.results['one']['body_read_error'] == 'TimeoutError'
