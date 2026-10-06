# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Headless fleet navigation, selection and snapshot preservation contracts."""

import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("textual")
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import DataTable
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


def two_service_snapshot(snapshot):
    first = copy.deepcopy(snapshot["services"][0])
    first["gpus"] = [0]
    second = copy.deepcopy(first)
    second.update(id="second-service", model="second-model", gpu_gb=1)
    snapshot["services"] = [first, second]
    snapshot["gpus"][0]["occupants"] = [
        {"container": service["container"], "kind": "llm", "used_gb": 1,
         "service_id": service["id"]} for service in snapshot["services"]]
    return snapshot


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
def test_each_wheel_tick_scrolls_contents_without_jumping_gpu(interaction_snapshot, size):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            start = viewport.scroll_y
            for number in range(1, 7):
                await wheel(pilot)
                assert viewport.scroll_y == start + number
            assert app.selected_gpu == 0
            await wheel(pilot, events.MouseScrollUp)
            assert viewport.scroll_y == start + 5
            assert app._exception is None
    asyncio.run(scenario())


def test_owned_wheels_are_consumed_once_at_scroll_boundaries(interaction_snapshot):
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
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.scroll_to(y=0, animate=False)
            await pilot.pause()
            first = Wheel()
            app.handle_gpu_wheel(first, -1)
            await pilot.pause()
            assert viewport.scroll_y == 0 and first.stopped and first.prevented
            viewport.scroll_to(y=viewport.max_scroll_y, animate=False)
            await pilot.pause()
            last = Wheel()
            app.handle_gpu_wheel(last, 1)
            await pilot.pause()
            assert viewport.scroll_y == viewport.max_scroll_y
            assert app.selected_gpu == 5 and last.stopped and last.prevented
    asyncio.run(scenario())


