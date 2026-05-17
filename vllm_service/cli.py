import argparse
import socket
import sys
import time
from pathlib import Path

from . import launcher, process, readiness
from .config import load_config

_CONFIG_FILE = Path(__file__).parent.parent / "config" / "server.yaml"
SUPPORTED_MODELS = (
    "google/gemma-4-31B-it",
    "Qwen/Qwen3-4B-Instruct-2507",
)


def _display_host(host: str) -> str:
    if host == "0.0.0.0":
        return socket.gethostbyname(socket.gethostname())
    return host


def _start(model: str | None = None):
    running, pid = process.is_running()
    if running:
        print(f"Service already running (pid {pid})")
        return
    config = load_config(_CONFIG_FILE)
    if model is not None:
        config.model = model
    cmd = launcher.build_command(config)
    pid = process.start(cmd, launcher.LOG_FILE, model=config.model)
    print(f"Loading model: {config.model} (pid {pid})")
    alive_fn = lambda: process.is_running()[0]
    start_time = time.time()
    if readiness.wait_until_ready(config.host, config.port, alive_fn=alive_fn):
        elapsed = time.time() - start_time
        print(f"Ready in {elapsed:.0f}s at http://{_display_host(config.host)}:{config.port}/v1")
    else:
        sys.exit(1)


def _stop():
    running, pid = process.is_running()
    if not running:
        print("Service is not running")
        return
    print(f"Stopping vLLM (pid {pid})...")
    if process.stop():
        print("Service stopped")
    else:
        print("Service did not stop within timeout")


def _status():
    config = load_config(_CONFIG_FILE)
    running, pid = process.is_running()
    if not running:
        print("Service: stopped")
        return
    metadata = process.read_metadata()
    print("Service: running")
    print(f"  model:  {metadata.get('model', config.model)}")
    print(f"  pid:    {pid}")
    print(f"  url:    http://{_display_host(config.host)}:{config.port}/v1")
    print(f"  api:    {'ready' if readiness.is_ready(config.host, config.port) else 'not ready'}")
    print(f"  log:    {launcher.LOG_FILE}")


def _restart(model: str | None = None):
    _stop()
    _start(model)


def main():
    parser = argparse.ArgumentParser(prog="vllm_service")
    sub = parser.add_subparsers(dest="command")
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--model", choices=SUPPORTED_MODELS, help="Hugging Face model ID to serve")
    sub.add_parser("stop")
    sub.add_parser("status")
    restart_parser = sub.add_parser("restart")
    restart_parser.add_argument("--model", choices=SUPPORTED_MODELS, help="Hugging Face model ID to serve")
    args = parser.parse_args()
    if args.command == "start":
        _start(args.model)
    elif args.command == "stop":
        _stop()
    elif args.command == "status":
        _status()
    elif args.command == "restart":
        _restart(args.model)
    else:
        parser.print_help()
        sys.exit(1)
