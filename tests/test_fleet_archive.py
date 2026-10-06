# Generated-By: Codex / gpt-6.1-sol
"""Synthetic sessions exercise the actual FleetStore and standalone archiver."""

import copy
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import zlib

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.fleet import archive
from llmsvc.fleet.store import FleetStore


BASE = 1_800_003_600.0
PRIVATE = "synthetic-private-value"


def service(instance="session-a", **updates):
    row = {"id": instance, "engine": "vllm", "engine_version": "0.25.0",
           "container": "owner-a", "host": False, "managed_by": None,
           "pid": 123 if instance == "session-a" else 456, "started_at": BASE - 86400,
           "model": "demo-model", "model_path": "/private/model/" + PRIVATE,
           "argv_redacted": "vllm serve " + PRIVATE, "bind": "192.0.2.123", "port": 8000,
           "api_address": "http://192.0.2.123:8000", "gpus": [{"index": 0, "used_mib": 1024}],
           "metrics_series_id": PRIVATE,
           "metrics": {"requests_total": 100, "generation_tokens_total": 1000,
                       "prompt_tokens_total": 2000, "prompt_tokens_cached_total": 500,
                       "counter_created_at": BASE - 100, "num_requests_running": 0,
                       "num_requests_waiting": 0},
           "ollama": None, "scrape": {"ok": True, "error": None}}
    row.update(updates)
    return row


def snapshot(ts=BASE, services=None, complete=True):
    return {"schema_version": 1, "generated_at": ts, "sample_interval_seconds": 60,
            "inventory_complete": complete, "gpu_inventory_complete": True,
            "gpu_attribution_complete": True, "host": {"gpu_count": 1},
            "services": [service()] if services is None else services,
            "gpus": [{"index": 0, "used_mib": 1024, "total_mib": 10000, "util_percent": 0}],
            "other_gpu_processes": [], "errors": []}


@pytest.fixture
def source(tmp_path):
    settings = SchedulerConfig("127.0.0.1", 8001, fleet_enabled=True,
        fleet_db_path=str(tmp_path / "fleet ?#%.sqlite"),
        fleet_raw_retention_days=1, fleet_hourly_retention_days=2)
    store = FleetStore(settings.fleet_db_path)
    config = {"database": settings.fleet_db_path, "directory": str(tmp_path / "archive"),
              "hourly_retention_days": settings.fleet_hourly_retention_days}
    try:
        yield store, settings, config
    finally:
        store.close()


def ingest(source, ts=BASE, rows=None, complete=True):
    store, settings, _ = source
    assert store.ingest(snapshot(ts, rows, complete), settings, ts)


def files(config):
    return sorted(Path(config["directory"]).glob("*.json.gz"))


def read(config, identity="session-a"):
    for path in files(config):
        try:
            result = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, EOFError, ValueError, RecursionError, zlib.error):
            continue
        if isinstance(result, dict) and result["identity"]["id"] == identity:
            return path, result
    raise AssertionError("Expected synthetic archive was not found")


def increased(**metrics):
    row = service()
    row["metrics"].update(metrics)
    return row


def test_repeat_replaces_partial_hour_and_preserves_observed_totals(source):
    ingest(source)
    config = source[2]
    assert archive.run_archive(config)["written"] == 1
    path, first = read(config)
    assert first["usage"] == {"basis": "observed_counter_deltas", "completeness": "unverified",
        "requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
    assert first["latest_reported_counters"]["output_tokens"]["value"] == 1000
    assert first["coverage"]["startup_unobserved_seconds"] == 86400
    assert first["latest_reported_counters"]["requests"]["epoch"]["series_sha256"] == hashlib.sha256(PRIVATE.encode()).hexdigest()
    initial = path.stat()
    assert archive.run_archive(config)["unchanged"] == 1
    assert path.stat().st_mtime_ns == initial.st_mtime_ns
    ingest(source, BASE + 60, [increased(requests_total=103, generation_tokens_total=1010,
        prompt_tokens_total=2020, prompt_tokens_cached_total=502)])
    assert archive.run_archive(config)["written"] == 1
    _, current = read(config)
    assert current["usage"]["requests"] == 3
    assert current["usage"]["total_tokens"] == 30
    assert len(current["hourly"]) == 1
    assert archive.run_archive(config)["unchanged"] == 1
    assert len(files(config)) == 1


def test_prior_hour_coverage_is_replaced_after_boundary(source):
    ingest(source, BASE + 3590)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 3610, [increased(requests_total=102, generation_tokens_total=1002)])
    archive.run_archive(config)
    _, result = read(config)
    assert result["hourly"][str(int(BASE))]["observed_seconds"] == 10
    assert result["hourly"][str(int(BASE + 3600))]["observed_seconds"] == 10
    assert result["usage"]["requests"] == 2


