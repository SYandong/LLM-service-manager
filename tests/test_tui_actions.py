# Generated-By: Codex / gpt-6-astra
"""Free/wake UI against real core HTTP; all model effects are synthetic fixtures."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from tui.app import ConfirmRam, SchedulerApp
from test_llm_actions import action_service, pin_api, pin_service, command, free_outcome
from test_tui import IdleEvents
from test_tui_pin import submit
from test_tui_teardown import TeardownApp


def app_for(api, service, cls=SchedulerApp):
    return cls(api["SchedulerClient"](service.url, timeout=2), SimpleNamespace(**api), event_reader=IdleEvents())


def output(app):
    return str(app.query_one("#result", Static).render())


@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
def test_free_wake_refresh_and_ram_confirmation(pin_api, action_service, size):
    async def scenario():
        app = app_for(pin_api, action_service)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await submit(app, pilot, "free --need 10G")
            assert "Free status: complete" in output(app) and "12 GiB" in output(app)
            assert app.snapshot["models"][0]["state"] == "sleeping"
            assert action_service.requests[-2:] == [("POST", "/v1/free"), ("GET", "/v1/state")]
            await submit(app, pilot, "wake model")
            assert "status: ready" in output(app)
            assert app.snapshot["models"][0]["state"] == "awake"
            await submit(app, pilot, "free --need 10G")
            before = len(action_service.requests)
            await submit(app, pilot, "free --ram --need 20G")
            assert isinstance(app.screen, ConfirmRam)
            assert app.screen.focused.id == "ram-cancel"  # Repeated Enter is cancel, not stop.
            assert len(action_service.requests) == before
            # Mounted timers remain safe while a confirmation screen is on top.
            app.update_events()
            await app.refresh_state().wait()
            await pilot.resize_terminal(*size)
            assert app.dashboard.query_one("#models").is_attached
            await pilot.press("escape")
            assert not isinstance(app.screen, ConfirmRam)
            assert "cancelled" in output(app)
            assert not any(call[0] == "stop" for call in action_service.effects["calls"])
            await submit(app, pilot, "free --ram --need 20G")
            await pilot.click("#ram-confirm")
            await app.workers.wait_for_complete()
            assert app.snapshot["models"][0]["state"] == "stopped"
            assert "host MemAvailable change: 25 GiB" in output(app)
            assert "Stopped: model" in output(app)
            assert action_service.effects["calls"].count(("stop", "vllm-0.service")) == 1
    asyncio.run(scenario())


def test_ram_preview_needs_no_confirmation_and_does_not_write(pin_api, action_service):
    async def scenario():
        app = app_for(pin_api, action_service)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            before = action_service.database.read_bytes()
            await submit(app, pilot, "free --ram --dry-run")
            assert not isinstance(app.screen, ConfirmRam)
            assert "Dry run" in output(app) and "not measured" in output(app)
            assert not action_service.effects["calls"]
            assert action_service.database.read_bytes() == before
    asyncio.run(scenario())


def test_slow_wake_stays_responsive_and_never_queues_second_write(pin_api, action_service):
    async def scenario():
        app = app_for(pin_api, action_service)
        started, release = threading.Event(), threading.Event()
        original = app.client.request
        calls = []
        def delayed(method, path, payload=None, **kwargs):
            calls.append((method, path, kwargs))
            if path.startswith("/v1/wake/"):
                started.set()
                assert release.wait(5)
            return original(method, path, payload, **kwargs)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = delayed
            pending = app.run_write(command(pin_api, "wake", "model"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.run_write(command(pin_api, "free")).wait()
                assert "already running" in output(app)
                await pilot.press("slash")
                assert app.focused.id == "command"
                app.update_events()
                await app.refresh_state().wait()
                assert calls == [("POST", "/v1/wake/model", {"timeout": 930})]
            finally:
                release.set()
            await pending.wait()
            assert "status: ready" in output(app)
            assert [call[:2] for call in calls] == [("POST", "/v1/wake/model"), ("GET", "/v1/state")]
    asyncio.run(scenario())


def test_stale_state_cannot_overwrite_action_refresh(pin_api, action_service):
    async def scenario():
        app = app_for(pin_api, action_service)
        started, release = threading.Event(), threading.Event()
        original = app.client.request
        hold = [True]
        def delayed(method, path, payload=None, **kwargs):
            result = original(method, path, payload, **kwargs)
            if method == "GET" and hold[0]:
                hold[0] = False
                started.set()
                assert release.wait(5)
            return result
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = delayed
            stale = app.refresh_state()
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.run_write(command(pin_api, "free", "--need", "10G")).wait()
                assert app.snapshot["models"][0]["state"] == "sleeping"
            finally:
                release.set()
            await stale.wait()
            assert app.snapshot["models"][0]["state"] == "sleeping"
    asyncio.run(scenario())


def test_partial_result_and_failed_refresh_keep_details_without_retry(pin_api, action_service):
    async def scenario():
        app = app_for(pin_api, action_service)
        calls = []
        def outcome(method, path, payload=None, **kwargs):
            calls.append((method, path))
            if method == "GET":
                raise pin_api["ClientError"]("fixture refresh unavailable")
            return free_outcome()
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.client.request = outcome
            await submit(app, pilot, "free")
            for text in ["status: partial", "change: unknown", "Slept: a", "Stopped: b", "in_flight", "measurement_unavailable", "State refresh failed"]:
                assert text in output(app)
            assert calls == [("POST", "/v1/free"), ("GET", "/v1/state")]
    asyncio.run(scenario())


def test_accepted_action_reply_after_shutdown_cannot_redraw(pin_api, action_service):
    async def scenario():
        app = app_for(pin_api, action_service, TeardownApp)
        started, release = threading.Event(), threading.Event()
        original = app.client.request
        def delayed(method, path, payload=None, **kwargs):
            result = original(method, path, payload, **kwargs)
            if method == "POST":
                started.set()
                assert release.wait(5)
            return result
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            before = app.snapshot
            app.client.request = delayed
            pending = app.run_write(command(pin_api, "free", "--need", "10G"))
            assert await asyncio.to_thread(started.wait, 2)
            async def boundary():
                release.set()
                await pending.wait()
                assert app.snapshot is before and not app._write_busy
            app.at_teardown = boundary
        assert action_service.scheduler.snapshot().models[0].state == "sleeping"
        assert action_service.requests[-1] == ("POST", "/v1/free")
    asyncio.run(scenario())


def test_readonly_error_remains_visible_without_state_change(pin_api, action_service):
    async def scenario():
        app = app_for(pin_api, action_service)
        action_service.scheduler.config = replace(action_service.scheduler.config, read_only=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await submit(app, pilot, "free")
            assert "read_only" in output(app)
            assert app.snapshot["models"][0]["state"] == "awake"
            assert not action_service.effects["calls"]
    asyncio.run(scenario())
