# Generated-By: Codex / gpt-6-astra
"""Offline lease durability, admission, deadlines and real HTTP reentry."""

import http.client
import json
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.leases import LeaseError, LeaseUnitProbe, PlacementController, UnitObservation
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, Pin, Reserve, StateSnapshot
from llmsvc.store import IntentStore


def request(address, path, body=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request("POST", path, json.dumps(body or {}))
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def system(tmp_path):
    models = {name: ModelState(name, state="stopped", unit="vllm-"+name+".service", unit_active=False,
                              weights_gb=10, util=0.6, cold_start_seconds=1) for name in ("a", "b")}
    state = {"models": models, "observations": {}, "errors": (), "memory": 500, "probes": [], "collected": threading.Event()}
    def collect():
        state["collected"].set()
        return StateSnapshot(sampled_at=time.time(), gpus=(GPUState(0, total_gb=100, free_gb=100, external_gb=0),),
            models=tuple(models.values()), errors=state["errors"], memory=MemoryState(state["memory"], 0),
            activity=tuple(Activity(name, time.time()-1000, 0, 0, 0) for name in models))
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, placement_enabled=True,
        # Positive durable grants are functional tests, not an 80ms host/IO
        # benchmark. Negative waits opt into their short deadline explicitly.
        state_db_path=str(tmp_path / "state.sqlite"), placement_wait_seconds=2.0,
        action_poll_seconds=0.005, request_timeout_seconds=0.3, lease_probe_seconds=0.03)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    scheduler = Scheduler(config, collect, store=store)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:1", models={name: {"util": 0.6, "weights_gb": 10} for name in models},
        systemctl="fake-systemctl", run=lambda *a, **k: pytest.fail("no actuator invocation"))
    def probe(model, *, deadline):
        state["probes"].append(model)
        return state["observations"].get(model, UnitObservation(False, True))
    scheduler.placement = PlacementController(scheduler, transport, probe=probe)
    scheduler.sample_once()
    try:
        yield scheduler, state, transport
    finally:
        scheduler.stop()
        store.close()


def grant(scheduler, model="a", util=0.6):
    return scheduler.placement.place({"model": model, "util": util})


def ready(scheduler, state, lease_id):
    lease, unit = scheduler.store.lease(lease_id)
    state["models"][lease.model] = replace(state["models"][lease.model], state="awake", unit_active=True,
        health_ok=True, is_sleeping=False, swap_state="ready", gpu=lease.gpu, budget_gb=lease.budget_gb)
    state["observations"][lease.model] = UnitObservation(True, False, True, lease_id)


def test_unique_model_accounting_blocks_concurrent_double_allocation(system):
    scheduler, _, _ = system
    def place(name):
        try:
            return grant(scheduler, name)
        except LeaseError as exc:
            return exc
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(place, ("a", "b")))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(lease.budget_gb for lease, _ in scheduler.store.leases()) == 60
    with pytest.raises(LeaseError, match="outstanding_lease"):
        grant(scheduler, scheduler.store.leases()[0][0].model)


def test_same_model_atomic_database_constraint_and_confirm_promotion(system):
    scheduler, state, _ = system
    result = grant(scheduler)
    lease, unit = scheduler.store.lease(result["lease_id"])
    with pytest.raises(sqlite3.IntegrityError):
        scheduler.store.create_lease(replace(lease, lease_id="duplicate"), unit)
    ready(scheduler, state, lease.lease_id)
    for _ in range(2):
        assert scheduler.placement.finish("confirm", lease.lease_id)["status"] == "confirmed"
    assert len(scheduler.store.leases()) == 1
    assert scheduler.snapshot().models[0].budget_gb == 60
    # 40 GiB fits beside one confirmed 60 GiB daemon: no double debit.
    scheduler.placement.transport.models["b"]["util"] = 0.4
    assert grant(scheduler, "b", 0.4)["gpu"] == 0


def test_release_needs_exit_and_tombstone_fences_old_confirm(system):
    scheduler, state, _ = system
    first = grant(scheduler)["lease_id"]
    state["observations"]["a"] = UnitObservation(True, False)
    with pytest.raises(LeaseError, match="lease_exit_unconfirmed"):
        scheduler.placement.finish("release", first)
    assert scheduler.store.lease(first)[0].status == "pending"
    state["observations"]["a"] = UnitObservation(False, True)
    assert scheduler.placement.finish("release", first)["status"] == "released"
    assert scheduler.placement.finish("release", first)["status"] == "released"
    second = grant(scheduler)["lease_id"]
    assert second != first
    with pytest.raises(LeaseError) as caught:
        scheduler.placement.finish("confirm", first)
    assert caught.value.status == 409
    assert len(scheduler.store.leases()) == 1


