# Generated-By: Codex / gpt-6-astra
"""Real queue/recovery read projections through the scheduler HTTP boundary."""
import json
import os
import threading
from types import SimpleNamespace

import pytest

from llmsvc.__main__ import build_registry
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from test_registry_http_preview import request
from test_reload import harness


@pytest.fixture
def inspection(harness):
    queue, quiet, clock, calls, logs = harness
    config = SchedulerConfig("127.0.0.1", 8103, registry={"config_path": str(queue.path),
        "shared_roots": [str(queue.path.parent)], "daemon_port_range": [8104, 8110]})
    scheduler = Scheduler(config, collect=queue.snapshot, clock=clock)
    scheduler.sample_once()
    scheduler.registry = build_registry(config, scheduler)
    # The fixture can seed actual jobs before the read-only HTTP boundary is used.
    queue.action_lock = scheduler.action_lock
    queue.snapshot = scheduler.snapshot
    scheduler.registry.queue = queue
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    thread.start()
    result = SimpleNamespace(scheduler=scheduler, queue=queue, quiet=quiet, clock=clock,
        calls=calls, logs=logs, address=server.server_address)
    try:
        yield result
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(3)


def prohibit_effects(inspection, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Registry inspection attempted a mutation, verifier, probe or action")
    queue = inspection.scheduler.registry.queue
    for name in ("enqueue", "process_once", "reconcile", "_stage", "validate", "notify_reload", "log"):
        monkeypatch.setattr(queue, name, forbidden)
    monkeypatch.setattr("llmsvc.reload_witness.NativeGenerationReader.read", forbidden)
    monkeypatch.setattr("llmsvc.reload.uuid.uuid4", forbidden)


def test_empty_status_is_actual_configured_queue_not_commit_readiness(inspection, monkeypatch):
    prohibit_effects(inspection, monkeypatch)
    before = inspection.queue.path.read_bytes(), inspection.scheduler.events_since(0)
    status, result = request(inspection.address, "GET", "/v1/registry")
    assert status == 200 and result["writes_enabled"] is False
    assert result["queue"] == inspection.scheduler.registry.queue_snapshot()
    assert result["queue"]["jobs"] == [] and result["queue"]["pending_ids"] == []
    assert result["queue"]["recovery"]["status"] == "none"
    assert {x["reason"] for x in result["blocked_by"]} >= {"registry_writes_disabled", "inflight_stream_unknown"}
    assert (inspection.queue.path.read_bytes(), inspection.scheduler.events_since(0)) == before


def test_pending_and_elapsed_jobs_are_projections_not_consumed_requests(inspection, monkeypatch):
    queue = inspection.queue
    job = queue.enqueue(lambda data: data + b"# proposed\n", description={"kind": "add_model", "model": "candidate"})
    before = queue.path.read_bytes(), queue.get(job["id"]), list(inspection.calls), list(inspection.logs)
    prohibit_effects(inspection, monkeypatch)
    status, result = request(inspection.address, "GET", "/v1/registry")
    row = result["queue"]["jobs"][0]
    assert status == 200 and row["id"] == job["id"] and row["source"] == "memory"
    assert row["status"] == "blocked" and row["recorded_status"] == "queued"
    assert row["config_committed"] is False and result["queue"]["pending_ids"] == [job["id"]]
    inspection.clock.advance(600)
    status, result = request(inspection.address, "GET", "/v1/registry")
    assert status == 200 and result["queue"]["jobs"][0]["status"] == "timed_out"
    assert result["queue"]["jobs"][0]["remaining_seconds"] == 0 and result["queue"]["pending_ids"] == []
    assert len(queue._pending) == 1
    assert (queue.path.read_bytes(), queue.get(job["id"]), inspection.calls, inspection.logs) == before


def seed_marker(inspection):
    import hashlib
    queue = inspection.queue
    # This is a crash-image fixture with the exact owner's persisted format, not
    # proof that any real config was adopted or any old process exited.
    job = queue.enqueue(lambda data: data, description={"kind": "add_model", "model": "candidate"})
    record = {"schema_version": 1, "sha256": hashlib.sha256(queue.path.read_bytes()).hexdigest(), "job": job}
    queue.marker.write_text(json.dumps(record))
    return job


def test_restart_retains_marker_unknowns_and_does_not_resurrect_ephemeral_jobs(inspection, monkeypatch):
    job = seed_marker(inspection)
    old = inspection.queue
    registry = build_registry(inspection.scheduler.config, inspection.scheduler)
    inspection.scheduler.registry = registry
    before = old.path.read_bytes(), old.marker.read_bytes(), inspection.scheduler.events_since(0)
    prohibit_effects(inspection, monkeypatch)
    status, result = request(inspection.address, "GET", "/v1/registry")
    queue = result["queue"]
    assert status == 200 and queue["fenced"] and queue["pending_ids"] == []
    row = queue["jobs"][0]
    assert row["id"] == job["id"] and row["source"] == "recovery_marker"
    assert row["status"] == "reconciliation_required"
    assert row["elapsed_seconds"] is None and row["remaining_seconds"] is None and row["config_committed"] is None
    assert queue["recovery"]["candidate_file_matches"] is True
    assert queue["recovery"]["candidate_generation_visible"] is False and queue["recovery"]["settlement_confirmed"] is None
    assert not registry.queue._jobs and not registry.queue._pending
    assert (old.path.read_bytes(), old.marker.read_bytes(), inspection.scheduler.events_since(0)) == before


@pytest.mark.parametrize("kind", ["malformed", "oversize", "fifo", "symlink"])
def test_invalid_marker_remains_fenced_and_read_does_not_clear_it(inspection, monkeypatch, kind):
    marker = inspection.queue.marker
    if kind == "malformed": marker.write_bytes(b"{")
    elif kind == "oversize": marker.write_bytes(b" " * 65537)
    elif kind == "fifo": os.mkfifo(marker)
    else: marker.symlink_to(inspection.queue.path)
    before = marker.lstat()
    prohibit_effects(inspection, monkeypatch)
    status, result = request(inspection.address, "GET", "/v1/registry")
    recovery = result["queue"]["recovery"]
    assert status == 200 and result["queue"]["fenced"]
    assert recovery["status"] == "reconciliation_required" and recovery["marker_valid"] is False
    assert recovery["settlement_confirmed"] is None
    assert marker.lstat() == before


@pytest.mark.parametrize("method,path,body,expected", [("GET", "/v1/registry?url=http://127.0.0.1:1", None, 400),
    ("GET", "/v1/registry", {"proof": True}, 400), ("POST", "/v1/registry", {"proof": True}, 405),
    ("POST", "/v1/registry/reconcile?dry_run=1", {"proof": True}, 405)])
def test_no_caller_path_native_probe_or_proof_is_accepted(inspection, monkeypatch, method, path, body, expected):
    prohibit_effects(inspection, monkeypatch)
    assert request(inspection.address, method, path, body)[0] == expected


def test_unconfigured_or_unserializable_inspection_has_explicit_error(inspection, monkeypatch):
    registry = inspection.scheduler.registry
    inspection.scheduler.registry = None
    assert request(inspection.address, "GET", "/v1/registry") == (503, {"error": "registry_not_configured"})
    inspection.scheduler.registry = registry
    monkeypatch.setattr(registry, "queue_snapshot", lambda: {"invalid": float("nan")})
    assert request(inspection.address, "GET", "/v1/registry") == (503, {"error": "registry_unavailable"})


def test_source_observation_proceeds_while_read_projection_holds_action_lock(inspection, monkeypatch):
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    def precheck():
        entered.set()
        assert release.wait(3), "test did not release inspection barrier"
        return []
    inspection.queue.enqueue(lambda data: data, description={}, precheck=lambda: [])
    # The precheck belongs to the seeded job; real snapshot executes it under
    # action_lock. A producer must still update the independently locked quiet state.
    inspection.queue._pending[0].precheck = precheck
    prohibit_effects(inspection, monkeypatch)
    result = []
    consumer = threading.Thread(target=lambda: result.append(request(inspection.address, "GET", "/v1/registry")))
    def observe():
        inspection.quiet.observe(1)
        observed.set()
    producer = threading.Thread(target=observe)
    consumer.start()
    try:
        assert entered.wait(3)
        producer.start()
        assert observed.wait(3), "source observation waited for the scheduler action lock"
    finally:
        release.set()
        consumer.join(4)
        if producer.ident is not None: producer.join(3)
    assert result and result[0][0] == 200
    assert {"reason": "in_flight"} in result[0][1]["blocked_by"]
