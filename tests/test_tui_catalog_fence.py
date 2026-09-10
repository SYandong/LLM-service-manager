# Generated-By: Codex / gpt-6-astra
"""Shared registry formatter consumes actual durable catalog failure in the UI."""
import asyncio
from types import SimpleNamespace

import pytest
pytest.importorskip('textual')
from tui.app import SchedulerApp
from test_llm_catalog_fence import api, catalog, registry_catalog, registry_fixture, published_fence, retained_state
from test_tui import IdleEvents
from test_tui_actions import output
from test_tui_pin import submit


@pytest.mark.parametrize('size', [(100, 30), (40, 24)])
def test_catalog_global_fence_remains_visible_in_actual_registry_command(api, published_fence, size):
    async def scenario():
        c = published_fence
        before = retained_state(c)
        client = api['SchedulerClient']('http://%s:%s' % c.address)
        calls = []
        request = client.request
        def tracked(method, path, *args, **kwargs):
            calls.append((method, path))
            return request(method, path, *args, **kwargs)
        client.request = tracked
        app = SchedulerApp(client, SimpleNamespace(**api), event_reader=IdleEvents())
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app, pilot, 'registry')
            text = output(app)
            assert 'Queue/config fence: no' in text
            global_line = text.split('Global blockers: ', 1)[1].split('Queue status', 1)[0]
            assert 'catalog_reconciliation_required' in global_line
            assert 'not global action readiness' in text
            assert '"status": "applied"' in text
            assert '\nFenced: no' not in text
        assert calls.count(('GET', '/v1/registry')) == 1
        assert all(method == 'GET' for method, _ in calls)
        assert retained_state(c) == before
    asyncio.run(scenario())
