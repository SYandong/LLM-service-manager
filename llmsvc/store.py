# Generated-By: Codex / gpt-6-astra
"""SQLite intent persistence under the scheduler's single accounting lock.

Opening read_only mode and every dry-run method perform zero database writes.
Expiry is a read filter, so observation never deletes protection records.
"""

import json
import logging
import math
import re
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from llmsvc.state import FaultClaim, Lease, Pin, RecoveryClaim, Reserve

LOG = logging.getLogger("llmsvc.store")


def intent_result(kind, record, dry_run):
    action = {"kind": kind, **record}
    LOG.info(json.dumps({"kind": "intent_operation", "dry_run": dry_run, "intent": action}, allow_nan=False))
    return {"would": [action]} if dry_run else record


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def finite_positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def validate_pin(pin):
    nonempty(pin.model, "model")
    nonempty(pin.by, "by")
    finite_positive(pin.until, "until")


def validate_reserve(reserve):
    nonempty(reserve.id, "id")
    nonempty(reserve.by, "by")
    if type(reserve.gpu) is not int or reserve.gpu < 0:
        raise ValueError("gpu must be a nonnegative integer")
    finite_positive(reserve.size_gb, "size_gb")
    finite_positive(reserve.until, "until")


class IntentStore:
    def __init__(self, path, *, action_lock, read_only=False):
        self.action_lock = action_lock
        self.read_only = read_only
        uri = Path(path).resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rwc")
        self._db = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        try:
            with self.action_lock, self._db:
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3, 4, 5, 6):
                    raise ValueError("unsupported intent database version")
                if read_only:
                    self._db.execute("SELECT model, until, owner FROM llmsvc_pins LIMIT 0")
                    self._db.execute("SELECT id, gpu, size_gb, until, owner FROM llmsvc_reserves LIMIT 0")
                else:
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_pins (model TEXT PRIMARY KEY, until REAL NOT NULL, owner TEXT NOT NULL)")
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_reserves (id TEXT PRIMARY KEY, gpu INTEGER NOT NULL, size_gb REAL NOT NULL, until REAL NOT NULL, owner TEXT NOT NULL)")
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_leases (lease_id TEXT PRIMARY KEY, model TEXT NOT NULL, gpu INTEGER NOT NULL, util REAL NOT NULL, expires_at REAL NOT NULL, budget_gb REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','stale','confirmed','released')), unit TEXT NOT NULL)")
                    self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_allocation ON llmsvc_leases(model) WHERE status != 'released'")
                    self._db.execute("PRAGMA user_version = " + str(max(version, 2)))
                self._has_leases = not read_only or version >= 2
                self._has_faults = version >= 3
                self._has_recoveries = version >= 4
                self._has_catalog = version >= 5
                self._has_maintenance = version >= 6
                if self._has_faults:
                    self._db.execute("SELECT lease_id, model, stage, record FROM llmsvc_faults LIMIT 0")
                if self._has_leases:
                    self._db.execute("SELECT lease_id, model, gpu, util, expires_at, budget_gb, status, unit FROM llmsvc_leases LIMIT 0")
                if self._has_recoveries:
                    self._db.execute("SELECT id, model, stage, record FROM llmsvc_recoveries LIMIT 0")
                    self.recoveries()  # Reject unsupported/corrupt active fences.
                if self._has_catalog:
                    self.catalog_checkpoint()
                if self._has_maintenance:
                    self._db.execute("SELECT transaction_id, record FROM llmsvc_maintenance LIMIT 0")
                    self.maintenance_checkpoints()
        except Exception:
            self._db.close()
            raise

    def faults(self):
        """Read unfinished fences, including when fault execution is disabled."""
        with self.action_lock:
            if not self._has_faults:
                return ()
            return tuple(FaultClaim(**json.loads(row[0])) for row in self._db.execute(
                "SELECT record FROM llmsvc_faults WHERE stage != 'complete' ORDER BY lease_id"))

    def catalog_checkpoint(self):
        from llmsvc.catalog_state import validate_checkpoint
        with self.action_lock:
            if not self._has_catalog:
                return None
            size = self._db.execute("SELECT length(CAST(record AS BLOB)) FROM llmsvc_catalog WHERE singleton=1").fetchone()
            from llmsvc.catalog_state import MAX_CHECKPOINT_BYTES
            if size is None or type(size[0]) is not int or size[0] > MAX_CHECKPOINT_BYTES:
                raise ValueError("invalid catalog checkpoint size")
            rows = self._db.execute("SELECT record FROM llmsvc_catalog WHERE singleton=1").fetchall()
            if len(rows) != 1:
                raise ValueError("catalog checkpoint absent")
            record = json.loads(rows[0][0])
            validate_checkpoint(record)
            return record

    def catalog_pending(self):
        record = self.catalog_checkpoint()
        if record is None:
            return False
        maintenance = self.maintenance_checkpoint(record["transaction_id"])
        return (record["phase"] not in ("released", "aborted", "rolled_back")
                or maintenance is not None and maintenance["stage"] not in ("released", "rolled_back", "aborted"))

    def _catalog_allows_accounting(self):
        if self.catalog_pending():
            raise ValueError("catalog reconciliation pending")

    def save_catalog(self, expected, record, *, dry_run=False, maintenance=None):
        """Compare-and-swap checkpoint; runtime verifies proof before phase changes."""
        from llmsvc.catalog_state import catalog_json, validate_checkpoint
        validate_checkpoint(record)
        if maintenance is not None:
            from llmsvc.maintenance_state import validate_maintenance
            validate_maintenance(maintenance)
            if (record["phase"] != "claimed" or maintenance["stage"] != "claimed" or maintenance["effects"]
                    or any(maintenance[key] != record[key] for key in ("transaction_id", "job_id", "base_sha256", "candidate_sha256"))):
                raise ValueError("maintenance initial claim mismatch")
            from llmsvc.maintenance import instance
            from llmsvc.reload_witness import CandidateBinding
            binding = CandidateBinding.from_dict(record["binding"])
            if instance(maintenance["old_identity"]) != binding.instance or maintenance["generation"] != binding.generation:
                raise ValueError("maintenance source binding mismatch")
        with self.action_lock:
            if self.catalog_checkpoint() != expected:
                raise ValueError("catalog checkpoint changed")
            initial = expected is None or expected["phase"] in ("released", "aborted")
            if initial:
                if record["phase"] != "claimed":
                    raise ValueError("catalog must begin claimed")
                previous = expected
                if previous is not None and previous["phase"] == "aborted":
                    previous = previous["previous"]
                if previous is not None:
                    previous = {**previous, "previous": None}
                if record["previous"] != previous:
                    raise ValueError("catalog prior checkpoint mismatch")
                if expected is not None and record["transaction_id"] == expected["transaction_id"]:
                    raise ValueError("catalog transaction id reused")
            else:
                immutable = set(record)-{"phase", "marker_sha256", "marker_json", "previous"}
                if any(record[k] != expected[k] for k in immutable):
                    raise ValueError("catalog checkpoint identity changed")
                if expected["marker_sha256"] is not None and record["marker_sha256"] != expected["marker_sha256"]:
                    raise ValueError("catalog marker changed")
                if record["previous"] != expected["previous"] and not (record["phase"] == "released" and record["previous"] is None):
                    raise ValueError("catalog prior checkpoint changed")
                transitions = {"claimed": {"claimed", "published", "aborted"}, "published": {"published", "released"}}
                if record["phase"] not in transitions.get(expected["phase"], set()):
                    raise ValueError("catalog phase transition rejected")
            if dry_run:
                return {"would": [{"kind": "catalog_"+record["phase"]}]}
            if self.read_only:
                raise PermissionError("intent store is read-only")
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                if self.catalog_checkpoint() != expected:
                    raise ValueError("catalog changed before commit")
                self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_faults (lease_id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL CHECK(stage IN ('claimed','released','complete')), record TEXT NOT NULL)")
                self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_fault ON llmsvc_faults(model) WHERE stage != 'complete'")
                self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_recoveries (id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL, record TEXT NOT NULL)")
                self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_recovery ON llmsvc_recoveries(model) WHERE stage != 'complete'")
                self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_catalog (singleton INTEGER PRIMARY KEY CHECK(singleton=1), record TEXT NOT NULL)")
                self._db.execute("INSERT OR REPLACE INTO llmsvc_catalog VALUES (1, ?)", (catalog_json(record),))
                if record["phase"] == "aborted" and self._has_maintenance:
                    old_maintenance = self.maintenance_checkpoint(record["transaction_id"])
                    if old_maintenance is not None:
                        if old_maintenance["effects"]:
                            raise ValueError("maintenance effects cannot be silently aborted")
                        self._db.execute("UPDATE llmsvc_maintenance SET record=? WHERE transaction_id=?",
                            (json.dumps({**old_maintenance, "stage": "aborted"}, allow_nan=False), record["transaction_id"]))
                if maintenance is not None:
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_maintenance (transaction_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
                    self._db.execute("INSERT INTO llmsvc_maintenance VALUES (?, ?)",
                                     (maintenance["transaction_id"], json.dumps(maintenance, allow_nan=False)))
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                self._db.execute("PRAGMA user_version="+str(max(version, 6 if maintenance is not None else 5)))
            self._has_catalog = self._has_recoveries = self._has_faults = True
            self._has_maintenance = self._db.execute("PRAGMA user_version").fetchone()[0] >= 6
            return record

    def maintenance_checkpoints(self):
        from llmsvc.maintenance_state import MAX_MAINTENANCE_BYTES, validate_maintenance
        with self.action_lock:
            if not self._has_maintenance:
                return ()
            rows = []
            for transaction_id, length in self._db.execute("SELECT transaction_id, length(CAST(record AS BLOB)) FROM llmsvc_maintenance"):
                if type(length) is not int or length > MAX_MAINTENANCE_BYTES:
                    raise ValueError("maintenance checkpoint exceeds limit")
                raw = self._db.execute("SELECT record FROM llmsvc_maintenance WHERE transaction_id=?", (transaction_id,)).fetchone()[0]
                record = json.loads(raw)
                validate_maintenance(record)
                if record["transaction_id"] != transaction_id:
                    raise ValueError("maintenance transaction metadata mismatch")
                rows.append(record)
            return tuple(rows)

    def maintenance_checkpoint(self, transaction_id):
        return next((row for row in self.maintenance_checkpoints() if row["transaction_id"] == transaction_id), None)

    def save_maintenance(self, expected, record):
        from llmsvc.maintenance_state import validate_maintenance
        validate_maintenance(record)
        mutable = {"stage", "effects", "observations", "new_identity", "rollback_identity", "error"}
        if any(record[key] != expected[key] for key in set(record)-mutable):
            raise ValueError("maintenance identity changed")
        for key in ("new_identity", "rollback_identity"):
            if expected[key] is not None and expected[key] != record[key]:
                raise ValueError("maintenance instance changed")
        for key, old in expected["effects"].items():
            if key not in record["effects"] or (old["acknowledged"] and not record["effects"][key]["acknowledged"]):
                raise ValueError("maintenance effect receipt regressed")
        with self.action_lock:
            if self.read_only:
                raise PermissionError("intent store is read-only")
            checkpoint = self.catalog_checkpoint()
            if checkpoint is None or checkpoint["transaction_id"] != record["transaction_id"]:
                raise ValueError("maintenance catalog claim changed")
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                if self.maintenance_checkpoint(record["transaction_id"]) != expected:
                    raise ValueError("maintenance checkpoint changed")
                self._db.execute("UPDATE llmsvc_maintenance SET record=? WHERE transaction_id=?",
                                 (json.dumps(record, allow_nan=False), record["transaction_id"]))
        return record

    def release_maintenance_lease(self, transaction_id, lease_id, unit):
        """Only the exact removed target after core's positive unit-exit check."""
        with self.action_lock:
            if self.read_only:
                raise PermissionError("intent store is read-only")
            catalog = self.catalog_checkpoint()
            state = self.maintenance_checkpoint(transaction_id)
            row = self.lease(lease_id)
            if (catalog is None or catalog["transaction_id"] != transaction_id or state is None or row is None
                    or row[1] != unit or row[0].status != "confirmed"
                    or row[0].model not in catalog["old_manifest"]["active"]
                    or row[0].model in catalog["new_manifest"]["active"]
                    or not state["effects"].get("stop_model:"+row[0].model, {}).get("submitted")
                    or self.fault(row[0].model) or self.recovery(row[0].model)):
                raise ValueError("maintenance cleanup account is unbound")
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                if self.lease(lease_id) != row or self.maintenance_checkpoint(transaction_id) != state:
                    raise ValueError("maintenance cleanup account changed")
                self._db.execute("UPDATE llmsvc_leases SET status='released' WHERE lease_id=?", (lease_id,))
            return {"lease_id": lease_id, "status": "released"}

    def restore_catalog_abort(self, expected):
        """Restore the prior checkpoint only after a durably proven no-effect abort."""
        with self.action_lock:
            if (self.catalog_checkpoint() != expected or expected["phase"] != "aborted"
                    or expected["previous"] is None):
                raise ValueError("catalog abort checkpoint mismatch")
            if self.read_only:
                raise PermissionError("intent store is read-only")
            from llmsvc.catalog_state import catalog_json
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                if self.catalog_checkpoint() != expected:
                    raise ValueError("catalog abort changed")
                self._db.execute("UPDATE llmsvc_catalog SET record=? WHERE singleton=1", (catalog_json(expected["previous"]),))
            return expected["previous"]

    @staticmethod
    def _validate_recovery(claim):
        for key in ("id", "model", "source_lease_id", "unit", "invocation_id", "profile_hash"):
            nonempty(getattr(claim, key), key)
        if (not re.fullmatch(r"[0-9a-f]{32}", claim.id)
                or not re.fullmatch(r"[0-9a-f]{32}", claim.invocation_id)
                or not re.fullmatch(r"[0-9a-f]{64}", claim.profile_hash)
                or type(claim.source_gpu) is not int or claim.source_gpu < 0
                or type(claim.relocate) is not bool or claim.reason not in ("reserve", "cannot_wake")):
            raise ValueError("invalid recovery identity")
        finite_positive(claim.created_at, "created_at")
        if finite_positive(claim.util_floor, "util_floor") > 1:
            raise ValueError("invalid recovery util floor")
        finite_positive(claim.budget_floor_gb, "budget_floor_gb")
        for key in ("stop_submitted", "stop_acknowledged", "proxy_submitted", "proxy_acknowledged",
                    "wake_submitted", "wake_acknowledged"):
            if type(getattr(claim, key)) is not bool:
                raise ValueError("invalid recovery progress")
        for prefix in ("stop", "proxy", "wake"):
            if getattr(claim, prefix+"_acknowledged") and not getattr(claim, prefix+"_submitted"):
                raise ValueError("unsubmitted recovery acknowledgment")
        if claim.stage not in ("claimed", "released", "settled", "waking", "destination", "complete"):
            raise ValueError("unsupported recovery stage")
        if claim.stage == "claimed" and (claim.proxy_submitted or claim.wake_submitted or claim.destination_lease_id):
            raise ValueError("invalid claimed recovery")
        if claim.stage not in ("claimed", "complete") and not claim.stop_submitted:
            raise ValueError("source stop unsubmitted")
        if claim.proxy_submitted and not claim.stop_acknowledged:
            raise ValueError("source stop unacknowledged")
        if claim.wake_submitted and (not claim.proxy_acknowledged or claim.stage not in ("waking", "destination", "complete")):
            raise ValueError("proxy cleanup unsettled")
        if claim.stage in ("settled", "waking", "destination") and not claim.proxy_acknowledged:
            raise ValueError("proxy cleanup unacknowledged")
        if claim.stage in ("waking", "destination") and (not claim.relocate or not claim.wake_submitted):
            raise ValueError("cold wake unsubmitted")
        if (claim.stage == "destination") != bool(claim.destination_lease_id) and claim.stage != "complete":
            raise ValueError("invalid recovery destination binding")
        if claim.destination_lease_id is not None:
            nonempty(claim.destination_lease_id, "destination_lease_id")
        if claim.destination_invocation_id and (not claim.destination_lease_id
                or not re.fullmatch(r"[0-9a-f]{32}", claim.destination_invocation_id)
                or claim.destination_invocation_id == claim.invocation_id):
            raise ValueError("invalid destination incarnation")
        if claim.wake_acknowledged and not claim.destination_invocation_id:
            raise ValueError("destination incarnation unconfirmed")
        if claim.error is not None and not isinstance(claim.error, str):
            raise ValueError("invalid recovery error")

    def recoveries(self):
        """Read active ordinary fences even when their worker is disabled."""
        with self.action_lock:
            if not self._has_recoveries:
                return ()
            claims = []
            for id_, model, stage, raw in self._db.execute(
                    "SELECT id, model, stage, record FROM llmsvc_recoveries WHERE stage != 'complete' ORDER BY id"):
                claim = RecoveryClaim(**json.loads(raw))
                self._validate_recovery(claim)
                if (claim.id, claim.model, claim.stage) != (id_, model, stage):
                    raise ValueError("recovery fence metadata mismatch")
                claims.append(claim)
            return tuple(claims)

    def recovery(self, model):
        return next((claim for claim in self.recoveries() if claim.model == model), None)

    def claim_recovery(self, claim, *, dry_run=False):
        self._validate_recovery(claim)
        if (claim.stage != "claimed" or claim.stop_submitted or claim.stop_acknowledged
                or claim.proxy_submitted or claim.wake_submitted or claim.destination_lease_id):
            raise ValueError("invalid initial recovery")
        with self.action_lock:
            self._catalog_allows_accounting()
            source = self.lease(claim.source_lease_id)
            if (source is None or (source[0].model, source[0].gpu, source[0].status, source[1]) !=
                    (claim.model, claim.source_gpu, "confirmed", claim.unit)
                    or claim.util_floor < source[0].util or claim.budget_floor_gb < source[0].budget_gb
                    or self.fault(claim.model) is not None or self.recovery(claim.model) is not None):
                raise ValueError("recovery source account unavailable")
            if dry_run:
                return intent_result("sleeping_recovery_claim", asdict(claim), True)
            if self.read_only:
                raise PermissionError("intent store is read-only")
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_faults (lease_id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL CHECK(stage IN ('claimed','released','complete')), record TEXT NOT NULL)")
                    self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_fault ON llmsvc_faults(model) WHERE stage != 'complete'")
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_recoveries (id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL, record TEXT NOT NULL)")
                    self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_recovery ON llmsvc_recoveries(model) WHERE stage != 'complete'")
                    if self.lease(claim.source_lease_id) != source or self._db.execute(
                            "SELECT 1 FROM llmsvc_faults WHERE model=? AND stage != 'complete'", (claim.model,)).fetchone():
                        raise ValueError("recovery source changed")
                    self._db.execute("INSERT INTO llmsvc_recoveries VALUES (?, ?, ?, ?)",
                                     (claim.id, claim.model, claim.stage, json.dumps(asdict(claim), allow_nan=False)))
                    self._db.execute("PRAGMA user_version = " + str(max(self._db.execute("PRAGMA user_version").fetchone()[0], 4)))
            finally:
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                self._has_faults, self._has_recoveries = version >= 3, version >= 4
        intent_result("sleeping_recovery_claim", asdict(claim), False)
        return claim

    def advance_recovery(self, claim, *, stage=None, error=None, dry_run=False,
                         destination_invocation_id=None, **progress):
        allowed = {"stop_submitted", "stop_acknowledged", "proxy_submitted", "proxy_acknowledged",
                   "wake_submitted", "wake_acknowledged"}
        if set(progress)-allowed or any(type(value) is not bool or (getattr(claim, key) and not value)
                                       for key, value in progress.items()):
            raise ValueError("invalid recovery progress update")
        for prefix in ("stop", "proxy", "wake"):
            if progress.get(prefix+"_acknowledged") and not getattr(claim, prefix+"_submitted"):
                raise ValueError("acknowledgment needs prior durable submission")
        stage = stage or claim.stage
        next_stage = {"claimed": "released", "released": "settled", "settled": "waking"}
        if stage != claim.stage and stage != next_stage.get(claim.stage) and stage != "complete":
            raise ValueError("invalid recovery stage transition")
        if destination_invocation_id is not None:
            if claim.destination_invocation_id and destination_invocation_id != claim.destination_invocation_id:
                raise ValueError("destination incarnation changed")
            progress["destination_invocation_id"] = destination_invocation_id
        updated = replace(claim, stage=stage, error=error, **progress)
        self._validate_recovery(updated)
        with self.action_lock:
            self._catalog_allows_accounting()
            if self.recovery(claim.model) != claim or self.fault(claim.model) is not None:
                raise ValueError("recovery claim changed")
            source = self.lease(claim.source_lease_id)
            expected = "confirmed" if claim.stage == "claimed" else "released"
            if source is None or (source[0].model, source[0].gpu, source[0].status, source[1]) != (
                    claim.model, claim.source_gpu, expected, claim.unit):
                raise ValueError("recovery source account changed")
            if stage == "complete":
                abort = claim.stage == "claimed" and not claim.stop_submitted
                retired = (claim.stage == "settled" and claim.proxy_acknowledged and not claim.wake_submitted
                           and (not claim.relocate or error is not None))
                destination = self.lease(claim.destination_lease_id) if claim.destination_lease_id else None
                ready = (claim.stage == "destination" and claim.wake_acknowledged and destination is not None
                         and (destination[0].model, destination[0].status, destination[1]) ==
                         (claim.model, "confirmed", claim.unit) and destination[0].gpu != claim.source_gpu)
                if not (abort or retired or ready):
                    raise ValueError("recovery completion unconfirmed")
            if not dry_run:
                if self.read_only:
                    raise PermissionError("intent store is read-only")
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    if self.recovery(claim.model) != claim or self.lease(claim.source_lease_id) != source:
                        raise ValueError("recovery changed before commit")
                    if claim.stage == "claimed" and stage == "released":
                        self._db.execute("UPDATE llmsvc_leases SET status='released' WHERE lease_id=?", (claim.source_lease_id,))
                    self._db.execute("UPDATE llmsvc_recoveries SET stage=?, record=? WHERE id=?",
                                     (stage, json.dumps(asdict(updated), allow_nan=False), claim.id))
        intent_result("sleeping_recovery_"+stage, asdict(updated), dry_run)
        return updated

    def fault(self, model):
        return next((claim for claim in self.faults() if claim.model == model), None)

    def claim_fault(self, claim: FaultClaim, *, dry_run=False):
        for key in ("lease_id", "model", "unit", "invocation_id", "reason", "proxy_origin_hash"):
            nonempty(getattr(claim, key), key)
        finite_positive(claim.proved_at, "proved_at")
        if (type(claim.gpu) is not int or claim.gpu < 0 or claim.stage != "claimed"
                or claim.proxy_submitted is not False or claim.proxy_acknowledged is not False):
            raise ValueError("invalid initial fault claim")
        record = asdict(claim)
        with self.action_lock:
            if self.recovery(claim.model) is not None:
                raise ValueError("sleeping recovery pending")
            self._catalog_allows_accounting()
            row = self.lease(claim.lease_id)
            if row is None or (row[0].model, row[0].gpu, row[0].status, row[1]) != (
                    claim.model, claim.gpu, "confirmed", claim.unit):
                raise ValueError("fault account changed")
            if dry_run:
                return intent_result("fault_claim", record, True)
            if self.read_only:
                raise PermissionError("intent store is read-only")
            # Lazy migration occurs only for an actual proven-fault claim. Keep
            # v2 behavior for ordinary writable stores, and no DDL in dry-run.
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_faults (lease_id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL CHECK(stage IN ('claimed','released','complete')), record TEXT NOT NULL)")
                self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_fault ON llmsvc_faults(model) WHERE stage != 'complete'")
                self._db.execute("INSERT INTO llmsvc_faults VALUES (?, ?, ?, ?)",
                                 (claim.lease_id, claim.model, claim.stage, json.dumps(record, allow_nan=False)))
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                self._db.execute("PRAGMA user_version = " + str(max(version, 3)))
            self._has_faults = True
        return intent_result("fault_claim", record, False)

    def advance_fault(self, claim: FaultClaim, *, stage, error=None, dry_run=False,
                      proxy_submitted=None, proxy_acknowledged=None):
        if stage not in (claim.stage, "released" if claim.stage == "claimed" else "complete"):
            raise ValueError("invalid fault transition")
        changes = {}
        for name, value in (("proxy_submitted", proxy_submitted), ("proxy_acknowledged", proxy_acknowledged)):
            if value is not None:
                if type(value) is not bool or (getattr(claim, name) and not value):
                    raise ValueError("invalid proxy progress transition")
                changes[name] = value
        if proxy_acknowledged is True and not claim.proxy_submitted:
            raise ValueError("proxy acknowledgment requires a prior durable submission")
        updated = replace(claim, stage=stage, error=error, **changes)
        if (updated.proxy_acknowledged and not updated.proxy_submitted
                or stage == "claimed" and updated.proxy_submitted
                or stage == "complete" and not updated.proxy_acknowledged):
            raise ValueError("fault proxy progress is unconfirmed")
        with self.action_lock:
            if self.fault(claim.model) != claim:
                raise ValueError("fault claim changed")
            self._catalog_allows_accounting()
            row = self.lease(claim.lease_id)
            if row is None or (row[0].model, row[0].gpu, row[1]) != (claim.model, claim.gpu, claim.unit):
                raise ValueError("fault account changed")
            expected = "confirmed" if claim.stage == "claimed" else "released"
            if row[0].status != expected:
                raise ValueError("fault account status changed")
            if not dry_run:
                if self.read_only:
                    raise PermissionError("intent store is read-only")
                with self._db:
                    if claim.stage == "claimed" and stage == "released":
                        self._db.execute("UPDATE llmsvc_leases SET status='released' WHERE lease_id=?", (claim.lease_id,))
                    self._db.execute("UPDATE llmsvc_faults SET stage=?, record=? WHERE lease_id=?",
                                     (stage, json.dumps(asdict(updated), allow_nan=False), claim.lease_id))
        intent_result("fault_" + stage, asdict(updated), dry_run)
        return updated

    def close(self):
        with self.action_lock:
            self._db.close()

    def _write(self, statement, values):
        if self.read_only:
            raise PermissionError("intent store is read-only")
        with self.action_lock, self._db:
            self._db.execute(statement, values)

    def put_pin(self, pin: Pin, *, dry_run=False):
        validate_pin(pin)
        if not dry_run:
            self._write("INSERT INTO llmsvc_pins VALUES (?, ?, ?) ON CONFLICT(model) DO UPDATE SET until=excluded.until, owner=excluded.owner",
                        (pin.model, pin.until, pin.by))
        return intent_result("pin", asdict(pin), dry_run)

    def remove_pin(self, model: str, *, dry_run=False):
        nonempty(model, "model")
        if not dry_run:
            self._write("DELETE FROM llmsvc_pins WHERE model = ?", (model,))
        return intent_result("unpin", {"model": model}, dry_run)

    def put_reserve(self, reserve: Reserve, *, dry_run=False):
        validate_reserve(reserve)
        if not dry_run:
            self._write("INSERT INTO llmsvc_reserves VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET gpu=excluded.gpu, size_gb=excluded.size_gb, until=excluded.until, owner=excluded.owner",
                        (reserve.id, reserve.gpu, reserve.size_gb, reserve.until, reserve.by))
        return intent_result("reserve", asdict(reserve), dry_run)

    def reserve(self, reserve_id):
        """Read one intent including expired rows, without changing expiry state."""
        nonempty(reserve_id, "id")
        with self.action_lock:
            row = self._db.execute("SELECT id, gpu, size_gb, until, owner FROM llmsvc_reserves WHERE id = ?", (reserve_id,)).fetchone()
            if row is None:
                return None
            reserve = Reserve(*row)
            validate_reserve(reserve)
            return reserve

    def remove_reserve(self, reserve_id: str, *, dry_run=False):
        nonempty(reserve_id, "id")
        if not dry_run:
            self._write("DELETE FROM llmsvc_reserves WHERE id = ?", (reserve_id,))
        return intent_result("unreserve", {"id": reserve_id}, dry_run)

    def active(self, now):
        finite_positive(now, "now")
        with self.action_lock:
            pins = tuple(Pin(*row) for row in self._db.execute(
                "SELECT model, until, owner FROM llmsvc_pins WHERE until > ? ORDER BY model", (now,)))
            reserves = tuple(Reserve(*row) for row in self._db.execute(
                "SELECT id, gpu, size_gb, until, owner FROM llmsvc_reserves WHERE until > ? ORDER BY id", (now,)))
            for pin in pins:
                validate_pin(pin)
            for reserve in reserves:
                validate_reserve(reserve)
            return pins, reserves

    def leases(self, *, include_released=False):
        """One current row per model; confirmed rows ARE durable daemon accounts.

        Released rows are tombstones for idempotency and late launcher fencing.
        Version-1 read-only opens neither migrate nor create a lease table.
        """
        with self.action_lock:
            if not self._has_leases:
                return ()
            query = "SELECT lease_id, model, gpu, util, expires_at, budget_gb, status, unit FROM llmsvc_leases"
            if not include_released:
                query += " WHERE status != 'released'"
            return tuple((Lease(*row[:7]), row[7]) for row in self._db.execute(query + " ORDER BY model, lease_id"))

    def lease(self, lease_id):
        with self.action_lock:
            if not self._has_leases:
                return None
            row = self._db.execute("SELECT lease_id, model, gpu, util, expires_at, budget_gb, status, unit FROM llmsvc_leases WHERE lease_id = ?", (lease_id,)).fetchone()
            return (Lease(*row[:7]), row[7]) if row else None

    def create_lease(self, lease, unit, *, dry_run=False, recovery_claim=None):
        nonempty(lease.lease_id, "lease_id")
        nonempty(lease.model, "model")
        nonempty(unit, "unit")
        finite_positive(lease.expires_at, "expires_at")
        finite_positive(lease.budget_gb, "budget_gb")
        if type(lease.gpu) is not int or lease.gpu < 0 or not 0 < finite_positive(lease.util, "util") <= 1 or lease.status != "pending":
            raise ValueError("invalid lease")
        with self.action_lock:
            self._catalog_allows_accounting()
            if self.fault(lease.model) is not None:
                raise ValueError("fault recovery pending")
            active = self.recovery(lease.model)
            if active is not None or recovery_claim is not None:
                source = self.lease(active.source_lease_id) if active is not None else None
                if (active is None or active != recovery_claim or active.stage != "waking"
                        or not active.relocate or not active.wake_submitted
                        or active.destination_lease_id is not None or lease.gpu == active.source_gpu
                        or lease.budget_gb < active.budget_floor_gb or unit != active.unit
                        or source is None or source[0].status != "released"):
                    raise ValueError("sleeping recovery placement is not authorized")
                if not dry_run:
                    if self.read_only:
                        raise PermissionError("intent store is read-only")
                    bound = replace(active, stage="destination", destination_lease_id=lease.lease_id)
                    with self._db:
                        self._db.execute("BEGIN IMMEDIATE")
                        if self.recovery(lease.model) != active:
                            raise ValueError("sleeping recovery changed")
                        self._db.execute("INSERT INTO llmsvc_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                         (*asdict(lease).values(), unit))
                        self._db.execute("UPDATE llmsvc_recoveries SET stage=?, record=? WHERE id=?",
                                         (bound.stage, json.dumps(asdict(bound), allow_nan=False), bound.id))
                return intent_result("place", asdict(lease), dry_run)
            if not dry_run:
                self._write("INSERT INTO llmsvc_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (*asdict(lease).values(), unit))
        return intent_result("place", asdict(lease), dry_run)

    def transition_lease(self, lease_id, status, *, dry_run=False):
        if status not in ("confirmed", "stale", "released"):
            raise ValueError("invalid lease transition")
        with self.action_lock:
            self._catalog_allows_accounting()
            row = self.lease(lease_id)
            if row is None or row[0].status == "released":
                raise ValueError("lease is absent or revoked")
            if self.fault(row[0].model) is not None:
                raise ValueError("fault recovery pending")
            recovery = self.recovery(row[0].model)
            if recovery is not None and lease_id != recovery.destination_lease_id:
                raise ValueError("sleeping recovery pending")
            if not dry_run:
                self._write("UPDATE llmsvc_leases SET status = ? WHERE lease_id = ?", (status, lease_id))
        return intent_result("lease_" + status, {"lease_id": lease_id, "status": status}, dry_run)
