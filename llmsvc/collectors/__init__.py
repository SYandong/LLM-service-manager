# Generated-By: Codex / gpt-6-astra
"""Bounded observations using the shared state contract."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace

from llmsvc.state import Activity, MemoryState, ModelState, StateSnapshot
from .probes import Probes


class Collector:
    """Callable read-only collector; the scheduler owns its 15-second cadence.

    All configuration is supplied by the core configuration adapter. Model
    mappings accept daemon_url (direct vLLM, never /upstream), port, util,
    weights_gb, is_default, cold_start_seconds, and optional unit name.
    """

    def __init__(self, models, *, swap_url, activity_reader=None, probes=None,
                 deadline=1.8, memory_budget_gb=200, host_min_available_gb=150,
                 max_workers=32):
        if not 0 < deadline < 2:
            raise ValueError("collector deadline must be between zero and two seconds")
        self.models = {name: dict(value) for name, value in models.items()}
        self.probes = probes or Probes(swap_url)
        self.activity_reader = activity_reader
        self.deadline = deadline
        self.memory_budget_gb = memory_budget_gb
        self.host_min_available_gb = host_min_available_gb
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="telemetry")
        self.pending = {}
        self.lock = threading.Lock()
        self._closed = threading.Event()

    def close(self):
        # Retirement does not wait for already running bounded probes. Their
        # results are discarded if retirement is observed at round completion;
        # core still owns the generation fence at snapshot publication.
        self._closed.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _read(self, jobs, deadline, errors):
        current = {}
        for name, fn in jobs.items():
            if self._closed.is_set():
                break
            if time.monotonic() >= deadline:
                errors.append(name + ": deadline exceeded")
                continue
            previous = self.pending.get(name)
            if previous is not None and not previous.done():
                errors.append(name + ": previous probe still running")
                continue
            try:
                current[name] = self.pool.submit(fn)
            except RuntimeError as exc:
                # close() can shut down the executor between the check and
                # submit. A broken executor also makes this source unknown.
                errors.append(name + ": " + type(exc).__name__)
                continue
            self.pending[name] = current[name]
        if current:
            wait(current.values(), timeout=max(0, deadline - time.monotonic()))
        result = {}
        for name, future in current.items():
            if not future.done():
                future.cancel()
                errors.append(name + ": deadline exceeded")
                continue
            try:
                result[name] = future.result()
            except Exception as exc:
                # Do not expose URLs, paths, commands, or response payloads.
                errors.append(name + ": " + type(exc).__name__)
        return result

    def collect(self):
        if self._closed.is_set():
            return StateSnapshot(errors=("collector: closed",))
        if not self.lock.acquire(blocking=False):
            return StateSnapshot(errors=("collector: concurrent round",))
        try:
            snapshot = self._collect()
            if self._closed.is_set():
                return StateSnapshot(errors=("collector: closed",))
            return snapshot
        finally:
            self.lock.release()

    __call__ = collect

    def _collect(self):
        self.pending = {k: f for k, f in self.pending.items() if not f.done()}
        started = time.monotonic()
        sampled_at = time.time()
        deadline = started + self.deadline
        errors = []
        jobs = {key: getattr(self.probes, key) for key in
                ("gpus", "processes", "units", "memory", "running", "events")}
        if self.activity_reader:
            def read_activity():
                values = self.activity_reader.read(now=sampled_at)
                if self.activity_reader.last_error:
                    raise ValueError("activity unavailable")
                return values
            jobs["activity"] = read_activity
        first = self._read(jobs, started + self.deadline / 2, errors)
        units = first.get("units")
        configured = dict(self.models)
        if units is not None:
            for value in units.values():
                configured.setdefault(value["model"], {})
        probe_jobs = {}
        for name, config in configured.items():
            unit_name = config.get("unit", "vllm-" + name + ".service")
            unit = units.get(unit_name) if units is not None else None
            url = config.get("daemon_url")
            if url and (units is None or (unit is not None and unit["unit_active"] is not False)):
                probe_jobs["health:" + name] = lambda url=url: self.probes.health(url)
                probe_jobs["sleeping:" + name] = lambda url=url: self.probes.sleeping(url)
        probe_jobs["ownership"] = lambda: self._ownership(first)
        second = self._read(probe_jobs, deadline, errors)
        events = first.get("events")
        running = first.get("running")
        models = []
        activity = []
        for name, config in sorted(configured.items()):
            unit_name = config.get("unit", "vllm-" + name + ".service")
            unit = units.get(unit_name) if units is not None else None
            active = unit["unit_active"] if unit is not None else (False if units is not None else None)
            sleeping = second.get("sleeping:" + name)
            health = second.get("health:" + name)
            swap_state = events.states.get(name) if events is not None else (
                running.get(name, "stopped") if running is not None else None)
            # Retain independently observed fault signals even on disagreement.
            failed_unit = unit is not None and unit.get("active_state") == "failed"
            state = "stopped" if active is False and not failed_unit else "unknown"
            if failed_unit:
                errors.append("unit:" + name + ": failed; fault cleanup required")
            if active is True and sleeping is not None and health is True:
                state = "sleeping" if sleeping else "awake"
            gpu = unit.get("gpu") if unit is not None else None
            util = unit.get("util") if unit is not None else None
            if util is None:
                util = config.get("util")
            total = next((g.total_gb for g in first.get("gpus", ()) if g.index == gpu), None)
            models.append(ModelState(
                name=name, state=state, gpu=gpu, util=util,
                budget_gb=total * util if total is not None and util is not None else None,
                weights_gb=config.get("weights_gb"), unit=unit_name, unit_active=active,
                health_ok=health, is_sleeping=sleeping, swap_state=swap_state,
                port=unit.get("port") if unit and unit.get("port") else config.get("port"),
                is_default=config.get("is_default", False),
                cold_start_seconds=config.get("cold_start_seconds")))
            row = first.get("activity", {}).get(name, {})
            activity.append(Activity(
                model=name, last_request_at=row.get("last_used"),
                requests_last_hour=row.get("requests_last_hour"),
                requests_last_10m=row.get("requests_last_10m"),
                in_flight=events.count(name) if events is not None else None,
                by=(row["source_container"],) if row.get("source_container") else ()))
        gpus, resident = second.get("ownership", (first.get("gpus", ()), {}))
        models = [replace(m, resident_gb=resident.get(m.name)) for m in models]
        sleeping_models = [m for m in models if m.state == "sleeping"]
        weights_known = all(m.state != "unknown" for m in models) and all(m.weights_gb is not None for m in sleeping_models)
        memory = MemoryState(first.get("memory"),
            sum(m.weights_gb for m in sleeping_models) if weights_known else None,
            self.memory_budget_gb, self.host_min_available_gb)
        if not self.activity_reader:
            errors.append("activity: not configured")
        return StateSnapshot(sampled_at=sampled_at, gpus=gpus, models=tuple(models),
            activity=tuple(activity), memory=memory, errors=tuple(errors))

    def _ownership(self, first):
        gpus = first.get("gpus", ())
        processes = first.get("processes")
        units = first.get("units")
        if processes is None or units is None:
            return gpus, {}
        output = []
        resident = {}
        for gpu in gpus:
            managed = 0.0
            external = []
            known = True
            for process in processes.get(gpu.uuid, ()):
                owner = self.probes.owner(process.pid, units)
                if process.used_gb is None:
                    known = False
                elif owner is not None:
                    managed += process.used_gb
                    resident[owner] = resident.get(owner, 0.0) + process.used_gb
                if owner is None:
                    external.append(process)
            # Used memory can include invisible foreign-container PIDs. Keep
            # unattributed memory reserved rather than treating an empty list
            # as an empty GPU. This conservative residual includes driver use.
            external_gb = max(0.0, gpu.used_gb - managed) if known and gpu.used_gb is not None else None
            output.append(replace(gpu, managed_gb=managed if known else None,
                                  external_gb=external_gb, external_processes=tuple(external)))
        return tuple(output), resident


def build_collector(config):
    """Core adapter for the nested ``collectors`` configuration mapping."""
    from llmsvc.activity import ActivityReader
    if not isinstance(config, dict):
        raise ValueError("collectors must be a mapping")
    allowed = {"models", "swap_url", "activity_path", "ip_containers", "deadline",
               "probe_timeout", "nvidia_smi", "systemctl", "proc_root", "host_meminfo_path",
               "memory_budget_gb", "host_min_available_gb"}
    if set(config) - allowed:
        raise ValueError("unknown collectors configuration keys")
    models = config.get("models", {})
    if not isinstance(models, dict) or any(not isinstance(v, dict) for v in models.values()):
        raise ValueError("collectors.models must map names to settings")
    from urllib.parse import urlsplit
    urls = [config.get("swap_url", "")] + [v["daemon_url"] for v in models.values() if "daemon_url" in v]
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("probe URLs must be HTTP(S) URLs without credentials or query")
        if "/upstream" in parts.path:
            raise ValueError("daemon probes must use direct endpoints, never upstream routing")
    timeout = config.get("probe_timeout", .5)
    if not isinstance(timeout, (int, float)) or not 0 < timeout < 1:
        raise ValueError("probe_timeout must be between zero and one second")
    probes = Probes(config["swap_url"], timeout=timeout,
                    **{k: config[k] for k in ("nvidia_smi", "systemctl", "proc_root", "host_meminfo_path") if k in config})
    reader = ActivityReader(config["activity_path"], config.get("ip_containers")) if config.get("activity_path") else None
    return Collector(models, swap_url=config["swap_url"], probes=probes, activity_reader=reader,
                     **{k: config[k] for k in ("deadline", "memory_budget_gb", "host_min_available_gb") if k in config})
