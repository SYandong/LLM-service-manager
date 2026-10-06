# Generated-By: Codex / gpt-6.1-sol
"""Anonymous presentation and independent six-GPU keyboard navigation."""

import asyncio
import copy
from unittest.mock import patch

import pytest

pytest.importorskip("textual")
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Input

from tui.fleet_app import GpuOverview
from tui.fleet_format import owner_info
from tui.fleet_gpu import drawing_segments
from test_tui_fleet import fleet_snapshot as snapshot_fixture, make_app, ready


@pytest.fixture
def fleet_snapshot():
    return snapshot_fixture.__wrapped__()


@pytest.mark.parametrize("name", ["private-owner", "User", "Work"])
def test_anonymous_gpu_people_details_history_and_copy_toggle(fleet_snapshot, name):
    async def scenario():
        service = fleet_snapshot["services"][0]
        old_id = service["id"]
        service.update(id=name + ":42:55", container=name,
                       model="/srv/%s/gemma-4-31b-it-qat-w4a16-ct" % name)
        for gpu in fleet_snapshot["gpus"]:
            for occupant in gpu["occupants"]:
                if occupant.get("service_id") == old_id:
                    occupant.update(service_id=service["id"], container=name)
        fleet_snapshot["errors"] = ["Reading failed for " + name]
        before = copy.deepcopy(fleet_snapshot)
        app, client = make_app(fleet_snapshot)
        original_request = client.request

        def request(method, path, payload=None):
            result = original_request(method, path, payload)
            if path.startswith("/v1/fleet/history"):
                result["service"].update(container=name, model=service["model"],
                    argv_redacted="vllm serve %s --owner %s" % (service["model"], name))
            return result

        client.request = request
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            assert not app.show_names
            app.select_gpu(2, ("container:" + name, "llm"), service["id"])
            await ready(app, pilot)
            label = owner_info(service)[1]
            panel = app.query_one("#fleet-gpus", GpuOverview)
            assert label in panel.render().plain
            assert label + ":42:55" in panel.render().plain
            assert "/srv/" not in panel.render().plain
            assert "gemma-4-31b-it-qat-w4a16-ct" in panel.render().plain
            assert "LLM · Work · Unattributed · Free" in app._rendered["fleet-controls"].plain
            assert app._rendered["fleet-banner"].endswith(label)
            history = app._rendered["fleet-history-text"]
            assert "--owner " + label in history and "/srv/" not in history
            assert any(service["id"].replace(":", "%3A") in path for _, path, _ in client.calls)
            identity = app.selected_gpu, app.selected_segment, app.selected_service_id()
            colors = [segment.color for segment in drawing_segments(app.selected_gpu_account())]
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.scroll_to(y=viewport.scroll_y + 3, animate=False)
            await pilot.pause()
            scroll = viewport.scroll_y
            await pilot.press("n")
            await ready(app, pilot)
            assert app.show_names and name + ":42:55" in panel.render().plain
            assert "/srv/" not in panel.render().plain
            assert viewport.scroll_y == scroll
            assert (app.selected_gpu, app.selected_segment, app.selected_service_id()) == identity
            assert [segment.color for segment in drawing_segments(app.selected_gpu_account())] == colors
            await pilot.press("n", "enter")
            await ready(app, pilot)
            assert label in app.screen.query_one("#gpu-allocation-text").render().plain
            assert label in str(app.screen.query_one("#gpu-detail-services", DataTable).get_row_at(0))
            assert "--owner " + label in app.screen.query_one("#gpu-service-history").render().plain
            await pilot.press("n")
            assert app.show_names and name in app.screen.query_one("#gpu-allocation-text").render().plain
            await pilot.press("n", "escape", "p")
            await ready(app, pilot)
            key = "person:container:" + name
            table = app.query_one("#fleet-table", DataTable)
            assert table.get_cell(key, "service").plain.startswith(label)
            table.move_cursor(row=app.row_keys.index(key), animate=False)
            table.focus(scroll_visible=False)
            await pilot.pause()
            with patch.object(app, "copy_to_clipboard") as clipboard:
                await pilot.press("ctrl+c")
                copied = clipboard.call_args[0][0]
            assert copied.startswith(label)
            assert client.snapshot == before
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
@pytest.mark.parametrize("compact", [False, True])
def test_all_six_gpus_are_selected_after_manual_scroll(fleet_snapshot, size, compact):
    async def scenario():
        for service in fleet_snapshot["services"]:
            service["model"] = " ".join("long-model-%s" % index for index in range(90))
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            if compact:
                await pilot.press("z")
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            assert overview.bar_rows == 3 and viewport.max_scroll_y > 0
            for expected in range(6):
                if expected:
                    await pilot.press("down")
                assert app.selected_gpu == expected
                assert viewport.scroll_y == min(app._gpu_anchors[expected], viewport.max_scroll_y)
                heading = overview.heading_rows[expected][0]
                assert viewport.scroll_y <= heading < viewport.scroll_y + viewport.size.height
                viewport.scroll_to(y=0 if expected else viewport.max_scroll_y, animate=False)
                await pilot.pause()
                assert app.selected_gpu == expected
                await app.on_event(events.Key("fleet_scroll_down", None))
                await app.on_event(events.Key("fleet_scroll_up", None))
                await pilot.pause()
                assert app.selected_gpu == expected
                await pilot.press("right")
                assert app.selected_gpu == expected
                assert viewport.scroll_y == min(app._gpu_anchors[expected], viewport.max_scroll_y)
            for expected in reversed(range(5)):
                await pilot.press("up")
                assert app.selected_gpu == expected
                assert viewport.scroll_y == min(app._gpu_anchors[expected], viewport.max_scroll_y)
            await pilot.press("up")
            assert app.selected_gpu == 0 and viewport.scroll_y == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("show_names", [False, True])
