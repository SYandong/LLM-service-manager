# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Synthetic accounting, visible GPU selection, detail, and claim contracts."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from rich.console import Console

pytest.importorskip("textual")
from textual.widgets import Button, DataTable, Input
from textual.containers import VerticalScroll

from tui.fleet_app import ClaimDialog, FleetHelpDialog, GpuDetailDialog, GpuOverview
from tui.fleet_gpu import (FREE_KEY, MEASURED_KEY, RESIDUAL_KEY, account_gpu,
                           allocation_legend, card_header, compact_gib, detail_lines,
                           drawing_segments, expanded_header, gib, owner_color,
                           proportional_cells, render_bar, render_expanded, render_overview, service_lines)
from test_tui_fleet import fleet_snapshot, make_app, ready


@pytest.fixture
def gpu_snapshot():
    return json.loads((Path(__file__).parent / "fixtures/fleet_gpu_overview.json").read_text())


def accounts(snapshot):
    return [account_gpu(gpu, snapshot["services"]) for gpu in snapshot["gpus"]]


def test_synthetic_totals_are_counted_once(gpu_snapshot):
    cards = accounts(gpu_snapshot)
    assert sum(card.total_gb for card in cards) == 840
    assert sum(card.used_gb for card in cards) == 618
    assert sum(card.free_gb for card in cards) == 222
    assert sum(card.residual_gb for card in cards) == 28
    allocations = [allocation for card in cards for allocation in card.allocations]
    assert sum(item.used_gb for item in allocations if item.kind == "llm") == 456
    assert sum(item.used_gb for item in allocations if item.kind == "other") == 134
    totals = {owner: sum(item.used_gb for item in allocations if item.owner == owner)
              for owner in ("sample-a", "sample-b", "sample-c", "sample-d", "unknown")}
    assert totals == {"sample-a": 220, "sample-b": 168, "sample-c": 120, "sample-d": 76, "unknown": 6}
    assert all(card.reconciled for card in cards)
    first = next(item for item in cards[0].allocations if item.owner == "sample-a")
    assert first.used_gb == 74
    assert first.members == (("demo-0-0", 52), ("demo-0-1", 22))
    assert next(item for item in cards[0].allocations if item.owner == "sample-b").kind == "llm"


def test_owner_color_is_stable_across_cards_kinds_and_order(gpu_snapshot):
    original = accounts(gpu_snapshot)
    gpu_snapshot["gpus"].reverse()
    for gpu in gpu_snapshot["gpus"]:
        gpu["occupants"].reverse()
    refreshed = accounts(gpu_snapshot)
    for cards in (original, refreshed):
        matches = [segment for card in cards for segment in drawing_segments(card)
                   if segment.key[0] == "container:sample-a"]
        assert {segment.color for segment in matches} == {owner_color("container:sample-a")}
        assert {segment.pattern for segment in matches} == {" ", "·"}
    assert owner_color("container:sample-a") != owner_color("container:sample-b")
    assert len({owner_color("container:sample-" + suffix) for suffix in "abcd"}) == 4
    assert owner_color("unknown") != owner_color("container:unknown")


@pytest.mark.parametrize("rows", [1, 3, 5])
@pytest.mark.parametrize("expanded", [False, True], ids=["compact", "expanded"])
def test_bar_labels_have_equal_padding_on_all_sides(rows, expanded):
    card = account_gpu({"index": 0, "total_gb": 100, "used_gb": 52,
                        "occupants": [{"container": "sample-a", "kind": "llm", "used_gb": 52}]})
    if expanded:
        view, hits, _, _ = render_expanded([card], [], 100, bar_rows=rows)
    else:
        view, hits = render_overview([card], 100, rows)
    bars = view.plain.splitlines()[1:rows + 1]
    for label, key in (("sample-a LLM 52", ("container:sample-a", "llm")), ("Free 48", FREE_KEY)):
        labeled_rows = [index for index, line in enumerate(bars) if label in line]
        assert len(labeled_rows) == 1
        row = labeled_rows[0]
        assert row == len(bars) - row - 1
        _, left, right, _, _ = next(hit for hit in hits if hit[0] == row + 1 and hit[4] == key)
        start = bars[row].index(label)
        assert abs((start - left) - (right - start - len(label))) <= 1


