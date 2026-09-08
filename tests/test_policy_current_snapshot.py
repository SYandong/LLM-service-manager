# Generated-By: Codex / gpt-6-astra
"""Policy-authored outcomes for one unmodified real current-state capture."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from llmsvc import policy
from llmsvc.state import (
    Activity, Blocker, GPUProcess, GPUState, Lease, MemoryState, ModelState,
    Pin, Reserve, StateSnapshot,
)

ROOT = Path(__file__).parent / "fixtures/policy"
MANIFEST = json.loads((ROOT / "current_snapshot_expectations.json").read_text())
SOURCE = ROOT / MANIFEST["source"]["path"]


def captured_snapshot(raw):
    """Convert JSON containers to core tuples without filling observations."""
    data = dict(raw)
    data["gpus"] = tuple(GPUState(**{**g, "external_processes": tuple(
        GPUProcess(**p) for p in g["external_processes"])}) for g in raw["gpus"])
    data["activity"] = tuple(Activity(**{**a, "by": tuple(a["by"])}) for a in raw["activity"])
    for field, record in (("models", ModelState), ("pins", Pin), ("reserves", Reserve),
                          ("leases", Lease), ("blocked_by", Blocker)):
        data[field] = tuple(record(**row) for row in raw[field])
    data["memory"] = MemoryState(**raw["memory"])
    data["errors"] = tuple(raw["errors"])
    return StateSnapshot(**data)


@pytest.mark.parametrize("evaluation", MANIFEST["evaluations"], ids=lambda e:e["policy"])
def test_unmodified_real_current_snapshot_policy_outcomes(evaluation):
    original = SOURCE.read_bytes()
    assert hashlib.sha256(original).hexdigest() == MANIFEST["source"]["sha256"]
    exported = json.loads(original)
    s = captured_snapshot(exported["snapshot"])
    # Round-trip equality prevents clearing errors/nulls or inventing metadata.
    assert json.loads(json.dumps(s.to_dict())) == exported["snapshot"]
    args = [s]
    if "request_from_model" in evaluation:
        args.append(next(m for m in s.models if m.name == evaluation["request_from_model"]))
    decision = getattr(policy, evaluation["policy"])(*args)
    assert [asdict(a) for a in decision.actions] == MANIFEST["expected"]["actions"]
    assert [asdict(b) for b in decision.blocked_by] == MANIFEST["expected"]["blocked_by"]
    assert json.loads(json.dumps(s.to_dict())) == exported["snapshot"]
    assert SOURCE.read_bytes() == original


def test_current_snapshot_expectations_do_not_reconstruct_historical_journal_state():
    exported = json.loads(SOURCE.read_text())
    assert exported["expected_actions"] is None  # Exporter/telemetry fixture untouched.
    assert exported["capture"]["not_contemporaneous"] is True
    assert max(e["timestamp"] for e in exported["journal_events"]) < exported["snapshot"]["sampled_at"]
    assert exported["snapshot"]["sampled_at"] == MANIFEST["source"]["sampled_at"]
    assert MANIFEST["provenance"]["kind"] == "captured_current_snapshot"
    assert MANIFEST["provenance"]["independent_captures"] == 1
    assert MANIFEST["provenance"]["historical_reconstruction"] is False
    assert len(exported["journal_events"]) == 30 and exported["journal_dropped"] == 70
