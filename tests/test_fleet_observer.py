# Generated-By: Codex / gpt-6.1-sol
"""Standalone fleet runtime preserves observation data and rejects controls."""

import copy
from dataclasses import asdict
import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from llmsvc.fleet import FleetError
from llmsvc.fleet.config import ObserverConfig
from llmsvc.fleet.events import FleetEvents, MAX_EVENT_ID
from llmsvc.fleet.observer import FleetObserver, main
from llmsvc.fleet.server import ObserverHTTPServer
from llmsvc.fleet.store import FleetStore
from test_fleet import BASE, service, snapshot, write_mapping, write_snapshot


def config(tmp_path, **changes):
    return ObserverConfig("127.0.0.1", 8011, str(tmp_path / "fleet.json"),
                          str(tmp_path / "fleet.sqlite"), str(tmp_path / "ip-containers.json"), **changes)


@pytest.fixture
def observer(tmp_path):
    settings = config(tmp_path, event_heartbeat_seconds=0.02, event_history_size=2)
    now = [BASE]
    runtime = FleetObserver(settings, clock=lambda: now[0])
    write_mapping(settings)
    try:
        yield runtime, now
    finally:
        runtime.close()


def ingest(runtime, now, payload):
    now[0] = payload["generated_at"]
    write_snapshot(runtime.config, payload)
    runtime.ingest_once()
    assert runtime.controller.last_error is None


