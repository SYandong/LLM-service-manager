# Generated-By: Codex / gpt-6-astra
"""Actual registry behind the core HTTP server; no reload or staged writer."""

import http.client
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from llmsvc.__main__ import build_registry
from llmsvc.config import SchedulerConfig
from llmsvc.registry import ModelRegistry, add_full_weight_model
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import Activity, ModelState, Pin
from test_registry_api import registry as registry_fixture


def request(address, method, path, payload=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request(method, path, body=json.dumps(payload) if payload is not None else None)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def mounted(registry_fixture, monkeypatch, tmp_path):
    api, queue, weights, state, clock, calls, units, _ = registry_fixture
    # Seed a real saved temporary record with the owner's pure transform.
    original = queue.path.read_bytes()
    added = add_full_weight_model(yaml.safe_load(original), {}, name="saved", model_path=weights,
        base_model="base", shared_roots=(weights.parent,), daemon_port_range=(8101, 8110), created_at=clock[0])
    queue.path.write_bytes(ModelRegistry._encode(original, added.config, added.records))
    state[0] = replace(state[0], models=state[0].models + (ModelState("saved", state="stopped"),),
                       activity=state[0].activity + (Activity("saved", last_request_at=clock[0], in_flight=0),))
    config = SchedulerConfig("127.0.0.1", 8103, registry={"config_path": str(queue.path),
        "shared_roots": [str(weights.parent)], "daemon_port_range": [8101, 8110], "reserved_ports": [8104]})
    scheduler = Scheduler(config, lambda: replace(state[0], sampled_at=clock[0]), clock=lambda: clock[0])
    scheduler.sample_once()
    scheduler.registry = build_registry(config, scheduler)
    registry = scheduler.registry
    assert isinstance(registry, ModelRegistry) and registry.queue.action_lock is scheduler.action_lock
    def forbidden(*args, **kwargs):
        pytest.fail("Registry preview invoked a staging, writer, unit or reload callback")
    for name in ("_stage", "validate", "notify_reload", "process_once"):
        monkeypatch.setattr(registry.queue, name, forbidden)
    monkeypatch.setattr(registry, "stop_model", forbidden)
    monkeypatch.setattr(registry, "unit_absent", forbidden)
    monkeypatch.setattr("llmsvc.reload.uuid.uuid4", forbidden)
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    thread.start()
    def files():
        return {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = SimpleNamespace(scheduler=scheduler, registry=registry, address=server.server_address,
        path=queue.path, weights=weights, state=state, clock=clock, files=files, records=added.records,
        calls=calls, units=units)
    try:
        yield result
    finally:
        scheduler.stop()
        server.shutdown(); server.server_close(); thread.join(3)


def assert_readonly(mounted, before):
    files, events = before
    assert mounted.files() == files
    assert mounted.scheduler.events_since(0) == events
    assert not mounted.registry.queue._jobs and not mounted.registry.queue._pending
    assert not mounted.registry._removals and mounted.calls == [] and mounted.units == set()


def test_list_and_real_add_remove_preview_keep_unknown_quiet_blocked(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, listed = request(mounted.address, "GET", "/v1/models")
    assert status == 200 and listed["records"] == mounted.records and listed["writes_enabled"] is False
    assert {b["reason"] for b in listed["blocked_by"]} >= {"registry_writes_disabled", "inflight_stream_unknown"}
    status, added = request(mounted.address, "POST", "/v1/models?dry_run=1",
                            {"name": "candidate", "path": str(mounted.weights), "base": "base"})
    assert status == 200 and added["would"] == [{"kind": "add_model", "model": "candidate", "base": "base"}]
    status, removed = request(mounted.address, "DELETE", "/v1/models/saved?dry_run=1")
    assert status == 200 and removed["would"][0]["kind"] == "remove_model"
    for result in (added, removed):
        assert result["dry_run"] is True and result["config_committed"] is False and "id" not in result
        assert any(b["reason"] == "inflight_stream_unknown" for b in result["blocked_by"])
    assert_readonly(mounted, before)


@pytest.mark.parametrize("readonly", [True, False])
@pytest.mark.parametrize("method,path,body", [("POST", "/v1/models", {}), ("DELETE", "/v1/models/saved", None)])
def test_actual_writes_stay_rejected_even_with_writable_intents(mounted, readonly, method, path, body):
    mounted.scheduler.config = replace(mounted.scheduler.config, read_only=readonly,
        state_db_path=str(mounted.path.parent / "unused.sqlite"))
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, method, path, body)
    assert status == 405 and result["error"] == ("read_only" if readonly else "operation_not_enabled")
    assert_readonly(mounted, before)


@pytest.mark.parametrize("change", ["duplicate", "outside", "symlink-outside", "no-weights", "bad-name", "missing-base", "lora"])
def test_registry_validation_is_used_before_preview_success(mounted, tmp_path, change):
    body = {"name": "candidate", "path": str(mounted.weights), "base": "base"}
    if change == "duplicate":
        body["name"] = "saved"
    elif change in ("outside", "symlink-outside"):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "config.json").write_text("{}")
        (outside / "model.safetensors").write_bytes(b"fixture")
        if change == "symlink-outside":
            link = mounted.weights.parent / "escape"
            link.symlink_to(outside, target_is_directory=True)
            body["path"] = str(link)
        else:
            body["path"] = str(outside)
    elif change == "no-weights":
        empty = mounted.weights.parent / "empty"
        empty.mkdir()
        (empty / "config.json").write_text("{}")
        body["path"] = str(empty)
    elif change == "bad-name":
        body["name"] = "../escape"
    elif change == "missing-base":
        body["base"] = "missing"
    else:
        body["lora"] = True
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "POST", "/v1/models?dry_run=1", body)
    assert status == 400 and result["error"] == "registry_invalid_request" and result["message"]
    assert_readonly(mounted, before)


