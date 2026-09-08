# Generated-By: Codex / gpt-6-astra
"""Actual current reserve preview/405 UI; live envelope uses a seeded intent fixture."""

import asyncio
import threading
from dataclasses import replace

import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from llmsvc.state import Reserve
from test_llm_reserve import ARGS, reserve_args, reserve_service, live_receipt
from test_llm_actions import action_service, pin_api, pin_service
from test_tui_actions import app_for, output
from test_tui_pin import submit
from test_tui_teardown import TeardownApp


@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
def test_actual_preview_refreshes_without_allocating_or_writing(pin_api, reserve_service, monkeypatch, size):
    async def scenario():
        service = reserve_service
        app = app_for(pin_api, service)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            before = service.database.read_bytes(), service.effects['collects']
            def forbidden(*args, **kwargs):
                pytest.fail('preview attempted a persistent/transport write')
            monkeypatch.setattr(service.store, '_write', forbidden)
            monkeypatch.setattr(service.scheduler.model_actions.transport, 'stop_unit', forbidden)
            await submit(app, pilot, ' '.join(ARGS)+' --dry-run')
            assert 'no reservation ID allocated' in output(app) and 'hypothetical' in output(app)
            assert app.snapshot['reserves'] == []
            assert 'reserved for placement' not in str(app.dashboard.query_one('#gpus', Static).render())
            assert (service.database.read_bytes(), service.effects['collects']) == before
            posts = [r for r in service.requests if r[0] != 'GET']
            assert posts == [('POST', '/v1/reserve?dry_run=1')]
            index = service.requests.index(posts[0])
            assert ('GET', '/v1/state') in service.requests[index+1:]
            assert not service.effects['calls']
    asyncio.run(scenario())


def test_current_405_and_invalid_input_leave_no_reservation(pin_api, reserve_service):
    async def scenario():
        reserve_service.scheduler.config = replace(reserve_service.scheduler.config, read_only=True)
        app = app_for(pin_api, reserve_service)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            before = reserve_service.database.read_bytes()
            await submit(app, pilot, 'reserve --gpu 0 --size 80G')
            assert '--for' in output(app)
            assert not [r for r in reserve_service.requests if r[0] != 'GET']
            await submit(app, pilot, ' '.join(ARGS))
            assert '405' in output(app) and 'read_only' in output(app)
            assert [r for r in reserve_service.requests if r[0] != 'GET'] == [('POST', '/v1/reserve')]
            assert reserve_service.database.read_bytes() == before and app.snapshot['reserves'] == []
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['blocked', 'partial'])
def test_proposed_receipt_fixture_preserves_seeded_intent_and_refreshes_gpu(pin_api, reserve_service, status):
    async def scenario():
        app = app_for(pin_api, reserve_service)
        reply = live_receipt(status)
        original = app.client.request
        writes = []
        def reply_fixture(method, path, payload=None, **kwargs):
            if method == 'POST':
                writes.append((path, payload, kwargs))
                # Explicit seeded persisted-state fixture; NOT current core live HTTP.
                reserve_service.store.put_reserve(Reserve(reply['id'], reply['gpu'], reply['size_gb'], reply['until'], reply['by']))
                return reply
            return original(method, path, payload, **kwargs)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = reply_fixture
            await submit(app, pilot, ' '.join(ARGS))
            assert len(writes) == 1 and writes[0][2]['timeout'] == 150
            assert app.snapshot['reserves'][0]['id'] == reply['id']
            assert reserve_service.store.active(pin_api['time'].time())[1][0].id == reply['id']
            text = output(app)
            for expected in ['Reservation saved', 'owner server-owner', 'Evacuation status: '+status, 'evacuation_incomplete']:
                assert expected in text
            assert 'reserved for placement' in str(app.dashboard.query_one('#gpus', Static).render())
            assert not reserve_service.effects['calls']
    asyncio.run(scenario())


def test_delayed_preview_cannot_queue_another_write(pin_api, reserve_service):
    async def scenario():
        app = app_for(pin_api, reserve_service)
        started, release = threading.Event(), threading.Event()
        original = app.client.request
        writes = []
        def delayed(method, path, payload=None, **kwargs):
            if method == 'POST':
                writes.append(path)
                started.set()
                assert release.wait(5)
            return original(method, path, payload, **kwargs)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = delayed
            pending = app.run_write(reserve_args(pin_api, '--dry-run'))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.run_write(reserve_args(pin_api, '--dry-run')).wait()
                assert 'already running' in output(app)
            finally:
                release.set()
            await pending.wait()
            assert writes == ['/v1/reserve?dry_run=1']
    asyncio.run(scenario())


def test_preview_reply_during_teardown_does_not_redraw(pin_api, reserve_service):
    async def scenario():
        app = app_for(pin_api, reserve_service, TeardownApp)
        started, release = threading.Event(), threading.Event()
        original = app.client.request
        def delayed(method, path, payload=None, **kwargs):
            result = original(method, path, payload, **kwargs)
            if method == 'POST':
                started.set()
                assert release.wait(5)
            return result
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            before = app.snapshot
            app.client.request = delayed
            pending = app.run_write(reserve_args(pin_api, '--dry-run'))
            assert await asyncio.to_thread(started.wait, 2)
            async def boundary():
                release.set()
                await pending.wait()
                assert app.snapshot is before and not app._write_busy
            app.at_teardown = boundary
        assert not reserve_service.scheduler.snapshot().reserves
    asyncio.run(scenario())
