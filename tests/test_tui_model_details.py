# Generated-By: Codex / gpt-6-astra
# Generated-By: Claude Code / claude-fable-5-1
"""Headless optional details with actual owner metadata; HTTP mounting is core-owned.

Registration is directory-driven, so the TUI only renders the inventory and the
shared-root discovery rows; there is no add/rm/import plan to preview.
"""
import asyncio
import pytest
pytest.importorskip('textual')
from test_llm_model_details import api,registry,inventory_reply
from test_tui_models import app_for,snapshot
from test_tui_actions import output
from test_tui_pin import submit


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_owner_inventory_renders_without_replacing_runtime_state(api,registry,snapshot,size):
    async def scenario():
        owner,queue,weights,*_=registry
        app,client=app_for(api,snapshot)
        original=client.request
        def metadata(method,path,payload=None):
            if path=='/v1/models': return inventory_reply(owner)
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
            assert 'add' in text.lower() and ('unknown' in text.lower() or 'invalid' in text.lower() or 'usage' in text.lower())
            assert app.snapshot==snapshot
        assert (queue.path.read_bytes(),queue.queue_snapshot())==before
    asyncio.run(scenario())


from types import SimpleNamespace
from tui.app import SchedulerApp
from test_registry_http_preview import mounted,registry_fixture,assert_readonly
from test_tui import IdleEvents


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_actual_http_inventory_and_discovery_details_in_tui(api,mounted,size):
    async def scenario():
        app=SchedulerApp(api['SchedulerClient']('http://%s:%s'%mounted.address),SimpleNamespace(**api),event_reader=IdleEvents())
        before=mounted.files(),mounted.scheduler.events_since(0)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app,pilot,'models')
            text=output(app)
            assert '[permanent; source=config]' in text and '[temporary; source=config]' in text
            assert 'not global commit readiness' in text
            assert 'Shared roots' in text or 'Nothing new under the shared roots' in text
            await submit(app,pilot,'rm ft')
            text=output(app)
            assert 'rm' in text.lower()
            assert '[temporary; source=config]' in output(app) or 'models' in text.lower()
        assert (mounted.files(),mounted.scheduler.events_since(0))==before
        assert_readonly(mounted,before)
    asyncio.run(scenario())