def test_all_retained_hours_merge_and_archived_pruned_hours_survive(source):
    store, settings, config = source
    ingest(source)
    ingest(source, BASE + 60, [increased(requests_total=103)])
    archive.run_archive(config)
    ingest(source, BASE + 86400 + 60, [increased(requests_total=110)])
    archive.run_archive(config)
    ingest(source, BASE + 4 * 86400, [increased(requests_total=120)])
    assert store.retention_tick(settings, BASE + 4 * 86400)
    assert len(store._db.execute("SELECT * FROM fleet_hourly").fetchall()) == 1
    archive.run_archive(config)
    _, result = read(config)
    assert len(result["hourly"]) == 3
    assert result["usage"]["requests"] == 20
    assert result["source"]["hourly_retention_days"] == 2
    assert result["source"]["hourly_cutoff"] == BASE + 2 * 86400
    assert result["coverage"]["missing_intervals"] == [{"start_at": BASE + 86400 + 60,
        "end_at": BASE + 2 * 86400, "reason": "archive_outage_exceeded_source_retention"}]
    assert archive.run_archive(config)["unchanged"] == 1


def test_initial_archive_records_actual_retention_loss_and_never_backfills(source):
    store, settings, config = source
    ingest(source)
    ingest(source, BASE + 60, [increased(requests_total=103)])
    ingest(source, BASE + 3 * 86400, [increased(requests_total=110)])
    store.retention_tick(settings, BASE + 3 * 86400)
    archive.run_archive(config)
    _, result = read(config)
    assert result["usage"]["requests"] == 7
    assert result["latest_reported_counters"]["requests"]["value"] == 110
    assert result["coverage"]["missing_intervals"] == [{"start_at": BASE,
        "end_at": BASE + 86400, "reason": "source_retention_before_initial_archive"}]


def test_absent_retention_tick_does_not_invent_retention_loss(source):
    ingest(source)
    ingest(source, BASE + 10 * 86400, [increased(requests_total=102)])
    archive.run_archive(source[2])
    _, result = read(source[2])
    assert result["source"]["last_retention"] is None
    assert result["coverage"]["missing_intervals"] == []


def test_lifecycle_incomplete_end_closed_no_rewrite_and_reopen(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 60, [], complete=False)
    archive.run_archive(config)
    _, incomplete = read(config)
    assert incomplete["state"] == "running" and incomplete["ended_at"] is None
    ingest(source, BASE + 120, [])
    archive.run_archive(config)
    path, ended = read(config)
    assert ended["state"] == "ended" and ended["ended_at"] == BASE + 120
    checkpoint = path.read_bytes(), path.stat().st_mtime_ns
    ingest(source, BASE + 180, [])
    source[0].retention_tick(source[1], BASE + 180)
    assert archive.run_archive(config)["unchanged"] == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == checkpoint
    ingest(source, BASE + 240, [increased(requests_total=105)])
    assert archive.run_archive(config)["written"] == 1
    _, reopened = read(config)
    assert reopened["state"] == "running" and reopened["ended_at"] is None
    assert reopened["first_seen"] == BASE and reopened["usage"]["requests"] == 5
    assert len(files(config)) == 1


