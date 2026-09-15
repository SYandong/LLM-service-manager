# Generated-By: OpenCode / deepseek-v4.1-flash
"""Directory reconciler: one submission per tick, backoff, orphans and annotate."""

import threading
from types import SimpleNamespace

import pytest

from llmsvc.reconcile import DirectoryReconciler
from llmsvc.registry import RegistryError
from llmsvc.state import Activity, MemoryState, ModelState, StateSnapshot


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def snapshot(models, *, sampled_at=1000.0, errors=(), read_only=False):
    return StateSnapshot(
        sampled_at=sampled_at, read_only=read_only,
        models=tuple(models),
        activity=tuple(Activity(model.name, in_flight=0) for model in models),
        memory=MemoryState(500, 0))


class FakeQueue:
    def __init__(self):
        self.fenced = False
        self.jobs = []

    def queue_snapshot(self):
        return {"jobs": list(self.jobs), "fenced": self.fenced}


class FakeRegistry:
    """Only the surface the reconciler reads or submits through."""

    def __init__(self, *, rows=(), records=None, add_error=None, stop_error=None, discover=object()):
        self.rows = [dict(row) for row in rows]
        self.record_map = dict(records or {})
        self.add_error = add_error
        self.stop_error = stop_error
        self.discover = discover
        self.add_calls = []
        self.remove_calls = []
        self.queue = FakeQueue()
        self.submit_change = lambda *args, **kwargs: None

    def discovered(self, configured_names=()):
        return [dict(row) for row in self.rows]

    def records(self):
        return dict(self.record_map)

    def inventory(self, include_records=False):
        names = {row["name"] for row in self.rows if row["status"] == "configured"}
        return {"models": [{"name": name} for name in sorted(names)]}

    def add(self, body, *, dry_run=False):
        self.add_calls.append(dict(body))
        if self.add_error is not None:
            raise self.add_error
        return {"id": "job", "status": "queued"}

    def remove(self, name, *, dry_run=False):
        self.remove_calls.append(name)
        if self.stop_error is not None:
            raise self.stop_error
        return {"id": "job", "status": "queued"}


class FakeCatalog:
    """Like CatalogRuntime, submit_change is a *method*: connect_registry hands the
    registry a bound method, and every later attribute access yields a new bound
    object, so the reconciler must compare them by equality, never identity."""
    def __init__(self):
        self.enabled = True
        self.busy = False

    def submit_change(self, transform, **options):
        raise AssertionError("fakes never submit through the catalog")

    def connect_registry(self, registry):
        registry.submit_change = self.submit_change

    def can_submit(self):
        return self.enabled


class FakeStore:
    """Like IntentStore, catalog_pending() answers with a boolean."""
    def __init__(self):
        self.pending = False

    def catalog_pending(self):
        return self.pending


class FakeActions:
    def __init__(self, outcome=None):
        self.pending = set()
        self.calls = []
        self.outcome = outcome if outcome is not None else {"model": "x", "status": "ready", "error": None}

    def stop_model(self, name, *, by="reconcile", **kwargs):
        self.calls.append((name, by))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeScheduler:
    def __init__(self, snap, *, actions=None):
        self.action_lock = threading.RLock()
        self.config = SimpleNamespace(max_snapshot_age_seconds=30)
        self.catalog_fenced = False
        self._snapshot = snap
        self.model_actions = actions
        self.store = FakeStore()
        self.events = []
        # Wall clock, as Scheduler.clock; the reconciler's own clock is monotonic.
        self.clock = lambda: 1000.0

    def snapshot(self):
        return self._snapshot

    def emit(self, kind, *, model=None, detail=None):
        self.events.append({"kind": kind, "model": model, "detail": detail})

    def connect(self, registry):
        self.catalog = FakeCatalog()
        self.catalog.connect_registry(registry)
        return self.catalog


def build(*, rows=(), records=None, snap=None, actions=None, interval=30.0, **kwargs):
    registry = FakeRegistry(rows=rows, records=records, **kwargs)
    scheduler = FakeScheduler(snap or snapshot(()), actions=actions)
    catalog = scheduler.connect(registry)
    clock = Clock(1000.0)
    reconciler = DirectoryReconciler(scheduler, registry, interval_seconds=interval, clock=clock)
    return SimpleNamespace(reconciler=reconciler, registry=registry, scheduler=scheduler,
                           catalog=catalog, clock=clock)


def row(name, status="pending", path=None, reason=None):
    return {"name": name, "path": path or "/srv/models/" + name, "base": "base",
            "util": None, "weights_gb": None, "status": status, "reason": reason}


def record(name, path="/srv/models/" + "gone"):
    return {"name": name, "path": path, "base": "base", "util": 0.4, "weights_gb": 10.0,
            "created_at": 1.0, "daemon_port": 8101}


