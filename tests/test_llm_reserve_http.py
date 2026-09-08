# Generated-By: Codex / gpt-6-astra
"""Real mounted #112 API compatibility; core fixtures simulate managed-unit effects."""

import concurrent.futures
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_reserve_http import api as core_reserve_api, evacuation, system
from test_llm_reserve import ARGS, reserve_args, pin_api
from test_llm_pin import command


@pytest.fixture
def mounted_reserve(core_reserve_api, pin_api):
    scheduler, address, controller, state, transport, leases = core_reserve_api
    # Positive integration cases are synchronized by effects/barriers, not a
    # subsecond CI scheduling assumption. Production120s is unchanged.
    scheduler.config = replace(scheduler.config, reserve_timeout_seconds=10, action_observe_seconds=3)
    url = 'http://%s:%s' % address
    client = pin_api['SchedulerClient'](url, timeout=4)
    opener = client.opener
    requests = []
    def tracked(request, **kwargs):
        requests.append((request.get_method(), request.full_url.removeprefix(url)))
        request.add_header('X-Forwarded-For', '192.0.2.33')
        return opener(request, **kwargs)
    client.opener = tracked
    return SimpleNamespace(scheduler=scheduler, controller=controller, state=state, transport=transport,
        leases=leases, url=url, client=client, requests=requests)


def configure_outcome(service, outcome):
    if outcome == 'blocked':
        service.scheduler.config = replace(service.scheduler.config, model_actions_enabled=False)
    elif outcome == 'partial':
        service.state['mode'] = 'exit-error'


@pytest.mark.parametrize('outcome,stopped', [('complete', ['a','b']), ('blocked', []), ('partial', ['a'])])
def test_mounted_client_receipt_owner_outcomes_and_idempotent_delete(pin_api, mounted_reserve, monkeypatch, capsys, outcome, stopped):
    service = mounted_reserve
    configure_outcome(service, outcome)
    monkeypatch.setitem(pin_api['execute_command'].__globals__, 'PIN_COMPATIBILITY_LABEL', 'spoofed-body-owner')
    args = reserve_args(pin_api)
    monkeypatch.setitem(pin_api['main'].__globals__, 'SchedulerClient', lambda **config: service.client)
    code = pin_api['main'](['--url', service.url, *ARGS, '--json'])
    result = json.loads(capsys.readouterr().out)
    assert code == (0 if outcome == 'complete' else 1)
    assert result['by'] == 'actual-owner'
    assert result['evacuation']['status'] == outcome and result['evacuation']['stopped'] == stopped
    assert pin_api['result_exit_code'](args, result) == (0 if outcome == 'complete' else 1)
    assert service.scheduler.store.reserve(result['id']).by == 'actual-owner'
    current = pin_api['execute_command'](command(pin_api, 'status'), service.client)
    assert [record['id'] for record in current['reserves']] == [result['id']]
    text = pin_api['format_result'](args, result)
    for expected in ['Reservation saved', result['id'], 'owner actual-owner', 'Evacuation status: '+outcome]:
        assert expected in text
    assert 'spoofed-body-owner' not in text
    assert service.state['calls'] == stopped
    for name, lease_id in service.leases.items():
        assert service.scheduler.store.lease(lease_id)[0].status == ('released' if name in stopped else 'confirmed')
    for _ in range(2):
        deleted = service.client.request('DELETE', '/v1/reserve/'+result['id'])
        assert deleted == {'id':result['id'], 'by':'actual-owner'}
    assert pin_api['execute_command'](command(pin_api, 'status'), service.client)['reserves'] == []
    assert service.state['calls'] == stopped
    assert [r for r in service.requests if r[0] == 'POST'] == [('POST','/v1/reserve')]


