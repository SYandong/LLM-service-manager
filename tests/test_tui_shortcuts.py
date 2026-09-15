# Generated-By: Codex / gpt-6-astra
# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Actual keyboard interactions on the command line; no model effects here."""

import asyncio
import time

import pytest

pytest.importorskip("textual")

from textual.widgets import RichLog, Static
from tui.app import INTERRUPT_WINDOW
from test_tui import make_app, snapshot

__all__ = ["snapshot"]


def entry(app):
    return app.dashboard.query_one("#command")


def output(app):
    return str(app.dashboard.query_one("#result", Static).render())


def bottom(app):
    return str(app.dashboard.query_one("#event-status", Static).render())


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_former_shortcut_letters_are_plain_text_now(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            before = list(client.calls)
            await pilot.press("f", "p", "w", "u", "r", "q", "e", "slash")
            assert entry(app).value == "fpwurqe/"
            assert app.is_running and not app.usage_active
            assert client.calls == before
    asyncio.run(scenario())


def test_question_mark_helps_only_on_an_empty_line(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.press("question_mark")
            assert entry(app).value == ""
            for text in ["Ctrl+O item menu", "Tab complete", "/copy", "sleep MODEL", "--dry-run"]:
                assert text in output(app)
            await pilot.press("w", "a", "k", "e", "space", "question_mark")
            assert entry(app).value == "wake ?"
    asyncio.run(scenario())


def test_command_history_walks_this_session(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for text in ["status", "models"]:
                entry(app).value = text
                await pilot.press("enter")
                await settle(app, pilot)
            assert app.history == ["status", "models"]
            await pilot.press("d", "r", "a", "f", "t")
            await pilot.press("up")
            assert entry(app).value == "models"
            await pilot.press("up")
            assert entry(app).value == "status"
            await pilot.press("up")
            assert entry(app).value == "status"  # The oldest entry is the end of the walk.
            await pilot.press("down")
            assert entry(app).value == "models"
            await pilot.press("down")
            assert entry(app).value == "draft"  # The unsent draft comes back intact.
    asyncio.run(scenario())


def test_tab_completes_commands_models_and_ui_actions(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.press("p", "i", "tab")
            assert entry(app).value == "pin "  # A unique prefix completes in place.
            await pilot.press("r", "e", "s", "tab")
            assert entry(app).value == "pin research-model "
            entry(app).value = ""
            await pilot.press("r", "e", "tab")
            assert entry(app).value == "re"  # Ambiguous prefixes only list candidates.
            assert "registry" in output(app) and "reserve" in output(app)
            entry(app).value = ""
            await pilot.press("slash", "q", "u", "i", "tab")
            assert entry(app).value == "/quit "
            entry(app).value = ""
            await pilot.press("z", "z", "tab")
            assert entry(app).value == "zz" and "No completion" in bottom(app)
    asyncio.run(scenario())


def test_escape_clears_the_line(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.press("f", "r", "e", "e")
            await pilot.press("escape")
            assert entry(app).value == "" and app.is_running
    asyncio.run(scenario())


def test_ctrl_c_clears_then_exits_on_a_confirmed_second_press(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.press("f", "r", "e", "e")
            await pilot.press("ctrl+c")
            assert entry(app).value == "" and app.is_running
            await pilot.press("ctrl+c")
            assert app.is_running and "Press Ctrl+C again to exit" in bottom(app)
            # An expired first press is not an exit authorization.
            app._interrupt_at = time.monotonic() - INTERRUPT_WINDOW - 1
            await pilot.press("ctrl+c")
            assert app.is_running and "Press Ctrl+C again to exit" in bottom(app)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running
    asyncio.run(scenario())


def test_ctrl_d_exits_only_on_an_empty_line(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.press("s", "t", "a", "t", "u", "s", "home")
            await pilot.press("ctrl+d")
            assert entry(app).value == "tatus" and app.is_running
            entry(app).value = ""
            await pilot.press("ctrl+d")
            await pilot.pause()
            assert not app.is_running
    asyncio.run(scenario())


def test_shift_keys_move_the_model_selection(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            assert app.selected_model() == "default-model"
            await pilot.press("shift+down")
            assert app.selected_model() == "research-model"
            await pilot.press("shift+tab")
            assert app.selected_model() == "cold-model"
            await pilot.press("shift+up")
            assert app.selected_model() == "research-model"
            assert app.focused is entry(app)
            for _ in range(6):
                await pilot.press("shift+up")
            assert app.selected_model() == "default-model"
    asyncio.run(scenario())


def test_ctrl_l_clears_the_event_display_without_resetting_the_cursor(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        resets = []

        class Reader:
            def start(self):
                pass

            def close(self):
                return True

            def reset_cursor(self):
                resets.append(True)

            def drain(self):
                batch, self.events = self.events, []
                return {"generation": 0, "events": batch, "status": "connected", "dropped": 0}

        reader = Reader()
        reader.events = []
        app.event_reader = reader
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            reader.events = [{"id": 3, "timestamp": 3, "kind": "sleep", "model": "demo"}]
            app.update_events()
            await settle(app, pilot)
            log = app.query_one("#events", RichLog)
            assert log.lines and app.event_history
            await pilot.press("ctrl+l")
            assert not log.lines
            assert app.event_history and not resets  # Display only; history/cursor kept.
            await pilot.press("ctrl+r")
            assert resets == [True]
    asyncio.run(scenario())


def test_slash_commands_are_ui_actions_with_no_second_parser(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()  # Command output must not race a background poll.
            for command, expected in [("/help", "Ctrl+O item menu"),
                                      ("/usage 90", "/usage accepts 1|7|30 and user|model|day"),
                                      ("/bogus", "Unknown UI command")]:
                entry(app).value = command
                await pilot.press("enter")
                await settle(app, pilot)
                assert expected in output(app), command
            entry(app).value = "/usage 30"
            await pilot.press("enter")
            await settle(app, pilot)
            assert app.usage_active and app.usage_args.days == 30
            assert app.focused is entry(app)
            entry(app).value = "/refresh"
            await pilot.press("enter")
            await settle(app, pilot)
            assert "Refresh requested" in bottom(app)
            entry(app).value = "/clear"
            await pilot.press("enter")
            await settle(app, pilot)
            assert output(app) == "Cleared"
            entry(app).value = "/events"
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is not app.dashboard  # The frozen detail view opened.
            await pilot.press("escape")
            await pilot.pause()
            entry(app).value = "/quit"
            await pilot.press("enter")
            await pilot.pause()
            assert not app.is_running
    asyncio.run(scenario())
