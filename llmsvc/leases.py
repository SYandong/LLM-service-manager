# Generated-By: Codex / gpt-6-astra
"""Opt-in persisted placement admission and bounded waits (no eviction executor)."""

import math
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace

from llmsvc.policy import plan_placement
from llmsvc.scheduler import IntentWriteError
from llmsvc.state import Blocker, Lease, ModelState
from llmsvc.store import finite_positive, nonempty


@dataclass(frozen=True)
class UnitObservation:
    exists: object = None
    exited: bool = False
    active: bool = False
    lease_id: str = ""


class LeaseError(IntentWriteError):
    def __init__(self, status, error, blockers=()):
        super().__init__(status, error)
        self.blockers = tuple(blockers)


class LeaseUnitProbe:
    """Read only, bounded systemctl inspection of configured managed units.

    Empty/failed/malformed inspection is unknown. MainPID=0 alone is insufficient:
    a loaded unit must be inactive AND have no control group before release.
    """

    def __init__(self, transport, *, monotonic=time.monotonic):
        self.transport = transport
        self.monotonic = monotonic

    def __call__(self, model, *, deadline):
        unit = self.transport.unit_for_model(model)
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            return UnitObservation()
        try:
            result = self.transport.run([self.transport.systemctl, "show", unit,
                "--property=LoadState,ActiveState,MainPID,ControlGroup,Environment"],
                capture_output=True, text=True, check=False, timeout=remaining)
            if result.returncode != 0 or len(result.stdout) > 65536:
                return UnitObservation()
            values = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if not separator or key in values:
                    return UnitObservation()
                values[key] = value
            if values.get("LoadState") == "not-found":
                absent = (values.get("ActiveState") == "inactive" and values.get("MainPID") == "0"
                          and values.get("ControlGroup") == "")
                return UnitObservation(False, True) if absent else UnitObservation()
            if values.get("LoadState") != "loaded":
                return UnitObservation()
            environment = shlex.split(values.get("Environment", ""))
            tokens = [item.split("=", 1)[1] for item in environment if item.startswith("LLMSVC_LEASE_ID=")]
            token = tokens[0] if len(tokens) == 1 else ""
            exited = (values.get("ActiveState") in ("inactive", "failed")
                      and values.get("MainPID") == "0" and values.get("ControlGroup") == "")
            return UnitObservation(True, exited, values.get("ActiveState") == "active", token)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return UnitObservation()


