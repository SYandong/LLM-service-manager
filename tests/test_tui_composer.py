# Generated-By: OpenCode / deepseek-v4.1-flash
"""Enlarged multi-line command composer preserves every existing key (#247)."""

import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from test_tui import make_app, snapshot


def composer(app):
    return app.query_one("#command")


def output(app):
    return str(app.query_one("#result", Static).render())


def test_composer_is_a_larger_wrapped_editable_area(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            entry = composer(app)
            assert entry.soft_wrap is True
            # Bordered row plus a multi-line document area, not one empty line.
            assert app.query_one("#command-row").region.height >= 5
            assert entry.region.height >= 3
            assert app.focused is entry
    asyncio.run(scenario())


def test_plain_enter_submits_and_clears_but_modified_enter_adds_a_newline(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            before = list(client.calls)
            entry = composer(app)
            entry.value = "status"
            await pilot.press("shift+enter")
            assert "\n" in entry.value
            assert client.calls == before  # Modified Enter never submits.
            entry.value = "status"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert entry.value == ""
            assert client.calls != before
    asyncio.run(scenario())


def test_multiline_up_down_moves_the_cursor_instead_of_walking_history(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            entry = composer(app)
            app.history = ["status"]
            entry.value = "line one\nline two"
            assert entry.cursor_location == (1, len("line two"))
            await pilot.press("up")
            assert entry.cursor_location[0] == 0
            assert entry.value == "line one\nline two"  # History was not substituted.
    asyncio.run(scenario())


def test_single_line_history_still_works(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            entry = composer(app)
            for text in ["status", "models"]:
                entry.value = text
                await pilot.press("enter")
                await app.workers.wait_for_complete()
            assert app.history == ["status", "models"]
            await pilot.press("d", "r", "a", "f", "t")
            await pilot.press("up")
            assert entry.value == "models"
            await pilot.press("down")
            assert entry.value == "draft"
    asyncio.run(scenario())


def test_value_alias_places_the_cursor_at_the_end(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            entry = composer(app)
            entry.value = "wake model"
            assert entry.value == "wake model"
            assert entry.cursor_location == (0, len("wake model"))
            entry.value = "first\nsecond"
            assert entry.cursor_location == (1, len("second"))
    asyncio.run(scenario())


def test_tab_completion_and_escape_are_unchanged(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            entry = composer(app)
            await pilot.press("p", "i", "tab")
            assert entry.value == "pin "
            await pilot.press("escape")
            assert entry.value == ""
    asyncio.run(scenario())
