# Generated-By: Codex / gpt-6-astra
"""Read-only scheduler entry point; systemd captures structured stderr logs."""

import argparse
import json
import logging
import signal
import sqlite3
import threading

from llmsvc import __version__
from llmsvc.config import load_config
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.store import IntentStore


def build_collector(config):
    if not config.collectors:
        return None
    # The telemetry lane owns this adapter and its probe configuration.
    from llmsvc.collectors import build_collector as factory
    collector = factory(config.collectors)
    if not callable(collector):
        raise TypeError("collector factory must return a callable")
    return collector


def main():
    parser = argparse.ArgumentParser(description="llmsvc read-only scheduler")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", required=True, help="scheduler YAML configuration")
    parser.add_argument("--dry-run", action="store_true", help="disable actions (always enforced in M1)")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument("--once", action="store_true", help="collect one JSON snapshot and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = None
    try:
        config = load_config(args.config)
        if args.check_config:
            return 0
        collector = build_collector(config)
        if config.state_db_path:
            store = IntentStore(config.state_db_path, action_lock=threading.RLock(), read_only=True)
        scheduler = Scheduler(config, collect=collector, store=store)
    except (OSError, ValueError, TypeError, ImportError, sqlite3.Error) as exc:
        if store is not None:
            store.close()
        parser.error(str(exc))
    if args.once:
        try:
            scheduler.sample_once()
            print(json.dumps(scheduler.snapshot().to_dict(), allow_nan=False))
        finally:
            if store is not None:
                store.close()
        return 0
    try:
        server = SchedulerHTTPServer((config.listen_host, config.listen_port), scheduler)
    except OSError as exc:
        if store is not None:
            store.close()
        parser.error(str(exc))
    # Signal handlers only notify. HTTP shutdown must run outside serve_forever.
    exit_requested = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: exit_requested.set())
    http_thread = threading.Thread(target=server.serve_forever, name="llmsvc-http", daemon=True)
    try:
        scheduler.start()
        http_thread.start()
        scheduler.emit("started", detail={"read_only": True})
        while not exit_requested.wait(0.5):
            if not http_thread.is_alive():
                raise RuntimeError("HTTP server thread exited")
    finally:
        scheduler.stop()
        if http_thread.is_alive():
            server.shutdown()
        server.server_close()
        http_thread.join(timeout=config.request_timeout_seconds)
        if store is not None:
            store.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
