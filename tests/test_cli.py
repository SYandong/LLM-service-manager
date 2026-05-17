import sys

import vllm_service.cli as cli_mod


def test_start_accepts_model_id(tmp_path, monkeypatch):
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        """
model: "google/gemma-4-31B-it"
host: "127.0.0.1"
port: 8000
gpu_memory_utilization: 0.9
max_model_len: 32768
enable_reasoning: false
reasoning_parser: "deepseek_r1"
"""
    )
    captured = {}

    monkeypatch.setattr(cli_mod, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(cli_mod.process, "is_running", lambda: (False, None))
    monkeypatch.setattr(cli_mod.readiness, "wait_until_ready", lambda *args, **kwargs: True)

    def fake_start(cmd, log_file, model=None):
        captured["cmd"] = cmd
        captured["model"] = model
        return 123

    monkeypatch.setattr(cli_mod.process, "start", fake_start)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vllm_service", "start", "--model", "Qwen/Qwen3-4B-Instruct-2507"],
    )

    cli_mod.main()

    assert captured["cmd"][:3] == ["vllm", "serve", "Qwen/Qwen3-4B-Instruct-2507"]
    assert captured["model"] == "Qwen/Qwen3-4B-Instruct-2507"
