# Generated-By: Codex / gpt-6-astra
"""Registry contract UI fixtures; actual core HTTP tests follow mounted owner handoff."""
import asyncio
import threading
from types import SimpleNamespace
import pytest
pytest.importorskip('textual')
from tui.app import SchedulerApp
from test_tui import FakeClient,IdleEvents,snapshot
from test_llm_events import api
from test_tui_pin import submit
from test_tui_teardown import TeardownApp
from test_tui_actions import output


class RegistryClient(FakeClient):
    def __init__(self,snapshot):
        super().__init__(snapshot)
        self.reply={'would':[{'kind':'add_model','model':'ft','base':'base'}], 'dry_run':True,
                    'config_committed':False,'blocked_by':[{'reason':'registry_writes_disabled'}]}
    def request(self,method,path,payload=None):
        self.calls.append((method,path,payload))
        if path=='/v1/state': return self.snapshot
        if method=='GET':
            return {'records':{'ft':{'name':'ft','base':'base','path':'/shared/ft'}},
                    'writes_enabled':False,'blocked_by':[{'reason':'registry_writes_disabled'}]}
        return self.reply


def app_for(api,snapshot,cls=SchedulerApp):
    client=RegistryClient(snapshot)
    return cls(client,SimpleNamespace(**api),event_reader=IdleEvents()),client


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_list_and_preview_share_parser_and_refresh_without_replacing_runtime_state(api,snapshot,size):
    async def scenario():
        app,client=app_for(api,snapshot)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app,pilot,'models')
            assert 'Temporary model registry' in output(app) and '/shared/ft' in output(app)
            assert app.snapshot==snapshot
            await submit(app,pilot,'add "/shared/path with space" --name ft --base base --dry-run')
            assert 'nothing queued or applied' in output(app) and 'registry_writes_disabled' in output(app)
            post=('POST','/v1/models?dry_run=1',{'name':'ft','path':'/shared/path with space','base':'base'})
            assert post in client.calls
            assert ('GET','/v1/state',None) in client.calls[client.calls.index(post)+1:]
    asyncio.run(scenario())


def test_blocked_http_details_and_queued_outcome_are_visible(api,snapshot):
    async def scenario():
        app,client=app_for(api,snapshot)
        original=client.request
        async with app.run_test(size=(40,24)) as pilot:
            await app.workers.wait_for_complete()
            def unavailable(method,path,payload=None):
                if method=='DELETE':
                    raise api['ClientError']('HTTP409',status=409,payload={'error':'registry_reconciliation_required','config_committed':True})
                return original(method,path,payload)
            client.request=unavailable
            await submit(app,pilot,'rm ft --dry-run')
            assert 'registry_reconciliation_required' in output(app) and 'config_committed' in output(app)
            client.request=original
            client.reply={'id':'job','status':'queued','config_committed':False,'blocked_by':[{'reason':'quiet_unknown'}]}
            await submit(app,pilot,'add /shared/ft --name ft --base base')
            assert 'queued; not applied' in output(app) and 'quiet_unknown' in output(app)
    asyncio.run(scenario())


def test_late_registry_list_is_ignored_after_write_and_during_teardown(api,snapshot):
    async def scenario():
        app,client=app_for(api,snapshot,TeardownApp)
        entered,release=threading.Event(),threading.Event()
        original=client.request
        def held(method,path,payload=None):
            result=original(method,path,payload)
            if path=='/v1/models':
                entered.set()
                assert release.wait(5)
            return result
        async with app.run_test(size=(40,24)) as pilot:
            await app.workers.wait_for_complete()
            client.request=held
            pending=app.show_models(api['build_parser']().parse_args(['models']))
            assert await asyncio.to_thread(entered.wait,2)
            await app.run_write(api['build_parser']().parse_args(['add','/shared/ft','--name','ft','--base','base','--dry-run'])).wait()
            before=output(app)
            async def boundary():
                release.set()
                await pending.wait()
                assert output(app)==before
            app.at_teardown=boundary
    asyncio.run(scenario())