def test_missing_counter_stays_null_and_total_requires_both_sides(source):
    row = service()
    row["metrics"].pop("prompt_tokens_total")
    row["metrics"].pop("prompt_tokens_cached_total")
    ingest(source, rows=[row])
    later = copy.deepcopy(row)
    later["metrics"].update(requests_total=102, generation_tokens_total=1005)
    ingest(source, BASE + 60, [later])
    archive.run_archive(source[2])
    _, result = read(source[2])
    assert result["usage"]["output_tokens"] == 5
    assert result["usage"]["input_tokens"] is None and result["usage"]["total_tokens"] is None
    assert result["usage"]["cached_tokens"] is None
    assert result["latest_reported_counters"]["input_tokens"] == {"value": None, "ts": None, "epoch": None}


def test_failed_scrape_and_gap_preserve_baseline_timestamps_without_certifying_coverage(source):
    ingest(source)
    failed = service(scrape={"ok": False, "error": "timeout"})
    ingest(source, BASE + 3600, [failed])
    archive.run_archive(source[2])
    _, result = read(source[2])
    assert result["hourly"][str(int(BASE + 3600))]["requests"] is None
    assert result["latest_reported_counters"]["requests"]["ts"] == BASE
    ingest(source, BASE + 7200, [increased(requests_total=107)])
    archive.run_archive(source[2])
    _, result = read(source[2])
    assert result["usage"]["requests"] == 7
    assert result["hourly"][str(int(BASE + 7200))]["observed_seconds"] == 0
    assert result["usage"]["completeness"] == "unverified"


@pytest.mark.parametrize("kind", ["negative", "epoch"])
def test_counter_resets_add_zero_observed_flow(source, kind):
    ingest(source)
    ingest(source, BASE + 60, [increased(requests_total=105)])
    archive.run_archive(source[2])
    row = increased(requests_total=1 if kind == "negative" else 999)
    if kind == "epoch":
        row["metrics"]["requests_created"] = BASE + 61
    ingest(source, BASE + 120, [row])
    archive.run_archive(source[2])
    _, result = read(source[2])
    assert result["usage"]["requests"] == 5
    assert result["latest_reported_counters"]["requests"]["value"] == row["metrics"]["requests_total"]
    assert result["latest_reported_counters"]["requests"]["ts"] == BASE + 120


def test_ollama_is_one_process_session_with_null_tokens_and_bounded_model_history(source):
    for index in range(archive.MAX_MODEL_HISTORY + 3):
        row = service(engine="ollama", model="demo-" + str(index), metrics=None,
            ollama={"models": [{"name": "demo-" + str(index), "expires_at": BASE + index * 60 + 600}]})
        ingest(source, BASE + index * 60, [row])
        archive.run_archive(source[2])
    _, result = read(source[2])
    assert len(files(source[2])) == 1
    assert all(result["usage"][key] is None for key in (*archive.COUNTERS, "total_tokens"))
    assert all(counter["value"] is None for counter in result["latest_reported_counters"].values())
    assert len(result["model_history"]) == archive.MAX_MODEL_HISTORY
    assert result["model_history_truncated"] is True


@pytest.mark.parametrize("mutation,code", [
    ("generation", "source_generation_regression"),
    ("decrease", "source_hour_regression"),
    ("null", "source_hour_regression"),
    ("first_seen", "archive_identity_mismatch")])
def test_source_regressions_preserve_existing_file(source, mutation, code):
    store, _, config = source
    ingest(source)
    ingest(source, BASE + 60, [increased(requests_total=105)])
    archive.run_archive(config)
    path, _ = read(config)
    before = path.read_bytes()
    with store._db as db:
        if mutation == "generation":
            store._meta(db, "generated_at", BASE)
            db.execute("UPDATE fleet_instances SET last_seen=?", (BASE,))
        elif mutation == "first_seen":
            db.execute("UPDATE fleet_instances SET first_seen=?", (BASE + 1,))
        else:
            db.execute("UPDATE fleet_hourly SET requests=?", (4 if mutation == "decrease" else None,))
    result = archive.run_archive(config)
    assert result["ok"] is False and result["errors"] == {code: 1}
    assert path.read_bytes() == before


