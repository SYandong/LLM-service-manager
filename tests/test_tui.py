# Generated-By: Codex / gpt-6-astra
# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Headless tests for the optional, read-only terminal UI."""

import asyncio
import runpy
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, RichLog, Static
from tui.app import QueueEntry, SchedulerApp
from test_llm import snapshot


def record_clipboard(app):
    """Capture clipboard requests across Textual versions.

    8.x's test driver stores the text on ``app._clipboard``; 0.70's driver only
    writes an OSC 52 escape, so a recording wrapper is needed for assertions.
    """
    log = []
    original = getattr(app, "copy_to_clipboard", None)

    def capture(text):
        log.append(text)
        if callable(original):
            try:
                original(text)
            except Exception:
                pass

    app.copy_to_clipboard = capture
    app._clipboard_log = log
    if hasattr(app, "_clipboard"):
        app._clipboard = ""
    return app


def clipboard(app):
    log = getattr(app, "_clipboard_log", None)
    if log:
        return log[-1]
    return getattr(app, "_clipboard", "")


def reset_clipboard(app):
    log = getattr(app, "_clipboard_log", None)
    if log is not None:
        log.clear()
    if hasattr(app, "_clipboard"):
        app._clipboard = ""


class FakeClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.error = None

    def request(self, method, path):
        self.calls.append((method, path))
        if self.error:
            raise OSError(self.error)
        return self.snapshot


class IdleEvents:
    def start(self):
        pass

    def close(self):
        return True

    def drain(self):
        return {"generation": 0, "events": [], "status": "SSE fixture", "dropped": 0}


async def open_details(app, pilot):
    """The frozen event view is a UI command now, not a printable shortcut."""
    field = app.dashboard.query_one("#command")
    field.value = "/events"
    await pilot.press("enter")
    await pilot.pause()


def make_app(snapshot):
    path = Path(__file__).resolve().parents[1] / "cli" / "llm"
    api = SimpleNamespace(**runpy.run_path(str(path)))
    client = FakeClient(snapshot)
    return record_clipboard(SchedulerApp(client, api, event_reader=IdleEvents())), client


@pytest.mark.parametrize("size", [(100, 30), (60, 24), (40, 24)])
def test_layout_and_selection(snapshot, size):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#models", DataTable)
            command = app.query_one("#command")
            assert app.focused is command  # Opening the UI is opening a command line.
            assert table.row_count == 4
            assert table.size.height >= 3
            assert command.region.bottom <= size[1]
            assert command.region.width <= size[0]
            assert table.region.bottom <= app.query_one("#result-view").region.y
            panel = app.query_one("#event-panel")
            if size[0] < 100:
                assert panel.region.y >= table.region.bottom
            else:
                assert panel.region.x >= table.region.right
            assert panel.region.bottom <= app.query_one("#result-view").region.y
            assert app.screen.has_class("narrow") == (size[0] < 100)
            await pilot.press("shift+down")
            assert app.selected_model() == "research-model"
            assert set(client.calls) == {("GET", "/v1/state")}
    asyncio.run(scenario())


