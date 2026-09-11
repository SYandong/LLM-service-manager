# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Actual loopback transport/reentry tests; placement and GPU effects are fixtures."""

import http.client
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from llmsvc.actions import ActionDispatchError, ManagedModelTransport, ModelActionController
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHandler, SchedulerHTTPServer
from llmsvc.state import Activity, GPUState, MemoryState, ModelState, Pin, StateSnapshot


def request(address, method, path, body=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def system(tmp_path):
    state = {"model": ModelState("model", state="stopped", unit="vllm-model.service", unit_active=False,
                                health_ok=None, is_sleeping=None, swap_state="stopped", weights_gb=40,
                                budget_gb=80, resident_gb=None, cold_start_seconds=120),
             "free": 150.0, "http_calls": [], "stop_calls": [], "mode": "reenter", "reentries": 0}
    def collect():
        return StateSnapshot(sampled_at=time.time(), gpus=(GPUState(0, total_gb=200, free_gb=state["free"], external_gb=0),),
            models=(state["model"],), pins=state.get("pins", ()),
            memory=MemoryState(state.get("available", 500), 40 if state["model"].state == "sleeping" else 0),
            activity=(Activity("model", time.time()-1000, 0, 0, 0),))
    config = SchedulerConfig("127.0.0.1", 8011, read_only=False, model_actions_enabled=True,
        state_db_path=str(tmp_path/"unused.sqlite"), free_timeout_seconds=1, wake_timeout_seconds=0.4,
        action_poll_seconds=0.005, action_observe_seconds=0.1)
    scheduler = Scheduler(config, collect)
    class PlacementFixture(SchedulerHandler):
        def do_POST(self):
            if self.path == "/v1/place":
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                with self.server.scheduler.action_lock:
                    state["reentries"] += 1
                    self._json(200, {"gpu": 0, "fixture_only": True})
            else:
                super().do_POST()
    core = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    core.RequestHandlerClass = PlacementFixture
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            state["http_calls"].append(("GET", self.path))
            if self.path.startswith("/logs/stream/"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            assert self.path == "/upstream/model/"
            if state["mode"] == "reenter":
                status, response = request(core.server_address, "POST", "/v1/place", {"model": "model"})
                assert status == 200 and response["fixture_only"]
            if state["mode"] in ("reenter", "ready"):
                state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                                         is_sleeping=False, swap_state="ready", gpu=0, resident_gb=80)
            self.send_response(404 if state["mode"] in ("reenter", "ready") else 202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def do_POST(self):
            state["http_calls"].append(("POST", self.path))
            assert self.path == "/api/models/unload/model"
            if state["mode"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "/forbidden")
            else:
                state["model"] = replace(state["model"], state="sleeping", is_sleeping=True, resident_gb=2)
                state["free"] += 12
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    def run(argv, **kwargs):
        state["stop_calls"].append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:"+str(upstream.server_port),
        models={"model": {"unit": "vllm-model.service"}}, systemctl="configured-systemctl", run=run)
    controller = ModelActionController(scheduler, transport)
    scheduler.model_actions = controller
    scheduler.sample_once()
    threads = [threading.Thread(target=lambda: core.serve_forever(poll_interval=0.01)),
               threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))]
    for thread in threads:
        thread.start()
    try:
        yield scheduler, core.server_address, transport, state
    finally:
        scheduler.stop()
        core.shutdown()
        upstream.shutdown()
        core.server_close()
        upstream.server_close()
        for thread in threads:
            thread.join(2)


def test_wake_reenters_real_loopback_place_without_holding_global_lock(system):
    scheduler, address, _, state = system
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["status"] == "ready" and result["ready"] is True
    assert result["cold_start"] is True
    assert state["reentries"] == 1  # The fixture /v1/place handler acquired the SAME lock.
    assert state["http_calls"].count(("GET", "/upstream/model/")) == 1
    assert not scheduler.model_actions.pending
    assert not state["stop_calls"]
    # This proves reentry/unblocking, not a production lease/placement protocol.


def test_stopped_wake_progress_reader_is_advisory_and_closed_before_result(system):
    scheduler, address, transport, state = system
    seen = []

    class FixtureReader:
        def __init__(self, **kwargs):
            seen.append(("init", kwargs["model"], kwargs["base_url"], kwargs["deadline"]))
            self.emit = kwargs["emit"]
            self.closed = False

        def start(self):
            seen.append(("lock_owned", scheduler.action_lock._is_owned()))
            self.emit({"stage": "process_started", "source": "llama-swap",
                       "source_model": "model", "progress_source": "per_model_log",
                       "log_epoch": "fixture", "sequence": 1, "received_at": 1.0,
                       "trusted_for_quiet": False})
            seen.append("started")

        def close(self, timeout=2.0):
            self.closed = True
            seen.append(("closed", timeout))
            return True

    scheduler.model_actions.progress_reader_factory = FixtureReader
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["ready"] is True
    assert seen[0][0] == "init" and ("lock_owned", False) in seen and "started" in seen and seen[-1][0] == "closed"
    progress = [item for item in scheduler.events_since(0) if item.kind == "wake_progress"]
    assert len(progress) == 1
    assert progress[0].detail["source_model"] == "model"
    assert state["http_calls"] == [("GET", "/upstream/model/")]
    assert transport.swap_url.startswith("http://127.0.0.1:")


