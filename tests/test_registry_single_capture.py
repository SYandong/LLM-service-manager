# Generated-By: Codex / gpt-6-astra
"""One source capture for model records/rows/hash, with actual HTTP consumption."""
import hashlib
import json

from test_registry_http_preview import assert_readonly, mounted, registry_fixture, request


def digest(data):
    return hashlib.sha256(data).hexdigest()


def swap_after_capture(mounted, monkeypatch):
    old = mounted.path.read_bytes()
    new = old.replace(b"saved", b"replacement")
    assert new != old
    external = mounted.path.with_suffix(".external")
    external.write_bytes(new)
    before_files = mounted.files()
    old_key = next(key for key in before_files if key.endswith("config.yaml"))
    replacement_key = next(key for key in before_files if key.endswith("config.external"))
    expected_files = {key: value for key, value in before_files.items() if key != replacement_key}
    expected_files[old_key] = new
    captures, parses = [], []
    read = mounted.registry.queue._read
    decode = mounted.registry._decode
    def read_then_external_replace():
        result = read()
        captures.append(result[0])
        if len(captures) == 1:
            # Deterministic external writer: replacement happens after the safe
            # descriptor read returned, before the legacy caller's second read.
            external.replace(mounted.path)
        return result
    def counted_decode(data):
        parses.append(data)
        return decode(data)
    monkeypatch.setattr(mounted.registry.queue, "_read", read_then_external_replace)
    monkeypatch.setattr(mounted.registry, "_decode", counted_decode)
    return old, new, expected_files, captures, parses


def assert_old_capture(result, old):
    assert set(result["records"]) == {"saved"}
    inventory = result["inventory"]
    assert {row["name"] for row in inventory["models"] if row["temporary"]} == {"saved"}
    assert inventory["config_sha256"] == digest(old)
    row = next(row for row in inventory["models"] if row["name"] == "saved")
    for key in ("base", "created_at", "daemon_port"):
        assert row[key] == result["records"]["saved"][key]
    # Internal capture plumbing must not add a field to the public inventory.
    assert set(result) == {"records", "inventory", "writes_enabled", "blocked_by"}
    assert set(inventory) == {"models", "config_sha256", "pending_changes", "fenced", "recovery"}


def test_actual_http_records_and_inventory_share_one_capture(mounted, monkeypatch):
    old, new, expected_files, captures, parses = swap_after_capture(mounted, monkeypatch)
    events = mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200
    assert_old_capture(result, old)
    assert captures == parses == [old]
    assert_readonly(mounted, (expected_files, events))
    assert mounted.path.read_bytes() == new  # Coherence does not mean still current.
    # The simulated writer replaces only after its first capture. A later
    # request uses the new file; keep all mounted no-effect guards installed.
    status, following = request(mounted.address, "GET", "/v1/models")
    assert status == 200 and set(following["records"]) == {"replacement"}
    assert following["inventory"]["config_sha256"] == digest(new)
    assert captures == parses == [old, new]
    assert_readonly(mounted, (expected_files, events))


def test_capture_keeps_unknown_runtime_separate(mounted, monkeypatch):
    mounted.clock[0] += mounted.scheduler.config.max_snapshot_age_seconds + 1
    old, _, expected_files, captures, parses = swap_after_capture(mounted, monkeypatch)
    events = mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200
    assert_old_capture(result, old)
    assert all(row["runtime_state"] == "unknown" for row in result["inventory"]["models"])
    assert all(not row["removable"] for row in result["inventory"]["models"])
    assert captures == parses == [old]
    assert_readonly(mounted, (expected_files, events))


def test_recovery_still_checks_current_file_and_keeps_fence(mounted, monkeypatch):
    old = mounted.path.read_bytes()
    marker = {"schema_version": 1, "sha256": digest(old), "job": {
        "id": "a" * 32, "description": {"kind": "fixture"}, "status": "queued",
        "blocked_by": [], "error": None, "config_committed": False, "apply_seconds": None}}
    mounted.registry.queue.marker.write_text(json.dumps(marker))
    old, new, expected_files, captures, parses = swap_after_capture(mounted, monkeypatch)
    events = mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200
    assert_old_capture(result, old)
    # The independent recovery diagnostic retains its current-file read. It must
    # not treat the earlier capture as proof that the marker's candidate is current.
    assert captures == [old, new] and parses == [old]
    recovery = result["inventory"]["recovery"]
    assert result["inventory"]["fenced"] and recovery["fenced"]
    assert recovery["candidate_file_matches"] is False
    assert recovery["settlement_confirmed"] is None
    assert {"reason": "candidate_file_digest_changed"} in recovery["blocked_by"]
    assert {"reason": "registry_reconciliation_required"} in result["blocked_by"]
    assert_readonly(mounted, (expected_files, events))


def test_inventory_records_are_opt_in_and_detached(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    baseline = mounted.registry.inventory()
    capture = mounted.registry.inventory(include_records=True)
    records = capture.pop("records")
    assert capture == baseline and records == mounted.registry.records()
    records["saved"]["created_at"] = -1
    capture["models"].clear()
    assert mounted.registry.records()["saved"]["created_at"] != -1
    assert mounted.registry.inventory() == baseline
    assert_readonly(mounted, before)