def test_proportional_rounding_keeps_tiny_amounts_without_forcing_a_cell():
    assert proportional_cells([50, .0001, 49.9999], 100, 80) == [40, 0, 40]
    assert proportional_cells([1, 1, 1], 3, 80) == [27, 27, 26]
    assert proportional_cells([0, 100], 100, 80) == [0, 80]
    with pytest.raises(ValueError):
        proportional_cells([90, 30], 100, 80)
    card = account_gpu({"index": 0, "total_gb": 100, "used_gb": 50.0001,
                        "occupants": [{"container": "tiny", "kind": "other", "used_gb": .0001}]})
    assert gib(.0001) == "0.0001"
    assert "tiny Other 0.0001" in allocation_legend(card, 100)[0].plain
    assert "tiny · Other · 0.0001 GiB" in detail_lines(card)


@pytest.mark.parametrize("field", ["total_gb", "used_gb"])
def test_missing_measurement_withholds_a_percentage_bar(gpu_snapshot, field):
    gpu_snapshot["gpus"][0][field] = None
    card = accounts(gpu_snapshot)[0]
    assert not drawing_segments(card)
    assert not render_bar(card, 80)[1]
    assert "VRAM" not in card_header(card, 80).plain
    assert "?" in detail_lines(card)


def test_unknown_allocation_retains_measured_usage(gpu_snapshot):
    gpu_snapshot["gpus"][0]["occupants"][0]["used_gb"] = None
    card = accounts(gpu_snapshot)[0]
    assert card.attributed_gb is card.residual_gb is None
    assert card.allocations[0].members == (("demo-0-0", None), ("demo-0-1", 22))
    assert "allocation amount unknown" in card.issues
    assert [(item.key, item.used_gb) for item in drawing_segments(card)] == [
        (MEASURED_KEY, 123), (FREE_KEY, 17)]


def test_conflict_draws_measurement_and_preserves_supplied_allocations(gpu_snapshot):
    gpu_snapshot["gpus"][0]["used_gb"] = 100
    card = accounts(gpu_snapshot)[0]
    assert card.attributed_gb == 118
    assert card.residual_gb is None
    assert card.free_gb == 40
    assert sum(item.used_gb for item in card.allocations) == 118
    segments = drawing_segments(card)
    assert [(item.key, item.used_gb) for item in segments] == [(MEASURED_KEY, 100), (FREE_KEY, 40)]
    assert proportional_cells([item.used_gb for item in segments], 140, 80) == [57, 23]
    assert "conflict +18 GiB" in card_header(card, 80).plain
    assert "attribution conflict +18 GiB" in detail_lines(card)
    assert "sample-a · LLM · 74 GiB" in detail_lines(card)
    assert "Unattributed: ?" in detail_lines(card)


def test_over_capacity_keeps_actual_value_and_caps_only_the_drawing(gpu_snapshot):
    gpu_snapshot["gpus"][0]["used_gb"] = 145
    card = accounts(gpu_snapshot)[0]
    assert card.used_gb == 145 and card.free_gb is None
    assert "over capacity +5 GiB" in card_header(card, 80).plain
    assert [(item.key, item.used_gb) for item in drawing_segments(card)] == [(MEASURED_KEY, 140)]
    assert render_bar(card, 80)[0].cell_len == 80
    assert "145/140 GiB" in detail_lines(card)