@pytest.mark.parametrize("observation", [UnitObservation(), UnitObservation(True, False, True, "wrong-lease")])
def test_uncertain_confirmation_is_503_not_launcher_stop_conflict(system, observation):
    scheduler, state, _ = system
    lease_id = grant(scheduler)["lease_id"]
    ready(scheduler, state, lease_id)
    state["observations"]["a"] = observation
    with pytest.raises(LeaseError) as caught:
        scheduler.placement.finish("confirm", lease_id)
    assert caught.value.status == 503
    assert scheduler.store.lease(lease_id)[0].status == "pending"


@pytest.mark.parametrize("pause", [0.12, 0.4])
def test_functional_grant_survives_predecision_delay_then_keeps_unknown_confirm(system, pause):
    scheduler, state, _ = system
    clock = [1000.0]
    scheduler.placement.monotonic = lambda: clock[0]
    reconcile = scheduler.placement.reconcile
    timeline = []

    def delayed_reconcile(*, deadline=None):
        result = reconcile(deadline=deadline)
        if not timeline:
            # Simulate scheduling/administrative delay before decision/proof,
            # not a late unit observation or a real sleep on the test host.
            timeline.append({"phase": "before_decision", "deadline": deadline,
                             "now": clock[0], "leases": len(scheduler.store.leases()),
                             "errors": scheduler.snapshot().errors})
            clock[0] += pause
        return result

    scheduler.placement.reconcile = delayed_reconcile
    try:
        lease_id = grant(scheduler)["lease_id"]
    except LeaseError as exc:
        pytest.fail(f"functional grant failed before confirmation: {exc.error}; "
                    f"timeline={timeline}; now={clock[0]}; blockers={exc.blockers}; "
                    f"probes={state['probes']}; leases={scheduler.store.leases()}")
    assert timeline[0]["errors"] == () and timeline[0]["leases"] == 0
    assert state["probes"] == ["a"]
    ready(scheduler, state, lease_id)
    state["observations"]["a"] = UnitObservation()
    with pytest.raises(LeaseError) as caught:
        scheduler.placement.finish("confirm", lease_id)
    assert caught.value.status == 503
    assert scheduler.store.lease(lease_id)[0].status == "pending"
    assert scheduler.store.lease(lease_id)[0].budget_gb == 60


def test_expiry_retains_loading_then_auto_confirms_or_releases(system):
    scheduler, state, _ = system
    lease_id = grant(scheduler)["lease_id"]
    lease, _ = scheduler.store.lease(lease_id)
    scheduler.clock = lambda: lease.expires_at + 1
    scheduler.config = replace(scheduler.config, max_snapshot_age_seconds=1000)
    state["observations"]["a"] = UnitObservation(True, False, True, lease_id)
    scheduler.placement.reconcile()
    assert scheduler.store.lease(lease_id)[0].status == "stale"
    assert scheduler.snapshot().leases[0].budget_gb == 60
    ready(scheduler, state, lease_id)
    scheduler.sample_once()
    scheduler.placement.reconcile()
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"
    assert scheduler.placement.finish("confirm", lease_id)["status"] == "confirmed"
    state["models"]["a"] = replace(state["models"]["a"], state="stopped", unit_active=False)
    scheduler.sample_once()
    assert "daemon_exit_unconfirmed:a" in scheduler.snapshot().errors
    scheduler.placement.reconcile()
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"
    state["observations"]["a"] = UnitObservation(False, True)
    scheduler.placement.reconcile()
    assert not scheduler.snapshot().leases


