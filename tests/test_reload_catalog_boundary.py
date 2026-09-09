# Generated-By: Codex / gpt-6-astra
"""Existing queue seam for core157; no real runtime catalog installation."""
import hashlib
import json
import threading

import pytest

from test_reload import add, harness, make_quiet


@pytest.mark.parametrize("fault", [None, "adoption", "cleanup", "publication"])
def test_catalog_callback_composition_stays_under_lock_and_marker(harness, fault):
    queue, quiet, clock, _, _ = harness
    trace, deadlines = [], []
    # These are fixture values, not trusted profiles, measured budgets or an
    # implementation of the core/telemetry runtime catalog lifecycle.
    catalog = {"epoch": "old", "retained_resource_metadata": ("removed",)}
    original = queue.path.read_bytes()

    def assert_boundary():
        assert queue.marker.exists()
        record = json.loads(queue.marker.read_bytes())
        assert record["sha256"] == hashlib.sha256(queue.path.read_bytes()).hexdigest()
        assert queue.path.read_bytes() != original
        assert queue.get(job["id"])["status"] != "applied"

    def adopted(*, deadline):
        trace.append("adoption")
        deadlines.append(deadline)
        assert_boundary()
        if fault == "adoption":
            raise RuntimeError("fixture settlement unknown")

    def cleanup_then_publish(*, deadline):
        deadlines.append(deadline)
        trace.append("cleanup")
        assert_boundary()
        if fault == "cleanup":
            raise RuntimeError("fixture removed unit absence unknown")
        # A separate thread deterministically tries the lock once; the fixture
        # clock never advances with wall-clock scheduling or thread completion.
        acquired = []
        def contender():
            locked = queue.action_lock.acquire(blocking=False)
            acquired.append(locked)
            if locked:
                queue.action_lock.release()
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive() and acquired == [False]
        trace.append("publication")
        catalog["epoch"] = "new"
        assert_boundary()  # Publication itself has not released the marker.
        if fault == "publication":
            raise RuntimeError("fixture publication acknowledgement failed")

    queue.notify_reload = adopted
    job = add(queue, after_apply=cleanup_then_publish)
    assert queue.process_once()["status"] == "queued"
    assert trace == [] and catalog["epoch"] == "old"
    make_quiet(quiet, clock)  # Explicit synthetic quiet, no source certification.
    result = queue.process_once()
    assert catalog["retained_resource_metadata"] == ("removed",)
    if fault is None:
        assert trace == ["adoption", "cleanup", "publication"]
        assert catalog["epoch"] == "new" and result["status"] == "applied"
        assert not queue.marker.exists()
    else:
        assert result["status"] == "reconciliation_required" and result["config_committed"]
        assert queue.marker.exists() and queue.queue_snapshot()["fenced"]
        assert queue.process_once() is None
        assert catalog["epoch"] == ("new" if fault == "publication" else "old")
        assert trace == {"adoption": ["adoption"], "cleanup": ["adoption", "cleanup"],
                         "publication": ["adoption", "cleanup", "publication"]}[fault]
    assert len(set(deadlines)) == 1
