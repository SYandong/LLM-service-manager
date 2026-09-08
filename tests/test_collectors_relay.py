# Generated-By: Codex / gpt-6-astra
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmsvc.collectors.relay import DataPlaneEventBuffer, DataPlaneEventRelay
from llmsvc.collectors.subscription import InflightSubscription


def model_status(state='starting', **extra):
    return {'type': 'modelStatus', 'data': json.dumps([{'id': 'm', 'state': state, **extra}])}


def frame(envelope):
    return b'event:message\ndata:' + json.dumps(envelope).encode() + b'\n\n'


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('condition timed out')
        time.sleep(.005)


def test_only_allowlisted_summary_fields_are_retained():
    buffer = DataPlaneEventBuffer(['m'], clock=lambda: 123)
    buffer.observe({'type': 'logData', 'data': 'SECRET raw logs are not even JSON'})
    buffer.observe({'type': 'profileChanged', 'data': {'active': 'SECRET'}})
    buffer.observe(model_status(name='SECRET', description='SECRET', request={'body': 'SECRET'}))
    buffer.observe({'type': 'modelStatus', 'data': [{'id': 'SECRET', 'state': 'ready'}]})
    result = buffer.drain()
    assert 'SECRET' not in json.dumps(result)
    assert result['events'] == [{'kind': 'data_plane_state', 'model': 'm', 'detail': {
        'source': 'llama-swap', 'trusted_for_quiet': False, 'received_at': 123, 'state': 'starting',
    }}]
    assert result['dropped_by_reason'] == {'unlisted_model': 1}
    assert result['upstream_loss_unknown'] is True


def test_state_dedup_reconnect_and_raw_stopped_semantics():
    buffer = DataPlaneEventBuffer(['m'])
    buffer.observe(model_status())
    buffer.observe(model_status())
    buffer.observe(model_status('ready'))
    buffer.observe(model_status('stopped'))
    events = buffer.drain()['events']
    assert [e['detail']['state'] for e in events] == ['starting', 'ready', 'stopped']
    assert {e['kind'] for e in events} == {'data_plane_state'}  # Never inferred daemon sleep/stop.
    buffer.connection('connected')
    buffer.observe(model_status('stopped'))
    assert buffer.drain()['events'][-1]['detail']['state'] == 'stopped'


def test_overflow_is_bounded_and_diagnostic_not_starved_by_small_drains():
    buffer = DataPlaneEventBuffer(['m'], capacity=2)
    for count in range(5):
        buffer.inflight(count, 'upsert')
    first = buffer.drain(1)
    assert first['events'][0]['detail']['count'] == 0
    assert first['dropped'] == 3
    assert first['dropped_by_reason'] == {'buffer_full': 3}
    second = buffer.drain(1)
    assert second['events'][0]['detail']['count'] == 1
    assert second['dropped'] == 0
    buffer.inflight(4, 'upsert')  # Retry current observation after space is available.
    assert buffer.drain()['events'][0]['detail']['count'] == 4


def test_invalid_or_oversized_model_payload_emits_only_fixed_error_summary():
    buffer = DataPlaneEventBuffer(['m'])
    for payload in ('{SECRET', [{'id': 'm', 'state': 'SECRET'}], [{}], [{}] * 1025):
        buffer.observe({'type': 'modelStatus', 'data': payload})
    result = buffer.drain()
    assert result['dropped_by_reason'] == {'invalid_event': 4}
    assert all(e['detail']['reason'] == 'invalid_event' for e in result['events'])
    assert 'SECRET' not in json.dumps(result)


def test_returned_items_and_discard_counts_are_detached():
    buffer = DataPlaneEventBuffer(['m'], capacity=1)
    buffer.inflight(0, 'snapshot')
    buffer.inflight(1, 'upsert')
    first = buffer.drain()
    first['events'][0]['detail']['count'] = 99
    first['dropped_by_reason']['buffer_full'] = 99
    buffer.inflight(2, 'upsert')
    second = buffer.drain()
    assert second['events'][0]['detail']['count'] == 2
    assert second['dropped'] == 0


def test_full_ui_buffer_does_not_block_or_change_untrusted_quiet_callbacks():
    calls, heartbeats = [], []
    buffer = DataPlaneEventBuffer(['m'], capacity=1)
    sub = InflightSubscription('http://unused', lambda n, *, connected: calls.append((n, connected)),
                               on_heartbeat=lambda: heartbeats.append(True), event_buffer=buffer)
    envelopes = [model_status(), {'type': 'inflight', 'data': {'operation': 'snapshot', 'requests': []}},
                 {'type': 'inflight', 'data': {'operation': 'upsert', 'request': {'id': '1', 'model': 'm'}}},
                 {'type': 'inflight', 'data': {'operation': 'remove', 'id': '1'}}]
    with pytest.raises(ConnectionError):
        sub._consume(io.BytesIO(b''.join(frame(e) for e in envelopes) + b': heartbeat\n\n'))
    assert calls == [(0, False), (1, False), (0, False)]
    assert heartbeats == []
    assert buffer.drain()['dropped'] == 3


