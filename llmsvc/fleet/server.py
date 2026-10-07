# Generated-By: Codex / gpt-6.1-sol
"""Three read-only fleet routes, independent of the scheduler HTTP server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
from urllib.parse import parse_qs, urlsplit

from llmsvc.fleet import FleetError
from llmsvc.fleet.events import MAX_EVENT_ID

LOG = logging.getLogger("llmsvc.fleet.http")


class ObserverHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, observer):
        self.observer = observer
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, ObserverHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.observer.config.request_timeout_seconds)
        return connection, address


class ObserverHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        LOG.info('{"kind":"observer_http_request"}')

    def _json(self, status, payload, *, allow=None):
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if allow is not None:
            self.send_header("Allow", allow)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.command != "GET":
            self._json(405, {"error": "read_only_observer"}, allow="GET")
            return False
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get_all("Content-Length", ["0"]) != ["0"]:
            self._json(400, {"error": "invalid_request"})
            return False
        return True

    def do_GET(self):
        try:
            target = urlsplit(self.path)
            if len(self.path) > 2048 or target.fragment or target.netloc:
                raise FleetError(400, "invalid_request")
            query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8) if target.query else {}
            if target.path == "/v1/fleet":
                if query not in ({}, {"mine": ["1"]}):
                    raise FleetError(400, "invalid_fleet_query")
                self._json(200, self.server.observer.report(source_ip=self.client_address[0], mine="mine" in query))
            elif target.path == "/v1/fleet/history":
                if (set(query) != {"service", "hours"} or any(len(values) != 1 for values in query.values())
                        or not 1 <= len(query["service"][0]) <= 512 or query["hours"][0] not in ("24", "168")):
                    raise FleetError(400, "invalid_fleet_history_query")
                self._json(200, self.server.observer.history(query["service"][0], int(query["hours"][0])))
            elif target.path == "/v1/events":
                if set(query) - {"since", "incarnation"} or any(len(values) != 1 for values in query.values()):
                    raise FleetError(400, "invalid_event_cursor")
                headers = self.headers.get_all("Last-Event-ID", [])
                value = query.get("since", headers or ["0"])[0]
                if (len(headers) > 1 or len(value) > 19 or not value.isascii() or not value.isdigit()
                        or int(value) > MAX_EVENT_ID or (headers and headers[0] != value)):
                    raise FleetError(400, "invalid_event_cursor")
                incarnation = query.get("incarnation", [None])[0]
                if incarnation is not None and (len(incarnation) != 32 or any(char not in "0123456789abcdef" for char in incarnation)):
                    raise FleetError(400, "invalid_event_incarnation")
                self._events(int(value), incarnation)
            else:
                self._json(404, {"error": "not_found"})
        except FleetError as error:
            self._json(error.status, {"error": error.error})
        except (ValueError, TypeError):
            self._json(400, {"error": "invalid_request"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _event(self, event):
        data = json.dumps(event, allow_nan=False, separators=(",", ":"))
        self.wfile.write(f"id: {event['id']}\nevent: {event['kind']}\ndata: {data}\n\n".encode())

    def _events(self, cursor, incarnation):
        observer = self.server.observer
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Observer-Incarnation", observer.events.incarnation)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": connected\n\n")
        while True:
            reason = observer.events.reset_reason(cursor, incarnation)
            if reason is not None:
                self._event(observer.events.reset_event(reason))
                cursor = 0
            incarnation = observer.events.incarnation
            events = observer.events.events_since(cursor, observer.config.event_heartbeat_seconds)
            for event in events:
                self._event(event)
                cursor = event["id"]
            if not events:
                self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            if observer.events.closed.is_set() and not observer.events.events_since(cursor):
                break
