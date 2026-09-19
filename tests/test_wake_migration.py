# Generated-By: OpenCode / glm-5.3
"""Sleeping-model wake migration: placement preflight, stop/release, receipt."""

import http.client
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController, WakeMigration
from llmsvc.config import SchedulerConfig
from llmsvc.leases import PlacementController, UnitObservation
from llmsvc.policy import PolicySettings
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, Lease, MemoryState, ModelState, StateSnapshot
from llmsvc.store import IntentStore


def request(address, method, path, body=None):
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def sleeper():
    return ModelState("m", state="sleeping", gpu=0, unit="vllm-m.service", unit_active=True,
                      health_ok=True, is_sleeping=True, swap_state="stopped", budget_gb=80,
                      weights_gb=40, resident_gb=2, cold_start_seconds=120)


@pytest.fixture
def migration_system(tmp_path):
    state = {
        "model": sleeper(),
        # GPU0 carries external pressure (the production incident shape); the
        # sleeper's in-place wake budget does not fit its 10 GiB free.
        "gpus": (GPUState(0, total_gb=200, free_gb=10, external_gb=130),
                 GPUState(1, total_gb=200, free_gb=200, external_gb=0)),
        "memory": MemoryState(500, 40),
        "extra_models": (), "extra_activity": (),
        "stop_calls": [], "gets": [], "unloads": [], "places": [],
    }

    def collect():
        models = (state["model"],) + tuple(state["extra_models"])
        return StateSnapshot(sampled_at=time.time(), gpus=tuple(state["gpus"]), models=models,
                             memory=state["memory"], activity=(Activity(
                                 "m", time.time() - 60, 3, 1, 0),) + tuple(state["extra_activity"]))

    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
                             placement_enabled=True, state_db_path=str(tmp_path / "wake.sqlite"),
                             wake_timeout_seconds=2, action_observe_seconds=0.3,
                             action_poll_seconds=0.005, placement_wait_seconds=2,
                             lease_probe_seconds=0.03)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())
    store.create_lease(Lease("source-lease", "m", 0, .4, time.time() + 900, 80), "vllm-m.service")
    store.transition_lease("source-lease", "confirmed")
    scheduler = Scheduler(config, collect, store=store)

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["gets"].append(self.path)
            if self.path.startswith("/logs/stream/"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            assert self.path == "/upstream/m/"
            status, placed = request(core.server_address, "POST", "/v1/place",
                                     {"model": "m", "util": .4})
            state["places"].append((status, placed))
            assert status == 200 and placed["gpu"] == 1
            gpu = placed["gpu"]
            state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                                     is_sleeping=False, swap_state="ready", gpu=gpu, resident_gb=80)
            state["gpus"] = tuple(replace(item, free_gb=item.free_gb - 80) if item.index == gpu else item
                                  for item in state["gpus"])
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            state["unloads"].append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:" + str(upstream.server_port),
                                      models={"m": {"unit": "vllm-m.service", "util": .4, "weights_gb": 40}},
                                      systemctl="fixture-systemctl")
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.placement = PlacementController(
        scheduler, transport, probe=lambda model, **kwargs: UnitObservation(False, True))

    def run(argv, **kwargs):
        state["stop_calls"].append(argv[-1])
        if argv[-1] != "vllm-m.service":
            return SimpleNamespace(returncode=1)
        state["model"] = replace(state["model"], state="stopped", unit_active=False,
                                 health_ok=None, is_sleeping=None, swap_state="stopped",
                                 resident_gb=0, gpu=None)
        state["gpus"] = tuple(replace(gpu, free_gb=gpu.free_gb + 2) if gpu.index == 0 else gpu
                              for gpu in state["gpus"])
        state["memory"] = replace(state["memory"], host_available_gb=540, sleeping_weights_gb=0)
        return SimpleNamespace(returncode=0)

    transport.run = run
    core = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    threads = [threading.Thread(target=lambda: core.serve_forever(poll_interval=0.01)),
               threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))]
    for thread in threads:
        thread.start()
    scheduler.sample_once()
    try:
        yield SimpleNamespace(scheduler=scheduler, store=store, transport=transport,
                              state=state, address=core.server_address)
    finally:
        scheduler.stop()
        core.shutdown()
        upstream.shutdown()
        core.server_close()
        upstream.server_close()
        for thread in threads:
            thread.join(2)
        store.close()


