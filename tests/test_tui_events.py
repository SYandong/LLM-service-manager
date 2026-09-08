# Generated-By: Codex / gpt-6-astra
"""Event-panel integration uses only synthetic scheduler/loopback observations."""

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import RichLog, Static
from textual.containers import VerticalScroll
from tui.app import SchedulerApp
from test_llm import snapshot
from test_llm_events import api, core_stream
from test_tui import FakeClient


def test_scheduler_event_visible_within_one_second_and_reader_cleanup(api, core_stream):
    async def scenario():
        _, scheduler, _, url = core_stream
        app = SchedulerApp(api["SchedulerClient"](url, timeout=1), SimpleNamespace(**api))
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            started = time.monotonic()
            scheduler.emit("sleep", model="synthetic-model")
            while not app.event_history and time.monotonic() - started < 1:
                await pilot.pause(0.02)
            assert time.monotonic() - started < 1
            log = app.query_one("#events", RichLog)
            assert any("synthetic-model" in line.text for line in log.lines)
            styles = [segment.style for line in log.lines for segment in line if "sleep" in segment.text]
            assert any(style.color.name == "yellow" for style in styles)
        assert not app.event_reader.thread.is_alive()
    asyncio.run(scenario())


class BufferedEvents:
    def __init__(self):
        self.events = []
        self.generation = 0
        self.closed = False

    def start(self):
        pass

    def close(self):
        self.closed = True

    def reset_cursor(self):
        self.generation += 1
        self.events.clear()

    def drain(self):
        events, self.events = self.events, []
        return {"generation": self.generation, "events": events,
                "status": "SSE connected", "dropped": 3}


def test_timestamp_order_reset_and_safe_event_text(api, snapshot):
    async def scenario():
        events = BufferedEvents()
        app = SchedulerApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=events)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            events.events = [
                {"id": 1, "timestamp": 20, "kind": "error", "model": "\x1b[2J[red]late"},
                {"id": 2, "timestamp": 10, "kind": "pin", "model": "early"},
            ]
            app.update_events()
            await pilot.pause()
            assert [event["id"] for event in app.event_history] == [2, 1]
            log = app.query_one("#events", RichLog)
            text = "\n".join(line.text for line in log.lines)
            assert "\x1b" not in text
            assert "[red]late" in text
            assert "3 events dropped" in str(app.query_one("#event-status", Static).render())
            await pilot.press("ctrl+r")
            assert events.generation == 1
            assert app.event_history == []
            assert not log.lines
        assert events.closed
    asyncio.run(scenario())


def test_bounded_history(api, snapshot):
    async def scenario():
        events = BufferedEvents()
        app = SchedulerApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=events)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            events.events = [{"id": i, "timestamp": i, "kind": "state"} for i in range(300)]
            app.update_events()
            await pilot.pause()
            assert len(app.event_history) == 200
            assert app.event_history[0]["id"] == 100
            assert len(app.query_one("#events", RichLog).lines) <= 500
    asyncio.run(scenario())


def test_long_result_and_details_are_scrollable(api, snapshot):
    async def scenario():
        snapshot["models"][0]["name"] = "long model details " * 100 + "END"
        app = SchedulerApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=BufferedEvents())
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.show_result("long explanation " * 100 + "END")
            await pilot.pause()
            for name in ["result", "details"]:
                view = app.query_one("#" + name + "-view", VerticalScroll)
                assert view.max_scroll_y > 0
                view.scroll_end(animate=False)
                await pilot.pause()
                assert view.scroll_y == view.max_scroll_y
    asyncio.run(scenario())
