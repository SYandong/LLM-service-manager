# Generated-By: Codex / gpt-6-astra
"""Read-only witness tests: real fake HTTP, fixed #91 envelopes, explicit barriers."""
from copy import deepcopy
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time

import pytest

from llmsvc.reload_witness import (BindingObservation, CandidateBinding, GenerationRead,
                                  InstanceIdentity, NativeGenerationReader, WitnessError,
                                  check_visibility, parse_generation, QUERY_DIALECT, V252_DIALECT)

_EVIDENCE = json.loads((Path(__file__).resolve().parents[1] / 'deploy' / 'watcher-witness-results-20260908.json').read_text())
_SAMPLES = [sample for case in _EVIDENCE['cases'] for sample in case['selected_native_samples']
            if sample.get('generation')]
_SAMPLE = _SAMPLES[0]
GENERATION = _SAMPLE['generation']
INSTANCE = InstanceIdentity(123, '456')
DIGEST = 'a' * 64


def envelope(request_id, dialect=V252_DIALECT):
    result = deepcopy(_SAMPLE['response'])
    result['id'] = request_id
    if dialect == QUERY_DIALECT:
        # Source-derived scalar fixture, NOT a captured native execution receipt.
        # 8fa85899 internal/config/mcpprovider.go:220-238 formats this exact text.
        result['result']['content'][0]['text'] = (
            'Current llama-swap configuration, jq query .macros.llmsvc_reload_generation'
            ' (credentials redacted, values resolved):\n\n```yaml\n'
            + GENERATION + '\n```\n')
    return result


@pytest.fixture
def fake_http():
    dialect = V252_DIALECT
    state = {'dialect': dialect, 'envelope': lambda rid: envelope(rid, dialect),
             'requests': [], 'response': lambda body: envelope(body['id'], dialect), 'status': 200,
             'headers': {}, 'entered': threading.Event(), 'release': threading.Event(), 'stall': False, 'drip': False}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append((self.path, dict(self.headers), body))
            value = state['response'](body)
            payload = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(state['status'])
            headers = {'Content-Type': 'application/json', 'Content-Length': str(len(payload)), **state['headers']}
            for key, val in headers.items():
                if val is not None:
                    self.send_header(key, val)
            if not state['stall']:
                self.end_headers()
            self.wfile.flush()
            state['entered'].set()
            if state['stall']:
                state['release'].wait(10)
                self.close_connection = True
                return
            try:
                if state['drip']:
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if state['release'].wait(.05):
                            break
                else:
                    self.wfile.write(payload)
            except OSError:
                pass
            self.close_connection = True
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    state['url'] = f'http://127.0.0.1:{server.server_port}'
    try:
        yield state
    finally:
        state['release'].set()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture(params=[V252_DIALECT, QUERY_DIALECT])
def dialect_http(fake_http, request):
    # Preserve the default fake_http fixture used by existing recovery tests.
    fake_http['dialect'] = request.param
    fake_http['envelope'] = lambda rid: envelope(rid, request.param)
    fake_http['response'] = lambda body: fake_http['envelope'](body['id'])
    return fake_http


def read_fixture(state, **kwargs):
    if state['dialect'] != V252_DIALECT:
        kwargs.setdefault('dialect', state['dialect'])
    reader = NativeGenerationReader(state['url'], request_timeout=5, **kwargs)
    return reader, reader.read(deadline=time.monotonic() + 5)


def check_read(reader, reading, **kwargs):
    binding = CandidateBinding(reader.endpoint, GENERATION, INSTANCE, DIGEST)
    before = BindingObservation(reading.started_at, INSTANCE, DIGEST)
    after = BindingObservation(reading.received_at, INSTANCE, DIGEST)
    return check_visibility(binding, before, reading, after, now=reading.received_at, **kwargs)


@pytest.mark.parametrize('sample', _SAMPLES, ids=lambda sample: sample['request']['id'])
def test_parser_reuses_exact_sanitized_native_samples(sample):
    assert parse_generation(sample['response'], sample['request']['id']) == sample['generation']


