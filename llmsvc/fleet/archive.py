#!/usr/bin/env python3
# Generated-By: Codex / gpt-6.1-sol
"""Archive observed fleet process sessions without changing the source database."""

import argparse
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import zlib


# More than 180 days of hourly rows for 256 continuously observed services.
MAX_SOURCE_ROWS = 2_000_000
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_SECONDS = 30
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_MODEL_HISTORY = 32
COUNTERS = {"requests": "requests", "input_tokens": "prompt_tokens",
            "output_tokens": "gen_tokens", "cached_tokens": "cached_tokens"}
HOUR_FIELDS = tuple(COUNTERS) + ("active_minutes", "observed_seconds", "samples")
ARCHIVE_KEYS = {"schema_version", "identity", "model", "engine_version", "host", "gpus",
                "started_at", "first_seen", "last_seen", "ended_at", "state", "usage",
                "latest_reported_counters", "hourly", "coverage", "source", "model_history",
                "model_history_truncated", "loaded_models", "loaded_models_at",
                "loaded_models_history", "loaded_models_history_truncated"}


class ArchiveError(ValueError):
    """An error code safe for public summaries, with no source identifiers."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(value):
    def reject_constant(_):
        raise ArchiveError("invalid_json")

    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ArchiveError("invalid_json")
            result[key] = item
        return result

    try:
        return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ArchiveError("invalid_json") from error


def _number(value, nullable=False):
    if value is None and nullable:
        return value
    try:
        valid = type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ArchiveError("invalid_number")
    return value


def _config(config):
    keys = {"database", "directory", "hourly_retention_days"}
    if not isinstance(config, dict) or set(config) - keys - {"_generated_by", "_comments"} or not keys <= set(config):
        raise ArchiveError("invalid_config")
    days = config["hourly_retention_days"]
    if type(days) is not int or not 1 <= days <= 3650:
        raise ArchiveError("invalid_config")
    paths = []
    for key in ("database", "directory"):
        value = config[key]
        if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
            raise ArchiveError("invalid_config")
        paths.append(Path(value))
    database, directory = paths
    if directory.resolve() in (database.resolve(), *database.resolve().parents):
        raise ArchiveError("overlapping_paths")
    return database, directory, days


def _source_preflight(database, dry_run):
    header = None
    try:
        fd = os.open(database, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ArchiveError("source_unavailable")
            header = os.read(fd, 100)
        finally:
            os.close(fd)
        if len(header) != 100 or header[:16] != b"SQLite format 3\0":
            raise ArchiveError("invalid_source_database")
        if dry_run and header[18:20] == b"\x02\x02":
            for suffix in ("-wal", "-shm"):
                fd = os.open(str(database) + suffix, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ArchiveError("source_wal_companions_required")
                finally:
                    os.close(fd)
    except OSError as error:
        code = "source_wal_companions_required" if dry_run and header is not None and header[18:20] == b"\x02\x02" else "source_unavailable"
        raise ArchiveError(code) from error


def _read_source(database, days, dry_run):
    _source_preflight(database, dry_run)
    deadline = time.monotonic() + MAX_SOURCE_SECONDS
    rows_read = 0
    bytes_read = 0
    try:
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ArchiveError("unsupported_source_schema")

            def read(sql):
                nonlocal rows_read, bytes_read
                for row in db.execute(sql):
                    rows_read += 1
                    bytes_read += sum(len(value.encode()) if isinstance(value, str) else 16 for value in row)
                    if rows_read > MAX_SOURCE_ROWS or bytes_read > MAX_SOURCE_BYTES:
                        raise ArchiveError("source_read_limit")
                    yield dict(row)

            meta = {row["key"]: _number(_loads(row["value"])) for row in read(
                "SELECT key,value FROM fleet_meta WHERE key IN ('generated_at','last_retention','raw_cutoff')")}
            if "generated_at" not in meta:
                raise ArchiveError("source_snapshot_unavailable")
            meta.update(hourly_retention_days=days, last_retention=meta.get("last_retention"), raw_cutoff=meta.get("raw_cutoff"))
            meta["hourly_cutoff"] = None if meta["last_retention"] is None else math.floor((meta["last_retention"] - days * 86400) / 3600) * 3600
            instances = list(read("SELECT id,container,pid,started_at,engine,model,engine_version,host,gpus_json,"
                                  "first_seen,last_seen,ended_at,state_json FROM fleet_instances"))
            hourly = {}
            for row in read("SELECT * FROM fleet_hourly"):
                hourly.setdefault(row.pop("instance_id"), []).append(row)
            if set(hourly) - {row["id"] for row in instances}:
                raise ArchiveError("source_history_without_instance")
            return meta, instances, hourly
        finally:
            db.close()
    except (sqlite3.Error, OSError, UnicodeError) as error:
        raise ArchiveError("source_read_failed") from error


@contextmanager
def _writer_lock(directory):
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ArchiveError("unsafe_archive_directory")
        directory.chmod(0o700)
        fd = os.open(directory / ".archive.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ArchiveError("unsafe_archive_lock")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ArchiveError("archive_locked") from error
            yield
        finally:
            os.close(fd)
    except OSError as error:
        raise ArchiveError("archive_directory_unavailable") from error


def _safe_label(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512 or any(ord(char) < 32 for char in value):
        raise ArchiveError("invalid_source_metadata")
    if value.startswith(("/", "~", "\\")) or "://" in value or ".." in value.split("/") or "\\" in value:
        return None
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return value
    return None


def _identity(row):
    identity = {key: row[key] for key in ("id", "container", "pid", "started_at", "engine")}
    for key in ("id", "engine"):
        if not isinstance(identity[key], str) or not identity[key] or len(identity[key]) > 512:
            raise ArchiveError("invalid_source_identity")
    if identity["container"] is not None and (not isinstance(identity["container"], str) or not identity["container"] or len(identity["container"]) > 512):
        raise ArchiveError("invalid_source_identity")
    if type(identity["pid"]) is not int or identity["pid"] <= 0:
        raise ArchiveError("invalid_source_identity")
    _number(identity["started_at"])
    return identity


def _hour(row):
    timestamp = _number(row["hour_ts"])
    if timestamp % 3600:
        raise ArchiveError("invalid_source_hour")
    result = {key: _number(row[column], nullable=True) for key, column in COUNTERS.items()}
    result.update({key: _number(row[key]) for key in ("active_minutes", "observed_seconds", "samples")})
    return str(int(timestamp)), result


def _usage(hourly):
    totals = {}
    for key in COUNTERS:
        values = [_number(row[key], nullable=True) for row in hourly.values()]
        known = [value for value in values if value is not None]
        totals[key] = _number(sum(known)) if known else None
    totals["total_tokens"] = None if totals["input_tokens"] is None or totals["output_tokens"] is None else _number(totals["input_tokens"] + totals["output_tokens"])
    return dict(basis="observed_counter_deltas", completeness="unverified", **totals)


def _load_archive(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
            raise ArchiveError("archive_read_limit")
        with os.fdopen(fd, "rb", closefd=False) as stream, gzip.GzipFile(fileobj=stream) as compressed:
            raw = compressed.read(MAX_ARCHIVE_BYTES + 1)
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise ArchiveError("archive_read_limit")
        archive = _loads(raw)
        if not isinstance(archive, dict) or set(archive) != ARCHIVE_KEYS or archive.get("schema_version") != 1 or not isinstance(archive.get("hourly"), dict):
            raise ArchiveError("archive_corrupt")
        for key, row in archive["hourly"].items():
            if not key.isdecimal() or int(key) % 3600 or not isinstance(row, dict) or set(row) != set(HOUR_FIELDS):
                raise ArchiveError("archive_corrupt")
            for field in HOUR_FIELDS:
                _number(row[field], nullable=field in COUNTERS)
        if archive.get("usage") != _usage(archive["hourly"]):
            raise ArchiveError("archive_corrupt")
        return archive
    except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error) as error:
        if isinstance(error, ArchiveError) and error.code == "archive_read_limit":
            raise
        raise ArchiveError("archive_corrupt") from error
    finally:
        os.close(fd)


def _merge(row, hours, source, old):
    identity = _identity(row)
    first_seen, last_seen = _number(row["first_seen"]), _number(row["last_seen"])
    ended_at = _number(row["ended_at"], nullable=True)
    if not identity["started_at"] <= first_seen <= last_seen <= source["generated_at"] or (ended_at is not None and not last_seen <= ended_at <= source["generated_at"]):
        raise ArchiveError("invalid_source_lifecycle")
    if old is not None:
        if old.get("identity") != identity or old.get("first_seen") != first_seen:
            raise ArchiveError("archive_identity_mismatch")
        if source["generated_at"] < _number(old["source"]["generated_at"]):
            raise ArchiveError("source_generation_regression")
        previous_end = _number(old["ended_at"], nullable=True)
        if last_seen < _number(old["last_seen"]) or (previous_end is not None and (
                (ended_at is not None and ended_at < previous_end)
                or (ended_at is None and last_seen < previous_end))):
            raise ArchiveError("source_lifecycle_regression")
    hourly = {} if old is None else dict(old["hourly"])
    for hour in hours:
        key, current = _hour(hour)
        previous = hourly.get(key)
        if previous is not None:
            for field in HOUR_FIELDS:
                before, after = previous[field], current[field]
                if before is not None and (after is None or after < before):
                    raise ArchiveError("source_hour_regression")
        hourly[key] = current
    state = _loads(row["state_json"])
    counters = {}
    for key, original in COUNTERS.items():
        baseline = (state.get("counters") or {}).get(original) if identity["engine"] == "vllm" else None
        if baseline is None:
            counters[key] = {"value": None, "ts": None, "epoch": None}
        else:
            epoch = baseline.get("epoch")
            if not isinstance(epoch, list) or len(epoch) != 2 or (epoch[1] is not None and not isinstance(epoch[1], str)):
                raise ArchiveError("invalid_source_counter_epoch")
            counters[key] = {"value": _number(baseline["value"]), "ts": _number(baseline["ts"]),
                             "epoch": {"created_at": _number(epoch[0], nullable=True),
                                       "series_sha256": None if epoch[1] is None else hashlib.sha256(epoch[1].encode()).hexdigest()}}
    gpus = [{"index": gpu["index"], "used_mib": _number(gpu.get("used_mib"), nullable=True)} for gpu in _loads(row["gpus_json"])]
    model = _safe_label(row["model"])
    history = [] if old is None else list(old.get("model_history", []))
    if not history or history[-1]["model"] != model:
        history.append({"ts": last_seen, "model": model})
    loaded_models = None if old is None else old["loaded_models"]
    loaded_models_at = None if old is None else old["loaded_models_at"]
    loaded_history = [] if old is None else list(old["loaded_models_history"])
    if (identity["engine"] == "ollama" and state.get("observed") is True
            and state.get("supported") is True and (state.get("latest") or {}).get("scrape_ok") == 1):
        expiries = state.get("expiries")
        if not isinstance(expiries, dict):
            raise ArchiveError("invalid_source_loaded_models")
        names = [_safe_label(name) for name in expiries]
        loaded_models = sorted(name for name in names if name is not None)
        loaded_models_at = _number(state["ts"])
        if not loaded_history or loaded_history[-1]["models"] != loaded_models:
            loaded_history.append({"ts": loaded_models_at, "models": loaded_models})
    missing = [] if old is None else list(old["coverage"]["missing_intervals"])
    cutoff = source["hourly_cutoff"]
    start = first_seen if old is None else old["source"]["generated_at"]
    end = min(cutoff, ended_at if ended_at is not None else source["generated_at"]) if cutoff is not None else start
    if end > start:
        missing.append({"start_at": start, "end_at": end,
                        "reason": "source_retention_before_initial_archive" if old is None else "archive_outage_exceeded_source_retention"})
    archive = {"schema_version": 1, "identity": identity, "model": model,
               "engine_version": _safe_label(row["engine_version"]), "host": bool(row["host"]), "gpus": gpus,
               "started_at": identity["started_at"], "first_seen": first_seen, "last_seen": last_seen,
               "ended_at": ended_at, "state": "ended" if ended_at is not None else "running",
               "usage": _usage(hourly), "latest_reported_counters": counters, "hourly": hourly,
               "coverage": {"startup_unobserved_seconds": first_seen - identity["started_at"], "missing_intervals": missing},
               "source": source, "model_history": history[-MAX_MODEL_HISTORY:],
               "model_history_truncated": len(history) > MAX_MODEL_HISTORY or bool(old and old.get("model_history_truncated")),
               "loaded_models": loaded_models, "loaded_models_at": loaded_models_at,
               "loaded_models_history": loaded_history[-MAX_MODEL_HISTORY:],
               "loaded_models_history_truncated": len(loaded_history) > MAX_MODEL_HISTORY or bool(old and old["loaded_models_history_truncated"])}
    if old is not None and ended_at is not None and dict(archive, source=old["source"]) == old:
        return old
    if len(_dumps(archive).encode()) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("archive_read_limit")
    return archive


def _atomic_write(directory, path, archive):
    fd, temporary = tempfile.mkstemp(prefix=".fleet-archive-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
                compressed.write(_dumps(archive).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_archive(config, dry_run=False):
    """Merge a consistent source snapshot into atomic per-process checkpoints."""
    database, directory, days = _config(config)
    _source_preflight(database, dry_run)
    summary = {"ok": True, "dry_run": bool(dry_run), "instances": 0, "written": 0,
               "would_write": 0, "unchanged": 0, "failed": 0, "errors": {}}

    def run():
        source, instances, hourly = _read_source(database, days, dry_run)
        summary["instances"] = len(instances)
        for row in instances:
            try:
                digest = hashlib.sha256(_dumps(_identity(row)).encode()).hexdigest()
                path = directory / (digest + ".json.gz")
                old = _load_archive(path)
                archive = _merge(row, hourly.get(row["id"], []), source, old)
                if archive == old:
                    summary["unchanged"] += 1
                elif dry_run:
                    summary["would_write"] += 1
                else:
                    _atomic_write(directory, path, archive)
                    summary["written"] += 1
            except (ArchiveError, OSError, KeyError, TypeError, AttributeError) as error:
                code = error.code if isinstance(error, ArchiveError) else "archive_update_failed"
                summary["failed"] += 1
                summary["errors"][code] = summary["errors"].get(code, 0) + 1
        summary["ok"] = summary["failed"] == 0
        return summary

    if dry_run:
        return run()
    with _writer_lock(directory):
        result = run()
        try:
            # This also completes a prior rename whose directory barrier failed.
            _sync_directory(directory)
        except OSError as error:
            raise ArchiveError("archive_directory_sync_failed") from error
        return result


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArchiveError("invalid_arguments")


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--config", required=True)
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(argv)
        with Path(args.config).open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ArchiveError("config_read_limit")
        summary = run_archive(_loads(raw), dry_run=args.dry_run)
    except (ArchiveError, OSError) as error:
        code = error.code if isinstance(error, ArchiveError) else "config_unavailable"
        summary = {"ok": False, "errors": {code: 1}}
    print(_dumps(summary))
    if not summary["ok"]:
        print(_dumps({"ok": False, "errors": summary["errors"]}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
