# Generated-By: Codex / gpt-6-astra
import json
from pathlib import Path

import pytest

from llmsvc.collectors.events import EventSnapshot


def feed(snapshot, kind, data):
    snapshot.feed({'type': kind, 'data': json.dumps(data)})


def test_real_initial_snapshot_and_no_log_retention():
    events = EventSnapshot()
    fixture = Path(__file__).parent / 'fixtures/telemetry/events-real.sse'
    for line in fixture.read_text().splitlines():
        if line.startswith('data:'):
            events.feed(json.loads(line[5:]))
    events.feed({'type': 'logData', 'data': 'arbitrary secret non-JSON log'})
    assert events.complete
    assert events.count('gemma-4-26b-a4b-nvfp4') == 0
    assert events.count('unobserved-model') is None
    assert 'secret' not in str(events.__dict__)


def test_snapshot_updates_idempotent_upsert_remove_and_reconnect():
    events = EventSnapshot()
    feed(events, 'modelStatus', [{'id': 'm', 'state': 'ready'}])
    assert events.count('m') is None
    feed(events, 'inflight', {'operation': 'snapshot', 'requests': [{'id': 'a', 'modelID': 'm'}]})
    assert events.count('m') == 1
    for _ in range(2):
        feed(events, 'inflight', {'operation': 'upsert', 'request': {'id': 'a', 'modelID': 'm'}})
    assert events.count('m') == 1
    feed(events, 'inflight', {'operation': 'remove', 'id': 'a'})
    assert events.count('m') == 0
    reconnected = EventSnapshot()
    assert reconnected.count('m') is None


@pytest.mark.parametrize('payload', [
    {'operation': 'snapshot', 'requests': None},
    {'operation': 'snapshot', 'requests': [{'id': 'a'}]},
    {'operation': 'snapshot', 'requests': [{'id': 'a', 'modelID': 'm'}] * 2},
    {'operation': 'upsert', 'request': {'id': 'a', 'modelID': 'm'}},
])
def test_malformed_or_out_of_order_events_do_not_claim_zero_inflight(payload):
    events = EventSnapshot()
    with pytest.raises(ValueError):
        feed(events, 'inflight', payload)
    assert events.count('m') is None
