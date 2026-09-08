#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Stage per-model caps and measure one isolated pinned-swap concurrency wave."""
import argparse
import copy
import hashlib
import http.server
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.reload_smoke import Deadline, HarnessError, kill_owned_session, owned_session_members, timestamp_text
from deploy.watcher_witness import PINNED_SHA256, PINNED_COMMIT, NoRedirect
from llmsvc.reload import CommandValidator, ValidationError


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_binary(binary):
    if digest(Path(binary).read_bytes()) != PINNED_SHA256 or not os.access(binary, os.X_OK):
        raise HarnessError("requires the exact executable pinned v252 binary")


def mapping(node):
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise HarnessError("edited YAML mappings must use explicit block style")
    fields = {}
    for key, value in node.value:
        if key.tag != 'tag:yaml.org,2002:str' or key.value in fields:
            raise HarnessError("duplicate or unsupported YAML key")
        fields[key.value] = (key, value)
    return fields


def candidate_config(original, limit):
    """Change only concurrencyLimit scalars; preserve comments/other bytes."""
    if type(limit) is not int or limit < 32:
        raise HarnessError("candidate limit must be an integer >=32")
    text = original.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    if not text.endswith('\n') or '\r' in text.replace('\r\n', '') or (newline == '\r\n' and '\n' in text.replace('\r\n', '')):
        raise HarnessError("configuration requires consistent line endings and final newline")
    if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken, yaml.TagToken)) for token in yaml.scan(text)):
        raise HarnessError("anchors, aliases or explicit tags require a reviewed manual edit")
    root = mapping(yaml.compose(text, Loader=yaml.SafeLoader))
    if 'models' not in root:
        raise HarnessError("configuration has no explicit models mapping")
    models = mapping(root['models'][1])
    if not models:
        raise HarnessError("configuration has no models")
    before = yaml.safe_load(text)
    expected = copy.deepcopy(before)
    edits, changes = [], []
    for name, (model_key, model_node) in models.items():
        fields = mapping(model_node)
        previous = before['models'][name].get('concurrencyLimit')
        if previous is not None and (type(previous) is not int or previous < 0):
            raise HarnessError("existing concurrencyLimit must be an explicit nonnegative integer")
        chosen = max(limit, previous or 0)  # Never silently lower an existing larger cap.
        if previous == chosen:
            continue
        if 'concurrencyLimit' in fields:
            value = fields['concurrencyLimit'][1]
            if not isinstance(value, yaml.ScalarNode) or value.tag != 'tag:yaml.org,2002:int':
                raise HarnessError("concurrencyLimit must be a direct integer scalar")
            edits.append((value.start_mark.index, value.end_mark.index, str(chosen)))
        else:
            if not fields:
                raise HarnessError("model needs existing block fields")
            end = text.index('\n', model_key.end_mark.index) + 1
            if not re.fullmatch(r':[ \t]*(?:#[^\r\n]*)?\r?\n', text[model_key.end_mark.index:end]):
                raise HarnessError("unsupported model header layout")
            indent = min(key.start_mark.column for key, _ in fields.values())
            edits.append((end, end, ' ' * indent + 'concurrencyLimit: ' + str(chosen) + newline))
        expected['models'][name]['concurrencyLimit'] = chosen
        changes.append({'model': name, 'previous': previous, 'candidate': chosen})
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if yaml.safe_load(text) != expected:
        raise HarnessError("localized edit changed unintended values")
    return text.encode('utf-8'), changes


