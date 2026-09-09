# Generated-By: Codex / gpt-6-astra
"""Fail-closed, serialized quiet-period configuration transactions.

The scheduler supplies its action lock and feeds a continuous inflight stream.
Polling a zero count does not prove a quiet period. All paths are configured;
this module does not signal or modify a production service by itself.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
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
from llmsvc.reload_witness import (BindingObservation, CandidateBinding, GenerationRead,
                                   InstanceIdentity, check_visibility)


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
    witness_binding: CandidateBinding | None = field(default=None, repr=False)
    status: str = "queued"
    blocked_by: list[dict] = field(default_factory=list)
    config_committed: bool = False
    error: str | None = None
    apply_seconds: float | None = None

    def to_dict(self) -> dict:
        return copy.deepcopy({"id": self.id, "description": self.description, "status": self.status,
                              "blocked_by": self.blocked_by, "error": self.error,
                              "config_committed": self.config_committed, "apply_seconds": self.apply_seconds})


@dataclass(frozen=True)
class RecoveryProof:
    """Explicit trusted verifier attestation; never produced by a native read.

    All flags must be literally True. The marker digest binds the attestation to
    one recovery record. A persisted native binding also requires matching instance.
    These fields do not supply a new source of settlement/identity proof.
    """
    marker_sha256: str
    generation_confirmed: bool = False
    instance_confirmed: bool = False
    settlement_confirmed: bool = False
    cleanup_confirmed: bool = False
    instance: InstanceIdentity | None = None


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
                after_apply: Callable[..., None] | None = None,
                witness_binding: CandidateBinding | None = None) -> dict:
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
            self._validate_candidate_binding(witness_binding, candidate)
            if dry_run:
                result = {"would": [copy.deepcopy(description)]}
                self.log({"kind": "config_change", "dry_run": True, **result})
                return result
            staged = self._stage(candidate, info)
            staged.unlink()
            job = ReloadJob(uuid.uuid4().hex, copy.deepcopy(description), self.clock(), transform,
                            precheck, after_apply, witness_binding)
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
                self._validate_candidate_binding(job.witness_binding, candidate)
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
                    record = {"schema_version": 1, "sha256": digest, "job": job.to_dict()}
                    if job.witness_binding is not None:
                        record["witness_binding"] = job.witness_binding.to_dict()
                    marker_bytes = json.dumps(record, allow_nan=False).encode("utf-8")
                    if len(marker_bytes) > 65536:
                        raise ReloadError("recovery marker would exceed read limit")
                    with self.marker.open("xb") as stream:
                        marker_created = True
                        stream.write(marker_bytes)
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

    @staticmethod
    def _validate_candidate_binding(binding: CandidateBinding | None, candidate: bytes) -> None:
        if binding is not None:
            binding.to_dict()  # Strict, read-only shape validation.
            if binding.candidate_sha256 != hashlib.sha256(candidate).hexdigest():
                raise ReloadError("candidate digest does not match witness binding")

    @staticmethod
    def _record_identity(info: os.stat_result) -> tuple:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _read_marker(self) -> tuple[dict, bytes, tuple]:
        """Bounded regular-file read; never follows links or blocks on a FIFO."""
        fd = os.open(self.marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
                raise ReloadError("invalid recovery marker")
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
        identity = self._record_identity(before)
        try:
            current_identity = self._record_identity(self.marker.lstat())
        except OSError as exc:
            raise ReloadError("recovery marker changed while reading") from exc
        if len(raw) > 65536 or identity != self._record_identity(after) or identity != current_identity:
            raise ReloadError("recovery marker changed while reading")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate marker key")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError("nonfinite marker value")
        try:
            record = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
            if not isinstance(record, dict) or not {"sha256", "job"} <= set(record) <= {"schema_version", "sha256", "job", "witness_binding"}:
                raise ValueError("invalid record")
            version = record.get("schema_version", 0)
            if type(version) is not int or version not in (0, 1):
                raise ValueError("unsupported marker version")
            if not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
                raise ValueError("invalid digest")
            job = record["job"]
            if (not isinstance(job, dict) or set(job) != {"id", "description", "status", "blocked_by", "error", "config_committed", "apply_seconds"}
                    or not isinstance(job["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", job["id"])
                    or not isinstance(job["description"], dict)
                    or job["status"] not in ("queued", "applied", "failed", "timed_out", "reconciliation_required")
                    or type(job["config_committed"]) is not bool
                    or not isinstance(job["blocked_by"], list) or not all(isinstance(x, dict) for x in job["blocked_by"])
                    or (job["error"] is not None and not isinstance(job["error"], str))
                    or (job["apply_seconds"] is not None and (not _number(job["apply_seconds"]) or job["apply_seconds"] < 0))):
                raise ValueError("invalid job")
            if "witness_binding" in record:
                binding = CandidateBinding.from_dict(record["witness_binding"])
                if binding.candidate_sha256 != record["sha256"]:
                    raise ValueError("binding digest mismatch")
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ReloadError("invalid recovery marker") from exc
        return record, raw, identity

    def inspect_recovery(self, *, reading: GenerationRead | None = None,
                         before: BindingObservation | None = None,
                         after: BindingObservation | None = None, max_age: float = 5) -> dict:
        """Read persisted state and optional native evidence, with zero mutation.

        NativeGenerationReader.read() and instance observations are supplied by
        the caller; this inspection does not unexpectedly perform HTTP or probes.
        A persisted marker always fences this inspection, even when G is visible.
        """
        with self.action_lock:
            result = {"status": "reconciliation_required", "fenced": True, "marker_valid": False,
                      "candidate_file_matches": None, "candidate_generation_visible": False,
                      "settlement_confirmed": None, "blocked_by": []}
            try:
                record, raw, _ = self._read_marker()
            except FileNotFoundError:
                result.update(status="none", fenced=False, marker_valid=None)
                return result
            except (OSError, ReloadError):
                result["blocked_by"] = [{"reason": "invalid_or_unreadable_recovery_marker"}]
                return result
            result.update(marker_valid=True, marker_sha256=hashlib.sha256(raw).hexdigest(),
                          candidate_sha256=record["sha256"], job_id=record["job"]["id"],
                          description=copy.deepcopy(record["job"]["description"]),
                          recorded_status=record["job"]["status"])
            try:
                data, _ = self._read()
                result["candidate_file_matches"] = hashlib.sha256(data).hexdigest() == record["sha256"]
            except (OSError, ReloadError):
                result["blocked_by"].append({"reason": "candidate_file_unavailable"})
            if result["candidate_file_matches"] is False:
                result["blocked_by"].append({"reason": "candidate_file_digest_changed"})
            if "witness_binding" not in record:
                result["blocked_by"].append({"reason": "native_binding_not_persisted"})
            elif reading is None or before is None or after is None:
                result["blocked_by"].append({"reason": "native_evidence_unavailable"})
            else:
                try:
                    visible = check_visibility(CandidateBinding.from_dict(record["witness_binding"]),
                                               before, reading, after, now=self.clock(), max_age=max_age)
                    result["candidate_generation_visible"] = visible.candidate_generation_visible and result["candidate_file_matches"] is True
                    result["blocked_by"].extend({"reason": reason} for reason in visible.reasons)
                except (ValueError, TypeError, AttributeError):
                    result["blocked_by"].append({"reason": "invalid_native_evidence"})
            result["blocked_by"].append({"reason": "independent_old_server_settlement_unavailable"})
            return result

    def queue_snapshot(self) -> dict:
        """Detached, current diagnostics; do not advance, validate or execute jobs.

        `status` is a read-time view; recorded_status is the actual stored state.
        An elapsed queued job is shown timed_out without removing or notifying it.
        Prechecks retain their existing read-only callback contract.
        """
        with self.action_lock:
            now = self.clock()
            recovery = self.inspect_recovery()
            rows = []
            pending = {job.id for job in self._pending}
            for job in self._jobs.values():
                row = job.to_dict()
                row.update(recorded_status=job.status, source="memory",
                           elapsed_seconds=max(0, now - job.submitted_at),
                           remaining_seconds=max(0, self.timeout - (now - job.submitted_at)))
                if job.status == "queued":
                    if now - job.submitted_at >= self.timeout:
                        row.update(status="timed_out", error="no safe reload within timeout")
                    else:
                        try:
                            row["blocked_by"] = copy.deepcopy(self._blockers(job))
                        except Exception:
                            row["blocked_by"] = [{"reason": "inspection_unavailable"}]
                        if recovery["fenced"]:
                            row["blocked_by"].append({"reason": "reconciliation_required"})
                        if row["blocked_by"]:
                            row["status"] = "blocked"
                row["pending"] = job.id in pending and row["status"] != "timed_out"
                rows.append(row)
            if recovery.get("job_id") and recovery["job_id"] not in self._jobs:
                rows.append({"id": recovery["job_id"], "description": copy.deepcopy(recovery["description"]),
                             "status": "reconciliation_required", "recorded_status": recovery["recorded_status"],
                             "source": "recovery_marker", "pending": False, "config_committed": None,
                             "elapsed_seconds": None, "remaining_seconds": None,
                             "blocked_by": copy.deepcopy(recovery["blocked_by"]), "error": None,
                             "apply_seconds": None})
            return {"schema_version": 1, "observed_at_monotonic": now, "jobs": rows,
                    "pending_ids": [job.id for job in self._pending if now - job.submitted_at < self.timeout],
                    "fenced": recovery["fenced"], "recovery": recovery}

    def reconcile(self, confirm: Callable[[dict], RecoveryProof], *, dry_run: bool = False) -> dict:
        """Clear only with explicit full proof, bound to an unchanged valid marker.

        Native visibility/inspection results, booleans and partial truthy objects
        are not full proof. The callback is a trusted read-only verifier, not a
        retry of the reload. No native settlement source is introduced here.
        """
        with self.action_lock:
            if dry_run:
                self.log({"kind": "config_reconcile", "dry_run": True})
                return {"would": [{"kind": "reconcile_config"}]}
            try:
                record, raw, identity = self._read_marker()
                data, _ = self._read()
            except (OSError, ReloadError) as exc:
                raise ReloadError("invalid or unavailable recovery state") from exc
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ReloadError("adoption and cleanup not confirmed")
            proof = confirm(copy.deepcopy(record))
            if (not isinstance(proof, RecoveryProof) or proof.marker_sha256 != hashlib.sha256(raw).hexdigest()
                    or not all(value is True for value in (proof.generation_confirmed, proof.instance_confirmed,
                                                           proof.settlement_confirmed, proof.cleanup_confirmed))):
                raise ReloadError("adoption and cleanup not confirmed")
            if "witness_binding" in record and proof.instance != CandidateBinding.from_dict(record["witness_binding"]).instance:
                raise ReloadError("recovery instance not confirmed")
            try:
                _, current_raw, current_identity = self._read_marker()
                current_data, _ = self._read()
            except (OSError, ReloadError) as exc:
                raise ReloadError("recovery state changed during confirmation") from exc
            if (current_raw != raw or current_identity != identity
                    or hashlib.sha256(current_data).hexdigest() != record["sha256"]):
                raise ReloadError("recovery state changed during confirmation")
            self.marker.unlink()
            self._sync_directory()
            self.log({"kind": "config_reconcile", "dry_run": False})
            return {"status": "reconciled"}