def test_real_fake_http_request_and_readonly_binding(dialect_http, tmp_path, monkeypatch):
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    marker = tmp_path / 'candidate.yaml'
    marker.write_bytes(b'unchanged')
    old_stat = marker.stat()
    reader, result = read_fixture(dialect_http)
    assert result.error is None and result.generation == GENERATION
    path, headers, body = dialect_http['requests'][0]
    assert path == '/api/mcp' and len(dialect_http['requests']) == 1
    assert headers['Mcp-Protocol-Version'] == '2026-07-28'
    assert headers['Mcp-Method'] == 'tools/call' and headers['Mcp-Name'] == 'config__get_config'
    assert body == {'jsonrpc': '2.0', 'id': result.request_id, 'method': 'tools/call',
                    'params': {'name': 'config__get_config', 'arguments': ({'path': 'macros.llmsvc_reload_generation'} if dialect_http['dialect'] == V252_DIALECT
                                              else {'query': '.macros.llmsvc_reload_generation'})}}
    checked = check_read(reader, result)
    assert checked.candidate_generation_visible and checked.settlement_confirmed is None
    assert 'applied' not in asdict(checked) and 'status' not in asdict(checked)
    assert marker.read_bytes() == b'unchanged'
    assert (marker.stat().st_mtime_ns, marker.stat().st_ino) == (old_stat.st_mtime_ns, old_stat.st_ino)
    assert list(tmp_path.iterdir()) == [marker]


@pytest.mark.parametrize('mutation', [
    lambda e: e.update(id='wrong'),
    lambda e: e.update(jsonrpc='1.0'),
    lambda e: e.update(error={'message': 'do not expose secret payload'}),
    lambda e: e['result'].update(isError=True),
    lambda e: e['result'].update(isError=1),
    lambda e: e['result'].update(content=[]),
    lambda e: e['result']['content'].append(deepcopy(e['result']['content'][0])),
    lambda e: e['result']['content'][0].update(type='resource'),
    lambda e: e['result']['content'][0].update(text='```yaml\n' + GENERATION + '\n```\n'),
    lambda e: e['result']['content'][0].update(text=e['result']['content'][0]['text'][:-4]),
    lambda e: e['result']['content'][0].update(text=e['result']['content'][0]['text'].replace(GENERATION, '[REDACTED]')),
    lambda e: e['result']['content'][0].update(text=e['result']['content'][0]['text'].replace(GENERATION, '{unexpected: value}')),
])
def test_http200_errors_never_become_visibility(dialect_http, mutation):
    def respond(body):
        value = dialect_http['envelope'](body['id'])
        mutation(value)
        return value
    dialect_http['response'] = respond
    reader, result = read_fixture(dialect_http)
    assert result.http_status == 200 and result.error and result.generation is None
    assert 'secret payload' not in repr(result)
    assert not check_read(reader, result).candidate_generation_visible


@pytest.mark.parametrize('mode,error', [
    ('malformed', 'invalid_json'), ('duplicate_id', 'invalid_json'), ('nan', 'invalid_json'),
    ('truncated_length', 'truncated_response'), ('declared_large', 'response_too_large'),
    ('body_large', 'response_too_large'), ('content_type', 'invalid_content_type'),
    ('encoding', 'unsupported_content_encoding'), ('invalid_length', 'invalid_http_framing'),
])
def test_malformed_truncated_and_oversized_http(dialect_http, mode, error):
    if mode == 'malformed':
        dialect_http['response'] = lambda body: b'{'
    elif mode == 'duplicate_id':
        dialect_http['response'] = lambda body: ('{"id":"wrong",' + json.dumps(dialect_http['envelope'](body['id']))[1:]).encode()
    elif mode == 'nan':
        dialect_http['response'] = lambda body: json.dumps(dialect_http['envelope'](body['id'])).replace('"result":', '"extra":NaN,"result":').encode()
    elif mode == 'truncated_length':
        dialect_http['headers']['Content-Length'] = '10000'
    elif mode == 'declared_large':
        dialect_http['headers']['Content-Length'] = '65537'
    elif mode == 'body_large':
        dialect_http['headers']['Content-Length'] = None
        dialect_http['response'] = lambda body: b'x' * 65537
    elif mode == 'content_type':
        dialect_http['headers']['Content-Type'] = 'text/html'
    elif mode == 'encoding':
        dialect_http['headers']['Content-Encoding'] = 'gzip'
    elif mode == 'invalid_length':
        dialect_http['headers']['Content-Length'] = '-1'
    _, result = read_fixture(dialect_http)
    assert result.error == error and result.generation is None


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 401, 503])
def test_redirects_and_http_errors_do_not_follow_or_retry(dialect_http, status):
    dialect_http['status'] = status
    dialect_http['headers']['Location'] = dialect_http['url'] + '/must-not-follow'
    _, result = read_fixture(dialect_http)
    assert result.http_status == status
    assert result.error == ('redirect_rejected' if status < 400 else 'http_error')
    assert len(dialect_http['requests']) == 1


