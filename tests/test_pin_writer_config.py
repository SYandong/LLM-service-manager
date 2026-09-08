# Generated-By: Codex / gpt-6-astra
"""Explicit write opt-in and canonical source mapping checks."""

from dataclasses import replace
import subprocess
import sys
import threading

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.store import IntentStore


def test_default_is_readonly_and_live_mode_requires_database(tmp_path):
    config = SchedulerConfig("127.0.0.1", 8011)
    assert config.read_only
    with pytest.raises(ValueError, match="requires state_db_path"):
        replace(config, read_only=False)
    assert replace(config, read_only=False, state_db_path=str(tmp_path / "state.sqlite")).read_only is False


def test_ipv4_mapped_peer_uses_same_configured_owner():
    config = SchedulerConfig("127.0.0.1", 8011, collectors={"ip_containers": {"127.0.0.1": "container"}})
    assert config.owner_for_ip("::ffff:127.0.0.1") == "container"
    assert config.owner_for_ip("::1") == "ip:::1"


@pytest.mark.parametrize("mapping", [{"not-an-ip": "owner"}, {"127.0.0.1": ""},
    {"127.0.0.1": 1}, {"127.0.0.1": "one", "::ffff:127.0.0.1": "two"}, []])
def test_invalid_source_mappings_rejected(mapping):
    with pytest.raises(ValueError):
        SchedulerConfig("127.0.0.1", 8011, collectors={"ip_containers": mapping})


@pytest.mark.parametrize("flags", [["--dry-run", "--once"], ["--once"], ["--check-config"]])
def test_read_commands_never_create_database_despite_live_config(tmp_path, flags):
    path = tmp_path / "state.sqlite"
    config = tmp_path / "scheduler.yaml"
    config.write_text(f"listen_host: 127.0.0.1\nlisten_port: 8011\nread_only: false\nstate_db_path: {path}\n")
    result = subprocess.run([sys.executable, "-m", "llmsvc", "--config", str(config), *flags],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert not path.exists()


def test_dryrun_live_config_reads_existing_store_without_schema_write(tmp_path):
    import json
    path = tmp_path / "state.sqlite"
    IntentStore(path, action_lock=threading.RLock()).close()
    before = path.read_bytes()
    config = tmp_path / "scheduler.yaml"
    config.write_text(f"listen_host: 127.0.0.1\nlisten_port: 8011\nread_only: false\nstate_db_path: {path}\n")
    result = subprocess.run([sys.executable, "-m", "llmsvc", "--config", str(config), "--dry-run", "--once"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["read_only"] is True
    assert path.read_bytes() == before


def test_standalone_dryrun_flag_overrides_live_config_for_http(tmp_path):
    import http.client
    import json
    import socket
    import time
    path = tmp_path / "state.sqlite"
    IntentStore(path, action_lock=threading.RLock()).close()
    before = path.read_bytes()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "scheduler.yaml"
    config.write_text(f"listen_host: 127.0.0.1\nlisten_port: {port}\nread_only: false\nstate_db_path: {path}\n")
    process = subprocess.Popen([sys.executable, "-m", "llmsvc", "--config", str(config), "--dry-run"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
            raise AssertionError("dry-run daemon did not start")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("POST", "/v1/pin", body=json.dumps({"model": "x", "until": 4102444800}))
            response = connection.getresponse()
            assert response.status == 405
            assert json.loads(response.read())["error"] == "read_only"
        finally:
            connection.close()
        process.terminate()
        process.communicate(timeout=3)
        assert process.returncode == 0
        assert path.read_bytes() == before
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


def test_explicit_normal_startup_opens_writable_store(tmp_path, monkeypatch):
    import sqlite3
    import llmsvc.__main__ as entry
    database = tmp_path / "new.sqlite"
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, state_db_path=str(database))
    monkeypatch.setattr(entry, "load_config", lambda path: config)
    monkeypatch.setattr("sys.argv", ["llmsvc", "--config", "unused"])
    def stop_before_serving(*args):
        raise OSError("test stops before serving")
    monkeypatch.setattr(entry, "SchedulerHTTPServer", stop_before_serving)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 2
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM llmsvc_pins").fetchone() == (0,)
