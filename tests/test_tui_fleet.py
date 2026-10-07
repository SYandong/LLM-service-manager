# Generated-By: Codex / gpt-6.1-sol
"""Actual Pilot coverage of read-only fleet layout and incremental views."""

import asyncio
import copy
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.cells import cell_len

pytest.importorskip("textual")
from textual.widgets import DataTable, Input, Sparkline
from textual.containers import VerticalScroll
from tui.fleet_app import FleetApp, GpuDetailDialog, GpuOverview


@pytest.fixture
def fleet_snapshot():
    window = {"active_minutes": 90, "requests": 120, "gen_tokens": 12000,
              "prompt_tokens": 24000, "active_ratio": .125, "observed_seconds": 43200,
              "coverage_ratio": .5}
    services = []
    for ident, container, memory, gpu, status, mine in (
        ("busy", "group-a", 96, 2, "active", False),
        ("quiet", "group-b", 82, 1, "over_limit", False),
        ("own", "group-c", 40, 3, "idle", True),
    ):
        services.append({"id": ident, "container": container, "model": ident + "-model",
                         "engine": "vllm", "gpus": [gpu], "gpu_gb": memory,
                         "started_at": 1899900000, "uptime_seconds": 100000,
                         "status": status, "idle_seconds": 30000 if status == "over_limit" else 120,
                         "last_active_at": 1899999880, "never_active": False,
                         "window_24h": copy.deepcopy(window), "window_7d": copy.deepcopy(window),
                         "hourly_active_24h": [0, 10, 60, None] + [0] * 20,
                         "mine": mine, "claim": None, "scrape": {"ok": True, "error": None}})
    gpus = [{"index": index, "total_gb": 140, "used_gb": 60, "util_percent": 20,
             "occupants": [{"container": "training-group", "kind": "other", "used_gb": 20,
                            "service_id": None}]} for index in range(6)]
    for service in services:
        gpus[service["gpus"][0]]["occupants"].append({"container": service["container"],
            "kind": "inference", "used_gb": service["gpu_gb"], "service_id": service["id"]})
        gpus[service["gpus"][0]]["used_gb"] = service["gpu_gb"] + 30
    return {"schema_version": 1, "generated_at": 1900000000, "snapshot_age_seconds": 12,
            "stale": False, "config": {"idle_limit_hours": 6, "active_window_seconds": 900},
            "gpus": gpus, "containers": [], "services": services, "errors": []}


class FixtureEvents:
    def __init__(self):
        self.events = []
        self.status = "SSE connected"
        self.generation = 0
        self.notify = None
        self.closes = 0

    def set_notify(self, callback):
        self.notify = callback

    def start(self):
        pass

    def drain(self):
        events, self.events = self.events, []
        return {"events": events, "status": self.status, "generation": self.generation,
                "dropped": 0, "missed": 0}

    def close(self):
        self.closes += 1
        return True


class FleetClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.read_error = None
        self.history_error = None

    def request(self, method, path, payload=None):
        assert method == "GET" and payload is None, "Fleet UI must perform only reads"
        self.calls.append((method, path, payload))
        if path == "/v1/fleet":
            if self.read_error:
                raise self.read_error
            return copy.deepcopy(self.snapshot)
        if path.startswith("/v1/fleet/history"):
            from urllib.parse import parse_qs, urlsplit
            if self.history_error:
                raise self.history_error
            ident = parse_qs(urlsplit(path).query)["service"][0]
            return {"schema_version": 1, "service_id": ident, "hours": 168, "resolution": "hourly",
                    "samples": [{"ts": 1899900000 + hour * 3600, "active_minutes": hour % 60,
                                 "gen_tokens": hour * 10} for hour in range(168)],
                    "service": {"id": ident, "argv_redacted": "vllm serve … --max-model-len 8192"}}
        pytest.fail("Fleet UI attempted an unsupported read: " + path)


def make_app(snapshot, reader=None, *, show_names=False):
    api = SimpleNamespace(**runpy.run_path(str(Path(__file__).parents[1] / "cli/fleet-llm")))
    client = FleetClient(snapshot)
    return FleetApp(client, api, event_reader=reader or FixtureEvents(), show_names=show_names), client