def test_recreated_source_with_same_process_and_new_first_seen_is_ambiguous(source):
    store, settings, config = source
    ingest(source)
    archive.run_archive(config)
    path, _ = read(config)
    before = path.read_bytes()
    store.close()
    Path(settings.fleet_db_path).unlink()
    recreated = FleetStore(settings.fleet_db_path)
    try:
        recreated.ingest(snapshot(BASE + 120), settings, BASE + 120)
        result = archive.run_archive(config)
        assert result["errors"] == {"archive_identity_mismatch": 1}
        assert path.read_bytes() == before
    finally:
        recreated.close()


@pytest.mark.parametrize("corruption", ["header", "deflate", "deep_json", "totals", "extra_key", "number_overflow"])
def test_corrupt_archive_is_preserved_and_healthy_sessions_continue(source, corruption):
    ingest(source, rows=[service(), service("session-b")])
    config = source[2]
    archive.run_archive(config)
    path, result = read(config)
    if corruption == "header":
        raw = b"not-gzip"
    elif corruption == "deflate":
        raw = bytearray(path.read_bytes())
        raw[10] = 0xff
        with pytest.raises(zlib.error):
            gzip.decompress(raw)
    elif corruption == "deep_json":
        decoded = b'[' * 2000 + b'0' + b']' * 2000
        with pytest.raises(RecursionError):
            json.loads(decoded)
        raw = gzip.compress(decoded)
    else:
        if corruption == "totals":
            result["usage"]["requests"] = 999
        elif corruption == "number_overflow":
            next(iter(result["hourly"].values()))["requests"] = 10 ** 1000
        else:
            result["unknown"] = PRIVATE
        raw = gzip.compress(json.dumps(result).encode())
    path.write_bytes(raw)
    preserved = path.read_bytes()
    second = service("session-b")
    second["metrics"]["requests_total"] = 102
    ingest(source, BASE + 60, [increased(requests_total=102), second])
    summary = archive.run_archive(config)
    assert summary["errors"] == {"archive_corrupt": 1} and summary["written"] == 1
    assert path.read_bytes() == preserved
    assert read(config, "session-b")[1]["usage"]["requests"] == 2


def test_archive_identity_must_match_full_filename_identity(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    path, _ = read(config)
    other = service("session-b", host=True, container=None, pid=123)
    ingest(source, BASE + 60, [service(), other])
    identity = {key: other[key] for key in ("id", "container", "pid", "started_at", "engine")}
    target = Path(config["directory"]) / (hashlib.sha256(archive._dumps(identity).encode()).hexdigest() + ".json.gz")
    shutil.copyfile(path, target)
    before = target.read_bytes()
    assert archive.run_archive(config)["errors"] == {"archive_identity_mismatch": 1}
    assert target.read_bytes() == before


def test_dry_run_has_no_archive_lock_config_or_state_artifacts(source, tmp_path):
    ingest(source)
    config = source[2]
    before = set(tmp_path.rglob("*"))
    assert archive.run_archive(config, dry_run=True)["would_write"] == 1
    assert set(tmp_path.rglob("*")) == before
    assert not Path(config["directory"]).exists()
    archive.run_archive(config)
    path, _ = read(config)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(config["directory"]).iterdir()}
    ingest(source, BASE + 60, [increased(requests_total=102)])
    assert archive.run_archive(config, dry_run=True)["would_write"] == 1
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(config["directory"]).iterdir()} == before
    assert read(config)[0] == path


def test_closed_wal_dry_run_fails_before_creating_companions(source, tmp_path):
    ingest(source)
    source[0].close()
    before = set(tmp_path.rglob("*"))
    with pytest.raises(archive.ArchiveError, match="source_wal_companions_required"):
        archive.run_archive(source[2], dry_run=True)
    assert set(tmp_path.rglob("*")) == before


def test_closed_delete_mode_source_supports_zero_artifact_preview(source, tmp_path):
    ingest(source)
    source[0].close()
    with sqlite3.connect(source[2]["database"]) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    before = set(tmp_path.rglob("*"))
    assert archive.run_archive(source[2], dry_run=True)["would_write"] == 1
    assert set(tmp_path.rglob("*")) == before


