# Generated-By: OpenCode / deepseek-v4.1-flash
"""Actionable failure detail for maintenance adapter, reload and cycle errors."""

import json
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.maintenance import CommandBackend, MaintenanceError
from llmsvc.reload import QuietPeriod, ReloadError, ReloadQueue, describe
from llmsvc.scheduler import Scheduler
from test_reload import Clock, snapshot
from test_reload_maintenance import enqueue, setup


@pytest.fixture
def _harness(tmp_path):
    clock = Clock()
    quiet = QuietPeriod(clock)
    path = tmp_path / "config.yaml"
    path.write_bytes(b"models: {}\n")
    path.chmod(0o640)
    calls, logs = [], []
    queue = ReloadQueue(path, action_lock=threading.RLock(), quiet=quiet,
                        snapshot=lambda: snapshot(clock),
                        validate=lambda p: calls.append(("validate", p.read_bytes())),
                        notify_reload=lambda **kwargs: calls.append(("reload", path.read_bytes())),
                        log=logs.append, clock=clock, wall_clock=clock)
    return queue, quiet, clock, calls, logs


def adapter(tmp_path, body):
    script = tmp_path / "adapter.py"
    script.write_text(body)
    return CommandBackend([sys.executable, str(script)])


def test_adapter_failure_message_and_last_failure_carry_stderr(tmp_path):
    backend = adapter(tmp_path, "import json,sys\n"
                                "print(json.dumps({'error':'native_actor_unattributed'}), file=sys.stderr)\n"
                                "sys.exit(1)\n")
    with pytest.raises(MaintenanceError) as info:
        backend.request("inspect", {"transaction_id": "test"}, deadline=time.monotonic() + 5)
    message = str(info.value)
    assert "operation=inspect" in message
    assert "exit=1" in message
    assert "native_actor_unattributed" in message
    assert backend.last_failure["operation"] == "inspect"
    assert backend.last_failure["exit_code"] == 1
    assert "native_actor_unattributed" in backend.last_failure["stderr_tail"]
    assert isinstance(backend.last_failure["at"], float)


def test_adapter_message_keeps_only_a_bounded_stderr_tail(tmp_path):
    backend = adapter(tmp_path, "import sys\n"
                                "sys.stderr.write('BEGIN' + 'x' * 10000 + 'END')\n"
                                "sys.exit(1)\n")
    with pytest.raises(MaintenanceError) as info:
        backend.request("inspect", {"transaction_id": "test"}, deadline=time.monotonic() + 5)
    message = str(info.value)
    assert len(message) < 1024
    assert "END" in message
    assert "BEGIN" not in message


def test_adapter_success_leaves_no_last_failure(tmp_path):
    backend = adapter(tmp_path, "import json,sys\n"
                                "request=json.load(sys.stdin)\n"
                                "print(json.dumps({'request_id':request['request_id'],\n"
                                "                  'transaction_id':request['context'].get('transaction_id'),\n"
                                "                  'accepted':True}))\n")
    result = backend.request("inspect", {"transaction_id": "test"}, deadline=time.monotonic() + 5)
    assert result["accepted"] is True and result["transaction_id"] == "test"
    assert backend.last_failure is None


def test_adapter_wrong_request_id_reports_unbound_and_exit_zero(tmp_path):
    backend = adapter(tmp_path, "import json,sys\n"
                                "request=json.load(sys.stdin)\n"
                                "print(json.dumps({'request_id':'stale-request-id',\n"
                                "                  'transaction_id':request['context'].get('transaction_id')}))\n")
    with pytest.raises(MaintenanceError) as info:
        backend.request("inspect", {"transaction_id": "test"}, deadline=time.monotonic() + 5)
    message = str(info.value)
    assert "unbound or late" in message
    assert "exit=0" in message
    assert backend.last_failure["exit_code"] == 0


def test_describe_renders_message_only():
    assert describe(ValueError("boom")) == "boom"


def test_describe_follows_cause_chain():
    mid = RuntimeError("mid")
    mid.__cause__ = ValueError("root")
    assert describe(mid) == "mid <- ValueError: root"


def test_describe_caps_depth():
    exc = ValueError("0")
    for index in range(1, 6):
        nxt = RuntimeError(str(index))
        nxt.__cause__ = exc
        exc = nxt
    text = describe(exc)
    assert text.count(" <- ") == 3
    assert "RuntimeError: 4" in text and "RuntimeError: 2" in text
    assert "RuntimeError: 1" not in text and "ValueError: 0" not in text


