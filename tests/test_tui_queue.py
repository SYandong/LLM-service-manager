# Generated-By: OpenCode / deepseek-v4.1-flash
"""Bounded FIFO management-command queue for the TUI (#247).

These are headless CPU tests: no GPU, HTTP server or production action runs.
The fake client records the request order and can hold one request open so a
second submission is deterministically queued.
"""

import asyncio
import re
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, OptionList, Static
from tui.app import MAX_QUEUED_WRITES, SchedulerApp
from test_llm import snapshot
from test_tui import IdleEvents
from test_tui_teardown import TeardownApp

CLI = Path(__file__).resolve().parents[1] / "cli" / "llm"


class QueueClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = set()

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if method != "GET":
            if path in self.fail:
                raise OSError("fixture transport failure")
            if self.block:
                self.entered.set()
                if not self.release.wait(5):
                    raise OSError("fixture wait timed out")
        if method == "GET":
            return self.snapshot
        verb, _, model = path.removeprefix("/v1/").partition("/")
        reply = {"model": model, "status": "ready", "elapsed_seconds": 1.0}
        if verb == "wake":
            return {**reply, "ready": True, "cold_start": False}
        if verb == "preload":
            return {**reply, "state": "sleeping", "already_resident": False}
        return {**reply, "state": "sleeping" if verb == "sleep" else "stopped"}


def queue_app(snapshot, cls=SchedulerApp):
    client = QueueClient(snapshot)
    api = SimpleNamespace(**runpy.run_path(str(CLI)))
    return cls(client, api, event_reader=IdleEvents()), client


def output(app):
    return str(app.query_one("#result", Static).render())


def posts(client):
    return [path for method, path in client.calls if method != "GET"]


