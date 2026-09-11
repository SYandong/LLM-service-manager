# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Dirty rendering preserves model identity, cursor and event delivery semantics."""
import asyncio
import copy
from unittest.mock import patch

import pytest
pytest.importorskip("textual")
from textual.widgets import DataTable, RichLog
from test_tui import make_app, snapshot
from test_tui_events import BufferedEvents


def test_steady_snapshot_changes_only_changed_cells_and_keeps_selection(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("down")
            table = app.query_one("#models", DataTable)
            selected = app.selected_model()
            rows = dict(table.rows)
            gpu_text = str(app.query_one("#gpus").render())
            assert "60%" in gpu_text and "87/144 GiB" in gpu_text
            assert "?%" in gpu_text  # Missing observations never become zero usage.
            with patch.object(table, "clear", wraps=table.clear) as clear, \
                 patch.object(table, "update_cell", wraps=table.update_cell) as update:
                for _ in range(3):
                    app.snapshot = copy.deepcopy(snapshot)
                    app.render_snapshot()
                assert clear.call_count == 0
                assert update.call_count == 0
                app.snapshot["models"][1]["resident_gb"] = 12.3
                app.render_snapshot()
                assert update.call_count == 1
                assert app.selected_model() == selected
                assert all(table.rows[key] is row for key, row in rows.items())
                # Source reordering is not a reason to move a selected model.
                app.snapshot["models"].reverse()
                app.render_snapshot()
                assert app.selected_model() == selected
                assert clear.call_count == 0
    asyncio.run(scenario())


def test_event_append_and_unchanged_connection_do_not_rebuild_log(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        events = BufferedEvents()
        app.event_reader = events
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            log = app.query_one("#events", RichLog)
            status = app.query_one("#event-status")
            app.update_events()
            with patch.object(log, "clear", wraps=log.clear) as clear, \
                 patch.object(log, "write", wraps=log.write) as write, \
                 patch.object(status, "update", wraps=status.update) as update:
                for number in range(1, 5):
                    events.events = [{"id": number, "timestamp": number, "kind": "pin", "model": "demo"}]
                    app.update_events()
                for _ in range(10):
                    app.update_events()
                assert clear.call_count == 0
                assert write.call_count == 4
                assert update.call_count == 0
                assert len(app.event_history) == 4
    asyncio.run(scenario())


def test_event_notifications_coalesce_and_close_safely(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        events = BufferedEvents()
        events.set_notify = lambda callback: setattr(events, "notify", callback)
        app.event_reader = events
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            with patch.object(app, "post_message", wraps=app.post_message) as post:
                for _ in range(100):
                    events.notify()
                notices = [call for call in post.call_args_list if call.args[0].__class__.__name__ == "EventsChanged"]
                assert len(notices) == 1
            events.events = [{"id": 42, "timestamp": 42, "kind": "pin", "model": "wake-on-dirty"}]
            deadline = asyncio.get_running_loop().time() + 2
            while not app.event_history:
                assert asyncio.get_running_loop().time() < deadline
                await pilot.pause(0.02)
            assert app.event_history[0]["id"] == 42
            callback = events.notify
        assert events.notify is None
        callback()  # A callback already captured by the reader is harmless after close.
        assert events.closed
    asyncio.run(scenario())


def test_progress_uses_actual_target_elapsed_and_only_configured_estimate(snapshot):
    async def scenario():
        import time
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app._progress = {"command": "wake", "target": "cold-model", "started": time.monotonic() - 42,
                             "stage": "awaiting scheduler response", "estimate": 210}
            app.render_progress()
            text = str(app.query_one("#result").render())
            assert "cold-model" in text and "42s elapsed" in text
            assert "estimated" in text and "configured" in text
            assert "loading weights" not in text and "%" not in text
            app._progress["estimate"] = None
            app.render_progress()
            assert "ETA unknown" in str(app.query_one("#result").render())
            app._progress = None
    asyncio.run(scenario())


def test_progress_ignores_stale_other_model_and_dataplane_stages(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app._progress = {"command": "wake", "target": "cold-model", "started": 0,
                             "stage": "awaiting scheduler response", "estimate": None,
                             "after_id": 10, "since": 100}
            item = {"id": 11, "timestamp": 101, "kind": "wake_progress", "model": "cold-model",
                    "detail": {"state": "starting", "swap_state": None}}
            for change in [{"id": 9}, {"timestamp": 99}, {"model": "other"}, {"kind": "data_plane_state"}]:
                app.observe_progress({**item, **change})
                assert app._progress["stage"] == "awaiting scheduler response"
            app.observe_progress(item)
            assert app._progress["stage"] == "observed: state starting, swap unknown"
            assert app._progress is not None  # An event never completes the local HTTP operation.
            app._progress = None
    asyncio.run(scenario())


def test_cold_wake_progress_uses_sanitized_model_log_stage_only(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app._progress = {"command": "wake", "target": "cold-model", "started": 0,
                             "stage": "awaiting scheduler response", "estimate": None,
                             "after_id": 10, "since": 100}
            valid = {"id": 11, "timestamp": 101, "kind": "wake_progress", "model": "cold-model",
                     "detail": {"stage": "health_wait", "source": "llama-swap",
                                "source_model": "cold-model", "progress_source": "per_model_log",
                                "log_epoch": "epoch", "sequence": 1, "received_at": 101,
                                "trusted_for_quiet": False}}
            app.observe_progress(valid)
            assert app._progress["stage"] == "observed: waiting for health (source: llama-swap; advisory)"
            app.observe_progress({**valid, "detail": {**valid["detail"], "source_model": "other"}})
            assert app._progress["stage"].endswith("advisory)")
            app._progress = None
    asyncio.run(scenario())