@pytest.mark.parametrize("mode,expected", [("ready", "confirmed"), ("loading", "stale"), ("absent", "released")])
def test_restart_reconciles_persisted_accounts_and_keeps_pin(system, mode, expected):
    scheduler, state, transport = system
    lease_id = grant(scheduler)["lease_id"]
    scheduler.store.put_pin(Pin("a", time.time()+2000, "owner"))
    if mode == "ready":
        ready(scheduler, state, lease_id)
    elif mode == "loading":
        state["observations"]["a"] = UnitObservation(True, False, True, lease_id)
    scheduler.store.close()
    scheduler.store = IntentStore(scheduler.config.state_db_path, action_lock=scheduler.action_lock)
    reopened = scheduler.store
    try:
        probe = scheduler.placement.probe
        scheduler.placement = PlacementController(scheduler, transport, probe=probe)
        scheduler.sample_once()
        scheduler.placement.reconcile()
        assert scheduler.store.lease(lease_id)[0].status == expected
        assert scheduler.snapshot().pins[0].model == "a"
    finally:
        reopened.close()


def test_wait_releases_lock_for_confirm_release_and_never_resets_deadline(system):
    scheduler, state, _ = system
    first = grant(scheduler)["lease_id"]
    entered = threading.Event()
    original_wait = scheduler.changed.wait
    def wait(timeout=None):
        entered.set()
        return original_wait(timeout)
    scheduler.changed.wait = wait
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(grant, scheduler, "b")
        assert entered.wait(2)
        ready(scheduler, state, first)
        assert scheduler.placement.finish("confirm", first)["status"] == "confirmed"
        state["models"]["a"] = replace(state["models"]["a"], state="stopped", unit_active=False)
        state["observations"]["a"] = UnitObservation(False, True)
        assert scheduler.placement.finish("release", first)["status"] == "released"
        assert waiting.result(timeout=3)["gpu"] == 0
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.05)
    stop = threading.Event()
    def notify():
        while not stop.wait(0.001):
            with scheduler.changed:
                scheduler.changed.notify_all()
    thread = threading.Thread(target=notify)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(LeaseError, match="placement_timeout"):
            grant(scheduler, "a")
        assert time.monotonic() - started < 0.25
    finally:
        stop.set()
        thread.join()


@pytest.mark.parametrize("condition,reason", [("reserve", "reserved"), ("ram", "unknown_memory"),
    ("unit", "unit_exists"), ("unknown-unit", "unit_state_unknown"), ("errors", "unknown_or_stale_snapshot")])
def test_blockers_do_not_actuate_or_allocate(system, condition, reason):
    scheduler, state, _ = system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08)
    if condition == "reserve":
        scheduler.store.put_reserve(Reserve("held", 0, 1, time.time()+100, "owner"))
    elif condition == "ram":
        state["memory"] = None
    elif condition == "unit":
        state["observations"]["a"] = UnitObservation(True, True)
    elif condition == "unknown-unit":
        state["observations"]["a"] = UnitObservation()
    else:
        state["errors"] = ("source unavailable",)
    scheduler.sample_once()
    with pytest.raises(LeaseError) as caught:
        grant(scheduler)
    assert reason in [b.reason for b in caught.value.blockers]
    assert not scheduler.store.leases()


def test_dry_run_never_probes_collects_allocates_or_writes(system):
    scheduler, state, _ = system
    before = open(scheduler.config.state_db_path, "rb").read()
    scheduler.collect = lambda: pytest.fail("dry-run collected")
    scheduler.placement.probe = lambda *a, **k: pytest.fail("dry-run inspected unit")
    scheduler.config = replace(scheduler.config, read_only=True)
    result = scheduler.preview("place", {"model": "a", "util": 0.6})
    assert result["would"][0]["kind"] == "place"
    assert "lease_id" not in json.dumps(result)
    assert not scheduler.store.leases()
    assert open(scheduler.config.state_db_path, "rb").read() == before


