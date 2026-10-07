# Generated-By: Codex / gpt-6.1-sol
"""Run fleet observation and history without constructing the shared control plane."""

import argparse
import logging
import os
import signal
import threading
import time

from llmsvc import __version__
from llmsvc.fleet import FleetError
from llmsvc.fleet.config import ObserverConfig
from llmsvc.fleet.controller import FleetController
from llmsvc.fleet.events import FleetEvents
from llmsvc.fleet.server import ObserverHTTPServer


class FleetObserver:
    def __init__(self, config, *, clock=time.time):
        self.config = config
        self.events = FleetEvents(config.event_history_size, clock=clock)
        self.controller = FleetController(config, clock=clock, emit=self.events.emit, observe_claims=False)
        self.stopping = threading.Event()
        self.thread = None
        self._publication = None

    def report(self, *, source_ip=None, mine=False):
        return dict(self.controller.report(source_ip=source_ip, mine=mine),
                    observer_incarnation=self.events.incarnation)

    def history(self, service_id, hours):
        return self.controller.history(service_id, hours)

    def ingest_once(self):
        self.controller.ingest_once()
        try:
            payload = self.report()
        except FleetError:
            return
        publication = payload["generated_at"], payload["stale"], tuple(payload["errors"])
        if publication != self._publication:
            self._publication = publication
            self.events.emit("fleet_snapshot_changed", detail={"generated_at": payload["generated_at"],
                                                              "stale": payload["stale"]})

    def start(self):
        if self.thread is not None or self.stopping.is_set():
            raise RuntimeError("observer_already_started_or_stopped")
        self.thread = threading.Thread(target=self._run, name="llmsvc-fleet-observer", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopping.is_set():
            self.ingest_once()
            self.stopping.wait(self.config.fleet_ingest_interval_seconds)

    def close(self):
        self.stopping.set()
        try:
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    raise RuntimeError("observer_worker_did_not_stop")
            self.controller.close()
        finally:
            self.events.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--version", action="version", version="Fleet observer " + __version__)
    args = parser.parse_args(argv)
    try:
        config = ObserverConfig.load(args.config)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        print('{"ok":false,"error":"invalid_observer_config"}')
        return 1
    if args.check_config:
        print('{"ok":true,"check_config":true}')
        return 0
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    observer = FleetObserver(config)
    server = None
    stopping = threading.Event()
    previous = {}
    try:
        server = ObserverHTTPServer((config.listen_host, config.listen_port), observer)
        server.timeout = 0.5
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda number, frame: stopping.set())
        observer.start()
        while not stopping.is_set():
            server.handle_request()
    except (OSError, RuntimeError):
        print('{"ok":false,"error":"observer_runtime_failed"}')
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if server is not None:
            server.server_close()
        observer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
