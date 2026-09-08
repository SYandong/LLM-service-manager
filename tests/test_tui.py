# Generated-By: Codex / gpt-6-astra
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

from textual.widgets import DataTable, Input, Static
from tui.app import SchedulerApp
from test_llm import snapshot


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


def make_app(snapshot):
    path = Path(__file__).resolve().parents[1] / "cli" / "llm"
    api = SimpleNamespace(**runpy.run_path(str(path)))
    client = FakeClient(snapshot)
    return SchedulerApp(client, api), client


@pytest.mark.parametrize("size", [(100, 30), (60, 24), (40, 24)])
def test_layout_and_selection(snapshot, size):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#models", DataTable)
            command = app.query_one("#command", Input)
            assert table.row_count == 4
            assert table.size.height >= 3
            assert command.region.bottom <= size[1]
            assert command.region.width <= size[0]
            assert table.region.bottom <= app.query_one("#details").region.y
            assert app.screen.has_class("narrow") == (size[0] < 100)
            await pilot.press("down")
            assert app.selected_model() == "research-model"
            assert "ctr-b" in str(app.query_one("#details", Static).render())
            assert set(client.calls) == {("GET", "/v1/state")}
    asyncio.run(scenario())


def test_command_refresh_and_parser_errors(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            command = app.query_one("#command", Input)
            command.focus()
            command.value = "status"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert len(client.calls) == 2
            assert command.value == ""
            for value in ["free --ram", "status --bad", "'unterminated", "--help"]:
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
            await pilot.press("down")
            client.error = "connection refused"
            await app.refresh_state().wait()
            assert app.snapshot == snapshot
            assert app.selected_model() == "research-model"
            assert "connection refused" in str(app.query_one("#result", Static).render())
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
            await pilot.press("down")
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            assert app.screen.has_class("narrow")
            assert app.selected_model() == "research-model"
            assert len(app.query_one("#models", DataTable).columns) == 4
    asyncio.run(scenario())


def test_polling_interval_is_five_seconds(snapshot):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            assert len(client.calls) == 1
            await pilot.pause(5.2)
            await app.workers.wait_for_complete()
            assert len(client.calls) == 2
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
            client.request = slow
            first = app.refresh_state()
            try:
                assert await asyncio.to_thread(started.wait, 2)
                await app.refresh_state().wait()
                await pilot.press("slash")
                assert app.query_one("#command", Input).has_focus
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