def test_stale_generation_is_a_read_but_not_expected_visibility(dialect_http):
    def respond(body):
        value = dialect_http['envelope'](body['id'])
        value['result']['content'][0]['text'] = value['result']['content'][0]['text'].replace(GENERATION, 'gen_' + '0' * 32)
        return value
    dialect_http['response'] = respond
    reader, reading = read_fixture(dialect_http)
    assert reading.error is None
    result = check_read(reader, reading)
    assert not result.candidate_generation_visible and result.settlement_confirmed is None


@pytest.mark.parametrize('variant,reason', [
    ('pid', 'service_identity_changed'), ('start_ticks', 'service_identity_changed'),
    ('missing_instance', 'instance_unknown'), ('digest', 'candidate_file_digest_unconfirmed'),
    ('missing_digest', 'candidate_file_digest_unconfirmed'), ('endpoint', 'endpoint_mismatch'),
    ('stale', 'observations_stale_or_unordered'), ('unbracketed', 'observations_stale_or_unordered'),
    ('late', 'deadline_expired'),
])
def test_binding_checks_are_pure_and_report_detectable_changes(dialect_http, variant, reason):
    reader, reading = read_fixture(dialect_http)
    expected = CandidateBinding(reader.endpoint, GENERATION, INSTANCE, DIGEST)
    before = BindingObservation(reading.started_at, INSTANCE, DIGEST)
    after = BindingObservation(reading.received_at, INSTANCE, DIGEST)
    now = reading.received_at
    if variant == 'pid': after = replace(after, instance=InstanceIdentity(999, '456'))
    elif variant == 'start_ticks': after = replace(after, instance=InstanceIdentity(123, '999'))
    elif variant == 'missing_instance': after = replace(after, instance=None)
    elif variant == 'digest': after = replace(after, candidate_sha256='b' * 64)
    elif variant == 'missing_digest': after = replace(after, candidate_sha256=None)
    elif variant == 'endpoint': expected = replace(expected, endpoint='http://127.0.0.1:1/api/mcp')
    elif variant == 'stale': before = replace(before, observed_at=now - 20)
    elif variant == 'unbracketed': after = replace(after, observed_at=reading.started_at - 1)
    elif variant == 'late': now = reading.deadline
    result = check_visibility(expected, before, reading, after, now=now)
    assert not result.candidate_generation_visible and reason in result.reasons
    assert result.settlement_confirmed is None
    assert len(dialect_http['requests']) == 1


@pytest.mark.parametrize('dialect', [V252_DIALECT, QUERY_DIALECT])
def test_expired_deadline_opens_no_socket(monkeypatch, dialect):
    reader = NativeGenerationReader('http://127.0.0.1:1', clock=lambda: 10, dialect=dialect)
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('opened socket after deadline'))
    assert reader.read(deadline=10).error == 'deadline_expired'


def test_late_success_is_rejected_without_scheduler_timing_assumptions(dialect_http):
    clock = [100.0]
    reader = NativeGenerationReader(dialect_http['url'], request_timeout=5, clock=lambda: clock[0], dialect=dialect_http['dialect'])
    def respond(body):
        clock[0] = 106.0
        return dialect_http['envelope'](body['id'])
    dialect_http['response'] = respond
    result = reader.read(deadline=105)
    assert result.error == 'deadline_expired' and result.generation is None


def test_header_stall_is_interrupted_and_resources_close(dialect_http):
    dialect_http['stall'] = True
    reader = NativeGenerationReader(dialect_http['url'], request_timeout=1, dialect=dialect_http['dialect'])
    results = []
    worker = threading.Thread(target=lambda: results.append(reader.read(deadline=time.monotonic() + 5)))
    worker.start()
    assert dialect_http['entered'].wait(5), 'request never reached fake server'
    worker.join(5)
    dialect_http['release'].set()
    assert not worker.is_alive(), 'read exceeded its one-second I/O budget'
    assert results[0].error == 'deadline_expired' and results[0].generation is None


@pytest.mark.parametrize('url', ['http://localhost:8000', 'https://127.0.0.1', 'http://user:pass@127.0.0.1',
                                  'http://127.0.0.1/path', 'http://127.0.0.1?query=1', 'http://127.0.0.1:0',
                                  'http://127.0.0.1#fragment', ' http://127.0.0.1'])
def test_unsupported_endpoints_rejected_before_io(url):
    with pytest.raises(ValueError):
        NativeGenerationReader(url)


@pytest.mark.parametrize('setting,value', [('request_timeout',0), ('request_timeout',float('nan')),
                                           ('max_response_bytes',True), ('max_response_bytes',65537)])
def test_invalid_limits_rejected(setting, value):
    with pytest.raises(ValueError):
        NativeGenerationReader('http://127.0.0.1:1', **{setting: value})


