# Generated-By: Codex / gpt-6-astra
"""Pin clients against opt-in, disposable core HTTP/SQLite; no model actuator."""

import json
import runpy
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import IntentWriteError, Scheduler
from llmsvc.server import SchedulerHTTPServer, SchedulerHandler
from llmsvc.state import ModelState, StateSnapshot
from llmsvc.store import IntentStore


@pytest.fixture
def pin_api():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))


@pytest.fixture
def pin_service(tmp_path):
    database = tmp_path / "intent.sqlite"
    names = ["model", "org/a b?#%/模型", "literal%2Fmodel", "-leading-dash"]
    config = SchedulerConfig("127.0.0.1", 1, read_only=False, state_db_path=str(database),
                             collectors={"ip_containers": {"127.0.0.1": "actual-owner"}})
    store = IntentStore(database, action_lock=threading.RLock())
    observed = StateSnapshot(sampled_at=time.time(), models=tuple(ModelState(name) for name in names))
    scheduler = Scheduler(config, lambda: observed, store=store)
    scheduler.sample_once()  # The collector is a pure fixture; no external probes.
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    requests = []

    class TrackingHandler(SchedulerHandler):
        def do_GET(self):
            requests.append(("GET", self.path))
            super().do_GET()

        def do_POST(self):
            requests.append(("POST", self.path))
            super().do_POST()

        def do_DELETE(self):
            requests.append(("DELETE", self.path, int(self.headers.get("Content-Length", "0"))))
            super().do_DELETE()

    server.RequestHandlerClass = TrackingHandler
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield SimpleNamespace(url="http://127.0.0.1:%s" % server.server_port,
                              scheduler=scheduler, store=store, database=database,
                              requests=requests, names=names)
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        store.close()


def command(api, *args):
    return api["build_parser"]().parse_args(list(args))


@pytest.mark.parametrize("duration,seconds", [("8h", 28800), ("30m", 1800), ("1.5h", 5400), ("2d", 172800), ("1s", 1)])
def test_required_duration_parses_to_seconds(pin_api, duration, seconds):
    assert command(pin_api, "pin", "model", "--for", duration).duration == seconds


@pytest.mark.parametrize("args", [
    ["pin", "model"], ["pin", "model", "--for", "0h"], ["pin", "model", "--for", "-1h"],
    ["pin", "model", "--for", "nan"], ["pin", "model", "--for", "1e309h"],
    ["pin", "model", "--for", "8"], ["unpin", ""], ["unpin", "x\nheader"],
    ["reserve"], ["add", "/tmp/model"], ["rm", "model"],
])
def test_invalid_or_unavailable_commands_never_execute(pin_api, args):
    with pytest.raises(SystemExit) as exc:
        command(pin_api, *args)
    assert exc.value.code == 2


@pytest.mark.parametrize("name", ["model", "org/a b?#%/模型", "literal%2Fmodel", "-leading-dash"])
def test_live_pin_owner_and_url_safe_empty_body_unpin(pin_api, pin_service, monkeypatch, name):
    monkeypatch.setitem(pin_api["execute_command"].__globals__, "PIN_COMPATIBILITY_LABEL", "spoof-owner")
    client = pin_api["SchedulerClient"](pin_service.url)
    now = time.time()
    args = command(pin_api, "pin", "--for", "1h", "--", name)
    result = pin_api["execute_command"](args, client, now=now)
    assert result == {"model": name, "until": now + 3600, "by": "actual-owner"}
    assert pin_service.scheduler.snapshot().pins[0].by == "actual-owner"
    text = pin_api["format_result"](args, result)
    assert "actual-owner" in text and "spoof-owner" not in text
    for _ in range(2):
        args = command(pin_api, "unpin", "--", name)
        result = pin_api["execute_command"](args, client)
        assert result == {"model": name, "by": "actual-owner"}
    assert pin_service.requests[-1] == ("DELETE", "/v1/pin/" + quote(name, safe=""), 0)
    assert not pin_service.scheduler.snapshot().pins


