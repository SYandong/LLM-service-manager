# Generated-By: Codex / gpt-6-astra
"""SQLite intent persistence under the scheduler's single accounting lock.

Opening read_only mode and every dry-run method perform zero database writes.
Expiry is a read filter, so observation never deletes protection records.
"""

import math
import sqlite3
from dataclasses import asdict
from pathlib import Path

from llmsvc.state import Pin, Reserve


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
                if version not in (0, 1):
                    raise ValueError("unsupported intent database version")
                if read_only:
                    self._db.execute("SELECT model, until, owner FROM llmsvc_pins LIMIT 0")
                    self._db.execute("SELECT id, gpu, size_gb, until, owner FROM llmsvc_reserves LIMIT 0")
                else:
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_pins (model TEXT PRIMARY KEY, until REAL NOT NULL, owner TEXT NOT NULL)")
                    self._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_reserves (id TEXT PRIMARY KEY, gpu INTEGER NOT NULL, size_gb REAL NOT NULL, until REAL NOT NULL, owner TEXT NOT NULL)")
                    self._db.execute("PRAGMA user_version = 1")
        except Exception:
            self._db.close()
            raise

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
        return {"would": [{"kind": "pin", **asdict(pin)}]} if dry_run else asdict(pin)

    def remove_pin(self, model: str, *, dry_run=False):
        nonempty(model, "model")
        if not dry_run:
            self._write("DELETE FROM llmsvc_pins WHERE model = ?", (model,))
        return {"would": [{"kind": "unpin", "model": model}]} if dry_run else {"model": model}

    def put_reserve(self, reserve: Reserve, *, dry_run=False):
        validate_reserve(reserve)
        if not dry_run:
            self._write("INSERT INTO llmsvc_reserves VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET gpu=excluded.gpu, size_gb=excluded.size_gb, until=excluded.until, owner=excluded.owner",
                        (reserve.id, reserve.gpu, reserve.size_gb, reserve.until, reserve.by))
        return {"would": [{"kind": "reserve", **asdict(reserve)}]} if dry_run else asdict(reserve)

    def remove_reserve(self, reserve_id: str, *, dry_run=False):
        nonempty(reserve_id, "id")
        if not dry_run:
            self._write("DELETE FROM llmsvc_reserves WHERE id = ?", (reserve_id,))
        return {"would": [{"kind": "unreserve", "id": reserve_id}]} if dry_run else {"id": reserve_id}

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
