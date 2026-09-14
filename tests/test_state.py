# Generated-By: Codex / gpt-6-astra
# Generated-By: OpenCode / deepseek-v4.1-flash
"""JSON compatibility and explicit unknown observation regression tests."""

import json
from dataclasses import FrozenInstanceError

import pytest

from llmsvc.state import Activity, GPUState, ModelState, Pin, StateSnapshot


def test_snapshot_json_preserves_unknown_and_all_public_sections():
    snapshot = StateSnapshot(
        gpus=(GPUState(index=0),),
        models=(ModelState(name="example"),),
        activity=(Activity(model="example"),),
        pins=(Pin(model="example", until=42.0, by="container-a"),),
    )
    data = json.loads(json.dumps(snapshot.to_dict(), allow_nan=False))
    assert data["schema_version"] == 1
    assert data["read_only"] is True
    assert data["models"][0]["state"] == "unknown"
    assert data["gpus"][0]["free_gb"] is None
    assert data["activity"][0]["in_flight"] is None
    assert data["pins"][0] == {"model": "example", "until": 42.0, "by": "container-a"}
    assert all(key in data for key in ("leases", "reserves", "memory", "blocked_by", "errors"))


def test_inactive_model_transition_is_omitted_from_json():
    snapshot = StateSnapshot(models=(ModelState(name="inactive"),))
    data = json.loads(json.dumps(snapshot.to_dict(), allow_nan=False))
    assert "transition" not in data["models"][0]  # Historical payloads unchanged.
    # Every pre-existing field/null is still present verbatim.
    assert data["models"][0]["state"] == "unknown"
    assert data["models"][0]["resident_gb"] is None
    assert data["models"][0]["is_default"] is False


def test_active_model_transition_is_present_and_roundtrips():
    snapshot = StateSnapshot(gpus=(GPUState(index=0, total_gb=144),), models=(
        ModelState(name="idle"), ModelState(name="loading", transition="SSDtoGPU")))
    data = snapshot.to_dict()
    assert "transition" not in data["models"][0]
    assert data["models"][1]["transition"] == "SSDtoGPU"
    # The serialized mapping converts back to the same records.
    assert json.loads(json.dumps(data, allow_nan=False))["models"][1]["transition"] == "SSDtoGPU"
    assert ModelState(name="loading", transition="SSDtoGPU") == snapshot.models[1]


def test_snapshot_records_are_frozen_and_serialization_is_detached():
    snapshot = StateSnapshot(models=(ModelState(name="example"),))
    with pytest.raises(FrozenInstanceError):
        snapshot.models[0].state = "awake"
    result = snapshot.to_dict()
    result["models"][0]["state"] = "awake"
    assert snapshot.models[0].state == "unknown"
