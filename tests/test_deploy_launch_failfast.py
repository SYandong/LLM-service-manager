# Generated-By: Codex / gpt-6.1-sol
"""A placement rejection terminates the waiting vllm-wrapper parent so llama-swap fails fast."""

import io
import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_deploy_launch import FakeHTTP, argv, load_launcher, write_config


def rejected_placement():
    return FakeHTTP([(409, {"error": "placement_timeout", "blockers": []})])


def parent_pidfd_spy(monkeypatch, launcher, kill):
    virtual_fd = 987654321
    original_close = launcher.os.close
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid, flags=0: virtual_fd, raising=False)
    monkeypatch.setattr(launcher.signal, "pidfd_send_signal",
                        lambda fd, sig: kill(launcher.os.getppid(), sig), raising=False)
    monkeypatch.setattr(launcher.os, "close", lambda fd: None if fd == virtual_fd else original_close(fd))


def run(tmp_path, monkeypatch, *, parent="vllm-wrapper", response=None, **overrides):
    launcher = load_launcher()
    config_path = write_config(tmp_path, **overrides)
    kills = []
    monkeypatch.setattr(launcher, "request_json", FakeHTTP([response]) if response else rejected_placement())
    monkeypatch.setattr(launcher, "run_checked", lambda command: launcher.subprocess.CompletedProcess(
        command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), ""))
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: parent)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    parent_pidfd_spy(monkeypatch, launcher, lambda pid, sig: kills.append((pid, sig)))
    return launcher.main(argv(config_path)), kills, launcher


def test_placement_rejection_terminates_a_waiting_wrapper(tmp_path, monkeypatch):
    code, kills, launcher = run(tmp_path, monkeypatch)
    assert code == 75
    assert kills == [(launcher.os.getppid(), signal.SIGTERM)]


def test_unavailable_marker_is_bounded_and_flushed_before_fail_fast(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)

    class FlushedOutput(io.StringIO):
        flushed = ""

        def flush(self):
            self.flushed = self.getvalue()

    output = FlushedOutput()
    kills = []
    monkeypatch.setattr(launcher.sys, "stderr", output)
    monkeypatch.setattr(launcher, "request_json", FakeHTTP([(503, {
        "error": "no_feasible_gpu", "retry_after_seconds": 17,
        "message": "Placement GPUs are occupied",
        "gpus": [{"index": 0, "external_gb": 60.4, "free_gb": 82.7}],
    })]))
    monkeypatch.setattr(launcher, "run_checked", lambda command: launcher.subprocess.CompletedProcess(
        command, 0, "not-found\n", ""))
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: "vllm-wrapper")

    def kill(pid, sig):
        markers = [json.loads(line) for line in output.flushed.splitlines()
                   if json.loads(line).get("event") == "llmsvc_unavailable"]
        assert len(markers) == 1
        kills.append((pid, sig))

    monkeypatch.setattr(launcher.os, "kill", kill)
    parent_pidfd_spy(monkeypatch, launcher, kill)
    assert launcher.main(argv(config_path)) == 75
    lines = output.getvalue().splitlines(keepends=True)
    marker_line = next(line for line in lines if json.loads(line).get("event") == "llmsvc_unavailable")
    assert len(marker_line.encode("utf-8")) <= 4096
    assert json.loads(marker_line) == {
        "event": "llmsvc_unavailable", "status": 503, "code": "no_feasible_gpu",
        "retry_after": 17, "message": "Placement GPUs are occupied; GPU0: external 60.4 GiB, free 82.7 GiB",
    }
    assert kills == [(launcher.os.getppid(), signal.SIGTERM)]


@pytest.mark.parametrize("message", ["😀" * 5000, "a\n\"b\\c" * 2000, "\ud800" * 600])
def test_unavailable_marker_limits_unicode_and_escaped_messages(tmp_path, monkeypatch, capsys, message):
    code, kills, _ = run(tmp_path, monkeypatch, response=(503, {
        "error": "no_feasible_gpu", "retry_after_seconds": True, "message": message,
    }))
    assert code == 75 and kills
    output = capsys.readouterr()
    assert not output.out
    marker_line = next(line for line in output.err.splitlines(keepends=True)
                       if json.loads(line).get("event") == "llmsvc_unavailable")
    marker = json.loads(marker_line)
    assert len(marker_line.encode("utf-8")) <= 4096
    assert len(marker["message"]) <= 512
    assert marker["retry_after"] == 60


