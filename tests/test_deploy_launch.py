import importlib.util
import json
import os
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "vllm-launch"


def load_launcher():
    loader = SourceFileLoader("deploy_vllm_launch", str(SCRIPT))
    spec = importlib.util.spec_from_loader("deploy_vllm_launch", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_config(tmp_path, **overrides):
    config = {
        "scheduler_url": "http://scheduler.test",
        "lock_dir": str(tmp_path / "locks"),
        "request_timeout_seconds": 121,
        "health_timeout_seconds": 1,
        "health_poll_seconds": 0,
        "startup_timeout_seconds": 0.01,
        "lock_timeout_seconds": 1,
        "systemd_run": {
            "collect": True,
            "quiet": True,
            "properties": {"Restart": "no"},
            "environment_file": "/tmp/llama-swap.env",
        },
    }
    config.update(overrides)
    path = tmp_path / "launcher.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, method, url, payload, timeout):
        with self.lock:
            self.requests.append((method, url, payload, timeout))
            response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def argv(config_path, unit="vllm-qwen"):
    return [
        "0.9",
        unit,
        "--config",
        str(config_path),
        "--",
        "vllm",
        "serve",
        "Qwen/Qwen3-32B",
        "--port",
        "8101",
    ]


def test_dry_run_posts_place_without_lock_or_unit_mutation(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(200, {"would": [{"kind": "place", "gpu": 0}]})])
    calls = []

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", lambda command: calls.append(command))

    assert launcher.main(["0.9", "vllm-qwen", "--dry-run", "--config", str(config_path), "--", "vllm", "serve", "Qwen/Qwen3-32B"]) == 0

    assert http.requests == [
        (
            "POST",
            "http://scheduler.test/v1/place?dry_run=1",
            {"model": "qwen", "util": 0.9},
            121.0,
        )
    ]
    assert calls == []
    assert not (tmp_path / "locks").exists()


def test_successful_start_confirms_lease_and_uses_scheduler_gpu(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200, {"gpu": 2, "lease_id": "lease-a"}), (200, {"ok": True})])
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: True)

    assert launcher.main(argv(config_path)) == 0

    systemd = next(command for command in commands if command[0] == "systemd-run")
    assert "--setenv=CUDA_VISIBLE_DEVICES=2" in systemd
    assert "--setenv=LLMSVC_LEASE_ID=lease-a" in systemd
    assert "--setenv=LLMSVC_MODEL=qwen" in systemd
    assert systemd[-5:] == ["vllm", "serve", "Qwen/Qwen3-32B", "--port", "8101"]
    assert http.requests[1][1] == "http://scheduler.test/v1/place/lease-a/confirm"


def test_preexisting_unit_exits_before_place_without_stop(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([])
    commands = []

    def fake_run(command):
        commands.append(command)
        return launcher.subprocess.CompletedProcess(command, 0, "loaded\n", "")

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)

    assert launcher.main(argv(config_path)) == 0

    assert http.requests == []
    assert commands == [["systemctl", "show", "-p", "LoadState", "--value", "vllm-qwen.service"]]


def test_systemd_run_failure_releases_lease(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(200, {"gpu": 1, "lease_id": "lease-fail"}), (200, {"released": True})])

    def fake_run(command):
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 1, "", "boom")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)

    assert launcher.main(argv(config_path)) == 1

    assert http.requests[1][1] == "http://scheduler.test/v1/place/lease-fail/release"


def test_systemd_run_failure_retains_lease_without_gone_evidence(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(200, {"gpu": 1, "lease_id": "lease-retain"})])

    def fake_run(command):
        if command == ["systemctl", "show", "-p", "LoadState", "--value", "vllm-qwen.service"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "LoadState":
            return launcher.subprocess.CompletedProcess(command, 0, "LoadState=loaded\nActiveState=active\nMainPID=123\nControlGroup=/system.slice/vllm-qwen.service\n", "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 1, "", "boom")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)

    assert launcher.main(argv(config_path)) == 1

    assert len(http.requests) == 1


def test_unit_exit_during_start_releases_lease(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200, {"gpu": 1, "lease_id": "lease-dead"}), (200, {"released": True})])

    def fake_run(command):
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 3, "inactive\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)

    assert launcher.main(argv(config_path)) == 1

    assert http.requests[1][1] == "http://scheduler.test/v1/place/lease-dead/release"


def test_confirm_409_stops_only_unit_with_matching_lease(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200, {"gpu": 0, "lease_id": "lease-old"}), (409, {"error": "superseded"})])
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "LoadState":
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "Environment":
            return launcher.subprocess.CompletedProcess(command, 0, "CUDA_VISIBLE_DEVICES=0 LLMSVC_LEASE_ID=lease-old\n", "")
        if command[:2] == ["systemctl", "stop"]:
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: True)

    assert launcher.main(argv(config_path)) == 75

    assert ["systemctl", "stop", "vllm-qwen.service"] in commands


def test_confirm_409_without_ownership_evidence_does_not_stop(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200, {"gpu": 0, "lease_id": "lease-old"}), (409, {"error": "superseded"})])
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "LoadState":
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "Environment":
            return launcher.subprocess.CompletedProcess(command, 0, "CUDA_VISIBLE_DEVICES=0\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: True)

    assert launcher.main(argv(config_path)) == 1

    assert not any(command[:2] == ["systemctl", "stop"] for command in commands)


def test_confirm_409_does_not_stop_on_lease_prefix_collision(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200, {"gpu": 0, "lease_id": "lease"}), (409, {"error": "superseded"})])
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "LoadState":
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 0, "active\n", "")
        if command[:3] == ["systemctl", "show", "-p"] and command[3] == "Environment":
            return launcher.subprocess.CompletedProcess(command, 0, "LLMSVC_LEASE_ID=lease-other CUDA_VISIBLE_DEVICES=0\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: True)

    assert launcher.main(argv(config_path)) == 1

    assert not any(command[:2] == ["systemctl", "stop"] for command in commands)


def test_startup_timeout_while_unit_active_retains_budget(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(200, {"gpu": 0, "lease_id": "lease-slow"})])

    def fake_run(command):
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: False)

    assert launcher.main(argv(config_path)) == 1

    assert len(http.requests) == 1


