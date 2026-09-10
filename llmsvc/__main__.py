# Generated-By: Codex / gpt-6-astra
"""Scheduler entry point: read-only by default, with opt-in pin intent writes."""

import argparse
import copy
import json
import logging
import signal
import sqlite3
import threading
from dataclasses import replace
from urllib.parse import urlsplit

from llmsvc import __version__
from llmsvc.actions import AutomaticPolicyController, ManagedModelTransport, ModelActionController
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



def build_event_relay(config):
    """Construct one opt-in UI reader; never start it during validation/import."""
    if not config.data_plane_events_enabled:
        return None
    from llmsvc.collectors.relay import DataPlaneEventRelay
    url = config.collectors.get("swap_url", "")
    if not isinstance(url, str):
        raise ValueError("data-plane swap_url must be a string")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or "/upstream" in parts.path
            or parts.port == 0):
        raise ValueError("data-plane events require a trusted HTTP(S) swap_url without credentials or upstream routing")
    models = config.collectors.get("models", {})
    if not isinstance(models, dict):
        raise ValueError("collectors.models must be a mapping")
    return DataPlaneEventRelay(url, tuple(models), capacity=config.data_plane_event_capacity,
                              timeout=config.data_plane_event_timeout_seconds,
                              reconnect_delay=config.data_plane_event_reconnect_seconds)


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


def build_registry(config, scheduler):
    """Mount existing registry transforms with no live validation/reload path."""
    if not config.registry:
        return None
    from pathlib import Path
    from llmsvc.registry import ModelRegistry
    from llmsvc.reload import QuietPeriod, ReloadQueue
    def disabled(*args, **kwargs):
        raise RuntimeError("registry writes are not enabled")
    def reserved_ports():
        ports = list(config.registry.get("reserved_ports", [])) + [config.listen_port]
        for model in config.collectors.get("models", {}).values():
            if "port" in model:
                ports.append(model["port"])
        return ports
    queue = ReloadQueue(Path(config.registry["config_path"]), action_lock=scheduler.action_lock,
        quiet=QuietPeriod(), snapshot=scheduler.snapshot, validate=disabled, notify_reload=disabled,
        log=lambda event: logging.getLogger("llmsvc.registry").info(json.dumps(event, allow_nan=False)),
        wall_clock=scheduler.clock, max_snapshot_age=config.max_snapshot_age_seconds,
        operation_timeout=min(config.request_timeout_seconds, 60),
        **{key: config.registry[key] for key in ("config_max_bytes",) if key in config.registry})
    return ModelRegistry(queue, shared_roots=config.registry["shared_roots"],
        daemon_port_range=tuple(config.registry["daemon_port_range"]), reserved_ports=reserved_ports,
        now=scheduler.clock, **{key: config.registry[key] for key in
            ("model_config_max_bytes", "weight_index_max_bytes") if key in config.registry})


def maintenance_config(config):
    """Use explicit trusted profile completions; never change existing fields."""
    if config.catalog_mode != "maintenance" or not config.catalog_enabled:
        return config
    models = config.collectors.get("models", {})
    completed = {}
    for name, existing in models.items():
        profile = config.catalog_profiles.get(name)
        if not isinstance(profile, dict):
            raise ValueError("maintenance needs trusted profiles for every current model")
        if any(key in existing and existing[key] != value for key, value in profile.items()):
            raise ValueError("maintenance profile disagrees with configured model")
        completed[name] = {**copy.deepcopy(existing), **copy.deepcopy(profile)}
    return replace(config, collectors={**copy.deepcopy(config.collectors), "models": completed})


