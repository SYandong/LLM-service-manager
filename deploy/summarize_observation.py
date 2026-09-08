#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Summarize existing capture artifacts offline; never run a probe or producer."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

MAX_FILE_BYTES = 16 * 1024 * 1024


class SummaryError(ValueError):
    pass


def timestamp(value):
    if not isinstance(value, str):
        raise SummaryError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        raise SummaryError("invalid_timestamp") from None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def positive(value, name, *, zero=False):
    if not finite(value) or (value < 0 if zero else value <= 0):
        raise SummaryError("invalid_" + name)
    return float(value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SummaryError("duplicate_json_key")
        result[key] = value
    return result


def no_constant(value):
    raise SummaryError("nonfinite_json")


def read_json(path):
    """Bound reads and reject symlinks/FIFOs without blocking on a special file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SummaryError("not_regular_file")
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise SummaryError("file_too_large")
        data = json.loads(raw, object_pairs_hook=unique_object, parse_constant=no_constant)
        if not isinstance(data, dict):
            raise SummaryError("not_json_object")
        return data, hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        raise SummaryError("missing_file") from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise SummaryError("unreadable_or_invalid_json") from None


def record(directory, path):
    manifest, manifest_hash = read_json(path)
    name = manifest.get("snapshot")
    if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
            or not name.startswith("snapshot-") or not name.endswith(".json")):
        raise SummaryError("unsafe_snapshot_reference")
    snapshot, digest = read_json(directory / name)
    if not isinstance(manifest.get("sha256"), str) or digest != manifest["sha256"]:
        raise SummaryError("snapshot_hash_mismatch")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1
            or snapshot.get("read_only") is not True or snapshot.get("dry_run") is not False):
        raise SummaryError("unsupported_capture_schema_or_dry_run")
    captured, created = timestamp(snapshot.get("sampled_at")), timestamp(manifest.get("created_at"))
    if created < captured:
        raise SummaryError("manifest_precedes_capture")
    sources = snapshot.get("sources")
    if not isinstance(sources, list):
        raise SummaryError("invalid_source_list")
    names = [source.get("name") for source in sources if isinstance(source, dict)]
    if (len(names) != len(sources) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in names)
            or len(set(names)) != len(names)):
        raise SummaryError("invalid_or_duplicate_source_names")
    declared = snapshot.get("provenance", {}).get("sources", []) if isinstance(snapshot.get("provenance"), dict) else []
    expected = {item["name"] for item in declared if isinstance(item, dict)
                and isinstance(item.get("name"), str) and re.fullmatch(r"[A-Za-z0-9_.-]+", item["name"])} if isinstance(declared, list) else set()
    return {"manifest": path.name, "manifest_sha256": manifest_hash, "snapshot": name,
            "snapshot_sha256": digest, "captured": captured, "created": created,
            "sources": {item["name"]: item for item in sources}, "expected": expected,
            "provenance": snapshot.get("provenance", {})}


def coverage(points, interval, tolerance, window=None, *, basis=None):
    points = sorted(set(points))
    start, end = window if window is not None else ((points[0], points[-1]) if points else (None, None))
    if start is not None:
        points = [value for value in points if start <= value <= end]
    if start is not None and not finite((end - start) / interval):
        raise SummaryError("cadence_numeric_range_exceeded")
    gaps = []
    for left, right in zip(points, points[1:]):
        delta = right - left
        if delta > interval + tolerance:
            gaps.append({"from": iso(left), "to": iso(right), "seconds": round(delta, 6),
                         "estimated_missing_intervals": max(0, math.ceil((delta - tolerance) / interval) - 1)})
    leading = points[0] - start if points else None
    trailing = end - points[-1] if points else None
    expected = math.floor((end - start) / interval) + 1 if start is not None else 0
    missing = (sum(row["estimated_missing_intervals"] for row in gaps)
               + max(0, math.ceil((leading - tolerance) / interval))
               + max(0, math.ceil((trailing - tolerance) / interval))) if points else expected
    return {"window_start": iso(start) if start is not None else None,
            "window_end": iso(end) if end is not None else None,
            "window_basis": basis or ("requested" if window is not None else "observed_bounds"),
            "window_seconds": round(end - start, 6) if start is not None else 0,
            "first_sampled_at": iso(points[0]) if points else None,
            "last_sampled_at": iso(points[-1]) if points else None,
            "observed_span_seconds": round(points[-1] - points[0], 6) if points else 0,
            "unique_samples": len(points), "expected_samples_at_configured_cadence": expected,
            "leading_gap_seconds": round(leading, 6) if leading is not None else None,
            "trailing_gap_seconds": round(trailing, 6) if trailing is not None else None,
            "max_internal_gap_seconds": round(max((b - a for a, b in zip(points, points[1:])), default=0), 6),
            "gaps": gaps, "estimated_missing_intervals": missing,
            "sampled_window_complete": bool(points) and not gaps and leading <= tolerance and trailing <= tolerance,
            "continuous_running_proven": False}


def distribution(values, bucket):
    values = sorted(values)
    def percentile(fraction):
        location = (len(values) - 1) * fraction
        low, high = math.floor(location), math.ceil(location)
        return round(values[low] + (values[high] - values[low]) * (location - low), 6)
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None, "p99": None,
                "zero_count": 0, "positive_count": 0, "histogram": []}
    if any(not finite(value / bucket) for value in values):
        raise SummaryError("histogram_numeric_range_exceeded")
    bins = Counter(math.floor(value / bucket) for value in values)
    if any(not index * bucket < (index + 1) * bucket for index in bins):
        raise SummaryError("histogram_resolution_exceeded")
    return {"count": len(values), "min": values[0], "max": values[-1],
            "mean": round(math.fsum(value / len(values) for value in values), 6),
            "p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99),
            "zero_count": values.count(0), "positive_count": sum(value > 0 for value in values),
            "histogram": [{"lower_inclusive": index * bucket, "upper_exclusive": (index + 1) * bucket, "count": count}
                          for index, count in sorted(bins.items())]}


def source_valid(source, item):
    truncation = source.get("truncated", False)
    if isinstance(truncation, dict):
        truncated = any(value is True for value in truncation.values())
        shape_valid = all(type(value) is bool for value in truncation.values())
    else:
        truncated, shape_valid = truncation is True, type(truncation) is bool
    truncated = truncated or source.get("error") == "output_limit_exceeded"
    try:
        started, ended = timestamp(source.get("started_at")), timestamp(source.get("ended_at"))
        timing = item["captured"] <= started <= ended <= item["created"]
    except SummaryError:
        ended, timing = None, False
    transport = ((source.get("type") == "http_json" and source.get("http_status") == 200)
                 or (source.get("type") == "command" and type(source.get("returncode")) is int and source["returncode"] == 0))
    valid = source.get("status") == "ok" and not source.get("error") and not truncated and shape_valid and timing and transport
    return bool(valid), bool(truncated), ended


def state_data(source, ended, max_age):
    state = source.get("json")
    if (source.get("type") != "http_json" or not isinstance(state, dict)
            or type(state.get("schema_version")) is not int or state["schema_version"] != 1
            or not isinstance(state.get("gpus"), list) or type(state.get("read_only")) is not bool
            or not isinstance(state.get("errors"), list)):
        raise SummaryError("invalid_state_schema")
    sampled = state.get("sampled_at")
    if not finite(sampled):
        raise SummaryError("invalid_state_timestamp")
    try:
        iso(sampled)
    except (ValueError, OverflowError, OSError):
        raise SummaryError("invalid_state_timestamp") from None
    if not 0 <= ended - sampled <= max_age:
        raise SummaryError("stale_or_future_state")
    gpus = []
    seen_indices, seen_ids = set(), set()
    for gpu in state["gpus"]:
        if not isinstance(gpu, dict) or type(gpu.get("index")) is not int or gpu["index"] < 0:
            raise SummaryError("invalid_gpu_identity")
        identity = gpu.get("uuid")
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise SummaryError("invalid_gpu_identity")
        key = "uuid:" + identity if identity is not None else "index:" + str(gpu["index"])
        if gpu["index"] in seen_indices or key in seen_ids:
            raise SummaryError("duplicate_gpu_identity")
        seen_indices.add(gpu["index"])
        seen_ids.add(key)
        external, total = gpu.get("external_gb"), gpu.get("total_gb")
        known = finite(external) and external >= 0
        if known and finite(total) and (total <= 0 or external > total):
            known = False
        gpus.append({"identity": key, "index": gpu["index"], "external_gib": external if known else None})
    return {"sampled": sampled, "gpus": sorted(gpus, key=lambda row: row["identity"]),
            "with_errors": bool(state["errors"]), "read_only": state["read_only"]}


def summarize(directory, *, interval_seconds=15, tolerance_seconds=2, state_source="scheduler-state",
              max_state_age_seconds=30, bucket_gib=10, window_start=None, window_end=None):
    interval = positive(interval_seconds, "interval")
    tolerance = positive(tolerance_seconds, "tolerance", zero=True)
    age = positive(max_state_age_seconds, "max_state_age")
    bucket = positive(bucket_gib, "bucket")
    if tolerance >= interval:
        raise SummaryError("tolerance_must_be_less_than_interval")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", state_source):
        raise SummaryError("invalid_state_source")
    if (window_start is None) != (window_end is None):
        raise SummaryError("both_window_bounds_required")
    window = (timestamp(window_start), timestamp(window_end)) if window_start is not None else None
    if window and window[1] < window[0]:
        raise SummaryError("window_end_before_start")
    directory = Path(directory)
    if not directory.is_dir():
        raise SummaryError("input_directory_unavailable")
    paths = sorted(directory.glob("manifest-*.json"))
    rejected, loaded, inventory = [], [], []
    snapshots, hashes = set(), set()
    duplicate_references = duplicate_payloads = reordered = 0
    previous = None
    for path in paths:
        try:
            item = record(directory, path)
        except SummaryError as exc:
            rejected.append({"manifest": path.name, "reason": str(exc)})
            continue
        inventory.append({key: item[key] for key in ("manifest", "manifest_sha256", "snapshot", "snapshot_sha256")})
        if item["snapshot"] in snapshots:
            duplicate_references += 1
            continue
        snapshots.add(item["snapshot"])
        if item["snapshot_sha256"] in hashes:
            duplicate_payloads += 1
            continue
        hashes.add(item["snapshot_sha256"])
        if previous is not None and item["captured"] < previous:
            reordered += 1
        previous = item["captured"]
        loaded.append(item)
    # Ambiguous values at one capture timestamp cannot be selected by filename luck.
    capture_counts = Counter(item["captured"] for item in loaded)
    conflicts = sum(count for count in capture_counts.values() if count > 1)
    accepted = [item for item in loaded if capture_counts[item["captured"]] == 1
                and (window is None or window[0] <= item["captured"] <= window[1])]
    source_names = {state_source}
    for item in accepted:
        source_names.update(item["expected"] | item["sources"].keys())
    sources = {name: {"observations": 0, "valid": 0, "failures": 0, "truncated": 0, "missing": 0} for name in sorted(source_names)}
    states, state_errors = [], Counter()
    state_reordered, previous_state = 0, None
    for item in accepted:
        for name, stats in sources.items():
            source = item["sources"].get(name)
            if source is None:
                stats["missing"] += 1
                continue
            valid, truncated, ended = source_valid(source, item)
            stats["observations"] += 1
            stats["valid"] += int(valid)
            stats["failures"] += int(not valid)
            stats["truncated"] += int(truncated)
            if name == state_source and valid:
                try:
                    state = state_data(source, ended, age)
                    provenance = item["provenance"]
                    declared = [entry for entry in provenance.get("sources", [])
                                if isinstance(entry, dict) and entry.get("name") == name] if isinstance(provenance, dict) and isinstance(provenance.get("sources"), list) else []
                    if (len(declared) != 1 or declared[0].get("type") != "http_json"
                            or not isinstance(source.get("url"), str) or not source["url"]
                            or source["url"] != declared[0].get("url")
                            or not isinstance(provenance.get("config_path"), str)):
                        raise SummaryError("unverified_state_source_identity")
                    state["source_identity"] = hashlib.sha256(json.dumps(
                        {"type": "http_json", "url": source["url"], "capture_config": provenance["config_path"]},
                        sort_keys=True).encode()).hexdigest()
                except SummaryError as exc:
                    state_errors[str(exc)] += 1
                    continue
                if previous_state is not None and state["sampled"] < previous_state:
                    state_reordered += 1
                previous_state = state["sampled"]
                states.append(state)
    fresh_count = len(states)
    identities = sorted({item["source_identity"] for item in states})
    if len(identities) > 1:
        state_errors["mixed_state_source_identities"] += len(states)
        states = []  # Never merge index-only GPUs from different producers.
    by_time = defaultdict(list)
    for item in states:
        by_time[item["sampled"]].append(item)
    unique_states = []
    state_duplicates = state_conflicts = 0
    for _, items in sorted(by_time.items()):
        signatures = {json.dumps(item["gpus"], sort_keys=True) for item in items}
        if len(signatures) != 1:
            state_conflicts += len(items)
        else:
            state_duplicates += len(items) - 1
            unique_states.append(items[0])
    common_window = window
    if common_window is None and accepted:
        common_window = (min(item["captured"] for item in accepted), max(item["captured"] for item in accepted))
    # Fresh states can precede capture start or arrive during its HTTP request.
    # Keep those measured values unless the caller explicitly selected a window.
    state_window = [item for item in unique_states if window is None or window[0] <= item["sampled"] <= window[1]]
    series = defaultdict(list)
    for item in state_window:
        for gpu in item["gpus"]:
            series[gpu["identity"]].append((item["sampled"], gpu))
    gpu_report = []
    for identity, observations in sorted(series.items()):
        values = [gpu["external_gib"] for _, gpu in observations if gpu["external_gib"] is not None]
        gpu_report.append({"identity": identity, "indices_seen": sorted({gpu["index"] for _, gpu in observations}),
                           "first_sampled_at": iso(observations[0][0]), "last_sampled_at": iso(observations[-1][0]),
                           "observations": len(observations), "missing_from_state_samples": len(state_window) - len(observations),
                           "unknown_external_samples": len(observations) - len(values),
                           "reported_external_gib": distribution(values, bucket)})
    return {"schema_version": 1, "generated_by": "Codex / gpt-6-astra",
            "scope": {"offline": True, "continuous_running_proven": False,
                      "long_term_stability": "NOT MEASURED", "threshold_calibration": "NOT MEASURED",
                      "calendar_wait_required": False, "occupancy_is_reported_not_independent_process_proof": True},
            "settings": {"interval_seconds": interval, "tolerance_seconds": tolerance,
                         "state_source": state_source, "max_state_age_seconds": age, "bucket_gib": bucket},
            "integrity": {"manifest_count": len(paths), "sha_verified_records": len(inventory),
                          "rejected": rejected, "duplicate_snapshot_references": duplicate_references,
                          "duplicate_snapshot_payloads": duplicate_payloads, "conflicting_capture_records": conflicts,
                          "capture_order_inversions": reordered,
                          "unreferenced_snapshots": sorted(path.name for path in directory.glob("snapshot-*.json") if path.name not in snapshots),
                          "input_inventory": inventory},
            "sample_count": len(accepted), "sources": sources,
            "capture_coverage": coverage([item["captured"] for item in accepted], interval, tolerance, window),
            "state_observations": {"fresh": fresh_count, "source_identity_sha256": identities,
                                   "errors": dict(sorted(state_errors.items())),
                                   "duplicate_timestamps": state_duplicates, "conflicting_records": state_conflicts,
                                   "order_inversions": state_reordered, "unique_for_statistics": len(state_window),
                                   "with_collection_errors": sum(item["with_errors"] for item in states),
                                   "read_only_true": sum(item["read_only"] for item in states)},
            "state_coverage": coverage([item["sampled"] for item in state_window], interval, tolerance, common_window,
                                       basis="requested" if window is not None else "capture_bounds"),
            "gpus": gpu_report}


def render(report, format):
    if format == "json":
        return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["field", "json_value"])
    def visit(value, path):
        if isinstance(value, dict) and value:
            for key in sorted(value):
                visit(value[key], path + [str(key)])
        elif isinstance(value, list) and value:
            for index, item in enumerate(value):
                visit(item, path + [str(index)])
        else:
            # JSON string quoting also prevents spreadsheet formula evaluation.
            pointer = "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)
            writer.writerow([pointer, json.dumps(value, ensure_ascii=False, allow_nan=False)])
    visit(report, [])
    return stream.getvalue()


def write_report(path, text):
    """Publish one private output atomically without overwriting an existing file."""
    fd, temporary = tempfile.mkstemp(prefix=".observation-summary-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="new file outside input directory; default stdout")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--tolerance-seconds", type=float, default=2)
    parser.add_argument("--state-source", default="scheduler-state")
    parser.add_argument("--max-state-age-seconds", type=float, default=30)
    parser.add_argument("--bucket-gib", type=float, default=10)
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--dry-run", action="store_true", help="read/verify and print report without writing --output")
    args = parser.parse_args(argv)
    try:
        if args.output and (args.output.resolve().is_relative_to(args.input_dir.resolve()) or args.output.exists() or args.output.is_symlink()):
            raise SummaryError("output_must_be_new_and_outside_input")
        report = summarize(args.input_dir, interval_seconds=args.interval_seconds,
                           tolerance_seconds=args.tolerance_seconds, state_source=args.state_source,
                           max_state_age_seconds=args.max_state_age_seconds, bucket_gib=args.bucket_gib,
                           window_start=args.window_start, window_end=args.window_end)
        text = render(report, args.format)
        if args.output and not args.dry_run:
            write_report(args.output, text)
        else:
            print(text, end="")
        return 0
    except (SummaryError, OSError, ValueError) as exc:
        print("observation summary: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
