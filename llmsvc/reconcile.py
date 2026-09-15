# Generated-By: OpenCode / deepseek-v4.1-flash
"""Directory-driven model registration and unregistration, one action per tick.

The shared-root scan is the source of truth: a ``pending`` candidate is
submitted through the existing registry/catalog transaction, and a record whose
descriptor disappeared (or now names a different model) is stopped and then
unregistered. Only models the scheduler itself registered carry a
``metadata.llmsvc_registry`` record; hand-written models are never touched.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import time
from pathlib import Path

from llmsvc.registry import RegistryError
from llmsvc.reload import ReloadError, reload_blockers
from llmsvc.scheduler import IntentWriteError

LOG = logging.getLogger("llmsvc.reconcile")

FIRST_BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0


class DirectoryReconciler:
    """Submit at most one add/stop/remove per ``run_once`` call."""

    def __init__(self, scheduler, registry, *, interval_seconds, clock=time.monotonic):
        if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
                or not math.isfinite(interval_seconds) or not 0 < interval_seconds <= MAX_BACKOFF_SECONDS):
            raise ValueError("interval_seconds must be a finite number in (0, 3600]")
        self.scheduler = scheduler
        self.registry = registry
        self.interval_seconds = float(interval_seconds)
        self.clock = clock
        self.last = None
        self._last_submission = None
        self._last_scan = None
        self._backoff: dict[str, dict] = {}

    # --------------------------------------------------------------- run_once

    def run_once(self) -> dict:
        name = None
        try:
            blocked = self._precondition()
            if blocked is not None:
                return self._finish({"action": None, "model": None, "reason": blocked}, log=False)
            self._last_scan = self.clock()
            rows = self._discovered_rows()
            configured = {row["name"] for row in rows if row["status"] == "configured"}
            for stale in [key for key in self._backoff if key in configured]:
                del self._backoff[stale]
            for row in rows:
                if row["status"] == "pending" and not self._in_backoff(row["name"]):
                    name = row["name"]
                    self.registry.add({"import": name})
                    self._last_submission = self.clock()
                    self._set_backoff(name, None)
                    return self._finish({"action": "add", "model": name, "reason": None}, log=True)
            return self._reconcile_orphans(rows)
        except (RegistryError, ReloadError, IntentWriteError, OSError, ValueError) as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)
            if name is not None:
                self._last_submission = self.clock()
                self._set_backoff(name, reason)
            return self._finish({"action": None, "model": name, "reason": reason}, log=True)

    def _precondition(self) -> str | None:
        scheduler = self.scheduler
        catalog = scheduler.catalog
        if catalog is None or not catalog.can_submit():
            return "catalog_unavailable"
        if self.registry.submit_change is not catalog.submit_change:
            return "catalog_not_connected"
        if scheduler.catalog_fenced:
            return "catalog_reconciliation_required"
        if scheduler.store is None or scheduler.store.catalog_pending() is not None:
            return "catalog_pending"
        if self.registry.queue.fenced:
            return "registry_reconciliation_required"
        if any(job.get("pending") for job in self.registry.queue.queue_snapshot()["jobs"]):
            return "pending_change"
        if catalog.busy:
            return "catalog_busy"
        if self.registry.discover is None:
            return "discovery_unconfigured"
        if self._last_submission is not None and self.clock() - self._last_submission < self.interval_seconds:
            return "interval"
        if self._last_scan is not None and self.clock() - self._last_scan < self.interval_seconds:
            return "interval"
        return None

    def _discovered_rows(self) -> list:
        with self.scheduler.action_lock:
            names = [row["name"] for row in self.registry.inventory()["models"]]
        return self.registry.discovered(names)

    def _reconcile_orphans(self, rows) -> dict:
        records = self.registry.records()
        for stale in [key for key in self._backoff if key not in records]:
            del self._backoff[stale]
        for name, record in records.items():
            if self._in_backoff(name):
                continue
            why = self._orphan_reason(record, rows)
            if why is None:
                continue
            snapshot = self.scheduler.snapshot()
            blockers = reload_blockers(snapshot, self.clock(), self.scheduler.config.max_snapshot_age_seconds)
            if blockers:
                return self._finish({"action": None, "model": name, "reason": "snapshot_blocked"}, log=False)
            model = next((item for item in snapshot.models if item.name == name), None)
            if model is None or model.state not in ("stopped", "awake", "sleeping"):
                return self._finish({"action": None, "model": name, "reason": "unknown_model_state"}, log=False)
            if model.state == "stopped":
                self.registry.remove(name)
                self._last_submission = self.clock()
                self._set_backoff(name, None)
                return self._finish({"action": "remove", "model": name, "reason": None}, log=True)
            actions = self.scheduler.model_actions
            if actions is None:
                return self._finish({"action": None, "model": name, "reason": "model_actions_disabled"}, log=False)
            if actions.pending:
                return self._finish({"action": None, "model": name, "reason": "operation_in_progress"}, log=False)
            outcome = actions.stop_model(name, by="reconcile")
            reason = self._stop_reason(outcome)
            self._last_submission = self.clock()
            self._set_backoff(name, reason)
            return self._finish({"action": "stop", "model": name, "reason": reason}, log=True)
        return self._finish({"action": None, "model": None, "reason": None}, log=False)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _stop_reason(outcome) -> str | None:
        if isinstance(outcome, dict):
            blocked = outcome.get("blocked_by")
            if blocked:
                return ", ".join(str(item.get("reason")) for item in blocked)
            if outcome.get("error"):
                return str(outcome["error"])
        return None

    @staticmethod
    def _orphan_reason(record, rows) -> str | None:
        """Why a registered record no longer matches its directory, or None.

        A descriptor that fails to parse is not an orphan: the model stays
        registered and the ``invalid`` discovery row carries the reason, so a
        typo while editing ``llmsvc.json`` never unregisters a model.
        """
        directory = os.path.normpath(str(record.get("path")))
        path = Path(directory) / "llmsvc.json"
        try:
            info = os.lstat(path)
        except OSError:
            return "the descriptor llmsvc.json is missing"
        if not stat.S_ISREG(info.st_mode):
            return "the descriptor llmsvc.json is not a regular file"
        same_directory = [row for row in rows if os.path.normpath(str(row.get("path"))) == directory]
        if any(row.get("status") == "invalid" for row in same_directory):
            return None
        if any(row.get("status") == "configured" and row.get("name") == record.get("name") for row in same_directory):
            return None
        if any(row.get("status") in ("pending", "configured") for row in same_directory):
            return "the descriptor now declares a different model name"
        return None

    def _in_backoff(self, name: str) -> bool:
        entry = self._backoff.get(name)
        return entry is not None and self.clock() < entry["until"]

    def _set_backoff(self, name: str, reason) -> None:
        entry = self._backoff.get(name)
        attempts = (entry["attempts"] if entry is not None else 0) + 1
        delay = min(FIRST_BACKOFF_SECONDS * (2 ** (attempts - 1)), MAX_BACKOFF_SECONDS)
        self._backoff[name] = {"until": self.clock() + delay, "reason": reason, "attempts": attempts}

    def _finish(self, result: dict, *, log: bool) -> dict:
        self.last = result
        if log:
            LOG.info(json.dumps({"kind": "model_reconcile", **result}, allow_nan=False))
            self.scheduler.emit("model_reconcile", model=result.get("model"), detail=result)
        return result

    # --------------------------------------------------------------- annotate

    def annotate(self, rows) -> list:
        annotated = [dict(row) for row in rows]
        for row in annotated:
            if row.get("status") == "pending":
                entry = self._backoff.get(row["name"])
                if entry is not None:
                    row["reason"] = entry.get("reason") or "submitted; waiting for the configuration transaction"
        try:
            records = self.registry.records()
        except (RegistryError, ReloadError, OSError, ValueError):
            return annotated
        try:
            snapshot = self.scheduler.snapshot()
        except Exception:
            snapshot = None
        for name, record in sorted(records.items()):
            why = self._orphan_reason(record, rows)
            if why is None:
                continue
            model = next((item for item in snapshot.models if item.name == name), None) if snapshot is not None else None
            if model is None or model.state not in ("stopped", "awake", "sleeping"):
                outcome = "waiting for a known runtime state before unregistering"
            elif model.state == "stopped":
                outcome = "it will be unregistered"
            else:
                outcome = "the model is stopped first, then unregistered"
            annotated.append({
                "name": name,
                "path": record.get("path"),
                "base": record.get("base"),
                "util": record.get("util"),
                "weights_gb": record.get("weights_gb"),
                "status": "orphaned",
                "reason": why + "; " + outcome,
            })
        return annotated
