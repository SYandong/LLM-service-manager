# Generated-By: Codex / gpt-6-astra
"""Opt-in placement leases with protected, observed victim execution."""

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
    invocation_id: str = ""
    inactive: bool = False


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
                "--property=LoadState,ActiveState,MainPID,ControlGroup,Environment,InvocationID"],
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
            return UnitObservation(True, exited, values.get("ActiveState") == "active", token, values.get("InvocationID", ""),
                                   values.get("ActiveState") in ("inactive", "failed"))
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

    def _catalog_current(self):
        from llmsvc.actions import ActionDispatchError
        try:
            getattr(self.transport, "check_catalog", lambda: None)()
        except ActionDispatchError as exc:
            raise LeaseError(503, exc.reason) from exc

    def _enabled(self):
        self._catalog_current()
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
        if name not in getattr(self.transport, "active_models", self.transport.models):
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
        from llmsvc.actions import ActionDispatchError
        try:
            getattr(self.transport, "check_catalog", lambda: None)()
        except ActionDispatchError as exc:
            return None, (Blocker(request.name, exc.reason),)
        if self.scheduler.store is not None and self.scheduler.store.fault(request.name) is not None:
            return None, (Blocker(request.name, "fault_recovery_pending"),)
        if not self._fresh(snapshot):
            return None, (Blocker(request.name, "unknown_or_stale_snapshot"),)
        try:
            recovery_claim = self._recovery_context(request.name)
        except LeaseError as exc:
            return None, (Blocker(request.name, exc.error),)
        gpu_exclusions = None
        if recovery_claim is not None:
            request = replace(request, util=max(request.util or 0, recovery_claim.util_floor),
                              budget_gb=max(request.budget_gb or 0, recovery_claim.budget_floor_gb))
            gpu_exclusions = {recovery_claim.source_gpu: "relocation_source"}
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
        controller = self.scheduler.model_actions
        enabled = self.scheduler.config.model_actions_enabled and controller is not None
        guarded = {}
        # An unleased daemon has no durable retained budget during uncertain exit.
        # Adoption/orphan cleanup is a separate protocol, never a fabricated lease.
        confirmed = {lease.model for lease in snapshot.leases if lease.status == "confirmed"}
        models = []
        for model in snapshot.models:
            if model.name != request.name and model.state in ("awake", "sleeping"):
                unit = self.transport.units.get(model.name)
                if unit is None or model.unit != unit:
                    guarded[model.name] = "unmanaged_or_changed_unit"
                elif enabled and model.name not in confirmed:
                    guarded[model.name] = "unleased_model"
                elif enabled and controller.transport.units.get(model.name) != unit:
                    guarded[model.name] = "action_unit_mismatch"
                elif enabled and controller._fault_pending(model.name):
                    guarded[model.name] = "fault_recovery_pending"
                elif self.scheduler.store is not None and self.scheduler.store.recovery(model.name) is not None:
                    guarded[model.name] = "sleeping_recovery_pending"
                elif enabled and (model.name in controller.pending or controller.free_active):
                    guarded[model.name] = "operation_in_progress"
            models.append(replace(model, is_default=True)
                          if self.transport.models.get(model.name, {}).get("is_default") is True else model)
        protected = replace(snapshot, models=tuple(models))
        recovery_options = ({"gpu_exclusions": gpu_exclusions,
                             "settings": self.scheduler.sleeping_recovery.controller.settings}
                            if recovery_claim is not None else {})
        decision = plan_placement(protected, request, waiting=waiting, exclusions=guarded, **recovery_options)
        blockers = decision.blocked_by
        if any(action.kind != "place" for action in decision.actions) and not enabled:
            return None, blockers + (Blocker(request.name, "eviction_required", decision.gpu),)
        return decision, blockers

    def _recovery_context(self, model):
        claim = self.scheduler.store.recovery(model) if self.scheduler.store is not None else None
        if claim is None:
            return None
        recovery = getattr(self.scheduler, "sleeping_recovery", None)
        if recovery is None:
            raise LeaseError(503, "sleeping_recovery_pending")
        from llmsvc.actions import ActionDispatchError
        try:
            return recovery.placement_claim(model)
        except ActionDispatchError as exc:
            raise LeaseError(503, exc.reason) from exc

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
        with self._locked(deadline):
            if self._recovery_context(request.name) is not None:
                deadline = min(deadline, self.scheduler.sleeping_recovery.deadline)
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
                    action = decision.actions[0] if decision and decision.actions else None
                    if action is not None and action.kind == "place":
                        observation = self._inspect(request.name, deadline)
                        self._enabled()
                        latest, latest_blockers = self._decision(self.scheduler.snapshot(), request, waiting=waiting)
                        if latest is None or not latest.actions or latest.actions[0] != action:
                            blockers = latest_blockers or (Blocker(request.name, "placement_changed"),)
                            self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                                   max(0, deadline-self.monotonic())))
                            waiting = True
                            continue
                        decision = latest
                        if observation.exists is False and self.monotonic() < deadline:
                            lease = Lease(uuid.uuid4().hex, request.name, decision.gpu, payload["util"],
                                          self.scheduler.clock() + self.scheduler.config.lease_timeout_seconds,
                                          decision.budget_gb)
                            recovery_claim = self._recovery_context(request.name)
                            if recovery_claim is None:
                                self.scheduler.store.create_lease(lease, self.transport.unit_for_model(request.name))
                            else:
                                try:
                                    self.scheduler.store.create_lease(lease, self.transport.unit_for_model(request.name),
                                                                     recovery_claim=recovery_claim)
                                except ValueError as exc:
                                    raise LeaseError(503, "sleeping_recovery_changed") from exc
                            self.scheduler.emit("place", model=request.name, detail={**asdict(lease), "dry_run": False})
                            return {"gpu": lease.gpu, "lease_id": lease.lease_id}
                        blockers = (Blocker(request.name, "unit_exists" if observation.exists else "unit_state_unknown"),)
                    elif action is not None:
                        controller = self.scheduler.model_actions
                        # Pure placement currently emits coalesced direct stops.
                        # Never reinterpret a new policy action as a different effect.
                        if action.kind != "stop":
                            raise LeaseError(503, "placement_action_not_supported", (Blocker(action.model, action.kind),))
                        account = next((lease for lease in current.leases
                                        if lease.model == action.model and lease.status == "confirmed"), None)
                        identity = self._inspect(action.model, deadline)
                        if account is None or not identity.active or identity.lease_id != account.lease_id:
                            raise LeaseError(503, "placement_action_blocked",
                                                (Blocker(action.model, "unit_identity_unconfirmed", action.gpu),))
                        self._enabled()
                        latest, latest_blockers = self._decision(self.scheduler.snapshot(), request, waiting=waiting)
                        if latest is None or not latest.actions or latest.actions[0] != action:
                            blockers = latest_blockers or (Blocker(request.name, "placement_changed"),)
                            self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                                   max(0, deadline-self.monotonic())))
                            waiting = True
                            continue
                        controller.pending.add(action.model)
                        failure = None
                        from llmsvc.actions import ActionDispatchError
                        try:
                            controller.dispatcher.execute(action, dry_run=False, deadline=deadline)
                        except ActionDispatchError as exc:
                            failure = exc.reason
                        except Exception:
                            controller.pending.discard(action.model)
                            raise
                    if action is None or action.kind == "place":
                        self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                               max(0, deadline - self.monotonic())))
                        waiting = True
                        continue
                # Only one chosen action was submitted. Release the action lock
                # for sampling/reconciliation, then discard the old victim plan.
                try:
                    confirmed = self._observe_victim(action, min(deadline, self.monotonic()+self.scheduler.config.action_observe_seconds))
                    self.scheduler.emit("placement_action_result", model=action.model,
                        detail={"action": asdict(action), "confirmed": confirmed,
                                "error": failure if failure else (None if confirmed else "exit_unconfirmed"),
                                "dry_run": False})
                    if failure or not confirmed:
                        outcome = (Blocker(action.model, failure or "exit_unconfirmed", action.gpu),)
                        if self.monotonic() >= deadline:
                            raise LeaseError(409, "placement_timeout", outcome)
                        raise LeaseError(503, "placement_action_failed" if failure else "placement_no_progress", outcome)
                finally:
                    with self.scheduler.changed:
                        controller.pending.discard(action.model)
                        self.scheduler.changed.notify_all()
                # The next iteration revalidates policy, protection, RAM and all
                # allocations before another action or the final durable grant.
        except LeaseError as exc:
            if exc.error != "placement_timeout":
                raise
            blockers = exc.blockers or blockers
        raise LeaseError(409, "placement_timeout", blockers)

    def _observe_victim(self, action, deadline, *, reconcile_exit=False):
        """Observe exit/account release in two newer rounds within the deadline.

        Request sampling outside the lock. Reserve may additionally reconcile
        its stopped account with one bounded unit probe per published round;
        readiness waits still release the lock and never wait on collector I/O.
        """
        effect_seen_at = None
        checked_generation = None
        self.scheduler.request_sample()  # Recollect even after an uncertain late submission.
        try:
            while self.monotonic() < deadline and not self.scheduler.stopping.is_set():
                self.scheduler.request_sample()
                with self._locked(deadline):
                    if reconcile_exit and action.kind == "stop" and checked_generation != self.scheduler._sample_published:
                        checked_generation = self.scheduler._sample_published
                        self._reconcile_action_exit(action, deadline)
                    snapshot = self.scheduler.snapshot()
                    active_account = any(lease.model == action.model and lease.status != "released" for lease in snapshot.leases)
                    account_ok = (any(lease.model == action.model and lease.status == "confirmed" for lease in snapshot.leases)
                                  if action.kind == "sleep" else not active_account)
                    effect = (self._fresh(snapshot) and account_ok
                              and self.scheduler.model_actions._effect(action, snapshot))
                    if effect and effect_seen_at is not None and snapshot.sampled_at > effect_seen_at:
                        return True
                    effect_seen_at = snapshot.sampled_at if effect else None
                    self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                           max(0, deadline-self.monotonic())))
        except LeaseError as exc:
            if exc.error != "placement_timeout":
                raise
        return False

    def _reconcile_action_exit(self, action, deadline):
        """Reconcile only an explicitly stopped account, even with place disabled.

        The caller holds action_lock. This never adopts an orphan, confirms a
        loading model, expires other leases, or enables the placement API.
        """
        if self.scheduler.config.read_only or self.scheduler.store is None or self.scheduler.store.read_only:
            return
        if self.scheduler.store.fault(action.model) is not None:
            return
        if self.scheduler.store.recovery(action.model) is not None:
            return
        rows = [(lease, unit) for lease, unit in self.scheduler.store.leases()
                if lease.model == action.model and lease.status == "confirmed"]
        if len(rows) != 1:
            return
        lease, unit = rows[0]
        if unit != self.transport.units.get(action.model) or lease.gpu != action.gpu:
            return
        observation = self._inspect(action.model, deadline)
        if self.monotonic() < deadline and self._exited(lease, unit, observation):
            self._transition(lease, "released")

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
        self._catalog_current()
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
            self._enabled()
            row = self.scheduler.store.lease(lease_id)
            if row is None:
                raise LeaseError(404, "unknown_lease")
            lease, unit = row
            if self.scheduler.store.fault(lease.model) is not None:
                raise LeaseError(503, "fault_recovery_pending")
            if lease.status == "released":
                if operation == "confirm":
                    raise LeaseError(409, "lease_revoked")
                return {"lease_id": lease_id, "status": "released"}
            recovery = self.scheduler.store.recovery(lease.model)
            if recovery is not None and lease.lease_id != recovery.destination_lease_id:
                raise LeaseError(503, "sleeping_recovery_pending")
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
                faults = getattr(self.scheduler, "faults", None)
                if self.scheduler.store.fault(lease.model) is not None or (faults is not None and faults.hold_account(lease)):
                    continue
                recovery = self.scheduler.store.recovery(lease.model)
                if recovery is not None and lease.lease_id != recovery.destination_lease_id:
                    continue
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