def test_actual_confirmed_preview_keeps_label_and_is_entirely_effect_free(pin_api, mounted_reserve, monkeypatch):
    service = mounted_reserve
    service.scheduler.stop()  # Stop the fixture's sampler, not any external service.
    service.scheduler.config = replace(service.scheduler.config, read_only=True)
    before = Path(service.scheduler.config.state_db_path).read_bytes(), service.scheduler.snapshot(), service.scheduler.events_since(0)
    def forbidden(*args, **kwargs):
        pytest.fail('preview attempted allocation/collection/probe/transport/write')
    monkeypatch.setattr('llmsvc.scheduler.uuid.uuid4', forbidden)
    monkeypatch.setattr(service.scheduler, 'collect', forbidden)
    monkeypatch.setattr(service.scheduler.store, '_write', forbidden)
    monkeypatch.setattr(service.controller.accounting, 'probe', forbidden)
    monkeypatch.setattr(service.transport, 'run', forbidden)
    args = reserve_args(pin_api, '--dry-run')
    result = pin_api['execute_command'](args, service.client)
    assert result['would'][0]['by'] == pin_api['PIN_COMPATIBILITY_LABEL'] and 'id' not in result['would'][0]
    assert [item['kind'] for item in result['would']] == ['reserve','stop','stop']
    assert result['blocked_by'] == [] and pin_api['result_exit_code'](args, result) == 0
    assert (Path(service.scheduler.config.state_db_path).read_bytes(), service.scheduler.snapshot(), service.scheduler.events_since(0)) == before
    assert service.state['calls'] == []


@pytest.mark.parametrize('cancel', ['delete', 'expiry'])
def test_actual_receipt_can_be_inactive_before_response_without_retry(pin_api, mounted_reserve, monkeypatch, cancel):
    service = mounted_reserve
    finished, release = threading.Event(), threading.Event()
    evacuate = service.controller.evacuate
    def held_reply(*args, **kwargs):
        outcome = evacuate(*args, **kwargs)
        finished.set()
        assert release.wait(5)
        return outcome
    monkeypatch.setattr(service.controller, 'evacuate', held_reply)
    args = reserve_args(pin_api)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(pin_api['execute_command'], args, service.client)
        try:
            assert finished.wait(5)
            current = service.client.request('GET', '/v1/state')['reserves'][0]
            if cancel == 'delete':
                service.client.request('DELETE', '/v1/reserve/'+current['id'])
            else:
                service.scheduler.clock = lambda: current['until']+1
            assert service.client.request('GET', '/v1/state')['reserves'] == []
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result['id'] == current['id'] and result['evacuation']['status'] == 'complete'
    assert 'write receipt' in pin_api['format_result'](args, result)
    assert 'check status for current expiry/deletion' in pin_api['format_result'](args, result)
    assert [r for r in service.requests if r[0] == 'POST'] == [('POST','/v1/reserve')]


def test_lost_actual_http_reply_keeps_one_persisted_intent_without_retry(pin_api, mounted_reserve):
    service = mounted_reserve
    configure_outcome(service, 'blocked')
    opener = service.client.opener
    def lose_response(request, **kwargs):
        response = opener(request, **kwargs)
        if request.get_method() == 'POST':
            with response:
                response.read()  # Actual scheduler completed persistence before loss.
            raise TimeoutError('fixture discarded the saved reply')
        return response
    service.client.opener = lose_response
    with pytest.raises(pin_api['ClientError'], match='reservation may have persisted'):
        pin_api['execute_command'](reserve_args(pin_api), service.client)
    saved = service.client.request('GET','/v1/state')['reserves']
    assert len(saved) == 1 and saved[0]['by'] == 'actual-owner'
    assert [r for r in service.requests if r[0] == 'POST'] == [('POST','/v1/reserve')]
    assert service.state['calls'] == []


def test_unleased_preview_remains_blocked_without_inventing_executable_stops(pin_api, mounted_reserve):
    service = mounted_reserve
    for lease_id in service.leases.values():
        service.scheduler.store.transition_lease(lease_id, 'released')
    result = pin_api['execute_command'](reserve_args(pin_api, '--dry-run'), service.client)
    assert [item['kind'] for item in result['would']] == ['reserve']
    assert {item['model'] for item in result['blocked_by'] if item['reason'] == 'unleased_model'} == {'a','b'}
    assert pin_api['result_exit_code'](reserve_args(pin_api, '--dry-run'), result) == 1
    assert not service.scheduler.snapshot().reserves and not service.state['calls']