def test_existing_core_and_reserved_ports_are_not_proposed(mounted):
    # Base8101, saved8102, scheduler8103 and explicit8104 fill this range.
    mounted.registry.daemon_port_range = (8101, 8104)
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "POST", "/v1/models?dry_run=1",
                            {"name": "new", "path": str(mounted.weights), "base": "base"})
    assert status == 400 and "port" in result["message"]
    assert_readonly(mounted, before)


@pytest.mark.parametrize("protection", ["permanent", "pin", "default", "inflight", "unknown"])
def test_delete_preserves_existing_protection_checks(mounted, protection):
    state = mounted.state[0]
    name = "saved"
    if protection == "permanent":
        name = "base"
    elif protection == "pin":
        state = replace(state, pins=(Pin("saved", 2000, "owner"),))
    elif protection == "default":
        state = replace(state, models=(state.models[0], replace(state.models[1], is_default=True)))
    elif protection == "inflight":
        state = replace(state, activity=(state.activity[0], replace(state.activity[1], in_flight=1)))
    else:
        state = replace(state, models=(state.models[0], replace(state.models[1], state="unknown")))
    mounted.state[0] = state
    mounted.scheduler.sample_once()
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "DELETE", "/v1/models/"+name+"?dry_run=1")
    assert status == 400 and result["error"] == "registry_invalid_request"
    assert_readonly(mounted, before)


def test_pending_transaction_is_listed_as_blocked_and_preview_cannot_bypass_it(mounted):
    mounted.registry.queue.marker.write_text("fixture interrupted transaction")
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, listed = request(mounted.address, "GET", "/v1/models")
    assert status == 200 and any(b["reason"] == "registry_reconciliation_required" for b in listed["blocked_by"])
    status, result = request(mounted.address, "POST", "/v1/models?dry_run=1",
                            {"name": "new", "path": str(mounted.weights), "base": "base"})
    assert status == 409 and result["error"] == "registry_reconciliation_required"
    assert_readonly(mounted, before)


@pytest.mark.parametrize("method,path,body", [("GET", "/v1/models?x=1", None),
    ("POST", "/v1/models?dry_run=0", {}), ("POST", "/v1/models?dry_run=1", []),
    ("DELETE", "/v1/models/saved?dry_run=1", {}), ("DELETE", "/v1/models/%2e%2e?dry_run=1", None)])