def test_legend_continuation_and_selection_retain_exact_details(gpu_snapshot):
    card = accounts(gpu_snapshot)[0]
    legend, _ = allocation_legend(card, 48)
    assert "+" in legend.plain and "Enter" in legend.plain
    selected = next(item.key for item in card.allocations if item.owner == "sample-c")
    legend, _ = allocation_legend(card, 48, selected)
    assert "sample-c Other 10" in legend.plain
    assert "sample-b · LLM · 34 GiB" in detail_lines(card)


def test_fractional_overview_is_compact_and_details_retain_source_precision():
    card = account_gpu({"index": 5, "total_gb": 140.1201171875, "used_gb": 123.03125,
                        "util_percent": 61.125, "occupants": [
                            {"container": "sample-a", "kind": "llm", "used_gb": 100.0009765625}]})
    header = card_header(card, 80, selected=True).plain
    assert "123.03/140.12 GiB" in header
    assert "VRAM 88%" in header and "compute 61.12%" in header
    assert "123.03125/140.1201171875 GiB" in detail_lines(card)
    assert "100.0009765625 GiB" in detail_lines(card)
    assert compact_gib(0) == "0"
    assert compact_gib(.0009765625) != "0"
    assert compact_gib(20.0) == "20"


def test_fractional_conflict_keeps_compute_visible_with_a_stale_label():
    card = account_gpu({"index": 0, "total_gb": 140.1201171875, "used_gb": 100.03125,
                        "util_percent": 61.125, "occupants": [
                            {"container": "sample-a", "kind": "llm", "used_gb": 118}]})
    header = card_header(card, 80, selected=True, stale=True).plain
    assert "STALE" in header
    assert "compute 61.12%" in header
    assert "conflict +17.96875 GiB" in header
    assert "attribution conflict +17.96875 GiB" in detail_lines(card)


def test_expanded_cards_wrap_full_labels_and_source_values(gpu_snapshot):
    service = gpu_snapshot["services"][0]
    old_id = service["id"]
    service["id"] = "synthetic-service-" + "identifier-" * 16
    service["model"] = "synthetic-model-" + "long-label-" * 12
    service["gpu_gb"] = 999
    owner = "synthetic-owner-" + "long-name-" * 12
    occupant = gpu_snapshot["gpus"][0]["occupants"][0]
    occupant.update(container=owner, service_id=service["id"], used_gb=52.0009765625)
    assert occupant["service_id"] != old_id
    card = accounts(gpu_snapshot)[0]
    view, hits, anchors, service_hits = render_expanded([card], gpu_snapshot["services"], 79)
    joined = "".join(view.plain.split())
    assert owner in joined and service["id"] in joined and service["model"] in joined
    assert "GPU0VRAM:52.0009765625GiB" in joined
    assert "GPU0VRAM:999" not in joined
    assert "+1 · Enter" not in view.plain and "…" not in view.plain
    assert all(line.cell_len <= 79 for line in view.split("\n"))
    owner_key = ("container:" + owner, "llm")
    owner_row = anchors[(0, owner_key)]
    assert (owner_row, 0, 79, 0, owner_key) in hits
    id_rows = [row for row, ident in service_hits.items() if ident == service["id"]]
    assert len(id_rows) > 4
    assert all((row, 0, 79, 0, owner_key) in hits for row in id_rows)
    assert all(service["id"] not in service_hits.get(row, "") for row in range(owner_row))


def test_expanded_typography_retains_owner_and_state_semantics(gpu_snapshot):
    card = accounts(gpu_snapshot)[0]
    header = expanded_header(card, selected=True)
    assert "123 used of 140 GiB" in header.plain and "Compute 61%" in header.plain
    console = Console()
    assert header.get_style_at_offset(console, header.plain.index("GPU")).bold
    assert not header.get_style_at_offset(console, header.plain.index("VRAM")).bold
    first, ident, activity, counters = service_lines(gpu_snapshot["services"][0], 52, 0, status="active")
    assert first.get_style_at_offset(console, first.plain.index("demo-model")).bold
    assert not first.get_style_at_offset(console, first.plain.index("Model")).bold
    assert not first.get_style_at_offset(console, first.plain.index("Engine")).bold
    assert first.get_style_at_offset(console, first.plain.index("active")).color.get_truecolor().hex == "#71c695"
    assert "Service ID:" in ident.plain
    assert "24h service activity" in activity.plain and "coverage 50%" in activity.plain
    assert activity.cell_len <= 79
    assert "24h service requests 120 · input 24000 · output 12000" in counters.plain
    view, _, anchors, _ = render_expanded([card], [], 100)
    row = view.split("\n")[anchors[(0, card.allocations[0].key)]]
    assert row.get_style_at_offset(console, 2).color.get_truecolor().hex == owner_color(card.allocations[0].identity)


