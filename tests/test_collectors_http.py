# Generated-By: Codex / gpt-6-astra
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmsvc.collectors.probes import Probes


@pytest.fixture
def http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(503)
                self.end_headers()
                return
            if self.path == '/api/events':
                payloads = [
                    {'type': 'logData', 'data': 'sensitive logs discarded'},
                    {'type': 'modelStatus', 'data': json.dumps([{'id': 'm', 'state': 'ready'}])},
                    {'type': 'inflight', 'data': json.dumps({'operation': 'snapshot'})},
                ]
                data = ''.join('event:message\ndata:' + json.dumps(p) + '\n\n' for p in payloads).encode()
            elif self.path == '/is_sleeping':
                data = b'{"is_sleeping":true}'
            else:
                data = b'{"running":[]}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_read_only_http_probes_and_sse_snapshot(http_server):
    probes = Probes(http_server)
    assert probes.health(http_server) is False
    assert probes.sleeping(http_server) is True
    assert probes.running() == {}
    events = probes.events()
    assert events.states == {'m': 'ready'}
    assert events.count('m') == 0
    assert 'sensitive' not in str(events.__dict__)
