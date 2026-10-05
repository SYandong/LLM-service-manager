# Generated-By: Codex / gpt-6.1-sol
"""Headless fleet navigation, selection and snapshot preservation contracts."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("textual")
from textual import events
from textual.containers import VerticalScroll
from rich.text import Text

from tui.fleet_app import ClaimDialog, FleetHelpDialog, GpuDetailDialog, GpuOverview
from tui.fleet_selection import SelectableStatic
from test_tui_fleet import make_app, ready


@pytest.fixture
def interaction_snapshot():
    return json.loads((Path(__file__).parent / "fixtures/fleet_gpu_overview.json").read_text())


async def wheel(pilot, kind=events.MouseScrollDown, shift=False):
    # Target the visible viewport so this remains valid after the long child scrolls.
    await pilot._post_mouse_events([kind], "#fleet-gpu-scroll", (5, 3), shift=shift)
    await pilot.pause()


async def drag(pilot, selector, start, end):
    await pilot.mouse_down(selector, offset=start)
    await pilot.hover(selector, offset=end)
    await pilot.mouse_up(selector, offset=end)


def test_wheel_burst_uses_the_last_tick_quiet_gap_and_consumes_rejected_ticks(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            with patch("tui.fleet_app.monotonic", side_effect=[1, 1.1, 1.22, 1.34, 1.46, 1.72]):
                await wheel(pilot)
                await pilot.pause()
                assert app.selected_gpu == 1
                selected_scroll = viewport.scroll_y
                for _ in range(4):
                    await wheel(pilot)
                    assert app.selected_gpu == 1
                    assert viewport.scroll_y == selected_scroll
                assert app._gpu_wheel_at == 1.46
                await wheel(pilot)
                assert app.selected_gpu == 2
                assert viewport.scroll_y == app._gpu_anchors[2]
            assert app._exception is None
    asyncio.run(scenario())


def test_owned_wheels_prevent_defaults_even_at_gpu_boundaries(interaction_snapshot):
    class Wheel:
        shift = False

        def __init__(self):
            self.stopped = self.prevented = False

        def stop(self):
            self.stopped = True

        def prevent_default(self):
            self.prevented = True

    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            with patch("tui.fleet_app.monotonic", side_effect=[1, 1.1, 1.4, 1.7, 2]):
                first, ignored = Wheel(), Wheel()
                app.handle_gpu_wheel(first, -1)
                app.handle_gpu_wheel(ignored, 1)
                assert app.selected_gpu == 0
                assert first.stopped and first.prevented and ignored.stopped and ignored.prevented
                app.handle_gpu_wheel(Wheel(), 1)
                assert app.selected_gpu == 1
                app.select_gpu(5)
                last = Wheel()
                app.handle_gpu_wheel(last, 1)
                assert app.selected_gpu == 5 and last.stopped and last.prevented
                app.handle_gpu_wheel(Wheel(), -1)
                assert app.selected_gpu == 4
    asyncio.run(scenario())


def test_wheel_over_viewport_padding_selects_once_without_default_scroll(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.styles.padding = 1
            await pilot.pause()
            with patch("tui.fleet_app.monotonic", return_value=1) as tick:
                await pilot._post_mouse_events([events.MouseScrollDown], "#fleet-gpu-scroll", (0, 0))
                tick.assert_called_once()
            assert app.selected_gpu == 1
            assert viewport.scroll_y == app._gpu_anchors[1]
    asyncio.run(scenario())


def test_wheel_over_empty_compact_viewport_selects_the_next_gpu(interaction_snapshot):
    async def scenario():
        interaction_snapshot["gpus"] = interaction_snapshot["gpus"][:2]
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("z")
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            point_y = viewport.size.height - 2
            assert viewport.region.y + point_y >= overview.region.bottom
            with patch("tui.fleet_app.monotonic", return_value=1) as tick:
                await pilot._post_mouse_events([events.MouseScrollDown], "#fleet-gpu-scroll", (5, point_y))
                tick.assert_called_once()
            assert app.selected_gpu == 1 and viewport.scroll_y == 0
    asyncio.run(scenario())


def test_shift_wheel_and_page_keys_scroll_without_changing_gpu(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            await wheel(pilot, shift=True)
            await pilot.pause()
            assert viewport.scroll_y > 0 and app.selected_gpu == 0
            assert app._gpu_wheel_at is None
            scroll = viewport.scroll_y
            await pilot.press("pagedown")
            assert viewport.scroll_y > scroll and app.selected_gpu == 0
            await pilot.press("pageup")
            assert viewport.scroll_y == scroll and app.selected_gpu == 0
            viewport.scroll_to(y=0, animate=False)
            await pilot.press("pageup")
            assert viewport.scroll_y == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
def test_allocation_navigation_keeps_heading_and_chart_in_the_stationary_viewport(interaction_snapshot, size):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            await pilot.press("down", "down")
            assert viewport.scroll_y == app._gpu_anchors[2]
            original_scroll = viewport.scroll_y
            for key in ("right", "right", "left", "right"):
                await pilot.press(key)
                assert app.selected_gpu == 2
                assert viewport.scroll_y == original_scroll
                heading, rows = overview.heading_rows[2]
                assert viewport.scroll_y <= heading < viewport.scroll_y + viewport.size.height
                assert heading + rows + overview.bar_rows <= viewport.scroll_y + viewport.size.height
            viewport.scroll_to(y=original_scroll + 3, animate=False)
            await pilot.pause()
            await pilot.press("left")
            assert viewport.scroll_y == original_scroll + 3
    asyncio.run(scenario())


def test_selected_heading_styles_and_pulse_never_mutate_copy_source(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            before = overview.render()
            row, count_rows = overview.heading_rows[0]
            lines = before.plain.splitlines(keepends=True)
            start = sum(map(len, lines[:row]))
            end = sum(map(len, lines[:row + count_rows]))
            for offset in range(start + 2, end):
                if before.plain[offset] != "\n":
                    style = before.get_style_at_offset(app.console, offset)
                    assert style.bold and style.bgcolor is not None
            marker = before.get_style_at_offset(app.console, start).bgcolor
            overview.pulse_marker()
            after = overview.render()
            assert after.plain == before.plain == overview._source_text.plain
            assert after.get_style_at_offset(app.console, start).bgcolor != marker
            await drag(pilot, "#fleet-gpus", (2, 0), (7, 0))
            assert overview.selected_text == "GPU 0"
            for _ in range(3):
                overview.pulse_marker()
                assert overview.selected_text == "GPU 0"
                assert overview.render().plain == before.plain
    asyncio.run(scenario())


def test_gpu_drag_freezes_source_click_targets_and_anchors_through_refresh(interaction_snapshot):
    async def scenario():
        app, client = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            source, hits = overview.render().plain, list(overview.hits)
            anchors, service_hits = dict(app._gpu_anchors), dict(overview.service_hits)
            await pilot.mouse_down("#fleet-gpus", offset=(2, 0))
            client.snapshot["services"][0]["model"] = "changed-" + "long model " * 20
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            await pilot.hover("#fleet-gpus", offset=(7, 0))
            await pilot.mouse_up("#fleet-gpus", offset=(7, 0))
            assert overview.selected_text == "GPU 0"
            assert overview.render().plain == source
            assert overview.hits == hits and overview.service_hits == service_hits
            assert app._gpu_anchors == anchors
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                copy.assert_called_once_with("GPU 0")
                assert app.is_running
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert overview.selected_text == "GPU 0" and overview.render().plain == source
            await pilot.press("down")
            assert not overview.has_selection
            assert "changed-long model" in overview.render().plain
            assert overview.hits != hits and app._gpu_anchors != anchors
            assert app.query_one("#fleet-gpu-scroll").scroll_y == app._gpu_anchors[1]
    asyncio.run(scenario())


def test_focus_refresh_and_help_close_keep_manual_scroll(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("down")
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.scroll_to(y=viewport.scroll_y + 4, animate=False)
            await pilot.pause()
            scroll = viewport.scroll_y
            app.focus_view()
            await pilot.pause()
            assert viewport.scroll_y == scroll
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert viewport.scroll_y == scroll
            await pilot.press("question_mark")
            assert isinstance(app.screen, FleetHelpDialog)
            await pilot.press("escape")
            assert viewport.scroll_y == scroll
            assert app.focused is app.query_one("#fleet-gpus")
    asyncio.run(scenario())


@pytest.mark.parametrize("panel_name", ["fleet-detail-text", "fleet-history-text"])
def test_people_detail_and_history_copy_preserve_selected_refresh_snapshot(interaction_snapshot, panel_name):
    async def scenario():
        app, client = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            panel = app.query_one("#" + panel_name, SelectableStatic)
            panel.scroll_visible(animate=False, top=True)
            await pilot.pause()
            source = panel.render().plain
            await drag(pilot, "#" + panel_name, (0, 0), (5, 0))
            expected = panel.selected_text
            assert expected
            selected = app.selected_service_id()
            service = next(item for item in client.snapshot["services"] if item["id"] == selected)
            service["model"] = "refresh changed this service"
            client.snapshot["generated_at"] += 60
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert panel.render().plain == source and panel.selected_text == expected
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                copy.assert_called_once_with(expected)
            assert app.is_running
            await pilot.press("g")
            assert not panel.has_selection
    asyncio.run(scenario())


@pytest.mark.parametrize("dialog", ["help", "gpu"])
def test_modal_selection_copy_is_exact_across_refresh_and_clears_on_close(interaction_snapshot, dialog):
    async def scenario():
        app, client = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("question_mark" if dialog == "help" else "enter")
            await ready(app, pilot)
            assert isinstance(app.screen, FleetHelpDialog if dialog == "help" else GpuDetailDialog)
            name = "fleet-help-text" if dialog == "help" else "gpu-service-details"
            panel = app.screen.query_one("#" + name, SelectableStatic)
            source = panel.render().plain
            await drag(pilot, "#" + name, (0, 0), (5, 0))
            expected = panel.selected_text
            assert expected
            if dialog == "help":
                panel.update("pending new help")
            else:
                selected = app.selected_service_id()
                next(item for item in client.snapshot["services"] if item["id"] == selected)["model"] = "new model"
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert panel.render().plain == source and panel.selected_text == expected
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                copy.assert_called_once_with(expected)
            assert app.is_running
            await pilot.press("escape")
            assert app.screen is app.dashboard
            assert app.mouse_captured is None and app._selection_widget is None
    asyncio.run(scenario())


def test_people_and_modal_show_literal_api_friendly_state_and_token_units(interaction_snapshot):
    async def scenario():
        service = interaction_snapshot["services"][0]
        service.update(status="over_limit", api_access="direct", api_address="http://[::1]:8080")
        service["window_24h"].update(prompt_tokens=24000, gen_tokens=12000)
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            assert not app.read_error
            assert app.service_status(app.selected_service()) == "over_limit"
            assert app._table.get_cell("service:" + service["id"], "status").plain == "Inactive"
            text = app.query_one("#fleet-detail-text").render().plain
            assert "Running · inactive" in text and "API: http://[::1]:8080" in text
            assert "input 24,000 tokens" in text and "output 12,000 tokens" in text
            assert "total 36,000 tokens" in text
            await pilot.press("g", "enter")
            await ready(app, pilot)
            assert "API: http://[::1]:8080" in app.screen.query_one("#gpu-service-details").render().plain
    asyncio.run(scenario())


@pytest.mark.parametrize("view", ["gpu", "help", "gpu_details", "claim"])
def test_control_c_without_a_selection_does_not_quit_and_q_does(interaction_snapshot, view):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            if view == "help":
                await pilot.press("question_mark")
                app.screen.query_one("#fleet-help-title").focus(scroll_visible=False)
            elif view == "gpu_details":
                await pilot.press("enter")
                app.screen.query_one("#gpu-allocation-text").focus(scroll_visible=False)
            elif view == "claim":
                app.push_screen(ClaimDialog(app, app.selected_service()))
                await pilot.pause()
                app.screen.query_one("#claim-title").focus(scroll_visible=False)
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                assert app.is_running
                copy.assert_not_called()
            await pilot.press("q")
            assert not app.is_running
        assert app._ui_closed
    asyncio.run(scenario())


@pytest.mark.parametrize("view", ["person", "modal"])
def test_control_c_copies_only_the_current_focused_table_row_without_selection(interaction_snapshot, view):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("p" if view == "person" else "enter")
            await ready(app, pilot)
            table = app.focused
            expected = "\t".join(cell.plain if isinstance(cell, Text) else app.clean(cell)
                                 for cell in table.get_row_at(table.cursor_row))
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                copy.assert_called_once_with(expected)
                assert app.is_running
            panel = app.screen.query_one("#fleet-detail-text" if view == "person" else "#gpu-service-details")
            panel.focus(scroll_visible=False)
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                copy.assert_not_called()
    asyncio.run(scenario())


def test_control_c_preserves_input_selection_when_supported_without_quitting(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("slash")
            field = app.focused
            field.value = "alpha beta"
            if hasattr(field, "selection"):
                field.selection = type(field.selection)(0, 5)
            await pilot.pause()
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+c")
                assert app.is_running
                if hasattr(field, "selected_text"):
                    copy.assert_called_once_with("alpha")
                    assert field.selected_text == "alpha"
                else:
                    copy.assert_not_called()
            await pilot.press("q")
            assert app.is_running and "q" in field.value
    asyncio.run(scenario())
