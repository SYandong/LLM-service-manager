# Generated-By: Codex / gpt-6-astra
"""The copied CLI must remain usable without an installed project or extras."""

import ast
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot


CLI = Path(__file__).resolve().parents[1] / "cli" / "llm"


@pytest.fixture
def llm():
    return runpy.run_path(str(CLI))


@pytest.fixture
def snapshot():
    return json.loads(json.dumps(StateSnapshot(
        sampled_at=1800000000,
        gpus=(GPUState(index=0, total_gb=144, used_gb=87, free_gb=57,
                       managed_gb=77, external_gb=10), GPUState(index=1)),
        memory=MemoryState(host_available_gb=823, sleeping_weights_gb=86),
        models=(
            ModelState(name="default-model", state="sleeping", gpu=0,
                       resident_gb=1.6, is_default=True),
            ModelState(name="research-model", state="awake", gpu=0, resident_gb=73),
            ModelState(name="cold-model", state="stopped", cold_start_seconds=210),
            ModelState(name="unknown-model"),
        ),
        activity=(
            Activity(model="default-model", last_request_at=1799998500,
                     requests_last_10m=0, by=("ctr-a",)),
            Activity(model="research-model", last_request_at=1799999940,
                     requests_last_10m=12, by=("ctr-b",)),
        ),
        pins=(Pin(model="research-model", until=1800003600, by="ctr-b"),),
        errors=("GPU1 probe unavailable",),
    ).to_dict()))


@pytest.fixture
def server(snapshot):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s" % httpd.server_port, paths
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_config_file_and_environment_precedence(llm, tmp_path):
    config = tmp_path / "config"
    config.write_text("# scheduler connection\nurl = http://scheduler:8011/\ntimeout = 2.5\n")
    result = llm["load_config"](environ={}, path=config)
    assert result == {"url": "http://scheduler:8011", "timeout": 2.5}
    result = llm["load_config"](
        environ={"LLM_URL": "https://alternate:9011", "LLM_TIMEOUT": "4"}, path=config
    )
    assert result == {"url": "https://alternate:9011", "timeout": 4.0}


@pytest.mark.parametrize("url", ["file:///etc/passwd", "localhost:8011", "http://user:secret@host", "http://host?query=1"])
def test_config_rejects_invalid_scheduler_addresses(llm, tmp_path, url):
    with pytest.raises(llm["ClientError"]):
        llm["load_config"](environ={"LLM_URL": url}, path=tmp_path / "missing")


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "word"])
def test_config_rejects_invalid_timeouts(llm, tmp_path, timeout):
    with pytest.raises(llm["ClientError"]):
        llm["load_config"](
            environ={"LLM_URL": "http://scheduler:8011", "LLM_TIMEOUT": timeout},
            path=tmp_path / "missing",
        )


def test_missing_address_is_actionable(llm, tmp_path):
    with pytest.raises(llm["ClientError"], match="LLM_URL"):
        llm["load_config"](environ={}, path=tmp_path / "missing")


def test_http_request_uses_timeout_and_preserves_json(llm):
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(b'{"models": [], "unavailable": null}')

    client = llm["SchedulerClient"]("http://scheduler:8011", timeout=2, opener=open_request)
    assert client.request("GET", "/v1/state") == {"models": [], "unavailable": None}
    request, timeout = calls[0]
    assert request.full_url == "http://scheduler:8011/v1/state"
    assert request.get_method() == "GET"
    assert timeout == 2


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"null", b"\xff", b'{"value":NaN}', b'{"value":Infinity}'])
def test_http_invalid_response_has_no_traceback(llm, payload):
    client = llm["SchedulerClient"](
        "http://scheduler:8011", opener=lambda *args, **kwargs: io.BytesIO(payload)
    )
    with pytest.raises(llm["ClientError"], match="response"):
        client.request("GET", "/v1/state")


def test_http_error_keeps_server_explanation(llm):
    def fail(*args, **kwargs):
        raise HTTPError("http://scheduler", 503, "Unavailable", {}, io.BytesIO(b'{"error":"snapshot unavailable"}'))

    client = llm["SchedulerClient"]("http://scheduler:8011", opener=fail)
    with pytest.raises(llm["ClientError"], match="503.*snapshot unavailable"):
        client.request("GET", "/v1/state")


def test_network_error_is_actionable(llm):
    def fail(*args, **kwargs):
        raise URLError("connection refused")

    client = llm["SchedulerClient"]("http://scheduler:8011", opener=fail)
    with pytest.raises(llm["ClientError"], match="connection refused"):
        client.request("GET", "/v1/state")


def test_python_310_and_no_third_party_imports():
    tree = ast.parse(CLI.read_text(), feature_version=(3, 10))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module.split(".")[0])
    assert imports <= sys.stdlib_module_names


def test_copy_alone_help_in_isolated_python(tmp_path):
    script = tmp_path / "llm"
    shutil.copyfile(CLI, script)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--help"],
        cwd=tmp_path, text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "status" in result.stdout
    assert "Traceback" not in result.stderr


