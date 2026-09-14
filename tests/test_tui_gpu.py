# Generated-By: OpenCode / deepseek-v4.1-flash
"""Six-GPU summary rendering: partial/empty probes, recovery and resize (#247).

Headless CPU tests using fixture snapshots only.  The UI view is additive and
never rewrites the scheduler snapshot or policy input.
"""

import asyncio
import copy
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from rich.console import Console
from tui.app import SchedulerApp
from test_llm import snapshot
from test_tui import IdleEvents, clipboard, record_clipboard, reset_clipboard

CLI = Path(__file__).resolve().parents[1] / "cli" / "llm"


def gpu(index, used=None, managed=None, total=144):
    return {"index": index, "uuid": None, "total_gb": total, "used_gb": used,
            "free_gb": None if total is None or used is None else total - used,
            "managed_gb": managed, "external_gb": 5, "utilization_percent": None,
            "external_processes": []}


def six_gpu_snapshot(base, gpus=None):
    snap = copy.deepcopy(base)
    snap["gpus"] = gpus if gpus is not None else [
        gpu(index, used=10 + index, managed=index) for index in range(6)]
    return snap


def make_app(snap):
    api = SimpleNamespace(**runpy.run_path(str(CLI)))
    return record_clipboard(SchedulerApp(_Client(snap), api, event_reader=IdleEvents()))


class _Client:
    def __init__(self, snap):
        self.snap = snap
        self.calls = []

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        return self.snap


def gpu_text(app):
    return str(app.query_one("#gpus", Static).render())