def test_read_only_consistent_snapshot_closes_before_file_io(source, monkeypatch):
    store, settings, config = source
    ingest(source)
    original = sqlite3.connect
    closed = []
    changed = []

    class Reader:
        def __init__(self, db):
            self.db = db

        @property
        def row_factory(self):
            return self.db.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.db.row_factory = value

        def execute(self, sql):
            if "FROM fleet_instances" in sql and not changed:
                changed.append(True)
                store.ingest(snapshot(BASE + 60, [increased(requests_total=102)]), settings, BASE + 60)
            result = self.db.execute(sql)
            if sql == "PRAGMA query_only=ON":
                assert self.db.execute("PRAGMA query_only").fetchone()[0] == 1
                with pytest.raises(sqlite3.OperationalError):
                    self.db.execute("INSERT INTO fleet_meta VALUES('forbidden','1')")
                self.db.rollback()
            return result

        def set_progress_handler(self, *args):
            self.db.set_progress_handler(*args)

        def close(self):
            closed.append(True)
            self.db.close()

    def connect(target, **options):
        assert target.endswith("?mode=ro") and "immutable" not in target and "nolock" not in target
        return Reader(original(target, **options))

    write = archive._atomic_write

    def atomic(*args):
        assert closed
        return write(*args)

    monkeypatch.setattr(archive.sqlite3, "connect", connect)
    monkeypatch.setattr(archive, "_atomic_write", atomic)
    assert archive.run_archive(config)["written"] == 1
    _, result = read(config)
    assert result["source"]["generated_at"] == BASE and result["usage"]["requests"] == 0
    assert store.metadata()["generated_at"] == BASE + 60


@pytest.mark.parametrize("after_replace", [False, True])
def test_atomic_write_failure_retry_is_idempotent(source, monkeypatch, after_replace):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    path, _ = read(config)
    before = path.read_bytes()
    ingest(source, BASE + 60, [increased(requests_total=102)])
    replace = archive.os.replace

    def fail(*args):
        if after_replace:
            replace(*args)
        raise OSError("synthetic interrupted rename")

    with monkeypatch.context() as patch:
        patch.setattr(archive.os, "replace", fail)
        assert archive.run_archive(config)["failed"] == 1
    assert not list(Path(config["directory"]).glob("*.tmp"))
    if not after_replace:
        assert path.read_bytes() == before
    summary = archive.run_archive(config)
    assert summary["unchanged" if after_replace else "written"] == 1
    assert read(config)[1]["usage"]["requests"] == 2
    assert archive.run_archive(config)["unchanged"] == 1


def test_process_crash_before_replace_leaves_checkpoint_and_retry_counts_once(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    path, _ = read(config)
    before = path.read_bytes()
    ingest(source, BASE + 60, [increased(requests_total=102)])
    script = "import json,os,runpy,sys; ns=runpy.run_path(sys.argv[1]); ns['os'].replace=lambda *args:os._exit(73); ns['run_archive'](json.loads(sys.argv[2]))"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, archive.__file__, json.dumps(config)],
        capture_output=True, timeout=10)
    assert result.returncode == 73 and path.read_bytes() == before
    assert archive.run_archive(config)["written"] == 1
    assert read(config)[1]["usage"]["requests"] == 2
    assert len(files(config)) == 1


def test_failed_directory_durability_barrier_is_retried_for_unchanged_file(source, monkeypatch):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 60, [increased(requests_total=102)])
    fsync = archive.os.fsync
    barriers = []

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            barriers.append("directory")
            if barriers.count("directory") == 1:
                raise OSError("synthetic directory barrier failure")
        else:
            barriers.append("file")
            # The gzip trailer is closed and flushed before the file barrier.
            temporary = Path(os.readlink('/proc/self/fd/' + str(fd)))
            assert json.loads(gzip.decompress(temporary.read_bytes()))["usage"]["requests"] == 2
        return fsync(fd)

    monkeypatch.setattr(archive.os, "fsync", sync)
    with pytest.raises(archive.ArchiveError, match="archive_directory_sync_failed"):
        archive.run_archive(config)
    assert read(config)[1]["usage"]["requests"] == 2
    assert archive.run_archive(config)["unchanged"] == 1
    assert barriers == ["file", "directory", "directory"]


