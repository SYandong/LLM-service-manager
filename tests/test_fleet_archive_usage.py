# Generated-By: Codex / gpt-6.1-sol
"""Sampled usage comes only from retained FleetStore observations."""

import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from llmsvc.fleet import archive
from test_fleet_archive import BASE, PRIVATE, increased, ingest, read, service, source as source_fixture

source = source_fixture


def records(config, instance="session-a", kind=None):
    result = []
    digest = read(config, instance)[0].name.removesuffix(".json.gz")
    for path in sorted(Path(config["directory"]).glob("usage/*/" + digest + ".jsonl.gz")):
        for line in gzip.decompress(path.read_bytes()).splitlines():
            event = json.loads(line)
            if event["instance_id"] == instance and (kind is None or event["type"] == kind):
                result.append(event)
    return result


def paths(config, instance="session-a"):
    summary, value = read(config, instance)
    digest = summary.name.removesuffix(".json.gz")
    usage = sorted(Path(config["directory"]).glob("usage/*/" + digest + ".jsonl.gz"))
    state = Path(config["directory"]) / "usage-state" / (digest + ".json.gz")
    return usage, state, value


def test_each_fresh_minute_has_iso_time_separate_tokens_and_no_duplicate(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    first = records(config, kind="usage")[0]
    assert first["baseline"] is True and first["interval_seconds"] == 0
    assert all(value is None for value in first["usage"].values())
    ingest(source, BASE + 60, [increased(requests_total=103, generation_tokens_total=1010,
        prompt_tokens_total=2020, prompt_tokens_cached_total=502)])
    result = archive.run_archive(config)
    usage = records(config, kind="usage")
    assert len(usage) == 2 and result["usage_records_added"] == 1
    assert usage[-1]["usage"] == {"requests": 3, "input_tokens": 20, "output_tokens": 10,
                                 "total_tokens": 30, "cached_tokens": 2}
    assert usage[-1]["ts"] == BASE + 60
    assert usage[-1]["timestamp"] == archive._iso(BASE + 60)
    assert usage[-1]["timestamp"].endswith("Z")
    assert usage[-1]["interval_seconds"] == usage[-1]["observed_seconds"] == 60
    assert usage[-1]["scrape_ok"] is True and usage[-1]["gap"] is False
    assert usage[-1]["model"] == "demo-model"
    assert usage[-1]["model_basis"] == "session_metadata_at_export"
    usage_paths, state, _ = paths(config)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in [*usage_paths, state]]
    result = archive.run_archive(config)
    assert result["usage_records_added"] == result["usage_files_written"] == 0
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in [*usage_paths, state]]
    assert len({event["event_id"] for event in records(config)}) == 3


def test_missing_scrapes_and_gaps_keep_nulls_and_source_endpoint_deltas(source):
    row = service()
    row["metrics"].pop("prompt_tokens_total")
    row["metrics"].pop("prompt_tokens_cached_total")
    ingest(source, rows=[row])
    ingest(source, BASE + 60, [service(scrape={"ok": False, "error": "timeout"})])
    later = service()
    later["metrics"] = dict(row["metrics"], requests_total=107, generation_tokens_total=1009)
    ingest(source, BASE + 600, [later])
    archive.run_archive(source[2])
    usage = records(source[2], kind="usage")
    assert len(usage) == 3
    assert usage[1]["scrape_ok"] is False and usage[1]["interval_seconds"] == 60
    assert all(value is None for value in usage[1]["usage"].values())
    assert usage[2]["gap"] is True and usage[2]["interval_seconds"] == 540
    assert usage[2]["observed_seconds"] == 0
    assert usage[2]["usage"] == {"requests": 7, "output_tokens": 9,
                                "input_tokens": None, "total_tokens": None, "cached_tokens": None}


@pytest.mark.parametrize("kind", ["negative", "epoch"])
def test_reset_is_explicit_and_independent_known_deltas_survive(source, kind):
    ingest(source)
    row = increased(requests_total=1 if kind == "negative" else 900,
                    generation_tokens_total=1011, prompt_tokens_total=2022)
    if kind == "epoch":
        row["metrics"]["requests_created"] = BASE + 30
    ingest(source, BASE + 60, [row])
    archive.run_archive(source[2])
    event = records(source[2], kind="usage")[-1]
    assert event["counter_reset"] is True and event["baseline"] is False
    assert event["usage"]["requests"] == 0
    assert event["usage"]["input_tokens"] == 22
    assert event["usage"]["output_tokens"] == 11
    assert event["observed_seconds"] == 0