def test_all_six_gpu_indices_render_and_stay_mapped(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            text = gpu_text(app)
            for index in range(6):
                assert "GPU%s" % index in text
            assert len(app._gpu_lines) == 6
            assert [row["index"] for row in app._gpu_lines] == list(range(6))
            assert all(row["fresh"] for row in app._gpu_lines)
            assert "RAM" in str(app.query_one("#memory", Static).render())
    asyncio.run(scenario())


def test_gpu_capacity_is_never_hardcoded_and_unknown_indices_are_kept(snapshot):
    async def scenario():
        # Non-contiguous known indices: the UI must follow observations.
        app = make_app(six_gpu_snapshot(snapshot, [gpu(2, used=20), gpu(5, used=30)]))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            assert [row["index"] for row in app._gpu_lines] == [2, 5]
            text = gpu_text(app)
            assert "GPU2" in text and "GPU5" in text and "GPU0" not in text
    asyncio.run(scenario())


def test_empty_probe_marks_known_indices_unavailable_without_old_numbers(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            assert "GPU3" in gpu_text(app)
            # A failed/empty probe must not fabricate or keep stale numbers.
            app.snapshot = six_gpu_snapshot(snapshot, [])
            app.snapshot["errors"] = ["gpus: unavailable"]
            app.render_snapshot()
            text = gpu_text(app)
            for index in range(6):
                assert "GPU%s unavailable" % index in text
            assert "GPU3 used" not in text and "13/144G" not in text
            assert [row["index"] for row in app._gpu_lines] == list(range(6))
            assert not any(row["fresh"] for row in app._gpu_lines)
            # The real snapshot keeps its own (empty) gpus: UI-only preservation.
            assert app.snapshot["gpus"] == []
    asyncio.run(scenario())


def test_partial_probe_keeps_missing_index_but_restores_on_recovery(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            partial = [gpu(0, used=10, managed=0), gpu(2, used=12, managed=2), gpu(4, used=14, managed=4)]
            app.snapshot = six_gpu_snapshot(snapshot, partial)
            app.render_snapshot()
            text = gpu_text(app)
            assert "GPU0 used" in text and "GPU2 used" in text and "GPU4 used" in text
            assert "GPU1 unavailable" in text and "GPU5 unavailable" in text
            # Recovery replaces the unavailable marker with fresh values.
            app.snapshot = six_gpu_snapshot(snapshot)
            app.render_snapshot()
            text = gpu_text(app)
            assert "GPU1 unavailable" not in text
            assert "GPU1 used" in text and "11/144G" in text
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(40, 24), (60, 24), (120, 40)])
def test_six_gpus_survive_resize_and_are_scroll_accessible(snapshot, size):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert len(app._gpu_lines) == 6
            for index in range(6):
                assert "GPU%s" % index in gpu_text(app)
            assert "RAM" in str(app.query_one("#memory", Static).render())
    asyncio.run(scenario())


def test_usage_roundtrip_keeps_gpu_and_ram_summary(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        usage = {"days": 7, "by": "container", "known": True, "error": None, "rows": [],
                 "totals": {"requests": 0, "input_tokens": 0, "output_tokens": 0}}
        original = app.client.request

        def routed(method, path, payload=None, **kwargs):
            if path.startswith("/v1/usage"):
                return usage
            return original(method, path, payload, **kwargs)

        app.client.request = routed
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            app.show_usage(app.api.build_parser().parse_args(["usage"]))
            await app.workers.wait_for_complete()
            assert app.usage_active
            app.show_status()
            await app.workers.wait_for_complete()
            assert not app.usage_active
            assert "GPU5" in gpu_text(app)
            assert "RAM" in str(app.query_one("#memory", Static).render())
    asyncio.run(scenario())


def test_gpu_bar_uses_one_total_capacity_denominator(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            assert app.gpu_bar(72, 144) == "█" * 5 + "░" * 5
            assert app.gpu_bar(144, 144) == "█" * 10
            assert app.gpu_bar(0, 144) == "░" * 10
            assert app.gpu_bar(None, 144) is None  # Unknown never becomes zero.
            assert app.gpu_bar(5, 0) is None
    asyncio.run(scenario())


def test_stale_gpu_line_copies_nothing_current(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            app.snapshot = six_gpu_snapshot(snapshot, [gpu(1, used=20)])
            app.render_snapshot()
            reset_clipboard(app)
            app.copy_gpu_index(3)  # Stale index from the earlier full observation.
            assert clipboard(app) == ""
            assert "stale" in str(app.query_one("#event-status", Static).render())
    asyncio.run(scenario())


def first_visual_line(app, index):
    """The rendered visual line where a GPU index starts (Rich wrapping aware)."""
    widget = app.query_one("#gpus", Static)
    for line in range(0, 200):
        item = app.gpu_visual_line(widget, line)
        if item is None:
            return None
        if item.get("index") == index:
            return line
    return None


def test_compact_legend_and_wrapped_rows_map_to_the_right_gpu(snapshot):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=(40, 40)) as pilot:
            await app.workers.wait_for_complete()
            # Compact mode starts with a legend; a click there copies nothing.
            assert app._gpu_render[0]["legend"]
            reset_clipboard(app)
            await pilot.click("#gpus", offset=(2, 0))
            await pilot.pause()
            assert clipboard(app) == ""
            assert "legend" in str(app.query_one("#event-status", Static).render())
            # Wrap long non-compact lines in a narrow viewport and verify the
            # visual-line mapping still resolves each row to its GPU identity.
            app.terminal_width = 90
            app.render_snapshot()
            await pilot.pause()
            start = first_visual_line(app, 3)
            assert start is not None and start > 0  # GPU3 is not the first line.
            reset_clipboard(app)
            await pilot.click("#gpus", offset=(2, start))
            await pilot.pause()
            assert clipboard(app) == app.gpu_text(gpu(3, used=13, managed=3))
    asyncio.run(scenario())


def test_reserved_row_maps_correctly_when_it_wraps(snapshot):
    async def scenario():
        snap = six_gpu_snapshot(snapshot)
        snap["reserves"] = [{"id": "r", "gpu": 0, "size_gb": 80, "until": 1800003600, "by": "owner"}]
        app = make_app(snap)
        async with app.run_test(size=(40, 40)) as pilot:
            await app.workers.wait_for_complete()
            start = first_visual_line(app, 1)
            assert start is not None and start > 0  # Reserved GPU0 wraps above it.
            reset_clipboard(app)
            await pilot.click("#gpus", offset=(2, start))
            await pilot.pause()
            assert clipboard(app) == app.gpu_text(gpu(1, used=11, managed=1))
    asyncio.run(scenario())


def test_reordered_and_partial_indices_copy_by_stable_index(snapshot):
    async def scenario():
        # Snapshot order is reversed/non-contiguous; the UI sorts indices and
        # /copy and clicks use the stable GPU index, not the list position.
        app = make_app(six_gpu_snapshot(snapshot, [gpu(5, used=15, managed=5),
                                                   gpu(0, used=10, managed=0),
                                                   gpu(3, used=13, managed=3)]))
        async with app.run_test(size=(200, 40)) as pilot:
            await app.workers.wait_for_complete()
            assert app._gpu_order == [0, 3, 5]
            for command, expected in [("/copy gpu 5", "GPU5 used 15/144G"),
                                      ("/copy gpu 3", "GPU3 used 13/144G"),
                                      ("/copy gpu 0", "GPU0 used 10/144G")]:
                reset_clipboard(app)
                app.submit_command(command)
                await app.workers.wait_for_complete()
                assert clipboard(app).startswith(expected), command
            # A click on line 1 copies the second sorted index (GPU3), proving
            # the mapping uses rendered order and stable identity.
            reset_clipboard(app)
            await pilot.click("#gpus", offset=(2, 1))
            await pilot.pause()
            assert clipboard(app).startswith("GPU3 used 13/144G")
            app.submit_command("/copy gpu 9")
            await app.workers.wait_for_complete()
            assert "needs an observed GPU index" in str(app.query_one("#result", Static).render())
            # A stale known index is reported, not silently copied.
            app.snapshot = six_gpu_snapshot(snapshot, [gpu(3, used=13, managed=3)])
            app.render_snapshot()
            reset_clipboard(app)
            app.submit_command("/copy gpu 5")
            await app.workers.wait_for_complete()
            assert clipboard(app) == ""
            assert "stale" in str(app.query_one("#event-status", Static).render())
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(40, 24), (60, 24), (100, 30), (120, 40)])
def test_six_gpus_fit_the_summary_without_clipping_a_row(snapshot, size):
    async def scenario():
        app = make_app(six_gpu_snapshot(snapshot))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            gpus = app.query_one("#gpus", Static)
            assert len(app._gpu_lines) == 6
            text = gpu_text(app)
            for index in range(6):
                assert "GPU%s" % index in text
            # Count Rich-wrapped visual rows and require them to fit the summary
            # box (CSS max-height 14); a hidden row would mean clipped content.
            width = max(1, int(gpus.content_region.width))
            console = Console(width=width, no_color=True, legacy_windows=False, force_terminal=False)
            visual_lines = sum(max(1, len(item["rich"].wrap(console, width)))
                               for item in app._gpu_render)
            memory_lines = len(str(app.query_one("#memory", Static).render()).splitlines())
            if app.screen.has_class("narrow"):
                assert visual_lines + memory_lines <= 14
            else:
                assert max(visual_lines, memory_lines) <= 14
            assert "RAM" in str(app.query_one("#memory", Static).render())
    asyncio.run(scenario())