def test_reserved_model_cold_wake_keeps_original_transport_without_log_stream(tmp_path):
    state = {"model": ModelState("proxy", state="stopped", unit="vllm-proxy.service",
                                  unit_active=False, health_ok=None, is_sleeping=None,
                                  swap_state="stopped", weights_gb=40, budget_gb=80,
                                  resident_gb=None), "calls": []}

    def collect():
        return StateSnapshot(sampled_at=time.time(),
            gpus=(GPUState(0, total_gb=200, free_gb=150, external_gb=0),),
            models=(state["model"],), memory=MemoryState(500, 40),
            activity=(Activity("proxy", time.time() - 1000, 0, 0, 0),))

    scheduler = Scheduler(SchedulerConfig("127.0.0.1", 8011, read_only=False,
        model_actions_enabled=True, state_db_path=str(tmp_path / "state.sqlite"),
        wake_timeout_seconds=1, action_poll_seconds=0.005), collect)
    core = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["calls"].append(self.path)
            assert self.path == "/upstream/proxy/"
            state["model"] = replace(state["model"], state="awake", unit_active=True,
                                      health_ok=True, is_sleeping=False, swap_state="ready",
                                      gpu=0, resident_gb=80)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    transport = ManagedModelTransport(
        swap_url="http://127.0.0.1:" + str(upstream.server_port),
        models={"proxy": {"unit": "vllm-proxy.service"}}, systemctl="configured-systemctl")
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.sample_once()
    threads = [threading.Thread(target=lambda: core.serve_forever(poll_interval=0.01)),
               threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))]
    for thread in threads:
        thread.start()
    try:
        status, result = request(core.server_address, "POST", "/v1/wake/proxy")
        assert status == 200 and result["ready"] is True
        assert state["calls"] == ["/upstream/proxy/"]
        assert not any(path.startswith("/logs/stream/") for path in state["calls"])
    finally:
        scheduler.stop()
        core.shutdown(); upstream.shutdown()
        core.server_close(); upstream.server_close()
        for thread in threads:
            thread.join(2)


def test_wake_wait_releases_lock_and_observes_later_readiness(system):
    scheduler, address, _, state = system
    state["mode"] = "accepted"
    result = []
    worker = threading.Thread(target=lambda: result.append(request(address, "POST", "/v1/wake/model", {})))
    worker.start()
    deadline = time.monotonic()+1
    while not state["http_calls"] and time.monotonic()<deadline:
        time.sleep(0.005)
    assert state["http_calls"]
    assert scheduler.action_lock.acquire(timeout=0.1)
    try:
        state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                                 is_sleeping=False, swap_state="ready", gpu=0, resident_gb=80)
        scheduler.changed.notify_all()
    finally:
        scheduler.action_lock.release()
    worker.join(2)
    assert result[0][1]["ready"] is True


def test_wake_timeout_is_not_false_readiness(system):
    scheduler, address, _, state = system
    state["mode"] = "accepted"
    scheduler.config = replace(scheduler.config, wake_timeout_seconds=0.05)
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["status"] == "timeout" and result["ready"] is False
    assert not scheduler.model_actions.pending


def test_sleeping_wake_checks_physical_headroom(system):
    scheduler, address, _, state = system
    state["model"] = replace(state["model"], state="sleeping", unit_active=True, health_ok=True,
                             is_sleeping=True, swap_state="stopped", gpu=0, resident_gb=2)
    state["free"] = 10
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["status"] == "blocked"
    assert result["error"] == "insufficient_gpu_memory"
    assert not state["http_calls"]


def test_http_free_reports_confirmed_observed_delta(system):
    scheduler, address, _, state = system
    state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                             is_sleeping=False, swap_state="ready", gpu=0, resident_gb=80)
    status, result = request(address, "POST", "/v1/free", {"need_gb": 10})
    assert status == 200 and result["status"] == "complete"
    assert result["freed_gb"] == 12 and result["slept"] == ["model"]
    assert state["http_calls"] == [("POST", "/api/models/unload/model")]
    assert not state["stop_calls"]


def test_readonly_and_disabled_modes_never_send_model_requests(system):
    scheduler, address, _, state = system
    scheduler.config = replace(scheduler.config, read_only=True)
    assert request(address, "POST", "/v1/wake/model")[0] == 405
    assert request(address, "POST", "/v1/free", {})[0] == 405
    scheduler.config = replace(scheduler.config, read_only=False, model_actions_enabled=False)
    assert request(address, "POST", "/v1/wake/model") == (405, {"error": "operation_not_enabled"})
    assert not state["http_calls"] and not state["stop_calls"]