def test_pending_candidate_is_submitted_once_and_then_backs_off():
    world = build(rows=[row("cand")])
    first = world.reconciler.run_once()
    assert first == {"action": "add", "model": "cand", "reason": None}
    assert world.registry.add_calls == [{"import": "cand"}]
    # Inside both the interval and the backoff nothing new is submitted.
    world.clock.advance(35)
    assert world.reconciler.run_once()["action"] is None
    assert world.registry.add_calls == [{"import": "cand"}]
    # Past the first 60 s backoff the same candidate is retried.
    world.clock.advance(30)
    assert world.reconciler.run_once()["action"] == "add"
    assert len(world.registry.add_calls) == 2


def test_second_candidate_waits_for_the_interval():
    world = build(rows=[row("first"), row("second")], interval=30.0)
    assert world.reconciler.run_once()["model"] == "first"
    world.clock.advance(10)
    assert world.reconciler.run_once()["reason"] == "interval"
    assert world.registry.add_calls == [{"import": "first"}]
    world.clock.advance(25)
    assert world.reconciler.run_once()["model"] == "second"
    assert [call["import"] for call in world.registry.add_calls] == ["first", "second"]


def test_add_failure_is_recorded_and_surfaced_through_annotate():
    world = build(rows=[row("cand")], add_error=RegistryError("boom"))
    result = world.reconciler.run_once()
    assert result == {"action": None, "model": "cand", "reason": "RegistryError: boom"}
    annotated = world.reconciler.annotate(world.registry.discovered())
    assert annotated[0]["reason"] == "RegistryError: boom"


def test_backoff_doubles_and_caps():
    world = build(rows=[row("cand")], add_error=RegistryError("boom"))
    delays = []
    for _ in range(8):
        world.reconciler.run_once()
        entry = world.reconciler._backoff["cand"]
        delays.append(entry["until"] - world.clock())
        world.clock.advance(entry["until"] - world.clock() + 1)
    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_orphan_with_stopped_state_is_removed(tmp_path):
    descriptor = tmp_path / "gone"
    descriptor.mkdir()
    snap = snapshot([ModelState("gone", state="stopped")])
    world = build(records={"gone": record("gone", str(descriptor))}, snap=snap)
    result = world.reconciler.run_once()
    assert result == {"action": "remove", "model": "gone", "reason": None}
    assert world.registry.remove_calls == ["gone"]


def test_orphan_that_is_awake_is_stopped_but_not_removed(tmp_path):
    descriptor = tmp_path / "gone"
    descriptor.mkdir()
    actions = FakeActions()
    snap = snapshot([ModelState("gone", state="awake", weights_gb=10.0)])
    world = build(records={"gone": record("gone", str(descriptor))}, snap=snap, actions=actions)
    result = world.reconciler.run_once()
    assert result == {"action": "stop", "model": "gone", "reason": None}
    assert actions.calls == [("gone", "reconcile")]
    assert world.registry.remove_calls == []


def test_blocked_stop_records_the_reason(tmp_path):
    descriptor = tmp_path / "gone"
    descriptor.mkdir()
    actions = FakeActions(outcome={"model": "gone", "status": "blocked", "error": "pinned_until"})
    snap = snapshot([ModelState("gone", state="sleeping", weights_gb=10.0)])
    world = build(records={"gone": record("gone", str(descriptor))}, snap=snap, actions=actions)
    result = world.reconciler.run_once()
    assert result == {"action": "stop", "model": "gone", "reason": "pinned_until"}


def test_orphan_with_unknown_state_or_stale_snapshot_does_nothing(tmp_path):
    descriptor = tmp_path / "gone"
    descriptor.mkdir()
    unknown = build(records={"gone": record("gone", str(descriptor))},
                    snap=snapshot([ModelState("gone", state="unknown")]))
    assert unknown.reconciler.run_once()["action"] is None
    assert unknown.registry.remove_calls == []
    stale = build(records={"gone": record("gone", str(descriptor))},
                  snap=snapshot([ModelState("gone", state="stopped")], sampled_at=900.0))
    assert stale.reconciler.run_once()["action"] is None
    assert stale.registry.remove_calls == []
    missing = build(records={"gone": record("gone", str(descriptor))}, snap=snapshot([]))
    assert missing.reconciler.run_once()["reason"] == "unknown_model_state"
    assert missing.registry.remove_calls == []


def test_hand_configured_model_without_a_record_is_never_touched():
    world = build(rows=[row("base", status="configured")])
    assert world.reconciler.run_once()["action"] is None
    assert world.registry.remove_calls == [] and world.registry.add_calls == []


