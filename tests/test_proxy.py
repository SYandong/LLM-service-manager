import json
from types import SimpleNamespace
from unittest.mock import Mock

from vllm_service.config import ServerConfig
import vllm_service.proxy as proxy_mod


def _config(**overrides):
    defaults = dict(
        model="google/gemma-4-31B-it",
        host="0.0.0.0",
        port=8000,
        gpu_memory_utilization=0.9,
        max_model_len=32768,
        enable_reasoning=False,
        reasoning_parser="deepseek_r1",
        backend_port=8001,
    )
    return ServerConfig(**{**defaults, **overrides})


def test_requested_model_restarts_backend_when_different(monkeypatch):
    config = _config()
    started = {}

    monkeypatch.setattr(proxy_mod.process, "is_running", lambda: (True, 123))
    monkeypatch.setattr(proxy_mod.process, "read_metadata", lambda: {"model": "google/gemma-4-31B-it"})
    monkeypatch.setattr(proxy_mod.process, "stop", Mock(return_value=True))
    monkeypatch.setattr(proxy_mod.launcher, "LOG_FILE", "vllm.log")
    monkeypatch.setattr(proxy_mod.launcher, "build_command", lambda cfg: ["vllm", "serve", cfg.model, "--port", str(cfg.port)])
    monkeypatch.setattr(proxy_mod.readiness, "wait_until_ready", Mock(return_value=True))

    def fake_start(cmd, log_file, model=None):
        started["cmd"] = cmd
        started["model"] = model
        return 456

    monkeypatch.setattr(proxy_mod.process, "start", fake_start)

    proxy_mod.ensure_model("Qwen/Qwen3-4B-Instruct-2507", config)

    proxy_mod.process.stop.assert_called_once()
    assert started["cmd"] == ["vllm", "serve", "Qwen/Qwen3-4B-Instruct-2507", "--port", "8001"]
    assert started["model"] == "Qwen/Qwen3-4B-Instruct-2507"
    proxy_mod.readiness.wait_until_ready.assert_called_once()


def test_same_model_does_not_restart(monkeypatch):
    config = _config()
    monkeypatch.setattr(proxy_mod.process, "is_running", lambda: (True, 123))
    monkeypatch.setattr(proxy_mod.process, "read_metadata", lambda: {"model": "Qwen/Qwen3-4B-Instruct-2507"})
    monkeypatch.setattr(proxy_mod.process, "stop", Mock())
    monkeypatch.setattr(proxy_mod.process, "start", Mock())

    proxy_mod.ensure_model("Qwen/Qwen3-4B-Instruct-2507", config)

    proxy_mod.process.stop.assert_not_called()
    proxy_mod.process.start.assert_not_called()


def test_missing_model_uses_default_model(monkeypatch):
    config = _config()
    started = {}

    monkeypatch.setattr(proxy_mod.process, "is_running", lambda: (False, None))
    monkeypatch.setattr(proxy_mod.launcher, "LOG_FILE", "vllm.log")
    monkeypatch.setattr(proxy_mod.launcher, "build_command", lambda cfg: ["vllm", "serve", cfg.model])
    monkeypatch.setattr(proxy_mod.readiness, "wait_until_ready", Mock(return_value=True))
    monkeypatch.setattr(proxy_mod.process, "start", lambda cmd, log_file, model=None: started.update(model=model) or 123)

    proxy_mod.ensure_model(None, config)

    assert started["model"] == "google/gemma-4-31B-it"


def test_invalid_model_returns_bad_request():
    config = _config()
    handler = SimpleNamespace(
        headers={"Content-Length": "13"},
        rfile=SimpleNamespace(read=lambda size: b'{"model": ""}'),
        send_response=Mock(),
        send_header=Mock(),
        end_headers=Mock(),
        wfile=SimpleNamespace(write=Mock()),
    )

    proxy_mod.handle_post(handler, "/v1/chat/completions", config)

    handler.send_response.assert_called_once_with(400)
    assert json.loads(handler.wfile.write.call_args.args[0])["error"] == "model must be a non-empty string"


def test_post_forwards_original_request_after_ensuring_model(monkeypatch):
    config = _config()
    handler = SimpleNamespace(
        headers={"Content-Length": "70"},
        rfile=SimpleNamespace(
            read=lambda size: b'{"model": "Qwen/Qwen3-4B-Instruct-2507", "messages": [{"role": "user"}]}'
        ),
    )
    ensured = Mock()
    forwarded = Mock()
    monkeypatch.setattr(proxy_mod, "ensure_model", ensured)
    monkeypatch.setattr(proxy_mod, "_forward", forwarded)

    proxy_mod.handle_post(handler, "/v1/chat/completions", config)

    ensured.assert_called_once_with("Qwen/Qwen3-4B-Instruct-2507", config)
    forwarded.assert_called_once_with(
        handler,
        "POST",
        "/v1/chat/completions",
        b'{"model": "Qwen/Qwen3-4B-Instruct-2507", "messages": [{"role": "user"}]}',
        config,
    )


def test_get_models_uses_default_model_before_forwarding(monkeypatch):
    config = _config()
    handler = SimpleNamespace()
    ensured = Mock()
    forwarded = Mock()
    monkeypatch.setattr(proxy_mod, "ensure_model", ensured)
    monkeypatch.setattr(proxy_mod, "_forward", forwarded)

    proxy_mod.handle_get(handler, "/v1/models", config)

    ensured.assert_called_once_with(None, config)
    forwarded.assert_called_once_with(handler, "GET", "/v1/models", b"", config)
