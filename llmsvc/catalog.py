# Generated-By: Codex / gpt-6-astra
"""Trusted, proof-gated runtime catalog publication over the existing queue.

No HTTP proof endpoint or live verifier is installed here. File visibility is
not adoption/settlement. A durable checkpoint fences every partial publication.
"""

import copy
import hashlib
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from llmsvc.catalog_state import catalog_json
from llmsvc.reload import RecoveryProof, ReloadError
from llmsvc.reload_witness import CandidateBinding
from llmsvc.store import finite_positive


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources(config):
    return copy.deepcopy({k: v for k, v in config.collectors.items() if k != "models"})


@dataclass(frozen=True)
class PreparedCatalog:
    base_sha256: str
    candidate: bytes
    manifest_json: str
    binding: CandidateBinding
    epoch: str


class CatalogRuntime:
    def __init__(self, scheduler, queue, *, verifier=None, collector_factory=None,
                 relay_factory=None, transport_factory=None, profile_provider=None, instance_provider=None):
        from llmsvc.__main__ import build_collector, build_event_relay
        from llmsvc.actions import ManagedModelTransport
        if queue.action_lock is not scheduler.action_lock:
            raise ValueError("catalog requires the scheduler action lock")
        self.scheduler, self.queue, self.verifier = scheduler, queue, verifier
        self.profile_provider, self.instance_provider = profile_provider, instance_provider
        self.collector_factory = collector_factory or build_collector
        self.relay_factory = relay_factory or build_event_relay
        self.transport_factory = transport_factory or (lambda config, models: ManagedModelTransport(
            swap_url=config.collectors.get("swap_url", ""), models=models,
            systemctl=config.collectors.get("systemctl", "systemctl")))
        self.manifest = {"sources": sources(scheduler.config),
                         "active": copy.deepcopy(scheduler.config.collectors.get("models", {})), "retained": {}}
        self.epoch = scheduler.catalog_epoch
        self.jobs = {}
        self.pending = None
        previous = scheduler.catalog
        if previous is not None and previous.busy:
            raise ReloadError("catalog owner is still active")
        self.retired = previous.retired if previous is not None else []
        if previous is not None:
            previous.retired = []
        self.staged = None
        self.busy = False
        self.deadline = 0.0
        scheduler.catalog = self
        for controller in (scheduler.model_actions, scheduler.placement):
            if controller is not None:
                epoch = self.epoch
                controller.transport.catalog_guard = lambda epoch=epoch: scheduler.check_catalog(epoch)
        record = scheduler.store.catalog_checkpoint() if scheduler.store else None
        # A checkpoint always survives disabled options. Restore observation
        # metadata now; a fresh bound verifier must establish restart authority.
        if record is not None:
            if record["phase"] == "aborted" and record["previous"] is None:
                if (record["old_manifest"] != self.manifest or digest(queue._read()[0]) != record["base_sha256"] or queue.fenced):
                    scheduler.catalog_fenced = True
                return
            if record["phase"] == "aborted":
                record = record["previous"]
            scheduler.catalog_fenced = True
            self.pending = record
            manifest = record["new_manifest"] if record["phase"] in ("published", "released") else record["old_manifest"]
            epoch = record["new_epoch"] if record["phase"] in ("published", "released") else record["old_epoch"]
            if manifest["sources"] != sources(scheduler.config):
                raise ValueError("catalog source settings changed; reconciliation required")
            bundle = self._construct(manifest)
            with scheduler.action_lock:
                self._publish(manifest, epoch, bundle)
            self.retire()

    def can_submit(self):
        s = self.scheduler
        return (s.catalog is self and s.config.catalog_enabled and not s.config.read_only and s.store is not None
                and not s.store.read_only and callable(self.verifier) and callable(self.profile_provider)
                and callable(self.instance_provider) and not s.stopping.is_set())

    def connect_registry(self, registry):
        if registry.queue is not self.queue or not self.can_submit():
            raise ReloadError("trusted catalog submission is unavailable")
        registry.submit_change = self.submit_change
        reserved = registry.reserved_ports
        registry.reserved_ports = lambda: list(reserved()) + [
            p["port"] for p in self.manifest["retained"].values() if type(p.get("port")) is int]

    def _enabled(self):
        s = self.scheduler
        if s.catalog is not self or s.config.read_only or not s.config.catalog_enabled or s.store is None or s.store.read_only:
            raise ReloadError("catalog installation is disabled")
        if not callable(self.verifier):
            raise ReloadError("catalog proof source is unavailable")
        if s.stopping.is_set():
            raise ReloadError("scheduler stopping")

    def _idle(self):
        s = self.scheduler
        if ((s.store and any(lease.status in ("pending", "stale") for lease, _ in s.store.leases()))
                or (s.model_actions and (s.model_actions.pending or s.model_actions.free_active))
                or any(c is not None and c.active for c in (s.automation, s.faults, s.sleeping_recovery))):
            raise ReloadError("catalog conflicts with an active operation")

    def prepare(self, candidate, trusted_profiles, *, binding):
        """Pure prospective description except for bounded configured source reads."""
        if not isinstance(candidate, bytes) or len(candidate) > self.queue.config_max_bytes:
            raise ValueError("invalid catalog candidate")
        if not isinstance(binding, CandidateBinding) or binding.candidate_sha256 != digest(candidate):
            raise ValueError("catalog binding mismatch")
        binding.to_dict()
        if binding.endpoint != self.scheduler.config.collectors.get("swap_url", "").rstrip("/")+"/api/mcp":
            raise ValueError("catalog binding uses another proxy origin")
        from llmsvc.registry import ModelRegistry
        document, records = ModelRegistry._decode(candidate)
        if not isinstance(document, dict) or not isinstance(document.get("models"), dict):
            raise ValueError("catalog candidate requires models")
        if not isinstance(trusted_profiles, dict) or set(trusted_profiles) != set(document["models"]):
            raise ValueError("every candidate model needs an explicit trusted profile")
        with self.scheduler.action_lock:
            original, _ = self.queue._read()
            active = copy.deepcopy(trusted_profiles)
            retained = copy.deepcopy(self.manifest["retained"])
            old = {**self.manifest["retained"], **self.manifest["active"]}
            ports, units = set(), set()
            for name, profile in active.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                    raise ValueError("invalid catalog name")
                if not isinstance(profile, dict):
                    raise ValueError("invalid trusted model profile")
                port = profile.get("port")
                record = records.get(name)
                if record is not None and (record.get("daemon_port") != port or record.get("base") not in document["models"]
                                           or record.get("base") == name or profile.get("is_default") is not False):
                    raise ValueError("catalog profile disagrees with temporary model identity")
                if type(port) is not int or not 1 <= port <= 65535 or port in ports:
                    raise ValueError("catalog daemon ports must be unique")
                parts = urlsplit(profile.get("daemon_url", ""))
                address = ipaddress.ip_address(parts.hostname)
                if (parts.scheme not in ("http", "https") or parts.port != port
                        or parts.username or parts.password or parts.query or parts.fragment
                        or parts.path not in ("", "/") or getattr(address, "scope_id", None)):
                    raise ValueError("catalog requires a direct configured daemon origin")
                unit = profile.get("unit")
                if unit != "vllm-"+name+".service" or unit in units:
                    raise ValueError("catalog requires canonical distinct managed units")
                if not 0 < finite_positive(profile.get("util"), "catalog util") <= 1:
                    raise ValueError("invalid catalog util")
                finite_positive(profile.get("budget_gb"), "catalog budget")
                finite_positive(profile.get("weights_gb"), "catalog weights")
                if type(profile.get("is_default")) is not bool:
                    raise ValueError("catalog requires a known model role")
                if name not in self.manifest["active"] and profile["is_default"]:
                    raise ValueError("temporary catalog model cannot become default")
                if name in old and old[name] != profile:
                    raise ValueError("existing catalog profile changes require separate configuration reconciliation")
                ports.add(port); units.add(unit); retained.pop(name, None)
            store = self.scheduler.store
            referenced = {lease.model for lease, _ in store.leases()} if store else set()
            if store:
                referenced.update(claim.model for claim in store.faults())
                referenced.update(claim.model for claim in store.recoveries())
            for name, profile in old.items():
                if name not in active:
                    if name in referenced:
                        retained[name] = profile
                    elif name in self.manifest["active"]:
                        # This candidate's bound cleanup proof is required before
                        # publication. Until then the old manifest remains live.
                        retained.pop(name, None)
                    elif store and any(lease.model == name and lease.status == "released" and unit == profile.get("unit")
                                       for lease, unit in store.leases(include_released=True)):
                        retained.pop(name, None)
            # Retained exact identities cannot alias a new active endpoint/unit.
            for profile in retained.values():
                if profile.get("unit") in units or profile.get("port") in ports:
                    raise ValueError("catalog aliases a retained resource")
            self._check_membership(active)
            manifest = {"sources": sources(self.scheduler.config), "active": active, "retained": retained}
            return PreparedCatalog(digest(original), candidate, catalog_json(manifest), binding, self.epoch)

    def _check_membership(self, active, previous=None):
        previous = self.manifest["active"] if previous is None else previous
        removed = set(previous)-set(active)
        pins = self.scheduler.store.active(self.scheduler.clock())[0] if self.scheduler.store else ()
        if any(pin.model in removed for pin in pins):
            raise ReloadError("catalog removal is pinned")
        if any(previous[name].get("is_default") is True for name in removed):
            raise ReloadError("catalog cannot remove a default model")

    def _config(self, manifest, *, events=False):
        models = manifest["active"] if events else {**manifest["retained"], **manifest["active"]}
        return replace(self.scheduler.config, collectors={**copy.deepcopy(manifest["sources"]), "models": copy.deepcopy(models)})

    def _construct(self, manifest):
        collector = relay = None
        try:
            config = self._config(manifest)
            collector = self.collector_factory(config)
            relay = self.relay_factory(self._config(manifest, events=True))
            transport = self.transport_factory(config, config.collectors["models"])
            transport.active_models = frozenset(manifest["active"])
            return collector, relay, transport
        except BaseException:
            if relay is not None: relay.close()
            if collector is not None and hasattr(collector, "close"): collector.close()
            raise

    def _publish(self, manifest, epoch, bundle):
        from llmsvc.actions import AutomaticPolicyController, ModelActionController
        from llmsvc.leases import PlacementController, LeaseUnitProbe
        from llmsvc.scheduler import DataPlaneBridge
        from llmsvc.__main__ import build_usage
        s = self.scheduler
        collector, relay, transport = bundle
        old_collect, old_bridge = s.collect, s.event_bridge
        self.retired.append((old_collect, old_bridge))
        self.manifest, self.epoch = copy.deepcopy(manifest), epoch
        s.catalog_epoch = epoch
        transport.catalog_guard = lambda: s.check_catalog(epoch)
        s.config = self._config(manifest)
        s.collect, s._usage = collector, build_usage(collector)
        s.event_bridge = DataPlaneBridge(s, relay, catalog_epoch=epoch) if relay else None
        old_place, old_fault = s.placement, s.faults
        if s.model_actions is not None:
            s.model_actions = ModelActionController(s, transport, monotonic=s.monotonic)
        if old_place is not None:
            probe = old_place.probe if not isinstance(old_place.probe, LeaseUnitProbe) else None
            s.placement = PlacementController(s, transport, probe=probe, monotonic=s.monotonic)
        if s.automation is not None:
            s.automation = AutomaticPolicyController(s, accounting=s.placement, monotonic=s.monotonic)
        if s.sleeping_recovery is not None:
            from llmsvc.recovery import SleepingRecoveryController
            s.sleeping_recovery = SleepingRecoveryController(s, monotonic=s.monotonic)
        if old_fault is not None:
            from llmsvc.faults import FaultRecoveryController
            probe = old_fault.probe if not isinstance(old_fault.probe, LeaseUnitProbe) else None
            s.faults = FaultRecoveryController(s, probe=probe, monotonic=s.monotonic)
        s._snapshot = s._unknown("catalog_changed_waiting_for_sample")
        s._sample_bounds = None
        s._sample_source_time_provided = False
        s.changed.notify_all()

    def retire(self):
        """Called outside action_lock, with the publication fence still held."""
        while self.retired:
            collector, bridge = self.retired[0]
            try:
                if bridge is not None: bridge.close()
            finally:
                if collector is not None and hasattr(collector, "close"): collector.close()
            self.retired.pop(0)  # Retain failed handles for explicit retry/reconciliation.
        s = self.scheduler
        if s._thread is not None and s._thread.is_alive() and s.event_bridge is not None and s.event_bridge.thread is None:
            s.event_bridge.start()
        s.request_sample()

    def enqueue(self, prepared, *, cleanup=None, dry_run=False, precheck=None, description=None):
        with self.scheduler.action_lock:
            if not isinstance(prepared, PreparedCatalog) or prepared.epoch != self.epoch:
                raise ReloadError("stale prepared catalog")
            current = self.prepare(prepared.candidate, json.loads(prepared.manifest_json)["active"], binding=prepared.binding)
            if current != prepared:
                raise ReloadError("prepared catalog no longer matches current trusted state")
            if self.scheduler.catalog_fenced or self.queue.fenced or self.queue._pending:
                raise ReloadError("catalog or queue requires reconciliation")
            original, _ = self.queue._read()
            if digest(original) != prepared.base_sha256:
                raise ReloadError("catalog source changed")
            if dry_run:
                return {"would": [{"kind": "install_catalog", "models": sorted(json.loads(prepared.manifest_json)["active"])}]}
            self._enabled(); self._idle()
            def combined_precheck():
                try:
                    self._check_membership(json.loads(prepared.manifest_json)["active"])
                    return list(precheck()) if precheck is not None else []
                except ReloadError:
                    return [{"reason": "catalog_removal_protected"}]
            def transform(raw):
                if digest(raw) != prepared.base_sha256:
                    raise ReloadError("catalog source changed")
                return prepared.candidate
            def after(*, deadline):
                if cleanup is not None:
                    cleanup(deadline=deadline)
                self._after_apply(deadline)
            result = self.queue.enqueue(transform, description=description or {"kind": "install_catalog"},
                after_apply=after, precheck=combined_precheck, witness_binding=prepared.binding)
            self.jobs[result["id"]] = prepared
            return result

    def submit_change(self, transform, *, description, dry_run=False, precheck=None, after_apply=None):
        """Existing registry callback shape; no fallback around a catalog error."""
        if dry_run:
            raise ReloadError("registry previews must use the pure queue preview")
        from llmsvc.registry import plan_generation_candidate
        with self.scheduler.action_lock:
            self._enabled()
            if not callable(self.profile_provider) or not callable(self.instance_provider):
                raise ReloadError("trusted catalog profile or instance source is unavailable")
            original, _ = self.queue._read()
            edited = transform(original)
            deadline = self.queue.clock()+self.queue.operation_timeout
            instance = self.instance_provider(deadline=deadline)
            if self.queue.clock() >= deadline:
                raise ReloadError("catalog instance source exceeded deadline")
            generated = plan_generation_candidate(edited, expected_sha256=digest(edited),
                generation="gen_"+uuid.uuid4().hex,
                endpoint=self.scheduler.config.collectors["swap_url"].rstrip("/")+"/api/mcp", instance=instance)
            profiles = self.profile_provider(generated.candidate)
            prepared = self.prepare(generated.candidate, profiles, binding=generated.binding)
            return self.enqueue(prepared, cleanup=after_apply, precheck=precheck, description=description)

    def _proof(self, record, marker_record, *, deadline):
        self._enabled()
        if self.queue.clock() >= deadline:
            raise ReloadError("catalog deadline exceeded")
        if sources(self.scheduler.config) != record["new_manifest"]["sources"]:
            raise ReloadError("catalog source settings changed")
        raw, _ = self.queue._read()
        if digest(raw) != record["candidate_sha256"]:
            raise ReloadError("catalog candidate changed")
        before = (self.scheduler.catalog_epoch, self.scheduler.store.catalog_checkpoint())
        proof = self.verifier(copy.deepcopy(marker_record), deadline=deadline)
        if before != (self.scheduler.catalog_epoch, self.scheduler.store.catalog_checkpoint()):
            raise ReloadError("catalog verifier changed runtime state")
        if (not isinstance(proof, RecoveryProof) or proof.marker_sha256 != record["marker_sha256"]
                or proof.instance != CandidateBinding.from_dict(record["binding"]).instance
                or not all(v is True for v in (proof.generation_confirmed, proof.instance_confirmed,
                                               proof.settlement_confirmed, proof.cleanup_confirmed))):
            raise ReloadError("catalog adoption or settlement unconfirmed")
        if (self.queue.clock() >= deadline or digest(self.queue._read()[0]) != record["candidate_sha256"]
                or sources(self.scheduler.config) != record["new_manifest"]["sources"]):
            raise ReloadError("catalog changed during confirmation")
        self._enabled()
        return proof

    def _release_ready(self, record):
        self._enabled()
        if (self.retired or self.queue.fenced or self.scheduler.store.catalog_checkpoint() != record
                or self.epoch != record["new_epoch"]
                or digest(self.queue._read()[0]) != record["candidate_sha256"]):
            raise ReloadError("catalog receipt or configuration changed before release")
        self._check_membership(record["new_manifest"]["active"], record["old_manifest"]["active"])

    def _after_apply(self, deadline):
        record = self.scheduler.store.catalog_checkpoint()
        marker, raw, _ = self.queue._read_marker()
        if (record != self.pending or marker.get("sha256") != record["candidate_sha256"]
                or marker.get("witness_binding") != record["binding"]):
            raise ReloadError("catalog marker binding changed")
        bound = {**record, "marker_sha256": digest(raw), "marker_json": raw.decode("utf-8")}
        self.scheduler.store.save_catalog(record, bound)
        self.pending = bound
        self._proof(bound, marker, deadline=min(deadline,self.deadline))
        self._check_membership(bound["new_manifest"]["active"], bound["old_manifest"]["active"])
        published = {**bound, "phase": "published"}
        self.scheduler.store.save_catalog(bound, published)
        self.pending = published
        self._publish(published["new_manifest"], published["new_epoch"], self.staged)
        self.staged = None

    def process_once(self):
        s = self.scheduler
        result = None
        owned = False
        try:
            with s.action_lock:
                self._enabled(); self._idle()
                if self.busy or s.catalog_fenced:
                    raise ReloadError("catalog reconciliation required")
                if not self.queue._pending:
                    return None
                job = self.queue._pending[0]
                prepared = self.jobs.get(job.id)
                if prepared is None or prepared.epoch != self.epoch:
                    raise ReloadError("unbound catalog job")
                if self.queue._blockers(job):
                    return self.queue.process_once()
                manifest = json.loads(prepared.manifest_json)
                if manifest["sources"] != sources(s.config):
                    raise ReloadError("catalog source settings changed")
                self.deadline = min(job.submitted_at+self.queue.timeout, self.queue.clock()+self.queue.operation_timeout)
                self.staged = self._construct(manifest)
                self.busy = True
                owned = True
                s.catalog_fenced = True
                previous = s.store.catalog_checkpoint()
                if previous is not None and previous["phase"] == "aborted":
                    previous = previous["previous"]
                if previous is not None:
                    previous = {**previous, "previous": None}
                record = {"transaction_id": uuid.uuid4().hex, "job_id": job.id, "old_epoch": self.epoch,
                    "new_epoch": uuid.uuid4().hex, "phase": "claimed", "base_sha256": prepared.base_sha256,
                    "candidate_sha256": prepared.binding.candidate_sha256, "marker_sha256": None, "marker_json": None,
                    "binding": prepared.binding.to_dict(), "old_manifest": self.manifest, "new_manifest": manifest, "previous": previous}
                s.store.save_catalog(s.store.catalog_checkpoint(), record)
                self.pending = record
                result = self.queue.process_once()
                if result and result["status"] == "applied":
                    pass  # Retire old objects outside lock before final durable release.
                elif (result and not result["config_committed"] and not self.queue.fenced
                      and digest(self.queue._read()[0]) == record["base_sha256"]):
                    s.store.save_catalog(record, {**record, "phase": "aborted"})
                    self.pending = None
                    s.catalog_fenced = False
                    return result
                else:
                    return result
            self.retire()
            with s.action_lock:
                record = s.store.catalog_checkpoint()
                if record != self.pending or record["phase"] != "published" or self.queue.fenced:
                    raise ReloadError("catalog retirement is not complete")
                marker = json.loads(record["marker_json"])
                self._proof(record, marker, deadline=self.deadline)
                self._release_ready(record)
                s.store.save_catalog(record, {**record, "phase": "released", "previous": None})
                self.pending = None
                s.catalog_fenced = False
                s.emit("catalog_installed", detail={"catalog_epoch": self.epoch, "job_id": record["job_id"]})
                return result
        finally:
            if owned:
                try:
                    if self.retired:
                        self.retire()
                    if self.staged is not None:
                        collector, relay, _ = self.staged
                        self.staged = None
                        if relay is not None: relay.close()
                        if collector is not None and hasattr(collector, "close"): collector.close()
                finally:
                    self.busy = False

    def reconcile(self, *, dry_run=False):
        """Explicit proof/install/retirement; never resubmit an old config job."""
        if dry_run:
            return {"would": [{"kind": "reconcile_catalog"}]}
        s = self.scheduler
        deadline = self.queue.clock()+self.queue.operation_timeout
        with s.action_lock:
            self._enabled(); self._idle()
            if self.busy:
                raise ReloadError("catalog operation in progress")
            record = s.store.catalog_checkpoint()
            if record is not None and record["phase"] == "aborted" and record["previous"] is not None:
                record = s.store.restore_catalog_abort(record)
            if record is None or record["phase"] == "aborted":
                raise ReloadError("no catalog transaction to reconcile")
            self.busy = True
            s.catalog_fenced = True
        try:
            with s.action_lock:
                if record["marker_json"] is None:
                    marker, raw, _ = self.queue._read_marker()
                    if marker.get("sha256") != record["candidate_sha256"] or marker.get("witness_binding") != record["binding"]:
                        raise ReloadError("catalog recovery marker mismatch")
                    bound = {**record, "marker_json": raw.decode("utf-8"), "marker_sha256": digest(raw)}
                    s.store.save_catalog(record, bound)
                    record = bound
                marker = json.loads(record["marker_json"])
                self._proof(record, marker, deadline=deadline)
                self._check_membership(record["new_manifest"]["active"], record["old_manifest"]["active"])
                if self.epoch != record["new_epoch"]:
                    bundle = self._construct(record["new_manifest"])
                    try:
                        if record["phase"] == "claimed":
                            published = {**record, "phase": "published"}
                            s.store.save_catalog(record, published)
                            record = published
                        self._publish(record["new_manifest"], record["new_epoch"], bundle)
                    except BaseException:
                        # Once published, objects belong to the scheduler and
                        # the durable fence remains held through any failure.
                        if s.collect is not bundle[0]:
                            if bundle[1] is not None: bundle[1].close()
                            if bundle[0] is not None and hasattr(bundle[0], "close"): bundle[0].close()
                        raise
                self.pending = record
            self.retire()
            with s.action_lock:
                if s.store.catalog_checkpoint() != record:
                    raise ReloadError("catalog checkpoint changed during retirement")
                confirm = lambda saved: self._proof(record, saved, deadline=deadline)
                if hasattr(self.queue, "confirm_retired_receipt"):
                    self.queue.confirm_retired_receipt(record["marker_json"].encode(), confirm)
                elif self.queue.marker.exists():
                    self.queue.reconcile(confirm)
                else:
                    raise ReloadError("catalog receipt retirement verifier unavailable")
                self._proof(record, marker, deadline=deadline)
                self._release_ready(record)
                if record["phase"] != "released":
                    s.store.save_catalog(record, {**record, "phase": "released", "previous": None})
                s.catalog_fenced = False
                self.pending = None
                return {"status": "reconciled", "catalog_epoch": self.epoch}
        finally:
            self.busy = False
