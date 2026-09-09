# Generated-By: Codex / gpt-6-astra
"""Actual registry inspection in the TUI; no verifier, queue worker or writes."""
import asyncio
from types import SimpleNamespace
import pytest
pytest.importorskip('textual')
from tui.app import SchedulerApp
from test_llm_registry_status import inspection,harness,api,client,prohibit_effects
from test_tui import IdleEvents
from test_tui_actions import output
from test_tui_pin import submit


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_fenced_actual_registry_status_is_displayed_without_clearing(api,inspection,monkeypatch,size):
    async def scenario():
        inspection.queue.marker.write_text('{')
        before=inspection.queue.path.read_bytes(),inspection.queue.marker.read_bytes(),inspection.scheduler.events_since(0)
        prohibit_effects(inspection,monkeypatch)
        app=SchedulerApp(client(api,inspection),SimpleNamespace(**api),event_reader=IdleEvents())
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app,pilot,'registry')
            text=output(app)
            assert 'Fenced: yes (retained)' in text
            assert 'reconciliation_required' in text and 'settlement_confirmed' in text
            assert 'process-local monotonic' in text
        assert (inspection.queue.path.read_bytes(),inspection.queue.marker.read_bytes(),inspection.scheduler.events_since(0))==before
    asyncio.run(scenario())