def test_describe_caps_length():
    assert len(describe(ValueError("x" * 2000))) == 600


def test_describe_strips_control_characters():
    assert describe(ValueError("a\x00b\nc\td")) == "abcd"


def test_reload_job_error_includes_wrapped_cause(_harness):
    context = setup(_harness)
    queue, adapter = context[0], context[6]

    def fail(job, raw, *, deadline):
        raise MaintenanceError(
            "maintenance adapter failed: operation=stop_old exit=1 stderr='native_actor_unattributed'"
        ) from ReloadError("native actor is unattributed")

    adapter.after_replace = fail
    enqueue(context)
    result = queue.process_once()
    assert result["status"] == "reconciliation_required"
    assert "maintenance adapter failed" in result["error"]
    assert "native actor is unattributed" in result["error"]
    assert "ReloadError" in result["error"]
    logged = [row for row in _harness[4] if row.get("kind") == "config_change_result"]
    assert "native actor is unattributed" in logged[-1]["error"]


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def scheduler_with_catalog(clock, exc):
    scheduler = Scheduler(SchedulerConfig(listen_host="127.0.0.1", listen_port=8011), monotonic=clock)
    state = {"exc": exc}

    class Catalog:
        def can_submit(self):
            return True

        def process_once(self):
            raise state["exc"]

    scheduler.catalog = Catalog()
    return scheduler, state


def cycle_lines(caplog):
    return [json.loads(record.getMessage()) for record in caplog.records
            if record.name == "llmsvc.scheduler" and record.levelno == logging.WARNING]


def test_catalog_cycle_error_logs_detail_and_is_rate_limited(caplog):
    clock = FakeClock()
    scheduler, state = scheduler_with_catalog(clock, ReloadError("catalog boom"))
    caplog.set_level(logging.WARNING, logger="llmsvc.scheduler")
    scheduler._catalog_cycle()
    clock.advance(30)
    scheduler._catalog_cycle()
    clock.advance(31)
    scheduler._catalog_cycle()
    lines = [row for row in cycle_lines(caplog) if row.get("kind") == "catalog_cycle_error"]
    assert len(lines) == 2
    assert lines[0]["error_type"] == "ReloadError" and lines[0]["error"] == "catalog boom"
    assert lines[0]["repeats"] == 0
    assert lines[1]["repeats"] == 1
    state["exc"] = ReloadError("second boom")
    scheduler._catalog_cycle()
    lines = [row for row in cycle_lines(caplog) if row.get("kind") == "catalog_cycle_error"]
    assert len(lines) == 3
    assert lines[2]["error"] == "second boom" and lines[2]["repeats"] == 0


def test_fault_cycle_error_logs_detail_and_is_rate_limited(caplog):
    clock = FakeClock()
    scheduler = Scheduler(SchedulerConfig(listen_host="127.0.0.1", listen_port=8011), monotonic=clock)
    scheduler.faults = SimpleNamespace(run_once=lambda: (_ for _ in ()).throw(ReloadError("fault boom")))
    caplog.set_level(logging.WARNING, logger="llmsvc.scheduler")
    scheduler._fault_cycle()
    scheduler._fault_cycle()
    lines = [row for row in cycle_lines(caplog) if row.get("kind") == "fault_error"]
    assert len(lines) == 1
    assert lines[0]["error_type"] == "ReloadError" and lines[0]["error"] == "fault boom"
    assert lines[0]["repeats"] == 0


def test_fault_and_catalog_errors_are_limited_per_kind(caplog):
    clock = FakeClock()
    scheduler, state = scheduler_with_catalog(clock, ReloadError("catalog boom"))
    scheduler.faults = SimpleNamespace(run_once=lambda: (_ for _ in ()).throw(ReloadError("fault boom")))
    caplog.set_level(logging.WARNING, logger="llmsvc.scheduler")
    for _ in range(3):
        scheduler._fault_cycle()
        scheduler._catalog_cycle()
    faults = [row for row in cycle_lines(caplog) if row.get("kind") == "fault_error"]
    catalogs = [row for row in cycle_lines(caplog) if row.get("kind") == "catalog_cycle_error"]
    assert len(faults) == 1 and faults[0]["error"] == "fault boom"
    assert len(catalogs) == 1 and catalogs[0]["error"] == "catalog boom"
