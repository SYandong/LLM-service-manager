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
            or len(value["start_ticks"]) > 32 or not isinstance(value["scope_sha256"], str)
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
        if (s.config.read_only or not s.config.catalog_enabled or s.config.catalog_mode != "maintenance"
                or not s.config.model_actions_enabled or s.store is None or s.store.read_only or s.stopping.is_set()):
            raise MaintenanceError("maintenance mode is disabled or stopping")

    def _request(self, operation, context, deadline):
        if self.queue.clock() >= deadline:
            raise MaintenanceError("maintenance deadline exceeded")
        started = self.queue.clock()
        result = self.backend.request(operation, copy.deepcopy(context), deadline=deadline)
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
                "new_identity": None, "rollback_identity": None,
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

    def _context(self, record, state):
        return {"transaction_id": state["transaction_id"], "job_id": state["job_id"],
                "old_identity": state["old_identity"], "new_identity": state["new_identity"],
                "rollback_identity": state["rollback_identity"], "base_sha256": state["base_sha256"],
                "candidate_sha256": state["candidate_sha256"], "generation": state["generation"],
                "marker_sha256": record["marker_sha256"], "accounts": state["accounts"],
                "backend_bindings": state["backend_bindings"], "effects": state["effects"],
                "operation_id": state["operation_id"], "observed_scope": state["observed_scope"],
                "observed_actors": state["observed_actors"],
                "exclusion_method": state["exclusion_method"],
                "removed_models": sorted(record["old_manifest"]["active"].keys()-record["new_manifest"]["active"].keys()),
                "current_accounts": [[asdict(lease), unit] for lease, unit in self.scheduler.store.leases()]}

    def _effect(self, operation, deadline, *, extra=None):
        self._enabled()
        record, state = self._state()
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
        observed = self._wait("observe_candidate", deadline, lambda r: self._candidate_valid(r, state))
        self._save(state, stage="adopted", observations={**state["observations"], "candidate": observed})

    def _candidate_valid(self, result, state, *, opened=False):
        return (state["new_identity"] is not None and result.get("identity") == state["new_identity"]
                and result.get("old_identity") == state["old_identity"]
                and result.get("old_settled") is True and result.get("helpers_settled") is True
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
                or state["effects"].get("start_candidate", {}).get("acknowledged") is not True):
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
            if not state["effects"].get("start_candidate", {}).get("submitted"):
                raise MaintenanceError("candidate was not started; explicit rollback is required")
            runtime.busy = True
            s.catalog_fenced = True
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
                    effects = {**state["effects"], "start_candidate": {"submitted": True, "acknowledged": True}}
                    state = self._save(state, new_identity=new, effects=effects, stage="adopted")
                marker = json.loads(record["marker_json"])
                runtime._proof(record, marker, deadline=deadline)
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
        if not observed.active or observed.lease_id != lease.lease_id or not observed.invocation_id:
            raise MaintenanceError("maintenance cleanup unit identity unconfirmed")
        def stop(target, *, deadline):
            if target != unit:
                raise MaintenanceError("maintenance cleanup target changed")
            self._effect("stop_model", deadline, extra={"model":name, "unit":unit,
                "lease_id":lease.lease_id, "invocation_id":observed.invocation_id})
            return 0
        dispatcher = ModelActionDispatcher(action_lock=self.scheduler.action_lock, snapshot=self.scheduler.snapshot,
            http_request=lambda *a, **k: (_ for _ in ()).throw(MaintenanceError("cleanup cannot unload")),
            stop_unit=stop, timeout_seconds=self.scheduler.config.request_timeout_seconds,
            max_snapshot_age_seconds=self.scheduler.config.max_snapshot_age_seconds,
            enabled=True, monotonic=self.queue.clock, wall_clock=self.scheduler.clock)
        dispatcher.execute(Action("stop",name,"temporary_model_remove",lease.gpu),dry_run=False,deadline=deadline)
        while self.queue.clock() < deadline:
            self.scheduler.request_sample()
            self.scheduler.changed.wait(timeout=min(self.scheduler.config.action_poll_seconds,max(0,deadline-self.queue.clock())))
            if self.unit_absent(name,deadline=deadline):
                self.scheduler.store.release_maintenance_lease(record["transaction_id"],lease.lease_id,unit)
                return
        raise MaintenanceError("maintenance cleanup exit is unconfirmed")
