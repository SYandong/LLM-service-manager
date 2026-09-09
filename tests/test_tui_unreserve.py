# Generated-By: Codex / gpt-6-astra
"""Actual unreserve input/refresh with no model actuator calls."""
import asyncio
import time
import pytest
pytest.importorskip('textual')
from textual.widgets import Static
from llmsvc.state import Reserve
from test_llm_actions import action_service,pin_api,pin_service
from test_tui_actions import app_for,output
from test_tui_pin import submit


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_unreserve_preview_then_delete_refreshes_active_marker(pin_api,action_service,size):
    async def scenario():
        service=action_service
        service.store.put_reserve(Reserve('r',0,80,time.time()+3600,'owner'))
        app=app_for(pin_api,service)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert 'reserved for placement' in str(app.dashboard.query_one('#gpus',Static).render())
            before=service.database.read_bytes()
            await submit(app,pilot,'unreserve r --dry-run')
            assert service.database.read_bytes()==before and app.snapshot['reserves']
            await submit(app,pilot,'unreserve r')
            assert app.snapshot['reserves']==[]
            assert 'reserved for placement' not in str(app.dashboard.query_one('#gpus',Static).render())
            assert 'actor actual-owner' in output(app)
            calls=[r for r in service.requests if r[0]!='GET']
            assert calls==[('DELETE','/v1/reserve/r?dry_run=1',0),('DELETE','/v1/reserve/r',0)]
            index=service.requests.index(calls[-1])
            assert ('GET','/v1/state') in service.requests[index+1:]
            assert service.effects['calls']==[]
    asyncio.run(scenario())
