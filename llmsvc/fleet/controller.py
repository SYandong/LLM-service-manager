# Generated-By: Codex / gpt-6.1-sol
"""Fleet lifecycle and API operations, independent of model-action permission."""

import json
import logging
import math
import sqlite3
import threading
import uuid

from llmsvc.config import canonical_ip
from llmsvc.fleet import FleetError
from llmsvc.fleet.ingest import number, read_json, validate_snapshot
from llmsvc.fleet.store import FleetStore
from llmsvc.policy.fleet import container_summary, service_status

LOG = logging.getLogger("llmsvc.fleet")


class FleetController:
    def __init__(self, config, *, clock, emit):
        self.config = config
        self.clock = clock
        self.emit = emit
        self.store = FleetStore(config.fleet_db_path)
        self.stopping = threading.Event()
        self.thread = None
        self.last_error = None
        self._statuses = {}
        self._publication_lock = threading.RLock()

    def start(self):
        if self.thread is not None or self.stopping.is_set():
            raise RuntimeError("fleet_worker_already_started_or_stopped")
        self.thread = threading.Thread(target=self._run, name="llmsvc-fleet", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("fleet_worker_did_not_stop")
        self.store.close()

    def _run(self):
        while not self.stopping.is_set():
            self.ingest_once()
            self.stopping.wait(self.config.fleet_ingest_interval_seconds)

    def ingest_once(self):
        try:
            payload = validate_snapshot(read_json(self.config.fleet_snapshot_path))
            now = self.clock()
            if payload["generated_at"] > now + 5:
                raise ValueError("future_fleet_snapshot")
            if now - payload["generated_at"] <= self.config.fleet_stale_after_seconds:
                self.store.ingest(payload, self.config, now)
            self.last_error = None
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
            self.last_error = "fleet_snapshot_unavailable"
        self._publish_statuses()

    def _publish_statuses(self):
        with self._publication_lock:
            self._publish_statuses_locked()

    def _publish_statuses_locked(self):
        try:
            payload = self.report()
        except FleetError:
            return
        statuses = {row["id"]: row["status"] for row in payload["services"]}
        previous = self._statuses
        self._statuses = statuses
        # Publish after releasing the fleet lock; model/action code never waits
        # for SQLite with the scheduler lock held in the opposite order.
        for service_id, status in statuses.items():
            old = previous.get(service_id)
            if old is not None and old != status:
                self.emit("fleet_status_changed", detail={"service_id": service_id, "from": old, "to": status})

    def owner_for_ip(self, source_ip, *, required=True):
        """Re-read the trusted host export for every request; no identity fallback."""
        owner = None
        try:
            source = canonical_ip(source_ip)
            path = self.config.collectors.get("ip_containers_path")
            if path is None:
                raise ValueError("missing_identity_export")
            payload = read_json(path, 1024 * 1024)
            now = self.clock()
            generated = payload.get("generated_at")
            containers = payload.get("containers")
            if not number(generated) or not 0 <= now - generated <= self.config.fleet_stale_after_seconds or not isinstance(containers, dict):
                raise ValueError("stale_identity_export")
            normalized = {}
            for address, name in containers.items():
                address = canonical_ip(address)
                if not isinstance(name, str) or not name or len(name) > 512 or any(ord(c) < 32 for c in name):
                    raise ValueError("invalid_identity_export")
                if address in normalized and normalized[address] != name:
                    raise ValueError("ambiguous_identity_export")
                normalized[address] = name
            owner = normalized.get(source)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            owner = None
        if owner is None and required:
            raise FleetError(403, "unmapped_container")
        return owner

    def report(self, *, source_ip=None, mine=False, shared=None):
        now = self.clock()
        owner = self.owner_for_ip(source_ip, required=mine) if source_ip is not None else None
        try:
            with self.store.lock:
                meta = self.store.metadata()
                generated = meta.get("generated_at")
                snapshot = meta.get("snapshot", {})
                age = None if generated is None else max(0, now - generated)
                stale = generated is None or now < generated or age > self.config.fleet_stale_after_seconds or self.last_error is not None
                instances = self.store.instances()
                claims = self.store.claims(now)
                windows = {"24h": self.store.window(now - 86400, now + 0.000001),
                           "7d": self.store.window(now - 604800, now + 0.000001)}
                hour = math.floor(now / 3600) * 3600
                start_hour = hour - 23 * 3600
                hourly = {}
                for point in self.store.hourly(start_hour, hour + 3600):
                    if point["observed_seconds"] > 0:
                        hourly.setdefault(point["instance_id"], [None] * 24)[int((point["hour_ts"] - start_hour) / 3600)] = min(60, point["active_minutes"])
                services = [service_status(instance, claims.get(instance["id"]), self.config, now,
                    stale=stale, generated_at=generated, hourly=hourly.get(instance["id"]),
                    windows={key: value.get(instance["id"]) for key, value in windows.items()},
                    mine=owner is not None and not instance["host"] and owner == instance["container"])
                    for instance in instances if not mine or instance["container"] == owner]
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise FleetError(503, "fleet_store_unavailable") from exc
        gpus = self._gpus(snapshot, services, mine=mine)
        errors = list(snapshot.get("errors", []))
        if self.last_error is not None:
            errors.append(self.last_error)
        if generated is None:
            errors.append("fleet_not_sampled")
        return {"schema_version": 1, "generated_at": generated, "snapshot_age_seconds": age, "stale": stale,
                "config": {"idle_limit_hours": self.config.fleet_idle_limit_hours,
                           "active_window_seconds": self.config.fleet_active_window_seconds},
                "gpus": gpus, "containers": container_summary(services), "services": services,
                "llmsvc_shared": {"models_loaded": shared or []}, "errors": errors,
                "inventory_complete": snapshot.get("inventory_complete", False),
                "gpu_inventory_complete": snapshot.get("gpu_inventory_complete", False),
                "gpu_attribution_complete": snapshot.get("gpu_attribution_complete", False),
                "claims_enabled": self.config.fleet_claims_enabled}

    @staticmethod
    def _gpus(snapshot, services, *, mine):
        occupants = {}
        visible = {service["id"] for service in services}
        for service in snapshot.get("services", []):
            if service["id"] not in visible:
                continue
            for gpu in service["gpus"]:
                occupants.setdefault(gpu["index"], []).append({"container": service["container"], "kind": "llm",
                    "used_gb": None if gpu.get("used_mib") is None else gpu["used_mib"] / 1024, "service_id": service["id"]})
        owners = {service["container"] for service in services}
        for process in snapshot.get("other_gpu_processes", []):
            if mine and process["container"] not in owners:
                continue
            occupants.setdefault(process["gpu"], []).append({"container": process["container"], "kind": "other",
                "used_gb": None if process.get("used_mib") is None else process["used_mib"] / 1024, "service_id": None})
        return [{"index": gpu["index"], "uuid": gpu.get("uuid"),
                 "total_gb": None if gpu.get("total_mib") is None else gpu["total_mib"] / 1024,
                 "used_gb": None if gpu.get("used_mib") is None else gpu["used_mib"] / 1024,
                 "util_percent": gpu.get("util_percent"), "occupants": occupants.get(gpu["index"], [])}
                for gpu in snapshot.get("gpus", [])]

    def history(self, instance_id, hours):
        if hours not in (24, 168) or not isinstance(instance_id, str) or not instance_id:
            raise FleetError(400, "invalid_fleet_history_query")
        now = self.clock()
        try:
            with self.store.lock:
                instance = self.store.instance(instance_id)
                if instance is None:
                    raise FleetError(404, "service_not_found")
                if hours == 24:
                    rows = self.store.raw(instance_id, now - hours * 3600, now)
                    for row in rows:
                        row["active_minutes"] = row["active_seconds"] / 60 if row["observed_seconds"] > 0 else None
                    resolution = "minute"
                else:
                    window_start = now - hours * 3600
                    start = math.floor(window_start / 3600) * 3600
                    last_hour = math.floor(now / 3600) * 3600
                    points = {row["hour_ts"]: row for row in self.store.hourly(start, now + 1, instance_id=instance_id)}
                    rows = []
                    for offset in range(int((last_hour - start) / 3600) + 1):
                        hour = start + offset * 3600
                        row = points.get(hour, {"hour_ts": hour, "active_minutes": None, "requests": None,
                            "gen_tokens": None, "prompt_tokens": None, "cached_tokens": None, "samples": 0, "observed_seconds": 0})
                        row["ts"] = hour
                        lower, upper = max(hour, window_start), min(hour + 3600, now)
                        if lower != hour or upper != hour + 3600:
                            counts = self.store.window(lower, upper + 0.000001).get(instance_id, {})
                            row.update({key: counts.get(key) for key in ("active_minutes", "requests", "gen_tokens", "prompt_tokens", "cached_tokens")})
                            row["observed_seconds"] = counts.get("observed_seconds", 0)
                        row["coverage_ratio"] = min(1, row["observed_seconds"] / (upper - lower)) if upper > lower else None
                        row["partial"] = upper - lower < 3600
                        if row["observed_seconds"] == 0:
                            row["active_minutes"] = None
                        rows.append(row)
                    resolution = "hourly"
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise FleetError(503, "fleet_store_unavailable") from exc
        service = {key: instance[key] for key in ("id", "container", "engine", "model", "started_at", "first_seen", "last_seen", "ended_at", "argv_redacted")}
        return {"schema_version": 1, "service_id": instance_id, "hours": hours,
                "start_at": now - hours * 3600, "end_at": now,
                "resolution": resolution, "samples": rows, "service": service}

    def write_claim(self, operation, payload, *, source_ip, dry_run=False):
        if not self.config.fleet_claims_enabled:
            raise FleetError(405, "fleet_claims_disabled")
        owner = self.owner_for_ip(source_ip)
        now = self.clock()
        if not isinstance(payload, dict):
            raise FleetError(400, "invalid_claim")
        try:
            with self.store.lock:
                if self.stopping.is_set():
                    raise FleetError(503, "fleet_stopping")
                if operation == "claim":
                    # Compatibility identity labels are ignored; only socket peer
                    # attribution above determines ownership of a declaration.
                    if set(payload) - {"service_id", "until", "reason", "container", "by", "created_by_container", "source_ip"}:
                        raise FleetError(400, "invalid_claim")
                    if not isinstance(payload.get("service_id"), str) or not 1 <= len(payload["service_id"]) <= 512:
                        raise FleetError(400, "invalid_claim")
                    instance = self.store.instance(payload.get("service_id"))
                    if instance is None:
                        raise FleetError(404, "service_not_found")
                    if instance["container"] != owner or instance["host"]:
                        raise FleetError(403, "forbidden_container")
                    meta = self.store.metadata()
                    generated = meta.get("generated_at")
                    if (instance["ended_at"] is not None or generated is None or self.last_error is not None
                            or not 0 <= now - generated <= self.config.fleet_stale_after_seconds or instance["last_seen"] != generated):
                        raise FleetError(409, "service_observation_unknown")
                    until = payload.get("until")
                    reason = payload.get("reason")
                    if (not number(until) or not now < until <= now + self.config.fleet_claim_max_days * 86400
                            or not isinstance(reason, str) or not 1 <= len(reason) <= 200 or not reason.strip()
                            or any(ord(c) < 32 for c in reason)):
                        raise FleetError(400, "invalid_claim")
                    claim = {"instance_id": instance["id"], "service_id": instance["id"], "container": owner, "model": instance["model"],
                             "until": until, "reason": reason.strip(), "created_by_container": owner,
                             "created_at": now, "revoked_at": None}
                    if not dry_run:
                        claim["id"] = uuid.uuid4().hex
                        self.store.put_claim(claim)
                elif operation == "unclaim":
                    if set(payload) != {"id"} or not isinstance(payload["id"], str) or not 1 <= len(payload["id"]) <= 512:
                        raise FleetError(400, "invalid_claim")
                    claim = self.store.claim(payload.get("id"))
                    if claim is None:
                        raise FleetError(404, "claim_not_found")
                    if claim["container"] != owner or claim["created_by_container"] != owner:
                        raise FleetError(403, "forbidden_container")
                    claim["revoked_at"] = claim["revoked_at"] or now
                    if not dry_run:
                        self.store.revoke_claim(claim["id"], now)
                else:
                    raise FleetError(400, "invalid_claim")
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise FleetError(503, "fleet_store_unavailable") from exc
        LOG.info(json.dumps({"kind": "fleet_" + operation, "dry_run": bool(dry_run),
                             "container": owner, "service_id": claim["instance_id"]}, allow_nan=False))
        if not dry_run:
            self._publish_statuses()
        return {"ok": True, "dry_run": bool(dry_run), "claim": claim}