@pytest.mark.parametrize('capacity', [0, True, 4097])
def test_invalid_capacity_is_rejected(capacity):
    with pytest.raises(ValueError):
        DataPlaneEventBuffer(['m'], capacity=capacity)


@pytest.mark.parametrize('names', ['m', ['bad\nname'], ['x' * 257], list(map(str, range(1025)))])
def test_model_allowlist_must_be_bounded_and_safe(names):
    with pytest.raises(ValueError):
        DataPlaneEventBuffer(names)


def test_ui_wrapper_does_not_expose_trusted_mode():
    relay = DataPlaneEventRelay('http://unused', ['m'])
    assert relay.subscription.ordered_source is False
    with pytest.raises(TypeError):
        DataPlaneEventRelay('http://unused', ['m'], ordered_source=True)


def test_reconnect_and_close_reuse_existing_transport_without_raw_errors():
    calls = []
    second_opened = threading.Event()

    class Stream(io.BytesIO):
        def __init__(self):
            super().__init__(frame(model_status('ready')))

    def open_stream():
        calls.append(True)
        if len(calls) == 1:
            raise OSError('SECRET transport detail')
        second_opened.set()
        return Stream()

    relay = DataPlaneEventRelay('http://unused', ['m'], stream_factory=open_stream,
                               timeout=.1, reconnect_delay=.01)
    events = []
    relay.start()
    try:
        assert second_opened.wait(2)
        def received_state():
            events.extend(relay.drain(256)['events'])
            return any(e['kind'] == 'data_plane_state' for e in events)
        wait_for(received_state)
    finally:
        relay.close()
    events.extend(relay.drain(256)['events'])
    assert any(e['kind'] == 'data_plane_error' for e in events)
    assert any(e['kind'] == 'data_plane_state' for e in events)
    assert events[-1]['detail'].get('status') == 'closed'
    assert 'SECRET' not in json.dumps(events)
    assert not relay.subscription._thread.is_alive()


def test_real_loopback_stream_to_bounded_drain_and_shutdown():
    sent = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/api/events'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(frame({'type': 'logData', 'data': 'SECRET'}))
            self.wfile.write(frame(model_status('starting')))
            self.wfile.write(frame(model_status('ready')))
            self.wfile.flush()
            sent.set()
            release.wait(2)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = DataPlaneEventRelay('http://127.0.0.1:' + str(server.server_port), ['m'], timeout=1)
    relay.start()
    events = []
    try:
        assert sent.wait(2)
        def received():
            events.extend(relay.drain()['events'])
            return len([e for e in events if e['kind'] == 'data_plane_state']) == 2
        wait_for(received)
        relay.close()
        events.extend(relay.drain()['events'])
        assert [e['detail']['state'] for e in events if e['kind'] == 'data_plane_state'] == ['starting', 'ready']
        assert all(e['detail']['trusted_for_quiet'] is False for e in events)
        assert 'SECRET' not in json.dumps(events)
        assert not relay.subscription._thread.is_alive()
    finally:
        relay.close()
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_source_derived_trace_relays_summaries_without_request_data():
    from pathlib import Path
    fixture = Path(__file__).parent / 'fixtures/telemetry/relay-wire-v252.json'
    envelopes = json.loads(fixture.read_text())['envelopes']
    buffer = DataPlaneEventBuffer(['m'])
    observations = []
    sub = InflightSubscription('http://unused', lambda n, *, connected: observations.append((n, connected)),
                               event_buffer=buffer)
    with pytest.raises(ConnectionError):
        sub._consume(io.BytesIO(b''.join(map(frame, envelopes))))
    batch = buffer.drain()
    assert [e['detail']['state'] for e in batch['events'] if e['kind'] == 'data_plane_state'] == ['starting', 'ready']
    assert [e['detail']['count'] for e in batch['events'] if e['kind'] == 'data_plane_inflight'] == [1, 2, 1, 0]
    assert observations == [(1, False), (2, False), (1, False), (0, False)]
    assert 'PRIVATE_MARKER' not in json.dumps(batch)
    assert '192.0.2.10' not in json.dumps(batch)
    assert batch['dropped'] == 0


def test_stalled_consumer_after_drain_does_not_hold_producer_lock():
    buffer = DataPlaneEventBuffer(['m'], capacity=2)
    buffer.observe(model_status())
    action_lock = threading.Lock()
    drained = threading.Event()
    published = []
    action_lock.acquire()

    def consumer():
        batch = buffer.drain()
        drained.set()
        with action_lock:
            published.extend(batch['events'])

    thread = threading.Thread(target=consumer, daemon=True)
    thread.start()
    try:
        assert drained.wait(2)
        buffer.observe(model_status('ready'))
        assert buffer.drain()['events'][0]['detail']['state'] == 'ready'
        assert not published
    finally:
        action_lock.release()
        thread.join(2)
    assert not thread.is_alive()
    assert published[0]['detail']['state'] == 'starting'
