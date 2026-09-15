# Generated-By: Codex / gpt-6-astra
# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Usage views reconcile with the fake report backend, without live probes."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from tui.app import SchedulerApp
from test_llm_usage import usage_api, usage_service
from test_tui import IdleEvents

__all__ = ["usage_api", "usage_service"]


def app_for(api, service):
    return SchedulerApp(api["SchedulerClient"](service.url, timeout=2),
                        SimpleNamespace(**api), event_reader=IdleEvents())


async def enter_usage(app, pilot, command="/usage"):
    app.query_one("#command").value = command
    await pilot.press("enter")


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def leave_usage(app, pilot):
    app.query_one("#command").value = "status"
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def usage_paths(service):
    return [path for path in service.paths if path.startswith("/v1/usage/report")]


def test_usage_defaults_and_buttons_change_one_dimension(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await enter_usage(app, pilot)
            await settle(app, pilot)
            assert app.usage_active and app.screen.has_class("usage")
            assert (app.usage_args.days, app.usage_args.by) == (7, "user")
            # A grouping button keeps the current window.
            await pilot.click("#usage-model")
            await app.workers.wait_for_complete()
            assert (app.usage_args.days, app.usage_args.by) == (7, "model")
            # A day button keeps the current grouping.
            await pilot.click("#usage-30")
            await app.workers.wait_for_complete()
            assert (app.usage_args.days, app.usage_args.by) == (30, "model")
            await pilot.click("#usage-1")
            await app.workers.wait_for_complete()
            assert (app.usage_args.days, app.usage_args.by) == (1, "model")
            await pilot.click("#usage-day")
            await app.workers.wait_for_complete()
            assert (app.usage_args.days, app.usage_args.by) == (1, "day")
            await pilot.click("#usage-status")
            await app.workers.wait_for_complete()
            assert not app.usage_active and not app.screen.has_class("usage")
    asyncio.run(scenario())


def test_usage_slash_accepts_any_order(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for command, expected in [("/usage", (7, "user")),
                                      ("/usage 30 model", (30, "model")),
                                      ("/usage model 30", (30, "model")),
                                      ("/usage 1 day", (1, "day"))]:
                await enter_usage(app, pilot, command)
                await settle(app, pilot)
                assert (app.usage_args.days, app.usage_args.by) == expected, command
    asyncio.run(scenario())


def test_rendered_text_equals_format_usage(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await enter_usage(app, pilot)
            await settle(app, pilot)
            width = max(1, app.terminal_width - 4)
            expected = usage_api["format_usage"](app.usage_snapshot, width=width)
            assert str(app.query_one("#usage-text", Static).render()) == expected
    asyncio.run(scenario())


def test_usage_command_grouping_and_unknown_source_clear_old_totals(usage_api, usage_service):
    async def scenario():
        app = app_for(usage_api, usage_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            command = app.query_one("#command")
            command.focus()
            command.value = "usage --days 30 --by model"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.usage_snapshot["by"] == "model"
            assert app.usage_snapshot["totals"]["input_tokens"] == 563919
            assert "MODEL" in str(app.query_one("#usage-text", Static).render())
            usage_service.scheduler._usage = None
            await app.refresh_usage().wait()
            assert app.usage_snapshot["known"] is False
            text = str(app.query_one("#usage-text", Static).render())
            assert "Unavailable: usage_not_configured" in text
            assert "Requests ?  Tokens ?" in text and "563,919" not in text
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
            await enter_usage(app, pilot)
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await pilot.click("#usage-30")
                assert app.usage_args.days == 30
                assert app.usage_snapshot is None
            finally:
                release.set()
            await app.workers.wait_for_complete()
            assert app.usage_snapshot["days"] == 30
            assert usage_paths(usage_service) == [
                "/v1/usage/report?days=7&by=user", "/v1/usage/report?days=30&by=user"]
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
            await enter_usage(app, pilot)
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await leave_usage(app, pilot)
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
            await enter_usage(app, pilot)
            await settle(app, pilot)
            await app.query_one("#event-panel").remove()
            app.update_events()  # Simulate a callback already queued at teardown.
    asyncio.run(scenario())