def test_wake_preview_is_transport_free_and_body_cannot_override_path(system):
    scheduler, address, _, state = system
    before = scheduler.snapshot(), scheduler.events_since(0)
    status, result = request(address, "POST", "/v1/wake/model?dry_run=1")
    assert status == 200 and result["would"][0]["kind"] == "wake"
    assert (scheduler.snapshot(), scheduler.events_since(0)) == before
    assert request(address, "POST", "/v1/wake/model", {"model": "other"})[0] == 400
    assert not state["http_calls"]


def test_redirects_and_unapproved_paths_cannot_change_transport_target(system):
    _, _, transport, state = system
    state["mode"] = "redirect"
    assert transport.http_request("POST", "/api/models/unload/model", deadline=time.monotonic()+1) == 302
    assert state["http_calls"] == [("POST", "/api/models/unload/model")]
    with pytest.raises(ActionDispatchError):
        transport.http_request("GET", "/health", deadline=time.monotonic()+1)
    with pytest.raises(ActionDispatchError):
        transport.http_request("POST", "/api/models/unload/other", deadline=time.monotonic()+1)
    assert len(state["http_calls"]) == 1


def test_stop_transport_uses_only_configured_unit_and_remaining_deadline(system):
    _, _, transport, state = system
    with pytest.raises(ActionDispatchError):
        transport.stop_unit("sshd.service", deadline=time.monotonic()+1)
    assert transport.stop_unit("vllm-model.service", deadline=time.monotonic()+1) == 0
    argv, options = state["stop_calls"][0]
    assert argv == ["configured-systemctl", "stop", "vllm-model.service"]
    assert 0 < options["timeout"] <= 1
    assert "shell" not in options


@pytest.mark.parametrize("url", ["http://user:secret@localhost", "http://localhost/upstream/other", "http://localhost?x=1", "file:///tmp/model"])
def test_action_transport_requires_an_explicit_origin(url):
    with pytest.raises(ValueError):
        ManagedModelTransport(swap_url=url, models={"model": {}}, systemctl="configured-systemctl")


def test_pinned_sleeping_model_can_wake_without_removing_pin(system):
    scheduler, address, _, state = system
    state["mode"] = "ready"
    state["model"] = replace(state["model"], state="sleeping", unit_active=True, health_ok=True,
                             is_sleeping=True, swap_state="stopped", gpu=0, resident_gb=2)
    state["pins"] = (Pin("model", time.time()+100, "owner"),)
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["ready"] is True and result["cold_start"] is False
    assert scheduler.snapshot().pins == state["pins"]
    assert state["reentries"] == 0 and not state["stop_calls"]


def test_unknown_ram_blocks_cold_wake_before_request(system):
    _, address, _, state = system
    state["available"] = None
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["status"] == "blocked" and result["error"] == "unknown_memory"
    assert not state["http_calls"]


def test_cold_wake_preserves_configured_host_memory_floor(system):
    _, address, _, state = system
    state["available"] = 170
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["status"] == "blocked" and result["error"] == "memory_budget"
    assert not state["http_calls"]


@pytest.mark.parametrize("active", [False, None])
def test_contradictory_sleeping_unit_observation_blocks_wake(system, active):
    _, address, _, state = system
    state["model"] = replace(state["model"], state="sleeping", unit_active=active, health_ok=True,
                             is_sleeping=True, swap_state="stopped", gpu=0, resident_gb=2)
    status, result = request(address, "POST", "/v1/wake/model")
    assert status == 200 and result["status"] == "blocked" and result["error"] == "model_state_changed"
    assert not state["http_calls"]


@pytest.mark.parametrize("guard", ["operation_in_progress", "unmanaged_model", "configured_unit_mismatch"])
def test_http_free_keeps_real_pin_and_operational_exclusion_reasons(system, guard):
    scheduler, address, transport, state = system
    state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                             is_sleeping=False, swap_state="ready", gpu=0, resident_gb=80)
    pin = Pin("model", time.time()+100, "real-owner")
    state["pins"] = (pin,)
    if guard == "operation_in_progress":
        scheduler.model_actions.pending.add("model")
    elif guard == "unmanaged_model":
        del transport.models["model"]
    else:
        state["model"] = replace(state["model"], unit="vllm-other.service")
    status, result = request(address, "POST", "/v1/free", {"need_gb": 10})
    assert status == 200 and result["status"] == "blocked"
    assert [b["reason"] for b in result["skipped"] if b["model"] == "model"] == ["pinned_until", guard]
    assert result["slept"] == [] and result["stopped"] == [] and result["freed_gb"] == 0
    assert state["http_calls"] == [] and state["stop_calls"] == []
    assert scheduler.snapshot().pins == (pin,) and scheduler.snapshot().models[0].budget_gb == 80
