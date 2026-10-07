# Generated-By: Codex / gpt-6.1-sol
"""The deployed standalone fleet reader exposes reads without scheduler controls."""

import copy
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


CLI = Path(__file__).resolve().parents[1] / "cli/fleet-llm"


@pytest.fixture
def reader_api():
    return runpy.run_path(str(CLI))


@pytest.fixture
def snapshot():
    return {"schema_version": 1, "generated_at": 1900000000, "snapshot_age_seconds": 12,
            "observer_incarnation": "a" * 32, "stale": False,
            "config": {"idle_limit_hours": 6, "claims_enabled": False}, "errors": [],
            "shared_models": [], "containers": [],
            "gpus": [{"index": 0, "used_gb": 50, "total_gb": 140, "util_percent": 25,
                      "occupants": [{"container": "private-owner", "kind": "other", "used_gb": 10}]}],
            "services": [{"id": "private-owner:1/2 %", "container": "private-owner",
                          "model": "/srv/models/gemma-4-31b-it-qat-w4a16-ct", "engine": "vllm",
                          "mine": True, "gpus": [0], "gpu_gb": 40, "status": "idle",
                          "idle_seconds": 120, "uptime_seconds": 3600,
                          "api_address": "http://192.0.2.10:8000", "api_access": "shared",
                          "hourly_active_24h": [None, 0, 15, 60] * 6,
                          "window_24h": {"requests": 5, "prompt_tokens": None, "gen_tokens": 10,
                                          "active_minutes": 15, "coverage_ratio": .5},
                          "claim": None}], "future_field": {"preserve": None}}


@pytest.fixture
def observer(snapshot):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.command, self.path))
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/fleet":
                result = copy.deepcopy(snapshot)
            elif parsed.path == "/v1/fleet/history":
                query = parse_qs(parsed.query)
                hours = int(query["hours"][0])
                result = {"schema_version": 1, "service_id": query["service"][0], "hours": hours,
                          "resolution": "hourly" if hours == 168 else "minute",
                          "service": dict(snapshot["services"][0],
                                          argv_redacted="vllm serve /srv/models/gemma-4-31b-it-qat-w4a16-ct --owner private-owner"),
                          "samples": [{"ts": 1900000000, "active": None, "d_requests": None,
                                       "d_prompt_tokens": None, "d_gen_tokens": None}]}
            else:
                self.send_error(404)
                return
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            calls.append((self.command, self.path))
            self.send_error(405)

        do_DELETE = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s" % server.server_port, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("command", [[], ["top"], ["status"], ["fleet"], ["fleet", "--by", "gpu"]])