def test_cached_read_history_and_notice_text_follows_names_toggle(fleet_snapshot, show_names):
    async def scenario():
        service = fleet_snapshot["services"][0]
        service["container"] = "private-owner"
        app, client = make_app(fleet_snapshot, show_names=show_names)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            app.select_gpu(2, service_id=service["id"])
            await ready(app, pilot)
            client.read_error = OSError("private-owner read failed")
            client.history_error = OSError("private-owner history failed")
            await app.refresh_fleet().wait()
            await app.fetch_history(service["id"], app._history_generation).wait()
            app.notice("private-owner notice")
            label = owner_info(service)[1]
            # Later observations can lose the original owner metadata while
            # the service and its cached diagnostic evidence remain selected.
            for item in app.display_items():
                if item.get("container") == "private-owner":
                    item["container"] = None
            app.history = None
            app.render_snapshot()
            assert all(item.get("container") != "private-owner" for item in app.display_items())
            for expected in (show_names, not show_names, show_names):
                if app.show_names != expected:
                    await pilot.press("n")
                await pilot.pause()
                for field in ("fleet-banner", "fleet-history-text", "fleet-notice"):
                    value = app._rendered[field]
                    assert ("private-owner" in value) is expected
                    assert (label in value) is not expected
            assert "private-owner" in app.read_error
            assert "private-owner" in app.history_error
            assert app._notice_raw == "private-owner notice"
    asyncio.run(scenario())


@pytest.mark.parametrize("show_names", [False, True])
def test_cached_notice_keeps_owner_context_after_owner_leaves(fleet_snapshot, show_names):
    async def scenario():
        service = fleet_snapshot["services"][0]
        service["container"] = "private-owner"
        app, client = make_app(fleet_snapshot, show_names=show_names)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            app.notice("private-owner notice")
            label = owner_info(service)[1]
            client.snapshot["services"].remove(service)
            for gpu in client.snapshot["gpus"]:
                gpu["occupants"] = [item for item in gpu["occupants"]
                                    if item.get("service_id") != service["id"]]
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert all(item.get("container") != "private-owner" for item in app.display_items())
            for expected in (show_names, not show_names, show_names):
                if app.show_names != expected:
                    await pilot.press("n")
                await pilot.pause()
                notice = app._rendered["fleet-notice"]
                assert ("private-owner" in notice) is expected
                assert (label in notice) is not expected
            assert app._notice_raw == "private-owner notice"
    asyncio.run(scenario())


def test_safe_structured_http_codes_remain_visible_when_anonymous(fleet_snapshot):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            app.select_gpu(2)
            await ready(app, pilot)
            client.read_error = app.api.ClientError("HTTP 503: fleet_store_unavailable", status=503,
                                                   payload={"error": "fleet_store_unavailable"})
            client.history_error = app.api.ClientError("HTTP 403: unmapped_container", status=403,
                                                      payload={"error": "unmapped_container"})
            await app.refresh_fleet().wait()
            await app.fetch_history(app.selected_service_id(), app._history_generation).wait()
            assert "fleet_store_unavailable" in app._rendered["fleet-banner"]
            assert "unmapped_container" in app._rendered["fleet-history-text"]
    asyncio.run(scenario())


def test_arrow_then_wheel_in_one_input_batch_keeps_the_last_scroll(fleet_snapshot):
    async def scenario():
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            app.on_key(events.Key("down", None))
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            anchor = app._gpu_anchors[1]
            assert viewport.scroll_y == anchor
            app.on_key(events.Key("fleet_scroll_down", None))
            await pilot.pause()
            assert app.selected_gpu == 1 and viewport.scroll_y == anchor + 1
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_expand_then_arrow_in_one_batch_finishes_alignment_after_layout(fleet_snapshot, size):
    async def scenario():
        for service in fleet_snapshot["services"]:
            service["model"] = " ".join("long-model-%s" % index for index in range(90))
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            await pilot.press("z")
            app.select_gpu(2)
            await pilot.pause()
            app.on_key(events.Key("z", None))
            app.on_key(events.Key("down", None))
            await pilot.pause()
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            anchor = app._gpu_anchors[3]
            assert app.selected_gpu == 3 and not app.compact_gpus
            assert viewport.scroll_y == min(anchor, viewport.max_scroll_y)
            assert viewport.scroll_y <= anchor < viewport.scroll_y + viewport.size.height
    asyncio.run(scenario())


def test_claim_failure_notice_retains_original_owner_for_names_toggle(fleet_snapshot):
    async def scenario():
        service = fleet_snapshot["services"][2]
        service["container"] = "private-owner"
        app, client = make_app(fleet_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            app.select_gpu(3, service_id=service["id"])
            app.action_claim()
            await pilot.pause()
            app.screen.query_one("#claim-reason", Input).value = "Working session"
            await pilot.click("#claim-preview")
            await ready(app, pilot)
            client.write_error = OSError("private-owner write failed")
            await pilot.click("#claim-submit")
            await ready(app, pilot)
            await pilot.click("#claim-close")
            label = owner_info(service)[1]
            assert label in app._rendered["fleet-notice"]
            assert "private-owner" in app._notice_raw
            await pilot.press("n")
            assert "private-owner" in app._rendered["fleet-notice"]
            await pilot.press("n")
            assert label in app._rendered["fleet-notice"] and "private-owner" not in app._rendered["fleet-notice"]
            assert len([call for call in client.calls if call[0] == "POST" and "dry_run" not in call[1]]) == 1
    asyncio.run(scenario())
