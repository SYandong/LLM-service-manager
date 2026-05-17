import json
import os
import pytest
import vllm_service.process as process_mod


@pytest.fixture(autouse=True)
def patch_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(process_mod, "PID_FILE", tmp_path / "vllm.pid")


def _write_pid_file(pid, start_time, model=None):
    process_mod.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "start_time": start_time}
    if model is not None:
        data["model"] = model
    process_mod.PID_FILE.write_text(json.dumps(data))


def test_not_running_without_pid_file():
    running, pid = process_mod.is_running()
    assert not running
    assert pid is None


def test_running_with_matching_start_time(monkeypatch):
    pid = 123
    start_time = 456
    monkeypatch.setattr(process_mod, "_get_start_time", lambda value: start_time)
    _write_pid_file(pid, start_time, model="Qwen/Qwen3-4B-Instruct-2507")
    running, result_pid = process_mod.is_running()
    assert running
    assert result_pid == pid
    assert process_mod.read_metadata()["model"] == "Qwen/Qwen3-4B-Instruct-2507"


def test_stale_pid_wrong_start_time(monkeypatch):
    pid = 123
    monkeypatch.setattr(process_mod, "_get_start_time", lambda value: 456)
    _write_pid_file(pid, start_time=0)
    running, _ = process_mod.is_running()
    assert not running
    assert not process_mod.PID_FILE.exists()


def test_stale_pid_nonexistent_process():
    _write_pid_file(pid=999999999, start_time=12345)
    running, _ = process_mod.is_running()
    assert not running
    assert not process_mod.PID_FILE.exists()


def test_missing_start_time_is_not_running(monkeypatch):
    monkeypatch.setattr(process_mod, "_get_start_time", lambda value: None)
    _write_pid_file(pid=123, start_time=None)

    running, _ = process_mod.is_running()

    assert not running
    assert not process_mod.PID_FILE.exists()
