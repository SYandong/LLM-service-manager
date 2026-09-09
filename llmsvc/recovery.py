# Generated-By: Codex / gpt-6-astra
"""Default-off ordinary sleeping retirement/relocation with durable fences."""

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, replace
from urllib.parse import quote

from llmsvc.actions import ActionDispatchError, ModelActionController, _known
from llmsvc.leases import PlacementController
from llmsvc.policy import Decision, plan_relocation, plan_sleeping_recovery
from llmsvc.state import Action, Blocker, RecoveryClaim

LOG = logging.getLogger("llmsvc.sleeping_recovery")


def _invocation(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value) else ""


class SleepingRecoveryController:
    def __init__(self, scheduler, *, accounting=None, monotonic=time.monotonic):
        self.scheduler = scheduler
        self.controller = scheduler.model_actions
        self.monotonic = monotonic
        self.accounting = accounting or scheduler.placement
        if self.accounting is None and scheduler.automation is not None:
            self.accounting = scheduler.automation.accounting
        if self.accounting is None and self.controller is not None:
            self.accounting = PlacementController(scheduler, self.controller.transport, monotonic=monotonic)
        self.active = False
        self.deadline = 0.0
        self._owner_thread = None
        self._claim_id = None
        self._permission = None
        self._stop_boundary = None
        self._proxy_boundary = None
        self._wake_boundary = None

    def enabled(self):
        c = self.scheduler.config
        return (getattr(self.scheduler, "sleeping_recovery", None) is self
                and c.sleeping_recovery_enabled and c.automation_enabled and c.model_actions_enabled
                and not c.read_only and self.controller is not None and self.accounting is not None
                and self.scheduler.store is not None and not self.scheduler.store.read_only)

    def _guard(self):
        if not self.enabled():
            raise ActionDispatchError("sleeping_recovery_disabled")
        if self.scheduler.stopping.is_set():
            raise ActionDispatchError("scheduler_stopping")
        if self.monotonic() >= self.deadline:
            raise ActionDispatchError("deadline_exceeded")

    def _profile_hash(self, model):
        t = self.controller.transport
        unit = t.unit_for_model(model)
        if sum(value == unit for value in t.units.values()) != 1:
            raise ActionDispatchError("ambiguous_unit")
        value = {"metadata": t.models[model], "unit": unit, "origin": t.swap_url, "systemctl": t.systemctl,
                 "policy": asdict(self.controller.settings)}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _claim(self, model=None):
        claims = self.scheduler.store.recoveries()
        claim = next((item for item in claims if item.id == self._claim_id and (model is None or item.model == model)), None)
        if claim is None:
            raise ActionDispatchError("sleeping_recovery_changed")
        if self.scheduler.store.fault(claim.model) is not None:
            raise ActionDispatchError("fault_recovery_pending")
        if claim.profile_hash != self._profile_hash(claim.model):
            raise ActionDispatchError("recovery_profile_changed")
        source = self.scheduler.store.lease(claim.source_lease_id)
        expected = "confirmed" if claim.stage == "claimed" else "released"
        if (source is None or (source[0].model, source[0].gpu, source[0].status, source[1]) !=
                (claim.model, claim.source_gpu, expected, claim.unit)
                or source[0].util > claim.util_floor or source[0].budget_gb > claim.budget_floor_gb):
            raise ActionDispatchError("recovery_source_account_changed")
        return claim

    def authorized(self, model, *, action=False):
        if not (self.active and self._owner_thread == threading.get_ident()
                and self._permission == ("stop" if action else "wake")):
            return False
        try:
            self._guard()
            claim = self._claim(model)
            return claim.stage == "claimed" if action else claim.stage in ("settled", "waking", "destination")
        except (ActionDispatchError, ValueError, KeyError):
            return False

    def _exclusions(self, snapshot, *, owned=None):
        confirmed = {lease.model for lease in snapshot.leases if lease.status == "confirmed"}
        result = {}
        t = self.controller.transport
        for model in snapshot.models:
            if model.state not in ("awake", "sleeping"):
                continue
            if model.name not in t.models or model.unit != t.units.get(model.name):
                result[model.name] = "unmanaged_or_changed_unit"
            elif model.name not in confirmed:
                result[model.name] = "unleased_model"
            elif model.unit_active is not True or model.health_ok is not True or model.is_sleeping is not (model.state == "sleeping"):
                result[model.name] = "model_state_changed"
            elif model.is_default is None:
                result[model.name] = "unknown_model_role"
            elif self.controller._fault_pending(model.name):
                result[model.name] = "fault_recovery_pending"
            elif model.name in self.controller.pending or self.controller.free_active:
                result[model.name] = "operation_in_progress"
            elif self.scheduler.store.recovery(model.name) is not None and model.name != owned:
                result[model.name] = "sleeping_recovery_pending"
            else:
                activity = next((a for a in snapshot.activity if a.model == model.name), None)
                if activity is not None and type(activity.requests_last_hour) is int and activity.requests_last_hour > 0:
                    if not _known(t.models[model.name].get("weights_gb")):
                        result[model.name] = "unknown_runtime_profile"
        return result

    def plan(self, snapshot, *, claim=None):
        if self.controller is None or self.accounting is None:
            return Decision(blocked_by=(Blocker(None, "recovery_executor_unavailable"),))
        if not self.controller._fresh(snapshot):
            return Decision(blocked_by=(Blocker(None, "unknown_or_stale_snapshot"),))
        try:
            self.controller.transport.bounded_origin()
        except (ValueError, TypeError):
            return Decision(blocked_by=(Blocker(None, "unsupported_recovery_origin"),))
        exclusions = self._exclusions(snapshot, owned=claim.model if claim else None)
        replacements = self._replacement_requests(snapshot)
        if claim is not None:
            return plan_relocation(snapshot, model=claim.model, reason=claim.reason,
                                   settings=self.controller.settings, exclusions=exclusions,
                                   replacement_requests=replacements)
        return plan_sleeping_recovery(snapshot, settings=self.controller.settings, exclusions=exclusions,
                                      replacement_requests=replacements)

    def _replacement_requests(self, snapshot):
        """Describe actual core cold admission without altering source accounting."""
        models = {model.name: model for model in snapshot.models}
        leases = {lease.model: lease for lease in snapshot.leases if lease.status != "released"}
        requests = {}
        for name, metadata in self.controller.transport.models.items():
            model, lease = models.get(name), leases.get(name)
            values = [value for value in (getattr(model, "util", None), getattr(lease, "util", None), metadata.get("util"))
                      if _known(value) and 0 < value <= 1]
            if not values:
                continue
            try:
                request = self.accounting._request({"model": name, "util": max(values)})
                if request.budget_gb is not None and (not _known(request.budget_gb) or request.budget_gb <= 0):
                    continue
                budgets = [value for value in (request.budget_gb, getattr(model, "budget_gb", None), getattr(lease, "budget_gb", None))
                           if _known(value) and value > 0]
                requests[name] = replace(request, budget_gb=max(budgets) if budgets else None)
            except (ValueError, KeyError):
                continue  # Supplied-map policy reports missing/unknown profile; never fall back to None.
        return requests

    def _fresh_round(self, deadline=None):
        deadline = min(self.deadline, deadline if deadline is not None else self.deadline)
        with self.controller._locked(deadline):
            previous = self.scheduler._sample_started
        self.scheduler.request_sample()
        while self.monotonic() < deadline:
            with self.controller._locked(deadline):
                self._guard()
                if self.scheduler._sample_published > previous:
                    bounds = self.scheduler._sample_bounds
                    if (not self.scheduler._sample_source_time_provided or bounds is None
                            or bounds[0] != self.scheduler._sample_published or bounds[2] < bounds[1]):
                        raise ActionDispatchError("unknown_collection_provenance")
                    return self.controller._snapshot()
                self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                       max(0, deadline-self.monotonic())))
        raise ActionDispatchError("deadline_exceeded")

    def _boundary(self):
        # Collection interval comparisons stay in scheduler's monotonic domain;
        # source sampled_at is checked independently in the wall-clock domain.
        return self.scheduler.monotonic(), self.scheduler.clock()

    def _post_boundary(self, boundary):
        bounds = self.scheduler._sample_bounds
        raw = self.scheduler._snapshot
        return (boundary is not None and bounds is not None and bounds[0] == self.scheduler._sample_published
                and bounds[1] >= boundary[0] and bounds[2] >= bounds[1]
                and self.scheduler._sample_source_time_provided
                and _known(raw.sampled_at) and raw.sampled_at >= boundary[1]
                and self.accounting._fresh(raw))

    def _source_absent(self, claim, deadline):
        source = self.scheduler.store.lease(claim.source_lease_id)
        if source is None or (source[0].model, source[0].gpu, source[1]) != (claim.model, claim.source_gpu, claim.unit):
            return False
        generation = self.scheduler._sample_published
        observation = self.accounting._inspect(claim.model, deadline)
        self._guard()
        if self._claim(claim.model) != claim:
            return False
        return (self.monotonic() < deadline and generation == self.scheduler._sample_published
                and self.accounting._exited(source[0], claim.unit, observation))

    def _ordinary_protection(self, claim):
        snapshot = self.controller._snapshot()
        if not self.controller._fresh(snapshot):
            raise ActionDispatchError("unknown_or_stale_snapshot")
        models = [model for model in snapshot.models if model.name == claim.model]
        if len(models) != 1 or models[0].unit != claim.unit:
            raise ActionDispatchError("configured_unit_mismatch")
        model = models[0]
        if model.is_default is not False:
            raise ActionDispatchError("default_or_unknown_role")
        if any(pin.model == claim.model and (not _known(pin.until) or pin.until > self.scheduler.clock()) for pin in snapshot.pins):
            raise ActionDispatchError("pinned")
        rows = [a for a in snapshot.activity if a.model == claim.model]
        if len(rows) != 1 or type(rows[0].in_flight) is not int or rows[0].in_flight != 0:
            raise ActionDispatchError("in_flight_or_unknown")
        if claim.reason == "reserve" and not any(r.gpu == claim.source_gpu and _known(r.until)
                                                 and r.until > self.scheduler.clock() for r in snapshot.reserves):
            raise ActionDispatchError("reserve_not_active")
        return snapshot, model

    def before_source_action(self, action):
        self._guard()
        claim = self._claim(action.model)
        if action.kind != "stop" or action.gpu != claim.source_gpu or claim.stage != "claimed" or claim.stop_submitted:
            raise ActionDispatchError("recovery_action_not_authorized")
        observation = self.accounting._inspect(claim.model, self.deadline)
        self._guard()
        if self._claim(action.model) != claim:
            raise ActionDispatchError("sleeping_recovery_changed")
        snapshot = self.controller._snapshot()
        current = self.plan(snapshot, claim=claim)
        replacement = self._replacement_requests(snapshot).get(claim.model)
        if (not observation.active or observation.lease_id != claim.source_lease_id
                or _invocation(observation.invocation_id) != claim.invocation_id or not current.actions
                or current.actions[0] != action or any(a.kind == "place" for a in current.actions) != claim.relocate
                or claim.relocate and (replacement is None or replacement.util > claim.util_floor
                                      or (replacement.budget_gb or 0) > claim.budget_floor_gb)):
            raise ActionDispatchError("recovery_plan_or_identity_changed")
        self.scheduler.store.advance_recovery(claim, stop_submitted=True)
        self._guard()
        self._claim(action.model)
        self._stop_boundary = self._boundary()

    def _observe_source(self, deadline, *, failure=None):
        seen = None
        while self.monotonic() < deadline:
            self._fresh_round(deadline)
            with self.controller._locked(deadline):
                self._guard()
                claim = self._claim()
                valid = self._post_boundary(self._stop_boundary) and self._source_absent(claim, deadline)
                if valid:
                    if claim.stage == "claimed":
                        claim = self.scheduler.store.advance_recovery(claim, stage="released", error=failure)
                        self.scheduler.emit("lease_released", model=claim.model,
                            detail={"lease_id": claim.source_lease_id, "status": "released", "dry_run": False})
                    now = self.scheduler._snapshot.sampled_at
                    if seen is not None and now > seen:
                        return True
                    seen = now
                else:
                    seen = None
        return False

    def _cleanup_proxy(self):
        with self.controller._locked(self.deadline):
            self._guard()
            claim = self._claim()
            if claim.stage != "released" or not claim.stop_acknowledged or claim.proxy_submitted:
                raise ActionDispatchError("recovery_proxy_not_authorized")
            # Source is stopped now; only normal protection remains relevant.
            self._ordinary_protection(claim)
            if not self._source_absent(claim, self.deadline):
                raise ActionDispatchError("source_exit_unconfirmed")
            submit = self.controller.transport.prepare_http("POST", "/api/models/unload/"+quote(claim.model, safe=""), bounded=True)
            claim = self.scheduler.store.advance_recovery(claim, proxy_submitted=True)
            self._guard()
            self._claim(claim.model)
            self._proxy_boundary = self._boundary()
        status = submit(deadline=self.deadline)
        with self.controller._locked(self.deadline):
            self._guard()
            claim = self._claim()
            if type(status) is not int or not 200 <= status < 300 or not self._source_absent(claim, self.deadline):
                raise ActionDispatchError("proxy_cleanup_unconfirmed")
            self.scheduler.store.advance_recovery(claim, proxy_acknowledged=True)
        seen = None
        while self.monotonic() < self.deadline:
            self._fresh_round()
            with self.controller._locked(self.deadline):
                self._guard()
                claim = self._claim()
                raw = self.scheduler._snapshot
                model = next((m for m in raw.models if m.name == claim.model), None)
                valid = (self._post_boundary(self._proxy_boundary) and model is not None and model.swap_state == "stopped"
                         and self._source_absent(claim, self.deadline))
                if valid:
                    if seen is not None and raw.sampled_at > seen:
                        self.scheduler.store.advance_recovery(claim, stage="settled")
                        return
                    seen = raw.sampled_at
                else:
                    seen = None
        raise ActionDispatchError("proxy_cleanup_unconfirmed")

    def placement_claim(self, model):
        self._guard()
        claim = self._claim(model)
        if not self.active or self._permission != "wake" or not claim.wake_submitted or claim.stage not in ("waking", "destination"):
            raise ActionDispatchError("sleeping_recovery_pending")
        if claim.stage == "destination":
            row = self.scheduler.store.lease(claim.destination_lease_id)
            if row is None or row[0].status == "released":
                raise ActionDispatchError("recovery_destination_lost")
            return claim  # Existing outstanding-lease check still rejects a second grant.
        snapshot = self.controller._snapshot()
        model_state = next((m for m in snapshot.models if m.name == model), None)
        if (not self.controller._fresh(snapshot) or model_state is None or model_state.is_default is not False
                or any(p.model == model and (not _known(p.until) or p.until > self.scheduler.clock()) for p in snapshot.pins)):
            raise ActionDispatchError("recovery_protection_changed")
        activity = [a for a in snapshot.activity if a.model == model]
        if len(activity) != 1 or type(activity[0].in_flight) is not int:
            raise ActionDispatchError("unknown_in_flight")
        # The triggering cold request may itself be the one in flight. There is
        # no stop here: only the ordinary fully-accounted launcher lease grant.
        if activity[0].in_flight < 0:
            raise ActionDispatchError("unknown_in_flight")
        return claim

    def before_wake_request(self, model, deadline):
        with self.controller._locked(deadline):
            self._guard()
            claim = self._claim(model)
            self._ordinary_protection(claim)
            if claim.stage != "settled" or not claim.relocate or claim.wake_submitted or not self.scheduler.config.placement_enabled:
                raise ActionDispatchError("recovery_wake_not_authorized")
            if not self._source_absent(claim, deadline):
                raise ActionDispatchError("source_exit_unconfirmed")
            submit = self.controller.transport.prepare_http("GET", "/upstream/"+quote(model, safe="")+"/", bounded=True)
            self.scheduler.store.advance_recovery(claim, stage="waking", wake_submitted=True)
            self._guard()
            self._claim(model)
            self._wake_boundary = self._boundary()
            return submit

    def refresh_for_wake(self, deadline):
        return self._fresh_round(deadline)

    def after_wake_request(self, model, error, deadline):
        with self.controller._locked(deadline):
            self._guard()
            claim = self._claim(model)
            if error:
                raise ActionDispatchError(error)
            if claim.stage != "destination" or not claim.destination_lease_id:
                raise ActionDispatchError("recovery_destination_unbound")
            observation = self.accounting._inspect(model, deadline)
            self._guard()
            if self._claim(model) != claim:
                raise ActionDispatchError("sleeping_recovery_changed")
            if (not observation.active or observation.lease_id != claim.destination_lease_id
                    or not _invocation(observation.invocation_id)
                    or _invocation(observation.invocation_id) == claim.invocation_id):
                raise ActionDispatchError("destination_identity_unconfirmed")
            self.scheduler.store.advance_recovery(claim, wake_acknowledged=True,
                                                 destination_invocation_id=_invocation(observation.invocation_id))

    def _destination_ready(self, claim):
        row = self.scheduler.store.lease(claim.destination_lease_id) if claim.destination_lease_id else None
        if row is None or row[0].status != "confirmed" or row[0].gpu == claim.source_gpu or row[1] != claim.unit:
            return False
        generation = self.scheduler._sample_published
        observation = self.accounting._inspect(claim.model, self.deadline)
        self._guard()
        if self._claim(claim.model) != claim:
            return False
        raw = self.scheduler._snapshot
        model = next((m for m in raw.models if m.name == claim.model), None)
        return (generation == self.scheduler._sample_published and self.accounting._fresh(raw)
                and observation.active and observation.lease_id == claim.destination_lease_id
                and _invocation(observation.invocation_id) == claim.destination_invocation_id and model is not None
                and model.is_default is False and model.gpu == row[0].gpu and model.unit == claim.unit
                and ModelActionController._ready(model))

    def _observe_destination(self):
        previous_time = None
        for _ in range(2):
            with self.controller._locked(self.deadline):
                self._guard()
                claim = self._claim()
                before = self.accounting._inspect(claim.model, self.deadline)
                self._guard()
                if (not before.active or before.lease_id != claim.destination_lease_id
                        or _invocation(before.invocation_id) != claim.destination_invocation_id):
                    return False
                boundary = self._boundary()
            self._fresh_round()
            with self.controller._locked(self.deadline):
                claim = self._claim()
                sampled_at = self.scheduler._snapshot.sampled_at
                if (not self._post_boundary(boundary) or not self._destination_ready(claim)
                        or previous_time is not None and sampled_at <= previous_time):
                    return False
                previous_time = sampled_at
        return True

    def _observe_existing_claim(self, claim, result):
        """Resume evidence/accounting only; never replay a persisted transport."""
        self._claim_id = claim.id
        result["observations_only"] = True
        claim = self._claim(claim.model)
        if claim.stage == "claimed" and not claim.stop_submitted:
            self.scheduler.store.advance_recovery(claim, stage="complete", error="aborted_before_submission")
            result.update(status="blocked", error="aborted_before_submission")
            return result
        if claim.stage == "destination" and claim.wake_acknowledged:
            if self._observe_destination():
                claim = self._claim()
                self.scheduler.store.advance_recovery(claim, stage="complete")
                lease = self.scheduler.store.lease(claim.destination_lease_id)[0]
                result.update(status="relocated", ready=True, source_released=True,
                              destination_gpu=lease.gpu, lease_id=lease.lease_id)
                result.pop("error", None)
            return result
        boundary = self._boundary()
        previous_time = None
        for round_index in range(2):
            self._fresh_round(min(self.deadline, self.monotonic()+self.scheduler.config.action_observe_seconds))
            with self.controller._locked(self.deadline):
                self._guard()
                claim = self._claim()
                sampled_at = self.scheduler._snapshot.sampled_at
                if not self._post_boundary(boundary) or previous_time is not None and sampled_at <= previous_time:
                    return result
                previous_time = sampled_at
                if claim.stage == "claimed" and claim.stop_submitted:
                    if self._source_absent(claim, self.deadline):
                        claim = self.scheduler.store.advance_recovery(claim, stage="released", error=claim.error)
                        self.scheduler.emit("lease_released", model=claim.model,
                            detail={"lease_id": claim.source_lease_id, "status": "released", "dry_run": False})
                        result.update(stage=claim.stage, source_released=True, status="partial")
                    return result
                if claim.stage in ("released", "settled") and claim.proxy_acknowledged:
                    model = next((m for m in self.scheduler._snapshot.models if m.name == claim.model), None)
                    if model is None or model.swap_state != "stopped" or not self._source_absent(claim, self.deadline):
                        return result
                    if round_index:
                        if claim.stage == "released":
                            claim = self.scheduler.store.advance_recovery(claim, stage="settled")
                        result["stage"] = claim.stage
                        if not claim.relocate:
                            self.scheduler.store.advance_recovery(claim, stage="complete")
                            result.update(status="retired", source_released=True)
                            result.pop("error", None)
                        return result
                else:
                    return result
        return result

    def run_once(self, *, dry_run=False):
        if dry_run:
            with self.scheduler.action_lock:
                decision = self.plan(self.controller._snapshot() if self.controller else self.scheduler.snapshot())
            result = {"would": [asdict(a) for a in decision.actions], "blocked_by": [asdict(b) for b in decision.blocked_by]}
            LOG.info(json.dumps({"kind": "sleeping_recovery_preview", "dry_run": True, **result}, allow_nan=False))
            return result
        if not self.enabled():
            return {"status": "disabled", "ready": False}
        result = {"status": "blocked", "ready": False, "source_released": False, "destination_gpu": None}
        started = self.monotonic()
        deadline = started + self.scheduler.config.sleeping_recovery_timeout_seconds
        try:
            with self.controller._locked(deadline):
                if self.active or (self.scheduler.automation is not None and self.scheduler.automation.active):
                    result["error"] = "cycle_in_progress"
                    return result
                self.deadline = deadline
                self.active = True
                self._owner_thread = threading.get_ident()
                self._guard()
                pending = self.scheduler.store.recoveries()
                if pending:
                    source = self.scheduler.store.lease(pending[0].source_lease_id)
                    result.update(model=pending[0].model, claim_id=pending[0].id, stage=pending[0].stage,
                                  source_lease_id=pending[0].source_lease_id,
                                  source_released=source is not None and source[0].status == "released",
                                  status="partial" if pending[0].stop_submitted else "blocked",
                                  error="recovery_requires_settlement")
                    return self._observe_existing_claim(pending[0], result)
            snapshot = self._fresh_round()
            with self.controller._locked(self.deadline):
                self._guard()
                decision = self.plan(snapshot)
                result["blocked_by"] = [asdict(b) for b in decision.blocked_by]
                if not decision.actions:
                    result["status"] = "blocked" if decision.blocked_by else "idle"
                    return result
                action = decision.actions[0]
                relocate = any(a.kind == "place" for a in decision.actions)
                if action.kind != "stop" or (relocate and not self.scheduler.config.placement_enabled):
                    raise ActionDispatchError("recovery_placement_unavailable")
                model = next(m for m in snapshot.models if m.name == action.model)
                row = next(((lease, unit) for lease, unit in self.scheduler.store.leases()
                            if lease.model == action.model and lease.status == "confirmed"), None)
                observation = self.accounting._inspect(action.model, self.deadline)
                self._guard()
                if (row is None or row[1] != model.unit or not observation.active or observation.lease_id != row[0].lease_id
                        or not _invocation(observation.invocation_id)):
                    raise ActionDispatchError("source_identity_unconfirmed")
                source_boundary = self._boundary()
            # Bind the decision's fields to a stable source unit incarnation
            # across an actual subsequent collection interval, not an old sample
            # followed by a probe of a replacement process.
            self._fresh_round()
            with self.controller._locked(self.deadline):
                self._guard()
                generation = self.scheduler._sample_published
                after = self.accounting._inspect(action.model, self.deadline)
                self._guard()
                if (generation != self.scheduler._sample_published or not self._post_boundary(source_boundary)
                        or not after.active or after.lease_id != observation.lease_id
                        or _invocation(after.invocation_id) != _invocation(observation.invocation_id)):
                    raise ActionDispatchError("source_identity_changed")
                current_snapshot = self.controller._snapshot()
                current = self.plan(current_snapshot)
                if not current.actions or current.actions[0] != action:
                    raise ActionDispatchError("recovery_plan_changed")
                if any(a.kind == "place" for a in current.actions) != relocate:
                    raise ActionDispatchError("recovery_plan_changed")
                model = next(m for m in current_snapshot.models if m.name == action.model)
                row = self.scheduler.store.lease(row[0].lease_id)
                if row is None or row[0].status != "confirmed" or row[0].model != model.name:
                    raise ActionDispatchError("recovery_source_account_changed")
                replacement = self._replacement_requests(current_snapshot).get(model.name)
                if relocate and replacement is None:
                    raise ActionDispatchError("unknown_runtime_profile")
                util_floor = replacement.util if replacement else row[0].util
                budget_floor = max(row[0].budget_gb, model.budget_gb, (replacement.budget_gb or 0) if replacement else 0)
                claim = RecoveryClaim(uuid.uuid4().hex, model.name, row[0].lease_id, model.unit, _invocation(observation.invocation_id),
                    model.gpu, action.reason, self.scheduler.clock(), util_floor,
                    budget_floor, self._profile_hash(model.name), relocate)
                self.scheduler.store.claim_recovery(claim)
                self._claim_id = claim.id
                result.update(model=claim.model, claim_id=claim.id, source_gpu=claim.source_gpu,
                              source_lease_id=claim.source_lease_id)
                self._permission = "stop"
                failure = None
                try:
                    self.controller.dispatcher.execute(action, dry_run=False, deadline=self.deadline)
                    self._guard()
                    self.scheduler.store.advance_recovery(self._claim(), stop_acknowledged=True)
                except ActionDispatchError as exc:
                    failure = exc.reason
                finally:
                    self._permission = None
            claim = self._claim()
            if not claim.stop_submitted:
                raise ActionDispatchError(failure or "source_stop_not_submitted")
            confirmed = self._observe_source(min(self.deadline, self.monotonic()+self.scheduler.config.action_observe_seconds), failure=failure)
            if failure or not confirmed:
                raise ActionDispatchError(failure or "source_exit_unconfirmed")
            self._cleanup_proxy()
            with self.controller._locked(self.deadline):
                claim = self._claim()
                if not claim.relocate:
                    self._guard()
                    if not self._source_absent(claim, self.deadline):
                        raise ActionDispatchError("source_exit_unconfirmed")
                    self.scheduler.store.advance_recovery(claim, stage="complete")
                    result.update(status="retired", source_released=True)
                    return result
                self._permission = "wake"
            try:
                wake = self.controller.wake(claim.model, by="sleeping_recovery", _recovery=self, _deadline=self.deadline)
            finally:
                with self.scheduler.action_lock:
                    self._permission = None
            if wake.get("ready") and not self._observe_destination():
                raise ActionDispatchError("destination_readiness_unconfirmed")
            with self.controller._locked(self.deadline):
                self._guard()
                claim = self._claim()
                if not wake.get("ready") or not claim.wake_acknowledged or not self._destination_ready(claim):
                    raise ActionDispatchError(wake.get("error", "destination_readiness_unconfirmed"))
                self.scheduler.store.advance_recovery(claim, stage="complete")
                lease = self.scheduler.store.lease(claim.destination_lease_id)[0]
                result.update(status="relocated", ready=True, source_released=True,
                              destination_gpu=lease.gpu, lease_id=lease.lease_id)
        except Exception as exc:
            error = exc.reason if isinstance(exc, ActionDispatchError) else "recovery_execution_failed"
            result["error"] = error
            result["status"] = "timeout" if error == "deadline_exceeded" else "blocked"
            if self._claim_id:
                with self.scheduler.action_lock:
                    try:
                        claim = self._claim()
                        if claim.stage == "claimed" and not claim.stop_submitted and self.enabled():
                            self.scheduler.store.advance_recovery(claim, stage="complete", error=error)
                        else:
                            result.update(stage=claim.stage, source_released=claim.stage != "claimed")
                            if claim.stop_submitted:
                                result["status"] = "partial"
                    except Exception:
                        result["status"] = "partial"
        finally:
            with self.scheduler.changed:
                if self._owner_thread == threading.get_ident():
                    self.active = False
                    self._owner_thread = self._claim_id = self._permission = None
                if result.get("source_lease_id"):
                    try:
                        row = self.scheduler.store.lease(result["source_lease_id"])
                        result["source_released"] = row is not None and row[0].status == "released"
                    except Exception:
                        result["source_released"] = None
                result["elapsed_seconds"] = max(0.0, self.monotonic()-started)
                self.scheduler.emit("sleeping_recovery_result", model=result.get("model"), detail=result)
                self.scheduler.changed.notify_all()
        return result