@pytest.mark.parametrize("change,reason", [
    ("catalog_fenced", "catalog_reconciliation_required"),
    ("catalog_pending", "catalog_pending"),
    ("can_submit", "catalog_unavailable"),
    ("queue_fenced", "registry_reconciliation_required"),
    ("pending_job", "pending_change"),
    ("catalog_busy", "catalog_busy"),
    ("discover", "discovery_unconfigured"),
])
def test_precondition_blocks_every_submission(change, reason):
    world = build(rows=[row("cand")], records={"gone": record("gone")})
    if change == "catalog_fenced":
        world.scheduler.catalog_fenced = True
    elif change == "catalog_pending":
        world.scheduler.store.pending = {"phase": "claimed"}
    elif change == "can_submit":
        world.catalog.enabled = False
    elif change == "queue_fenced":
        world.registry.queue.fenced = True
    elif change == "pending_job":
        world.registry.queue.jobs = [{"id": "j", "pending": True}]
    elif change == "catalog_busy":
        world.catalog.busy = True
    else:
        world.registry.discover = None
    result = world.reconciler.run_once()
    assert result["action"] is None and result["reason"] == reason
    assert world.registry.add_calls == [] and world.registry.remove_calls == []


def test_disconnected_submit_change_is_rejected():
    world = build(rows=[row("cand")])
    world.catalog.submit_change = object()
    assert world.reconciler.run_once()["reason"] == "catalog_not_connected"


def test_annotate_marks_submitted_candidate_and_adds_orphan_rows(tmp_path):
    descriptor = tmp_path / "gone"
    descriptor.mkdir()
    snap = snapshot([ModelState("gone", state="stopped")])
    world = build(rows=[row("cand")], records={"gone": record("gone", str(descriptor))}, snap=snap)
    world.reconciler.run_once()
    annotated = world.reconciler.annotate(world.registry.discovered())
    assert annotated[0]["reason"] == "submitted; waiting for the configuration transaction"
    orphan = next(item for item in annotated if item["status"] == "orphaned")
    assert orphan == {"name": "gone", "path": str(descriptor), "base": "base", "util": 0.4,
                      "weights_gb": 10.0, "status": "orphaned",
                      "reason": "the descriptor llmsvc.json is missing; it will be unregistered"}


def test_invalid_descriptor_keeps_the_model_registered(tmp_path):
    """A typo while editing llmsvc.json must never unregister a running model."""
    directory = tmp_path / "kept"
    directory.mkdir()
    (directory / "llmsvc.json").write_text("{not json")
    snap = snapshot([ModelState("kept", state="stopped")])
    world = build(rows=[row("kept", status="invalid", path=str(directory), reason="llmsvc.json must contain a JSON object")],
                  records={"kept": record("kept", str(directory))}, snap=snap)
    assert world.reconciler.run_once() == {"action": None, "model": None, "reason": None}
    assert world.registry.remove_calls == []
    assert [r["status"] for r in world.reconciler.annotate(world.registry.discovered())] == ["invalid"]


def test_descriptor_that_now_names_another_model_is_an_orphan(tmp_path):
    directory = tmp_path / "renamed"
    directory.mkdir()
    (directory / "llmsvc.json").write_text('{"base": "base", "name": "new-name"}')
    snap = snapshot([ModelState("old-name", state="stopped")], sampled_at=1000.0)
    world = build(rows=[row("new-name", status="pending", path=str(directory))],
                  records={"old-name": record("old-name", str(directory))}, snap=snap)
    # The pending new name is registered first; the old record is unregistered on a later tick.
    assert world.reconciler.run_once()["action"] == "add"
    world.clock.advance(31)
    world.registry.rows = [row("new-name", status="configured", path=str(directory))]
    result = world.reconciler.run_once()
    assert result == {"action": "remove", "model": "old-name", "reason": None}


def test_shared_roots_are_scanned_at_most_once_per_interval():
    world = build(rows=[], interval=30.0)
    assert world.reconciler.run_once() == {"action": None, "model": None, "reason": None}
    assert world.reconciler.run_once()["reason"] == "interval"
    world.clock.advance(30)
    assert world.reconciler.run_once() == {"action": None, "model": None, "reason": None}


def test_snapshot_freshness_uses_the_scheduler_wall_clock(tmp_path):
    """The reconciler's clock is monotonic; sampled_at is wall time (Scheduler.clock)."""
    directory = tmp_path / "gone"
    directory.mkdir()
    world = build(records={"gone": record("gone", str(directory))},
                  snap=snapshot([ModelState("gone", state="stopped")], sampled_at=5000.0))
    world.scheduler.clock = lambda: 5010.0  # fresh by wall clock, hopelessly stale by monotonic 1000
    assert world.reconciler.run_once() == {"action": "remove", "model": "gone", "reason": None}


def test_catalog_pending_boolean_blocks_only_when_true():
    world = build(rows=[row("cand")])
    world.scheduler.store.pending = True
    assert world.reconciler.run_once()["reason"] == "catalog_pending"
    world.scheduler.store.pending = False
    assert world.reconciler.run_once()["action"] == "add"