@pytest.fixture
def api(observer):
    runtime, now = observer
    server = ObserverHTTPServer(("127.0.0.1", 0), runtime)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield runtime, now, server.server_address
    finally:
        runtime.events.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def request(address, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
    finally:
        connection.close()


def event(response):
    while True:
        line = response.readline()
        if line.startswith(b"data: "):
            return json.loads(line[6:])
        if not line:
            raise AssertionError("Expected an SSE event")


def test_defaults_and_strict_configuration_have_no_control_switches(tmp_path):
    settings = config(tmp_path)
    assert (settings.fleet_raw_retention_days, settings.fleet_hourly_retention_days) == (14, 180)
    assert (settings.fleet_ingest_interval_seconds, settings.fleet_stale_after_seconds,
            settings.fleet_active_window_seconds, settings.fleet_idle_limit_hours) == (30, 180, 900, 6)
    assert settings.fleet_claims_enabled is False
    for name in ("native_witness", "placement_enabled", "registry", "bootstrap", "model_actions_enabled", "read_only"):
        assert not hasattr(settings, name)


@pytest.mark.parametrize("changes", [
    {"listen_host": "0.0.0.0"}, {"listen_host": "::"}, {"listen_host": "8.8.8.8"},
    {"listen_host": "host.invalid"}, {"listen_port": True}, {"listen_port": 0},
    {"fleet_db_path": "relative.sqlite"}, {"host_ips": "127.0.0.1"},
    {"host_ips": ["192.0.2.1", "::ffff:192.0.2.1"]},
    {"event_history_size": 10001}, {"event_history_size": True},
    {"fleet_raw_retention_days": 0}, {"fleet_raw_retention_days": 181},
    {"fleet_stale_after_seconds": float("nan")}, {"event_heartbeat_seconds": float("inf")},
    {"fleet_ingest_interval_seconds": 10 ** 1000},
    {"fleet_claims_enabled": False}, {"collectors": {}}, {"model_actions_enabled": False},
])
def test_invalid_config_fails_before_creating_any_artifacts(tmp_path, changes):
    payload = asdict(config(tmp_path))
    payload.update(changes)
    with pytest.raises(ValueError):
        ObserverConfig.from_mapping(payload)
    assert list(tmp_path.iterdir()) == []


def test_check_config_loads_no_store_and_creates_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(dict(asdict(config(tmp_path)), _comments="synthetic configuration")))
    path.chmod(0o600)
    import llmsvc.fleet.observer as module
    monkeypatch.setattr(module, "FleetObserver", lambda *args: pytest.fail("check-config constructed a runtime"))
    before = set(tmp_path.rglob("*"))
    assert main(["--config", str(path), "--check-config"]) == 0
    assert set(tmp_path.rglob("*")) == before
    assert json.loads(capsys.readouterr().out) == {"ok": True, "check_config": True}
    path.write_text('{"fleet_db_path":"/private/secret.sqlite"}')
    assert main(["--config", str(path), "--check-config"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "invalid_observer_config"}


def test_standalone_import_and_construction_are_standard_library_only(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
blocked = ('llmsvc.scheduler', 'llmsvc.server', 'llmsvc.config', 'llmsvc.store',
           'llmsvc.state', 'llmsvc.actions', 'llmsvc.native', 'llmsvc.model_actions',
           'llmsvc.bootstrap', 'llmsvc.collectors', 'llmsvc.policy', 'llmsvc.intent_store',
           'llmsvc.registry', 'llmsvc.reload', 'llmsvc.native_binding', 'yaml', 'rich')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert not any(fullname == key or fullname.startswith(key + '.') for key in blocked), fullname
sys.meta_path.insert(0, Guard())
from llmsvc.fleet.observer import FleetObserver
from llmsvc.fleet.config import ObserverConfig
root = Path(sys.argv[2])
runtime = FleetObserver(ObserverConfig('127.0.0.1', 8011, str(root / 'fleet.json'),
                                     str(root / 'fleet.sqlite'), str(root / 'ips.json')))
assert runtime.report()['claims_enabled'] is False
assert not any(root.iterdir())
runtime.close()
assert not any(key == item or item.startswith(key + '.') for key in blocked for item in sys.modules)
print(json.dumps({'isolated_imports': True, 'no_store_created': True}))
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script, str(repository), str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"isolated_imports": True, "no_store_created": True}


def test_migrated_baselines_watermark_history_and_claim_rows_are_preserved(observer):
    runtime, now = observer
    store = FleetStore(runtime.config.fleet_db_path)
    first = snapshot()
    later = copy.deepcopy(first)
    later["generated_at"] += 60
    later["services"][0]["metrics"].update(requests_total=104, generation_tokens_total=1012, prompt_tokens_total=3024)
    assert store.ingest(first, runtime.config, BASE)
    assert store.ingest(later, runtime.config, BASE + 60)
    claim = {"id": "legacy-claim", "instance_id": "instance-a", "container": "ctr-a",
             "model": "example-model", "until": BASE + 86400, "reason": "historic protection",
             "created_by_container": "ctr-a", "created_at": BASE, "revoked_at": None}
    store.put_claim(claim)
    before = store.instance("instance-a")["state_json"]
    hours = store.hourly(BASE, BASE + 3600)
    store.close()
    ingest(runtime, now, later)
    report = runtime.report()
    assert report["claims_enabled"] is False and report["llmsvc_shared"] == {"models_loaded": []}
    assert report["services"][0]["claim"] is None and report["services"][0]["status"] == "active"
    assert runtime.controller.store.instance("instance-a")["state_json"] == before
    assert runtime.controller.store.hourly(BASE, BASE + 3600) == hours
    assert runtime.controller.store.metadata()["generated_at"] == BASE + 60
    assert runtime.controller.store.claim("legacy-claim")["revoked_at"] is None
    assert report["services"][0]["window_24h"]["requests"] == 4
    assert runtime.history("instance-a", 24)["samples"][-1]["d_gen_tokens"] == 12
    with pytest.raises(FleetError, match="fleet_claims_disabled"):
        runtime.controller.write_claim("unclaim", {"id": "legacy-claim"}, source_ip="127.0.0.1")
    runtime.ingest_once()
    assert runtime.report()["services"][0]["window_24h"]["requests"] == 4


def test_historical_claim_does_not_change_idle_or_unknown_status(observer):
    runtime, now = observer
    ingest(runtime, now, snapshot())
    runtime.controller.store.put_claim({"id": "legacy-claim", "instance_id": "instance-a", "container": "ctr-a",
        "model": "example-model", "until": BASE + 86400, "reason": "history",
        "created_by_container": "ctr-a", "created_at": BASE, "revoked_at": None})
    assert runtime.report()["services"][0]["status"] == "idle"
    failed = snapshot(BASE + 60, [service(scrape={"ok": False, "error": "timeout"})])
    ingest(runtime, now, failed)
    assert runtime.report()["services"][0]["status"] == "unknown"
    assert runtime.report()["services"][0]["claim"] is None


def test_reset_gap_ollama_null_and_complete_inventory_exit_are_unchanged(observer):
    runtime, now = observer
    ingest(runtime, now, snapshot())
    reset = service()
    reset["metrics"]["requests_total"] = 1
    ingest(runtime, now, snapshot(BASE + 60, [reset]))
    assert runtime.history("instance-a", 24)["samples"][-1]["counter_reset"] == 1
    changed = copy.deepcopy(reset)
    changed["metrics"]["requests_total"] = 3
    ingest(runtime, now, snapshot(BASE + 600, [changed]))
    point = runtime.history("instance-a", 24)["samples"][-1]
    assert point["gap"] == 1 and point["d_requests"] == 2 and point["observed_seconds"] == 0
    ollama = service("ollama", engine="ollama", model=None, metrics=None,
                     ollama={"models": []})
    ingest(runtime, now, snapshot(BASE + 660, [changed, ollama]))
    point = runtime.history("ollama", 24)["samples"][-1]
    assert all(point[key] is None for key in ("d_requests", "d_gen_tokens", "d_prompt_tokens", "d_cached_tokens"))
    ingest(runtime, now, snapshot(BASE + 720, [], inventory_complete=False))
    assert runtime.controller.store.instance("instance-a")["ended_at"] is None
    ingest(runtime, now, snapshot(BASE + 780, []))
    assert runtime.controller.store.instance("instance-a")["ended_at"] == BASE + 780
    assert runtime.report()["services"] == []


def test_missing_snapshot_keeps_existing_data_unknown_and_not_empty(observer):
    runtime, now = observer
    ingest(runtime, now, snapshot())
    Path(runtime.config.fleet_snapshot_path).unlink()
    runtime.ingest_once()
    report = runtime.report()
    assert report["stale"] is True and report["services"][0]["status"] == "unknown"
    assert "fleet_snapshot_unavailable" in report["errors"]
    assert runtime.controller.store.instance("instance-a")["ended_at"] is None


def test_socket_peer_mine_ignores_headers_and_requires_fresh_mapping(api):
    runtime, now, address = api
    ingest(runtime, now, snapshot(services=[service(), service("instance-b", container="ctr-b")]))
    status, payload, _ = request(address, "GET", "/v1/fleet?mine=1",
                                 headers={"X-Forwarded-For": "192.0.2.2", "X-Real-IP": "192.0.2.2"})
    assert status == 200 and [row["container"] for row in payload["services"]] == ["ctr-a"]
    assert payload["services"][0]["mine"] is True
    now[0] += 181
    assert request(address, "GET", "/v1/fleet?mine=1")[0] == 403
    assert request(address, "GET", "/v1/fleet")[0] == 200


def test_mine_preserves_owned_generic_jobs_without_an_llm_service(api):
    runtime, now, address = api
    ingest(runtime, now, snapshot(services=[], other_gpu_processes=[
        {"container": "ctr-a", "pid": 1234, "gpu": 2, "used_mib": 2048, "comm": "python"},
        {"container": "ctr-b", "pid": 5678, "gpu": 2, "used_mib": 4096, "comm": "python"}]))
    status, report, _ = request(address, "GET", "/v1/fleet?mine=1")
    assert status == 200 and report["services"] == []
    assert [row["container"] for row in report["gpus"][0]["occupants"]] == ["ctr-a"]
    with pytest.raises(FleetError, match="unmapped_container"):
        runtime.report(mine=True)


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH", "CONNECT", "TRACE", "OPTIONS", "HEAD", "CUSTOM"])
@pytest.mark.parametrize("path", ["/v1/fleet", "/v1/fleet/claims?dry_run=1", "/v1/place", "/v1/models", "/v1/events"])
def test_every_non_get_verb_is_rejected_without_control_or_db_writes(api, method, path):
    runtime, _, address = api
    assert not Path(runtime.config.fleet_db_path).exists()
    status, payload, headers = request(address, method, path, body=b'{"operation":"stop"}')
    assert status == 405 and headers["Allow"] == "GET"
    if method != "HEAD":
        assert payload == {"error": "read_only_observer"}
    assert not Path(runtime.config.fleet_db_path).exists()


@pytest.mark.parametrize("path", ["/v1/state", "/v1/models", "/v1/registry", "/v1/usage", "/v1/place", "/v1/native", "/v1/fleet/claims"])
def test_retired_control_routes_are_not_present(api, path):
    assert request(api[2], "GET", path)[:2] == (404, {"error": "not_found"})


@pytest.mark.parametrize("path", ["/v1/fleet?mine=1&mine=1", "/v1/fleet?dry_run=1", "/v1/fleet?x=1",
    "/v1/fleet/history?service=instance-a&hours=48", "/v1/fleet/history?service=&hours=24",
    "/v1/events?since=-1", "/v1/events?since=1&since=2", "/v1/events?since=" + str(MAX_EVENT_ID + 1),
    "/v1/events?since=" + "9" * 100, "/v1/events?incarnation=no", "/v1/events?x=1"])
def test_invalid_read_queries_are_bounded_and_rejected(api, path):
    assert request(api[2], "GET", path)[0] == 400


def test_get_report_history_and_fresh_gpu_host_ownership_are_schema1(api):
    runtime, now, address = api
    row = snapshot(other_gpu_processes=[{"container": None, "host": True, "host_uid": 1234,
        "host_user": "worker", "pid": 4567, "gpu": 2, "used_mib": 2048, "comm": "python"}])
    ingest(runtime, now, row)
    status, payload, _ = request(address, "GET", "/v1/fleet")
    assert status == 200 and payload["schema_version"] == 1
    assert payload["observer_incarnation"] == runtime.events.incarnation
    assert payload["claims_enabled"] is False and payload["services"][0]["claim"] is None
    assert payload["llmsvc_shared"] == {"models_loaded": []}
    job = payload["gpus"][0]["occupants"][-1]
    assert job["host"] is True and job["host_uid"] == 1234 and job["host_user"] == "worker"
    status, payload, _ = request(address, "GET", "/v1/fleet/history?service=instance-a&hours=168")
    assert status == 200 and payload["resolution"] == "hourly" and len(payload["samples"]) <= 169


@pytest.mark.parametrize("mode,reason", [("different", "incarnation_changed"), ("ahead", "cursor_ahead"),
    ("expired", "cursor_expired"), ("missing", "incarnation_required")])
def test_sse_resets_old_cursor_then_replays_same_incarnation(api, mode, reason):
    runtime, _, address = api
    for _ in range(5):
        runtime.events.emit("fleet_snapshot_changed", detail={"generated_at": BASE})
    incarnation = runtime.events.incarnation
    cursor = 1 if mode != "ahead" else 900
    query = "/v1/events?since=" + str(cursor)
    if mode != "missing":
        query += "&incarnation=" + ("0" * 32 if mode == "different" else incarnation)
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", query)
        response = connection.getresponse()
        assert response.status == 200 and response.getheader("X-Observer-Incarnation") == incarnation
        first = event(response)
        assert first["id"] == 0 and first["kind"] == "cursor_reset" and first["detail"]["reason"] == reason
        replay = [event(response), event(response)]
        assert [item["id"] for item in replay] == [4, 5]
        assert all(item["observer_incarnation"] == incarnation for item in [first, *replay])
    finally:
        connection.close()


def test_sse_reconnect_current_cursor_heartbeat_and_close_wakeup(api):
    runtime, _, address = api
    runtime.events.emit("fleet_snapshot_changed", detail={"generated_at": BASE})
    first = runtime.events.emit("fleet_status_changed", detail={"service_id": "instance-a", "to": "active"})
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", "/v1/events?since=1&incarnation=" + runtime.events.incarnation,
                           headers={"Last-Event-ID": "1"})
        response = connection.getresponse()
        assert event(response) == first
        assert response.readline() == b"\n"
        assert response.readline() == b": heartbeat\n"
        assert response.readline() == b"\n"
        runtime.events.close()
        # Close wakes the condition rather than waiting the heartbeat deadline.
        assert response.readline() in (b": heartbeat\n", b"")
    finally:
        connection.close()


