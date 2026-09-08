# Generated-By: Codex / gpt-6-astra
"""Daemon lifecycle checks using only an ephemeral local server."""

import http.client
import json
import socket
import subprocess
import sys
import time


def test_once_keeps_stdout_json_and_logs_structured_events():
    result = subprocess.run(
        [sys.executable, "-m", "llmsvc", "--config", "tests/fixtures/core_scheduler.yaml", "--dry-run", "--once"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["read_only"] is True
    assert json.loads(result.stderr)["kind"] == "state"


def test_invalid_yaml_exits_with_config_error(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("listen_host: [invalid\n")
    result = subprocess.run([sys.executable, "-m", "llmsvc", "--config", str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "invalid scheduler YAML" in result.stderr
    assert "Traceback" not in result.stderr


def test_sigterm_stops_daemon_cleanly_with_sse_subscriber(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    path = tmp_path / "scheduler.yaml"
    path.write_text(f"listen_host: 127.0.0.1\nlisten_port: {port}\nsample_interval_seconds: 0.05\n")
    process = subprocess.Popen([sys.executable, "-m", "llmsvc", "--config", str(path), "--dry-run"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    connection = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
            try:
                connection.request("GET", "/v1/state")
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read())["read_only"] is True
                break
            except (ConnectionRefusedError, TimeoutError):
                assert process.poll() is None
                time.sleep(0.02)
            finally:
                connection.close()
        else:
            raise AssertionError("daemon did not become ready")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/v1/events")
        response = connection.getresponse()
        assert response.status == 200
        process.terminate()
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr
        assert not stdout
        assert all(isinstance(json.loads(line), dict) for line in stderr.splitlines())
    finally:
        if connection:
            connection.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)
