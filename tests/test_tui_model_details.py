# Generated-By: Codex / gpt-6-astra
"""Headless optional details with actual owner metadata; HTTP mounting is core-owned."""
import asyncio
import pytest
pytest.importorskip('textual')
from test_llm_model_details import api,registry,inventory_reply,preview_reply
from test_tui_models import app_for,snapshot
from test_tui_actions import output
from test_tui_pin import submit


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_owner_inventory_and_plan_render_without_replacing_runtime_state(api,registry,snapshot,size):
    async def scenario():
        owner,queue,weights,*_=registry
        app,client=app_for(api,snapshot)
        original=client.request
        def metadata(method,path,payload=None):
            if path=='/v1/models': return inventory_reply(owner)
            if path=='/v1/models?dry_run=1': return preview_reply(owner,payload)
            return original(method,path,payload)
        client.request=metadata
        before=queue.path.read_bytes(),queue.queue_snapshot()
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app,pilot,'models')
            assert '[permanent; source=config]' in output(app)
            assert 'not global commit readiness' in output(app)
            import shlex
            await submit(app,pilot,'add '+shlex.quote(str(weights))+' --name ft --base base --dry-run')
            text=output(app)
            assert 'not reserved' in text and 'not measured memory' in text
            assert 'hashes are not adoption/settlement' in text and 'inflight_stream_unknown' in text
            assert app.snapshot==snapshot
        assert (queue.path.read_bytes(),queue.queue_snapshot())==before
    asyncio.run(scenario())
