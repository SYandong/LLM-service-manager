import sys

import vllm_service.cli as cli_mod


def test_start_runs_proxy_service_by_default(tmp_path, monkeypatch):
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        """
model: "google/gemma-4-31B-it"
host: "127.0.0.1"
port: 8000
backend_port: 8001
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: false
reasoning_parser: "deepseek_r1"
"""
    )
    started = {}
    loaded = []

    monkeypatch.setattr(cli_mod, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(cli_mod, "_PROXY_PID_FILE", tmp_path / "proxy.pid")
    monkeypatch.setattr(cli_mod, "_wait_for_port", lambda host, port: True)
    monkeypatch.setattr(cli_mod, "_load_model", lambda config: loaded.append(config.model))
    monkeypatch.setattr(cli_mod.process, "is_running", lambda pid_file=cli_mod.process.PID_FILE: (False, None))
    monkeypatch.setattr(cli_mod.process, "start", lambda cmd, log_file, pid_file=None, **kwargs: started.update(cmd=cmd, pid_file=pid_file) or 123)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vllm_service", "start"],
    )

    cli_mod.main()

    assert started["cmd"] == [sys.executable, "-m", "vllm_service", "serve-proxy"]
    assert started["pid_file"] == tmp_path / "proxy.pid"
    assert loaded == ["google/gemma-4-31B-it"]


def test_restart_runs_proxy_not_backend(tmp_path, monkeypatch):
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        """
model: "google/gemma-4-31B-it"
host: "127.0.0.1"
port: 8000
backend_port: 8001
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: false
reasoning_parser: "deepseek_r1"
"""
    )
    started = {}
    loaded = []

    monkeypatch.setattr(cli_mod, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(cli_mod, "_PROXY_PID_FILE", tmp_path / "proxy.pid")
    monkeypatch.setattr(cli_mod, "_stop", lambda: None)
    monkeypatch.setattr(cli_mod, "_wait_for_port", lambda host, port: True)
    monkeypatch.setattr(cli_mod, "_load_model", lambda config: loaded.append(config.model))
    monkeypatch.setattr(cli_mod.process, "is_running", lambda pid_file=cli_mod.process.PID_FILE: (False, None))
    monkeypatch.setattr(cli_mod.process, "start", lambda cmd, log_file, pid_file=None, **kwargs: started.update(cmd=cmd, pid_file=pid_file) or 123)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vllm_service", "restart"],
    )

    cli_mod.main()

    assert started["cmd"] == [sys.executable, "-m", "vllm_service", "serve-proxy"]
    assert started["pid_file"] == tmp_path / "proxy.pid"
    assert loaded == ["google/gemma-4-31B-it"]


def test_restart_model_sets_proxy_default_model(tmp_path, monkeypatch):
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        """
model: "google/gemma-4-31B-it"
host: "127.0.0.1"
port: 8000
backend_port: 8001
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: false
reasoning_parser: "deepseek_r1"
"""
    )
    started = {}
    loaded = []

    monkeypatch.setattr(cli_mod, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(cli_mod, "_PROXY_PID_FILE", tmp_path / "proxy.pid")
    monkeypatch.setattr(cli_mod, "_stop", lambda: None)
    monkeypatch.setattr(cli_mod, "_wait_for_port", lambda host, port: True)
    monkeypatch.setattr(cli_mod, "_load_model", lambda config: loaded.append(config.model))
    monkeypatch.setattr(cli_mod.process, "is_running", lambda pid_file=cli_mod.process.PID_FILE: (False, None))
    monkeypatch.setattr(cli_mod.process, "start", lambda cmd, log_file, pid_file=None, **kwargs: started.update(cmd=cmd, pid_file=pid_file) or 123)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vllm_service", "restart", "--model", "Qwen/Qwen3-4B-Instruct-2507"],
    )

    cli_mod.main()

    assert started["cmd"] == [
        sys.executable,
        "-m",
        "vllm_service",
        "serve-proxy",
        "--model",
        "Qwen/Qwen3-4B-Instruct-2507",
    ]
    assert loaded == ["Qwen/Qwen3-4B-Instruct-2507"]


def test_serve_proxy_runs_foreground_with_model(tmp_path, monkeypatch):
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        """
model: "google/gemma-4-31B-it"
host: "127.0.0.1"
port: 8000
backend_port: 8001
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: false
reasoning_parser: "deepseek_r1"
"""
    )
    served = {}

    monkeypatch.setattr(cli_mod, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(cli_mod.proxy, "serve", lambda config: served.update(config=config))
    monkeypatch.setattr(
        sys,
        "argv",
        ["vllm_service", "serve-proxy", "--model", "Qwen/Qwen3-4B-Instruct-2507"],
    )

    cli_mod.main()

    assert served["config"].model == "Qwen/Qwen3-4B-Instruct-2507"


def test_stop_stops_proxy_and_backend(tmp_path, monkeypatch):
    stopped = []

    monkeypatch.setattr(cli_mod, "_PROXY_PID_FILE", tmp_path / "proxy.pid")
    monkeypatch.setattr(cli_mod.process, "is_running", lambda pid_file=cli_mod.process.PID_FILE: (True, 123))
    monkeypatch.setattr(cli_mod.process, "stop", lambda pid_file=cli_mod.process.PID_FILE: stopped.append(pid_file) or True)

    cli_mod._stop()

    assert stopped == [tmp_path / "proxy.pid", cli_mod.process.PID_FILE]
