# Generated-By: Codex / gpt-6-astra
"""CPU-only real source HTTP -> core bridge -> actual CLI EventReader.

Native events use the real pin API with a disposable SQLite store. No GPU,
production model endpoint, live free latency or TUI rendering is exercised.
"""

import http.client
import json
import queue
import runpy
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmsvc.__main__ import build_event_relay
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, ModelState, StateSnapshot
from llmsvc.store import IntentStore


PRIVATE = 'TEST_ONLY_PRIVATE_MARKER'


def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('dual-source fixture did not progress')


def request(address, method, path, body=None):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def dual_source(tmp_path):
    outgoing = queue.Queue()
    stopping = threading.Event()
    connections = []

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == '/api/events'
            connections.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            while not stopping.is_set():
                try:
                    item = outgoing.get(timeout=.05)
                except queue.Empty:
                    continue  # No invented source heartbeat, even in this fixture.
                if item is None:
                    return  # A real EOF exercises the existing source reconnect.
                try:
                    self.wfile.write(b'event:message\ndata:' + json.dumps(item).encode() + b'\n\n')
                    self.wfile.flush()
                except OSError:
                    return

    source_server = ThreadingHTTPServer(('127.0.0.1', 0), Source)
    source_thread = threading.Thread(target=lambda: source_server.serve_forever(poll_interval=.01))
    source_thread.start()
    database = tmp_path / 'fixture-intents.sqlite'
    config = SchedulerConfig('127.0.0.1', 1, read_only=False, state_db_path=str(database),
        collectors={'swap_url': 'http://127.0.0.1:' + str(source_server.server_port),
                    'models': {'m': {}}, 'ip_containers': {'127.0.0.1': 'fixture-owner'}},
        data_plane_events_enabled=True, data_plane_event_capacity=64,
        data_plane_event_batch_size=8, data_plane_event_interval_seconds=.01,
        data_plane_event_timeout_seconds=2, data_plane_event_reconnect_seconds=.02,
        event_history_size=512, event_heartbeat_seconds=.05, request_timeout_seconds=2)
    store = IntentStore(database, action_lock=threading.RLock())

    def observed():
        return StateSnapshot(sampled_at=time.time(),
            models=(ModelState('m', state='awake', unit='vllm-m.service', unit_active=True),),
            activity=(Activity('m', last_request_at=time.time(), in_flight=0),))

    relay = build_event_relay(config)
    scheduler = Scheduler(config, observed, store=store, event_relay=relay)
    server = SchedulerHTTPServer(('127.0.0.1', 0), scheduler)
    server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    server_thread.start()
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'cli' / 'llm'))
    client = api['SchedulerClient']('http://127.0.0.1:' + str(server.server_port), timeout=2)
    reader = api['EventReader'](client, stream_timeout=2, retry_delay=.02, queue_size=512)
    scheduler.start()
    reader.start()
    seen = []
    updates = []

    def collect(predicate):
        def enough():
            batch = reader.drain()
            updates.append(batch)
            seen.extend(batch['events'])
            return predicate(seen)
        until(enough)
        return seen

    try:
        until(lambda: bool(connections))
        collect(lambda events: any(e['kind'] == 'data_plane_connection'
                                  and e['detail']['status'] == 'connected' for e in events))
        yield SimpleNamespace(outgoing=outgoing, scheduler=scheduler, relay=relay,
                              reader=reader, seen=seen, updates=updates, collect=collect,
                              address=server.server_address, connections=connections)
    finally:
        try:
            scheduler.stop()
        finally:
            reader.close()
            stopping.set()
            server.shutdown()
            source_server.shutdown()
            server.server_close()
            source_server.server_close()
            server_thread.join(2)
            source_thread.join(2)
            store.close()
        assert not reader.thread.is_alive()
        assert not relay.subscription._thread.is_alive()
        assert not scheduler.event_bridge.thread.is_alive()


