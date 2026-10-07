# Generated-By: Codex / gpt-6.1-sol
"""Pilot exercises the existing event reader's real HTTP reconnect and cursor."""

import asyncio
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
pytest.importorskip("textual")

from tui.fleet_app import FleetApp
from test_tui_fleet import FixtureEvents, fleet_snapshot as snapshot_fixture, make_app, ready


@pytest.fixture
def fleet_snapshot():
    return snapshot_fixture.__wrapped__()


def frame(ident):
    item = {"id": ident, "timestamp": 1900000000 + ident, "kind": "fleet_status_changed",
            "detail": {"service_id": "quiet", "from": "idle", "to": "over_limit"}}
    return ("id: %s\nevent: fleet_status_changed\ndata: %s\n\n" %
            (ident, json.dumps(item))).encode()


@pytest.mark.parametrize("change", ["discovery", "exit", "address", "gpu", "freshness"])
def test_snapshot_event_refreshes_an_established_stream_without_status_changes(fleet_snapshot, change):
    async def scenario():
        reader = FixtureEvents()
        app, client = make_app(fleet_snapshot, reader)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            app.select_gpu(2, service_id="busy")
            await ready(app, pilot)
            assert app.connection == "Live changes connected"
            assert app.event_generation == reader.generation == 0 and app.event_cursor == 0
            states = {item["id"]: item["status"] for item in client.snapshot["services"]}
            if change == "discovery":
                discovered = copy.deepcopy(client.snapshot["services"][0])
                discovered["id"] = "discovered"
                client.snapshot["services"].append(discovered)
            elif change == "exit":
                client.snapshot["services"] = [item for item in client.snapshot["services"] if item["id"] != "busy"]
                for gpu in client.snapshot["gpus"]:
                    gpu["occupants"] = [item for item in gpu["occupants"] if item.get("service_id") != "busy"]
            elif change == "address":
                client.snapshot["services"][0].update(api_address="http://192.0.2.22:8000", api_access="direct")
            elif change == "gpu":
                client.snapshot["gpus"][2]["used_gb"] += 1
            else:
                client.snapshot["stale"] = True
            assert all(item["status"] == states.get(item["id"], item["status"])
                       for item in client.snapshot["services"])
            client.snapshot["generated_at"] += 1
            reads = sum(path == "/v1/fleet" for _, path, _ in client.calls)
            reader.events = [{"id": 1, "kind": "fleet_snapshot_changed",
                              "detail": {"generated_at": client.snapshot["generated_at"]}}]
            app.update_events()
            await ready(app, pilot)
            assert sum(path == "/v1/fleet" for _, path, _ in client.calls) == reads + 1
            assert app.snapshot == client.snapshot
            assert app.event_cursor == 1 and app.event_generation == reader.generation == 0
            assert app.connection == "Live changes connected"
            if change == "address":
                assert "http://192.0.2.22:8000" in app._rendered["fleet-detail-text"]
            reader.events = [{"id": 1, "kind": "fleet_snapshot_changed"}]
            app.update_events()
            await ready(app, pilot)
            assert sum(path == "/v1/fleet" for _, path, _ in client.calls) == reads + 1
    asyncio.run(scenario())


def test_real_sse_reconnect_keeps_cursor_and_closes_reader(fleet_snapshot):
    async def scenario():
        template, fixture = make_app(fleet_snapshot)
        api = template.api
        stop = threading.Event()
        event_requests, reads = [], []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/v1/events"):
                    event_requests.append((parse_qs(urlsplit(self.path).query)["since"][0],
                                           self.headers.get("Last-Event-ID")))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        if len(event_requests) == 1:
                            self.wfile.write(frame(1))
                        elif len(event_requests) == 2:
                            self.wfile.write(frame(1) + frame(2))
                        else:
                            while not stop.wait(.02):
                                self.wfile.write(b": heartbeat\n\n")
                                self.wfile.flush()
                        self.wfile.flush()
                    except (OSError, ConnectionError):
                        pass
                    return
                reads.append(self.path)
                body = json.dumps(fixture.request("GET", self.path)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("localhost", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = api.FleetClient("http://localhost:%s" % server.server_port, timeout=.5)
        reader = api.EventReader(client, stream_timeout=.5, retry_delay=.03, max_retry_delay=.03)
        app = FleetApp(client, api, event_reader=reader)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await ready(app, pilot)
                assert app.view == "gpu"
                await pilot.press("j", "right")
                selected = app.selected_gpu, app.selected_segment
                deadline = time.monotonic() + 3
                while app.event_cursor != 2 or len(event_requests) < 3:
                    assert time.monotonic() < deadline
                    await pilot.pause(.05)
                assert event_requests[:3] == [("0", "0"), ("1", "1"), ("2", "2")]
                assert reader.drain()["cursor"] == 2
                assert reads.count("/v1/fleet") >= 2
                assert "connected" in app.connection
                assert (app.selected_gpu, app.selected_segment) == selected
            assert not reader.thread.is_alive()
            assert reader._notify_callback is None
        finally:
            stop.set()
            reader.close()
            server.shutdown()
            server.server_close()
            thread.join(2)
    asyncio.run(scenario())
