# Generated-By: Codex / gpt-6-astra
"""Standard-library HTTP state endpoint and bounded-history SSE stream."""

import json
import logging
import socket
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

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
            elif target.path == "/v1/usage":
                self._usage(target.query)
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

    def _usage(self, raw_query):
        query = parse_qs(raw_query, keep_blank_values=True)
        if set(query) - {"days", "by"} or any(len(values) != 1 for values in query.values()):
            self._json(400, {"error": "invalid_usage_query"})
            return
        days = query.get("days", ["7"])[0]
        by = query.get("by", ["container"])[0]
        try:
            if not days.isascii() or not days.isdigit():
                raise ValueError("invalid days")
            result = self.server.scheduler.usage(days=int(days), by=by)
        except ValueError:
            self._json(400, {"error": "invalid_usage_query"})
            return
        self._json(200 if result["known"] else 503, result)

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

    do_POST = _read_only
    do_DELETE = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
