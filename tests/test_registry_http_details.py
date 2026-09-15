# Generated-By: Codex / gpt-6-astra
"""Existing registry planning metadata through actual unchanged HTTP routes."""

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest
import yaml

from llmsvc.reload import ReloadQueue
from llmsvc.state import Pin
from test_registry_http_preview import mounted, registry_fixture, request, assert_readonly


def add_body(mounted, name="candidate"):
    return {"name": name, "path": str(mounted.weights), "base": "base"}


def row(result, name):
    return next(item for item in result["inventory"]["models"] if item["name"] == name)


def test_inventory_keeps_temporary_records_and_distinguishes_configured_permanent_rows(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200 and result["records"] == mounted.records
    assert result["writes_enabled"] is False
    permanent, temporary = row(result, "base"), row(result, "saved")
    assert permanent["source"] == "config" and permanent["temporary"] is False
    assert temporary["source"] == "config" and temporary["temporary"] is True
    assert temporary["base"] == "base" and temporary["runtime_state"] == "stopped"
    assert temporary["removable"] is True and temporary["blocked_by"] == []
    assert {x["reason"] for x in result["blocked_by"]} >= {"registry_writes_disabled", "inflight_stream_unknown"}
    assert result["inventory"]["config_sha256"] == hashlib.sha256(mounted.path.read_bytes()).hexdigest()
    assert result["inventory"]["pending_changes"] == []
    assert result["inventory"]["recovery"]["settlement_confirmed"] is None
    assert_readonly(mounted, before)


def test_add_and_remove_writes_are_removed(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, added = request(mounted.address, "POST", "/v1/models?dry_run=1", add_body(mounted))
    assert status == 405 and added == {"error": "registry_writes_removed"}
    status, removed = request(mounted.address, "DELETE", "/v1/models/saved?dry_run=1")
    assert status == 405 and removed == {"error": "registry_writes_removed"}
    assert_readonly(mounted, before)


def test_pending_fifo_changes_are_listed_without_reserving_anything(mounted, monkeypatch):
    queue = mounted.registry.queue
    # Seed real pre-existing queued requests in the temporary fixture. No public
    # request can do this; callbacks are forbidden again before serving reads.
    identifiers = iter([UUID(int=1), UUID(int=2)])
    with monkeypatch.context() as setup:
        setup.setattr(queue, "_stage", ReloadQueue._stage.__get__(queue))
        setup.setattr(queue, "validate", lambda path: yaml.safe_load(path.read_bytes()))
        setup.setattr("llmsvc.reload.uuid.uuid4", lambda: next(identifiers))
        first = mounted.registry.add(add_body(mounted, "pending-a"))
        second = mounted.registry.add(add_body(mounted, "pending-b"))
    ids = [first["id"], second["id"]]
    before = mounted.files(), mounted.scheduler.events_since(0), [queue.get(i) for i in ids]
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200 and result["records"] == mounted.records
    inventory = result["inventory"]
    assert {item["name"] for item in inventory["models"]} == {"base", "saved"}
    assert [job["id"] for job in inventory["pending_changes"]] == ids
    assert [job["description"]["model"] for job in inventory["pending_changes"]] == ["pending-a", "pending-b"]
    assert all(job["status"] == "blocked" and job["recorded_status"] == "queued" for job in inventory["pending_changes"])
    assert (mounted.files(), mounted.scheduler.events_since(0), [queue.get(i) for i in ids]) == before
    assert [job.id for job in queue._pending] == ids and len(queue._jobs) == 2
    assert not mounted.calls and not mounted.units


@pytest.mark.parametrize("condition", ["pin", "unknown", "stale"])
def test_inventory_shows_protection_or_unknown_without_turning_remove_into_success(mounted, condition):
    if condition == "pin":
        mounted.state[0] = replace(mounted.state[0], pins=(Pin("saved", mounted.clock[0]+3600, "owner"),))
        mounted.scheduler.sample_once()
    elif condition == "unknown":
        state = mounted.state[0]
        mounted.state[0] = replace(state, models=(state.models[0], replace(state.models[1], state="unknown")),
                                  activity=tuple(item for item in state.activity if item.model != "saved"))
        mounted.scheduler.sample_once()
    else:
        mounted.clock[0] += mounted.scheduler.config.max_snapshot_age_seconds + 1
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200
    saved = row(result, "saved")
    assert saved["removable"] is False and saved["blocked_by"]
    if condition == "unknown":
        assert saved["runtime_state"] == "unknown" and saved["last_used_at"] is None
    elif condition == "stale":
        assert saved["runtime_state"] == "unknown"
    else:
        assert "pinned" in {item["reason"] for item in saved["blocked_by"]}
    assert request(mounted.address, "DELETE", "/v1/models/saved?dry_run=1") == (405, {"error": "registry_writes_removed"})
    assert_readonly(mounted, before)


def test_pending_marker_is_inspectable_but_still_rejects_change_preview(mounted):
    marker = mounted.registry.queue.marker
    marker.write_bytes(b"interrupted fixture marker")
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "GET", "/v1/models")
    assert status == 200 and result["inventory"]["fenced"]
    assert result["inventory"]["recovery"]["settlement_confirmed"] is None
    assert result["inventory"]["recovery"]["marker_valid"] is False
    assert {"reason": "registry_reconciliation_required"} in result["blocked_by"]
    assert request(mounted.address, "POST", "/v1/models?dry_run=1", add_body(mounted)) == (405, {"error": "registry_writes_removed"})
    assert_readonly(mounted, before)


@pytest.mark.parametrize("missing", ["daemon_port", "created_at"])
def test_incomplete_source_metadata_is_503_not_a_broken_inventory_response(mounted, missing):
    config = yaml.safe_load(mounted.path.read_bytes())
    del config["models"]["saved"]["metadata"]["llmsvc_registry"][missing]
    mounted.path.write_text(yaml.safe_dump(config))
    before = mounted.files(), mounted.scheduler.events_since(0)
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})
    assert_readonly(mounted, before)