@pytest.mark.parametrize("response", [
    (409, {"error": "placement_timeout", "blockers": []}),
    (503, {"error": "placement_action_failed"}),
    (500, {"error": "no_feasible_gpu"}),
])
def test_other_placement_rejections_do_not_emit_marker(tmp_path, monkeypatch, capsys, response):
    code, kills, _ = run(tmp_path, monkeypatch, response=response)
    assert code == 75 and kills
    assert "llmsvc_unavailable" not in capsys.readouterr().err


def test_unavailable_dry_run_does_not_emit_marker_or_signal(tmp_path, monkeypatch, capsys):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    monkeypatch.setattr(launcher, "request_json", FakeHTTP([(503, {"error": "no_feasible_gpu"})]))
    monkeypatch.setattr(launcher.os, "kill", lambda *args: pytest.fail("dry-run signalled parent"))
    args = argv(config_path)
    args.insert(args.index("--"), "--dry-run")
    assert launcher.main(args) == 75
    assert "llmsvc_unavailable" not in capsys.readouterr().err


def test_unavailable_ignores_unusable_gpu_summaries(tmp_path, monkeypatch, capsys):
    code, kills, _ = run(tmp_path, monkeypatch, response=(503, {
        "error": "no_feasible_gpu", "retry_after_seconds": None, "message": {},
        "gpus": [{"index": True, "external_gb": 3},
                 {"index": 0, "external_gb": 2 ** 4096, "free_gb": "bad"},
                 {"index": 1, "external_gb": False, "free_gb": float("nan")},
                 {"index": 2, "external_gb": float("inf"), "free_gb": -1}],
    }))
    assert code == 75 and kills
    marker = next(json.loads(line) for line in capsys.readouterr().err.splitlines()
                  if json.loads(line).get("event") == "llmsvc_unavailable")
    assert marker["message"] == "No placement GPU is currently available"
    assert marker["retry_after"] == 60


@pytest.mark.parametrize("scenario", ["success", "nonleader_child", "changed_child", "changed_parent",
                                     "wrong_argv", "missing_pidfd"])
def test_unavailable_journal_cleanup_binds_only_verified_direct_child(monkeypatch, scenario):
    launcher = load_launcher()
    ppid, child, foreign = 400001, 400002, 400003
    signals = []
    opened = []
    reads = {ppid: 0, child: 0}
    paths = {
        f"/proc/{ppid}/cmdline": b'vllm-wrapper\0serve\0--journal-unit\0vllm-test.service\0--\0launcher\0',
        f"/proc/{ppid}/task/{ppid}/children": f"{child} {foreign}",
        f"/proc/{child}/cmdline": b'journalctl\0--user-unit=vllm-test.service\0--follow\0--lines=0\0--output=cat\0--no-pager\0',
        f"/proc/{foreign}/cmdline": b'journalctl\0--user-unit=other.service\0--follow\0--lines=0\0--output=cat\0--no-pager\0',
    }
    if scenario == "wrong_argv":
        paths[f"/proc/{child}/cmdline"] = paths[f"/proc/{foreign}/cmdline"]
    if scenario == "nonleader_child":
        paths[f"/proc/{ppid}/task/{ppid}/children"] = str(foreign)
        paths[f"/proc/{ppid}/task/{ppid + 1}/children"] = str(child)

    class ProcPath:
        def __init__(self, path):
            self.path = str(path)

        def read_bytes(self):
            return paths[self.path]

        def read_text(self, **kwargs):
            return paths[self.path]

        def stat(self):
            return SimpleNamespace(st_uid=launcher.os.geteuid())

        def glob(self, pattern):
            assert pattern == "*/children"
            return [ProcPath(path) for path in paths
                    if path.startswith(self.path + "/") and path.endswith("/children")]

    def identity(pid):
        if pid in reads:
            reads[pid] += 1
        if pid == ppid:
            return (1, "changed" if scenario == "changed_parent" and reads[pid] > 1 else "parent-start")
        if pid == child:
            return (ppid, "changed" if scenario == "changed_child" and opened else "child-start")
        return (ppid, "foreign-start")

    class Poll:
        calls = 0

        def register(self, fd, flags):
            assert fd == 900001

        def poll(self, timeout):
            self.calls += 1
            return [] if self.calls == 1 else [(900001, launcher.select.POLLIN)]

    monkeypatch.setattr(launcher, "Path", lambda path: ProcPath(path) if str(path).startswith("/proc/") else Path(path))
    monkeypatch.setattr(launcher, "proc_identity", identity)
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: "journalctl")
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda pid, flags=0: opened.append(pid) or 900001, raising=False)
    monkeypatch.setattr(launcher.signal, "pidfd_send_signal", lambda fd, sig: signals.append((fd, sig)), raising=False)
    original_close = launcher.os.close
    monkeypatch.setattr(launcher.os, "close", lambda fd: None if fd == 900001 else original_close(fd))
    monkeypatch.setattr(launcher.select, "poll", Poll)
    if scenario == "missing_pidfd":
        monkeypatch.delattr(launcher.os, "pidfd_open")
    launcher.stop_wrapper_journal(ppid)
    if scenario in ("success", "nonleader_child"):
        assert opened == [child]
        assert signals == [(900001, signal.SIGTERM), (900001, signal.SIGKILL)]
    else:
        assert not signals
        assert foreign not in opened


