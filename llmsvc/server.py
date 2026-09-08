# Generated-By: Codex / gpt-6-astra
"""Standard-library HTTP state endpoint and bounded-history SSE stream."""

import json
import logging
import socket
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from llmsvc.scheduler import Scheduler

LOG = logging.getLogger("llmsvc.http")


class SchedulerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, scheduler: Scheduler):
        self.scheduler = scheduler
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, SchedulerHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.scheduler.config.request_timeout_seconds)
        return connection, address


class SchedulerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Avoid request paths, query strings and arbitrary client-controlled text
        # in the structured service log.
        LOG.info(json.dumps({"kind": "http_request", "method": self.command}))

    def _json(self, status, payload):
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        try:
            target = urlsplit(self.path)
            if target.path == "/v1/state":
                self._json(200, self.server.scheduler.snapshot().to_dict())
            elif target.path == "/v1/events":
                query = parse_qs(target.query, keep_blank_values=True)
                values = query.get("since", [self.headers.get("Last-Event-ID", "0")])
                if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
                    self._json(400, {"error": "since must be a nonnegative integer event ID"})
                    return
                try:
                    cursor = int(values[0])
                except ValueError:
                    self._json(400, {"error": "invalid event ID"})
                    return
                self._events(cursor)
            else:
                self._json(404, {"error": "not_found"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _events(self, cursor):
        scheduler = self.server.scheduler
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        while not scheduler.stopping.is_set():
            events = scheduler.events_since(cursor, scheduler.config.event_heartbeat_seconds)
            for event in events:
                data = json.dumps(asdict(event), allow_nan=False)
                self.wfile.write(f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n".encode("utf-8"))
                cursor = event.id
            if not events:
                self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()

    def _read_only(self):
        self._json(405, {"error": "read_only", "message": "M1 does not execute actions"})

    def _preview_request(self):
        try:
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            if "dry_run" not in query:
                self._read_only()
                return
            if query != {"dry_run": ["1"]}:
                raise ValueError("expected dry_run=1")
            operation = None
            payload = {}
            if self.command == "POST":
                operation = {"/v1/free": "free", "/v1/pin": "pin", "/v1/reserve": "reserve"}.get(target.path)
            elif self.command == "DELETE":
                for prefix, op, key in (("/v1/pin/", "unpin", "model"), ("/v1/reserve/", "unreserve", "id")):
                    if target.path.startswith(prefix):
                        operation = op
                        payload[key] = unquote(target.path[len(prefix):])
            if operation is None:
                self._read_only()
                return
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("transfer encoding is not supported")
            lengths = self.headers.get_all("Content-Length", ["0"])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise ValueError("invalid Content-Length")
            length = int(lengths[0])
            if length > 65536:
                self._json(413, {"error": "request_too_large"})
                return
            if self.command == "DELETE" and length:
                raise ValueError("DELETE preview does not accept a body")
            if length:
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("incomplete request body")
                def reject_constant(value):
                    raise ValueError("non-finite JSON number")
                payload = json.loads(data, parse_constant=reject_constant)
            self._json(200, self.server.scheduler.preview(operation, payload))
        except (ValueError, TypeError, UnicodeError):
            self._json(400, {"error": "invalid_request"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    do_POST = _preview_request
    do_DELETE = _preview_request
    do_PUT = _read_only
    do_PATCH = _read_only