def build_catalog(config, scheduler):
    from llmsvc.catalog import CatalogRuntime
    if scheduler.registry is None:
        raise ValueError("catalog checkpoint/install requires configured registry")
    queue = scheduler.registry.queue
    if config.catalog_mode != "maintenance" or not config.maintenance_command:
        return CatalogRuntime(scheduler, queue)
    from llmsvc.maintenance import CommandBackend, MaintenanceController
    from llmsvc.registry import ModelRegistry
    controller = MaintenanceController(scheduler, queue, CommandBackend(config.maintenance_command, monotonic=queue.clock))
    profiles = copy.deepcopy(config.catalog_profiles)
    def trusted_profiles(candidate):
        document, _ = ModelRegistry._decode(candidate)
        try:
            return {name: copy.deepcopy(profiles[name]) for name in document["models"]}
        except KeyError as exc:
            raise ValueError("requested model lacks a trusted maintenance profile") from exc
    queue.validate = controller.validate
    runtime = CatalogRuntime(scheduler, queue, transition=controller, verifier=controller.verify,
                             profile_provider=trusted_profiles, instance_provider=controller.inspect_instance)
    scheduler.registry.stop_model = controller.stop_model
    scheduler.registry.unit_absent = controller.unit_absent
    if runtime.can_submit():
        runtime.connect_registry(scheduler.registry)
    return runtime


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
    event_relay = None
    try:
        config = load_config(args.config)
        if args.dry_run or args.check_config or args.once:
            config = replace(config, read_only=True)
        config = maintenance_config(config)
        collector = build_collector(config)
        event_relay = build_event_relay(config)
        transport = None
        if config.model_actions_enabled or config.placement_enabled:
            transport = ManagedModelTransport(swap_url=config.collectors.get("swap_url", ""),
                models=config.collectors.get("models", {}), systemctl=config.collectors.get("systemctl", "systemctl"))
        if config.state_db_path:
            store = IntentStore(config.state_db_path, action_lock=threading.RLock(), read_only=config.read_only)
        scheduler = Scheduler(config, collect=collector, store=store, usage=build_usage(collector), event_relay=event_relay)
        scheduler.registry = build_registry(config, scheduler)
        if config.model_actions_enabled:
            scheduler.model_actions = ModelActionController(scheduler, transport)
        if config.placement_enabled:
            scheduler.placement = PlacementController(scheduler, transport)
        if config.automation_enabled:
            scheduler.automation = AutomaticPolicyController(scheduler)
        if config.sleeping_recovery_enabled:
            from llmsvc.recovery import SleepingRecoveryController
            scheduler.sleeping_recovery = SleepingRecoveryController(scheduler)
        if config.fault_recovery_enabled:
            from llmsvc.faults import FaultRecoveryController
            scheduler.faults = FaultRecoveryController(scheduler)
        if config.catalog_enabled or (store is not None and store.catalog_checkpoint() is not None):
            build_catalog(config, scheduler)
            checkpoint = store.catalog_checkpoint() if store is not None else None
            if checkpoint is not None:
                logging.getLogger("llmsvc.catalog").info(json.dumps({"kind": "catalog_restored",
                    "catalog_epoch": scheduler.catalog_epoch, "phase": checkpoint["phase"],
                    "fenced": scheduler.catalog_fenced, "mode": config.catalog_mode}))

    except (OSError, ValueError, TypeError, ImportError, sqlite3.Error) as exc:
        try:
            if event_relay is not None:
                event_relay.close()
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                close = getattr(collector, "close", None)
                if close is not None:
                    close()
        parser.error(str(exc))
    def close_scheduler():
        try:
            scheduler.stop()
        finally:
            if store is not None:
                store.close()

    if args.check_config:
        close_scheduler()
        return 0
    if args.once:
        try:
            print(json.dumps(scheduler.sample_once().to_dict(), allow_nan=False))
            if scheduler.automation is not None:
                scheduler.automation.run(dry_run=True)  # Plan-only structured log; no extra sample or action.
            if scheduler.faults is not None:
                scheduler.faults.run_once(dry_run=True)  # One sample cannot prove a fresh fault sequence.
        finally:
            close_scheduler()
        return 0
    try:
        server = SchedulerHTTPServer((config.listen_host, config.listen_port), scheduler)
    except OSError as exc:
        close_scheduler()
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
        try:
            scheduler.stop()
        finally:
            try:
                if http_thread.is_alive():
                    server.shutdown()
                server.server_close()
                if http_thread.ident is not None:
                    http_thread.join(timeout=config.request_timeout_seconds)
            finally:
                try:
                    if store is not None:
                        store.close()
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
