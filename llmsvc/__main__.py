# Generated-By: Codex / gpt-6-astra
"""Read-only scheduler entry point; systemd captures structured stderr logs."""

import argparse
import json
import logging
import signal
import threading

from llmsvc import __version__
from llmsvc.config import load_config
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer


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
    parser = argparse.ArgumentParser(description="llmsvc read-only scheduler")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", required=True, help="scheduler YAML configuration")
    parser.add_argument("--dry-run", action="store_true", help="disable actions (always enforced in M1)")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument("--once", action="store_true", help="collect one JSON snapshot and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = load_config(args.config)
        collector = build_collector(config)
        scheduler = Scheduler(config, collect=collector, usage=build_usage(collector))
    except (OSError, ValueError, TypeError, ImportError) as exc:
        parser.error(str(exc))
    if args.check_config:
        scheduler.stop()
        return 0
    if args.once:
        try:
            print(json.dumps(scheduler.sample_once().to_dict(), allow_nan=False))
        finally:
            scheduler.stop()
        return 0
    try:
        server = SchedulerHTTPServer((config.listen_host, config.listen_port), scheduler)
    except OSError as exc:
        scheduler.stop()
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
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