def test_incomplete_inventory_end_reappearance_and_restart_have_honest_lifecycle(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 60, [], complete=False)
    archive.run_archive(config)
    missing = records(config, kind="usage")[-1]
    assert missing["scrape_ok"] is False and all(value is None for value in missing["usage"].values())
    assert records(config, kind="session_ended") == []
    ingest(source, BASE + 120, [])
    archive.run_archive(config)
    end = records(config, kind="session_ended")
    assert len(end) == 1 and end[0]["ts"] == BASE + 120
    assert end[0]["final_counters_sampled"] is False and "usage" not in end[0]
    assert end[0]["last_seen"] == archive._iso(BASE)
    ingest(source, BASE + 180, [])
    archive.run_archive(config)
    assert records(config, kind="session_ended") == end
    ingest(source, BASE + 240, [increased(requests_total=105)])
    archive.run_archive(config)
    resumed = records(config, kind="session_reobserved")
    assert len(resumed) == 1 and resumed[0]["ts"] == BASE + 240
    assert resumed[0]["previous_ended_at"] == archive._iso(BASE + 120)
    assert resumed[0]["observation_basis"] == "first_retained_sample_after_absence"
    assert records(config, kind="usage")[-1]["usage"]["requests"] == 5
    replacement = service("session-b", pid=123, started_at=BASE + 299)
    ingest(source, BASE + 300, [replacement])
    archive.run_archive(config)
    assert records(config, "session-b", "usage")[0]["baseline"] is True
    assert records(config, "session-b")[0]["session_id"] != records(config)[0]["session_id"]
    assert len(records(config, kind="session_started")) == 1


def test_daily_rotation_and_existing_summaries_survive_raw_pruning(source):
    store, settings, config = source
    ingest(source)
    ingest(source, BASE + 60, [increased(requests_total=103, generation_tokens_total=1010)])
    archive.run_archive(config)
    old_path, old_summary = read(config)
    # Simulate upgrade from the legacy summary-only writer, retaining its bytes.
    shutil.rmtree(Path(config["directory"]) / "usage")
    shutil.rmtree(Path(config["directory"]) / "usage-state")
    previous_hour = old_summary["hourly"][str(int(BASE))]
    ingest(source, BASE + 3 * 86400, [increased(requests_total=110, generation_tokens_total=1020)])
    store.retention_tick(settings, BASE + 3 * 86400)
    assert len(store._db.execute("SELECT * FROM fleet_samples").fetchall()) == 1
    assert archive.run_archive(config)["ok"] is True
    usage = records(config, kind="usage")
    assert len(usage) == 1 and usage[0]["ts"] == BASE + 3 * 86400
    assert usage[0]["baseline"] is False
    assert usage[0]["usage"]["requests"] == 7 and usage[0]["usage"]["output_tokens"] == 10
    gap = records(config, kind="source_retention_gap")
    assert len(gap) == 1 and gap[0]["start_at"] == archive._iso(BASE)
    assert gap[0]["end_at"] == archive._iso(BASE + 2 * 86400)
    assert gap[0]["reason"] == "source_retention_before_initial_export"
    _, updated = read(config)
    assert updated["hourly"][str(int(BASE))] == previous_hour
    assert updated["usage"]["requests"] == 10 and updated["usage"]["output_tokens"] == 20
    assert set(updated) == archive.ARCHIVE_KEYS
    assert set(updated["source"]) == set(old_summary["source"]) == {
        "generated_at", "last_retention", "raw_cutoff", "hourly_retention_days", "hourly_cutoff"}
    assert archive._load_archive(old_path) == updated
    assert len(paths(config)[0]) == 3
    assert archive.run_archive(config)["usage_records_added"] == 0


