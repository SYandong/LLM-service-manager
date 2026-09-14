"""A placement rejection terminates the waiting vllm-wrapper parent so llama-swap fails fast."""

import signal

from test_deploy_launch import FakeHTTP, argv, load_launcher, write_config


def rejected_placement():
    return FakeHTTP([(409, {"error": "placement_timeout", "blockers": []})])


def run(tmp_path, monkeypatch, *, parent="vllm-wrapper", **overrides):
    launcher = load_launcher()
    config_path = write_config(tmp_path, **overrides)
    kills = []
    monkeypatch.setattr(launcher, "request_json", rejected_placement())
    monkeypatch.setattr(launcher, "run_checked", lambda command: launcher.subprocess.CompletedProcess(
        command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), ""))
    monkeypatch.setattr(launcher, "parent_command_name", lambda pid: parent)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    return launcher.main(argv(config_path)), kills, launcher


def test_placement_rejection_terminates_a_waiting_wrapper(tmp_path, monkeypatch):
    code, kills, launcher = run(tmp_path, monkeypatch)
    assert code == 75
    assert kills == [(launcher.os.getppid(), signal.SIGTERM)]


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
