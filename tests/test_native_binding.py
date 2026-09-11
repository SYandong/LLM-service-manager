# Generated-By: Codex / gpt-6-astra
"""Actual owned process/image/listener fixtures; protocol replies are synthetic.

The test pins the fixture Python image to a synthetic provenance mapping. It
is NOT a native binary build attestation or a production source/settlement test.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.native_binding import BoundNativeGenerationReader, build_bound_generation_reader, validate_settings
from llmsvc.reload import _read_regular_file
from llmsvc.reload_witness import (CandidateBinding, InstanceIdentity, PINNED_COMMIT,
    QUERY_DIALECT, QUERY_PINNED_COMMIT, V252_DIALECT, WitnessError)

GENERATION = 'gen_' + '7' * 32
SERVER = '''import json,os,signal,sys
signal.signal(signal.SIGUSR1,lambda *_:os.execv('/bin/sleep',['sleep','30']))
from http.server import HTTPServer,BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  with open(sys.argv[2],'a') as out:out.write(json.dumps(request)+'\\n')
  query=sys.argv[3]=='8fa85899-query'
  arguments={'query':'.macros.llmsvc_reload_generation'} if query else {'path':'macros.llmsvc_reload_generation'}
  if request['params']['arguments']!=arguments:
   result={'isError':True,'content':[]}
  else:
   prefix=('Current llama-swap configuration, jq query .macros.llmsvc_reload_generation (credentials redacted, values resolved):\\n\\n```yaml\\n' if query else 'Current llama-swap configuration at "macros.llmsvc_reload_generation" (credentials redacted, values resolved):\\n\\n```yaml\\n')
   result={'content':[{'type':'text','text':prefix+sys.argv[4]+'\\n```\\n'}]}
  body=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=HTTPServer(('127.0.0.1',0),Handler)
with open(sys.argv[1]+'.tmp','w') as ready:json.dump({'port':server.server_port},ready)
os.replace(sys.argv[1]+'.tmp',sys.argv[1])
server.serve_forever()
'''


def identity(pid):
    raw = Path('/proc/' + str(pid) + '/stat').read_text()
    return InstanceIdentity(pid, raw[raw.rfind(')') + 2:].split()[19])


@pytest.fixture
def native_process(tmp_path):
    processes = []
    def start(dialect=V252_DIALECT):
        root = tmp_path / str(len(processes)); root.mkdir()
        ready, requests = root / 'ready.json', root / 'requests.jsonl'
        script = root / 'fixture.py'; script.write_text(SERVER)
        process = subprocess.Popen([sys.executable, str(script), str(ready), str(requests), dialect, GENERATION],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), 'owned fixture source did not start'
        port = json.loads(ready.read_text())['port']
        config_path = root / 'config.yaml'
        config_path.write_text('macros:\n  llmsvc_reload_generation: ' + GENERATION + '\n')
        image_digest = hashlib.sha256(Path('/proc/' + str(process.pid) + '/exe').read_bytes()).hexdigest()
        source = QUERY_PINNED_COMMIT if dialect == QUERY_DIALECT else PINNED_COMMIT
        settings = {'images':[{'source_commit':source,'executable_sha256':image_digest,'dialect':dialect}],
                    'request_timeout_seconds':2}
        instance = identity(process.pid)
        url = 'http://127.0.0.1:' + str(port)
        expected = CandidateBinding(url + '/api/mcp', GENERATION, instance,
                                    hashlib.sha256(config_path.read_bytes()).hexdigest())
        reader = BoundNativeGenerationReader(url, settings,
            instance_provider=lambda **kwargs: identity(process.pid),
            config_reader=lambda: _read_regular_file(config_path, 1024 * 1024),
            settings_provider=lambda:settings)
        return SimpleNamespace(process=process, path=config_path, settings=settings, expected=expected,
            reader=reader, requests=lambda: [json.loads(line) for line in requests.read_text().splitlines()] if requests.exists() else [])
    try:
        yield start
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)


@pytest.mark.parametrize('dialect', [V252_DIALECT, QUERY_DIALECT])
def test_actual_image_listener_and_instance_choose_one_pinned_dialect(native_process, dialect):
    source = native_process(dialect)
    result = source.reader.read(source.expected, expected_image_sha256=source.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5)
    assert result.visibility.candidate_generation_visible
    assert result.visibility.settlement_confirmed is None
    assert result.pin.dialect == dialect and result.instance == identity(source.process.pid)
    requests = source.requests()
    assert len(requests) == 1
    assert requests[0]['params']['arguments'] == ({'query':'.macros.llmsvc_reload_generation'} if dialect == QUERY_DIALECT
                                                else {'path':'macros.llmsvc_reload_generation'})


@pytest.mark.parametrize('change', ['image', 'instance', 'configuration', 'settings', 'expired'])
def test_mismatched_binding_opens_no_native_request(native_process, change):
    source = native_process()
    deadline = time.monotonic()+5
    expected = source.expected
    if change == 'image':
        source.reader = BoundNativeGenerationReader(source.reader.endpoint[:-len('/api/mcp')],
            {**source.settings,'images':[{**source.settings['images'][0],'executable_sha256':'0'*64}]},
            instance_provider=lambda **kwargs: identity(source.process.pid),
            config_reader=lambda:_read_regular_file(source.path, 1024*1024))
    elif change == 'instance':
        expected = replace(expected, instance=replace(expected.instance, start_ticks=str(int(expected.instance.start_ticks)+1)))
    elif change == 'configuration':
        source.path.write_text('changed: true\n')
    elif change == 'settings':
        source.settings['request_timeout_seconds'] = 3
    else:
        deadline = time.monotonic()-1
    with pytest.raises(WitnessError):
        source.reader.read(expected, expected_image_sha256=source.reader._pins[0].executable_sha256, deadline=deadline)
    assert source.requests() == []


def test_same_image_other_process_cannot_claim_the_endpoint(native_process):
    expected_source, other = native_process(), native_process()
    other.reader.endpoint = expected_source.reader.endpoint
    other.reader.port = expected_source.reader.port
    other.reader._readers = expected_source.reader._readers
    expected = replace(other.expected, endpoint=expected_source.reader.endpoint)
    with pytest.raises(WitnessError, match='endpoint_not_owned_by_instance'):
        other.reader.read(expected, expected_image_sha256=other.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5)
    assert expected_source.requests() == other.requests() == []


@pytest.mark.parametrize('change', ['config', 'instance', 'settings', 'image', 'exec'])
def test_changes_after_rpc_invalidate_the_read_without_retry(native_process, monkeypatch, change):
    source = native_process(QUERY_DIALECT)
    native = next(iter(source.reader._readers.values()))
    read = native.read
    def changed(*, deadline):
        result = read(deadline=deadline)
        if change == 'config':
            source.path.write_text('foreign: true\n')
        elif change == 'settings':
            source.settings['request_timeout_seconds'] = 3
        elif change == 'instance':
            source.reader.instance_provider = lambda **kwargs: InstanceIdentity(source.process.pid, '1')
        elif change == 'exec':
            import signal
            previous = os.readlink('/proc/' + str(source.process.pid) + '/exe')
            source.process.send_signal(signal.SIGUSR1)
            limit = time.monotonic()+2
            while os.readlink('/proc/' + str(source.process.pid) + '/exe') == previous and time.monotonic() < limit:
                time.sleep(0.005)
            assert os.readlink('/proc/' + str(source.process.pid) + '/exe') != previous
            assert identity(source.process.pid) == source.expected.instance  # exec retains PID/start ticks.
        else:
            source.process.terminate(); source.process.wait(timeout=3)
        return result
    monkeypatch.setattr(native, 'read', changed)
    with pytest.raises(WitnessError):
        source.reader.read(source.expected, expected_image_sha256=source.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5)
    assert len(source.requests()) == 1


def test_explicit_wrong_dialect_never_falls_back(native_process):
    source = native_process(QUERY_DIALECT)
    settings = {**source.settings, 'images':[{**source.settings['images'][0], 'source_commit':PINNED_COMMIT,'dialect':V252_DIALECT}]}
    reader = BoundNativeGenerationReader(source.reader.endpoint[:-len('/api/mcp')], settings,
        instance_provider=lambda **kwargs:identity(source.process.pid),
        config_reader=lambda:_read_regular_file(source.path,1024*1024))
    result = reader.read(source.expected, expected_image_sha256=source.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5)
    assert not result.visibility.candidate_generation_visible
    assert result.reading.error == 'native_tool_error' and len(source.requests()) == 1


@pytest.mark.parametrize('change', ['source', 'dialect', 'duplicate', 'bool_timeout', 'unlimited_image'])
def test_settings_reject_unsupported_or_ambiguous_pins(change):
    value = {'images':[{'source_commit':PINNED_COMMIT, 'executable_sha256':'0'*64,'dialect':V252_DIALECT}]}
    if change == 'source': value['images'][0]['source_commit'] = QUERY_PINNED_COMMIT
    elif change == 'dialect': value['images'][0]['dialect'] = 'auto'
    elif change == 'duplicate': value['images'].append(copy.deepcopy(value['images'][0]))
    elif change == 'bool_timeout': value['request_timeout_seconds'] = True
    else: value['max_image_bytes'] = 0
    with pytest.raises(ValueError):
        SchedulerConfig('127.0.0.1', 8011, native_witness=value)


def test_default_factory_does_not_inspect_or_open_source():
    def forbidden(*args, **kwargs): raise AssertionError('default called a source')
    config = SchedulerConfig('127.0.0.1', 8011)
    assert build_bound_generation_reader(config, instance_provider=forbidden, config_reader=forbidden) is None


def test_running_image_read_limit_blocks_before_http(native_process):
    source = native_process()
    source.reader._max_image = 1
    with pytest.raises(WitnessError, match='running_image_unavailable_or_too_large'):
        source.reader.read(source.expected, expected_image_sha256=source.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5)
    assert source.requests() == []


def test_factory_binds_current_configuration_and_remains_inert_until_read(native_process):
    source = native_process(QUERY_DIALECT)
    current = [SchedulerConfig('127.0.0.1', 8011,
        collectors={'swap_url':source.reader.endpoint[:-len('/api/mcp')]}, native_witness=source.settings)]
    calls = []
    def inspected(**kwargs):
        calls.append('instance')
        return identity(source.process.pid)
    reader = build_bound_generation_reader(current[0], instance_provider=inspected,
        config_reader=lambda:_read_regular_file(source.path,1024*1024), config_provider=lambda:current[0])
    assert not calls and not source.requests()
    assert reader.read(source.expected, expected_image_sha256=source.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5).visibility.candidate_generation_visible
    current[0] = replace(current[0], native_witness={})
    with pytest.raises(WitnessError, match='witness_configuration_changed'):
        reader.read(source.expected, expected_image_sha256=source.settings['images'][0]['executable_sha256'], deadline=time.monotonic()+5)
    assert len(source.requests()) == 1


def test_phase_cannot_accept_another_allowed_image(native_process):
    source = native_process()
    other_digest = hashlib.sha256(Path('/bin/sleep').read_bytes()).hexdigest()
    settings = copy.deepcopy(source.settings)
    settings['images'].append({'source_commit':QUERY_PINNED_COMMIT,
                              'executable_sha256':other_digest, 'dialect':QUERY_DIALECT})
    reader = BoundNativeGenerationReader(source.reader.endpoint[:-len('/api/mcp')], settings,
        instance_provider=lambda **kwargs:identity(source.process.pid),
        config_reader=lambda:_read_regular_file(source.path,1024*1024))
    with pytest.raises(WitnessError, match='phase_running_image_mismatch'):
        reader.read(source.expected, expected_image_sha256=other_digest, deadline=time.monotonic()+5)
    assert source.requests() == []