def test_state_contract_display(llm, snapshot):
    text = llm["format_status"](snapshot)
    for expected in ["87/144 GiB", "llmsvc 77  external 10  free 57", "86/200 GiB",
                     "823 GiB", "default-model *", "sleeping", "25m", "12", "ctr-b",
                     "cold start estimate: 3.5m", "unknown-model", "WARNING GPU1 probe unavailable"]:
        assert expected in text
    assert "GPU1  ?/? GiB  llmsvc ?  external ?  free ?" in text
    assert "01-15 09:00Z (ctr-b)" in text
    assert len(text.splitlines()) <= 30


@pytest.mark.parametrize("width", [1, 20, 40, 60, 80, 99, 100, 140])
def test_terminal_width_and_untrusted_names(llm, snapshot, width):
    snapshot["models"][0]["name"] = "模型\x1b[2J\r\n" * 30
    text = llm["format_status"](snapshot, width=width)
    assert all(llm["cell_width"](line) <= width for line in text.splitlines())
    assert "\x1b" not in text
    assert "\r" not in text


def test_narrow_output_keeps_activity_and_pin_details(llm, snapshot):
    text = llm["format_status"](snapshot, width=80)
    assert "used 1m  10m 12  from ctr-b  pin 01-15 09:00Z (ctr-b)" in text


@pytest.mark.parametrize("width", [40, 80, 100])
def test_long_default_name_keeps_protection_marker(llm, snapshot, width):
    snapshot["models"][0]["name"] = "very-long-default-model-name-" * 4
    text = llm["format_status"](snapshot, width=width)
    row = next(line for line in text.splitlines() if "sleeping" in line)
    assert "~ *" in row


def test_importable_module_is_same_source():
    assert (CLI.parent / "llm.py").resolve() == CLI.resolve()
    module = runpy.run_path(str(CLI.parent / "llm.py"))
    assert callable(module["build_parser"])
    assert callable(module["SchedulerClient"])


@pytest.mark.parametrize("json_output", [False, True])
def test_copy_alone_status_over_http(tmp_path, server, snapshot, json_output):
    script = tmp_path / "llm"
    shutil.copyfile(CLI, script)
    address, paths = server
    env = dict(os.environ, LLM_URL=address, LLM_CONFIG=str(tmp_path / "missing"), COLUMNS="80")
    command = [sys.executable, "-I", "-S", str(script), "status"]
    if json_output:
        command.append("--json")
    result = subprocess.run(command, cwd=tmp_path, env=env, text=True,
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    if json_output:
        assert json.loads(result.stdout) == snapshot
    else:
        assert "research-model" in result.stdout
        assert max(map(len, result.stdout.splitlines())) <= 80
    assert paths == ["/v1/state"]


def test_python_310_copy_alone_status(tmp_path, server):
    python = shutil.which("python3.10")
    if python is None:
        pytest.skip("Python 3.10 executable unavailable; covered by Python 3.10 CI")
    script = tmp_path / "llm"
    shutil.copyfile(CLI, script)
    address, paths = server
    result = subprocess.run(
        [python, "-I", "-S", str(script), "--config", str(tmp_path / "missing"),
         "--url", address, "status"],
        cwd=tmp_path, text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "default-model" in result.stdout
    assert paths == ["/v1/state"]


def test_non_tty_without_command_uses_status(tmp_path, server):
    address, paths = server
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(CLI), "--config", str(tmp_path / "missing"), "--url", address],
        cwd=tmp_path, text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "research-model" in result.stdout
    assert paths == ["/v1/state"]


def test_invalid_state_is_reported_without_traceback(llm, monkeypatch, capsys):
    class BadClient:
        def __init__(self, **kwargs):
            pass

        def request(self, *args):
            return {"gpus": [None]}

    monkeypatch.setitem(llm["main"].__globals__, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(llm["main"].__globals__, "SchedulerClient", BadClient)
    assert llm["main"](["status"]) == 1
    assert "Invalid scheduler state response" in capsys.readouterr().err


def test_active_reservations_leases_and_blockers(llm, snapshot):
    snapshot["reserves"] = [
        {"id": "active", "gpu": 1, "size_gb": 80, "until": 1800003600, "by": "ctr-c"},
        {"id": "expired", "gpu": 1, "size_gb": 80, "until": 1799999999, "by": "ctr-c"},
    ]
    snapshot["leases"] = [
        {"model": "pending-model", "gpu": 0, "budget_gb": 70, "status": "stale", "expires_at": 1800000300},
        {"model": "released-model", "status": "released"},
        {"model": "confirmed-model", "status": "confirmed"},
    ]
    snapshot["blocked_by"] = [{"model": "research-model", "reason": "pinned"}]
    text = llm["format_status"](snapshot)
    assert "RESERVE active GPU1 80 GiB" in text
    assert "LEASE pending-model GPU0 70 GiB stale" in text
    assert "BLOCKED research-model: pinned" in text
    assert "expired" not in text
    assert "released-model" not in text
    assert "confirmed-model" not in text
