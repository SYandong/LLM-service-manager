# Generated-By: Codex / gpt-6-astra
"""Deterministically deliver callbacks inside Textual's teardown window (#69)."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from textual.css.query import NoMatches
from textual.widgets import RichLog
from tui.app import SchedulerApp
from test_llm_events import api
from test_tui import FakeClient, snapshot


class TrackedReader:
    def __init__(self):
        self.drains = 0
        self.closes = 0
        self.events = []

    def start(self):
        pass

    def drain(self):
        self.drains += 1
        events, self.events = self.events, []
        return {"generation": 0, "events": events, "status": "connected", "dropped": 0}

    def close(self):
        self.closes += 1
        return True


class TeardownApp(SchedulerApp):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.captured_timers = []
        self.at_teardown = None
        self.callback_phases = []

    def update_events(self):
        self.callback_phases.append(self.is_running)
        return super().update_events()

    def set_interval(self, interval, callback=None, **kwargs):
        timer = super().set_interval(interval, callback, **kwargs)
        self.captured_timers.append((callback, timer))
        return timer

    async def _close_all(self):
        # Version-scoped test seam: _shutdown has set is_running=False, but
        # App timers/on_unmount have not been closed/dispatched yet.
        assert not self.is_running
        if self.at_teardown is not None:
            await self.at_teardown()
        await super()._close_all()


def test_pending_timer_after_widget_removal_keeps_mounted_delivery(api, snapshot):
    async def scenario():
        reader = TrackedReader()
        app = TeardownApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=reader)
        release, queued = asyncio.Event(), asyncio.Event()
        drain_counts = []
        async with app.run_test(size=(60, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            timer = next(timer for callback, timer in app.captured_timers if callback == app.update_events)
            for _, owned_timer in app.captured_timers:
                owned_timer.pause()  # Remove wall-clock timing from this regression.
            reader.events = [{"id": 1, "timestamp": 1800000000, "kind": "sleep", "model": "mounted"}]
            await timer._tick(next_timer=asyncio.get_running_loop().time(), count=1)
            await pilot.pause()
            assert app.event_history[0]["model"] == "mounted"
            assert any("mounted" in line.text for line in app.query_one("#events", RichLog).lines)

            async def pending_tick():
                queued.set()
                await release.wait()
                await timer._tick(next_timer=asyncio.get_running_loop().time(), count=3)

            pending = asyncio.create_task(pending_tick())
            await queued.wait()

            async def teardown_boundary():
                assert not app._exit  # Timer._tick must invoke, not skip, its callback.
                before = reader.drains
                await timer._tick(next_timer=asyncio.get_running_loop().time(), count=2)
                drain_counts.append((before, reader.drains))
                await app.query_one("#event-panel").remove()
                before = reader.drains
                release.set()
                await pending
                drain_counts.append((before, reader.drains))

            app.at_teardown = teardown_boundary
        assert pending.done()
        assert app._exception is None
        assert app.callback_phases[-2:] == [False, False]
        assert all(before == after for before, after in drain_counts)
        assert reader.closes == 1
        # 0.70 retains its completed Task; current Textual clears the reference.
        assert all(timer._task is None or timer._task.done() for _, timer in app.captured_timers), [
            (getattr(callback, "__qualname__", str(callback)), str(timer._task))
            for callback, timer in app.captured_timers
        ]
    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["status", "usage"])
def test_late_response_cannot_redraw_during_shutdown(api, snapshot, command):
    async def scenario():
        reader = TrackedReader()
        client = FakeClient(snapshot)
        app = TeardownApp(client, SimpleNamespace(**api), event_reader=reader)
        started, release = threading.Event(), threading.Event()
        shutdown_snapshots = []
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for _, timer in app.captured_timers:
                timer.pause()
            original = app.snapshot if command == "status" else app.usage_snapshot

            def delayed(method, path):
                started.set()
                assert release.wait(2)
                if command == "status":
                    return {"models": [{"name": "late-response"}]}
                return {"days": 7, "by": "container", "known": True, "error": None, "rows": [],
                        "totals": {"requests": 0, "input_tokens": 0, "output_tokens": 0}}

            client.request = delayed
            if command == "status":
                app.refresh_state()
            else:
                app.show_usage(api["build_parser"]().parse_args(["usage"]))
            assert await asyncio.to_thread(started.wait, 2)

            async def teardown_boundary():
                release.set()
                await app.workers.wait_for_complete()
                shutdown_snapshots.append(app.snapshot if command == "status" else app.usage_snapshot)

            app.at_teardown = teardown_boundary
        assert shutdown_snapshots == [original]
        assert shutdown_snapshots[0] is original
        assert not app.fetching
        assert not app.usage_fetching
        assert reader.closes == 1
    asyncio.run(scenario())


def test_mounted_widget_errors_are_not_suppressed(api, snapshot, monkeypatch):
    async def scenario():
        app = TeardownApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=TrackedReader())
        async with app.run_test(size=(60, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for _, timer in app.captured_timers:
                timer.pause()
            status = app.query_one("#event-status")

            def broken_update(*args, **kwargs):
                raise NoMatches("mounted structural error")

            with monkeypatch.context() as patch:
                patch.setattr(status, "update", broken_update)
                assert app.is_running
                with pytest.raises(NoMatches, match="mounted structural error"):
                    app.update_events()
    asyncio.run(scenario())