def test_single_writer_lock_precedes_snapshot(source, monkeypatch):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    lock = Path(config["directory"]) / ".archive.lock"
    with lock.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(archive, "_read_source", lambda *args: pytest.fail("snapshot read before lock"))
        with pytest.raises(archive.ArchiveError, match="archive_locked"):
            archive.run_archive(config)


def test_private_metadata_permissions_and_deterministic_gzip(source, tmp_path):
    ingest(source, rows=[service(model="/private/model/" + PRIVATE)])
    config = source[2]
    archive.run_archive(config)
    path, result = read(config)
    decoded = gzip.decompress(path.read_bytes())
    for forbidden in (PRIVATE, "192.0.2.123", "model_path", "argv", "api_address", "prompt", "response"):
        assert forbidden.encode() not in decoded
    assert result["model"] is None and result["gpus"] == [{"index": 0, "used_mib": 1024}]
    assert path.name == hashlib.sha256(archive._dumps(result["identity"]).encode()).hexdigest() + ".json.gz"
    assert Path(config["directory"]).stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert (Path(config["directory"]) / ".archive.lock").stat().st_mode & 0o777 == 0o600
    assert path.read_bytes()[4:8] == b"\0" * 4
    other = dict(config, directory=str(tmp_path / "other"))
    archive.run_archive(other)
    assert files(other)[0].read_bytes() == path.read_bytes()


@pytest.mark.parametrize("limit", ["MAX_SOURCE_ROWS", "MAX_SOURCE_BYTES", "MAX_ARCHIVE_BYTES"])
def test_limits_fail_without_truncation_or_replacement(source, monkeypatch, limit):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    path, _ = read(config)
    before = path.read_bytes()
    monkeypatch.setattr(archive, limit, 1)
    if limit == "MAX_ARCHIVE_BYTES":
        assert archive.run_archive(config)["errors"] == {"archive_read_limit": 1}
    else:
        with pytest.raises(archive.ArchiveError, match="source_read_limit"):
            archive.run_archive(config)
    assert path.read_bytes() == before


def test_decoded_archive_limit_is_enforced_for_small_compressed_input(source, monkeypatch):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    path, _ = read(config)
    compressed = path.read_bytes()
    decoded = gzip.decompress(compressed)
    limit = (len(compressed) + len(decoded)) // 2
    assert len(compressed) < limit < len(decoded)
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", limit)
    assert archive.run_archive(config)["errors"] == {"archive_read_limit": 1}
    assert path.read_bytes() == compressed


@pytest.mark.parametrize("change", [{"database": "relative.sqlite"}, {"directory": "relative"},
    {"hourly_retention_days": 0}, {"hourly_retention_days": 3651}, {"hourly_retention_days": True},
    {"hourly_retention_days": 180.0}, {"typo": "synthetic"}])
def test_invalid_config_creates_nothing(source, tmp_path, change):
    config = dict(source[2], **change)
    with pytest.raises(archive.ArchiveError, match="invalid_config"):
        archive.run_archive(config)
    assert list(tmp_path.iterdir()) == []


def test_missing_database_never_creates_database_or_archive(source, tmp_path):
    with pytest.raises(archive.ArchiveError, match="source_unavailable"):
        archive.run_archive(source[2])
    assert list(tmp_path.iterdir()) == []


def test_copied_standalone_cli_uses_only_stdlib_and_safe_json_errors(source, tmp_path):
    ingest(source)
    script = tmp_path / "standalone.py"
    shutil.copyfile(archive.__file__, script)
    config = dict(source[2], _generated_by="Codex / gpt-6.1-sol", _comments=["synthetic configuration"])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    command = [sys.executable, "-I", "-S", "-B", str(script), "--config", str(config_path), "--dry-run"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)["would_write"] == 1 and result.stderr == ""
    assert not Path(config["directory"]).exists() and not list(tmp_path.glob("__pycache__"))
    config_path.write_text(json.dumps(dict(config, database=str(tmp_path / PRIVATE))))
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stderr)["errors"] == {"source_unavailable": 1}
    assert PRIVATE not in result.stdout + result.stderr