def test_pidfd_rejection_keeps_existing_parent_fail_fast(tmp_path, monkeypatch, capsys):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    kills = []
    monkeypatch.setattr(launcher, "request_json", FakeHTTP([(503, {"error": "no_feasible_gpu"})]))
    monkeypatch.setattr(launcher, "run_checked", lambda command: launcher.subprocess.CompletedProcess(command, 0, "not-found\n", ""))
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: "vllm-wrapper")
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    def denied(*args):
        raise PermissionError("pidfd unavailable")

    monkeypatch.setattr(launcher.os, "pidfd_open", denied, raising=False)
    monkeypatch.setattr(launcher.signal, "pidfd_send_signal", lambda *args: pytest.fail("unbound signal"), raising=False)
    assert launcher.main(argv(config_path)) == 75
    assert kills == [(launcher.os.getppid(), signal.SIGTERM)]
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(record["event"] == "llmsvc_unavailable" for record in records)


def test_other_parents_are_never_signalled(tmp_path, monkeypatch):
    code, kills, _ = run(tmp_path, monkeypatch, parent="bash")
    assert code == 75 and kills == []


def test_fail_fast_can_be_disabled_in_launcher_config(tmp_path, monkeypatch):
    code, kills, _ = run(tmp_path, monkeypatch, terminate_wrapper_on_placement_failure=False)
    assert code == 75 and kills == []


def test_kill_failure_is_logged_not_raised(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    monkeypatch.setattr(launcher, "request_json", rejected_placement())
    monkeypatch.setattr(launcher, "run_checked", lambda command: launcher.subprocess.CompletedProcess(
        command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), ""))
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: "vllm-wrapper")

    def refuse(pid, sig):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(launcher.os, "kill", refuse)
    assert launcher.main(argv(config_path)) == 75


def test_lock_timeout_and_dry_run_do_not_signal(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    kills = []
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: "vllm-wrapper")
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(launcher, "request_json", rejected_placement())
    assert launcher.main(["0.9", "vllm-qwen", "--dry-run", "--config", str(config_path), "--", "vllm", "serve", "m"]) == 75
    assert kills == []

    import contextlib

    @contextlib.contextmanager
    def timed_out(config, model, dry_run):
        raise launcher.LaunchError(f"timed out waiting for model lock {model}", 75)
        yield

    monkeypatch.setattr(launcher, "model_lock", timed_out)
    assert launcher.main(argv(config_path)) == 75
    assert kills == []


def test_non_boolean_fail_fast_setting_is_rejected(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, terminate_wrapper_on_placement_failure="false")
    monkeypatch.setattr(launcher, "request_json", rejected_placement())
    assert launcher.main(argv(config_path)) == 64