def test_wheel_burst_retains_every_tick_before_the_next_refresh(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            start = viewport.scroll_y
            for _ in range(8):
                app.handle_gpu_wheel(events.MouseScrollDown(
                    app.query_one("#fleet-gpus"), 3, 4, 0, 0, 0, False, False, False), 1)
            assert viewport.scroll_y == start + 8
            await pilot.pause()
            assert viewport.scroll_y == start + 8 and app.selected_gpu == 0
    asyncio.run(scenario())


def test_wheel_over_viewport_padding_scrolls_exactly_once(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.styles.padding = 1
            await pilot.pause()
            start = viewport.scroll_y
            await pilot._post_mouse_events([events.MouseScrollDown], "#fleet-gpu-scroll", (0, 0))
            await pilot.pause()
            assert viewport.scroll_y == start + 1 and app.selected_gpu == 0
    asyncio.run(scenario())


def test_wheel_over_compact_viewport_preserves_the_visible_overview(interaction_snapshot):
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
            await pilot._post_mouse_events([events.MouseScrollDown], "#fleet-gpu-scroll", (5, point_y))
            await pilot.pause()
            assert app.selected_gpu == 0 and viewport.scroll_y == 0
    asyncio.run(scenario())


def test_scroll_position_drives_gpu_selection_without_realignment(interaction_snapshot):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.scroll_to(y=app._gpu_anchors[2] + 3, animate=False)
            await pilot.pause()
            assert app.selected_gpu == 2
            start = viewport.scroll_y
            await wheel(pilot)
            assert viewport.scroll_y == start + 1 and app.selected_gpu == 2
            await pilot.press("right")
            assert viewport.scroll_y == start + 1 and app.selected_gpu == 2
            await wheel(pilot, shift=True)
            assert viewport.scroll_y == start + 2
            await pilot.press("pagedown")
            assert viewport.scroll_y > start + 2
            await pilot.press("pageup")
            assert viewport.scroll_y == start + 2
            viewport.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert app.selected_gpu == 0
            await pilot.press("pageup")
            assert viewport.scroll_y == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("selected_text", [False, True])
def test_keyboard_selection_survives_alignment_of_short_empty_gpu_panels(interaction_snapshot, selected_text):
    async def scenario():
        interaction_snapshot["services"] = []
        for gpu in interaction_snapshot["gpus"]:
            gpu.update(occupants=[], used_gb=0, util_percent=0)
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            assert app._gpu_anchors[1] < viewport.content_size.height / 2
            if selected_text:
                overview = app.query_one("#fleet-gpus", GpuOverview)
                start = overview.render().plain.splitlines()[0].index("GPU ")
                await drag(pilot, "#fleet-gpus", (start, 0), (start + 5, 0))
                assert overview.has_selection
            for expected in (1, 2, 3):
                await pilot.press("down")
                await pilot.pause()
                assert app.selected_gpu == expected
                assert viewport.scroll_y == min(app._gpu_anchors[expected], viewport.max_scroll_y)
            await pilot.press("up")
            await pilot.pause()
            assert app.selected_gpu == 2 and viewport.scroll_y == app._gpu_anchors[2]
    asyncio.run(scenario())


@pytest.mark.parametrize("key,expected_gpu", [("pageup", 0), ("pagedown", 5)])
def test_page_key_at_scroll_boundary_catches_up_gpu_after_clearing_text(interaction_snapshot, key, expected_gpu):
    async def scenario():
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            app.select_gpu(2)
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            row = overview.anchors[2]
            start = overview.render().plain.splitlines()[row].index("GPU ")
            await drag(pilot, "#fleet-gpus", (start, row), (start + 5, row))
            assert overview.has_selection
            boundary = 0 if key == "pageup" else viewport.max_scroll_y
            viewport.scroll_to(y=boundary, animate=False)
            await pilot.pause()
            assert viewport.scroll_y == boundary and app.selected_gpu == 2
            await pilot.press(key)
            await pilot.pause()
            assert not overview.has_selection and viewport.scroll_y == boundary
            assert app.selected_gpu == expected_gpu
    asyncio.run(scenario())


@pytest.mark.parametrize("active_window,idle_hours,recent,idle", [
    (900, 6, "15m", "6h"), (1800, 2, "30m", "2h"),
])
def test_help_explains_current_activity_and_idle_intervals(
        interaction_snapshot, active_window, idle_hours, recent, idle):
    async def scenario():
        interaction_snapshot["config"].update(
            active_window_seconds=active_window, idle_limit_hours=idle_hours)
        app, _ = make_app(interaction_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("question_mark")
            text = app.screen.query_one("#fleet-help-text").render().plain
            assert "Idle: no inference activity in the last %s." % recent in text
            assert "still running, idle for at least %s." % idle in text
            assert "Shared APIs have no idle reminder" in text
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
            if dialog == "help":
                help_text = app.screen.query_one("#fleet-help-text", SelectableStatic).render().plain
                assert "Shared APIs have no idle reminder" in help_text
                assert "use the proxy" not in help_text
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


@pytest.mark.parametrize("panel_name", ["gpu-service-details", "gpu-service-history"])
@pytest.mark.parametrize("navigation", ["key", "click"])
def test_nonfirst_modal_service_selection_survives_row_rebuild_and_clears_on_navigation(interaction_snapshot, panel_name, navigation):
    async def scenario():
        snapshot = two_service_snapshot(interaction_snapshot)
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 35)) as pilot:
            await ready(app, pilot)
            await pilot.press("enter")
            await ready(app, pilot)
            table = app.screen.query_one("#gpu-detail-services", DataTable)
            table.move_cursor(row=1, animate=False)
            await ready(app, pilot)
            assert app.selected_service_id() == "second-service"
            panel = app.screen.query_one("#" + panel_name, SelectableStatic)
            source = panel.render().plain
            await drag(pilot, "#" + panel_name, (0, 0), (5, 0))
            selected = panel.selected_text
            assert selected
            client.snapshot["services"][1]["model"] = "refreshed second model"
            client.snapshot["generated_at"] += 60
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert app.selected_service_id() == "second-service" and table.cursor_row == 1
            assert panel.has_selection and panel.selected_text == selected
            assert panel.render().plain == source
            with patch.object(app, "copy_to_clipboard") as clipboard:
                await pilot.press("ctrl+c")
                clipboard.assert_called_once_with(selected)
            table.focus(scroll_visible=False)
            if navigation == "key":
                await pilot.press("up")
            else:
                await pilot.click("#gpu-detail-services", offset=(2, table.header_height))
            await ready(app, pilot)
            assert app.selected_service_id() == snapshot["services"][0]["id"]
            assert not panel.has_selection
    asyncio.run(scenario())


@pytest.mark.parametrize("panel_name", ["fleet-detail-text", "fleet-history-text"])
@pytest.mark.parametrize("rebuild", ["reorder", "resize"])
@pytest.mark.parametrize("navigation", ["key", "click"])
def test_nonfirst_people_selection_survives_programmatic_rebuild_and_clears_on_navigation(interaction_snapshot, panel_name, rebuild, navigation):
    async def scenario():
        app, client = make_app(two_service_snapshot(interaction_snapshot))
        async with app.run_test(size=(100, 35)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            app._table.move_cursor(row=app.row_keys.index("service:second-service"), animate=False)
            await ready(app, pilot)
            assert app.selected_service_id() == "second-service"
            panel = app.query_one("#" + panel_name, SelectableStatic)
            panel.scroll_visible(animate=False, top=True)
            await pilot.pause()
            source = panel.render().plain
            await drag(pilot, "#" + panel_name, (0, 0), (5, 0))
            selected = panel.selected_text
            assert selected
            if rebuild == "reorder":
                client.snapshot["services"][1].update(model="refreshed second model", gpu_gb=100)
                client.snapshot["generated_at"] += 60
                await app.refresh_fleet().wait()
            else:
                await pilot.resize_terminal(80, 35)
            await ready(app, pilot)
            assert app.selected_service_id() == "second-service"
            assert app.history["service_id"] == "second-service"
            assert panel.has_selection and panel.selected_text == selected
            assert panel.render().plain == source
            with patch.object(app, "copy_to_clipboard") as clipboard:
                await pilot.press("ctrl+c")
                clipboard.assert_called_once_with(selected)
            app._table.focus(scroll_visible=False)
            if navigation == "key":
                await pilot.press("down" if rebuild == "reorder" else "up")
            else:
                row = app.row_keys.index("service:" + interaction_snapshot["services"][0]["id"])
                await pilot.click("#fleet-table", offset=(2, app._table.header_height + row))
            await ready(app, pilot)
            assert app.selected_service_id() != "second-service"
            assert not panel.has_selection
    asyncio.run(scenario())


@pytest.mark.parametrize("view", ["person", "modal"])
@pytest.mark.parametrize("failure", ["stale", "read"])
@pytest.mark.parametrize("selected", [False, True])
def test_api_freshness_refresh_hides_old_addresses_after_frozen_selection_clears(interaction_snapshot, view, failure, selected):
    async def scenario():
        snapshot = two_service_snapshot(interaction_snapshot)
        snapshot["services"][0].update(api_access="shared", api_address="http://192.0.2.1:8080")
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 35)) as pilot:
            await ready(app, pilot)
            await pilot.press("p" if view == "person" else "enter")
            await ready(app, pilot)
            panel_name = "fleet-detail-text" if view == "person" else "gpu-service-details"
            panel = app.screen.query_one("#" + panel_name, SelectableStatic)
            source = panel.render().plain
            assert "API: http://192.0.2.1:8080 · Shared" in source
            if selected:
                await drag(pilot, "#" + panel_name, (0, 0), (5, 0))
                copied = panel.selected_text
                assert copied
            if failure == "stale":
                client.snapshot["stale"] = True
            else:
                client.read_error = OSError("fixture read failed")
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert app.snapshot["services"][0]["api_address"] == "http://192.0.2.1:8080"
            if selected:
                assert panel.has_selection and panel.selected_text == copied
                assert panel.render().plain == source
                panel.clear_selection()
                await pilot.pause()
            assert "API: Unknown" in panel.render().plain
            assert "http://192.0.2.1:8080" not in panel.render().plain
            assert "Shared" not in panel.render().plain
    asyncio.run(scenario())


def test_fresh_api_remains_known_when_activity_is_unknown(interaction_snapshot):
    async def scenario():
        snapshot = two_service_snapshot(interaction_snapshot)
        snapshot["services"][0].update(status="unknown", engine="unsupported",
                                      api_access="shared", api_address="http://192.0.2.1:8080")
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 35)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            assert not app.read_error and app.service_status(app.selected_service()) == "unknown"
            assert "API: http://192.0.2.1:8080 · Shared" in app.query_one("#fleet-detail-text").render().plain
    asyncio.run(scenario())