def test_second_write_is_queued_and_runs_in_fifo_order(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            # A same-model follow-up must not be silently dropped.
            app.submit_command("sleep research-model")
            assert len(app._queue) == 1
            assert app._current_write.command == "wake" and app._current_write.model == "research-model"
            # The immutable id is displayed separately from the q-position.
            assert "id2 (#1) sleep research-model" in app.queue_text()
            # The queued model shows its own position in its row, separate from
            # the active transition/loading shown in the STATE cell.
            label = app.query_one("#models", DataTable).get_cell("research-model", "MODEL").plain
            assert "[q1]" in label
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/wake/research-model", "/v1/sleep/research-model"]
            assert not app._queue and app._current_write is None
    asyncio.run(scenario())


def test_queued_stop_after_wake_is_not_dropped(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake cold-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("stop cold-model")
            assert [entry.command for entry in app._queue] == ["stop"]
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/wake/cold-model", "/v1/stop/cold-model"]
    asyncio.run(scenario())


def test_cancel_removes_only_a_not_yet_dispatched_entry(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep research-model")
            queued_id = app._queue[0].id
            assert app.cancel_queue(str(queued_id)) is True
            assert not app._queue
            # The running entry cannot be cancelled, and cancelling again is safe.
            assert app.cancel_queue(str(app._current_write.id)) is False
            assert "already dispatched" in output(app)
            assert app.cancel_queue(str(queued_id)) is False
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/wake/research-model"]
    asyncio.run(scenario())


def test_cancel_by_model_removes_every_pending_entry_for_that_model(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep research-model")
            app.submit_command("sleep research-model")
            assert len(app._queue) == 2
            assert app.cancel_queue("research-model") is True
            assert not app._queue
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/wake/research-model"]
    asyncio.run(scenario())


def test_menu_cancel_queued_operations_for_selected_model(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep research-model")
            assert len(app._queue) == 1
            app.open_model_menu(app.model_names.index("research-model"))
            menu = app.query_one("#model-menu", OptionList)
            cancel = menu.get_option("cancel-queue")
            assert not cancel.disabled
            app.menu_action("cancel-queue", "research-model")
            assert not app._queue
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/wake/research-model"]
    asyncio.run(scenario())


def test_failed_request_does_not_retry_and_the_queue_continues(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.fail = {"/v1/wake/cold-model"}
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("sleep research-model")
            app.submit_command("wake cold-model")
            await app.workers.wait_for_complete()
            # One failed request, no automatic retry, then the queue continues.
            assert posts(client) == ["/v1/sleep/research-model", "/v1/wake/cold-model"]
            assert "wake request failed" in output(app)
            assert not app._queue
    asyncio.run(scenario())


def test_queue_is_bounded(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            for _ in range(MAX_QUEUED_WRITES):
                app.submit_command("sleep research-model")
            assert len(app._queue) == MAX_QUEUED_WRITES
            app.submit_command("sleep research-model")
            assert len(app._queue) == MAX_QUEUED_WRITES
            assert "Queue full" in output(app)
            app._queue.clear()
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_queue_prints_immutable_ids_apart_from_positions(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")   # running, id1
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep cold-model")      # id2
            app.submit_command("wake default-model")    # id3
            app.submit_command("sleep research-model")  # id4
            app.submit_command("/queue")
            text = output(app)
            assert "running id1 wake research-model" in text
            assert "id2 (#1) sleep cold-model" in text
            assert "id3 (#2) wake default-model" in text
            assert "id4 (#3) sleep research-model" in text
            # Cancel the immutable id that was printed for position #2 (id3),
            # then confirm the remaining positions are recomputed.
            app.submit_command("/cancel 3")
            assert [entry.id for entry in app._queue] == [2, 4]
            app.submit_command("/queue")
            text = output(app)
            assert "id2 (#1) sleep cold-model" in text
            assert "id4 (#2) sleep research-model" in text
            assert "id3" not in text
            # A bare position is not an id: /cancel 1 targets the running id1.
            app.submit_command("/cancel 1")
            assert "already dispatched" in output(app)
            assert [entry.id for entry in app._queue] == [2, 4]
            # A numeric id that matches nothing never removes a position.
            app.submit_command("/cancel 99")
            assert "No pending queue entry with id 99" in output(app)
            assert [entry.id for entry in app._queue] == [2, 4]
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client)[0] == "/v1/wake/research-model"
    asyncio.run(scenario())


def test_cancel_accepts_the_displayed_id_token_from_queue(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")   # running, id1
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep cold-model")      # id2
            app.submit_command("wake default-model")    # id3
            app.submit_command("/queue")
            text = output(app)
            match = re.search(r"(id2) \(#1\) sleep cold-model", text)
            assert match is not None, text
            # Copy the exact displayed token into /cancel (not just the bare 2).
            app.submit_command("/cancel %s" % match.group(1))
            assert [entry.id for entry in app._queue] == [3]
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client)[0] == "/v1/wake/research-model"
    asyncio.run(scenario())


def test_cancel_accepts_a_quoted_model_name_with_spaces(snapshot):
    async def scenario():
        snapshot["models"].append({"name": "org/big model", "state": "awake", "gpu": 0,
                                   "is_default": False, "resident_gb": 10, "transition": None})
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command('sleep -- "org/big model"')
            assert [entry.model for entry in app._queue] == ["org/big model"]
            app.submit_command('/cancel "org/big model"')
            assert not app._queue
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/wake/research-model"]
    asyncio.run(scenario())


def test_delayed_during_write_read_cannot_regress_the_final_snapshot(snapshot):
    async def scenario():
        import copy as _copy
        stale = _copy.deepcopy(snapshot)
        stale["models"][1]["resident_gb"] = 1
        fresh = _copy.deepcopy(snapshot)
        fresh["models"][1]["resident_gb"] = 99

        class RaceClient:
            def __init__(self, base):
                self.base = base
                self.get_count = 0
                self.post_entered = threading.Event()
                self.post_release = threading.Event()
                self.get_entered = threading.Event()
                self.get_release = threading.Event()

            def request(self, method, path, payload=None, **kwargs):
                if method != "GET":
                    self.post_entered.set()
                    assert self.post_release.wait(5)
                    verb, _, model = path.removeprefix("/v1/").partition("/")
                    return {"model": model, "status": "ready", "elapsed_seconds": 1.0,
                            "ready": True, "cold_start": False,
                            "state": "sleeping" if verb == "sleep" else "stopped"}
                if path == "/v1/state":
                    self.get_count += 1
                    if self.get_count == 2:
                        # The read launched during the write: delay it until after
                        # the operation's own fresh read has completed.
                        self.get_entered.set()
                        assert self.get_release.wait(5)
                        return stale
                    if self.get_count >= 3:
                        return fresh
                return self.base

        client = RaceClient(snapshot)
        api = SimpleNamespace(**runpy.run_path(str(CLI)))
        app = SchedulerApp(client, api, event_reader=IdleEvents())
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.post_entered.wait, 2)
            during = app.refresh_state()
            assert await asyncio.to_thread(client.get_entered.wait, 2)
            client.post_release.set()
            for _ in range(200):
                if app.snapshot["models"][1]["resident_gb"] == 99:
                    break
                await asyncio.sleep(0.01)
            assert app.snapshot["models"][1]["resident_gb"] == 99  # final fresh read
            # The stale during-write read finishes last but must be discarded.
            client.get_release.set()
            await during.wait()
            assert app.snapshot["models"][1]["resident_gb"] == 99
    asyncio.run(scenario())


def test_read_refresh_runs_while_a_queued_write_waits(snapshot):
    async def scenario():
        app, client = queue_app(snapshot)
        client.block = True
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            before = len([call for call in client.calls if call[0] == "GET"])
            await app.refresh_state().wait()
            after = len([call for call in client.calls if call[0] == "GET"])
            assert after > before  # Read-only refresh is not blocked by the write.
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_teardown_discards_queued_entries_without_claiming_remote_cancel(snapshot):
    async def scenario():
        app, client = queue_app(snapshot, cls=TeardownApp)
        client.block = True

        async def boundary():
            assert not app.is_running
            client.release.set()
            await app.workers.wait_for_complete()

        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake research-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep research-model")
            assert len(app._queue) == 1
            app.at_teardown = boundary
        assert not app._queue and app._current_write is None
        # The undelivered second command never reached the scheduler.
        assert posts(client) == ["/v1/wake/research-model"]
    asyncio.run(scenario())
