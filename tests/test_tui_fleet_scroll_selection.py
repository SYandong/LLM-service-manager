# Generated-By: Codex / gpt-6.1-sol
"""SGR mouse selection must keep the frame visible when a new drag begins."""

import asyncio
import inspect
import json
from pathlib import Path
import re
from unittest.mock import patch

import pytest

pytest.importorskip("textual")
from textual import events
from textual._xterm_parser import XTermParser
from textual.app import App
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.strip import Strip

from tui.fleet_app import GpuOverview
from tui.fleet_selection import SelectableStatic
from test_tui_fleet import make_app, ready


def screen_strip(app, y):
    row = app.screen._compositor.render_full_update().strips[y]
    return row if isinstance(row, Strip) else Strip.join(row)


def visible_selection(app, panel, viewport):
    """Choose real screen cells, excluding parent padding and scrollbars."""
    area = panel.content_region.intersection(viewport.scrollable_content_region)
    area = area.intersection(app.screen.region)
    for y in range(area.y, area.bottom):
        strip = screen_strip(app, y)
        visible = strip.crop(area.x, area.right).text
        match = re.search(r"[A-Za-z0-9][A-Za-z0-9 :/_-]{5,}", visible)
        if match is not None:
            x = area.x + match.start()
            if app.get_widget_at(x, y)[0] is panel:
                return (x, y), (x + 6, y), strip.crop(x, x + 6).text
    raise AssertionError("No visible selectable text in synthetic fixture")


class SgrMouse:
    """Use the terminal decoder and App routing, including automatic Click."""

    def __init__(self, app, pilot):
        self.app, self.pilot = app, pilot
        self.parser = (XTermParser(more_data=lambda: False)
                       if "more_data" in inspect.signature(XTermParser).parameters else XTermParser())

    async def send(self, buttons, position, release=False):
        code = "\x1b[<%s;%s;%s%s" % (buttons, position[0] + 1, position[1] + 1,
                                     "m" if release else "M")
        decoded = list(self.parser.feed(code))
        assert len(decoded) == 1 and isinstance(decoded[0], events.MouseEvent)
        await self.app.on_event(decoded[0])
        await self.pilot.pause()

    async def drag(self, start, end, *, reverse=False, shift=False):
        if reverse:
            start, end = end, start
        modifier = 4 if shift else 0
        await self.send(modifier, start)
        await self.send(32 | modifier, end)
        await self.send(modifier, end, release=True)


def assert_visible_highlight(app, start, end, expected):
    strip = screen_strip(app, start[1]).crop(start[0], end[0])
    assert strip.text == expected
    assert all(segment.style and segment.style.reverse for segment in strip)


def source_caret(panel, position):
    """GPU fixtures use single-cell characters and already wrapped source rows."""
    lines = panel.render().plain.splitlines(keepends=True)
    row = position[1] - panel.content_region.y
    column = position[0] - panel.content_region.x
    return sum(map(len, lines[:row])) + min(column, len(lines[row].rstrip("\n")))


class ScrolledSelectionApp(App):
    CSS = """
    #selection-scroll { width: 35; height: 12; padding: 1; border: solid white;
        overflow-x: auto; overflow-y: auto; }
    #selection-panel { width: 70; height: auto; padding: 1 2; }
    """
    BINDINGS = [Binding("ctrl+c", "copy_selection", "Copy", show=False, priority=True)]

    def __init__(self):
        super().__init__()
        self.source = "\n".join("ROW%02d LEFT%02d MIDDLE%02d RIGHT%02d" % (row, row, row, row)
                                for row in range(30))

    def compose(self):
        with VerticalScroll(id="selection-scroll"):
            yield SelectableStatic(self.source, id="selection-panel", markup=False)

    def action_copy_selection(self):
        self.copy_to_clipboard(self.query_one("#selection-panel", SelectableStatic).selected_text)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("shift", [False, True], ids=["direct", "shift"])
