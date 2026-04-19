import argparse
import socket
import sys
import time
from pathlib import Path

from . import launcher, process, readiness
from .config import load_config

_CONFIG_FILE = Path(__file__).parent.parent / "config" / "server.yaml"


def _display_host(host: str) -> str:
    if host == "0.0.0.0":
        return socket.gethostbyname(socket.gethostname())
    return host


def _start():
    running, pid = process.is_running()
    if running:
        print(f"Service already running (pid {pid})")
        return
    config = load_config(_CONFIG_FILE)
    cmd = launcher.build_command(config)
    pid = process.start(cmd, launcher.LOG_FILE)
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
    print("Service: running")
    print(f"  model:  {config.model}")
    print(f"  pid:    {pid}")
    print(f"  url:    http://{_display_host(config.host)}:{config.port}/v1")
    print(f"  api:    {'ready' if readiness.is_ready(config.host, config.port) else 'not ready'}")
    print(f"  log:    {launcher.LOG_FILE}")


def main():
    parser = argparse.ArgumentParser(prog="vllm_service")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "start":
        _start()
    elif args.command == "stop":
        _stop()
    elif args.command == "status":
        _status()
    else:
        parser.print_help()
        sys.exit(1)