def test_source_trace_and_actual_native_pin_reach_cli_reader(dual_source):
    service = dual_source
    wire = Path(__file__).parent / 'fixtures/telemetry/relay-wire-v252.json'
    for envelope in json.loads(wire.read_text())['envelopes']:
        service.outgoing.put(envelope)
    status, pin = request(service.address, 'POST', '/v1/pin',
                          {'model': 'm', 'until': time.time() + 60, 'by': PRIVATE})
    assert status == 200 and pin['by'] == 'fixture-owner'
    service.collect(lambda events: any(e['kind'] == 'pin' for e in events)
                    and [e['detail']['count'] for e in events if e['kind'] == 'data_plane_inflight'
                         and e['detail']['count'] is not None] == [1, 2, 1, 0])
    planes = [e for e in service.seen if e['kind'].startswith('data_plane_')]
    assert [e['detail']['state'] for e in planes if e['kind'] == 'data_plane_state'] == ['starting', 'ready']
    assert all(e['detail']['source'] == 'llama-swap' and e['detail']['trusted_for_quiet'] is False for e in planes)
    assert all(isinstance(e['detail']['received_at'], (float, int)) for e in planes)
    assert PRIVATE not in json.dumps(service.seen)
    assert '192.0.2.10' not in json.dumps(service.seen)
    ids = [e['id'] for e in service.seen]
    assert ids == sorted(set(ids))
    assert all(u['missed'] == 0 and u['dropped'] == 0 for u in service.updates)
    assert service.relay.subscription.ordered_source is False
    assert service.scheduler.model_actions is None


def test_local_filter_diagnostics_do_not_become_client_or_network_loss(dual_source):
    service = dual_source
    service.outgoing.put({'type': 'modelStatus', 'data': [
        {'id': 'not-configured', 'state': 'ready', 'name': PRIVATE}]})
    service.outgoing.put({'type': 'modelStatus', 'data': [{'id': 'm', 'state': PRIVATE}]})

    def both_reasons(events):
        reasons = {}
        for event in events:
            if event['kind'] == 'data_plane_dropped':
                for name, count in event['detail']['dropped_by_reason'].items():
                    reasons[name] = reasons.get(name, 0) + count
        return reasons.get('unlisted_model') == 1 and reasons.get('invalid_event') == 1

    service.collect(both_reasons)
    for event in service.seen:
        if event['kind'] == 'data_plane_dropped':
            detail = event['detail']
            assert detail['dropped'] == sum(detail['dropped_by_reason'].values())
            assert detail['upstream_loss_unknown'] is True
            assert detail['trusted_for_quiet'] is False
    assert all(u['dropped'] == u['missed'] == 0 for u in service.updates)
    assert PRIVATE not in json.dumps(service.seen)
    assert 'not-configured' not in json.dumps(service.seen)


def test_source_reconnect_preserves_scheduler_cursor_and_native_events(dual_source):
    service = dual_source
    state = {'type': 'modelStatus', 'data': [{'id': 'm', 'state': 'stopped'}]}
    service.outgoing.put(state)
    service.collect(lambda events: any(e['kind'] == 'data_plane_state' for e in events))
    old_cursor = service.updates[-1]['cursor']
    service.outgoing.put(None)
    until(lambda: len(service.connections) >= 2)
    service.outgoing.put(state)  # Current source snapshot republished on reconnect.
    status, _ = request(service.address, 'DELETE', '/v1/pin/m')
    assert status == 200
    service.collect(lambda events: len([e for e in events if e['kind'] == 'data_plane_state']) == 2
                    and any(e['kind'] == 'unpin' for e in events))
    planes = [e for e in service.seen if e['kind'] == 'data_plane_state']
    assert planes[-1]['id'] > old_cursor
    assert all(e['kind'] != 'stop' for e in service.seen)
    assert service.scheduler.snapshot().models[0].state == 'awake'
    assert all(u['generation'] == 0 and u['missed'] == 0 and u['dropped'] == 0 for u in service.updates)
    assert [e['id'] for e in service.seen] == sorted({e['id'] for e in service.seen})