def test_http_routes_mount_lease_protocol_and_pin_optin_does_not_enable_it(system):
    scheduler, state, _ = system
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        scheduler.config = replace(scheduler.config, placement_enabled=False)
        assert request(server.server_address, "/v1/place", {"model": "a", "util": 0.6})[0] == 405
        scheduler.config = replace(scheduler.config, placement_enabled=True)
        assert request(server.server_address, "/v1/place", {"model": "a", "util": 0.6, "is_default": False})[0] == 400
        status, result = request(server.server_address, "/v1/place", {"model": "a", "util": 0.6})
        assert status == 200
        path = "/v1/place/"+result["lease_id"]
        assert request(server.server_address, path+"/confirm", {"lease_id": "spoofed"})[0] == 400
        assert request(server.server_address, path+"/confirm")[0] == 503
        assert request(server.server_address, path+"/release")[1]["status"] == "released"
        assert request(server.server_address, path+"/confirm")[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_wake_reenters_mounted_place_and_confirm_endpoints(system):
    scheduler, state, _ = system
    core = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    results = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            status, body = request(core.server_address, "/v1/place", {"model": "a", "util": 0.6})
            results.append((status, body))
            if status == 200:
                ready(scheduler, state, body["lease_id"])
                results.append(request(core.server_address, "/v1/place/"+body["lease_id"]+"/confirm"))
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    scheduler.config = replace(scheduler.config, model_actions_enabled=True, wake_timeout_seconds=1)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:"+str(upstream.server_port),
        models=scheduler.placement.transport.models, systemctl="fake-systemctl")
    scheduler.model_actions = ModelActionController(scheduler, transport)
    threads = [threading.Thread(target=lambda: core.serve_forever(poll_interval=0.01)),
               threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))]
    for thread in threads:
        thread.start()
    try:
        status, result = request(core.server_address, "/v1/wake/a")
        assert status == 200 and result["ready"] is True
        assert [row[0] for row in results] == [200, 200]
        assert len(scheduler.store.leases()) == 1
        assert scheduler.store.leases()[0][0].status == "confirmed"
    finally:
        core.shutdown()
        upstream.shutdown()
        core.server_close()
        upstream.server_close()
        for thread in threads:
            thread.join()


@pytest.mark.parametrize("text,exited", [
    ("LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n", True),
    ("LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=/leftover\n", False),
    ("LoadState=loaded\nActiveState=active\nMainPID=123\nControlGroup=/own\n", False),
    ("", False), ("LoadState=loaded\nLoadState=not-found\n", False)])
def test_unit_exit_probe_does_not_equate_inactive_or_mainpid_zero_with_exit(system, text, exited):
    _, _, transport = system
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=text)
    transport.run = run
    assert LeaseUnitProbe(transport)("a", deadline=time.monotonic()+0.1).exited is exited
    assert calls[0][0][:3] == ["fake-systemctl", "show", "vllm-a.service"]
    assert 0 < calls[0][1]["timeout"] <= 0.1


def test_real_fake_process_timeout_is_bounded_and_unknown(system, tmp_path):
    _, _, transport = system
    script = tmp_path/"fake-systemctl"
    script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n")
    script.chmod(0o700)
    transport.systemctl = str(script)
    transport.run = subprocess.run
    start = time.monotonic()
    assert LeaseUnitProbe(transport)("a", deadline=start+0.03) == UnitObservation()
    assert time.monotonic()-start < 0.3


def test_readonly_version_one_does_not_migrate_and_writable_upgrade_preserves_pin(tmp_path):
    path = tmp_path/"old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE llmsvc_pins(model TEXT PRIMARY KEY, until REAL, owner TEXT); CREATE TABLE llmsvc_reserves(id TEXT, gpu INTEGER, size_gb REAL, until REAL, owner TEXT); PRAGMA user_version=1;")
        db.execute("INSERT INTO llmsvc_pins VALUES('a',9999999999,'owner')")
    before = path.read_bytes()
    store = IntentStore(path, action_lock=threading.RLock(), read_only=True)
    assert store.leases() == ()
    store.close()
    assert path.read_bytes() == before
    store = IntentStore(path, action_lock=threading.RLock())
    assert store.active(time.time())[0][0].model == "a"
    store.close()


def test_protected_or_eviction_required_residents_are_never_stopped(system):
    scheduler, state, _ = system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08)
    state["models"]["a"] = replace(state["models"]["a"], state="awake", unit_active=True,
        health_ok=True, is_sleeping=False, gpu=0, budget_gb=80, resident_gb=80)
    scheduler.sample_once()
    with pytest.raises(LeaseError) as caught:
        grant(scheduler, "b")
    assert "eviction_required" in [b.reason for b in caught.value.blockers]
    scheduler.store.put_pin(Pin("a", time.time()+100, "owner"))
    with pytest.raises(LeaseError) as caught:
        grant(scheduler, "b")
    assert "pinned_until" in [b.reason for b in caught.value.blockers]
    assert not scheduler.store.leases()
    assert "a" not in state["probes"]