@pytest.mark.parametrize('value', ['x' * 32769, '\ud800'])
def test_oversized_or_invalid_unicode_native_text_is_rejected(value):
    body = envelope('request')
    body['result']['content'][0]['text'] = value
    with pytest.raises(WitnessError, match='invalid_yaml_envelope'):
        parse_generation(body, 'request')


@pytest.mark.parametrize('dialect', [V252_DIALECT, QUERY_DIALECT])
def test_body_stall_is_interrupted_before_success(dialect):
    # Exercise a peer that completes headers but never completes its body.
    entered = threading.Event()
    release = threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    def serve():
        with listener.accept()[0] as conn:
            conn.recv(65536)
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{')
            entered.set()
            release.wait(10)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    reader = NativeGenerationReader(f'http://127.0.0.1:{listener.getsockname()[1]}', request_timeout=1, dialect=dialect)
    results = []
    worker = threading.Thread(target=lambda: results.append(reader.read(deadline=time.monotonic()+5)))
    try:
        worker.start()
        assert entered.wait(5)
        worker.join(5)
        assert not worker.is_alive()
        assert results[0].error == 'deadline_expired'
    finally:
        release.set()
        listener.close()
        worker.join(5)
        thread.join(5)


def test_trickled_body_cannot_reset_the_total_read_budget(dialect_http):
    # The delay models a hostile transport, not a scheduler-speed success test.
    # Without the absolute watchdog, each byte resets the socket idle timeout.
    dialect_http['drip'] = True
    reader = NativeGenerationReader(dialect_http['url'], request_timeout=1, dialect=dialect_http['dialect'])
    results = []
    worker = threading.Thread(target=lambda: results.append(reader.read(deadline=time.monotonic()+5)), daemon=True)
    try:
        worker.start()
        assert dialect_http['entered'].wait(5)
        worker.join(3)
        assert not worker.is_alive(), 'trickled bytes extended the total read budget'
        assert results[0].error == 'deadline_expired' and results[0].generation is None
    finally:
        dialect_http['release'].set()
        worker.join(5)


@pytest.mark.parametrize('dialect', [V252_DIALECT, QUERY_DIALECT])
def test_pinned_scalar_envelopes_parse_only_the_selected_dialect(dialect):
    value = envelope('query-fixture', dialect)
    assert parse_generation(value, 'query-fixture', dialect=dialect) == GENERATION
    other = QUERY_DIALECT if dialect == V252_DIALECT else V252_DIALECT
    with pytest.raises(WitnessError, match='invalid_yaml_envelope'):
        parse_generation(value, 'query-fixture', dialect=other)


def test_other_dialect_response_never_triggers_fallback(dialect_http):
    other = QUERY_DIALECT if dialect_http['dialect'] == V252_DIALECT else V252_DIALECT
    dialect_http['response'] = lambda body: envelope(body['id'], other)
    _, reading = read_fixture(dialect_http)
    assert reading.generation is None and reading.error == 'invalid_yaml_envelope'
    assert len(dialect_http['requests']) == 1


@pytest.mark.parametrize('bad', [None, '', 'auto', 'v255', 'query', {}, True])
def test_unknown_dialect_rejected_before_io(monkeypatch, bad):
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('invalid dialect opened socket'))
    with pytest.raises(ValueError, match='unsupported generation witness dialect'):
        NativeGenerationReader('http://127.0.0.1:1', dialect=bad)
    with pytest.raises(ValueError, match='unsupported generation witness dialect'):
        parse_generation(envelope('fixture'), 'fixture', dialect=bad)


@pytest.mark.parametrize('replacement', ['null', '', '[]', 'true', '1',
    GENERATION + '\n---\n' + GENERATION, '*missing', 'x' * 32769])
def test_native_text_must_return_one_complete_generation_scalar(dialect_http, replacement):
    def respond(body):
        value = dialect_http['envelope'](body['id'])
        value['result']['content'][0]['text'] = value['result']['content'][0]['text'].replace(GENERATION, replacement)
        return value
    dialect_http['response'] = respond
    _, result = read_fixture(dialect_http)
    assert result.generation is None and result.error in ('invalid_generation_scalar', 'invalid_yaml_envelope')
    assert len(dialect_http['requests']) == 1


@pytest.mark.parametrize('suffix', ['extra text', '\nnull means nothing is set there\n', '\n```yaml\nnull\n```\n'])
def test_extra_native_text_is_not_an_adoption_witness(dialect_http, suffix):
    def respond(body):
        value = dialect_http['envelope'](body['id'])
        value['result']['content'][0]['text'] += suffix
        return value
    dialect_http['response'] = respond
    _, result = read_fixture(dialect_http)
    assert result.generation is None and result.error
    assert len(dialect_http['requests']) == 1
