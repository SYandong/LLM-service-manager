# Generated-By: Codex / gpt-6-astra
"""Explicit instance-transition orchestration; ordinary hot reload is separate.

Configured site adapters own native lifecycle effects. This module binds their
bounded observations/effects to a durable catalog transaction, not HTTP clients.
"""

import copy
import hashlib
import json
import math
import os
import selectors
import subprocess
import time
from dataclasses import asdict

from llmsvc.reload import ReloadError, reload_blockers
from llmsvc.reload_witness import InstanceIdentity

MAX_ADAPTER_BYTES = 65536
MAX_CONTEXT_BYTES = 4 * 1024 * 1024


class MaintenanceError(ReloadError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(value):
    if (not isinstance(value, dict) or set(value) != {"pid", "start_ticks", "scope_sha256"}
            or type(value["pid"]) is not int or value["pid"] <= 0
            or not isinstance(value["start_ticks"], str) or not value["start_ticks"].isdigit()
            or len(value["start_ticks"]) > 32 or str(int(value["start_ticks"])) != value["start_ticks"]
            or not isinstance(value["scope_sha256"], str)
            or len(value["scope_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["scope_sha256"])):
        raise MaintenanceError("maintenance identity is unknown or invalid")
    return copy.deepcopy(value)


def instance(value):
    value = identity(value)
    return InstanceIdentity(value["pid"], value["start_ticks"])


class CommandBackend:
    """One explicit argv, no shell; bounded stdin/stdout/stderr and wall time.

    Killing a timed-out adapter is not evidence that an external effect stopped.
    The durable submitted flag must remain unresolved until positive observation.
    """
    def __init__(self, argv, *, monotonic=time.monotonic):
        if (not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= 16
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)
                or not os.path.isabs(argv[0])):
            raise ValueError("maintenance_command requires an explicit absolute executable argv")
        self.argv, self.monotonic = tuple(argv), monotonic

    def request(self, operation, context, *, deadline):
        if operation not in {"validate", "inspect", "preflight", "exclude", "stop_old", "observe_old",
                "start_candidate", "observe_candidate", "resume", "observe_unit", "stop_model",
                "stop_candidate", "observe_candidate_absent", "start_base", "observe_base"}:
            raise MaintenanceError("unsupported maintenance adapter operation")
        remaining = deadline-self.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise MaintenanceError("maintenance deadline exceeded")
        envelope = {"operation": operation, "context": context, "timeout_seconds": remaining}
        request_id = fingerprint(envelope)
        payload = (canonical({**envelope, "request_id": request_id})+"\n").encode()
        if len(payload) > MAX_CONTEXT_BYTES:
            raise MaintenanceError("maintenance context exceeds limit")
        process = subprocess.Popen([*self.argv, operation], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        sent = 0
        try:
            with selectors.DefaultSelector() as selected:
                for pipe, events, name in ((process.stdin, selectors.EVENT_WRITE, "stdin"),
                                           (process.stdout, selectors.EVENT_READ, "stdout"),
                                           (process.stderr, selectors.EVENT_READ, "stderr")):
                    os.set_blocking(pipe.fileno(), False)
                    selected.register(pipe, events, name)
                while selected.get_map():
                    remaining = deadline-self.monotonic()
                    if remaining <= 0:
                        raise MaintenanceError("maintenance adapter deadline exceeded")
                    for key, _ in selected.select(min(remaining, .1)):
                        if key.data == "stdin":
                            try:
                                sent += os.write(key.fd, payload[sent:])
                            except BrokenPipeError:
                                sent = len(payload)
                            if sent == len(payload):
                                selected.unregister(key.fileobj); key.fileobj.close()
                        else:
                            block = os.read(key.fd, 8192)
                            if not block:
                                selected.unregister(key.fileobj); key.fileobj.close()
                                continue
                            buffers[key.data].extend(block)
                            if len(buffers[key.data]) > MAX_ADAPTER_BYTES:
                                raise MaintenanceError("maintenance adapter response exceeds limit")
                remaining = deadline-self.monotonic()
                if remaining <= 0 or process.wait(timeout=remaining) != 0:
                    raise MaintenanceError("maintenance adapter failed")
            result = json.loads(buffers["stdout"].decode("utf-8"))
            if (not isinstance(result, dict) or result.get("request_id") != request_id
                    or result.get("transaction_id") != context.get("transaction_id")
                    or self.monotonic() >= deadline):
                raise MaintenanceError("maintenance adapter response is unbound or late")
            return result
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("maintenance adapter failed") from exc
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


class MaintenanceController:
    """Side-effect markers precede adapter commands; restart never replays them."""
    def __init__(self, scheduler, queue, backend):
        self.scheduler, self.queue, self.backend = scheduler, queue, backend
        self.runtime = None
        self._inspection = None

    def _enabled(self):
        s = self.scheduler
        if (self.runtime is None or s.catalog is not self.runtime
                or s.config.read_only or not s.config.catalog_enabled or s.config.catalog_mode != "maintenance"
                or not s.config.model_actions_enabled or s.store is None or s.store.read_only or s.stopping.is_set()):
            raise MaintenanceError("maintenance mode is disabled or stopping")

    def _request(self, operation, context, deadline):
        if self.queue.clock() >= deadline:
            raise MaintenanceError("maintenance deadline exceeded")
        started = self.queue.clock()
        context = copy.deepcopy(context)
        if operation in ("stop_candidate", "observe_candidate_absent") and context.get("new_scope") is not None:
            context["observed_scope"], context["observed_actors"] = context["new_scope"], context["new_actors"]
        try:
            result = self.backend.request(operation, context, deadline=deadline)
        except (OSError, ValueError, TypeError) as exc:
            raise MaintenanceError("maintenance adapter observation failed") from exc
        finished = self.queue.clock()
        if not isinstance(result, dict) or finished >= deadline:
            raise MaintenanceError("maintenance observation unavailable or late")
        observed = result.get("observed_at")
        if type(observed) not in (int, float) or not math.isfinite(observed) or not started <= observed <= finished:
            raise MaintenanceError("maintenance observation timestamp is stale or unbound")
        for key in ("identity", "old_identity", "attempt_identity"):
            if result.get(key) is not None:
                identity(result[key])
        if result.get("transaction_id") != context.get("transaction_id"):
            raise MaintenanceError("maintenance observation transaction mismatch")
        return result

    def inspect_instance(self, *, deadline):
        accounts = [[asdict(lease), unit] for lease, unit in self.scheduler.store.leases()] if self.scheduler.store else []
        result = self._request("inspect", {"transaction_id": None, "accounts": accounts}, deadline)
        self._inspection_receipt = result
        self._inspection = identity(result.get("identity"))
        return instance(self._inspection)

    def descriptor(self, prepared):
        import uuid
        self._enabled()
        observed = self.inspect_instance(deadline=self.queue.clock()+self.queue.operation_timeout)
        if observed != prepared.binding.instance:
            raise MaintenanceError("maintenance source identity changed")
        return {"mode": "maintenance", "transaction_id": uuid.uuid4().hex,
                "base_sha256": prepared.base_sha256,
                "old_scope_sha256": self._inspection["scope_sha256"]}

    def context_for_claim(self, record, prepared, job):
        self._enabled()
        observed = self.inspect_instance(deadline=self.runtime.deadline)
        descriptor = job.maintenance
        if (observed != prepared.binding.instance or self._inspection["scope_sha256"] != descriptor["old_scope_sha256"]
                or descriptor["mode"] != "maintenance"):
            raise MaintenanceError("maintenance source changed before claim")
        original, _ = self.queue._read()
        if hashlib.sha256(original).hexdigest() != prepared.base_sha256:
            raise MaintenanceError("maintenance base configuration changed")
        bindings = self._inspection_receipt.get("backend_bindings")
        accounts = [[asdict(lease), unit] for lease, unit in self.scheduler.store.leases()]
        if not isinstance(bindings, list) or len(bindings) != len(accounts):
            raise MaintenanceError("maintenance backend identity inventory is incomplete")
        for lease, unit in accounts:
            matches = [row for row in bindings if isinstance(row, dict) and row.get("lease_id") == lease["lease_id"]]
            if (len(matches) != 1 or matches[0].get("unit") != unit or matches[0].get("model") != lease["model"]
                    or matches[0].get("gpu") != lease["gpu"] or not matches[0].get("invocation_id")):
                raise MaintenanceError("maintenance backend identity differs from accounting")
        import uuid
        return {"transaction_id": record["transaction_id"], "job_id": job.id,
                "mode": "maintenance", "operation_id": descriptor["transaction_id"], "old_identity": self._inspection,
                "new_identity": None, "rollback_identity": None, "new_scope": None, "new_actors": [],
                "rollback_scope": None, "rollback_actors": [],
                "base_bytes": original.decode("utf-8"), "candidate_sha256": record["candidate_sha256"],
                "base_sha256": record["base_sha256"], "generation": prepared.binding.generation,
                "rollback_epoch": uuid.uuid4().hex, "backend_bindings": bindings,
                "exclusion_method": self._inspection_receipt.get("exclusion_method"),
                "observed_scope": self._inspection_receipt.get("scope"),
                "observed_actors": self._inspection_receipt.get("actors"),
                "backend_sha256": self._backend_fingerprint(),
                "stage": "claimed", "effects": {}, "observations": {}, "error": None,
                "accounts": [[asdict(lease), unit] for lease, unit in self.scheduler.store.leases()]}

    def _backend_fingerprint(self):
        config = self.scheduler.config
        return fingerprint({"command": config.maintenance_command, "registry": config.registry,
                            "sources": {k:v for k,v in config.collectors.items() if k != "models"}})

    def _state(self):
        record = self.scheduler.store.catalog_checkpoint()
        if record is None:
            raise MaintenanceError("maintenance catalog claim absent")
        state = self.scheduler.store.maintenance_checkpoint(record["transaction_id"])
        if state is None or state["job_id"] != record["job_id"]:
            raise MaintenanceError("maintenance claim mismatch")
        if state["stage"] not in ("released", "rolled_back", "aborted") and state["backend_sha256"] != self._backend_fingerprint():
            raise MaintenanceError("maintenance adapter configuration changed")
        return record, state

    def _save(self, state, **changes):
        updated = {**state, **changes}
        self.scheduler.store.save_maintenance(state, updated)
        self.scheduler.emit("catalog_transition", detail={"job_id": state["job_id"], "stage": updated["stage"]})
        return updated

    def _capture_scope(self, expected, receipt):
        scope, actors = receipt.get("scope"), receipt.get("actors")
        if (not isinstance(scope, dict) or fingerprint(scope) != expected["scope_sha256"]
                or not isinstance(actors, list) or not 1 <= len(actors) <= 4096 or expected not in actors):
            raise MaintenanceError("maintenance instance scope/actor inventory unavailable")
        for actor in actors:
            identity(actor)
        return copy.deepcopy(scope), copy.deepcopy(actors)

    def _context(self, record, state):
        return {"transaction_id": state["transaction_id"], "job_id": state["job_id"],
                "old_identity": state["old_identity"], "new_identity": state["new_identity"],
                "rollback_identity": state["rollback_identity"], "base_sha256": state["base_sha256"],
                "candidate_sha256": state["candidate_sha256"], "generation": state["generation"],
                "marker_sha256": record["marker_sha256"], "accounts": state["accounts"],
                "backend_bindings": state["backend_bindings"], "effects": state["effects"],
                "operation_id": state["operation_id"], "observed_scope": state["observed_scope"],
                "observed_actors": state["observed_actors"], "observations": state["observations"],
                "new_scope": state["new_scope"], "new_actors": state["new_actors"],
                "rollback_scope": state["rollback_scope"], "rollback_actors": state["rollback_actors"],
                "exclusion_method": state["exclusion_method"],
                "removed_models": sorted(record["old_manifest"]["active"].keys()-record["new_manifest"]["active"].keys()),
                "current_accounts": [[asdict(lease), unit] for lease, unit in self.scheduler.store.leases()]}

    def _effect(self, operation, deadline, *, extra=None):
        self._enabled()
        record, state = self._state()
        if not self.runtime.busy or not self.scheduler.catalog_fenced or record["phase"] not in ("claimed", "published"):
            raise MaintenanceError("maintenance effect lacks an active transaction owner")
        key = operation if extra is None else operation+":"+extra["model"]
        if key in state["effects"]:
            raise MaintenanceError("maintenance effect already submitted; observation required")
        effects = {**state["effects"], key: {"submitted": True, "acknowledged": False}}
        state = self._save(state, effects=effects, stage=operation)
        context = self._context(record, state)
        if extra: context.update(extra)
        result = self._request(operation, context, deadline)
        self._enabled()
        current_record, current = self._state()
        if current != state or current_record != record or result.get("accepted") is not True:
            raise MaintenanceError("maintenance effect acknowledgement unavailable")
        effects = {**state["effects"], key: {"submitted": True, "acknowledged": True}}
        self._save(state, effects=effects)
        return result

    def _wait(self, operation, deadline, predicate):
        while self.queue.clock() < deadline:
            self._enabled()
            record, state = self._state()
            result = self._request(operation, self._context(record, state), deadline)
            self._enabled()
            if predicate(result):
                record_now, state_now = self._state()
                if state_now != state or record_now != record:
                    raise MaintenanceError("maintenance claim changed during observation")
                return result
            self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,
                                                    max(0, deadline-self.queue.clock())))
        raise MaintenanceError("maintenance observation deadline exceeded")

    def validate(self, path):
        data = path.read_bytes()
        result = self._request("validate", {"transaction_id": None, "candidate_path": str(path),
            "candidate_sha256": hashlib.sha256(data).hexdigest()}, self.queue.clock()+self.queue.operation_timeout)
        if result.get("accepted") is not True:
            raise MaintenanceError("maintenance candidate validation failed")

    def validate_proof(self, proof, record, state):
        from llmsvc.reload import InstanceTransitionProof
        if (not isinstance(proof, InstanceTransitionProof) or proof.restored_base
                or proof.marker_sha256 != record["marker_sha256"]
                or proof.old_instance != instance(state["old_identity"])
                or state["new_identity"] is None or proof.new_instance != instance(state["new_identity"])
                or proof.old_scope_sha256 != state["old_identity"]["scope_sha256"]
                or proof.new_scope_sha256 != state["new_identity"]["scope_sha256"]
                or proof.current_sha256 != record["candidate_sha256"]):
            raise MaintenanceError("maintenance transition proof is not bound to its checkpoint")

    def bind(self, runtime):
        self.runtime = runtime
        self.queue.maintenance_adapter = self

    def blockers(self, job):
        try:
            self._enabled()
            descriptor = job.maintenance
            if descriptor is None or descriptor["mode"] != "maintenance":
                return [{"reason": "maintenance_mode_unavailable"}]
            deadline = min(job.submitted_at+self.queue.timeout,
                           self.runtime.deadline if self.runtime and self.runtime.busy else self.queue.clock()+self.queue.operation_timeout)
            context = {"transaction_id": descriptor["transaction_id"], "old_identity": {
                "pid": job.witness_binding.instance.pid, "start_ticks": job.witness_binding.instance.start_ticks,
                "scope_sha256": descriptor["old_scope_sha256"]}, "base_sha256": descriptor["base_sha256"]}
            result = self._request("preflight", context, deadline)
            if (result.get("ready") is not True or result.get("actors_known") is not True
                    or result.get("configuration_confirmed") is not True
                    or result.get("config_sha256") != descriptor["base_sha256"]
                    or type(result.get("in_flight")) is not int or result["in_flight"] != 0
                    or result.get("exclusion_method") != "stop_instance"
                    or identity(result.get("identity")) != context["old_identity"]):
                return [{"reason": "maintenance_preflight_unknown_or_busy"}]
            return []
        except (MaintenanceError, ValueError, KeyError):
            return [{"reason": "maintenance_preflight_unavailable"}]

    def _protection(self):
        blockers = reload_blockers(self.scheduler.snapshot(), self.scheduler.clock(),
                                   self.scheduler.config.max_snapshot_age_seconds)
        if blockers:
            raise MaintenanceError("maintenance protection or memory admission blocked")

    def _old_settled(self, result, state):
        return (result.get("old_identity") == state["old_identity"]
                and result.get("old_settled") is True and result.get("helpers_settled") is True
                and result.get("backends_confirmed") is True and result.get("ingress_state") == "excluded"
                and result.get("identity") is None)

    def before_replace(self, job, marker_bytes, *, deadline):
        self._enabled(); self._protection()
        record, state = self._state()
        if (job.id != state["job_id"] or hashlib.sha256(self.queue._read()[0]).hexdigest() != state["base_sha256"]
                or state["effects"]):
            raise MaintenanceError("maintenance before-replace binding changed or was already attempted")
        if self.inspect_instance(deadline=deadline) != instance(state["old_identity"]) or self._inspection != state["old_identity"]:
            raise MaintenanceError("maintenance source instance changed")
        marker, raw, _ = self.queue._read_marker()
        if (raw != marker_bytes or marker.get("job", {}).get("id") != state["job_id"]
                or marker.get("maintenance", {}).get("transaction_id") != state["operation_id"]):
            raise MaintenanceError("maintenance receipt changed before first effect")
        bound = {**record, "marker_json": raw.decode("utf-8"), "marker_sha256": hashlib.sha256(raw).hexdigest()}
        self.scheduler.store.save_catalog(record, bound)
        self.runtime.pending = bound
        self._protection()
        self._effect("stop_old", deadline)
        record, state = self._state()
        settled = self._wait("observe_old", deadline, lambda r: self._old_settled(r, state))
        self._save(state, stage="old_settled", observations={**state["observations"], "old": settled})

    def after_replace(self, job, marker_bytes, *, deadline):
        self._enabled()
        record, state = self._state()
        if (state["stage"] != "old_settled" or job.id != state["job_id"]
                or hashlib.sha256(self.queue._read()[0]).hexdigest() != state["candidate_sha256"]):
            raise MaintenanceError("maintenance replacement is not bound to settled source")
        self._wait("observe_old", deadline, lambda r: self._old_settled(r, state))
        result = self._effect("start_candidate", deadline)
        new = identity(result.get("identity"))
        if instance(new) == instance(state["old_identity"]) or new["scope_sha256"] == state["old_identity"]["scope_sha256"]:
            raise MaintenanceError("maintenance start did not create a distinct instance")
        _, state = self._state()
        state = self._save(state, new_identity=new)
        captured = self._request("inspect", self._context(record, state), deadline)
        if captured.get("identity") != new:
            raise MaintenanceError("candidate identity changed while capturing its scope")
        scope, actors = self._capture_scope(new, captured)
        state = self._save(state, new_scope=scope, new_actors=actors)
        observed = self._wait("observe_candidate", deadline, lambda r: self._candidate_valid(r, state))
        self._save(state, stage="adopted", observations={**state["observations"], "candidate": observed})

    def _candidate_valid(self, result, state, *, opened=False):
        return (state["new_identity"] is not None and result.get("identity") == state["new_identity"]
                and result.get("old_identity") == state["old_identity"]
                and result.get("old_settled") is True and result.get("helpers_settled") is True
                and result.get("configuration_confirmed") is True
                and result.get("config_sha256") == state["candidate_sha256"]
                and result.get("generation") == state["generation"]
                and result.get("backends_confirmed") is True and result.get("cleanup_confirmed") is True
                and result.get("ingress_state") == ("open" if opened or state["exclusion_method"] == "stop_instance" else "excluded"))

    def verify(self, marker_record, *, deadline):
        from llmsvc.reload import InstanceTransitionProof
        self._enabled()
        record, state = self._state()
        opened = state["effects"].get("resume", {}).get("acknowledged") is True
        observation = self._request("observe_candidate", self._context(record, state), deadline)
        if not self._candidate_valid(observation, state, opened=opened):
            raise MaintenanceError("new instance adoption or old/helper settlement is unconfirmed")
        return InstanceTransitionProof(
            marker_sha256=record["marker_sha256"], old_instance=instance(state["old_identity"]),
            new_instance=instance(state["new_identity"]), old_scope_sha256=state["old_identity"]["scope_sha256"],
            new_scope_sha256=state["new_identity"]["scope_sha256"], current_sha256=state["candidate_sha256"],
            generation_confirmed=True, instance_confirmed=True, settlement_confirmed=True,
            cleanup_confirmed=True, backends_confirmed=True, exclusion_confirmed=True)

    def before_release(self, record, *, deadline):
        self._enabled()
        current, state = self._state()
        if current != record or record["phase"] != "published":
            raise MaintenanceError("maintenance catalog was not published")
        # stop_instance has no invented ingress gate to release: the old
        # instance is gone, the new one is verified, and the core fence remains.
        self.verify(json.loads(record["marker_json"]), deadline=deadline)

    def finish(self, record):
        current, state = self._state()
        if (current != record or record["phase"] != "released"
                or not state["effects"].get("start_candidate", {}).get("submitted")
                or not state["observations"].get("candidate")):

            raise MaintenanceError("maintenance release is not acknowledged")
        self._save(state, stage="released")

    def reconcile(self):
        """Observe/publish/retire only. Never replay an old stop or start."""
        s, runtime = self.scheduler, self.runtime
        deadline = self.queue.clock()+self.queue.operation_timeout
        with s.action_lock:
            self._enabled()
            if runtime.busy:
                raise MaintenanceError("maintenance owner is active")
            record, state = self._state()
            resume_base = record["phase"] == "rolled_back" or state["effects"].get("start_base", {}).get("submitted")
            if not resume_base:
                if not state["effects"]:
                    return self.abort_unstarted()
                if not state["effects"].get("start_candidate", {}).get("submitted"):
                    raise MaintenanceError("candidate was not started; explicit rollback is required")
                runtime.busy = True
                s.catalog_fenced = True
        if resume_base:
            return self._reconcile_base(deadline)
        try:
            with s.action_lock:
                if record["marker_json"] is None:
                    marker, raw, _ = self.queue._read_marker()
                    if (marker.get("sha256") != record["candidate_sha256"]
                            or marker.get("job", {}).get("id") != record["job_id"]):
                        raise MaintenanceError("maintenance recovery receipt mismatch")
                    bound = {**record, "marker_json": raw.decode(), "marker_sha256": hashlib.sha256(raw).hexdigest()}
                    s.store.save_catalog(record, bound)
                    record = bound
                if state["new_identity"] is None or not state["effects"]["start_candidate"]["acknowledged"]:
                    observed = self._request("observe_candidate", self._context(record, state), deadline)
                    new = identity(observed.get("identity"))
                    if (observed.get("attempt_bound") is not True or observed.get("operation_id") != state["operation_id"]
                            or instance(new) == instance(state["old_identity"])
                            or new["scope_sha256"] == state["old_identity"]["scope_sha256"]
                            or not self._candidate_valid(observed, {**state, "new_identity": new})):
                        raise MaintenanceError("unknown candidate start is not positively bound")
                    state = self._save(state, new_identity=new, stage="adopted",
                                       observations={**state["observations"], "candidate": observed})
                if state["new_scope"] is None:
                    captured = self._request("inspect", self._context(record, state), deadline)
                    if captured.get("identity") != state["new_identity"]:
                        raise MaintenanceError("candidate identity changed during recovery inspection")
                    scope, actors = self._capture_scope(state["new_identity"], captured)
                    state = self._save(state, new_scope=scope, new_actors=actors)
                marker = json.loads(record["marker_json"])
                runtime._proof(record, marker, deadline=deadline)
                self._settle_model_accounts(deadline)
                runtime._check_membership(record["new_manifest"]["active"], record["old_manifest"]["active"])
                runtime._check_reactivation(record["new_manifest"], record["old_manifest"], during_claim=True)
                if runtime.epoch != record["new_epoch"]:
                    bundle = runtime._construct(record["new_manifest"])
                    published = {**record, "phase": "published"}
                    s.store.save_catalog(record, published)
                    record = published
                    runtime._publish(record["new_manifest"], record["new_epoch"], bundle)
                runtime.pending = record
            runtime.retire()
            with s.action_lock:
                self.queue.confirm_retired_receipt(record["marker_json"].encode(),
                    lambda saved: runtime._proof(record, saved, deadline=deadline))
                runtime._proof(record, marker, deadline=deadline)
                runtime._release_ready(record)
                released = {**record, "phase": "released", "previous": None}
                if record["phase"] != "released":
                    s.store.save_catalog(record, released)
                self.finish(released)
                s.catalog_fenced = False
                runtime.pending = None
                return {"status": "reconciled", "catalog_epoch": runtime.epoch}
        finally:
            runtime.busy = False

    def _settle_model_accounts(self, deadline):
        record, state = self._state()
        removed = record["old_manifest"]["active"].keys()-record["new_manifest"]["active"].keys()
        for captured, unit in state["accounts"]:
            name = captured["model"]
            if name not in removed or not state["effects"].get("stop_model:"+name, {}).get("submitted"):
                continue
            row = self.scheduler.store.lease(captured["lease_id"])
            if row is not None and row[0].status == "confirmed" and self.unit_absent(name, deadline=deadline):
                self.scheduler.store.release_maintenance_lease(record["transaction_id"],captured["lease_id"],unit)

    def unit_absent(self, name, *, deadline):
        record, state = self._state()
        if name not in record["old_manifest"]["active"] or name in record["new_manifest"]["active"]:
            raise MaintenanceError("maintenance cleanup target is not removed")
        controller = self.scheduler.placement or (self.scheduler.automation.accounting if self.scheduler.automation else None)
        if controller is None:
            raise MaintenanceError("maintenance unit accounting unavailable")
        raw = self.scheduler._snapshot
        models = [model for model in raw.models if model.name == name]
        observation = controller._inspect(name, deadline)
        return (controller._fresh(raw) and len(models) == 1 and models[0].state == "stopped"
                and models[0].unit == record["old_manifest"]["active"][name]["unit"]
                and models[0].unit_active is False and observation.exited)

    def stop_model(self, name, *, deadline):
        from llmsvc.actions import ModelActionDispatcher
        from llmsvc.state import Action
        self._enabled()
        if not self.scheduler.config.model_actions_enabled:
            raise MaintenanceError("model actions are not enabled")
        record, state = self._state()
        if name not in record["old_manifest"]["active"] or name in record["new_manifest"]["active"]:
            raise MaintenanceError("maintenance cleanup target is not removed")
        rows = [(lease, unit) for lease, unit in self.scheduler.store.leases() if lease.model == name]
        if len(rows) != 1 or rows[0][0].status != "confirmed" or self.scheduler.store.fault(name) or self.scheduler.store.recovery(name):
            raise MaintenanceError("maintenance cleanup account unavailable")
        lease, unit = rows[0]
        accounting = self.scheduler.placement or (self.scheduler.automation.accounting if self.scheduler.automation else None)
        if accounting is None:
            raise MaintenanceError("maintenance cleanup accounting unavailable")
        observed = accounting._inspect(name, deadline)
        bindings = [row for row in state["backend_bindings"] if row.get("model") == name and row.get("lease_id") == lease.lease_id]
        if (not observed.active or observed.lease_id != lease.lease_id or not observed.invocation_id
                or len(bindings) != 1 or observed.invocation_id != bindings[0].get("invocation_id")):
            raise MaintenanceError("maintenance cleanup unit identity unconfirmed")
        def stop(target, *, deadline):
            if target != unit:
                raise MaintenanceError("maintenance cleanup target changed")
            self._effect("stop_model", deadline, extra={"model":name, "unit":unit,
                "lease_id":lease.lease_id, "invocation_id":observed.invocation_id})
            return 0
        def expected(action):
            if self.scheduler.faults is not None:
                self.scheduler.faults.note_expected(action)
        dispatcher = ModelActionDispatcher(action_lock=self.scheduler.action_lock, snapshot=self.scheduler.snapshot,
            http_request=lambda *a, **k: (_ for _ in ()).throw(MaintenanceError("cleanup cannot unload")),
            stop_unit=stop, timeout_seconds=self.scheduler.config.request_timeout_seconds,
            max_snapshot_age_seconds=self.scheduler.config.max_snapshot_age_seconds,
            enabled=True, monotonic=self.queue.clock, wall_clock=self.scheduler.clock, before_action=expected)
        failure = None
        try:
            dispatcher.execute(Action("stop",name,"temporary_model_remove",lease.gpu),dry_run=False,deadline=deadline)
        except Exception as exc:
            failure = exc
        while self.queue.clock() < deadline:
            self.scheduler.request_sample()
            self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,max(0,deadline-self.queue.clock())))
            if self.unit_absent(name,deadline=deadline):
                self.scheduler.store.release_maintenance_lease(record["transaction_id"],lease.lease_id,unit)
                if failure is not None:
                    raise MaintenanceError("maintenance cleanup failed after observed exit") from failure
                return
            if failure is not None:
                raise MaintenanceError("maintenance cleanup failed with unconfirmed effect") from failure
        raise MaintenanceError("maintenance cleanup exit is unconfirmed")

    def abort_unstarted(self):
        """Retire only our unchanged receipt after durable proof of zero effects."""
        s, runtime = self.scheduler, self.runtime
        with s.action_lock:
            self._enabled()
            if runtime.busy:
                raise MaintenanceError("maintenance owner is active")
            record, state = self._state()
            if record["phase"] != "claimed" or state["effects"]:
                raise MaintenanceError("submitted maintenance effects cannot be aborted")
            if hashlib.sha256(self.queue._read()[0]).hexdigest() != record["base_sha256"]:
                raise MaintenanceError("unstarted maintenance base changed")
            if self.queue.fenced:
                receipt, raw, _ = self.queue._read_marker()
                if (receipt.get("job", {}).get("id") != record["job_id"]
                        or receipt.get("witness_binding") != record["binding"]
                        or receipt.get("maintenance", {}).get("transaction_id") != state["operation_id"]
                        or receipt.get("maintenance", {}).get("base_sha256") != state["base_sha256"]):
                    raise MaintenanceError("unstarted maintenance receipt changed")
                # This is the existing exact-byte retirement primitive, not a
                # source/instance transition attestation or caller-provided bool.
                self.queue._retire_marker(raw)
            current, current_state = self._state()
            if (self.queue.fenced or current != record or current_state != state
                    or hashlib.sha256(self.queue._read()[0]).hexdigest() != record["base_sha256"]):
                raise MaintenanceError("unstarted maintenance changed before abort")
            s.store.save_catalog(record,{**record,"phase":"aborted"})
            s.catalog_fenced=False;runtime.pending=None
            return {"status":"aborted","native_effects":False}

    def _attempt_absent(self, result, state):
        return (result.get("old_identity") == state["old_identity"]
                and result.get("old_settled") is True and result.get("helpers_settled") is True
                and result.get("attempt_settled") is True and result.get("backends_confirmed") is True
                and result.get("identity") is None and result.get("ingress_state") == "excluded"
                and (result.get("attempt_identity") == state["new_identity"] if state["new_identity"] is not None
                     else result.get("attempt_bound") is True and result.get("operation_id") == state["operation_id"]))

    def _base_valid(self, result, state):
        return (state["rollback_identity"] is not None
                and instance(state["rollback_identity"]) != instance(state["old_identity"])
                and (state["new_identity"] is None or instance(state["rollback_identity"]) != instance(state["new_identity"]))
                and result.get("identity") == state["rollback_identity"]
                and result.get("old_identity") == state["old_identity"]
                and result.get("configuration_confirmed") is True and result.get("config_sha256") == state["base_sha256"]
                and result.get("old_settled") is True and result.get("helpers_settled") is True
                and result.get("attempt_settled") is True and result.get("backends_confirmed") is True
                and result.get("cleanup_confirmed") is True and result.get("ingress_state") == "open"
                and (state["new_identity"] is None or result.get("attempt_identity") == state["new_identity"]))

    def _base_proof(self, record, state, *, deadline):
        from llmsvc.reload import InstanceTransitionProof
        self._enabled()
        if self._state() != (record, state):
            raise MaintenanceError("rollback checkpoint changed before confirmation")
        observed = self._request("observe_base", self._context(record, state), deadline)
        self._enabled()
        if self._state() != (record, state):
            raise MaintenanceError("rollback checkpoint changed during confirmation")
        if not self._base_valid(observed, state):
            raise MaintenanceError("restored instance or attempt settlement is unconfirmed")
        proof = InstanceTransitionProof(marker_sha256=record["marker_sha256"],
            old_instance=instance(state["old_identity"]), new_instance=instance(state["rollback_identity"]),
            old_scope_sha256=state["old_identity"]["scope_sha256"], new_scope_sha256=state["rollback_identity"]["scope_sha256"],
            current_sha256=state["base_sha256"], generation_confirmed=True, instance_confirmed=True,
            settlement_confirmed=True, cleanup_confirmed=True, backends_confirmed=True, exclusion_confirmed=True,
            restored_base=True, attempt_instance=instance(state["new_identity"]) if state["new_identity"] else None,
            attempt_settled=True)
        self.queue._confirm_proof(json.loads(record["marker_json"]),record["marker_json"].encode(),proof)
        return proof

    def _restore_bytes(self, record, state, deadline):
        current, info = self.queue._read()
        current_hash = hashlib.sha256(current).hexdigest()
        if current_hash == state["base_sha256"]:
            return state
        if current_hash != state["candidate_sha256"] or state["effects"].get("restore_config", {}).get("submitted"):
            raise MaintenanceError("rollback configuration is foreign or an earlier restore is unresolved")
        staged = self.queue._stage(state["base_bytes"].encode(), info)
        try:
            again, same_info = self.queue._read()
            if (again != current or (same_info.st_dev,same_info.st_ino) != (info.st_dev,info.st_ino)
                    or self.queue.clock() >= deadline):
                raise MaintenanceError("configuration changed during rollback validation")
            effects = {**state["effects"], "restore_config": {"submitted":True,"acknowledged":False}}
            state = self._save(state,effects=effects,stage="restore_config")
            self._enabled()
            os.replace(staged,self.queue.path)
            self.queue._sync_directory()
            if self.queue.clock() >= deadline or hashlib.sha256(self.queue._read()[0]).hexdigest() != state["base_sha256"]:
                raise MaintenanceError("rollback file acknowledgement is unavailable")
            effects = {**state["effects"], "restore_config": {"submitted":True,"acknowledged":True}}
            return self._save(state,effects=effects)
        finally:
            staged.unlink(missing_ok=True)

    def rollback(self, *, dry_run=False):
        """A new explicit rollback request, never an automatic retry of forward work."""
        if dry_run:
            return {"would":[{"kind":"rollback_maintenance"}]}
        s,runtime=self.scheduler,self.runtime
        deadline=self.queue.clock()+self.queue.operation_timeout
        with s.action_lock:
            self._enabled()
            if runtime.busy:
                raise MaintenanceError("maintenance owner is active")
            record,state=self._state()
            if record["phase"] in ("released","rolled_back","aborted"):
                raise MaintenanceError("completed maintenance cannot be replayed as rollback")
            if not state["effects"]:
                return self.abort_unstarted()
            if not state["effects"].get("stop_old",{}).get("submitted"):
                raise MaintenanceError("unbound native effects prevent rollback")
            resume_base = state["effects"].get("start_base",{}).get("submitted")
            if not resume_base:
                runtime.busy=True;s.catalog_fenced=True
        if resume_base:
            return self._reconcile_base(deadline)
        try:
            with s.action_lock:
                if not state["effects"].get("start_candidate",{}).get("submitted"):
                    self._wait("observe_old",deadline,lambda r:self._old_settled(r,state))
                absent=self._request("observe_candidate_absent",self._context(record,state),deadline)
                if not self._attempt_absent(absent,state) and absent.get("identity") is None:
                    absent=self._wait("observe_candidate_absent",deadline,lambda r:self._attempt_absent(r,state))
                if not self._attempt_absent(absent,state):
                    if state["new_identity"] is None:
                        observed=self._request("observe_candidate",self._context(record,state),deadline)
                        new=identity(observed.get("identity"))
                        if (observed.get("attempt_bound") is not True or observed.get("operation_id") != state["operation_id"]
                                or not self._candidate_valid(observed,{**state,"new_identity":new})):
                            raise MaintenanceError("candidate ownership is unknown; rollback cannot stop it")
                        state=self._save(state,new_identity=new)
                    current=self._request("preflight",self._context(record,state),deadline)
                    if (current.get("identity") != state["new_identity"] or current.get("actors_known") is not True
                            or type(current.get("in_flight")) is not int or current["in_flight"] != 0):
                        raise MaintenanceError("candidate is busy or changed; rollback is blocked")
                    scope,actors=self._capture_scope(state["new_identity"],current)
                    actors=state["new_actors"]+[actor for actor in actors if actor not in state["new_actors"]]
                    state=self._save(state,new_scope=scope,new_actors=actors)
                    self._protection()
                    if not state["effects"].get("stop_candidate",{}).get("submitted"):
                        self._effect("stop_candidate",deadline)
                    _,state=self._state()
                    absent=self._wait("observe_candidate_absent",deadline,lambda r:self._attempt_absent(r,state))
                if state["new_identity"] is None and absent.get("attempt_identity") is not None:
                    candidate = identity(absent["attempt_identity"])
                    if instance(candidate) == instance(state["old_identity"]):
                        raise MaintenanceError("rollback attempt identity matches the original source")
                    state = self._save(state,new_identity=candidate)
                state=self._save(state,stage="rollback_settled",observations={**state["observations"],"candidate_absence":absent})
                state=self._restore_bytes(record,state,deadline)
                # Absence/settlement must still hold after validation/restoration.
                self._wait("observe_candidate_absent",deadline,lambda r:self._attempt_absent(r,state))
                started=self._effect("start_base",deadline)
                restored=identity(started.get("identity"))
                if instance(restored)==instance(state["old_identity"]) or (state["new_identity"] and instance(restored)==instance(state["new_identity"])):
                    raise MaintenanceError("rollback did not create a distinct instance")
                _,state=self._state()
                state=self._save(state,rollback_identity=restored)
                captured=self._request("inspect",self._context(record,state),deadline)
                if captured.get("identity") != restored:
                    raise MaintenanceError("rollback instance changed while capturing scope")
                scope,actors=self._capture_scope(restored,captured)
                state=self._save(state,rollback_scope=scope,rollback_actors=actors)
                observed=self._wait("observe_base",deadline,lambda r:self._base_valid(r,state))
                state=self._save(state,stage="base_verified",observations={**state["observations"],"base":observed})
            return self._finish_base(record,state,deadline)
        finally:
            runtime.busy=False

    def _finish_base(self,record,state,deadline):
        s,runtime=self.scheduler,self.runtime
        with s.action_lock:
            if hashlib.sha256(self.queue._read()[0]).hexdigest()!=state["base_sha256"]:
                raise MaintenanceError("rollback source changed before publication")
            self._base_proof(record,state,deadline=deadline)
            if runtime.epoch != state["rollback_epoch"]:
                runtime._publish(record["old_manifest"],state["rollback_epoch"],runtime._construct(record["old_manifest"]))
        runtime.retire()
        with s.action_lock:
            current,state_now=self._state()
            if current!=record or state_now!=state:
                raise MaintenanceError("rollback checkpoint changed during publication")
            self.queue.confirm_retired_receipt(record["marker_json"].encode(),lambda saved:self._base_proof(record,state,deadline=deadline))
            self._base_proof(record,state,deadline=deadline)
            self._enabled()
            if self._state() != (record, state):
                raise MaintenanceError("rollback checkpoint changed before release")
            if (runtime.retired or self.queue.fenced or self.queue.clock()>=deadline
                    or hashlib.sha256(self.queue._read()[0]).hexdigest()!=state["base_sha256"]):
                raise MaintenanceError("rollback retirement is incomplete")
            if record["phase"]!="rolled_back":
                s.store.save_catalog(record,{**record,"phase":"rolled_back","previous":None})
            s.catalog_fenced=False;runtime.pending=None
            s.emit("catalog_rolled_back",detail={"job_id":record["job_id"],"catalog_epoch":runtime.epoch})
            return {"status":"rolled_back","catalog_epoch":runtime.epoch}

    def _reconcile_base(self, deadline):
        s, runtime = self.scheduler, self.runtime
        with s.action_lock:
            self._enabled()
            record, state = self._state()
            if runtime.busy:
                raise MaintenanceError("maintenance owner is active")
            runtime.busy = True
            s.catalog_fenced = True
        try:
            with s.action_lock:
                if state["rollback_identity"] is None:
                    observed = self._request("observe_base", self._context(record, state), deadline)
                    restored = identity(observed.get("identity"))
                    if (observed.get("attempt_bound") is not True or observed.get("operation_id") != state["operation_id"]
                            or not self._base_valid(observed, {**state, "rollback_identity": restored})):
                        raise MaintenanceError("unknown rollback start is not positively bound")
                    state = self._save(state, rollback_identity=restored)
                if state["rollback_scope"] is None:
                    captured = self._request("inspect", self._context(record, state), deadline)
                    if captured.get("identity") != state["rollback_identity"]:
                        raise MaintenanceError("rollback identity changed during recovery inspection")
                    scope, actors = self._capture_scope(state["rollback_identity"], captured)
                    state = self._save(state, rollback_scope=scope, rollback_actors=actors)
                observed = self._request("observe_base", self._context(record, state), deadline)
                if not self._base_valid(observed, state):
                    raise MaintenanceError("rollback observation is incomplete")
                if record["phase"] != "rolled_back":
                    state = self._save(state, stage="base_verified", observations={**state["observations"], "base": observed})
            return self._finish_base(record, state, deadline)
        finally:
            with s.action_lock:
                runtime.busy = False