def test_wait_only_consumes_published_samples_and_enforces_same_deadline(system):
    scheduler, _, _ = system
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08)
    scheduler.store.put_reserve(Reserve("held", 0, 100, time.time()+100, "owner"))
    scheduler.collect = lambda: pytest.fail("place must not start a probe beyond its deadline")
    started = time.monotonic()
    with pytest.raises(LeaseError, match="placement_timeout"):
        grant(scheduler)
    assert time.monotonic()-started < 0.25


def test_reconcile_does_not_apply_old_probe_after_newer_publication(system):
    scheduler, state, _ = system
    lease_id = grant(scheduler)["lease_id"]
    ready(scheduler, state, lease_id)
    scheduler.sample_once()
    scheduler.placement.finish("confirm", lease_id)
    state["models"]["a"] = replace(state["models"]["a"], state="stopped", unit_active=False)
    scheduler.sample_once()
    def stale_probe(model, *, deadline):
        ready(scheduler, state, lease_id)
        # Publish a newer round directly to avoid recursively invoking this probe.
        with scheduler.changed:
            scheduler._sample_published += 1
            scheduler._snapshot = scheduler.collect()
            scheduler.changed.notify_all()
        return UnitObservation(False, True)
    scheduler.placement.probe = stale_probe
    scheduler.placement.reconcile()
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"


def test_pending_cold_starts_reserve_host_memory_and_configured_budget_floor(system):
    scheduler, state, transport = system
    # Util .2 from the HTTP body cannot undercharge the configured .6 minimum.
    lease_id = grant(scheduler, util=0.2)["lease_id"]
    assert scheduler.store.lease(lease_id)[0].budget_gb == 60
    transport.models["b"]["util"] = 0.2
    state["memory"] = 165  # Two 10 GiB cold starts would breach the 150 floor.
    scheduler.sample_once()
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08)
    with pytest.raises(LeaseError) as caught:
        grant(scheduler, "b", util=0.2)
    assert "host_memory_floor" in [b.reason for b in caught.value.blockers]


@pytest.mark.parametrize("option,value", [("placement_enabled", 1), ("placement_wait_seconds", 121),
    ("lease_timeout_seconds", float("nan")), ("lease_probe_seconds", 0)])
def test_placement_config_validates_boolean_and_bounded_positive_limits(option, value):
    with pytest.raises(ValueError):
        SchedulerConfig("127.0.0.1", 8011, **{option: value})


def test_free_observations_reconcile_confirmed_account_before_budget_reuse(system):
    scheduler, state, transport = system
    lease_id = grant(scheduler)["lease_id"]
    ready(scheduler, state, lease_id)
    state["models"]["a"] = replace(state["models"]["a"], state="sleeping", is_sleeping=True)
    scheduler.sample_once()
    assert scheduler.store.lease(lease_id)[0].status == "confirmed"
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv == ["fake-systemctl", "stop", "vllm-a.service"]
        state["models"]["a"] = replace(state["models"]["a"], state="stopped", unit_active=False)
        state["observations"]["a"] = UnitObservation(False, True)
        state["memory"] += 10
        return SimpleNamespace(returncode=0)
    transport.run = run
    scheduler.config = replace(scheduler.config, model_actions_enabled=True, free_timeout_seconds=0.3)
    scheduler.model_actions = ModelActionController(scheduler, transport)
    result = scheduler.model_actions.free({"ram": True, "need_gb": 10}, by="test")
    assert result["status"] == "complete" and result["stopped"] == ["a"] and result["freed_gb"] == 10
    assert scheduler.store.lease(lease_id)[0].status == "released"
    assert grant(scheduler, "b")["gpu"] == 0
    assert len(calls) == 1


def test_probe_contradictory_not_found_is_not_exit_proof(system):
    _, _, transport = system
    transport.run = lambda *a, **k: SimpleNamespace(returncode=0,
        stdout="LoadState=not-found\nActiveState=active\nMainPID=42\nControlGroup=/leftover\n")
    assert LeaseUnitProbe(transport)("a", deadline=time.monotonic()+0.1) == UnitObservation()


def test_reconciliation_rotates_after_a_slow_first_unit(system):
    scheduler, _, _ = system
    grant(scheduler)
    scheduler.store.create_lease(Lease("second", "b", 0, 0.1, time.time()+100, 10), "vllm-b.service")
    calls = []
    def probe(model, *, deadline):
        calls.append(model)
        while time.monotonic() < deadline:
            time.sleep(0.001)
        return UnitObservation()
    scheduler.placement.probe = probe
    scheduler.placement.reconcile()
    scheduler.placement.reconcile()
    assert calls == ["a", "b"]