def test_outage_retention_gap_does_not_recreate_expired_minute_records(source):
    store, settings, config = source
    ingest(source)
    archive.run_archive(config)
    captured = records(config, kind="usage")
    ingest(source, BASE + 60, [increased(requests_total=102)])
    ingest(source, BASE + 3 * 86400, [increased(requests_total=110)])
    store.retention_tick(settings, BASE + 3 * 86400)
    archive.run_archive(config)
    usage = records(config, kind="usage")
    assert len(usage) == 2 and usage[0] == captured[0]
    assert usage[-1]["usage"]["requests"] == 8
    assert records(config, kind="source_retention_gap")[0]["reason"] == "archive_outage_exceeded_raw_retention"
    assert archive.run_archive(config)["usage_records_added"] == 0


def test_fully_captured_ended_session_does_not_gain_false_gap_after_pruning(source):
    ingest(source)
    ingest(source, BASE + 60, [])
    config = source[2]
    assert archive.run_archive(config)["ok"] is True
    assert records(config, kind="source_retention_gap") == []
    before = records(config)
    state_path = paths(config)[1]
    checkpoint = state_path.read_bytes()
    source[0].retention_tick(source[1], BASE + 3 * 86400)
    assert archive.run_archive(config)["ok"] is True
    assert records(config) == before
    assert state_path.read_bytes() == checkpoint