def prepare(source, output, binary, limit, dry_run):
    if source.is_symlink() or not source.is_file():
        raise HarnessError("source must be a regular non-symlink configuration copy")
    original = source.read_bytes()
    candidate, changes = candidate_config(original, limit)
    if output.exists() or output.is_symlink() or output.resolve() == source.resolve():
        raise HarnessError("staging directory must be new")
    plan = {'generated_by': 'Codex / gpt-6-astra', 'source_sha256': digest(original),
            'candidate_sha256': digest(candidate), 'limit_floor': limit, 'changes': changes,
            'dry_run': dry_run, 'activation': False, 'production_rollback_ready': False}
    if dry_run:
        return plan
    check_binary(binary)
    output.mkdir(mode=0o700)
    try:
        for name, data in [('original.yaml', original), ('candidate.yaml', candidate)]:
            path = output / name
            with path.open('xb') as handle:
                path.chmod(0o600)
                handle.write(data)
            CommandValidator(binary, timeout=5)(path)
        plan['pinned_binary_sha256'] = PINNED_SHA256
        plan['validated_original_and_candidate'] = True
        (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
        (output / 'plan.json').chmod(0o600)
    except BaseException:
        shutil.rmtree(output)
        raise
    return plan


def rollback_candidate(stage, current, output, dry_run):
    """Verify the retained exact bytes; never replace an active configuration."""
    plan = json.loads((stage / 'plan.json').read_text())
    original = (stage / 'original.yaml').read_bytes()
    if digest(original) != plan['source_sha256'] or digest(current.read_bytes()) != plan['candidate_sha256']:
        raise HarnessError("rollback backup or current candidate identity differs")
    if output.exists() or output.is_symlink() or output.resolve() in (current.resolve(), (stage / 'original.yaml').resolve()):
        raise HarnessError("rollback output must be a new separate file")
    if not dry_run:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(original)
    return {'dry_run': dry_run, 'restored_candidate_sha256': digest(original), 'active_config_modified': False}


class Gate:
    def __init__(self, count):
        self.count = count
        self.changed = threading.Condition()
        self.release = threading.Event()
        self.attempted = set()
        self.arrived = set()
        self.active = set()
        self.peak = 0
        self.results = {}
        self.startup_marker = None

    def enter(self, request_id):
        with self.changed:
            if request_id not in self.attempted or request_id in self.arrived:
                raise HarnessError("unknown or duplicate fixture request")
            self.arrived.add(request_id)
            self.active.add(request_id)
            self.peak = max(self.peak, len(self.active))
            self.changed.notify_all()

    def finish(self, request_id, result):
        with self.changed:
            self.results[request_id] = result
            self.changed.notify_all()

    def wait_adjudicated(self, deadline):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.arrived | self.results.keys()) == self.count,
                                         timeout=deadline.remaining())


def backend_handler(gate, deadline):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            if gate.startup_marker is not None and (not gate.startup_marker.exists() or not gate.startup_marker.read_text().strip()):
                self.send_error(503, 'fixture process has not signaled readiness')
                return
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def do_POST(self):
            request_id = None
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.path != '/v1/chat/completions' or not 0 < length <= 65536:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                request_id = body['messages'][0]['content']
                gate.enter(request_id)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"held"}}]}\n\n')
                self.wfile.flush()
                if not gate.release.wait(deadline.remaining()):
                    return
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            except (OSError, ValueError, KeyError, HarnessError):
                return
            finally:
                self.close_connection = True
                with gate.changed:
                    gate.active.discard(request_id)
                    gate.changed.notify_all()
    return Handler


