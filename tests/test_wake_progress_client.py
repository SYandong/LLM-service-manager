# Generated-By: Codex / gpt-5.6-luna
"""CLI/TUI consume the sanitized scheduler wake-progress envelope only."""

import runpy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def api():
    return runpy.run_path(str(Path(__file__).parents[1] / "cli" / "llm"))


def event(detail, *, model="model", event_id=8, timestamp=100.0):
    return {"id": event_id, "timestamp": timestamp, "kind": "wake_progress", "model": model,
            "detail": detail}


def detail(stage="health_wait", **changes):
    value = {"stage": stage, "source": "llama-swap", "source_model": "model",
             "progress_source": "per_model_log", "log_epoch": "abc", "sequence": 2,
             "received_at": 100.0, "trusted_for_quiet": False}
    value.update(changes)
    return value


def test_cli_progress_parser_rejects_stale_wrong_source_and_untrusted_shapes():
    module = api()
    assert module["parse_wake_progress"](event(detail()), "model", after_id=7, since=99)["label"] == "waiting for health"
    assert module["parse_wake_progress"](event(detail(), event_id=7), "model", after_id=7) is None
    assert module["parse_wake_progress"](event(detail(source="proxy")), "model") is None
    assert module["parse_wake_progress"](event(detail(source_model="other")), "model") is None
    assert module["parse_wake_progress"](event(detail(stage="loading_weights")), "model") is None
    assert module["parse_wake_progress"](event(detail(trusted_for_quiet=True)), "model") is None
    assert module["parse_wake_progress"](event(detail(), model="other"), "model") is None


def test_cli_progress_labels_never_include_raw_log_values():
    module = api()
    parsed = module["parse_wake_progress"](event(detail("process_started")), "model")
    assert module["format_wake_progress"](parsed) == "daemon process started"
    assert parsed["log_epoch"] == "abc"
    assert "PID" not in module["format_wake_progress"](parsed)
    assert module["format_wake_progress"]({"stage": "unavailable"}) == "progress unavailable"


def test_epoch_tracker_retires_replayed_epochs_within_one_wake():
    module = api()
    state = {"log_epoch": None, "sequence": 0, "retired_epochs": set()}
    first = module["parse_wake_progress"](event(detail(log_epoch="A", sequence=1)), "model")
    second = module["parse_wake_progress"](event(detail(log_epoch="B", sequence=1), event_id=9), "model")
    replay = module["parse_wake_progress"](event(detail(log_epoch="A", sequence=1), event_id=10), "model")
    replay_later = module["parse_wake_progress"](event(detail(log_epoch="A", sequence=2), event_id=11), "model")
    assert module["accept_wake_progress"](first, state)
    assert module["accept_wake_progress"](second, state)
    assert not module["accept_wake_progress"](replay, state)
    assert not module["accept_wake_progress"](replay_later, state)


def test_cli_wake_uses_one_sse_reader_while_delayed_post_runs(capsys):
    module = api()
    events_ready, post_seen, stop = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != "/v1/events?since=0":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            events_ready.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            post_seen.wait(2)
            item = {"id": 1, "timestamp": time.time(), "kind": "wake_progress", "model": "model",
                    "detail": {"stage": "health_wait", "source": "llama-swap", "source_model": "model",
                               "progress_source": "per_model_log", "log_epoch": "epoch",
                               "sequence": 1, "received_at": time.time(), "trusted_for_quiet": False}}
            try:
                data = json.dumps(item).encode()
                self.wfile.write(b"id: 1\nevent: wake_progress\ndata: " + data + b"\n\n")
                self.wfile.flush()
                stop.wait(2)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            assert self.path == "/v1/wake/model"
            calls.append(self.path)
            post_seen.set()
            assert events_ready.wait(2)
            time.sleep(0.05)
            body = json.dumps({"model": "model", "status": "ready", "ready": True,
                               "elapsed_seconds": 0.1, "cold_start": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%s" % server.server_port
    try:
        assert module["main"](["--url", url, "wake", "model"]) == 0
        out, err = capsys.readouterr()
        assert "Wake model status: ready" in out
        assert "Wake observed: waiting for health" in err
        assert calls == ["/v1/wake/model"]
        stop.set()
        events_ready.clear(); post_seen.clear(); stop.clear()
        assert module["main"](["--url", url, "wake", "model", "--json"]) == 0
        out, err = capsys.readouterr()
        assert json.loads(out)["status"] == "ready"
        assert err == ""
        assert calls == ["/v1/wake/model", "/v1/wake/model"]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
