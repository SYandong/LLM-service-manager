# Generated-By: Codex / gpt-6-astra
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


def test_snapshot_records_are_frozen_and_serialization_is_detached():
    snapshot = StateSnapshot(models=(ModelState(name="example"),))
    with pytest.raises(FrozenInstanceError):
        snapshot.models[0].state = "awake"
    result = snapshot.to_dict()
    result["models"][0]["state"] = "awake"
    assert snapshot.models[0].state == "unknown"