class Backend(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = False


def client(url, request_id, start, gate, deadline, model="fixture"):
    result = {'id': request_id, 'status': 'not_attempted'}
    try:
        start.wait(timeout=deadline.remaining())
        with gate.changed:
            gate.attempted.add(request_id)
        result['started_at'] = timestamp_text()
        body = json.dumps({'model': model, 'stream': True,
                           'messages': [{'role': 'user', 'content': request_id}]}).encode()
        request = urllib.request.Request(url + '/v1/chat/completions', data=body,
                                         headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=deadline.remaining()) as response:
            result['http_status'] = response.status
            result['status'] = 'truncated'
            for line in response:
                if line.strip() == b'data: [DONE]':
                    if result['status'] != 'stream_error':
                        result['status'] = 'completed'
                    break
                if line.startswith(b'data: '):
                    payload = json.loads(line[6:])
                    if isinstance(payload, dict) and 'error' in payload:
                        result['status'] = 'stream_error'
    except urllib.error.HTTPError as exc:
        result.update(http_status=exc.code, status='rejected' if exc.code == 429 else 'http_error')
        with exc:
            try:
                result['response_body'] = exc.read(4096).decode('utf-8', errors='replace')
            except (OSError, http.client.HTTPException) as error:
                result['body_read_error'] = type(error).__name__
    except (TimeoutError, threading.BrokenBarrierError):
        result['status'] = 'timeout' if request_id in gate.attempted else 'not_attempted'
    except (OSError, ValueError, http.client.HTTPException) as exc:
        result.update(status='truncated' if result.get('http_status') == 200 else 'transport_error', error_type=type(exc).__name__)
    finally:
        result['ended_at'] = timestamp_text()
        gate.finish(request_id, result)


def counts(results):
    categories = ('completed', 'rejected', 'timeout', 'http_error', 'transport_error', 'stream_error', 'truncated', 'not_attempted')
    return {name: sum(row['status'] == name for row in results) for name in categories}


def run_worker(settings, directory):
    deadline = Deadline(settings['deadline_seconds'])
    gate = Gate(settings['requests'])
    server = None
    evidence = {'generated_by': 'Codex / gpt-6-astra', 'status': 'failed', 'started_at': timestamp_text(),
                'scope': 'real pinned llama-swap; synthetic controlled backend; no vLLM/GPU/production',
                'upstream_commit': PINNED_COMMIT, 'binary_sha256': PINNED_SHA256,
                'configured_limit': settings['limit'], 'expected_default_limit': 10,
                'request_count': settings['requests'], 'signal_reload_count': 0}
    try:
        check_binary(settings['binary'])
        gate.startup_marker = directory / 'model-starts.txt'
        model = 'fixture-' + uuid.uuid4().hex
        evidence['model'] = model
        server = Backend(('127.0.0.1', 0), backend_handler(gate, deadline))
        serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        serving.start()
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        url = 'http://127.0.0.1:' + str(port)
        command = shlex.join([sys.executable, '-c',
            "import os,sys,time;open(sys.argv[1],'a').write(str(os.getpid())+'\\n');time.sleep(max(0,float(sys.argv[2])-time.monotonic()))",
            str(directory / 'model-starts.txt'), str(deadline.end)])
        config = {'globalTTL': 0, 'healthCheckTimeout': 10, 'store': {'path': str(directory / 'activity.sqlite')},
                  'models': {model: {'cmd': command, 'proxy': 'http://127.0.0.1:' + str(server.server_port)}}}
        if settings['limit'] is not None:
            config['models'][model]['concurrencyLimit'] = settings['limit']
        path = directory / 'config.yaml'
        path.write_text(json.dumps(config))
        evidence['config_sha256'] = digest(path.read_bytes())
        CommandValidator(settings['binary'], timeout=deadline.timeout(5))(path)
        with (directory / 'swap.log').open('w') as log:
            process = subprocess.Popen([settings['binary'], '-config', str(path), '-listen', '127.0.0.1:' + str(port)],
                                       cwd=directory, stdout=log, stderr=log)
        evidence['owned_swap_pid'] = process.pid
        evidence['loopback_ports'] = {'swap': port, 'backend': server.server_port}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        while True:
            if process.poll() is not None:
                raise HarnessError('owned swap failed before readiness')
            try:
                with opener.open(url + '/v1/models', timeout=deadline.timeout(1)) as response:
                    if any(item['id'] == model for item in json.load(response)['data']):
                        break
            except (OSError, ValueError, KeyError):
                pass
            threading.Event().wait(deadline.timeout(.1))
        start = threading.Barrier(settings['requests'] + 1)
        workers = [threading.Thread(target=client, args=(url, 'req-%03d' % index, start, gate, deadline, model), daemon=True)
                   for index in range(settings['requests'])]
        for thread in workers:
            thread.start()
        evidence['wave_started_at'] = timestamp_text()
        start.wait(timeout=deadline.remaining())
        if not gate.wait_adjudicated(deadline):
            raise HarnessError('not every request reached backend or a terminal outcome before deadline')
        with gate.changed:
            evidence['before_release'] = {'at': timestamp_text(), 'attempted': len(gate.attempted),
                'arrived': len(gate.arrived), 'held_active': len(gate.active),
                'terminal': counts(list(gate.results.values()))}
        gate.release.set()
        for thread in workers:
            thread.join(timeout=deadline.remaining())
        results = [gate.results.get('req-%03d' % index, {'id': 'req-%03d' % index, 'status': 'timeout'})
                   for index in range(settings['requests'])]
        evidence.update(results=results, outcomes=counts(results), attempted=len(gate.attempted), peak_backend_active=gate.peak)
        effective = settings['limit'] or 10
        admitted = min(settings['requests'], effective)
        expected = {'completed': admitted, 'rejected': settings['requests'] - admitted}
        evidence['expected_outcomes'] = expected
        evidence['single_model_start_count'] = len((directory / 'model-starts.txt').read_text().splitlines())
        evidence['status'] = 'ok' if (evidence['attempted'] == settings['requests']
            and evidence['before_release']['held_active'] == admitted and gate.peak == admitted
            and evidence['single_model_start_count'] == 1
            and all(evidence['outcomes'][key] == value for key, value in expected.items())
            and not any(value for key, value in evidence['outcomes'].items() if key not in expected)) else 'failed'
    except (OSError, ValueError, HarnessError, ValidationError, RuntimeError, threading.BrokenBarrierError) as exc:
        evidence['failure'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        gate.release.set()
        with gate.changed:
            evidence.setdefault('attempted', len(gate.attempted))
            evidence.setdefault('results', [gate.results.get('req-%03d' % index,
                {'id': 'req-%03d' % index, 'status': 'timeout' if 'req-%03d' % index in gate.attempted else 'not_attempted'})
                for index in range(settings['requests'])])
            evidence.setdefault('outcomes', counts(evidence['results']))
        evidence['ended_at'] = timestamp_text()
        (directory / 'result.json').write_text(json.dumps(evidence))
        if server is not None:
            server.shutdown()
            server.server_close()
    return evidence


def bounded_run(settings, output):
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise HarnessError('Linux pidfd-enabled Python is required before creating workers')
    fd = os.pidfd_open(os.getpid()); os.close(fd)
    deadline = Deadline(settings['deadline_seconds'])
    reserve = min(2, settings['deadline_seconds'] / 4)
    directory = Path(tempfile.mkdtemp(prefix='llmsvc-concurrency-'))
    process = None
    result = {'status': 'deadline_exceeded', 'request_count': settings['requests']}
    timed_out = False
    try:
        path = directory / 'worker.json'
        path.write_text(json.dumps(dict(settings, deadline_seconds=deadline.remaining() - reserve)))
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=max(.001, deadline.remaining() - reserve))
            if (directory / 'result.json').exists():
                result = json.loads((directory / 'result.json').read_text())
            if process.returncode:
                result['status'] = 'worker_failed'
                if stderr:
                    result['worker_stderr'] = stderr[-2000:]
                result.setdefault('failure', 'worker returned a nonzero status')
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        if process is not None:
            kill_owned_session(process.pid)
            if process.poll() is None:
                process.communicate(timeout=max(.05, deadline.end - time.monotonic()))
            until = min(deadline.end, time.monotonic() + .5)
            while owned_session_members(process.pid) and time.monotonic() < until:
                time.sleep(.01)
            if owned_session_members(process.pid):
                raise HarnessError('owned process cleanup failed; retained ' + str(directory))
        if timed_out:
            if (directory / 'result.json').exists():
                try:
                    partial = json.loads((directory / 'result.json').read_text())
                    if isinstance(partial, dict):
                        result = partial
                except (ValueError, OSError):
                    pass
            result['status'] = 'deadline_exceeded'
            result['accounting_available'] = 'outcomes' in result
        if (directory / 'swap.log').exists():
            result['private_log'] = (directory / 'swap.log').read_text()[-65536:]
        shutil.rmtree(directory)
    result.setdefault('request_count', settings['requests'])
    result['cleanup'] = {'owned_session_empty': True, 'owned_temp_removed': not directory.exists()}
    result['elapsed_seconds'] = time.monotonic() - deadline.start
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = output / ('concurrency-' + uuid.uuid4().hex + '.json')
    with path.open('x') as handle:
        path.chmod(0o600)
        json.dump(result, handle, indent=2)
    result['output'] = str(path)
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == '--worker':
        path = Path(argv[1])
        return 0 if run_worker(json.loads(path.read_text()), path.parent)['status'] == 'ok' else 2
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    stage = sub.add_parser('prepare')
    stage.add_argument('--source-config', type=Path, required=True)
    stage.add_argument('--output-dir', type=Path, required=True)
    stage.add_argument('--llama-swap-binary', required=True)
    stage.add_argument('--limit', type=int, default=64)
    stage.add_argument('--dry-run', action='store_true')
    rollback = sub.add_parser('rollback-candidate')
    rollback.add_argument('--stage-dir', type=Path, required=True)
    rollback.add_argument('--current-config', type=Path, required=True)
    rollback.add_argument('--output', type=Path, required=True)
    rollback.add_argument('--dry-run', action='store_true')
    smoke = sub.add_parser('measure')
    smoke.add_argument('--llama-swap-binary', required=True)
    smoke.add_argument('--output-dir', type=Path, required=True)
    smoke.add_argument('--limit', default='64', help='positive integer or default (omit override)')
    smoke.add_argument('--requests', type=int, default=32)
    smoke.add_argument('--deadline-seconds', type=float, default=45)
    smoke.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'prepare':
            result = prepare(args.source_config, args.output_dir, args.llama_swap_binary, args.limit, args.dry_run)
        elif args.action == 'rollback-candidate':
            result = rollback_candidate(args.stage_dir, args.current_config, args.output, args.dry_run)
        else:
            limit = None if args.limit == 'default' else int(args.limit)
            if not 1 <= args.requests <= 64 or (limit is not None and limit <= 0):
                raise HarnessError('requests must be1..64 and limit positive or default')
            Deadline(args.deadline_seconds)
            settings = {'binary': str(Path(args.llama_swap_binary).resolve()), 'limit': limit,
                        'requests': args.requests, 'deadline_seconds': args.deadline_seconds}
            result = {'dry_run': True, 'settings': settings, 'would': 'one owned isolated batch; no production reload'} if args.dry_run else bounded_run(settings, args.output_dir)
        print(json.dumps({key: value for key, value in result.items() if key not in ('private_log', 'results')}, indent=2))
        return 0 if result.get('status', 'ok') == 'ok' else 2
    except (OSError, ValueError, HarnessError, ValidationError, yaml.YAMLError) as exc:
        print('concurrency: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