def test_expanded_conflicts_and_unknowns_keep_allocations_readable(gpu_snapshot):
    gpu_snapshot["gpus"][0]["used_gb"] = 100
    gpu_snapshot["gpus"][1]["total_gb"] = None
    cards = accounts(gpu_snapshot)[:2]
    view, _, _, _ = render_expanded(cards, gpu_snapshot["services"], 79, stale=True)
    assert "attribution conflict +18 GiB" in view.plain
    assert "sample-a · LLM · 74 GiB" in view.plain
    assert "Unattributed used · ? GiB" in view.plain
    assert "Free VRAM · 40 GiB" in view.plain
    assert "capacity unknown" in view.plain
    assert "Measured VRAM · Used 136 GiB · Total ? GiB" in view.plain
    assert view.plain.count("STALE") == 2


@pytest.mark.parametrize("service_id", [[], {}, 0, "", False])
def test_malformed_gpu_service_id_preserves_the_last_good_snapshot(gpu_snapshot, service_id):
    async def scenario():
        app, client = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            original = app.snapshot
            original_accounts = app.gpu_accounts.copy()
            client.snapshot["gpus"][0]["occupants"][0]["service_id"] = service_id
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert app.snapshot is original
            assert app.gpu_accounts == original_accounts
            assert "Invalid GPU service ID" in app.read_error
            assert "STALE" in app.query_one("#fleet-gpus").render().plain
            assert app._exception is None
    asyncio.run(scenario())


@pytest.mark.parametrize("mixed_unknown", [False, True], ids=["owner-total", "service-subtotal"])
def test_accounting_overflow_preserves_the_last_good_snapshot(gpu_snapshot, mixed_unknown):
    async def scenario():
        app, client = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            original = app.snapshot
            original_accounts = app.gpu_accounts.copy()
            occupants = client.snapshot["gpus"][0]["occupants"]
            occupants[0]["used_gb"] = 1e308
            if mixed_unknown:
                occupants[1]["used_gb"] = None
                occupants.append(copy.deepcopy(occupants[0]))
            else:
                occupants[1]["used_gb"] = 1e308
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert app.snapshot is original
            assert app.gpu_accounts == original_accounts
            assert app.read_error
            assert app._exception is None
    asyncio.run(scenario())