def test_malformed_requests_fail_without_mutation(mounted, method, path, body):
    before = mounted.files(), mounted.scheduler.events_since(0)
    assert request(mounted.address, method, path, body)[0] == 400
    assert_readonly(mounted, before)


def test_unconfigured_and_unsafe_file_are_explicit_errors(mounted):
    original = mounted.scheduler.registry
    mounted.scheduler.registry = None
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_not_configured"})
    mounted.scheduler.registry = original
    mounted.path.unlink()
    mounted.path.symlink_to(mounted.weights / "config.json")
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})


@pytest.mark.parametrize("registry", [None, [], {"config_path": "/tmp/config"},
    {"config_path": "relative", "shared_roots": ["/tmp"], "daemon_port_range": [1, 2]},
    {"config_path": "/tmp/config", "shared_roots": [], "daemon_port_range": [1, 2]},
    {"config_path": "/tmp/config", "shared_roots": ["/tmp"], "daemon_port_range": [2, 1]},
    {"config_path": "/tmp/config", "shared_roots": ["/tmp"], "daemon_port_range": [True, 2]},
    {"config_path": "/tmp/config", "shared_roots": ["/tmp"], "daemon_port_range": [1, 2], "reserved_ports": [False]},
    {"config_path": "/tmp/config", "shared_roots": ["/tmp"], "daemon_port_range": [1, 2], "writes_enabled": True}])
def test_registry_config_rejects_ambiguous_or_write_enabling_settings(registry):
    with pytest.raises(ValueError, match="registry"):
        SchedulerConfig("127.0.0.1", 19001, registry=registry)


def test_entrypoint_mounts_registry_without_a_reload_worker(mounted, monkeypatch):
    import llmsvc.__main__ as entry
    from llmsvc.reload import ReloadQueue
    built = []
    config = mounted.scheduler.config
    original = entry.Scheduler
    def scheduler(*args, **kwargs):
        result = original(*args, **kwargs, clock=lambda: mounted.clock[0])
        built.append(result)
        return result
    monkeypatch.setattr(entry, "Scheduler", scheduler)
    monkeypatch.setattr(entry, "load_config", lambda path: config)
    monkeypatch.setattr(entry, "build_collector", lambda config: mounted.scheduler.collect)
    monkeypatch.setattr(entry, "build_event_relay", lambda config: None)
    def forbidden(*args, **kwargs):
        pytest.fail("Registry bootstrap/--once called a reload worker")
    monkeypatch.setattr(ReloadQueue, "process_once", forbidden)
    monkeypatch.setattr("sys.argv", ["llmsvc", "--config", "fixture", "--once"])
    before = mounted.files()
    assert entry.main() == 0
    assert isinstance(built[0].registry, ModelRegistry)
    assert built[0].registry.records() == mounted.records and not built[0].registry.queue._jobs
    assert mounted.files() == before


def test_configured_collector_daemon_port_is_reserved_in_preview(mounted):
    mounted.scheduler.config = replace(mounted.scheduler.config, collectors={"models": {"local": {"port": 8105}}})
    registry = build_registry(mounted.scheduler.config, mounted.scheduler)
    registry.daemon_port_range = (8101, 8105)
    mounted.scheduler.registry = registry
    status, result = request(mounted.address, "POST", "/v1/models?dry_run=1",
                            {"name": "new", "path": str(mounted.weights), "base": "base"})
    assert status == 400 and "port" in result["message"]
    assert not registry.queue._pending and not registry.queue._jobs


def test_oversized_registry_body_uses_existing_http_limit(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, result = request(mounted.address, "POST", "/v1/models?dry_run=1", {"name": "x"*65536})
    assert status == 413 and result["error"] == "request_too_large"
    assert_readonly(mounted, before)


def test_invalid_or_non_json_registry_source_is_unavailable_not_a_broken_response(mounted):
    source = yaml.safe_load(mounted.path.read_bytes())
    source["models"]["saved"]["metadata"]["llmsvc_registry"]["unexpected"] = {"not-json"}
    mounted.path.write_text(yaml.safe_dump(source))
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})
    mounted.path.write_text("models: [invalid\n")
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})