def test_copied_reader_runs_with_only_stdlib_and_gets(tmp_path, observer, command):
    address, calls = observer
    copied = tmp_path / "llm"
    shutil.copyfile(CLI, copied)
    completed = subprocess.run([sys.executable, "-I", "-S", str(copied), "--url", address,
                                "--config", str(tmp_path / "absent"), *command], cwd=tmp_path,
                               env=dict(os.environ, COLUMNS="100"), capture_output=True,
                               text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert "User " in completed.stdout and "private-owner" not in completed.stdout
    assert "gemma-4-31b-it-qat-w4a16-ct" in completed.stdout and "/srv/" not in completed.stdout
    assert "http://192.0.2.10:8000 · Shared" in completed.stdout
    assert calls == [("GET", "/v1/fleet")]


def test_raw_json_mine_and_history_keep_route_identity(reader_api, observer, snapshot):
    address, calls = observer
    client = reader_api["FleetClient"](address)
    args = reader_api["build_parser"]().parse_args(["fleet", "--mine", "--json"])
    result = reader_api["execute_command"](args, client)
    assert json.loads(reader_api["format_result"](args, result)) == snapshot
    args = reader_api["build_parser"]().parse_args(["history", snapshot["services"][0]["id"], "--hours", "168"])
    history = reader_api["execute_command"](args, client)
    text = reader_api["format_result"](args, history, width=200)
    assert "private-owner" not in text and "User " in text and "/srv/" not in text
    assert "Parameters: vllm serve gemma-4-31b-it-qat-w4a16-ct --owner User " in text
    assert calls[0] == ("GET", "/v1/fleet?mine=1")
    assert parse_qs(urlsplit(calls[1][1]).query) == {"service": [snapshot["services"][0]["id"]], "hours": ["168"]}


@pytest.mark.parametrize("command", ["claim", "unclaim", "pin", "unpin", "free", "wake", "sleep", "stop",
                                      "preload", "reserve", "unreserve", "models", "registry", "usage", "legacy-tui"])
def test_retired_commands_are_absent(reader_api, command):
    with pytest.raises(SystemExit) as exit_:
        reader_api["build_parser"]().parse_args([command])
    assert exit_.value.code == 2


def test_shared_controls_and_legacy_imports_are_absent(reader_api):
    with pytest.raises(SystemExit):
        reader_api["build_parser"]().parse_args(["status", "--shared"])
    assert "tui_app" not in reader_api and "SchedulerClient" not in reader_api
    assert "claim_until" not in reader_api and "execute_wake_with_progress" not in reader_api
    assert "{status,fleet,top,history}" in reader_api["build_parser"]().format_help()


@pytest.mark.parametrize("method,path,payload", [("POST", "/v1/fleet/claims", {}),
    ("DELETE", "/v1/fleet/claims/id", None), ("GET", "/v1/state", None),
    ("GET", "/v1/fleet", {}), ("GET", "https://example.invalid/v1/fleet", None),
    ("GET", "/v1/fleet#fragment", None)])
def test_transport_rejects_writes_and_unrelated_reads_before_open(reader_api, method, path, payload):
    client = reader_api["FleetClient"]("http://127.0.0.1:1", opener=lambda *a, **k: pytest.fail("opened"))
    with pytest.raises(reader_api["ClientError"], match="only GET"):
        client.request(method, path, payload)


@pytest.mark.parametrize("show_names", [False, True])
def test_legacy_claims_are_inert_and_names_are_optional(reader_api, snapshot, show_names):
    snapshot["services"][0]["claim"] = {"id": "old-claim", "reason": "retired-claim-reason", "until": 2000000000}
    reader_api["validate_fleet"](snapshot)
    text = reader_api["format_fleet"](snapshot, width=200, show_names=show_names)
    assert ("private-owner" in text) is show_names
    assert "old-claim" not in text and "retired-claim-reason" not in text
    snapshot["services"][0]["status"] = "claimed"
    with pytest.raises(reader_api["ClientError"], match="Invalid fleet"):
        reader_api["validate_fleet"](snapshot)


def test_existing_endpoint_config_survives_without_inference_controls(reader_api, tmp_path):
    config = tmp_path / "config"
    config.write_text("[llm]\nurl=http://127.0.0.1:8015\ntimeout=4\napi_url=http://127.0.0.1:8000/v1\n")
    assert reader_api["load_config"](environ={}, path=config) == {"url": "http://127.0.0.1:8015", "timeout": 4}
    assert reader_api["load_config"](environ={"LLM_URL": "http://127.0.0.1:8020"}, path=config)["url"].endswith(":8020")


@pytest.mark.parametrize("command", [[], ["top"], ["top", "--show-names"], ["status"], ["history", "id"]])
@pytest.mark.parametrize("tty,available", [(False, False), (False, True), (True, False), (True, True)])
def test_optional_tui_dispatch_is_fleet_only(reader_api, monkeypatch, capsys, snapshot, command, tty, available):
    globals_ = reader_api["main"].__globals__
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, path):
            calls.append((method, path))
            if path.startswith("/v1/fleet/history"):
                return {"schema_version": 1, "service_id": "id", "hours": 24, "resolution": "minute", "samples": []}
            return snapshot

    class App:
        def __init__(self, client, api, show_names=False):
            assert api.FleetClient is Client
            calls.append(("TUI", show_names))

        def run(self):
            pass

    def optional():
        assert tty
        return App if available else None

    monkeypatch.setattr(globals_["sys"].stdout, "isatty", lambda: tty)
    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "FleetClient", Client)
    monkeypatch.setitem(globals_, "fleet_tui_app", optional)
    assert reader_api["main"](command) == 0
    launches = tty and available and (not command or command[0] == "top")
    assert calls[0][0] == ("TUI" if launches else "GET")
    if launches:
        assert calls[0][1] is ("--show-names" in command)
    assert not capsys.readouterr().err