def test_fractional_gpu_measurements_keep_compute_visible_at_80_columns(gpu_snapshot):
    async def scenario():
        for gpu in gpu_snapshot["gpus"]:
            gpu["total_gb"] = 140.1201171875
            gpu["used_gb"] += .03125
            gpu["util_percent"] += .125
        app, _ = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("z")
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            headers = [line for line in overview.render().plain.splitlines() if "GPU " in line]
            assert len(headers) == 6
            assert all("VRAM" in line and "compute" in line and "140.12" in line for line in headers)
            await pilot.press("enter")
            await ready(app, pilot)
            assert "140.1201171875" in str(app.screen.query_one("#gpu-allocation-text").render())
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_gpu_keyboard_segment_details_and_individual_services(gpu_snapshot, size):
    async def scenario():
        app, _ = make_app(gpu_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            assert app.selected_gpu == 0
            await pilot.press("right", "enter")
            await ready(app, pilot)
            assert isinstance(app.screen, GpuDetailDialog)
            assert app.selected_segment == ("container:sample-a", "llm")
            assert "sample-a · LLM · 74 GiB" in str(app.screen.query_one("#gpu-allocation-text").render())
            table = app.screen.query_one("#gpu-detail-services", DataTable)
            assert table.row_count == 2
            assert table.get_cell_at((0, 1)) == "52"
            assert table.get_cell_at((1, 1)) == "22"
            assert app.history["service_id"] == "demo-0-0"
            await pilot.press("down")
            await ready(app, pilot)
            assert app.selected_service_id() == app.history["service_id"] == "demo-0-1"
            await pilot.press("escape", "down", "down", "right")
            assert app.selected_gpu == 2
            assert app.selected_segment == ("container:sample-a", "llm")
            await pilot.press("p", "g")
            assert app.selected_gpu == 2
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            assert viewport.scroll_y > 0
            anchor = app._gpu_anchors[(app.selected_gpu, app.selected_segment)]
            assert viewport.scroll_y <= anchor < viewport.scroll_y + viewport.size.height
            assert app._exception is None
    asyncio.run(scenario())


def test_mouse_targets_gpu_headers_and_owner_segments(gpu_snapshot):
    async def scenario():
        app, _ = make_app(gpu_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("z")
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            header = next(hit for hit in overview.hits if hit[3:] == (5, None))
            await pilot.click("#fleet-gpus", offset=(1, header[0]))
            assert app.selected_gpu == 5 and app.selected_segment is None
            segment = next(hit for hit in overview.hits if hit[3:] == (5, ("container:sample-a", "other")))
            await pilot.click("#fleet-gpus", offset=(segment[1] + 1, segment[0]))
            await ready(app, pilot)
            assert isinstance(app.screen, GpuDetailDialog)
            assert "sample-a · Other · 12 GiB" in str(app.screen.query_one("#gpu-allocation-text").render())
            assert app.screen.query_one("#gpu-detail-services", DataTable).row_count == 0
            assert app.screen.query_one("#gpu-detail-claim", Button).disabled
            assert app.screen.query_one("#gpu-detail-revoke", Button).disabled
    asyncio.run(scenario())


def test_refresh_keeps_owner_selection_and_colors(gpu_snapshot):
    async def scenario():
        app, client = make_app(gpu_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("down", "down", "right")
            selected = app.selected_gpu, app.selected_segment
            colors = [segment.color for segment in drawing_segments(app.selected_gpu_account())]
            client.snapshot["gpus"].reverse()
            for gpu in client.snapshot["gpus"]:
                gpu["occupants"].reverse()
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert (app.selected_gpu, app.selected_segment) == selected
            assert [segment.color for segment in drawing_segments(app.selected_gpu_account())] == colors
            client.snapshot["gpus"] = [gpu for gpu in client.snapshot["gpus"] if gpu["index"] != 2]
            await app.refresh_fleet().wait()
            assert app.selected_gpu == 0 and app.selected_segment is None
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_expanded_selection_scroll_refresh_and_compact_roundtrip(gpu_snapshot, size):
    async def scenario():
        app, client = make_app(gpu_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            assert not app.compact_gpus
            assert viewport.virtual_size.height > viewport.size.height
            for service in gpu_snapshot["services"]:
                assert service["id"] in overview.render().plain
                assert service["model"] in overview.render().plain
            await pilot.press("down", "down", "right")
            await ready(app, pilot)
            selected = app.selected_gpu, app.selected_segment
            anchor = app._gpu_anchors[selected]
            assert viewport.scroll_y <= anchor < viewport.scroll_y + viewport.size.height
            viewport.scroll_to(y=viewport.scroll_y + 3, animate=False)
            await pilot.pause()
            manual_scroll = viewport.scroll_y
            client.snapshot["services"].reverse()
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            assert (app.selected_gpu, app.selected_segment) == selected
            assert viewport.scroll_y == manual_scroll
            await pilot.press("z")
            await ready(app, pilot)
            assert app.compact_gpus and viewport.scroll_y == 0
            assert (app.selected_gpu, app.selected_segment) == selected
            await pilot.press("z")
            await ready(app, pilot)
            anchor = app._gpu_anchors[selected]
            assert not app.compact_gpus
            assert viewport.scroll_y <= anchor < viewport.scroll_y + viewport.size.height
            assert "Q quit" in str(app.query_one("#fleet-footer").render())
            assert "/ filter" not in str(app.query_one("#fleet-footer").render())
    asyncio.run(scenario())


def test_mouse_on_wrapped_inline_service_selects_that_service(gpu_snapshot):
    async def scenario():
        service = gpu_snapshot["services"][1]
        ident = "synthetic-service-" + "long-identifier-" * 12
        service["id"] = ident
        gpu_snapshot["gpus"][0]["occupants"][1]["service_id"] = ident
        app, _ = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            rows = overview.render().plain.splitlines()
            id_start = next(row for row in overview.service_hits if overview.service_hits[row] == ident
                            and rows[row].strip().startswith("Service ID:"))
            continuation = id_start + 1
            assert overview.service_hits[continuation] == ident
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            viewport.scroll_to(y=continuation - 2, animate=False)
            await pilot.pause()
            await pilot.click("#fleet-gpus", offset=(2, continuation))
            await ready(app, pilot)
            assert isinstance(app.screen, GpuDetailDialog)
            assert app.selected_service_id() == ident
            assert app.history["service_id"] == ident
            assert app.selected_segment == ("container:sample-a", "llm")
            await pilot.press("escape")
            assert app.focused is overview
    asyncio.run(scenario())


def test_help_scrolls_at_80_columns_and_restores_gpu_focus(gpu_snapshot):
    async def scenario():
        app, _ = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("down", "right", "question_mark")
            await pilot.pause()
            assert isinstance(app.screen, FleetHelpDialog)
            content = app.screen.query_one("#fleet-help-text")
            text = str(content.render())
            assert "Press / to search" in text
            assert "LLM: inference models, solid fill" in text
            assert "Other: other GPU jobs, dotted fill" in text
            assert "Unattributed: used VRAM with no matched workload" in text
            assert "24h activity describes the whole service" in text
            viewport = app.screen.query_one("#fleet-help-scroll", VerticalScroll)
            assert viewport.virtual_size.height > viewport.size.height
            assert content.region.width <= 80
            viewport.scroll_end(animate=False)
            await pilot.pause()
            assert viewport.scroll_y > 0
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app.dashboard
            assert app.focused is app.query_one("#fleet-gpus")
            assert app.selected_gpu == 1 and app.selected_segment == ("container:sample-b", "llm")
            await pilot.press("p")
            assert "sort State" in str(app.query_one("#fleet-controls").render())
            await pilot.press("s")
            assert "sort Idle time" in str(app.query_one("#fleet-controls").render())
            await pilot.press("z")
            assert app.screen is app.dashboard and not app.compact_gpus
    asyncio.run(scenario())


def test_filter_and_mine_never_turn_hidden_allocations_into_free_memory(gpu_snapshot):
    async def scenario():
        app, _ = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            original = copy.deepcopy(app.gpu_accounts)
            original_bar = render_bar(app.gpu_accounts[0], 80)[0]
            await pilot.press("slash")
            app.query_one("#fleet-filter", Input).value = "sample-a"
            await pilot.press("enter", "m")
            assert app.gpu_accounts == original
            assert render_bar(app.gpu_accounts[0], 80)[0] == original_bar
            assert "sample-b · LLM · 34 GiB" in str(app.query_one("#fleet-gpus").render())
            assert "demo-model-34" not in str(app.query_one("#fleet-gpus").render())
            assert "demo-model-52" in str(app.query_one("#fleet-gpus").render())
            assert app.selected_gpu_account().free_gb == 17
            await pilot.press("right", "right", "enter")
            await ready(app, pilot)
            assert "sample-b · LLM · 34 GiB" in str(app.screen.query_one("#gpu-allocation-text").render())
            assert app.screen.query_one("#gpu-detail-services", DataTable).row_count == 0
    asyncio.run(scenario())


def test_stale_and_conflict_are_visible_in_the_compact_overview(gpu_snapshot):
    async def scenario():
        app, client = make_app(gpu_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("z")
            client.snapshot["stale"] = True
            client.snapshot["gpus"][0]["used_gb"] = 100
            await app.refresh_fleet().wait()
            await ready(app, pilot)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            assert "STALE" in str(overview.render())
            assert "conflict +18 GiB" in str(overview.render())
            header = overview.render().plain.splitlines()[0]
            assert "compute 61%" in header
            assert str(overview.render()).count("GPU ") == 6
            assert overview.region.y + len(overview.render().plain.splitlines()) <= app.query_one("#fleet-notice").region.y
            await pilot.press("right", "enter")
            await ready(app, pilot)
            assert "sample-a · LLM · 74 GiB" in str(app.screen.query_one("#gpu-allocation-text").render())
            assert app.screen.query_one("#gpu-detail-claim", Button).disabled
            assert app.selected_service()["status"] == "idle"
            assert "unknown" in str(app.screen.query_one("#gpu-service-details").render())
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
@pytest.mark.parametrize("failure", ["stale", "read"])
def test_six_gpu_headers_fit_with_filter_and_failure_banner(gpu_snapshot, size, failure):
    async def scenario():
        app, client = make_app(gpu_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            await pilot.press("z")
            if failure == "stale":
                client.snapshot["stale"] = True
            else:
                client.read_error = OSError("synthetic read failure")
            await app.refresh_fleet().wait()
            await pilot.press("slash")
            await pilot.pause()
            overview = app.query_one("#fleet-gpus", GpuOverview)
            assert overview.bar_rows == 1
            assert app.query_one("#fleet-filter").display
            assert app.query_one("#fleet-banner").display
            headers = [hit for hit in overview.hits if hit[4] is None]
            assert [hit[3] for hit in headers] == list(range(6))
            assert all(overview.region.y + hit[0] < overview.region.bottom for hit in headers)
            assert overview.region.y + len(overview.render().plain.splitlines()) <= overview.region.bottom
            assert app.query_one("#fleet-footer").region.bottom <= size[1]
    asyncio.run(scenario())


def test_claim_preview_submit_and_revoke_from_gpu_details(fleet_snapshot):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("down", "down", "down", "right", "enter")
            await ready(app, pilot)
            assert app.view == "gpu" and app.selected_service_id() == "own"
            await pilot.click("#gpu-detail-claim")
            assert isinstance(app.screen, ClaimDialog)
            app.screen.query_one("#claim-reason", Input).value = "Working session"
            await pilot.click("#claim-preview")
            await ready(app, pilot)
            assert not app.screen.query_one("#claim-submit", Button).disabled
            assert not next(service for service in client.snapshot["services"] if service["id"] == "own")["claim"]
            await pilot.click("#claim-submit")
            await ready(app, pilot)
            assert next(service for service in client.snapshot["services"] if service["id"] == "own")["claim"]
            await pilot.click("#claim-close")
            assert isinstance(app.screen, GpuDetailDialog)
            assert not app.screen.query_one("#gpu-detail-revoke", Button).disabled
            await pilot.click("#gpu-detail-revoke")
            await pilot.click("#claim-preview")
            await ready(app, pilot)
            await pilot.click("#claim-submit")
            await ready(app, pilot)
            assert len([call for call in client.calls if call[0] == "POST" and "dry_run" not in call[1]]) == 1
            assert len([call for call in client.calls if call[0] == "DELETE" and "dry_run" not in call[1]]) == 1
    asyncio.run(scenario())
