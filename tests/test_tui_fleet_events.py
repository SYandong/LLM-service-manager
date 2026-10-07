# Generated-By: Codex / gpt-6.1-sol
"""Pilot exercises the existing event reader's real HTTP reconnect and cursor."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
pytest.importorskip("textual")

from tui.fleet_app import FleetApp
from test_tui_fleet import fleet_snapshot, make_app, ready


def frame(ident):
    item = {"id": ident, "timestamp": 1900000000 + ident, "kind": "fleet_status_changed",
            "detail": {"service_id": "quiet", "from": "idle", "to": "over_limit"}}
    return ("id: %s\nevent: fleet_status_changed\ndata: %s\n\n" %
            (ident, json.dumps(item))).encode()


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
