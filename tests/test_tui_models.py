# Generated-By: Codex / gpt-6-astra
# Generated-By: Claude Code / claude-fable-5-1
"""Registry contract UI fixtures; actual core HTTP tests follow mounted owner handoff.

Registration is directory-driven: `add` / `rm` / `import` are not commands any
more, so typing them must produce the ordinary unknown-command message and no
registry write request.
"""
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
        self.reply={'error':'registry_writes_removed'}
    def request(self,method,path,payload=None):
        self.calls.append((method,path,payload))
        if path=='/v1/state': return self.snapshot
        if method=='GET':
            return {'records':{'ft':{'name':'ft','base':'base','path':'/shared/ft'}},
                    'writes_enabled':False,'blocked_by':[{'reason':'registry_writes_disabled'}],
                    'discovered':[{'name':'cand','path':'/shared/cand','base':'base','util':None,
                                   'weights_gb':None,'status':'pending','reason':None}],
                    'reconcile':{'enabled':True,'last':None}}
        return self.reply


def app_for(api,snapshot,cls=SchedulerApp):
    client=RegistryClient(snapshot)
    return cls(client,SimpleNamespace(**api),event_reader=IdleEvents()),client


def registry_writes(client):
    return [call for call in client.calls if call[0] in ('POST','DELETE') and call[1].startswith('/v1/models')]


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_list_shows_discovery_and_removed_commands_never_write(api,snapshot,size):
    async def scenario():
        app,client=app_for(api,snapshot)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app,pilot,'models')
            assert 'Temporary model registry' in output(app) and '/shared/ft' in output(app)
            assert 'Shared roots' in output(app) and 'cand' in output(app)
            assert app.snapshot==snapshot
            for line in ('add "/shared/path with space" --name ft --base base --dry-run','import cand','rm ft --dry-run'):
                await submit(app,pilot,line)
                assert 'invalid choice' in output(app)
            assert registry_writes(client)==[]
            assert app.snapshot==snapshot
    asyncio.run(scenario())


def test_blocked_http_details_are_visible_for_the_read_path(api,snapshot):
    async def scenario():
        app,client=app_for(api,snapshot)
        original=client.request
        async with app.run_test(size=(40,24)) as pilot:
            await app.workers.wait_for_complete()
            def unavailable(method,path,payload=None):
                if method=='GET' and path=='/v1/models':
                    raise api['ClientError']('HTTP409',status=409,payload={'error':'registry_reconciliation_required','config_committed':True})
                return original(method,path,payload)
            client.request=unavailable
            await submit(app,pilot,'models')
            assert 'registry_reconciliation_required' in output(app)
            client.request=original
            await submit(app,pilot,'rm ft')
            assert 'invalid choice' in output(app) and registry_writes(client)==[]
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
            await app.run_write(api['build_parser']().parse_args(['pin','ft','--for','1h'])).wait()
            before=output(app)
            async def boundary():
                release.set()
                await pending.wait()
                assert output(app)==before
            app.at_teardown=boundary
    asyncio.run(scenario())


from test_registry_http_preview import mounted, registry_fixture, assert_readonly


@pytest.mark.parametrize('size',[(100,30),(40,24)])
def test_actual_core_list_and_removed_commands_in_tui(api,mounted,size):
    async def scenario():
        app=SchedulerApp(api['SchedulerClient']('http://%s:%s'%mounted.address),SimpleNamespace(**api),event_reader=IdleEvents())
        before=mounted.files(),mounted.scheduler.events_since(0)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app,pilot,'models')
            assert 'Temporary model registry' in output(app) and 'saved' in output(app)
            assert 'writes enabled: no' in output(app)
            import shlex
            await submit(app,pilot,'add '+shlex.quote(str(mounted.weights))+' --name ft --base base --dry-run')
            assert 'invalid choice' in output(app)
            await submit(app,pilot,'rm saved')
            assert 'invalid choice' in output(app)
            assert app.snapshot['models'][0]['name']=='base'
        assert_readonly(mounted,before)
    asyncio.run(scenario())