async def ready(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause(.15)
    await app.workers.wait_for_complete()
    await pilot.pause()


async def select(app, pilot, ident):
    if app.view != "person":
        await pilot.press("p")
    key = next(key for key in app.row_keys if app.row_services.get(key) == ident)
    app._table.move_cursor(row=app.row_keys.index(key), animate=False)
    await ready(app, pilot)


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_layout_views_and_seven_day_details(fleet_snapshot, size, tmp_path):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            table = app.query_one("#fleet-table", DataTable)
            assert app.refresh_seconds == 15
            overview = app.query_one("#fleet-gpus", GpuOverview)
            assert app.view == "gpu"
            assert not table.display and not app.query_one("#fleet-details").display
            assert not app.compact_gpus
            assert overview.bar_rows == 3
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            assert viewport.virtual_size.height > viewport.size.height
            assert " · Work · 20 GiB" in str(overview.render())
            assert "quiet-model" in str(overview.render())
            assert "sort priority" not in str(app.query_one("#fleet-controls").render())
            assert "Live changes" not in str(app.query_one("#fleet-controls").render())
            app.save_screenshot(filename="fleet-expanded-%sx%s.svg" % size, path=str(tmp_path))
            await pilot.press("z")
            await ready(app, pilot)
            assert app.compact_gpus
            assert overview.bar_rows == 3
            lines = overview.render().plain.splitlines()
            assert len(lines) == 6 * (overview.bar_rows + 2)
            assert all(len(line) <= size[0] for line in lines)
            assert str(overview.render()).count("GPU ") == 6
            assert "Work 20" in str(overview.render())
            assert viewport.max_scroll_y > 0
            app.save_screenshot(filename="fleet-%sx%s.svg" % size, path=str(tmp_path))
            await pilot.press("enter")
            assert isinstance(app.screen, GpuDetailDialog)
            assert " · Work · 20 GiB" in str(app.screen.query_one("#gpu-allocation-text").render())
            await pilot.press("escape", "p")
            await ready(app, pilot)
            assert table.size.height >= 3
            assert table.virtual_size.width <= table.size.width
            assert app.query_one("#fleet-footer").region.bottom <= size[1]
            assert cell_len(str(app.query_one("#fleet-footer").render())) <= size[0]
            assert app.query_one("#fleet-details").region.bottom <= app.query_one("#fleet-notice").region.y
            assert app.selected_service_id() == "quiet"
            assert app.history["hours"] == 168
            assert "7d active" in str(app.query_one("#fleet-detail-text").render())
            assert app.query_one("#fleet-active-chart", Sparkline).data
            assert "--max-model-len" in str(app.query_one("#fleet-history-text").render())
            assert table.get_cell("service:own", "activity").plain == "▁▂█·" + "▁" * 20
            await pilot.press("g")
            await ready(app, pilot)
            assert app.view == "gpu"
            assert app.selected_gpu == 0
            others = [key for key in app.row_keys if key.startswith("other:")]
            assert len(others) == 6
            for key in others:
                assert "(Work)" in table.get_cell(key, "service").plain
                assert table.get_cell(key, "mem").plain == "20"
                for column in ("activity", "idle", "status"):
                    assert table.get_cell(key, column).plain == ""
                if size[0] >= 100:
                    assert table.get_cell(key, "tokens").plain == ""
            await pilot.press("p")
            assert app.view == "person"
            assert all(path == "/v1/fleet" or path.startswith("/v1/fleet/history?")
                       for method, path, _ in client.calls if method == "GET")
    asyncio.run(scenario())


def test_unknown_history_and_host_owner_are_not_zero(fleet_snapshot):
    async def scenario():
        service = fleet_snapshot["services"][0]
        service.update(container=None, host=True, model=None, gpu_gb=None,
                       status="unknown", idle_seconds=None)
        app, client = make_app(fleet_snapshot)
        original = client.request

        def partial(method, path, payload=None):
            result = original(method, path, payload)
            if path.startswith("/v1/fleet/history"):
                result["samples"][4].update(active_minutes=None, gen_tokens=None)
            return result

        client.request = partial
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await select(app, pilot, "busy")
            assert "person:host" in app.row_keys
            assert app._table.get_cell("person:host", "mem").plain == "?"
            assert app._table.get_cell("service:busy", "idle").plain == "?"
            assert "unknown" in app._table.get_cell("service:busy", "service").plain
            assert not app.query_one("#fleet-active-chart", Sparkline).display
            assert "·" in str(app.query_one("#fleet-history-bars").render())
            assert "unknown hours" in str(app.query_one("#fleet-history-bars").render())
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_people_group_and_filter_proven_host_uids(fleet_snapshot, size):
    for index, service in enumerate(fleet_snapshot["services"]):
        service.update(container=None, host=True, host_uid=1000 if index < 2 else 1005,
                       host_user="operator", mine=False)
        for gpu in fleet_snapshot["gpus"]:
            for occupant in gpu["occupants"]:
                if occupant.get("service_id") == service["id"]:
                    occupant["container"] = None

    async def scenario():
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            await pilot.press("p", "n")
            await ready(app, pilot)
            groups = [key for key in app.row_keys if key.startswith("person:")]
            assert set(groups) == {"person:host:uid:1000", "person:host:uid:1005"}
            assert app._table.get_cell("person:host:uid:1000", "service").plain == "operator · 2svc"
            assert app._table.get_cell("person:host:uid:1005", "service").plain == "operator · 1svc"
            await select(app, pilot, "busy")
            assert "operator" in str(app.query_one("#fleet-detail-text").render())
            app.query_one("#fleet-filter", Input).value = "operator"
            await ready(app, pilot)
            assert set(app.row_services.values()) == {"busy", "quiet", "own"}
            assert not hasattr(app, "claim_allowed")
            fleet_snapshot["services"][0]["host_user"] = "operator-renamed"
            app.refresh_fleet()
            await ready(app, pilot)
            assert "person:host:uid:1000" in app.row_keys
            assert app.selected_service_id() == "busy"
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_people_owner_namespaces_keep_rows_and_selection_distinct(fleet_snapshot, size):
    for service, container in zip(fleet_snapshot["services"], ("host", None, "unknown")):
        service.update(container=container, host=container is None, host_uid=None, host_user=None)
        for gpu in fleet_snapshot["gpus"]:
            for occupant in gpu["occupants"]:
                if occupant.get("service_id") == service["id"]:
                    occupant.update(container=container, host=container is None, host_uid=None, host_user=None)

    async def scenario():
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            expected_groups = {"person:container:host", "person:host", "person:container:unknown"}
            assert app._table.row_count == len(app.row_keys) == len(set(app.row_keys)) == 6
            assert {key for key in app.row_keys if key.startswith("person:")} == expected_groups
            for service in fleet_snapshot["services"]:
                await select(app, pilot, service["id"])
                assert app.selected_service_id() == service["id"]
                assert service["model"] in str(app.query_one("#fleet-detail-text").render())
                assert app.history["service_id"] == service["id"]
            await select(app, pilot, "quiet")
            await pilot.press("j")
            await ready(app, pilot)
            assert app._table.cursor_row == app._table.get_row_index("person:container:host")
            assert app.selected_service_id() is None
            await pilot.press("j")
            await ready(app, pilot)
            assert app.selected_service_id() == "busy"
            await pilot.press("k", "k")
            await ready(app, pilot)
            assert app.selected_service_id() == "quiet"
            app.refresh_fleet()
            await ready(app, pilot)
            assert app.selected_service_id() == "quiet"
            assert app._table.row_count == len(app.row_keys) == len(set(app.row_keys)) == 6
            assert {key for key in app.row_keys if key.startswith("person:")} == expected_groups
    asyncio.run(scenario())


def test_late_history_cannot_replace_new_selection(fleet_snapshot):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        started, release = threading.Event(), threading.Event()
        original = client.request

        def delayed(method, path, payload=None):
            if "service=own" in path:
                started.set()
                assert release.wait(3)
            return original(method, path, payload)

        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            client.request = delayed
            app._table.move_cursor(row=app.row_keys.index("service:own"), animate=False)
            try:
                await pilot.pause(.15)
                assert await asyncio.to_thread(started.wait, 2)
                app._table.move_cursor(row=app.row_keys.index("service:busy"), animate=False)
                await pilot.pause(.15)
                release.set()
                await ready(app, pilot)
                assert app.selected_service_id() == "busy"
                assert app.history["service_id"] == "busy"
            finally:
                release.set()
    asyncio.run(scenario())


def test_invalid_snapshot_retains_last_good_observations(fleet_snapshot):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            client.snapshot = {"schema_version": 1, "services": [{"id": "broken", "window_7d": [1]}],
                               "gpus": [], "errors": []}
            await app.refresh_fleet().wait()
            assert app.snapshot["services"] == fleet_snapshot["services"]
            assert app.read_error
            assert app._table.get_cell("service:quiet", "status").plain == "Unknown"
            assert app.selected_service_id() == "quiet"
    asyncio.run(scenario())






def test_sort_filter_and_mine_keep_selection(fleet_snapshot):
    async def scenario():
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await pilot.press("s", "s")
            assert app.sort_mode == "mem"
            assert app.row_keys[0] == "person:container:group-a"
            assert app.selected_service_id() == "quiet"
            await pilot.press("slash")
            field = app.query_one("#fleet-filter", Input)
            assert field.has_focus
            await pilot.press("o", "w", "n")
            assert app.view == "person"
            await pilot.press("enter")
            assert not field.display
            assert app.selected_service_id() == "own"
            assert set(app.row_services.values()) == {"own"}
            field.value = ""
            await pilot.press("m")
            assert app.mine_only
            assert set(app.row_services.values()) == {"own"}
    asyncio.run(scenario())


def test_identical_reads_do_not_clear_update_or_reorder_rows(fleet_snapshot):
    async def scenario():
        app, _ = make_app(fleet_snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            await select(app, pilot, "own")
            table = app._table
            identities = dict(table.rows)
            with patch.object(table, "clear", wraps=table.clear) as clear, \
                 patch.object(table, "update_cell", wraps=table.update_cell) as update, \
                 patch.object(table, "sort", wraps=table.sort) as sort:
                for _ in range(3):
                    app.snapshot = copy.deepcopy(fleet_snapshot)
                    app.snapshot["services"].reverse()
                    app.render_snapshot()
                assert clear.call_count == update.call_count == sort.call_count == 0
                assert app.selected_service_id() == "own"
                assert all(table.rows[key] is value for key, value in identities.items())
                app.snapshot["services"][0]["idle_seconds"] = 600
                app.render_snapshot()
                assert update.call_count == 1
                assert app.selected_service_id() == "own"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["stale", "collector", "read"])
def test_failure_banner_marks_unknown_without_replacing_data(fleet_snapshot, failure):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            await pilot.press("p")
            await ready(app, pilot)
            if failure == "stale":
                client.snapshot["stale"] = True
            elif failure == "collector":
                client.snapshot["errors"] = ["GPU observations unavailable"]
            else:
                client.read_error = OSError("connection lost")
            await app.refresh_fleet().wait()
            assert app.query_one("#fleet-banner").display
            assert app._table.get_cell("service:quiet", "status").plain == "Unknown"
            assert app._table.get_cell("service:quiet", "idle").plain == "?"
            assert app.snapshot["services"][1]["status"] == "over_limit"
            assert app.selected_service_id() == "quiet"
    asyncio.run(scenario())






def test_events_coalesce_reads_and_exit_detaches_notifications(fleet_snapshot):
    async def scenario():
        reader = FixtureEvents()
        app, client = make_app(fleet_snapshot, reader)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            before = len([call for call in client.calls if call[1] == "/v1/fleet"])
            reader.events = [{"id": number, "kind": "fleet_status_changed"} for number in range(1, 101)]
            with patch.object(app, "post_message", wraps=app.post_message) as post:
                for _ in range(100):
                    reader.notify()
                assert sum(call.args[0].__class__.__name__ == "FleetEventsChanged" for call in post.call_args_list) == 1
            await pilot.pause(.35)
            await ready(app, pilot)
            assert len([call for call in client.calls if call[1] == "/v1/fleet"]) == before + 1
            reader.events = [{"id": 100, "kind": "fleet_status_changed"}]
            app.update_events()
            await ready(app, pilot)
            assert len([call for call in client.calls if call[1] == "/v1/fleet"]) == before + 1
            callback = reader.notify
        assert reader.notify is None
        assert reader.closes == 1
        callback()
        app.update_events()
        assert all(timer._task is None or timer._task.done() for timer in app._ui_timers)
    asyncio.run(scenario())


def test_slow_read_merges_refresh_and_allows_view_keys(fleet_snapshot):
    async def scenario():
        app, client = make_app(fleet_snapshot)
        started, release = threading.Event(), threading.Event()
        original = client.request
        count_reads = 0

        def delayed(method, path, payload=None):
            nonlocal count_reads
            if path == "/v1/fleet":
                count_reads += 1
                if count_reads == 1:
                    started.set()
                    assert release.wait(3)
            return original(method, path, payload)

        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            client.request = delayed
            worker = app.refresh_fleet()
            try:
                assert await asyncio.to_thread(started.wait, 2)
                for _ in range(20):
                    app.refresh_fleet()
                await pilot.press("g")
                assert app.view == "gpu"
            finally:
                release.set()
            await worker.wait()
            await ready(app, pilot)
            assert count_reads == 2
    asyncio.run(scenario())


def test_late_read_cannot_publish_during_shutdown(fleet_snapshot):
    async def scenario():
        base, client = make_app(fleet_snapshot)
        started, release = threading.Event(), threading.Event()
        snapshots = []

        class ClosingFleet(FleetApp):
            async def _close_all(self):
                assert not self.is_running
                release.set()
                await self.workers.wait_for_complete()
                snapshots.append(self.snapshot)
                await super()._close_all()

        reader = FixtureEvents()
        app = ClosingFleet(client, base.api, event_reader=reader)
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(app, pilot)
            original = app.snapshot

            def delayed(method, path, payload=None):
                started.set()
                assert release.wait(3)
                value = copy.deepcopy(fleet_snapshot)
                value["services"][0]["model"] = "late-model"
                return value

            client.request = delayed
            app.refresh_fleet()
            assert await asyncio.to_thread(started.wait, 2)
        assert snapshots == [original]
        assert snapshots[0] is original
        assert not app.fetching
        assert reader.closes == 1
        assert app._exception is None
    asyncio.run(scenario())