def test_unmapped_transport_owner_is_not_invented(pin_api, pin_service):
    pin_service.scheduler.config = replace(pin_service.scheduler.config, collectors={})
    args = command(pin_api, "pin", "model", "--for", "1h")
    result = pin_api["execute_command"](args, pin_api["SchedulerClient"](pin_service.url))
    assert result["by"] == "ip:127.0.0.1"


@pytest.mark.parametrize("operation", ["pin", "unpin"])
def test_dry_run_is_writer_free_in_read_only_mode(pin_api, pin_service, monkeypatch, operation):
    client = pin_api["SchedulerClient"](pin_service.url)
    pin_api["execute_command"](command(pin_api, "pin", "model", "--for", "1h"), client)
    pin_service.scheduler.config = replace(pin_service.scheduler.config, read_only=True)
    before = pin_service.database.read_bytes()
    snapshot = pin_service.scheduler.snapshot().to_dict()
    events = pin_service.scheduler.events_since(0)

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run called a store writer")

    monkeypatch.setattr(pin_service.store, "put_pin", forbidden)
    monkeypatch.setattr(pin_service.store, "remove_pin", forbidden)
    argv = [operation, "model", "--dry-run"] + (["--for", "1h"] if operation == "pin" else [])
    args = command(pin_api, *argv)
    result = pin_api["execute_command"](args, client)
    assert result["would"][0]["kind"] == operation
    assert "hypothetical" in pin_api["format_result"](args, result)
    assert pin_service.database.read_bytes() == before
    assert pin_service.scheduler.snapshot().to_dict() == snapshot
    assert pin_service.scheduler.events_since(0) == events


def test_readonly_and_disabled_errors_have_no_fallback_write(pin_api, pin_service, monkeypatch):
    client = pin_api["SchedulerClient"](pin_service.url)
    args = command(pin_api, "pin", "model", "--for", "1h")
    pin_service.scheduler.config = replace(pin_service.scheduler.config, read_only=True)
    before = pin_service.database.read_bytes()
    with pytest.raises(pin_api["ClientError"], match="read_only"):
        pin_api["execute_command"](args, client)
    pin_service.scheduler.config = replace(pin_service.scheduler.config, read_only=False)

    def disabled(*args, **kwargs):
        raise IntentWriteError(405, "operation_not_enabled")

    monkeypatch.setattr(pin_service.scheduler, "write_pin", disabled)
    with pytest.raises(pin_api["ClientError"], match="operation_not_enabled"):
        pin_api["execute_command"](args, client)
    assert len(pin_service.requests) == 2
    assert pin_service.database.read_bytes() == before


def test_copied_cli_pin_and_unpin(pin_service, tmp_path):
    script = tmp_path / "llm"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cli" / "llm", script)
    base = [sys.executable, "-I", "-S", str(script), "--url", pin_service.url,
            "--config", str(tmp_path / "missing")]
    result = subprocess.run([*base, "pin", "model", "--for", "1h", "--json"],
                            cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["by"] == "actual-owner"
    result = subprocess.run([*base, "unpin", "model"], cwd=tmp_path,
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "actor actual-owner" in result.stdout
    assert not pin_service.scheduler.snapshot().pins


def test_blocked_preview_is_explicit_and_has_no_write(pin_api, pin_service, capsys):
    pin_service.scheduler._snapshot = replace(pin_service.scheduler._snapshot, errors=("stale_snapshot",))
    before = pin_service.database.read_bytes()
    code = pin_api["main"](["--url", pin_service.url, "pin", "model", "--for", "1h", "--dry-run"])
    assert code == 1
    assert "stale_snapshot" in capsys.readouterr().out
    assert pin_service.database.read_bytes() == before


def test_unrepresentable_expiry_rejected_before_request(pin_api, pin_service):
    args = command(pin_api, "pin", "model", "--for", "999999999d")
    with pytest.raises(pin_api["ClientError"], match="UTC range"):
        pin_api["execute_command"](args, pin_api["SchedulerClient"](pin_service.url))
    assert pin_service.requests == []
