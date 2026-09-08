# Generated-By: Codex / gpt-6-astra
"""Pin input and immediate refresh against real, isolated core pin persistence."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, Static
from tui.app import SchedulerApp
from test_llm_pin import command, pin_api, pin_service
from test_tui import IdleEvents
from test_tui_teardown import TeardownApp


def app_for(api, service):
    return SchedulerApp(api["SchedulerClient"](service.url, timeout=2),
                        SimpleNamespace(**api), event_reader=IdleEvents())


async def submit(app, pilot, text):
    entry = app.query_one("#command", Input)
    entry.focus()
    entry.value = text
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
def test_pin_and_unpin_refresh_column_with_server_owner(pin_api, pin_service, monkeypatch, size):
    async def scenario():
        monkeypatch.setitem(pin_api["execute_command"].__globals__, "PIN_COMPATIBILITY_LABEL", "spoof-owner")
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app, pilot, "pin model --for 1h")
            assert app.snapshot["pins"][0]["by"] == "actual-owner"
            table = app.query_one("#models", DataTable)
            row = table.get_row_at(app.model_names.index("model"))
            assert str(row[-1]) == "yes"
            assert "actual-owner" in str(app.query_one("#details", Static).render())
            output = str(app.query_one("#result", Static).render())
            assert "owner actual-owner" in output and "spoof-owner" not in output
            assert pin_service.requests[-2:] == [("POST", "/v1/pin"), ("GET", "/v1/state")]
            await submit(app, pilot, "unpin model")
            assert app.snapshot["pins"] == []
            assert str(table.get_row_at(app.model_names.index("model"))[-1]) == "-"
            assert "actor actual-owner" in str(app.query_one("#result", Static).render())
    asyncio.run(scenario())


def test_dry_run_and_readonly_errors_do_not_change_intents(pin_api, pin_service):
    async def scenario():
        pin_service.scheduler.config = replace(pin_service.scheduler.config, read_only=True)
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            before = pin_service.database.read_bytes()
            await submit(app, pilot, "pin model --for 1h --dry-run")
            assert "hypothetical" in str(app.query_one("#result", Static).render())
            assert app.snapshot["pins"] == []
            assert pin_service.database.read_bytes() == before
            await submit(app, pilot, "pin model --for 1h")
            assert "read_only" in str(app.query_one("#result", Static).render())
            assert pin_service.database.read_bytes() == before
            assert app.snapshot["pins"] == []
    asyncio.run(scenario())


def test_prewrite_state_response_cannot_erase_confirmed_pin(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        started, release = threading.Event(), threading.Event()
        original = app.client.request
        hold = [True]

        def delayed(method, path, payload=None):
            result = original(method, path, payload)
            if method == "GET" and path == "/v1/state" and hold[0]:
                hold[0] = False
                started.set()
                assert release.wait(3)
            return result

        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = delayed
            stale = app.refresh_state()
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.run_pin(command(pin_api, "pin", "model", "--for", "1h")).wait()
                assert app.snapshot["pins"][0]["model"] == "model"
            finally:
                release.set()
            await stale.wait()
            assert app.snapshot["pins"][0]["by"] == "actual-owner"
    asyncio.run(scenario())


def test_refresh_failure_does_not_retry_successful_pin(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        original = app.client.request

        def fail_refresh(method, path, payload=None):
            if method == "GET":
                raise pin_api["ClientError"]("refresh unavailable")
            return original(method, path, payload)

        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = fail_refresh
            await submit(app, pilot, "pin model --for 1h")
            text = str(app.query_one("#result", Static).render())
            assert "owner actual-owner" in text
            assert "State refresh failed" in text
            assert len([item for item in pin_service.requests if item[0] == "POST"]) == 1
            assert pin_service.scheduler.snapshot().pins[0].by == "actual-owner"
    asyncio.run(scenario())


def test_inflight_pin_is_not_submitted_twice(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        started, release = threading.Event(), threading.Event()
        original = pin_service.scheduler.write_pin

        def delayed(*args, **kwargs):
            started.set()
            assert release.wait(3)
            return original(*args, **kwargs)

        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            pin_service.scheduler.write_pin = delayed
            args = command(pin_api, "pin", "model", "--for", "1h")
            first = app.run_pin(args)
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.run_pin(args).wait()
                assert "already running" in str(app.query_one("#result", Static).render())
            finally:
                release.set()
            await first.wait()
            assert len([item for item in pin_service.requests if item[0] == "POST"]) == 1
    asyncio.run(scenario())


def test_accepted_pin_reply_after_shutdown_does_not_redraw(pin_api, pin_service):
    async def scenario():
        client = pin_api["SchedulerClient"](pin_service.url, timeout=2)
        app = TeardownApp(client, SimpleNamespace(**pin_api), event_reader=IdleEvents())
        started, release = threading.Event(), threading.Event()
        original = client.request

        def delayed(method, path, payload=None):
            result = original(method, path, payload)
            if method == "POST":
                started.set()
                assert release.wait(3)
            return result

        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            before = app.snapshot
            client.request = delayed
            pending = app.run_pin(command(pin_api, "pin", "model", "--for", "1h"))
            assert await asyncio.to_thread(started.wait, 2)

            async def teardown_boundary():
                release.set()
                await pending.wait()
                assert app.snapshot is before
                assert not app._pin_busy

            app.at_teardown = teardown_boundary
        assert pin_service.scheduler.snapshot().pins[0].by == "actual-owner"
        assert pin_service.requests[-1] == ("POST", "/v1/pin")
    asyncio.run(scenario())