def test_activating_state_retains_budget_without_release(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(200, {"gpu": 0, "lease_id": "lease-loading"})])

    def fake_run(command):
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 3, "activating\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: False)

    assert launcher.main(argv(config_path)) == 1

    assert len(http.requests) == 1


def test_place_409_does_not_stop_any_unit(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(409, {"error": "blocked"})])
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)

    assert launcher.main(argv(config_path)) == 75

    assert not any(command[:2] == ["systemctl", "stop"] for command in commands)


def test_unit_inspection_error_fails_before_place(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([])

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(
        launcher,
        "run_checked",
        lambda command: launcher.subprocess.CompletedProcess(command, 1, "", "dbus unavailable"),
    )

    assert launcher.main(argv(config_path)) == 1

    assert http.requests == []


def test_bad_util_is_rejected_before_scheduler_request(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([])

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(
        launcher,
        "run_checked",
        lambda command: launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), ""),
    )

    assert launcher.main(argv(config_path)[:0] + ["nan", "vllm-qwen", "--config", str(config_path), "--", "vllm"]) == 64

    assert http.requests == []


def test_malformed_place_response_fails_without_starting_unit(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(200, {"gpu": 0})])
    commands = []

    def fake_run(command):
        commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"]:
            return launcher.subprocess.CompletedProcess(command, 0, ("not-found\n" if "--value" in command else "LoadState=not-found\n"), "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)

    assert launcher.main(argv(config_path)) == 1

    assert not any(command and command[0] == "systemd-run" for command in commands)


def test_bad_timeout_config_is_rejected(tmp_path):
    launcher = load_launcher()
    config_path = write_config(tmp_path, request_timeout_seconds=-1)

    assert launcher.main(argv(config_path)) == 64


def test_dry_run_scheduler_error_fails_without_mutation(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path)
    http = FakeHTTP([(500, {"error": "bad dry-run"})])
    calls = []

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", lambda command: calls.append(command))

    assert launcher.main(["0.9", "vllm-qwen", "--dry-run", "--config", str(config_path), "--", "vllm"]) == 75

    assert calls == []
    assert not (tmp_path / "locks").exists()


def test_model_lock_serializes_concurrent_same_model_without_duplicate_systemd_run(tmp_path, monkeypatch):
    launcher = load_launcher()
    config_path = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200, {"gpu": 0, "lease_id": "lease-one"}), (200, {"ok": True})])
    load_state = ["not-found\n", "loaded\n"]
    commands = []
    commands_lock = threading.Lock()
    start = threading.Barrier(2)

    def fake_run(command):
        with commands_lock:
            commands.append(command)
        if command[:3] == ["systemctl", "show", "-p"]:
            with commands_lock:
                stdout = load_state.pop(0)
            return launcher.subprocess.CompletedProcess(command, 0, stdout, "")
        if command and command[0] == "systemd-run":
            return launcher.subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "is-active"]:
            return launcher.subprocess.CompletedProcess(command, 0, "active\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(launcher, "request_json", http)
    monkeypatch.setattr(launcher, "run_checked", fake_run)
    monkeypatch.setattr(launcher, "health_ok", lambda config, port: True)

    results = []

    def run_launch():
        start.wait()
        results.append(launcher.main(argv(config_path)))

    threads = [threading.Thread(target=run_launch), threading.Thread(target=run_launch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [0, 0]
    assert sum(1 for command in commands if command and command[0] == "systemd-run") == 1
    assert len(http.requests) == 2


def test_cli_dry_run_against_fake_http_server_without_lock_or_systemd(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append((self.path, json.loads(self.rfile.read(length))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"would":[{"kind":"place","gpu":0}]}')

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        config_path = write_config(tmp_path, scheduler_url=f"http://127.0.0.1:{server.server_port}")
        env = os.environ.copy()
        env["PATH"] = "/nonexistent"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "0.8", "vllm-qwen", "--dry-run", "--config", str(config_path), "--", "vllm"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=5,
        )
    finally:
        server.shutdown()
        thread.join()

    assert result.returncode == 0
    assert requests == [("/v1/place?dry_run=1", {"model": "qwen", "util": 0.8})]
    assert not (tmp_path / "locks").exists()


def test_release_proof_is_keyed_and_missing_evidence_fails_closed(monkeypatch):
    launcher = load_launcher()
    for stdout, expected in (
        ("ControlGroup=\nMainPID=0\nActiveState=inactive\nLoadState=loaded\n", True),
        ("LoadState=loaded\nActiveState=inactive\n", False),
        ("", False),
        ("LoadState=not-found\n", True),
        ("ControlGroup=/still-present\nMainPID=0\nActiveState=inactive\nLoadState=loaded\n", False),
    ):
        monkeypatch.setattr(launcher, "run_checked", lambda command: launcher.subprocess.CompletedProcess(command, 0, stdout, ""))
        assert launcher.release_safe("vllm-qwen.service") is expected
