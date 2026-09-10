# Generated-By: Codex / gpt-6-astra
"""Exact wildcard argv and owned literal probes; no systemd or model execution."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from deploy.maintenance_executor import ExecutorError, ScopeInspector, digest
from deploy.maintenance_native import NativeAdapter, NativeHTTP
from test_deploy_maintenance_native import native


def configured(native, host, origins=None, port=54321):
    adapter, _, _ = native
    profile = dict(adapter.profile, listen_host=host, listen_port=port)
    if origins is not None:
        profile['native_origin'] = origins[0]
        profile['native_probe_origins'] = origins
    return NativeAdapter(profile, adapter.profile_path)


def test_empty_host_requires_explicit_literal_probe_contract(native):
    with pytest.raises(ExecutorError, match='wildcard_probe_origins_required'):
        configured(native, '')
    adapter = configured(native, '', ['http://127.0.0.1:54321', 'http://[::1]:54321'])
    assert adapter.profile['listen_host'] == ''
    assert adapter.native_probe_origins == ('http://127.0.0.1:54321', 'http://[::1]:54321')


@pytest.mark.parametrize('host,origins,error', [
    ('localhost', ['http://127.0.0.1:54321'], 'literal_or_empty'),
    ('', ['http://localhost:54321'], 'literal_http'),
    ('', ['http://0.0.0.0:54321'], 'not_local'),
    ('', ['http://[::]:54321'], 'not_local'),
    ('', ['http://192.0.2.1:54321'], 'not_local'),
    ('', ['http://127.0.0.1:54321', 'http://[::1]:54322'], 'probe_port'),
    ('', ['http://127.0.0.1:54321', 'http://127.0.0.1:54321/'], 'duplicate'),
    ('0.0.0.0', ['http://[::1]:54321'], 'address_family'),
    ('127.0.0.1', ['http://127.0.0.2:54321'], 'not_local'),
    ('', ['http://user:secret@127.0.0.1:54321'], 'literal_http'),
])
def test_probe_profiles_reject_ambiguous_or_incompatible_destinations(native, host, origins, error):
    with pytest.raises(ExecutorError, match=error):
        configured(native, host, origins)


@pytest.mark.parametrize('host,origin,argument', [
    ('', 'http://127.0.0.1:54321', ':54321'),
    ('127.0.0.1', 'http://127.0.0.1:54321', '127.0.0.1:54321'),
    ('::', 'http://[::1]:54321', '[::]:54321'),
    ('::1', 'http://[::1]:54321', '[::1]:54321'),
])
def test_exact_listen_spelling_and_image_are_not_normalized(native, tmp_path, host, origin, argument):
    adapter = configured(native, host, [origin])
    proc = tmp_path/'proc'; process = proc/'42'; process.mkdir(parents=True)
    (process/'exe').write_bytes(b'pinned-image')
    adapter.profile['native_binary_sha256'] = hashlib.sha256(b'pinned-image').hexdigest()
    adapter.proc = proc
    argv = ['native', '-config', str(adapter.config), '-listen', argument]
    def write(values):
        (process/'cmdline').write_bytes(b'\0'.join(v.encode() for v in values)+b'\0')
    write(argv); adapter.native_image(42)
    write(argv[:-1]+['0.0.0.0:54321' if argument != '0.0.0.0:54321' else ':54321'])
    with pytest.raises(ExecutorError, match='listener_argument_unbound'):
        adapter.native_image(42)
    write(argv+['-listen', argument])
    with pytest.raises(ExecutorError, match='listener_argument_unbound'):
        adapter.native_image(42)
    write(argv); (process/'exe').write_bytes(b'replaced-image')
    with pytest.raises(ExecutorError, match='image_unpinned'):
        adapter.native_image(42)


def test_every_configured_family_is_required_without_fallback(native, monkeypatch):
    adapter = configured(native, '', ['http://127.0.0.1:54321', 'http://[::1]:54321'])
    adapter.listener_owned = lambda *a: True
    primary_calls = []
    adapter.http.snapshot = lambda *a: primary_calls.append(True)
    def unavailable(*a):
        raise OSError('IPv6 unavailable in this fixture')
    monkeypatch.setattr(NativeHTTP, 'snapshot', unavailable)
    with pytest.raises(OSError, match='IPv6 unavailable'):
        adapter._native_snapshot('/owned', time.monotonic()+1)
    assert primary_calls == []  # No successful-primary fallback hides the failure.


def test_listener_replacement_during_probe_is_not_confirmed(native):
    adapter = configured(native, '', ['http://127.0.0.1:54321'])
    ownership = iter([True, False])
    adapter.listener_owned = lambda *a: next(ownership)
    adapter.http.snapshot = lambda *a: SimpleNamespace(states={}, requests={})
    with pytest.raises(ExecutorError, match='listener_changed_during_probe'):
        adapter._native_snapshot('/owned', time.monotonic()+1)


def test_source_instance_change_after_probes_is_rejected(native, monkeypatch):
    adapter, scope, _ = native
    identity = {'pid': 42, 'start_ticks': '13', 'scope_sha256': digest(scope)}
    calls = []
    def inspect(*a):
        calls.append(True)
        current = identity if len(calls) == 1 else dict(identity, start_ticks='14')
        return {'identity': current, 'scope': scope, 'actors': [identity]}
    monkeypatch.setattr(ScopeInspector, 'inspect', inspect)
    adapter.show = lambda *a: {'Restart': 'no'}
    adapter.native_image = lambda *a: None
    adapter._native_snapshot = lambda *a: SimpleNamespace(states={}, requests={})
    adapter.process = lambda *a: {'pid': 42, 'start_ticks': '13'}
    with pytest.raises(ExecutorError, match='source_changed_during_observation'):
        adapter.inspect_native({'accounts': []}, time.monotonic()+1)


SERVER = r'''
import http.server,json,socket,sys
family=int(sys.argv[1]); dual=sys.argv[2]=='dual'; wildcard=sys.argv[2]!='single'
class Server(http.server.HTTPServer):
    address_family=socket.AF_INET6 if family==6 else socket.AF_INET
    def server_bind(self):
        if family==6:self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0 if dual else 1)
        super().server_bind()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path=='/api/events'
        self.send_response(200); self.end_headers()
        for row in [{'type':'modelStatus','data':[]},{'type':'inflight','data':{'operation':'snapshot'}}]:
            self.wfile.write(('data: '+json.dumps(row)+'\n\n').encode())
        self.wfile.flush()
    def log_message(self,*a):pass
try:server=Server(('::' if wildcard and family==6 else '::1' if family==6 else '127.0.0.1',0),Handler)
except OSError as exc:
    print(json.dumps({'errno':exc.errno}),flush=True);sys.exit(2)
print(json.dumps({'port':server.server_address[1]}),flush=True)
server.serve_forever()
'''


@contextlib.contextmanager
def server(family, dual=False, wildcard=False):
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', SERVER, str(family),
                                'dual' if dual else 'wildcard-v6only' if wildcard else 'single'], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert select.select([process.stdout], [], [], 5)[0], 'fixture startup deadline'
        line = process.stdout.readline(); assert line, process.stderr.read()
        ready = json.loads(line)
        if 'errno' in ready:
            if family == 6 and ready['errno'] in (97, 99, 92):
                pytest.skip('IPv6 unavailable on this host; no network settings changed')
            pytest.fail('listener fixture failed errno='+str(ready['errno']))
        yield process, ready['port']
    finally:
        if process.poll() is None: process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
        process.stdout.close(); process.stderr.close()


@pytest.mark.parametrize('family,dual', [(4, False), (6, False), (6, True)])
def test_real_owned_loopback_families_and_foreign_scope_rejection(native, family, dual):
    with server(family, dual) as (process, port):
        origins = (['http://127.0.0.1:'+str(port), 'http://[::1]:'+str(port)] if dual else
                   [('http://[::1]:' if family==6 else 'http://127.0.0.1:')+str(port)])
        adapter = configured(native, '' if dual else '::1' if family==6 else '127.0.0.1', origins, port)
        adapter.members = lambda *a: [process.pid]
        # Actual /proc socket inodes, actual temporary child, actual HTTP frames.
        assert adapter._native_snapshot('/owned-fixture', time.monotonic()+3).complete
        assert not adapter._address_available()
        adapter.members = lambda *a: [os.getpid()]
        with pytest.raises(ExecutorError, match='listener_not_owned'):
            adapter._native_snapshot('/foreign-fixture', time.monotonic()+3)
    assert adapter._address_available()  # Only after the exact temporary child exits.


def test_v6only_socket_does_not_certify_ipv4_even_with_owned_inode(native):
    with server(6, False, True) as (process, port):
        adapter = configured(native, '', ['http://127.0.0.1:'+str(port), 'http://[::1]:'+str(port)], port)
        adapter.members = lambda *a: [process.pid]
        assert adapter.listener_owned(adapter.profile['native_origin'], '/owned-fixture', time.monotonic()+3)
        with pytest.raises(OSError):
            adapter._native_snapshot('/owned-fixture', time.monotonic()+3)


def test_empty_wildcard_reserves_both_families_without_changing_native_options(native, monkeypatch):
    import errno
    adapter = configured(native, '', ['http://127.0.0.1:54321', 'http://[::1]:54321'])
    sockets = []
    class Bound:
        def __init__(self, family, kind):
            self.family = family; self.closed = False; self.bound = None; sockets.append(self)
        def setsockopt(self, *args): pass
        def bind(self, address):
            self.bound = address
            if self.family == socket.AF_INET6:
                assert sockets[0].bound == ('0.0.0.0', 54321) and not sockets[0].closed
                raise OSError(errno.EADDRINUSE, 'foreign IPv6 listener')
        def close(self): self.closed = True
    monkeypatch.setattr(socket, 'socket', Bound)
    assert not adapter._address_available()
    assert len(sockets) == 2 and all(s.closed for s in sockets)


@pytest.mark.parametrize('require_ipv6', [False, True])
def test_unavailable_family_does_not_satisfy_a_required_probe(native, monkeypatch, require_ipv6):
    import errno
    origins = ['http://127.0.0.1:54321']
    if require_ipv6: origins.append('http://[::1]:54321')
    adapter = configured(native, '', origins)
    class Bound:
        def __init__(self, family, kind):
            if family == socket.AF_INET6: raise OSError(errno.EAFNOSUPPORT, 'IPv6 unavailable')
        def setsockopt(self, *args): pass
        def bind(self, address): pass
        def close(self): pass
    monkeypatch.setattr(socket, 'socket', Bound)
    assert adapter._address_available() is (not require_ipv6)
