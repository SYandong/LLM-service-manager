# Generated-By: Codex / gpt-6-astra
"""Scheduler entry point: read-only by default, with opt-in pin intent writes."""

import argparse
import json
import logging
import signal
import sqlite3
import threading
from dataclasses import replace

from llmsvc import __version__
from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.config import load_config
from llmsvc.leases import PlacementController
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.store import IntentStore


def build_collector(config):
    if not config.collectors:
        return None
    # The telemetry lane owns this adapter and its probe configuration.
    from llmsvc.collectors import build_collector as factory
    options = {**config.collectors, "memory_budget_gb": config.memory_budget_gb,
               "host_min_available_gb": config.host_min_available_gb}
    collector = factory(options)
    if not callable(collector):
        close = getattr(collector, "close", None)
        if close is not None:
            close()
        raise TypeError("collector factory must return a callable")
    return collector



def build_usage(collector):
    reader = getattr(collector, "activity_reader", None)
    if reader is None:
        return None

    def usage(*, days, by):
        from llmsvc.activity import ActivityReader
        # ActivityReader.last_error is mutable. A request-local reader prevents
        # HTTP queries from overwriting the sampler's activity error signal.
        request_reader = ActivityReader(reader.path, reader.ip_containers, reader.deadline_ms)
        return request_reader.usage(days=days, by=by)

    return usage


def main():
    parser = argparse.ArgumentParser(description="llmsvc scheduler (read-only by default)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", required=True, help="scheduler YAML configuration")
    parser.add_argument("--dry-run", action="store_true", help="force read-only operation and never create or write the intent database")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument("--once", action="store_true", help="collect one JSON snapshot and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = None
    collector = None
    try:
        config = load_config(args.config)
        if args.dry_run or args.check_config or args.once:
            config = replace(config, read_only=True)
        collector = build_collector(config)
        transport = None
        if config.model_actions_enabled or config.placement_enabled:
            transport = ManagedModelTransport(swap_url=config.collectors.get("swap_url", ""),
                models=config.collectors.get("models", {}), systemctl=config.collectors.get("systemctl", "systemctl"))
        if config.state_db_path:
            store = IntentStore(config.state_db_path, action_lock=threading.RLock(), read_only=config.read_only)
        scheduler = Scheduler(config, collect=collector, store=store, usage=build_usage(collector))
        if config.model_actions_enabled:
            scheduler.model_actions = ModelActionController(scheduler, transport)
        if config.placement_enabled:
            scheduler.placement = PlacementController(scheduler, transport)
    except (OSError, ValueError, TypeError, ImportError, sqlite3.Error) as exc:
        if store is not None:
            store.close()
        close = getattr(collector, "close", None)
        if close is not None:
            close()
        parser.error(str(exc))
    if args.check_config:
        scheduler.stop()
        if store is not None:
            store.close()
        return 0
    if args.once:
        try:
            print(json.dumps(scheduler.sample_once().to_dict(), allow_nan=False))
        finally:
            scheduler.stop()
            if store is not None:
                store.close()
        return 0
    try:
        server = SchedulerHTTPServer((config.listen_host, config.listen_port), scheduler)
    except OSError as exc:
        scheduler.stop()
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
        scheduler.emit("started", detail={"read_only": config.read_only})
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