class PlacementController:
    def __init__(self, scheduler, transport, *, probe=None, monotonic=time.monotonic):
        self.scheduler = scheduler
        self.transport = transport
        self.monotonic = monotonic
        self.probe = probe or LeaseUnitProbe(transport, monotonic=monotonic)
        self._reconcile_cursor = 0
        self._configuration_alerts = set()
        self.recovered = {lease.lease_id for lease, _ in scheduler.store.leases()} if scheduler.store else set()

    @contextmanager
    def _locked(self, deadline, *, timeout_status=409):
        remaining = deadline - self.monotonic()
        if remaining <= 0 or not self.scheduler.action_lock.acquire(timeout=remaining):
            raise LeaseError(timeout_status, "placement_timeout" if timeout_status == 409 else "lease_observation_timeout")
        try:
            yield
        finally:
            self.scheduler.action_lock.release()

    def _enabled(self):
        config = self.scheduler.config
        if config.read_only:
            raise LeaseError(405, "read_only")
        if not config.placement_enabled:
            raise LeaseError(405, "operation_not_enabled")
        if self.scheduler.store is None or self.scheduler.store.read_only:
            raise LeaseError(503, "intent_store_unavailable")

    def _fresh(self, snapshot):
        value = snapshot.sampled_at
        return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                and 0 <= self.scheduler.clock() - value <= self.scheduler.config.max_snapshot_age_seconds
                and not snapshot.errors)

    def _inspect(self, model, deadline):
        return self.probe(model, deadline=min(deadline, self.monotonic() + self.scheduler.config.lease_probe_seconds))

    def _request(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("place body must be an object")
        self.scheduler._keys(payload, {"model", "util"}, required={"model", "util"})
        name = nonempty(payload["model"], "model")
        util = finite_positive(payload["util"], "util")
        if util > 1:
            raise ValueError("util must not exceed one")
        if name not in self.transport.models:
            raise LeaseError(404, "unknown_model")
        metadata = self.transport.models[name]
        configured_util = metadata.get("util", util)
        if not 0 < finite_positive(configured_util, "configured util") <= 1:
            raise ValueError("invalid configured util")
        # The launcher starts with the requested util. Charge at least the trusted
        # configured minimum, without silently changing the launcher's payload.
        return ModelState(name=name, state="stopped", util=max(util, configured_util),
                          budget_gb=metadata.get("budget_gb"), weights_gb=metadata.get("weights_gb"),
                          is_default=metadata.get("is_default") is True)

    def _decision(self, snapshot, request, *, waiting):
        if not self._fresh(snapshot):
            return None, (Blocker(request.name, "unknown_or_stale_snapshot"),)
        # Cold admission needs trusted host headroom even when no eviction is needed.
        available = snapshot.memory.host_available_gb
        weight = request.weights_gb
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
               for v in (available, weight)):
            return None, (Blocker(request.name, "unknown_memory"),)
        # Unobserved pending starts must not all spend the same host headroom.
        pending_weight = 0.0
        for lease in snapshot.leases:
            if lease.status not in ("pending", "stale"):
                continue
            amount = self.transport.models.get(lease.model, {}).get("weights_gb")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                return None, (Blocker(lease.model, "unknown_memory"),)
            pending_weight += amount
        if available - weight - pending_weight < snapshot.memory.host_min_available_gb:
            return None, (Blocker(request.name, "host_memory_floor"),)
        decision = plan_placement(snapshot, request, waiting=waiting)
        if any(action.kind != "place" for action in decision.actions):
            return None, decision.blocked_by + (Blocker(request.name, "eviction_required", decision.gpu),)
        return decision, decision.blocked_by

    def preview(self, operation, payload):
        # Deliberately no collector, process probe, ID allocation, event or writer.
        with self.scheduler.action_lock:
            if operation == "place":
                request = self._request(payload)
                decision, blockers = self._decision(self.scheduler.snapshot(), request, waiting=False)
                return {"would": [asdict(a) for a in decision.actions] if decision else [],
                        "blocked_by": [asdict(b) for b in blockers],
                        "budget_gb": decision.budget_gb if decision else None}
            row = self.scheduler.store.lease(payload["lease_id"]) if self.scheduler.store else None
            if row is None:
                raise LeaseError(404, "unknown_lease")
            if operation == "confirm" and row[0].status == "released":
                raise LeaseError(409, "lease_revoked")
            unchanged = row[0].status == ("confirmed" if operation == "confirm" else "released")
            return {"would": [] if unchanged else [{"kind": operation, "lease_id": row[0].lease_id}],
                    "blocked_by": [], "requires_observation": not unchanged}

    def place(self, payload):
        self._enabled()
        request = self._request(payload)
        deadline = self.monotonic() + self.scheduler.config.placement_wait_seconds
        waiting = False
        blockers = ()
        try:
            while self.monotonic() < deadline and not self.scheduler.stopping.is_set():
                # Consume published collector rounds; do not start a potentially
                # slow sample inside the request's single 120-second wait budget.
                self.reconcile(deadline=deadline)
                with self._locked(deadline):
                    self._enabled()
                    current = self.scheduler.snapshot()
                    if any(lease.model == request.name for lease in current.leases if lease.status != "released"):
                        raise LeaseError(409, "outstanding_lease", (Blocker(request.name, "outstanding_lease"),))
                    decision, blockers = self._decision(current, request, waiting=waiting)
                    if decision and decision.actions:
                        # A short bounded existence probe is part of the final locked
                        # admission, not a readiness wait. No unit start is issued here.
                        observation = self._inspect(request.name, deadline)
                        if observation.exists is False and self.monotonic() < deadline:
                            lease = Lease(uuid.uuid4().hex, request.name, decision.gpu, payload["util"],
                                          self.scheduler.clock() + self.scheduler.config.lease_timeout_seconds,
                                          decision.budget_gb)
                            self.scheduler.store.create_lease(lease, self.transport.unit_for_model(request.name))
                            self.scheduler.emit("place", model=request.name, detail={**asdict(lease), "dry_run": False})
                            return {"gpu": lease.gpu, "lease_id": lease.lease_id}
                        blockers = (Blocker(request.name, "unit_exists" if observation.exists else "unit_state_unknown"),)
                    self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                           max(0, deadline - self.monotonic())))
                    waiting = True
        except LeaseError as exc:
            if exc.error != "placement_timeout":
                raise
        raise LeaseError(409, "placement_timeout", blockers)

    def _healthy(self, lease, unit, observation):
        snapshot = self.scheduler._snapshot
        models = [model for model in snapshot.models if model.name == lease.model]
        return (self._fresh(snapshot) and len(models) == 1 and observation.active
                and observation.lease_id == lease.lease_id and models[0].unit == unit
                and models[0].gpu == lease.gpu and models[0].unit_active is True
                and models[0].health_ok is True and models[0].state in ("awake", "sleeping"))

    def _exited(self, lease, unit, observation):
        snapshot = self.scheduler._snapshot
        models = [model for model in snapshot.models if model.name == lease.model]
        return (observation.exited and self._fresh(snapshot) and len(models) == 1
                and models[0].unit == unit and models[0].unit_active is False and models[0].state == "stopped")

    def _transition(self, lease, status):
        self.scheduler.store.transition_lease(lease.lease_id, status)
        self.scheduler.emit("lease_" + status, model=lease.model,
                            detail={"lease_id": lease.lease_id, "status": status, "dry_run": False})
        self.recovered.discard(lease.lease_id)
        self._configuration_alerts = {item for item in self._configuration_alerts if item[0] != lease.lease_id}

    def finish(self, operation, lease_id):
        self._enabled()
        deadline = self.monotonic() + self.scheduler.config.request_timeout_seconds
        self.scheduler.sample_once()
        with self._locked(deadline, timeout_status=503):
            row = self.scheduler.store.lease(lease_id)
            if row is None:
                raise LeaseError(404, "unknown_lease")
            lease, unit = row
            if lease.status == "released":
                if operation == "confirm":
                    raise LeaseError(409, "lease_revoked")
                return {"lease_id": lease_id, "status": "released"}
            if operation == "confirm" and lease.status == "confirmed":
                return {"lease_id": lease_id, "status": "confirmed"}
            if unit != self.transport.units.get(lease.model):
                raise LeaseError(503, "configured_unit_mismatch")
            observation = self._inspect(lease.model, deadline)
            if self.monotonic() >= deadline:
                raise LeaseError(503, "lease_observation_timeout")
            if operation == "confirm":
                if not self._healthy(lease, unit, observation):
                    # 409 is reserved for revoked/superseded leases: launcher must
                    # not stop a still-loading valid lease on an uncertain probe.
                    raise LeaseError(503, "lease_not_ready")
                status = "confirmed"
            else:
                if not self._exited(lease, unit, observation):
                    raise LeaseError(503, "lease_exit_unconfirmed")
                status = "released"
            self._transition(lease, status)
            return {"lease_id": lease_id, "status": status}

    def reconcile(self, *, deadline=None):
        """Reconcile bounded observations with current publication and row checks.

        Recovery uses the same persisted row as daemon accounting; confirmation
        changes status atomically rather than creating a second allocation.
        """
        if self.scheduler.config.read_only or not self.scheduler.config.placement_enabled:
            return
        deadline = deadline if deadline is not None else self.monotonic() + self.scheduler.config.lease_probe_seconds
        with self._locked(deadline):
            rows = self.scheduler.store.leases()
            if rows:
                offset = self._reconcile_cursor % len(rows)
                rows = rows[offset:] + rows[:offset]
        for lease, unit in rows:
            if self.monotonic() >= deadline:
                break
            with self._locked(deadline):
                self._reconcile_cursor += 1
                if self.scheduler.store.lease(lease.lease_id) != (lease, unit):
                    continue
                generation = self.scheduler._sample_published
            configured_unit = self.transport.units.get(lease.model)
            alert = (lease.lease_id, unit, configured_unit)
            if unit != configured_unit:
                with self._locked(deadline):
                    if alert not in self._configuration_alerts:
                        self.scheduler.emit("lease_configuration_required", model=lease.model,
                            detail={"lease_id": lease.lease_id, "persisted_unit": unit,
                                    "configured_unit": configured_unit,
                                    "reason": "missing_or_changed_trusted_identity",
                                    "next_action": "restore_verified_model_configuration",
                                    "budget_retained": True})
                        self._configuration_alerts.add(alert)
                continue  # A persisted unit name does not authorize probing it.
            with self._locked(deadline):
                self._configuration_alerts = {item for item in self._configuration_alerts if item[0] != lease.lease_id}
            observation = self._inspect(lease.model, deadline)
            if self.monotonic() >= deadline:
                break
            with self._locked(deadline):
                # Do not apply an older probe after a newer sample or transition.
                if generation != self.scheduler._sample_published or self.scheduler.store.lease(lease.lease_id) != (lease, unit):
                    continue
                expired = lease.expires_at <= self.scheduler.clock()
                recovered = lease.lease_id in self.recovered
                if (expired or recovered or lease.status in ("confirmed", "stale")) and self._exited(lease, unit, observation):
                    self._transition(lease, "released")
                elif lease.status != "confirmed" and self._healthy(lease, unit, observation):
                    self._transition(lease, "confirmed")
                elif lease.status == "pending" and (expired or recovered):
                    self._transition(lease, "stale")


def accounting_snapshot(snapshot, rows):
    """Merge measured daemons and durable rows conservatively by model name."""
    models = list(snapshot.models)
    errors = list(snapshot.errors)
    for lease, unit in rows:
        found = [i for i, model in enumerate(models) if model.name == lease.model]
        if len(found) != 1:
            errors.append("lease_model_unobserved:" + lease.model)
            continue
        index = found[0]
        model = models[index]
        if model.state in ("awake", "sleeping"):
            if model.gpu != lease.gpu or model.unit != unit:
                errors.append("lease_accounting_conflict:" + lease.model)
            else:
                budget = model.budget_gb
                if isinstance(budget, bool) or not isinstance(budget, (float, int)) or not math.isfinite(budget):
                    budget = lease.budget_gb
                models[index] = replace(model, budget_gb=max(budget, lease.budget_gb))
        elif lease.status == "confirmed":
            # A sampled stopped state is not proof that all unit processes exited.
            errors.append("daemon_exit_unconfirmed:" + lease.model)
    return replace(snapshot, models=tuple(models), leases=tuple(lease for lease, _ in rows), errors=tuple(errors))