def event_frame(ident, incarnation, kind="fleet_status_changed"):
    event = {"id": ident, "timestamp": 1900000000 + ident, "kind": kind,
             "observer_incarnation": incarnation, "detail": {"reason": "cursor_expired"}}
    return ("id: %s\nevent: %s\ndata: %s\n\n" % (ident, kind, json.dumps(event))).encode()


@pytest.mark.parametrize("new_incarnation", ["a" * 32, "b" * 32])
def test_sse_reconnect_preserves_cursor_and_reset_replays_fresh_events(reader_api, new_incarnation):
    calls, release, stop = [], threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((parse_qs(urlsplit(self.path).query), self.headers.get("Last-Event-ID")))
            index = len(calls)
            if index == 3:
                release.wait(3)
            incarnation = "a" * 32 if index < 3 else new_incarnation
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Observer-Incarnation", incarnation)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if index == 1:
                    self.wfile.write(event_frame(1, incarnation))
                elif index == 2:
                    self.wfile.write(event_frame(1, incarnation) + event_frame(2, incarnation))
                elif index == 3:
                    self.wfile.write(event_frame(0, incarnation, "cursor_reset") + event_frame(1, incarnation))
                else:
                    while not stop.wait(.02):
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                self.wfile.flush()
            except (OSError, ConnectionError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reader = reader_api["EventReader"](reader_api["FleetClient"]("http://127.0.0.1:%s" % server.server_port, timeout=.5),
                                       stream_timeout=.5, retry_delay=.03, max_retry_delay=.03)
    try:
        reader.start()
        deadline = time.monotonic() + 3
        while len(calls) < 3:
            assert time.monotonic() < deadline
            time.sleep(.01)
        before = reader.drain()
        assert before["cursor"] == 2 and [item["id"] for item in before["events"]] == [1, 2]
        release.set()
        while len(calls) < 4:
            assert time.monotonic() < deadline
            time.sleep(.01)
        after = reader.drain()
        assert after["generation"] > before["generation"] and after["cursor"] == 1
        assert [item["id"] for item in after["events"]] == [1]
        assert after["missed"] == after["dropped"] == 0
        assert calls[:3] == [({"since": ["0"]}, "0"),
                             ({"since": ["1"], "incarnation": ["a" * 32]}, "1"),
                             ({"since": ["2"], "incarnation": ["a" * 32]}, "2")]
        assert calls[3] == ({"since": ["1"], "incarnation": [new_incarnation]}, "1")
    finally:
        release.set()
        stop.set()
        reader.close(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert not reader.thread.is_alive()


def test_polled_incarnation_change_resets_cursor_but_same_incarnation_does_not(reader_api):
    reader = reader_api["EventReader"](reader_api["FleetClient"]("http://127.0.0.1:1"))
    reader.observe_incarnation("a" * 32)
    reader._cursor, reader._missed, reader._dropped = 100, 4, 2
    reader._queue.append({"id": 100})
    reader.observe_incarnation("a" * 32)
    assert reader.drain()["cursor"] == 100
    reader._queue.append({"id": 100})
    reader.observe_incarnation("b" * 32)
    result = reader.drain()
    assert result["cursor"] == result["missed"] == result["dropped"] == 0
    assert result["generation"] == 1 and result["events"] == []


def test_invalid_json_and_safe_http_reasons_remain_actionable(reader_api, monkeypatch, capsys):
    client = reader_api["FleetClient"]("http://127.0.0.1:1", opener=lambda *a, **k: io.BytesIO(b'{"value": NaN}'))
    with pytest.raises(reader_api["ClientError"], match="invalid JSON"):
        client.request("GET", "/v1/fleet")
    globals_ = reader_api["main"].__globals__

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, *args):
            raise reader_api["ClientError"]("private-owner detail", status=503,
                                             payload={"error": "fleet_store_unavailable"})

    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "FleetClient", Client)
    assert reader_api["main"](["status"]) == 1
    assert "fleet_store_unavailable" in capsys.readouterr().err
