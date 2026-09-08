# Generated-By: Codex / gpt-6-astra
"""Proof-gated managed fault recovery, separate from ordinary eviction."""

import math
from dataclasses import dataclass
from typing import Optional


MAX_EVIDENCE_GAP_SECONDS = 2.0
SLEEP_MISMATCH_SECONDS = 10.0


def known(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def valid_invocation(value):
    return (isinstance(value, str) and len(value) == 32
            and all(char in "0123456789abcdefABCDEF" for char in value)
            and any(char != "0" for char in value))


@dataclass(frozen=True)
class FaultProof:
    model: str
    lease_id: str
    unit: str
    invocation_id: str
    gpu: int
    reason: str
    sampled_at: float
    received_at: float
    generation: int
    epoch: int
    samples: int
    round_started_at: float = 0.0
    window_lower_bound_seconds: Optional[float] = None


@dataclass
class _Evidence:
    key: tuple
    sampled_at: float
    received_at: float
    generation: int
    started_at: float
    finished_at: float
    epoch: int = 0
    served: bool = False
    eligible: bool = False
    health: object = None
    sleeping: object = None
    swap: object = None
    failures: int = 0
    mismatch_at: object = None
    mismatch_received: object = None
    mismatch_samples: int = 0

    def reset_counts(self):
        self.failures = 0
        self.mismatch_at = None
        self.mismatch_received = None
        self.mismatch_samples = 0


class FaultDetector:
    """Bounded sampled evidence, with no clocks, I/O, transport or persistence.

    A confirmed lease alone is not prior-service proof. New incarnations must
    first be observed serving healthily; expected stops clear that lifecycle.
    Intentional sleep disarms it until a known warm wake or observed new swap
    starting/ready transition. Gaps/identity loss discard the entire window.
    """

    def __init__(self, *, health_failures=3):
        if type(health_failures) is not int or health_failures < 3:
            raise ValueError("health_failures must be an integer >= 3")
        self.health_failures = health_failures
        self.records = {}

    def invalidate(self, model, *, stopped=False):
        record = self.records.get(model)
        if record is not None:
            record.epoch += 1
            record.eligible = False
            if stopped:
                record.served = False
            record.reset_counts()

    def warm_wake(self, model, lease_id, *, now):
        record = self.records.get(model)
        if (record is None or record.key[0] != lease_id or record.health is not True
                or record.sleeping is not True or not known(now)
                or not 0 <= now-record.received_at <= MAX_EVIDENCE_GAP_SECONDS):
            return False
        record.eligible = True
        record.epoch += 1
        record.reset_counts()
        return True

    def observe(self, snapshot, lease, unit_name, observation, *, generation, received_at, wall_now,
                started_at, finished_at, expected=False):
        name = lease.model
        sample = snapshot.sampled_at
        models = [model for model in snapshot.models if model.name == name]
        if (lease.status != "confirmed" or len(models) != 1 or not known(sample)
                or not known(received_at) or not known(wall_now) or not known(started_at) or not known(finished_at)
                or not started_at <= finished_at <= received_at
                or not 0 <= received_at-started_at <= MAX_EVIDENCE_GAP_SECONDS
                or not 0 <= wall_now-sample <= MAX_EVIDENCE_GAP_SECONDS or expected):
            self.records.pop(name, None)
            return None
        model = models[0]
        previous = self.records.get(name)
        if model.unit != unit_name or sum(item.unit == unit_name for item in snapshot.models) != 1:
            self.records.pop(name, None)
            return None
        absent = observation.exists is False and observation.exited and model.unit_active is False
        if absent:
            if previous is None or previous.key[:2] != (lease.lease_id, unit_name) or previous.key[3] != lease.gpu:
                return None
            key = previous.key
        else:
            if (observation.exists is not True or not valid_invocation(observation.invocation_id)
                    or observation.lease_id != lease.lease_id
                    or (observation.active and (model.unit_active is not True or model.gpu != lease.gpu))
                    or (not observation.active and not observation.inactive and not observation.exited)):
                self.records.pop(name, None)
                return None
            key = (lease.lease_id, unit_name, observation.invocation_id, lease.gpu)
        continuous = (previous is not None and previous.key == key
                      and generation > previous.generation and sample > previous.sampled_at
                      and started_at >= previous.received_at
                      and 0 < received_at-previous.received_at
                      and received_at-previous.started_at <= MAX_EVIDENCE_GAP_SECONDS
                      # Compare elapsed differences, never absolute timestamps
                      # from different clock domains. A wall step outside the
                      # actual collection-interval bounds invalidates the window.
                      and started_at-previous.finished_at <= sample-previous.sampled_at
                      <= finished_at-previous.started_at)
        record = previous if continuous else _Evidence(key, sample, received_at, generation, started_at, finished_at)
        record.epoch += 1
        reason = None
        samples = 0
        span = None
        if model.unit_active is False and (observation.exited or observation.inactive):
            if continuous and record.eligible:
                reason, samples = "unexpected_unit_exit", 1
            record.reset_counts()
        elif observation.active and model.unit_active is True:
            healthy_awake = (model.state == "awake" and model.health_ok is True
                             and model.is_sleeping is False and model.swap_state == "ready")
            if healthy_awake:
                record.served = record.eligible = True
                record.reset_counts()
            else:
                # A confirmed previously served daemon may warm through the
                # data plane without a scheduler wake HTTP request.
                if (record.served and model.health_ok is True and model.is_sleeping is True
                        and (model.swap_state == "starting"
                             or (record.swap in ("starting", "stopped") and model.swap_state == "ready"))):
                    record.eligible = True
                if not record.eligible:
                    record.reset_counts()
                else:
                    record.failures = record.failures+1 if model.health_ok is False else 0
                    mismatch = model.swap_state == "ready" and model.is_sleeping is True
                    if mismatch:
                        if record.mismatch_at is None:
                            record.mismatch_at, record.mismatch_received = received_at, received_at
                        record.mismatch_samples += 1
                    else:
                        record.mismatch_at = record.mismatch_received = None
                        record.mismatch_samples = 0
                    if record.failures >= self.health_failures:
                        reason, samples = "consecutive_health_failures", record.failures
                    elif (mismatch and started_at-record.mismatch_at >= SLEEP_MISMATCH_SECONDS
                          and received_at-record.mismatch_received >= SLEEP_MISMATCH_SECONDS
                          and record.mismatch_samples >= 6):
                        reason, samples = "ready_still_sleeping", record.mismatch_samples
                        span = min(started_at-record.mismatch_at, received_at-record.mismatch_received)
        else:
            record.reset_counts()
            record.eligible = False
        record.sampled_at, record.received_at, record.generation = sample, received_at, generation
        record.started_at, record.finished_at = started_at, finished_at
        record.health, record.sleeping, record.swap = model.health_ok, model.is_sleeping, model.swap_state
        self.records[name] = record
        if reason is None:
            return None
        return FaultProof(name, lease.lease_id, unit_name, key[2], lease.gpu, reason,
                          sample, received_at, generation, record.epoch, samples, started_at, span)

    def current(self, proof, *, wall_now, now):
        record = self.records.get(proof.model)
        return (record is not None and record.key == (proof.lease_id, proof.unit, proof.invocation_id, proof.gpu)
                and record.epoch == proof.epoch and record.generation == proof.generation
                and known(wall_now) and known(now)
                and 0 <= wall_now-proof.sampled_at <= MAX_EVIDENCE_GAP_SECONDS
                and proof.received_at <= now
                and 0 <= now-proof.round_started_at <= MAX_EVIDENCE_GAP_SECONDS)


class FaultRecoveryError(RuntimeError):
    pass


class FaultRecoveryController:
    """One bounded recovery owner, with durable fencing across process restart."""

    def __init__(self, scheduler, *, probe=None, monotonic=None):
        from llmsvc.leases import LeaseUnitProbe
        self.scheduler = scheduler
        self.controller = scheduler.model_actions
        self.monotonic = monotonic or scheduler.monotonic
        self.probe = probe or (LeaseUnitProbe(self.controller.transport, monotonic=self.monotonic)
                               if self.controller is not None else None)
        self.detector = FaultDetector(health_failures=scheduler.config.fault_health_failures)
        self.proofs = {}
        self.expected_stops = {}
        self.waking = set()
        self.active = False
        self.cursor = 0

    def enabled(self):
        config = self.scheduler.config
        return (config.fault_recovery_enabled and config.model_actions_enabled and not config.read_only
                and self.controller is not None and self.scheduler.store is not None
                and not self.scheduler.store.read_only)

    def hold_account(self, lease):
        # Preserve an unexpected confirmed exit for the proof/fenced cleanup
        # path. Explicit ordinary stops and explicit release retain their paths.
        return (self.enabled() and lease.status == "confirmed"
                and self.expected_stops.get(lease.model) != lease.lease_id)

    def note_expected(self, action):
        self.detector.invalidate(action.model, stopped=action.kind == "stop")
        self.proofs.pop(action.model, None)
        if action.kind == "stop" and self.scheduler.store is not None:
            for lease, _ in self.scheduler.store.leases():
                if lease.model == action.model:
                    self.expected_stops[action.model] = lease.lease_id

    def note_wake(self, name):
        if self.scheduler.store is not None:
            for lease, _ in self.scheduler.store.leases():
                if lease.model == name and lease.status == "confirmed":
                    if self.detector.warm_wake(name, lease.lease_id, now=self.monotonic()):
                        self.expected_stops.pop(name, None)
                        self.waking.add(name)

    def _locked(self, deadline):
        from contextlib import contextmanager
        @contextmanager
        def locked():
            remaining = deadline-self.monotonic()
            if remaining <= 0 or not self.scheduler.action_lock.acquire(timeout=remaining):
                raise FaultRecoveryError("fault_deadline_exceeded")
            try:
                if not self.enabled() or self.scheduler.stopping.is_set():
                    raise FaultRecoveryError("fault_recovery_disabled_or_stopping")
                yield
            finally:
                self.scheduler.action_lock.release()
        return locked()

    def _sample_fresh(self, snapshot, generation):
        bounds = self.scheduler._sample_bounds
        now = self.monotonic()
        return (self.scheduler._sample_source_time_provided and bounds is not None and bounds[0] == generation
                and all(known(value) for value in bounds[1:])
                and bounds[1] <= bounds[2] <= now
                and 0 <= now-bounds[1] <= MAX_EVIDENCE_GAP_SECONDS
                and known(snapshot.sampled_at)
                and 0 <= self.scheduler.clock()-snapshot.sampled_at <= MAX_EVIDENCE_GAP_SECONDS)

    def _fresh_round(self, deadline):
        with self._locked(deadline):
            previous = self.scheduler._sample_started
        self.scheduler.request_sample()
        while self.monotonic() < deadline:
            with self._locked(deadline):
                if self.scheduler._sample_published > previous:
                    raw = self.scheduler._snapshot
                    if not self.scheduler._sample_source_time_provided:
                        raise FaultRecoveryError("fault_source_timestamp_unknown")
                    if not self._sample_fresh(raw, self.scheduler._sample_published):
                        raise FaultRecoveryError("fault_observation_stale")
                    return raw, self.scheduler._sample_published
                self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                       max(0, deadline-self.monotonic())))
        raise FaultRecoveryError("fault_deadline_exceeded")

    def _probe(self, name, deadline):
        from llmsvc.leases import UnitObservation
        try:
            result = self.probe(name, deadline=min(deadline, self.monotonic()+self.scheduler.config.lease_probe_seconds))
        except Exception:
            result = UnitObservation()
        if not isinstance(result, UnitObservation):
            result = UnitObservation()
        if self.monotonic() >= deadline:
            raise FaultRecoveryError("fault_deadline_exceeded")
        return result

    @staticmethod
    def _same_active(left, right):
        return (left.exists is True and right.exists is True and left.active and right.active
                and valid_invocation(left.invocation_id) and left.invocation_id == right.invocation_id
                and bool(left.lease_id) and left.lease_id == right.lease_id)

    def _observations(self, deadline):
        # Identity probes bracket the whole requested collection round: HTTP
        # health/sleep replies themselves do not contain an incarnation token.
        with self._locked(deadline):
            rows = [(lease, unit) for lease, unit in self.scheduler.store.leases()
                    if lease.status == "confirmed" and self.scheduler.store.fault(lease.model) is None]
        active_models = {lease.model for lease, _ in rows}
        with self._locked(deadline):
            for name in set(self.detector.records) - active_models:
                self.detector.records.pop(name, None)
                self.proofs.pop(name, None)
        before = {}
        for lease, unit in rows:
            with self._locked(deadline):
                if not self._configured(lease.model, unit):
                    self.detector.records.pop(lease.model, None)
                    self.proofs.pop(lease.model, None)
                    continue
            observation = self._probe(lease.model, deadline)
            before[lease.model] = (observation, self.monotonic())
        snapshot, generation = self._fresh_round(deadline)
        for lease, unit in rows:
            if lease.model not in before:
                continue
            after = self._probe(lease.model, deadline)
            with self._locked(deadline):
                if (generation != self.scheduler._sample_published or not self._sample_fresh(snapshot, generation)
                        or self.scheduler.store.lease(lease.lease_id) != (lease, unit)
                        or self.controller.transport.units.get(lease.model) != unit):
                    self.proofs.pop(lease.model, None)
                    self.detector.records.pop(lease.model, None)
                    continue
                # Exit needs current absence plus the preceding verified
                # lifecycle; active-field proofs additionally need bracketing.
                bounds = self.scheduler._sample_bounds
                if (bounds[1] < before[lease.model][1]
                        or (after.active and not self._same_active(before[lease.model][0], after))):
                    self.detector.records.pop(lease.model, None)
                    self.proofs.pop(lease.model, None)
                    continue
                expected = lease.model in self.controller.pending and lease.model not in self.waking
                proof = self.detector.observe(snapshot, lease, unit, after, generation=generation,
                    received_at=self.monotonic(), wall_now=self.scheduler.clock(),
                    started_at=bounds[1], finished_at=bounds[2], expected=expected)
                record = self.detector.records.get(lease.model)
                if (not expected and record is not None and record.eligible
                        and record.health is True and record.sleeping is False):
                    self.expected_stops.pop(lease.model, None)
                if proof is None:
                    self.proofs.pop(lease.model, None)
                else:
                    self.proofs[lease.model] = proof

    def _origin_hash(self):
        import hashlib
        return hashlib.sha256(self.controller.transport.swap_url.encode("utf-8")).hexdigest()

    def _configured(self, name, unit):
        units = self.controller.transport.units
        return units.get(name) == unit and sum(value == unit for value in units.values()) == 1

    def _claim_current(self, claim):
        if (self.scheduler.store.fault(claim.model) != claim
                or not self._configured(claim.model, claim.unit)
                or claim.proxy_origin_hash != self._origin_hash()):
            raise FaultRecoveryError("fault_claim_or_configuration_changed")
        row = self.scheduler.store.lease(claim.lease_id)
        if (row is None or (row[0].model, row[0].gpu, row[1]) != (claim.model, claim.gpu, claim.unit)
                or row[0].status != ("confirmed" if claim.stage == "claimed" else "released")):
            raise FaultRecoveryError("fault_account_changed")
        if any(lease.model == claim.model and lease.lease_id != claim.lease_id
               for lease, _ in self.scheduler.store.leases()):
            raise FaultRecoveryError("fault_new_account_detected")

    @staticmethod
    def _identity_matches(claim, observation):
        return valid_invocation(claim.invocation_id) and ((observation.exists is False and observation.exited) or (
            observation.exists is True and observation.lease_id == claim.lease_id
            and observation.invocation_id == claim.invocation_id
            and (observation.active or observation.inactive or observation.exited)))

    def _begin(self, proof, deadline):
        from dataclasses import asdict
        from llmsvc.state import FaultClaim
        with self._locked(deadline):
            if (self.proofs.get(proof.model) != proof or self.scheduler._sample_published != proof.generation
                    or not self.detector.current(proof, wall_now=self.scheduler.clock(), now=self.monotonic())):
                raise FaultRecoveryError("fault_proof_stale_or_changed")
            claim = FaultClaim(proof.lease_id, proof.model, proof.unit, proof.invocation_id,
                               proof.gpu, proof.reason, proof.sampled_at, proxy_origin_hash=self._origin_hash())
            if not self._configured(proof.model, proof.unit):
                raise FaultRecoveryError("fault_configuration_changed")
            observation = self._probe(proof.model, deadline)
            if not self._identity_matches(claim, observation):
                raise FaultRecoveryError("fault_unit_identity_changed")
            if (self.scheduler._sample_published != proof.generation
                    or not self.detector.current(proof, wall_now=self.scheduler.clock(), now=self.monotonic())):
                raise FaultRecoveryError("fault_proof_stale_or_changed")
            if not self._configured(proof.model, proof.unit) or claim.proxy_origin_hash != self._origin_hash():
                raise FaultRecoveryError("fault_configuration_changed")
            self.scheduler.store.claim_fault(claim)
            self.scheduler.emit("fault_detected", model=claim.model, detail={"claim": asdict(claim), "proof": asdict(proof)})
            error = None
            if (self.monotonic() >= deadline or not self.enabled() or self.scheduler.stopping.is_set()
                    or not self.detector.current(proof, wall_now=self.scheduler.clock(), now=self.monotonic())):
                return claim, "fault_stop_not_submitted"
            if observation.exists is not False:
                try:
                    code = self.controller.transport.stop_unit(claim.unit,
                        deadline=min(deadline, self.monotonic()+self.scheduler.config.request_timeout_seconds))
                    if type(code) is not int or code != 0:
                        error = "fault_stop_rejected"
                    if self.monotonic() >= deadline:
                        error = "fault_stop_deadline_exceeded"
                except Exception:
                    error = "fault_stop_error"
                self.scheduler.emit("fault_stop_submitted", model=claim.model,
                                    detail={"lease_id": claim.lease_id, "reason": claim.reason, "error": error})
            return claim, error

    def _wait_absence(self, claim, deadline, *, proxy_stopped=False):
        last = None
        while self.monotonic() < deadline:
            with self._locked(deadline):
                self._claim_current(claim)
            snapshot, generation = self._fresh_round(deadline)
            observation = self._probe(claim.model, deadline)
            with self._locked(deadline):
                self._claim_current(claim)
                if generation != self.scheduler._sample_published or not self._sample_fresh(snapshot, generation):
                    last = None
                    continue
                if not self._identity_matches(claim, observation):
                    raise FaultRecoveryError("fault_unit_identity_unknown_or_changed")
                models = [model for model in snapshot.models if model.name == claim.model]
                absent = (len(models) == 1 and models[0].unit == claim.unit
                          and sum(model.unit == claim.unit for model in snapshot.models) == 1
                          and models[0].unit_active is False and models[0].gpu in (None, claim.gpu)
                          and observation.exited and not observation.active)
                if proxy_stopped:
                    absent = absent and models[0].swap_state == "stopped"
                if absent and last is not None and snapshot.sampled_at > last:
                    return snapshot, generation
                last = snapshot.sampled_at if absent else None
                self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                       max(0, deadline-self.monotonic())))
        raise FaultRecoveryError("fault_proxy_effect_unconfirmed" if proxy_stopped else "fault_exit_unconfirmed")

    def _cleanup(self, claim, deadline, *, error=None, newly_submitted=False):
        from dataclasses import asdict
        from urllib.parse import quote
        result = {"model": claim.model, "lease_id": claim.lease_id, "reason": claim.reason,
                  "status": "blocked", "account_released": claim.stage == "released", "proxy_unloaded": False}
        if claim.error is not None:
            result["previous_error"] = claim.error
        try:
            if claim.proxy_submitted and not claim.proxy_acknowledged:
                raise FaultRecoveryError("fault_proxy_outcome_unknown")
            # On restart or uncertainty this ONLY observes; no stale-proof stop
            # replay. Do not let a known still-active retained claim monopolize
            # the whole cycle deadline and starve other freshly proven faults.
            if claim.stage == "claimed" and not newly_submitted:
                with self._locked(deadline):
                    self._claim_current(claim)
                    current = self._probe(claim.model, deadline)
                    if not self._identity_matches(claim, current):
                        raise FaultRecoveryError("fault_unit_identity_unknown_or_changed")
                    if current.active or not current.exited:
                        raise FaultRecoveryError("fault_exit_unconfirmed")
            observed, generation = self._wait_absence(claim, deadline)
            with self._locked(deadline):
                self._claim_current(claim)
                if generation != self.scheduler._sample_published:
                    raise FaultRecoveryError("fault_observation_superseded")
                if claim.stage == "claimed":
                    claim = self.scheduler.store.advance_fault(claim, stage="released", error=error)
                    result["account_released"] = True
                    self.scheduler.emit("fault_account_released", model=claim.model, detail=asdict(claim))
                current = self._probe(claim.model, deadline)
                if not self._identity_matches(claim, current) or not current.exited or current.active:
                    raise FaultRecoveryError("fault_unit_identity_unknown_or_changed")
                self._claim_current(claim)
                if not claim.proxy_submitted:
                    # Write-ahead submission fence: a crash/timeout cannot
                    # authorize a second delayed request against a new model.
                    claim = self.scheduler.store.advance_fault(claim, stage="released", error=error,
                                                                proxy_submitted=True)
                    self.scheduler.emit("fault_proxy_submitted", model=claim.model, detail=asdict(claim))
                    if self.monotonic() >= deadline or not self.enabled() or self.scheduler.stopping.is_set():
                        raise FaultRecoveryError("fault_proxy_outcome_unknown")
                    self._claim_current(claim)
                    status = self.controller.transport.http_request("POST", "/api/models/unload/"+quote(claim.model, safe=""),
                        deadline=min(deadline, self.monotonic()+self.scheduler.config.request_timeout_seconds))
                    if self.monotonic() >= deadline or not self.enabled() or self.scheduler.stopping.is_set():
                        raise FaultRecoveryError("fault_proxy_outcome_unknown")
                    if type(status) is not int or not 200 <= status < 300:
                        raise FaultRecoveryError("fault_proxy_unload_rejected")
                    self._claim_current(claim)
                    current = self._probe(claim.model, deadline)
                    if (not self._identity_matches(claim, current) or not current.exited or current.active
                            or not self.enabled() or self.scheduler.stopping.is_set()):
                        raise FaultRecoveryError("fault_proxy_outcome_unknown")
                    self._claim_current(claim)
                    claim = self.scheduler.store.advance_fault(claim, stage="released", error=error,
                                                                proxy_acknowledged=True)
            # HTTP success alone cannot clear a fence. Require two fresh
            # post-submit stopped proxy observations plus continued unit exit.
            observed, generation = self._wait_absence(claim, deadline, proxy_stopped=True)
            with self._locked(deadline):
                self._claim_current(claim)
                current = self._probe(claim.model, deadline)
                if (generation != self.scheduler._sample_published
                        or not self._sample_fresh(observed, generation)
                        or not self._identity_matches(claim, current) or not current.exited or current.active):
                    raise FaultRecoveryError("fault_proxy_effect_unconfirmed")
                self._claim_current(claim)
                claim = self.scheduler.store.advance_fault(claim, stage="complete", error=error)
                self.detector.records.pop(claim.model, None)
                self.proofs.pop(claim.model, None)
                self.waking.discard(claim.model)
                self.expected_stops.pop(claim.model, None)
                result.update(status="partial" if error else "complete", proxy_unloaded=True)
        except FaultRecoveryError as exc:
            error = str(exc)
        except Exception as exc:
            error = "fault_recovery_"+type(exc).__name__
        if error:
            result.update(status="partial" if result["account_released"] else "blocked", error=error)
            # Persist only against the same claim; a concurrent identity/row
            # change must never be overwritten by an old outcome.
            with self.scheduler.action_lock:
                if (self.enabled() and not self.scheduler.stopping.is_set()
                        and self.scheduler.store.fault(claim.model) == claim and claim.error != error):
                    self.scheduler.store.advance_fault(claim, stage=claim.stage, error=error)
        self.scheduler.emit("fault_result", model=claim.model, detail={**result, "dry_run": False})
        return result

    def run_once(self, *, dry_run=False):
        from dataclasses import asdict
        import json
        import logging
        if dry_run:
            with self.scheduler.action_lock:
                claims = self.scheduler.store.faults() if self.scheduler.store is not None else ()
                claimed = {claim.model for claim in claims}
                proofs = [proof for proof in self.proofs.values()
                          if proof.model not in claimed and proof.generation == self.scheduler._sample_published
                          and self.detector.current(proof, wall_now=self.scheduler.clock(), now=self.monotonic())]
                unknown = [claim for claim in claims if claim.proxy_submitted and not claim.proxy_acknowledged]
                result = {"would": [{"kind": "observe_fault_exit_and_cleanup_proxy", **asdict(claim)}
                                    for claim in claims if claim not in unknown]
                          + [{"kind": "proven_fault_cleanup", **asdict(proof)} for proof in proofs],
                          "blocked_by": [{"model": claim.model, "reason": "fault_proxy_outcome_unknown"} for claim in unknown]}
                if not result["would"] and not result["blocked_by"]:
                    result["blocked_by"] = [{"model": None, "reason": "fault_evidence_unavailable"}]
            logging.getLogger("llmsvc.faults").info(json.dumps({"kind": "fault_preview", "dry_run": True, **result}))
            return result
        if not self.enabled():
            return {"status": "disabled"}
        deadline = self.monotonic()+self.scheduler.config.fault_timeout_seconds
        with self._locked(deadline):
            if self.active:
                return {"status": "busy"}
            self.active = True
        try:
            self._observations(deadline)
            with self._locked(deadline):
                work = [(claim, None) for claim in self.scheduler.store.faults()]
                work += [(None, proof) for proof in self.proofs.values()]
                if work:
                    claim, proof = work[self.cursor % len(work)]
                    self.cursor += 1
                else:
                    claim = proof = None
            if claim is not None:
                return self._cleanup(claim, deadline)
            if proof is not None:
                claim, error = self._begin(proof, deadline)
                return self._cleanup(claim, deadline, error=error, newly_submitted=True)
            return {"status": "observing"}
        except Exception as exc:
            with self.scheduler.action_lock:
                self.detector.records.clear()
                self.proofs.clear()
            error = str(exc) if isinstance(exc, FaultRecoveryError) else "fault_observation_"+type(exc).__name__
            return {"status": "blocked", "error": error}
        finally:
            with self.scheduler.changed:
                self.active = False
                self.scheduler.changed.notify_all()