def test_deeply_nested_source_is_an_explicit_unavailable_response(mounted):
    mounted.path.write_text("models: " + "[" * 2000 + "0" + "]" * 2000 + "\n")
    before = mounted.files(), mounted.scheduler.events_since(0)
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})
    assert_readonly(mounted, before)


@pytest.mark.parametrize("operation", ["models", "add", "rm"])
def test_copied_stdlib_cli_uses_actual_mounted_registry_without_effects(mounted, tmp_path, operation):
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    outside = tmp_path / "outside-cli"
    outside.mkdir()
    executable = outside / "llm"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cli" / "llm", executable)
    words = {"models": ["models", "--json"],
             "add": ["add", str(mounted.weights), "--name", "new", "--base", "base", "--dry-run", "--json"],
             "rm": ["rm", "saved", "--dry-run", "--json"]}[operation]
    before = mounted.files(), mounted.scheduler.events_since(0)
    run = subprocess.run([sys.executable, "-I", "-S", str(executable), "--url",
                          "http://127.0.0.1:"+str(mounted.address[1]), *words],
                         cwd=outside, env={**os.environ, "XDG_CONFIG_HOME": str(outside / "config")},
                         capture_output=True, text=True, timeout=5)
    assert run.returncode == (0 if operation == "models" else 1), (run.stdout, run.stderr)
    body = json.loads(run.stdout)
    if operation == "models":
        assert body["records"] == mounted.records and body["writes_enabled"] is False
    else:
        assert body["would"] and body["config_committed"] is False and body["dry_run"] is True
    assert any(b["reason"] == "inflight_stream_unknown" for b in body["blocked_by"])
    assert_readonly(mounted, before)


@pytest.mark.parametrize("key", ["config_max_bytes", "model_config_max_bytes", "weight_index_max_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, None, "1024", 16777217])
def test_registry_config_rejects_invalid_or_unlimited_source_caps(key, value):
    with pytest.raises(ValueError, match=key):
        SchedulerConfig("127.0.0.1", 19001, registry={"config_path": "/tmp/config.yaml",
            "shared_roots": ["/tmp/models"], "daemon_port_range": [19002, 19003], key: value})


@pytest.mark.parametrize("key", ["config_max_bytes", "model_config_max_bytes", "weight_index_max_bytes"])
def test_configured_source_cap_reaches_actual_http_preview(key, mounted):
    target = mounted.path if key == "config_max_bytes" else mounted.weights / "config.json"
    if key == "weight_index_max_bytes":
        target = mounted.weights / "model.safetensors.index.json"
        target.write_text('{"weight_map":{"layer":"model.safetensors"}}')
    before = mounted.files(), mounted.scheduler.events_since(0)
    body = {"name": "new", "path": str(mounted.weights), "base": "base"}
    for cap, expected in [(target.stat().st_size, 200), (target.stat().st_size - 1, 503 if key == "config_max_bytes" else 400)]:
        config = replace(mounted.scheduler.config, registry={**mounted.scheduler.config.registry, key: cap})
        mounted.scheduler.config = config
        mounted.registry = mounted.scheduler.registry = build_registry(config, mounted.scheduler)
        status, result = request(mounted.address, "POST", "/v1/models?dry_run=1", body)
        assert status == expected, result
        if expected == 200:
            assert result["config_committed"] is False and result["would"]
        else:
            assert result["error"] == ("registry_unavailable" if key == "config_max_bytes" else "registry_invalid_request")
        assert_readonly(mounted, before)


def test_unreadable_list_source_with_pending_marker_is_still_503(mounted):
    mounted.registry.queue.config_max_bytes = 1
    mounted.registry.queue.marker.write_text("pending fixture")
    before = mounted.files(), mounted.scheduler.events_since(0)
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})
    assert request(mounted.address, "POST", "/v1/models?dry_run=1",
                   {"name": "new", "path": str(mounted.weights), "base": "base"}) == (409, {"error": "registry_reconciliation_required"})
    assert_readonly(mounted, before)
