import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from huggingface_hub import snapshot_download

from . import launcher, process, readiness
from .config import ServerConfig

_switch_lock = threading.Lock()
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def backend_config(config: ServerConfig, model: str) -> ServerConfig:
    return replace(config, model=model, host="127.0.0.1", port=config.backend_port)


def current_model() -> str | None:
    running, _ = process.is_running()
    if not running:
        return None
    return process.read_metadata().get("model")


def ensure_model(requested_model: str | None, config: ServerConfig) -> None:
    model = requested_model if requested_model is not None else config.model
    with _switch_lock:
        if current_model() == model:
            return
        if not Path(model).exists():
            try:
                snapshot_download(model, local_files_only=True)
            except Exception as error:
                raise RuntimeError(f"model is not available locally: {model}") from error
        if process.is_running()[0]:
            process.stop()
        backend = backend_config(config, model)
        cmd = launcher.build_command(backend)
        env = os.environ.copy()
        env["HF_HUB_OFFLINE"] = "1"
        process.start(cmd, launcher.LOG_FILE, model=model, env=env)
        alive_fn = lambda: process.is_running()[0]
        if not readiness.wait_until_ready(backend.host, backend.port, alive_fn=alive_fn):
            raise RuntimeError(f"model did not become ready: {model}")


def _read_body(handler) -> bytes:
    length = int(handler.headers.get("Content-Length", "0"))
    return handler.rfile.read(length) if length else b""


def _model_from_body(body: bytes) -> str | None:
    if not body:
        return None
    payload = json.loads(body)
    if "model" not in payload:
        return None
    model = payload["model"]
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    return model


def _backend_url(config: ServerConfig, path: str) -> str:
    return f"http://127.0.0.1:{config.backend_port}{path}"


def _forward(handler, method: str, path: str, body: bytes, config: ServerConfig) -> None:
    headers = {
        key: value
        for key, value in handler.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    request = urllib.request.Request(_backend_url(config, path), data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=None) as response:
            response_body = response.read()
            handler.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() not in _HOP_BY_HOP_HEADERS:
                    handler.send_header(key, value)
            handler.end_headers()
            handler.wfile.write(response_body)
    except urllib.error.HTTPError as error:
        response_body = error.read()
        handler.send_response(error.code)
        for key, value in error.headers.items():
            if key.lower() not in _HOP_BY_HOP_HEADERS:
                handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(response_body)
    except urllib.error.URLError as error:
        _send_json_error(handler, 502, f"backend request failed: {error.reason}")


def _send_json_error(handler, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_post(handler, path: str, config: ServerConfig) -> None:
    body = _read_body(handler)
    try:
        model = _model_from_body(body)
        ensure_model(model, config)
    except ValueError as error:
        _send_json_error(handler, 400, str(error))
        return
    except RuntimeError as error:
        _send_json_error(handler, 503, str(error))
        return
    _forward(handler, "POST", path, body, config)


def handle_get(handler, path: str, config: ServerConfig) -> None:
    try:
        ensure_model(None, config)
    except RuntimeError as error:
        _send_json_error(handler, 503, str(error))
        return
    _forward(handler, "GET", path, b"", config)


def make_handler(config: ServerConfig):
    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            handle_get(self, self.path, config)

        def do_POST(self):
            handle_post(self, self.path, config)

    return ProxyHandler


def serve(config: ServerConfig) -> None:
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    print(f"Proxy ready at http://{config.host}:{config.port}/v1", flush=True)
    server.serve_forever()