def test_initial_sampled_export_keeps_existing_summary_bytes_unchanged(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    path, summary = read(config)
    before = path.read_bytes(), path.stat().st_mtime_ns
    shutil.rmtree(Path(config["directory"]) / "usage")
    shutil.rmtree(Path(config["directory"]) / "usage-state")
    result = archive.run_archive(config)
    assert result["unchanged"] == 1 and result["usage_records_added"] == 2
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert archive._load_archive(path) == summary


def test_preexisting_ended_session_without_raw_samples_does_not_invent_usage(source):
    store, settings, config = source
    ingest(source)
    ingest(source, BASE + 60, [])
    store.retention_tick(settings, BASE + 3 * 86400)
    result = archive.run_archive(config)
    assert result["ok"] is True
    assert records(config, kind="usage") == []
    assert {event["type"] for event in records(config)} == {"session_started", "session_ended", "source_retention_gap"}
    assert records(config, kind="source_retention_gap")[0]["end_at"] == archive._iso(BASE + 60)


@pytest.mark.parametrize("stage", ["day", "state", "summary"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_failure_at_each_publication_stage_retries_without_duplicate(source, monkeypatch, stage, after_replace):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 60, [increased(requests_total=103)])
    replace = archive.os.replace

    def fail(temporary, target):
        target = Path(target)
        matching = {"day": target.suffixes[-2:] == [".jsonl", ".gz"],
                    "state": target.parent.name == "usage-state",
                    "summary": target.parent == Path(config["directory"])}[stage]
        if matching:
            if after_replace:
                replace(temporary, target)
            raise OSError("synthetic publication interruption")
        return replace(temporary, target)

    with monkeypatch.context() as patch:
        patch.setattr(archive.os, "replace", fail)
        assert archive.run_archive(config)["failed"] == 1
    result = archive.run_archive(config)
    assert result["ok"] is True
    events = records(config)
    assert len(events) == len({event["event_id"] for event in events}) == 3
    assert sum(event["usage"]["requests"] or 0 for event in events if event["type"] == "usage") == 3
    assert read(config)[1]["usage"]["requests"] == 3
    assert json.loads(gzip.decompress(paths(config)[1].read_bytes()))["sample_count"] == 2
    assert archive.run_archive(config)["usage_records_added"] == 0
    assert not list(Path(config["directory"]).rglob("*.tmp"))


def test_actual_process_crash_between_two_daily_files_can_resume(source):
    ingest(source)
    config = source[2]
    ingest(source, BASE + 86400, [increased(requests_total=103)])
    script = """
import json, os, runpy, sys
ns = runpy.run_path(sys.argv[1])
replace = ns['os'].replace
calls = []
def crash(temporary, target):
    replace(temporary, target)
    calls.append(target)
    if len(calls) == 1:
        os._exit(73)
ns['os'].replace = crash
ns['run_archive'](json.loads(sys.argv[2]))
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script, archive.__file__, json.dumps(config)],
                            capture_output=True, timeout=10)
    assert result.returncode == 73
    assert not (Path(config["directory"]) / "usage-state").exists()
    assert archive.run_archive(config)["ok"] is True
    events = records(config)
    assert len(events) == len({event["event_id"] for event in events}) == 3
    assert len(paths(config)[0]) == 2
    assert records(config, kind="usage")[-1]["usage"]["requests"] == 3


def test_model_change_during_interrupted_retry_keeps_published_export_label(source, monkeypatch):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 60, [increased(requests_total=103)])
    original = archive._atomic_write

    def fail(directory, path, state):
        if directory.name == "usage-state":
            raise OSError("synthetic checkpoint interruption")
        return original(directory, path, state)

    with monkeypatch.context() as patch:
        patch.setattr(archive, "_atomic_write", fail)
        assert archive.run_archive(config)["failed"] == 1
    row = increased(requests_total=105)
    row["model"] = "renamed-demo"
    ingest(source, BASE + 120, [row])
    assert archive.run_archive(config)["ok"] is True
    usage = records(config, kind="usage")
    assert [event["model"] for event in usage] == ["demo-model", "demo-model", "renamed-demo"]


@pytest.mark.parametrize("target,corruption", [(target, corruption)
    for target in ("day", "state") for corruption in ("gzip", "extra_field", "null_counter", "duplicate", "missing")
    if (target, corruption) != ("state", "missing")])
def test_corrupt_artifacts_are_preserved_while_other_sessions_advance(source, target, corruption):
    ingest(source, rows=[service(), service("session-b")])
    config = source[2]
    archive.run_archive(config)
    usage_paths, state, _ = paths(config)
    path = usage_paths[0] if target == "day" else state
    if corruption == "missing":
        path.unlink()
        before = None
    else:
        if corruption == "gzip":
            raw = b"broken gzip"
        elif target == "day":
            events = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
            if corruption == "extra_field":
                events[-1]["prompt"] = PRIVATE
            elif corruption == "null_counter":
                events[-1]["usage"]["total_tokens"] = 1
            else:
                events.append(events[-1])
            raw = gzip.compress(b"".join((json.dumps(event) + "\n").encode() for event in events))
        else:
            value = json.loads(gzip.decompress(path.read_bytes()))
            if corruption == "extra_field":
                value["prompt"] = PRIVATE
            elif corruption == "null_counter":
                value["last_sample_sha256"] = None
            else:
                value["sample_count"] = -1
            raw = gzip.compress(json.dumps(value).encode())
        path.write_bytes(raw)
        before = path.read_bytes()
    second = service("session-b")
    second["metrics"]["requests_total"] = 102
    ingest(source, BASE + 60, [increased(requests_total=102), second])
    result = archive.run_archive(config)
    assert result["failed"] == 1 and result["written"] == 1
    if corruption == "missing":
        assert not path.exists() and result["errors"] == {"usage_checkpoint_without_records": 1}
    else:
        assert path.read_bytes() == before
        assert result["errors"] == {"usage_corrupt" if target == "day" else "usage_state_corrupt": 1}
    assert records(config, "session-b", "usage")[-1]["usage"]["requests"] == 2


def test_overlapping_source_sample_mutation_preserves_all_archives(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    ingest(source, BASE + 60, [increased(requests_total=103)])
    archive.run_archive(config)
    before = {path: path.read_bytes() for path in Path(config["directory"]).rglob("*") if path.is_file()}
    with source[0]._db as db:
        db.execute("UPDATE fleet_samples SET d_requests=1 WHERE ts=?", (BASE + 60,))
    assert archive.run_archive(config)["errors"] == {"source_sample_regression": 1}
    assert {path: path.read_bytes() for path in before} == before


def test_usage_checkpoint_advances_only_after_daily_directory_barrier(source, monkeypatch):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    usage_paths, state, _ = paths(config)
    before = state.read_bytes()
    ingest(source, BASE + 60, [increased(requests_total=103)])
    sync = archive.os.fsync
    barriers = []

    def fail(fd):
        path = Path(os.readlink('/proc/self/fd/' + str(fd)))
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            barriers.append(path)
            if path == usage_paths[0].parent:
                assert state.read_bytes() == before
                raise OSError("synthetic daily directory barrier failure")
        else:
            raw = gzip.decompress(path.read_bytes())
            if path.parent == usage_paths[0].parent:
                assert json.loads(raw.splitlines()[-1])["usage"]["requests"] == 3
        return sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(archive.os, "fsync", fail)
        assert archive.run_archive(config)["failed"] == 1
    assert state.read_bytes() == before and usage_paths[0].parent in barriers
    assert archive.run_archive(config)["ok"] is True
    assert json.loads(gzip.decompress(state.read_bytes()))["sample_count"] == 2
    assert len(records(config, kind="usage")) == 2


def test_missing_checkpoint_rebuild_deduplicates_retained_source_records(source):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    state = paths(config)[1]
    before = records(config)
    state.unlink()
    result = archive.run_archive(config)
    assert result["ok"] is True and result["usage_records_added"] == 0
    assert state.exists() and records(config) == before


def test_resume_query_is_bounded_to_cursor_overlap_and_new_samples(source, monkeypatch):
    for index in range(50):
        ingest(source, BASE + index * 60, [increased(requests_total=100 + index)])
    config = source[2]
    archive.run_archive(config)
    # The fresh capture needs three metadata rows at most, one instance, one
    # hourly bucket and only the overlapping + new raw sample, not all 51 rows.
    ingest(source, BASE + 50 * 60, [increased(requests_total=150)])
    monkeypatch.setattr(archive, "MAX_SOURCE_ROWS", 8)
    result = archive.run_archive(config)
    assert result["ok"] is True
    assert len(records(config, kind="usage")) == 51
    assert result["usage_records_added"] == 1


def test_sample_query_limit_aborts_without_publishing_partial_records(source, monkeypatch):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    for index in range(1, 30):
        ingest(source, BASE + index * 60, [increased(requests_total=100 + index)])
    before = {path: path.read_bytes() for path in Path(config["directory"]).rglob("*") if path.is_file()}
    monkeypatch.setattr(archive, "MAX_SOURCE_ROWS", 10)
    with pytest.raises(archive.ArchiveError, match="source_read_limit"):
        archive.run_archive(config)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("target", ["day", "state"])
def test_compressed_and_decoded_usage_read_bounds_preserve_previous_files(source, monkeypatch, target):
    ingest(source)
    config = source[2]
    archive.run_archive(config)
    usage_paths, state_path, _ = paths(config)
    path = usage_paths[0] if target == "day" else state_path
    encoded = path.read_bytes()
    decoded = gzip.decompress(encoded)
    limit = (len(encoded) + len(decoded)) // 2
    assert len(encoded) < limit < len(decoded)
    monkeypatch.setattr(archive, "MAX_USAGE_BYTES" if target == "day" else "MAX_USAGE_STATE_BYTES", limit)
    assert archive.run_archive(config)["errors"] == {"usage_read_limit": 1}
    assert path.read_bytes() == encoded


def test_private_jsonl_permissions_and_allowlist_cover_all_new_artifacts(source):
    ingest(source, rows=[service(model="/private/model/" + PRIVATE)])
    archive.run_archive(source[2])
    for path in Path(source[2]["directory"]).rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.suffix == ".gz":
            decoded = gzip.decompress(path.read_bytes())
            for forbidden in (PRIVATE, "192.0.2.123", "model_path", "argv", "api_address", "response", '"prompt"'):
                assert forbidden.encode() not in decoded
    assert all(event["model"] is None for event in records(source[2]))


@pytest.mark.parametrize("component", ["usage", "usage-state"])
def test_usage_directory_symlink_does_not_read_or_write_outside_archive(source, tmp_path, component):
    ingest(source)
    directory = Path(source[2]["directory"])
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / component).symlink_to(outside, target_is_directory=True)
    assert archive.run_archive(source[2])["errors"] == {"unsafe_usage_directory": 1}
    assert list(outside.iterdir()) == []


def test_new_usage_format_is_standalone_with_python_standard_library(source, tmp_path):
    ingest(source)
    script = tmp_path / "archive.py"
    shutil.copyfile(archive.__file__, script)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(source[2]))
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(script), "--config", str(config_path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)["usage_records_added"] == 2
    assert result.stderr == "" and len(records(source[2], kind="usage")) == 1