def test_command_line_keeps_focus_through_every_panel_click(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            command = app.query_one("#command")
            for target in ["#models", "#gpus", "#memory", "#events", "#source-status",
                           "#result", "#event-panel", "#summary"]:
                await pilot.click(target)
                await pilot.pause()
                assert app.focused is command, target
            # Non-focusable panels are the mechanism, not an accident of ordering.
            for widget in app.query("DataTable, RichLog, Button, VerticalScroll, OptionList"):
                assert not widget.can_focus, widget.id
            # The Details button opens its own screen and hands focus back on close.
            await pilot.click("#event-details")
            await pilot.pause()
            assert app.screen is not app.dashboard
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is command
    asyncio.run(scenario())


def test_command_refresh_and_parser_errors(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()  # Count only the requests this test issues.
            command = app.query_one("#command")
            command.value = "status"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert len(client.calls) == 2
            assert command.value == ""
            for value in ["reserve", "status --bad", "'unterminated", "--help"]:
                command.value = value
                await pilot.press("enter")
                await pilot.pause()
                assert len(client.calls) == 2
            assert "usage: llm" in str(app.query_one("#result", Static).render())
    asyncio.run(scenario())


def test_failed_refresh_retains_snapshot_and_selection(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("shift+down")
            client.error = "connection refused"
            await app.refresh_state().wait()
            assert app.snapshot == snapshot
            assert app.selected_model() == "research-model"
            bar = str(app.query_one("#event-status", Static).render())
            assert "connection refused" in bar and "Refresh failed at" in bar
            client.error = None
            await app.refresh_state().wait()
            assert app.selected_model() == "research-model"
    asyncio.run(scenario())


def test_resize_preserves_selection(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("shift+down")
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            assert app.screen.has_class("narrow")
            assert app.selected_model() == "research-model"
            assert len(app.query_one("#models", DataTable).columns) == 5
    asyncio.run(scenario())


def test_wide_table_shows_the_memory_budget_column(snapshot):
    async def scenario():
        snapshot["models"][1]["budget_gb"] = 80
        app, _ = make_app(snapshot)
        async with app.run_test(size=(200, 30)) as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one("#models", DataTable)
            assert [str(column.label) for column in table.columns.values()] == [
                "MODEL", "STATE", "GPU", "MEM", "BUDGET", "USED", "10m", "PIN"]
            assert table.get_cell("research-model", "BUDGET").plain.strip() == "80G"
            assert table.get_cell("cold-model", "BUDGET").plain.strip() == "?G"
    asyncio.run(scenario())


def test_polling_interval_is_half_a_second(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            assert app.refresh_seconds == 0.5
            assert len(client.calls) == 1
            await pilot.pause(0.7)
            await app.workers.wait_for_complete()
            assert len(client.calls) >= 2
    asyncio.run(scenario())


def test_refresh_interval_is_configurable(snapshot, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "cli" / "llm"
    api = SimpleNamespace(**runpy.run_path(str(path)))
    assert SchedulerApp(FakeClient(snapshot), api, event_reader=IdleEvents(),
                        refresh=2).refresh_seconds == 2
    monkeypatch.setenv("LLM_TUI_REFRESH", "1.5")
    assert SchedulerApp(FakeClient(snapshot), api, event_reader=IdleEvents()).refresh_seconds == 1.5
    monkeypatch.setenv("LLM_TUI_REFRESH", "not-a-number")
    assert SchedulerApp(FakeClient(snapshot), api, event_reader=IdleEvents()).refresh_seconds == 0.5
    monkeypatch.setenv("LLM_TUI_REFRESH", "0")
    assert SchedulerApp(FakeClient(snapshot), api, event_reader=IdleEvents()).refresh_seconds == 0.5


def test_footer_shows_the_real_version_even_with_long_status(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            version = app.api.app_version()
            assert app.version_label() == "v" + version
            # A long connection/read notice must not squeeze the version out.
            app.show_notice("very long status " * 20)
            await pilot.pause()
            bar = str(app.query_one("#event-status", Static).render())
            assert bar.startswith("v" + version)
            # Neither may a long queue message.
            app._current_write = QueueEntry(
                1, SimpleNamespace(command="wake", model="cold-model"),
                "wake cold-model", "wake", "cold-model", "cold-model")
            app.render_event_status()
            bar = str(app.query_one("#event-status", Static).render())
            assert bar.startswith("v" + version)
    asyncio.run(scenario())


def test_scheduler_event_triggers_an_immediate_state_read(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        events = []

        class Reader(IdleEvents):
            def drain(self):
                batch, events[:] = list(events), []
                return {"generation": 0, "events": batch, "status": "connected", "dropped": 0}

        app.event_reader = Reader()
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()  # Only the event may cause the next read.
            before = len(client.calls)
            events.append({"id": 7, "timestamp": 7, "kind": "sleep", "model": "research-model"})
            app.update_events()
            await app.workers.wait_for_complete()
            assert len(client.calls) == before + 1
            assert any("research-model" in line.text for line in app.query_one("#events", RichLog).lines)
            # A drain with no new event must not add another request.
            app.update_events()
            await app.workers.wait_for_complete()
            assert len(client.calls) == before + 1
    asyncio.run(scenario())


def test_slow_refresh_does_not_overlap_or_block_input(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        started, release = threading.Event(), threading.Event()
        original = client.request

        def slow(method, path):
            started.set()
            assert release.wait(timeout=5)
            return original(method, path)

        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            client.request = slow
            first = app.refresh_state()
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.refresh_state().wait()
                await pilot.press("s", "t", "a", "t", "u", "s")
                assert app.query_one("#command").value == "status"
                assert app.query_one("#command").has_focus
            finally:
                release.set()
            await first.wait()
            assert len(client.calls) == 2
    asyncio.run(scenario())


def test_copied_tui_imports_without_repository(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "cli" / "llm", tmp_path / "llm")
    shutil.copytree(root / "tui", tmp_path / "tui", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
         "api=runpy.run_path(sys.argv[1]+'/llm'); assert api['tui_app']() is not None",
         str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