def test_event_buffer_is_bounded_copies_records_and_close_wakes_waiter():
    events = FleetEvents(2, clock=lambda: BASE)
    emitted = events.emit("fleet_snapshot_changed", detail={"items": []})
    emitted["detail"]["items"].append("caller mutation")
    assert events.events_since(0)[0]["detail"] == {"items": []}
    events.emit("fleet_status_changed")
    events.emit("fleet_snapshot_changed")
    assert [item["id"] for item in events.events_since(0)] == [2, 3]
    completed = threading.Event()
    thread = threading.Thread(target=lambda: (events.events_since(3, timeout=30), completed.set()))
    thread.start()
    events.close()
    assert completed.wait(1)
    thread.join(timeout=1)
    assert events.emit("fleet_snapshot_changed") is None
    with pytest.raises(ValueError):
        FleetEvents(10001)


def test_background_worker_stops_without_constructing_a_collector(tmp_path):
    runtime = FleetObserver(config(tmp_path, fleet_ingest_interval_seconds=0.02), clock=lambda: BASE)
    write_snapshot(runtime.config, snapshot())
    write_mapping(runtime.config)
    runtime.start()
    try:
        deadline = time.monotonic() + 2
        while not Path(runtime.config.fleet_db_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.report()["generated_at"] == BASE
    finally:
        runtime.close()
    assert not runtime.thread.is_alive() and runtime.events.closed.is_set()
    with pytest.raises(RuntimeError):
        runtime.start()
