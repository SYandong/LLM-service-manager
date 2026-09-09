# Generated-By: Codex / gpt-6-astra
"""Standard-library HTTP state endpoint and bounded-history SSE stream."""

import json
import logging
import socket
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from llmsvc.scheduler import IntentWriteError, Scheduler

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
            elif target.path == "/v1/models":
                if target.query:
                    self._json(400, {"error": "invalid_request"})
                else:
                    self._json(200, self.server.scheduler.registry_request("GET", target.path))
            elif target.path == "/v1/registry":
                if (target.query or self.headers.get("Transfer-Encoding") is not None
                        or self.headers.get_all("Content-Length", ["0"]) != ["0"]):
                    self._json(400, {"error": "invalid_request"})
                else:
                    self._json(200, self.server.scheduler.registry_request("GET", target.path))
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
        except IntentWriteError as exc:
            self._json(exc.status, {"error": exc.error, **({"message": exc.message} if hasattr(exc, "message") else {})})
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
        while True:
            events = scheduler.events_since(cursor, scheduler.config.event_heartbeat_seconds)
            for event in events:
                data = json.dumps(asdict(event), allow_nan=False)
                self.wfile.write(f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n".encode("utf-8"))
                cursor = event.id
            if not events:
                self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            if scheduler.events_closed.is_set() and not scheduler.events_since(cursor):
                break

    def _read_only(self):
        self._json(405, {"error": "read_only", "message": "Scheduler is read-only"})

    def _reject_write(self):
        if self.server.scheduler.config.read_only:
            self._read_only()
        else:
            self._json(405, {"error": "operation_not_enabled"})

    def _write_request(self):
        try:
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            dry_run = "dry_run" in query
            if query and query != {"dry_run": ["1"]}:
                raise ValueError("expected dry_run=1")
            if not dry_run and self.server.scheduler.config.read_only:
                self._read_only()
                return
            registry_path = None
            if ((self.command == "POST" and target.path == "/v1/models")
                    or (self.command == "DELETE" and target.path.startswith("/v1/models/"))):
                if not dry_run:
                    self._reject_write()
                    return
                registry_path = "/v1/models" if self.command == "POST" else "/v1/models/" + unquote(target.path[len("/v1/models/"):])
            operation = "registry" if registry_path is not None else None
            payload = {}
            wake_model = None
            lease_id = None
            if self.command == "POST":
                operation = {"/v1/free": "free", "/v1/pin": "pin", "/v1/reserve": "reserve", "/v1/place": "place"}.get(target.path, operation)
                if target.path.startswith("/v1/wake/"):
                    operation = "wake"
                    wake_model = unquote(target.path[len("/v1/wake/"):])
                parts = target.path.split("/")
                if len(parts) == 5 and parts[:3] == ["", "v1", "place"] and parts[4] in ("confirm", "release"):
                    operation = parts[4]
                    lease_id = unquote(parts[3])
                    if not lease_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in lease_id):
                        raise ValueError("invalid lease id")
            elif self.command == "DELETE":
                for prefix, op, key in (("/v1/pin/", "unpin", "model"), ("/v1/reserve/", "unreserve", "id")):
                    if target.path.startswith(prefix):
                        operation = op
                        payload[key] = unquote(target.path[len(prefix):])
            if operation is None or (not dry_run and operation not in ("pin", "unpin", "free", "wake", "place", "confirm", "release", "reserve", "unreserve")):
                self._reject_write()
                return
            if not dry_run and operation in ("free", "wake") and (not self.server.scheduler.config.model_actions_enabled or self.server.scheduler.model_actions is None):
                self._reject_write()
                return
            if not dry_run and operation in ("place", "confirm", "release") and (not self.server.scheduler.config.placement_enabled or self.server.scheduler.placement is None):
                self._reject_write()
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
                raise ValueError("DELETE does not accept a body")
            if length:
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("incomplete request body")
                def reject_constant(value):
                    raise ValueError("non-finite JSON number")
                payload = json.loads(data, parse_constant=reject_constant)
            if operation == "wake":
                if payload != {}:
                    raise ValueError("wake accepts an empty body")
                payload = {"model": wake_model}
            if lease_id is not None:
                if payload != {}:
                    raise ValueError("lease transition accepts an empty body")
                payload = {"lease_id": lease_id}
            if operation == "registry":
                result = self.server.scheduler.registry_request(self.command, registry_path, payload, dry_run=True)
            elif operation in ("reserve", "unreserve"):
                result = self.server.scheduler.run_reserve(operation, payload, source_ip=self.client_address[0], dry_run=dry_run)
            elif dry_run:
                result = self.server.scheduler.preview(operation, payload)
            elif operation in ("place", "confirm", "release"):
                result = self.server.scheduler.run_placement(operation, payload)
            elif operation in ("free", "wake"):
                result = self.server.scheduler.run_model_action(operation, payload, source_ip=self.client_address[0])
            else:
                result = self.server.scheduler.write_pin(operation, payload, source_ip=self.client_address[0])
            self._json(200, result)
        except IntentWriteError as exc:
            error = {"error": exc.error}
            if hasattr(exc, "message"):
                error["message"] = exc.message
            if hasattr(exc, "blockers"):
                error["blockers"] = [asdict(blocker) for blocker in exc.blockers]
            self._json(exc.status, error)
        except (ValueError, TypeError, UnicodeError):
            self._json(400, {"error": "invalid_request"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    do_POST = _write_request
    do_DELETE = _write_request
    do_PUT = _reject_write
    do_PATCH = _reject_write
