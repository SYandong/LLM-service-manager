import json
import os
import pytest
import vllm_service.process as process_mod


@pytest.fixture(autouse=True)
def patch_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(process_mod, "PID_FILE", tmp_path / "vllm.pid")


def _write_pid_file(pid, start_time):
    process_mod.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    process_mod.PID_FILE.write_text(json.dumps({"pid": pid, "start_time": start_time}))


def test_not_running_without_pid_file():
    running, pid = process_mod.is_running()
    assert not running
    assert pid is None


def test_running_with_current_process():
    pid = os.getpid()
    start_time = process_mod._get_start_time(pid)
    _write_pid_file(pid, start_time)
    running, result_pid = process_mod.is_running()
    assert running
    assert result_pid == pid


def test_stale_pid_wrong_start_time():
    pid = os.getpid()
    _write_pid_file(pid, start_time=0)
    running, _ = process_mod.is_running()
    assert not running
    assert not process_mod.PID_FILE.exists()


def test_stale_pid_nonexistent_process():
    _write_pid_file(pid=999999999, start_time=12345)
    running, _ = process_mod.is_running()
    assert not running
    assert not process_mod.PID_FILE.exists()
