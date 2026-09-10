# Generated-By: Codex / gpt-6-astra
"""CLI usage reconciliation against a disposable SQLite-backed HTTP service."""

import json
import runpy
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import llmsvc.activity as activity_module
from llmsvc.__main__ import build_usage
from llmsvc.activity import ActivityReader
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer, SchedulerHandler


@pytest.fixture
def usage_api():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))


@pytest.fixture
def usage_service(tmp_path):
    database = tmp_path / "activity.sqlite"
    now = int(time.time())
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE activity (id INTEGER PRIMARY KEY, ts_created INTEGER, model_id TEXT, input_tokens INTEGER, output_tokens INTEGER, metadata_json TEXT)")
        connection.executemany("INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?)", [
            (1, now - 86400, "model-a", 11, 22, '{"src":"ip:192.0.2.1"}'),
            (2, now - 2 * 86400, "model-a", 7, 3, None),
            (3, now - 10 * 86400, "model-b", 100, 200, '{"src":"ip:192.0.2.2"}'),
            (4, now - 40 * 86400, "old", 999, 999, None),
            (5, now + 86400, "future", 555, 555, None),
        ])
    before = database.read_bytes()
    # This fixture tests HTTP/rendering semantics, not the 80 ms production
    # read budget. Keep a bounded 1 s read budget below the UI's 2 s timeout.
    reader = ActivityReader(database, {"192.0.2.1": "ctr-a"}, deadline_ms=1000)
    # Real request-local reader factory; no collector sampling or external probes.
    usage = build_usage(SimpleNamespace(activity_reader=reader))
    scheduler = Scheduler(SchedulerConfig("127.0.0.1", 1), usage=usage)
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    paths = []

    class TrackingHandler(SchedulerHandler):
        def do_GET(self):
            paths.append(self.path)
            super().do_GET()

    server.RequestHandlerClass = TrackingHandler
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield SimpleNamespace(url="http://127.0.0.1:%s" % server.server_port,
                              scheduler=scheduler, paths=paths, backend=usage, database=database)
        assert database.read_bytes() == before
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("days,totals", [(7, (2, 18, 25)), (30, (3, 118, 225))])
@pytest.mark.parametrize("by", ["container", "ip", "model"])
def test_http_totals_match_backend_and_render_without_rounding(usage_api, usage_service, days, totals, by):
    args = usage_api["build_parser"]().parse_args(["usage", "--days", str(days), "--by", by])
    result = usage_api["execute_command"](args, usage_api["SchedulerClient"](usage_service.url))
    expected = dict(zip(("requests", "input_tokens", "output_tokens"), totals))
    assert result["totals"] == expected
    assert result == json.loads(json.dumps(usage_service.backend(days=days, by=by)))
    text = usage_api["format_usage"](result, width=40)
    assert "Requests: %s" % totals[0] in text
    assert "Input tokens: %s" % totals[1] in text
    assert "Output tokens: %s" % totals[2] in text
    assert all(usage_api["cell_width"](line) <= 40 for line in text.splitlines())
    if by != "model":
        assert any(not row["source_known"] for row in result["rows"])
        assert "unknown" in text


def test_unknown_origin_and_unmapped_ip_are_distinct(usage_api, usage_service):
    result = usage_api["SchedulerClient"](usage_service.url).request("GET", "/v1/usage?days=30&by=container")
    text = usage_api["format_usage"](result)
    assert "ctr-a" in text
    assert "unknown" in text
    assert "ip:192.0.2.2" in text
    assert "IP only" in text
    assert result["totals"]["requests"] == 3


@pytest.mark.parametrize("as_json", [False, True])
def test_copied_cli_usage_and_unavailable_exit_status(tmp_path, usage_api, usage_service, as_json):
    script = tmp_path / "llm"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cli" / "llm", script)
    command = [sys.executable, "-I", "-S", str(script), "--url", usage_service.url,
               "--config", str(tmp_path / "missing"), "usage", "--days", "30"]
    if as_json:
        command.append("--json")
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    if as_json:
        assert json.loads(result.stdout)["totals"] == {"requests": 3, "input_tokens": 118, "output_tokens": 225}
    else:
        assert "TOTAL" in result.stdout and "118" in result.stdout and "225" in result.stdout
    usage_service.scheduler._usage = None
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["known"] is False
        assert all(value is None for value in payload["totals"].values())
    else:
        assert "Unavailable: usage_not_configured" in result.stdout
        assert "Requests ?" in result.stdout
        assert "TOTAL" not in result.stdout


def valid_result():
    return {"days": 7, "by": "container", "known": True, "error": None,
            "rows": [{"container": "unknown", "source_known": False, "requests": 1,
                      "input_tokens": 9007199254740993, "output_tokens": 17}],
            "totals": {"requests": 1, "input_tokens": 9007199254740993, "output_tokens": 17}}


def test_large_integer_counts_are_exact(usage_api):
    for width in [40, 100]:
        text = usage_api["format_usage"](valid_result(), width=width)
        assert "9007199254740993" in text
        assert "9007199254740992" not in text


@pytest.mark.parametrize("field,value", [("requests", None), ("requests", False), ("input_tokens", 1.5), ("output_tokens", -1)])
def test_invalid_counts_are_not_coerced(usage_api, field, value):
    result = valid_result()
    result["rows"][0][field] = value
    with pytest.raises(usage_api["ClientError"], match="Invalid usage"):
        usage_api["format_usage"](result)


def test_mismatched_or_missing_totals_fail_explicitly(usage_api):
    for totals in [{}, {"requests": 0, "input_tokens": 0, "output_tokens": 0}]:
        result = valid_result()
        result["totals"] = totals
        with pytest.raises(usage_api["ClientError"]):
            usage_api["format_usage"](result)


def test_available_empty_window_is_known_zero(usage_api):
    result = valid_result()
    result["rows"] = []
    result["totals"] = {key: 0 for key in result["totals"]}
    text = usage_api["format_usage"](result)
    assert "No requests in this window (source available)" in text
    assert "TOTAL" in text
    assert "Unavailable" not in text


def test_wrong_response_window_is_rejected(usage_api):
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](valid_result(), days=30, by="container")


@pytest.mark.parametrize("args", [["--days", "0"], ["--days", "7.5"], ["--by", "user"]])
def test_invalid_usage_parameters(usage_api, args):
    with pytest.raises(SystemExit) as error:
        usage_api["build_parser"]().parse_args(["usage", *args])
    assert error.value.code == 2


@pytest.mark.parametrize("elapsed,known", [(0.2, True), (1.2, False)])
def test_fixture_scheduling_delay_preserves_production_deadline(usage_service, monkeypatch, elapsed, known):
    def delayed_clock():
        ticks = iter([0.0])
        return SimpleNamespace(time=time.time, monotonic=lambda: next(ticks, elapsed))

    # Simulate descheduling after connection setup, without sleeping or changing
    # process-global time. The real production default must still fail closed.
    with monkeypatch.context() as patch:
        patch.setattr(activity_module, "time", delayed_clock())
        production = ActivityReader(usage_service.database).usage(days=30, by="model")
    assert production["known"] is False
    assert production["error"] == "activity read deadline exceeded"
    assert all(value is None for value in production["totals"].values())

    with monkeypatch.context() as patch:
        patch.setattr(activity_module, "time", delayed_clock())
        result = usage_service.backend(days=30, by="model")
    assert result["known"] is known, result
    if known:
        assert result["totals"] == {"requests": 3, "input_tokens": 118, "output_tokens": 225}
    else:
        assert result["error"] == "activity read deadline exceeded"
        assert all(value is None for value in result["totals"].values())
