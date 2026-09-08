# Generated-By: Codex / gpt-6-astra
"""SQLite intent persistence under the scheduler's single accounting lock.

Opening read_only mode and every dry-run method perform zero database writes.
Expiry is a read filter, so observation never deletes protection records.
"""

import json
import logging
import math
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from llmsvc.state import FaultClaim, Lease, Pin, Reserve

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
                if version not in (0, 1, 2, 3):
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
                self._has_faults = version == 3
                if self._has_faults:
                    self._db.execute("SELECT lease_id, model, stage, record FROM llmsvc_faults LIMIT 0")
                if self._has_leases:
                    self._db.execute("SELECT lease_id, model, gpu, util, expires_at, budget_gb, status, unit FROM llmsvc_leases LIMIT 0")
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
                self._db.execute("PRAGMA user_version = 3")
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

    def create_lease(self, lease, unit, *, dry_run=False):
        nonempty(lease.lease_id, "lease_id")
        nonempty(lease.model, "model")
        nonempty(unit, "unit")
        finite_positive(lease.expires_at, "expires_at")
        finite_positive(lease.budget_gb, "budget_gb")
        if type(lease.gpu) is not int or lease.gpu < 0 or not 0 < finite_positive(lease.util, "util") <= 1 or lease.status != "pending":
            raise ValueError("invalid lease")
        with self.action_lock:
            if self.fault(lease.model) is not None:
                raise ValueError("fault recovery pending")
            if not dry_run:
                self._write("INSERT INTO llmsvc_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (*asdict(lease).values(), unit))
        return intent_result("place", asdict(lease), dry_run)

    def transition_lease(self, lease_id, status, *, dry_run=False):
        if status not in ("confirmed", "stale", "released"):
            raise ValueError("invalid lease transition")
        with self.action_lock:
            row = self.lease(lease_id)
            if row is None or row[0].status == "released":
                raise ValueError("lease is absent or revoked")
            if self.fault(row[0].model) is not None:
                raise ValueError("fault recovery pending")
            if not dry_run:
                self._write("UPDATE llmsvc_leases SET status = ? WHERE lease_id = ?", (status, lease_id))
        return intent_result("lease_" + status, {"lease_id": lease_id, "status": status}, dry_run)