def test_confirm_observation_timeout_is_not_a_revocation_conflict(system):
    scheduler, _, _ = system
    lease_id = grant(scheduler)["lease_id"]
    initial = time.monotonic()
    values = iter([initial, initial + 1])
    scheduler.placement.monotonic = lambda: next(values, initial+1)
    scheduler.sample_once = lambda: scheduler.snapshot()
    with pytest.raises(LeaseError) as caught:
        scheduler.placement.finish("confirm", lease_id)
    assert caught.value.status == 503
    assert caught.value.error == "lease_observation_timeout"
    assert scheduler.store.lease(lease_id)[0].status == "pending"


@pytest.mark.parametrize("mode", ["removed", "changed"])
def test_missing_trusted_identity_reports_deduplicated_recovery_without_probe(system, mode):
    scheduler, state, transport = system
    lease_id = grant(scheduler)["lease_id"]
    del state["models"]["a"]
    if mode == "removed":
        del transport.models["a"]
        del transport.units["a"]
    else:
        transport.units["a"] = "vllm-replacement.service"
    state["probes"].clear()
    scheduler.sample_once()
    scheduler.placement.reconcile()
    errors = scheduler.snapshot().errors
    assert "lease_model_unobserved:a" in errors
    scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08)
    with pytest.raises(LeaseError):
        grant(scheduler, "b")
    assert state["probes"] == []
    assert scheduler.store.lease(lease_id)[0].status == "pending"
    events = [event for event in scheduler.events_since(0) if event.kind == "lease_configuration_required"]
    assert len(events) == 1
    assert events[0].detail["persisted_unit"] == "vllm-a.service"
    assert events[0].detail["next_action"] == "restore_verified_model_configuration"
    assert events[0].detail["budget_retained"] is True


@pytest.mark.parametrize("mode,expected", [("absent", "released"), ("healthy", "confirmed"), ("unknown", "stale")])
def test_restore_verified_identity_and_reopen_same_ledger_recovers_safely(system, mode, expected):
    scheduler, state, transport = system
    lease_id = grant(scheduler)["lease_id"]
    scheduler.store.put_pin(Pin("a", time.time()+2000, "owner"))
    original_model = state["models"].pop("a")
    original_metadata = transport.models.pop("a")
    original_unit = transport.units.pop("a")
    scheduler.sample_once()
    assert "lease_model_unobserved:a" in scheduler.snapshot().errors
    # Operator restores a previously verified complete model mapping; no unit is
    # automatically trusted just because its name occurs in a SQLite row.
    transport.models["a"] = original_metadata
    transport.units["a"] = original_unit
    state["models"]["a"] = original_model
    if mode == "healthy":
        ready(scheduler, state, lease_id)
    elif mode == "unknown":
        state["observations"]["a"] = UnitObservation()
    scheduler.store.close()
    scheduler.store = IntentStore(scheduler.config.state_db_path, action_lock=scheduler.action_lock)
    try:
        scheduler.placement = PlacementController(scheduler, transport, probe=scheduler.placement.probe)
        scheduler.sample_once()
        assert scheduler.store.lease(lease_id)[0].status == expected
        assert scheduler.snapshot().pins[0].model == "a"
        if mode == "absent":
            assert grant(scheduler, "b")["gpu"] == 0
        else:
            scheduler.config = replace(scheduler.config, placement_wait_seconds=0.08)
            with pytest.raises(LeaseError):
                grant(scheduler, "b")
            assert scheduler.store.lease(lease_id)[0].budget_gb == 60
    finally:
        scheduler.store.close()


def test_missing_identity_readonly_reconcile_is_zero_mutation(system):
    scheduler, state, transport = system
    grant(scheduler)
    del state["models"]["a"]
    del transport.models["a"]
    del transport.units["a"]
    scheduler.config = replace(scheduler.config, read_only=True)
    events = scheduler.events_since(0)
    before = open(scheduler.config.state_db_path, "rb").read()
    state["probes"].clear()
    scheduler.placement.reconcile()
    assert scheduler.events_since(0) == events
    assert open(scheduler.config.state_db_path, "rb").read() == before
    assert state["probes"] == []
