import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import launcher, process, proxy, readiness
from .config import load_config

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_FILE = Path(__file__).parent.parent / "config" / "server.yaml"
_PROXY_PID_FILE = _PROJECT_ROOT / "var" / "run" / "proxy.pid"
_PROXY_LOG_FILE = _PROJECT_ROOT / "var" / "log" / "proxy.log"


def _display_host(host: str) -> str:
    if host == "0.0.0.0":
        return socket.gethostbyname(socket.gethostname())
    return host


def _connect_host(host: str) -> str:
    return "127.0.0.1" if host == "0.0.0.0" else host


def _wait_for_port(host: str, port: int, timeout: int = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((_connect_host(host), port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _proxy_command(model: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "vllm_service", "serve-proxy"]
    if model is not None:
        cmd += ["--model", model]
    return cmd


def _print_log_updates(log_file: Path, position: int) -> int:
    if not log_file.exists():
        return position
    with open(log_file, "r", errors="replace") as handle:
        handle.seek(position)
        for line in handle:
            print(line, end="")
        return handle.tell()


def _request_model_load(config, result: dict) -> None:
    url = f"http://{_connect_host(config.host)}:{config.port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=None) as response:
            response.read()
    except urllib.error.HTTPError as error:
        result["error"] = error.read().decode(errors="replace") or str(error)
    except Exception as error:
        result["error"] = str(error)


def _load_model(config) -> None:
    print(f"Loading model: {config.model}")
    position = launcher.LOG_FILE.stat().st_size if launcher.LOG_FILE.exists() else 0
    result = {}
    thread = threading.Thread(target=_request_model_load, args=(config, result))
    thread.start()
    while thread.is_alive():
        position = _print_log_updates(launcher.LOG_FILE, position)
        thread.join(0.2)
    _print_log_updates(launcher.LOG_FILE, position)
    if "error" in result:
        print(f"Model failed to load: {result['error']}")
        sys.exit(1)
    print(f"Ready at http://{_display_host(config.host)}:{config.port}/v1")


def _start(model: str | None = None):
    running, pid = process.is_running(_PROXY_PID_FILE)
    if running:
        print(f"Service already running (pid {pid})")
        return
    config = load_config(_CONFIG_FILE)
    if model is not None:
        config.model = model
    pid = process.start(
        _proxy_command(model),
        _PROXY_LOG_FILE,
        model=config.model,
        pid_file=_PROXY_PID_FILE,
        tee_stderr=False,
    )
    print(f"Starting proxy (pid {pid})...")
    if _wait_for_port(config.host, config.port):
        print(f"Proxy ready at http://{_display_host(config.host)}:{config.port}/v1")
        _load_model(config)
    else:
        print(f"Proxy did not start within timeout. Check {_PROXY_LOG_FILE}")
        sys.exit(1)


def _serve_proxy(model: str | None = None):
    config = load_config(_CONFIG_FILE)
    if model is not None:
        config.model = model
    proxy.serve(config)


def _stop():
    stopped = False
    proxy_running, proxy_pid = process.is_running(_PROXY_PID_FILE)
    if proxy_running:
        print(f"Stopping proxy (pid {proxy_pid})...")
        stopped = process.stop(_PROXY_PID_FILE) or stopped

    backend_running, backend_pid = process.is_running()
    if backend_running:
        print(f"Stopping vLLM (pid {backend_pid})...")
        stopped = process.stop() or stopped

    if not stopped:
        print("Service is not running")
        return
    print("Service stopped")


def _status():
    config = load_config(_CONFIG_FILE)
    proxy_running, proxy_pid = process.is_running(_PROXY_PID_FILE)
    backend_running, backend_pid = process.is_running()
    if not proxy_running and not backend_running:
        print("Service: stopped")
        return
    metadata = process.read_metadata()
    proxy_metadata = process.read_metadata(_PROXY_PID_FILE)
    print("Service: running")
    print(f"  default model: {proxy_metadata.get('model', config.model)}")
    print(f"  backend model: {metadata.get('model', 'not loaded')}")
    print(f"  proxy pid:     {proxy_pid if proxy_running else 'stopped'}")
    print(f"  backend pid:   {backend_pid if backend_running else 'stopped'}")
    print(f"  url:    http://{_display_host(config.host)}:{config.port}/v1")
    print(f"  api:    {'ready' if readiness.is_ready(config.host, config.port) else 'not ready'}")
    print(f"  proxy log:     {_PROXY_LOG_FILE}")
    print(f"  backend log:   {launcher.LOG_FILE}")


def _restart(model: str | None = None):
    _stop()
    _start(model)


def main():
    parser = argparse.ArgumentParser(prog="vllm_service")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    restart_parser = sub.add_parser("restart")
    restart_parser.add_argument("--model", help="Default model for requests that omit the OpenAI model field")
    serve_proxy_parser = sub.add_parser("serve-proxy")
    serve_proxy_parser.add_argument("--model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command == "start":
        _start()
    elif args.command == "stop":
        _stop()
    elif args.command == "status":
        _status()
    elif args.command == "restart":
        _restart(args.model)
    elif args.command == "serve-proxy":
        _serve_proxy(args.model)
    else:
        parser.print_help()
        sys.exit(1)
