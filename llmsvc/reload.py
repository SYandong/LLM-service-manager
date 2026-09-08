# Generated-By: Codex / gpt-6-astra
"""Fail-closed, serialized quiet-period configuration transactions.

The scheduler supplies its action lock and feeds a continuous inflight stream.
Polling a zero count does not prove a quiet period. All paths are configured;
this module does not signal or modify a production service by itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from llmsvc.state import StateSnapshot


class ReloadError(RuntimeError):
    pass


class ValidationError(ReloadError):
    pass


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reload_blockers(snapshot: StateSnapshot, now: float, max_age: float = 30) -> list[dict]:
    """Admit the whole awake batch; no eviction to make reload fit in RAM."""
    if (not _number(snapshot.sampled_at) or not 0 <= now - snapshot.sampled_at <= max_age
            or snapshot.errors):
        return [{"reason": "state_unknown_or_stale"}]
    blockers = [{"reason": "read_only"}] if snapshot.read_only else []
    pins = {pin.model: pin.until for pin in snapshot.pins if pin.until > now}
    activity = {item.model: item for item in snapshot.activity}
    awake = sleeping = 0.0
    for model in snapshot.models:
        item = activity.get(model.name)
        if item is None or not _number(item.in_flight) or item.in_flight < 0:
            blockers.append({"model": model.name, "reason": "activity_unknown"})
        elif item.in_flight:
            blockers.append({"model": model.name, "reason": "in_flight"})
        if model.state not in {"awake", "sleeping", "stopped"}:
            blockers.append({"model": model.name, "reason": "state_unknown"})
        if model.state == "awake" and model.name in pins:
            blockers.append({"model": model.name, "reason": "pinned_until", "until": pins[model.name]})
        if model.state in {"awake", "sleeping"}:
            if not _number(model.weights_gb) or model.weights_gb < 0:
                blockers.append({"model": model.name, "reason": "weights_unknown"})
            elif model.state == "awake":
                awake += model.weights_gb
            else:
                sleeping += model.weights_gb
    blockers.extend({"model": lease.model, "reason": "active_lease"}
                    for lease in snapshot.leases if lease.status in {"pending", "stale"})
    memory = snapshot.memory
    amounts = (memory.host_available_gb, memory.sleeping_weights_gb,
               memory.budget_gb, memory.host_min_available_gb)
    if not all(_number(value) and value >= 0 for value in amounts):
        blockers.append({"reason": "memory_unknown"})
    elif (max(memory.sleeping_weights_gb, sleeping) + awake > memory.budget_gb
          or memory.host_available_gb - awake < memory.host_min_available_gb):
        blockers.append({"reason": "memory_budget", "awake_weights_gb": awake})
    return blockers


class QuietPeriod:
    """Every inflight event and stream gap must be delivered in order."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, *,
                 quiet_seconds: float = 5, max_event_age: float = 2):
        if not _number(quiet_seconds) or quiet_seconds < 5:
            raise ValueError("quiet_seconds must be at least 5")
        if not _number(max_event_age) or max_event_age <= 0:
            raise ValueError("max_event_age must be positive")
        self.clock, self.quiet_seconds, self.max_event_age = clock, quiet_seconds, max_event_age
        self._lock = threading.Lock()
        self._last: float | None = None
        self._zero: float | None = None
        self._inflight: int | None = None

    def observe(self, inflight: int | None, *, connected: bool = True) -> None:
        with self._lock:
            now = self.clock()
            if not connected or isinstance(inflight, bool) or not isinstance(inflight, int) or inflight < 0:
                self._last = self._zero = self._inflight = None
                return
            gap = self._last is None or not 0 <= now - self._last <= self.max_event_age
            if inflight:
                self._zero = None
            elif gap or self._zero is None:
                self._zero = now
            self._last, self._inflight = now, inflight

    def heartbeat(self) -> None:
        """An intact stream heartbeat; never use this for a disconnected stream."""
        with self._lock:
            now = self.clock()
            if self._last is None or not 0 <= now - self._last <= self.max_event_age:
                self._last = self._zero = self._inflight = None
            else:
                self._last = now

    def blockers(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            if self._last is None or not 0 <= now - self._last <= self.max_event_age:
                return [{"reason": "inflight_stream_unknown"}]
            if self._inflight:
                return [{"reason": "in_flight"}]
            if self._zero is None or now - self._zero < self.quiet_seconds:
                return [{"reason": "quiet_period"}]
            return []


class CommandValidator:
    def __init__(self, binary: str, timeout: float = 10):
        if not binary or not _number(timeout) or timeout <= 0:
            raise ValueError("validator needs a binary and positive timeout")
        self.binary, self.timeout = binary, timeout

    def __call__(self, path: Path) -> None:
        try:
            result = subprocess.run([self.binary, "-config", str(path), "-validate"],
                                    capture_output=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("configuration validator unavailable or timed out") from exc
        if result.returncode:
            # Do not expose model commands or possible credentials from validator output.
            raise ValidationError(f"configuration validator failed (exit {result.returncode})")


@dataclass
class ReloadJob:
    id: str
    description: dict
    submitted_at: float
    transform: Callable[[bytes], bytes] = field(repr=False)
    precheck: Callable[[], list[dict]] | None = field(default=None, repr=False)
    after_apply: Callable[..., None] | None = field(default=None, repr=False)
    status: str = "queued"
    blocked_by: list[dict] = field(default_factory=list)
    config_committed: bool = False
    error: str | None = None
    apply_seconds: float | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "description": dict(self.description), "status": self.status,
                "blocked_by": [dict(item) for item in self.blocked_by], "error": self.error,
                "config_committed": self.config_committed, "apply_seconds": self.apply_seconds}


class ReloadQueue:
    """FIFO queue. Call process_once on scheduler ticks; it never sleeps.

    notify_reload(*, deadline) and after_apply(*, deadline) must use bounded I/O
    respecting the monotonic deadline; Python callbacks cannot safely be preempted
    while holding the action lock. notify_reload must CONFIRM adoption, not just
    send a signal. With
    -watch-config it confirms the watcher reload without an extra SIGHUP.
    An interrupted commit leaves a durable marker and blocks subsequent writes.
    Queued, uncommitted requests are ephemeral and must be resubmitted on restart.
    """

    def __init__(self, config_path: Path, *, action_lock: Any, quiet: QuietPeriod,
                 snapshot: Callable[[], StateSnapshot], validate: Callable[[Path], None],
                 notify_reload: Callable[..., None], log: Callable[[dict], None],
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time, timeout: float = 600,
                 max_snapshot_age: float = 30, operation_timeout: float = 10):
        if not _number(timeout) or not 0 < timeout <= 600:
            raise ValueError("timeout must be positive and at most 600 seconds")
        if not isinstance(action_lock, type(threading.RLock())):
            raise ValueError("action_lock must be the scheduler threading.RLock")
        if not _number(operation_timeout) or not 0 < operation_timeout <= 60:
            raise ValueError("operation_timeout must be positive and at most 60 seconds")
        self.operation_timeout = operation_timeout
        self.path = Path(config_path)
        self.marker = self.path.with_name(self.path.name + ".llmsvc-pending")
        self.action_lock, self.quiet, self.snapshot = action_lock, quiet, snapshot
        self.validate, self.notify_reload, self.log = validate, notify_reload, log
        self.clock, self.wall_clock, self.timeout = clock, wall_clock, timeout
        self.max_snapshot_age = max_snapshot_age
        self._pending: deque[ReloadJob] = deque()
        self._jobs: dict[str, ReloadJob] = {}

    def _read(self) -> tuple[bytes, os.stat_result]:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReloadError("config must be a regular non-hardlinked file")
            return stream.read(), info

    def _sync_directory(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _stage(self, data: bytes, info: os.stat_result) -> Path:
        fd, name = tempfile.mkstemp(prefix=".llmsvc-", suffix=".yaml", dir=self.path.parent)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                if (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()):
                    os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.validate(path)
            if path.read_bytes() != data:
                raise ValidationError("validator modified candidate")
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def enqueue(self, transform: Callable[[bytes], bytes], *, description: dict,
                dry_run: bool = False, precheck: Callable[[], list[dict]] | None = None,
                after_apply: Callable[..., None] | None = None) -> dict:
        """Pure transforms only. Dry-run creates no staging files or queue entries."""
        with self.action_lock:
            if self.marker.exists() or self.marker.is_symlink():
                raise ReloadError("previous transaction requires reconciliation")
            data, info = self._read()
            for job in self._pending:
                data = job.transform(data)
            candidate = transform(data)
            if not isinstance(candidate, bytes):
                raise TypeError("config transform must return bytes")
            if dry_run:
                result = {"would": [dict(description)]}
                self.log({"kind": "config_change", "dry_run": True, **result})
                return result
            staged = self._stage(candidate, info)
            staged.unlink()
            job = ReloadJob(uuid.uuid4().hex, dict(description), self.clock(), transform, precheck, after_apply)
            self._pending.append(job)
            self._jobs[job.id] = job
            self.log({"kind": "config_change_queued", "dry_run": False, **job.to_dict()})
            return job.to_dict()

    def get(self, job_id: str) -> dict:
        with self.action_lock:
            return self._jobs[job_id].to_dict()

    def _blockers(self, job: ReloadJob) -> list[dict]:
        return (self.quiet.blockers()
                + reload_blockers(self.snapshot(), self.wall_clock(), self.max_snapshot_age)
                + (job.precheck() if job.precheck else []))

    def process_once(self) -> dict | None:
        with self.action_lock:
            if not self._pending or self.marker.exists() or self.marker.is_symlink():
                return None
            job = self._pending[0]
            if self.clock() - job.submitted_at >= self.timeout:
                job.status, job.error = "timed_out", "no safe reload within timeout"
                self._pending.popleft()
                self.log({"kind": "config_change_timeout", **job.to_dict()})
                return job.to_dict()
            job.blocked_by = self._blockers(job)
            if job.blocked_by:
                return job.to_dict()
            staged = None
            marker_created = False
            try:
                original, info = self._read()
                candidate = job.transform(original)
                staged = self._stage(candidate, info)
                # Validation may take time or overlap an inflight event.
                job.blocked_by = self._blockers(job)
                if job.blocked_by:
                    return job.to_dict()
                if self.clock() - job.submitted_at >= self.timeout:
                    job.status, job.error = "timed_out", "no safe reload within timeout"
                else:
                    current, current_info = self._read()
                    if ((current_info.st_dev, current_info.st_ino) != (info.st_dev, info.st_ino)
                            or current != original):
                        raise ReloadError("config changed during validation")
                    digest = hashlib.sha256(candidate).hexdigest()
                    with self.marker.open("x", encoding="utf-8") as stream:
                        marker_created = True
                        json.dump({"sha256": digest, "job": job.to_dict()}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    self._sync_directory()
                    job.blocked_by = self._blockers(job)
                    expired = self.clock() - job.submitted_at >= self.timeout
                    if job.blocked_by or expired:
                        self.marker.unlink()
                        self._sync_directory()
                        marker_created = False
                        if expired:
                            job.status, job.error = "timed_out", "no safe reload within timeout"
                            self._pending.popleft()
                            self.log({"kind": "config_change_timeout", **job.to_dict()})
                        return job.to_dict()
                    started = self.clock()
                    os.replace(staged, self.path)
                    staged = None
                    job.config_committed = True
                    self._sync_directory()
                    deadline = min(job.submitted_at + self.timeout, started + self.operation_timeout)
                    self._check_deadline(deadline)
                    self.notify_reload(deadline=deadline)
                    self._check_deadline(deadline)
                    job.apply_seconds = self.clock() - started
                    if job.after_apply:
                        job.after_apply(deadline=deadline)
                    self._check_deadline(deadline)
                    self.marker.unlink()
                    self._sync_directory()
                    marker_created = False
                    job.status = "applied"
            except Exception as exc:
                job.status = "reconciliation_required" if job.config_committed else "failed"
                job.error = f"{type(exc).__name__}: configuration transaction failed"
                if marker_created and not job.config_committed:
                    self.marker.unlink(missing_ok=True)
                    self._sync_directory()
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)
            self._pending.popleft()
            self.log({"kind": "config_change_result", "residual_interruption_risk": True, **job.to_dict()})
            return job.to_dict()

    def _check_deadline(self, deadline: float) -> None:
        if self.clock() >= deadline:
            raise ReloadError("configuration operation deadline exceeded")

    def reconcile(self, confirm: Callable[[dict], bool], *, dry_run: bool = False) -> dict:
        """Clear a crash marker only after external adoption AND cleanup checks.

        confirm is a read-only integration callback, never a retry of the reload.
        If disk has not reached the candidate, explicit operator recovery is needed.
        """
        with self.action_lock:
            if dry_run:
                result = {"would": [{"kind": "reconcile_config"}]}
                self.log({"kind": "config_reconcile", "dry_run": True})
                return result
            if self.marker.is_symlink():
                raise ReloadError("reconciliation marker is a symlink")
            record = json.loads(self.marker.read_text())
            data, _ = self._read()
            if hashlib.sha256(data).hexdigest() != record["sha256"] or not confirm(record):
                raise ReloadError("adoption and cleanup not confirmed")
            self.marker.unlink()
            self._sync_directory()
            self.log({"kind": "config_reconcile", "dry_run": False})
            return {"status": "reconciled"}