def upstream_wake_calls(state):
    return [path for path in state["gets"] if path == "/upstream/m/"]


def test_wake_migrates_sleeper_to_placement_gpu(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200
    assert result["status"] == "ready" and result["ready"] is True
    # The caller pays a cold start on a different card, not a warm wake.
    assert result["cold_start"] is True and result["migrated"] is True
    assert result["source_gpu"] == 0 and result["target_gpu"] == 1
    assert result["source_stopped"] is True
    assert state["stop_calls"] == ["vllm-m.service"]  # exactly one stop: the source
    assert upstream_wake_calls(state) == ["/upstream/m/"]  # one cold-start request, no in-place wake
    assert not state["unloads"]
    assert [call[0] for call in state["places"]] == [200]
    # The confirmed source account was reconciled away; the destination lease
    # was granted by the ordinary place path on the target card.
    leases = scheduler.store.leases()
    assert len(leases) == 1 and leases[0][0].lease_id != "source-lease"
    assert (leases[0][0].model, leases[0][0].gpu) == ("m", 1)
    model = scheduler.snapshot().models[0]
    assert model.state == "awake" and model.gpu == 1
    assert set(scheduler.store.cold_starts()) == {"m"}
    events = {event.kind: event for event in scheduler.events_since(0)
              if event.kind in ("wake_migration", "wake_result")}
    assert events["wake_migration"].detail["source_gpu"] == 0
    assert events["wake_migration"].detail["target_gpu"] == 1
    assert events["wake_result"].detail["migrated"] is True
    assert any(event.kind == "lease_released" for event in scheduler.events_since(0))


def test_preflight_returns_free_fit_plan_and_never_an_eviction(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    plan = scheduler.model_actions.plan_wake_migration(scheduler.snapshot(), "m")
    assert plan == WakeMigration(0, 1, 80)
    # A destination that only fits after evicting another sleeper is no plan:
    # the migration must find a free fit or keep today's hard failure.
    state["extra_models"] = (ModelState("other", state="sleeping", gpu=1, budget_gb=150,
                                        weights_gb=60, resident_gb=2, unit="vllm-other.service",
                                        unit_active=True, health_ok=True, is_sleeping=True,
                                        swap_state="stopped", cold_start_seconds=120),)
    state["extra_activity"] = (Activity("other", time.time() - 1000, 0, 0, 0),)
    scheduler.sample_once()
    assert scheduler.model_actions.plan_wake_migration(scheduler.snapshot(), "m") is None


def test_wake_without_any_feasible_gpu_keeps_the_hard_failure(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    state["gpus"] = (state["gpus"][0],)  # only the pressured source card exists
    scheduler.sample_once()
    before = scheduler.store.leases()
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "blocked" and result["ready"] is False
    assert result["error"] == "insufficient_gpu_memory"
    assert "migrated" not in result
    # Never stop a model we cannot start: nothing was touched at all.
    assert state["stop_calls"] == [] and upstream_wake_calls(state) == []
    assert not state["unloads"] and not state["places"]
    assert scheduler.snapshot().models[0].state == "sleeping"
    assert scheduler.store.leases() == before
    assert not any(event.kind == "wake_migration" for event in scheduler.events_since(0))


def test_eviction_only_destination_is_not_a_migration(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    state["extra_models"] = (ModelState("other", state="sleeping", gpu=1, budget_gb=150,
                                        weights_gb=60, resident_gb=2, unit="vllm-other.service",
                                        unit_active=True, health_ok=True, is_sleeping=True,
                                        swap_state="stopped", cold_start_seconds=120),)
    state["extra_activity"] = (Activity("other", time.time() - 1000, 0, 0, 0),)
    scheduler.sample_once()
    before = scheduler.store.leases()
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "blocked"
    assert result["error"] == "insufficient_gpu_memory"
    assert "migrated" not in result
    # Neither the source nor the potential victim was stopped.
    assert state["stop_calls"] == [] and upstream_wake_calls(state) == []
    assert scheduler.store.leases() == before
    assert {model.name: model.state for model in scheduler.snapshot().models} == {"m": "sleeping", "other": "sleeping"}


def test_disabled_switch_keeps_today_behavior(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    scheduler.config = replace(scheduler.config, wake_migration_enabled=False)
    before = scheduler.store.leases()
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "blocked"
    assert result["error"] == "insufficient_gpu_memory"
    assert "migrated" not in result
    assert state["stop_calls"] == [] and upstream_wake_calls(state) == []
    assert scheduler.snapshot().models[0].state == "sleeping"
    assert scheduler.store.leases() == before


def test_default_model_migrates_home_to_its_exclusive_gpu(migration_system):
    system = migration_system
    scheduler = system.scheduler
    # The default model sleeps off its exclusive card and cannot wake in place.
    # Its exclusive card is free, so that is where it goes.
    scheduler.model_actions.settings = replace(scheduler.model_actions.settings, exclusive_gpu=1)
    system.transport.models["m"]["is_default"] = True
    scheduler.sample_once()
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "ready" and result["ready"] is True
    assert result["migrated"] is True and result["source_stopped"] is True
    assert result["source_gpu"] == 0 and result["target_gpu"] == 1
    model = scheduler.snapshot().models[0]
    assert model.state == "awake" and model.gpu == 1


def test_default_model_falls_back_when_its_exclusive_gpu_is_taken(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    # The production deadlock: an external process owns 130 of the exclusive
    # card's 200 GiB, so the default model can neither wake there nor, before
    # this change, go anywhere else. The exclusive card is a preference, so it
    # falls back to the rest of the pool instead of staying unavailable.
    assert scheduler.model_actions.settings.exclusive_gpu == 0
    system.transport.models["m"]["is_default"] = True
    scheduler.sample_once()
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "ready" and result["ready"] is True
    assert result["migrated"] is True and result["source_gpu"] == 0 and result["target_gpu"] == 1
    assert state["stop_calls"] == ["vllm-m.service"]
    model = scheduler.snapshot().models[0]
    assert model.state == "awake" and model.gpu == 1


def test_failed_migration_stop_leaves_the_sleeper_untouched(migration_system, monkeypatch):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    monkeypatch.setattr(system.transport, "run",
                        lambda argv, **kwargs: SimpleNamespace(returncode=1))
    before = scheduler.store.leases()
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "failed" and result["ready"] is False
    assert result["error"] == "transport_rejected"
    assert result["migrated"] is True and result["source_stopped"] is False
    assert result["source_gpu"] == 0 and result["target_gpu"] == 1
    assert upstream_wake_calls(state) == []  # no cold start was attempted
    assert not state["places"]
    assert scheduler.snapshot().models[0].state == "sleeping"
    assert scheduler.store.leases() == before


def test_unreleased_source_account_reports_partial_without_cold_start(migration_system):
    system = migration_system
    scheduler, state = system.scheduler, system.state
    # The reconciler never sees the unit exit, so the confirmed account stays.
    scheduler.placement.probe = lambda model, **kwargs: UnitObservation(True, False, True, "source-lease")
    status, result = request(system.address, "POST", "/v1/wake/m")
    assert status == 200 and result["status"] == "partial" and result["ready"] is False
    assert result["error"] == "lease_release_unconfirmed"
    assert result["migrated"] is True and result["source_stopped"] is True
    assert upstream_wake_calls(state) == []  # the destination request was never sent
    assert not state["places"]
    assert scheduler.snapshot().models[0].state == "stopped"
    assert scheduler.store.leases()[0][0].lease_id == "source-lease"


def test_wake_migration_flags_are_strict_bools():
    for value in (1, 0, "yes", None, 1.0):
        with pytest.raises(ValueError, match="wake_migration_enabled"):
            SchedulerConfig("127.0.0.1", 8011, wake_migration_enabled=value)
        with pytest.raises(ValueError, match="wake_migration_enabled"):
            PolicySettings(wake_migration_enabled=value)


def test_config_passes_wake_migration_flag_into_policy_settings():
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, state_db_path="state.sqlite",
                             wake_migration_enabled=False)
    assert config.policy_settings().wake_migration_enabled is False
    assert SchedulerConfig("127.0.0.1", 8011).policy_settings().wake_migration_enabled is True