def test_scrolled_static_reselection_keeps_pending_source_until_explicit_clear(reverse, shift):
    async def scenario():
        app = ScrolledSelectionApp()
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            panel = app.query_one("#selection-panel", SelectableStatic)
            viewport = app.query_one("#selection-scroll", VerticalScroll)
            viewport.scroll_to(x=12, y=11, animate=False)
            await pilot.pause()
            assert viewport.scroll_x == 12 and viewport.scroll_y == 11
            mouse = SgrMouse(app, pilot)
            start, end, expected = visible_selection(app, panel, viewport)
            await mouse.drag(start, end)
            assert panel.selected_text == expected
            pending = "\n".join("NEWROW%02d pending row" % row for row in range(5)) + "\n" + app.source
            panel.update(pending)
            await pilot.pause()
            assert panel.render().plain == app.source
            start, end, expected = visible_selection(app, panel, viewport)
            copied = []
            with patch.object(app, "copy_to_clipboard", side_effect=copied.append):
                await mouse.drag(start, end, reverse=reverse, shift=shift)
                await pilot.press("ctrl+c")
            assert panel.selected_text == expected
            assert copied == [expected] and app.is_running
            assert panel.render().plain == app.source
            assert_visible_highlight(app, start, end, expected)
            assert viewport.scroll_x == 12 and viewport.scroll_y == 11
            panel.clear_selection()
            await pilot.pause()
            assert panel.render().plain == pending and not panel.has_selection
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("shift", [False, True], ids=["direct", "shift"])
def test_scrolled_gpu_reselection_retains_displayed_source_and_click_targets(size, reverse, shift):
    async def scenario():
        snapshot = json.loads((Path(__file__).parent / "fixtures/fleet_gpu_overview.json").read_text())
        app, client = make_app(snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            app.select_gpu(2)
            await ready(app, pilot)
            panel = app.query_one("#fleet-gpus", GpuOverview)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            assert viewport.scroll_y > 0 and panel.region.y < viewport.region.y
            scroll = viewport.scroll_y
            mouse = SgrMouse(app, pilot)
            start, end, expected = visible_selection(app, panel, viewport)
            await mouse.drag(start, end)
            assert panel.selected_text == expected
            frozen = panel.render().plain
            hits, service_hits, anchors = list(panel.hits), dict(panel.service_hits), dict(panel.anchors)
            client.snapshot["services"][0]["model"] = " ".join("REFRESHMODEL%02d" % i for i in range(150))
            client.snapshot["generated_at"] += 60
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert panel.render().plain == frozen
            assert (panel.hits, panel.service_hits, panel.anchors) == (hits, service_hits, anchors)
            start, end, expected = visible_selection(app, panel, viewport)
            copied = []
            with patch.object(app, "copy_to_clipboard", side_effect=copied.append):
                await mouse.drag(start, end, reverse=reverse, shift=shift)
                await pilot.press("ctrl+c")
            assert panel.selected_text == expected
            assert copied == [expected] and app.is_running
            assert panel.render().plain == frozen
            assert (panel.hits, panel.service_hits, panel.anchors) == (hits, service_hits, anchors)
            assert app._gpu_anchors is panel.anchors
            assert viewport.scroll_y == scroll
            assert_visible_highlight(app, start, end, expected)
            panel.clear_selection()
            await pilot.pause()
            assert "REFRESHMODEL00" in panel.render().plain
            assert panel.render().plain != frozen and not panel.has_selection
            assert panel.anchors[2] > anchors[2] and panel.hits != hits
            assert app._gpu_anchors is panel.anchors
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
@pytest.mark.parametrize("step", [-1, 1], ids=["wheel_up", "wheel_down"])
@pytest.mark.parametrize("shift", [False, True], ids=["direct", "shift"])
def test_captured_sgr_drag_tracks_visible_cells_across_wheel_scroll(size, step, shift):
    async def scenario():
        snapshot = json.loads((Path(__file__).parent / "fixtures/fleet_gpu_overview.json").read_text())
        app, client = make_app(snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            app.select_gpu(2)
            await ready(app, pilot)
            panel = app.query_one("#fleet-gpus", GpuOverview)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            frozen = panel.render().plain
            initial_scroll = viewport.scroll_y
            start, start_end, visible_start = visible_selection(app, panel, viewport)
            anchor = source_caret(panel, start)
            assert frozen[anchor:source_caret(panel, start_end)] == visible_start
            mouse = SgrMouse(app, pilot)
            modifier = 4 if shift else 0
            await mouse.send(modifier, start)
            assert panel.dragging and app.mouse_captured is panel
            client.snapshot["services"][0]["model"] = "WHEELREFRESH " * 150
            client.snapshot["generated_at"] += 60
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert panel.render().plain == frozen
            await mouse.send((64 if step < 0 else 65) | modifier, start)
            assert viewport.scroll_y == initial_scroll + step
            assert panel.dragging and app.mouse_captured is panel
            assert app.selected_gpu == 2 and panel.render().plain == frozen
            end_start, end, visible_end = visible_selection(app, panel, viewport)
            endpoint = source_caret(panel, end)
            assert frozen[source_caret(panel, end_start):endpoint] == visible_end
            expected = frozen[min(anchor, endpoint):max(anchor, endpoint)]
            assert expected
            copied = []
            with patch.object(app, "copy_to_clipboard", side_effect=copied.append):
                await mouse.send(32 | modifier, end)
                await mouse.send(modifier, end, release=True)
                await pilot.press("ctrl+c")
            assert panel.selected_text == expected and copied == [expected]
            assert app.is_running and app.mouse_captured is None
            assert app.selected_gpu == 2 and panel.render().plain == frozen
            assert viewport.scroll_y == initial_scroll + step
            # The endpoint came from the compositor after scrolling, rather
            # than from the old widget-local coordinates before the wheel.
            highlighted = screen_strip(app, end[1]).crop(end_start[0], end[0])
            assert highlighted.text == visible_end
            if endpoint > anchor:
                assert any(segment.style and segment.style.reverse for segment in highlighted)
            panel.clear_selection()
            await pilot.pause()
            assert "WHEELREFRESH" in panel.render().plain and not panel.has_selection
    asyncio.run(scenario())
