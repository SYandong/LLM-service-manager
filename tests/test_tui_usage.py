# Generated-By: Codex / gpt-6-astra
"""Usage views reconcile with the real read-only backend, without live probes."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, Static
from tui.app import SchedulerApp
from test_llm_usage import usage_api, usage_service
from test_tui import IdleEvents


def app_for(api, service):
    return SchedulerApp(api["SchedulerClient"](service.url, timeout=2),
                        SimpleNamespace(**api), event_reader=IdleEvents())


@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
def test_usage_toggle_and_windows_match_backend(usage_api, usage_service, size):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("u")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.usage_active and app.screen.has_class("usage")
            assert app.usage_snapshot["totals"] == {"requests": 2, "input_tokens": 18, "output_tokens": 25}
            text = str(app.query_one("#usage-text", Static).render())
            assert "unknown" in text and "18" in text and "25" in text
            assert app.query_one("#usage-scroll").region.width <= size[0]
            assert app.query_one("#command").region.bottom <= size[1]
            await pilot.click("#usage-30")
            await app.workers.wait_for_complete()
            assert app.usage_args.days == 30
            assert app.usage_snapshot["totals"] == {"requests": 3, "input_tokens": 118, "output_tokens": 225}
            text = str(app.query_one("#usage-text", Static).render())
            assert "118" in text and "225" in text and "IP only" in text
            await pilot.press("u")
            await app.workers.wait_for_complete()
            assert not app.usage_active
            assert not app.screen.has_class("usage")
            assert "/v1/usage?days=7&by=container" in usage_service.paths
            assert "/v1/usage?days=30&by=container" in usage_service.paths
    asyncio.run(scenario())


def test_usage_command_grouping_and_unknown_source_clear_old_totals(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            command = app.query_one("#command", Input)
            command.focus()
            command.value = "usage --days 30 --by model"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.usage_snapshot["by"] == "model"
            assert app.usage_snapshot["totals"]["input_tokens"] == 118
            assert "not grouped" in str(app.query_one("#usage-text", Static).render())
            usage_service.scheduler._usage = None
            await app.refresh_usage().wait()
            assert app.usage_snapshot["known"] is False
            text = str(app.query_one("#usage-text", Static).render())
            assert "Unavailable: usage_not_configured" in text
            assert "Requests ?" in text and "118" not in text
    asyncio.run(scenario())


def test_slow_old_window_cannot_replace_latest_selection(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        started, release = threading.Event(), threading.Event()
        original = usage_service.scheduler._usage

        def delayed(*, days, by):
            if days == 7:
                started.set()
                assert release.wait(3)
            return original(days=days, by=by)

        usage_service.scheduler._usage = delayed
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("u")
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await pilot.click("#usage-30")
                assert app.usage_args.days == 30
                assert app.usage_snapshot is None
            finally:
                release.set()
            await app.workers.wait_for_complete()
            assert app.usage_snapshot["days"] == 30
            assert app.usage_snapshot["totals"]["input_tokens"] == 118
            usage_paths = [path for path in usage_service.paths if path.startswith("/v1/usage")]
            assert usage_paths == ["/v1/usage?days=7&by=container", "/v1/usage?days=30&by=container"]
    asyncio.run(scenario())


def test_late_usage_result_does_not_switch_back_from_status(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        started, release = threading.Event(), threading.Event()
        original = usage_service.scheduler._usage

        def delayed(**kwargs):
            started.set()
            assert release.wait(3)
            return original(**kwargs)

        usage_service.scheduler._usage = delayed
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("u")
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await pilot.press("u")
                assert not app.usage_active
            finally:
                release.set()
            await app.workers.wait_for_complete()
            assert not app.screen.has_class("usage")
            assert "Usage updated" not in str(app.query_one("#result", Static).render())
    asyncio.run(scenario())


def test_usage_close_ignores_queued_event_timer(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("u")
            await app.workers.wait_for_complete()
            await app.query_one("#event-panel").remove()
            app.update_events()  # Simulate a callback already queued at teardown.
    asyncio.run(scenario())
