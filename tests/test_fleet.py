# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Synthetic fleet ingestion, durable history, state replay and identity contracts."""

import copy
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.fleet import FleetError
from llmsvc.fleet.controller import FleetController
from llmsvc.fleet.ingest import read_json, validate_snapshot
from llmsvc.fleet.store import FleetStore
from llmsvc.policy.fleet import container_summary, service_status
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import StateSnapshot

BASE = 1_800_003_600.0


def service(instance_id="instance-a", container="ctr-a", **updates):
    row = {"id": instance_id, "engine": "vllm", "engine_version": "0.30.0", "container": container,
           "host": False, "managed_by": None, "pid": 1234 if instance_id == "instance-a" else 4321,
           "started_at": BASE - 30 * 86400, "bind": "127.0.0.1", "port": 8010,
           "model": "example-model", "model_path": "/models/example", "argv_redacted": "vllm serve example-model",
           "gpus": [{"index": 2, "used_mib": 10240}], "gpu_observation_complete": True,
           "metrics": {"requests_total": 100, "generation_tokens_total": 1000, "prompt_tokens_total": 3000,
                       "prompt_tokens_cached_total": 500, "num_requests_running": 0, "num_requests_waiting": 0,
                       "kv_cache_usage_perc": 0}, "ollama": None,
           "scrape": {"ok": True, "error": None, "duration_ms": 10}}
    row.update(updates)
    return row


def snapshot(ts=BASE, services=None, **updates):
    row = {"schema_version": 1, "generated_at": ts, "scan_duration_ms": 20, "sample_interval_seconds": 60,
           "host": {"gpu_count": 1}, "inventory_complete": True, "gpu_inventory_complete": True,
           "gpu_attribution_complete": True,
           "gpus": [{"index": 2, "uuid": "GPU-example", "total_mib": 100 * 1024, "used_mib": 30 * 1024, "util_percent": 20}],
           "services": [service()] if services is None else services,
           "other_gpu_processes": [{"container": "ctr-b", "pid": 5678, "gpu": 2, "used_mib": 20 * 1024, "comm": "python"}],
           "errors": []}
    row.update(updates)
    return row


def config(tmp_path, **options):
    return SchedulerConfig("127.0.0.1", 8011, fleet_enabled=True,
        fleet_snapshot_path=str(tmp_path / "fleet.json"), fleet_db_path=str(tmp_path / "fleet.sqlite"),
        collectors={"ip_containers_path": str(tmp_path / "ip-containers.json")}, **options)


def write_snapshot(settings, data):
    path = Path(settings.fleet_snapshot_path)
    path.write_text(json.dumps(data))
    path.chmod(0o644)


def write_mapping(settings, now=BASE, containers=None):
    path = Path(settings.collectors["ip_containers_path"])
    path.write_text(json.dumps({"generated_at": now,
        "containers": containers or {"127.0.0.1": "ctr-a", "::ffff:192.0.2.2": "ctr-b"}}))
    path.chmod(0o644)


@pytest.fixture
def controller(tmp_path):
    settings = config(tmp_path)
    now = [BASE]
    events = []
    result = FleetController(settings, clock=lambda: now[0], emit=lambda kind, **values: events.append((kind, values)))
    write_mapping(settings)
    try:
        yield result, now, events
    finally:
        result.close()


def ingest(controller, now, data):
    now[0] = data["generated_at"]
    write_snapshot(controller.config, data)
    controller.ingest_once()
    assert controller.last_error is None


def test_replay_status_priority_and_observed_idle():
    scenarios = json.loads((Path(__file__).parent / "fixtures/fleet_replay.json").read_text())["scenarios"]
    started = time.monotonic()
    settings = SchedulerConfig("127.0.0.1", 8011)
    for case in scenarios:
        now = BASE + case["offset"]
        state = {"supported": case.get("supported", True), "failure_streak": case.get("failure_streak", 0),
                 "latest": {"active": case["active"], "scrape_ok": case.get("scrape_ok", 1), "counter_reset": case.get("counter_reset", 0)},
                 "idle_observed_seconds": case["observed_idle"]}
        if "last_active_offset" in case:
            state["last_active_at"] = now - case["last_active_offset"]
        row = service(bind=case.get("bind", "127.0.0.1"), host=case.get("host", False),
                      container=None if case.get("host") else "ctr-a")
        instance = {**row, "metadata": row, "state": state, "first_seen": BASE,
                    "last_seen": now - 60 if case.get("missing") else now}
        claim = {"until": now + 100, "revoked_at": None} if case.get("claim") else None
        result = service_status(instance, claim, settings, now, stale=case.get("stale", False), generated_at=now)
        assert result["status"] == case["expected"], case["name"]
        if case["name"] == "newly_observed_long_uptime":
            assert result["last_active_at"] is None and result["idle_seconds"] == 0
            assert result["never_active"] is True
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("bind,mapping,host_ips,host,address,access", [
    ("127.0.0.1", {}, [], False, "http://127.0.0.1:8010", "local_only"),
    ("::1", {}, [], False, "http://[::1]:8010", "local_only"),
    ("::ffff:127.0.0.1", {}, [], False, "http://[::ffff:7f00:1]:8010", "local_only"),
    ("192.0.2.10", {}, [], False, "http://192.0.2.10:8010", "direct"),
    ("2001:db8::10", {}, [], False, "http://[2001:db8::10]:8010", "direct"),
    ("fe80::123", {}, [], False, None, "unknown"),
    ("0.0.0.0", {"192.0.2.10": "ctr-a"}, [], False, "http://192.0.2.10:8010", "shared"),
    ("0.0.0.0", {"::ffff:192.0.2.10": "ctr-a"}, [], False, "http://192.0.2.10:8010", "shared"),
    ("0.0.0.0", {"192.0.2.20": "ctr-a", "192.0.2.10": "ctr-a"}, [], False, "http://192.0.2.10:8010", "shared"),
    ("::", {"192.0.2.10": "ctr-a", "2001:db8::10": "ctr-a"}, [], False, "http://[2001:db8::10]:8010", "shared"),
    ("::", {"192.0.2.10": "ctr-a"}, [], False, None, "shared"),
    ("0.0.0.0", {"192.0.2.10": "ctr-b", "127.0.0.1": "ctr-a"}, [], False, None, "shared"),
    ("0.0.0.0", {}, ["127.0.0.1", "::1", "192.0.2.20", "192.0.2.10"], True, "http://192.0.2.20:8010", "shared"),
    ("::", {}, ["127.0.0.1", "::1", "2001:db8::10"], True, "http://[2001:db8::10]:8010", "shared"),
    ("0.0.0.0", {}, ["127.0.0.1", "::1"], True, None, "shared"),
    ("0.0.0.0", {}, ["::ffff:127.0.0.1", "::ffff:192.0.2.20"], True, "http://192.0.2.20:8010", "shared"),
    ("::", {}, ["::ffff:192.0.2.20"], True, None, "shared"),
])
def test_service_invocation_metadata_uses_verified_listener_and_owner_addresses(
        controller, bind, mapping, host_ips, host, address, access):
    worker, now, _ = controller
    worker.config = replace(worker.config, collectors={**worker.config.collectors, "host_ips": host_ips})
    write_mapping(worker.config, containers=mapping)
    row = service(bind=bind, host=host, container=None if host else "ctr-a", listener_observation_complete=True,
                  model="http://198.51.100.99:9999/do-not-use", api_address="http://198.51.100.99:9999",
                  api_access="shared", idle_time_sensitive=False)
    ingest(worker, now, snapshot(services=[row]))
    result = worker.report()["services"][0]
    assert (result["api_address"], result["api_access"]) == (address, access)
    assert result["idle_time_sensitive"] is (access != "shared")
    assert result["status"] == "idle"


@pytest.mark.parametrize("failure", ["missing", "stale", "future", "conflict", "unreadable"])
def test_unavailable_owner_map_keeps_wildcard_address_unknown(controller, failure):
    worker, now, _ = controller
    ingest(worker, now, snapshot(services=[service(bind="0.0.0.0", listener_observation_complete=True)]))
    path = Path(worker.config.collectors["ip_containers_path"])
    if failure == "missing":
        path.unlink()
    elif failure == "unreadable":
        path.write_text("invalid-json")
    elif failure == "conflict":
        write_mapping(worker.config, containers={"192.0.2.10": "ctr-a", "::ffff:192.0.2.10": "ctr-b"})
    else:
        write_mapping(worker.config, now=BASE - 181 if failure == "stale" else BASE + 1,
                      containers={"192.0.2.10": "ctr-a"})
    result = worker.report()["services"][0]
    assert result["api_address"] is None
    assert result["api_access"] == "shared" and result["idle_time_sensitive"] is False
    assert result["status"] == "idle"


@pytest.mark.parametrize("failure", ["unverified", "stale", "missing_discovery", "legacy_failed_scrape"])
def test_unavailable_listener_metadata_is_unknown(controller, failure):
    worker, now, _ = controller
    row = service(bind="0.0.0.0", listener_observation_complete=failure != "unverified")
    if failure == "legacy_failed_scrape":
        row.pop("listener_observation_complete")
        row["scrape"] = {"ok": False, "error": "scrape_unavailable"}
    ingest(worker, now, snapshot(services=[row]))
    if failure == "stale":
        now[0] += worker.config.fleet_stale_after_seconds + 1
    elif failure == "missing_discovery":
        ingest(worker, now, snapshot(BASE + 60, services=[], inventory_complete=False))
    result = worker.report()["services"][0]
    assert result["api_address"] is None and result["api_access"] == "unknown"
    assert result["idle_time_sensitive"] is True


def test_legacy_successful_schema_one_listener_remains_compatible(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    result = worker.report()["services"][0]
    assert result["api_address"] == "http://127.0.0.1:8010"
    assert result["api_access"] == "local_only" and result["idle_time_sensitive"] is True
    with pytest.raises(ValueError, match="invalid_fleet_listener_observation"):
        validate_snapshot(snapshot(services=[service(listener_observation_complete="true")]))


@pytest.mark.parametrize("ipv6_only,address", [
    (False, "http://192.0.2.10:8010"), (True, None), (None, None),
])
@pytest.mark.parametrize("host", [False, True])
@pytest.mark.parametrize("host_ip", ["192.0.2.10", "::ffff:192.0.2.10"])
def test_ipv6_wildcard_ipv4_uri_requires_observed_dual_stack(controller, ipv6_only, address, host, host_ip):
    worker, now, _ = controller
    worker.config = replace(worker.config, collectors={**worker.config.collectors, "host_ips": [host_ip]})
    write_mapping(worker.config, containers={"192.0.2.10": "ctr-a"})
    ingest(worker, now, snapshot(services=[service(bind="::", host=host, container=None if host else "ctr-a",
        listener_observation_complete=True, listener_ipv6_only=ipv6_only)]))
    result = worker.report()["services"][0]
    assert result["api_address"] == address
    assert result["api_access"] == "shared" and result["idle_time_sensitive"] is False
    with pytest.raises(ValueError, match="invalid_fleet_listener_ipv6_only"):
        validate_snapshot(snapshot(services=[service(listener_ipv6_only=0)]))


def test_ingestion_deltas_durable_dedupe_and_rollups(controller):
    worker, now, events = controller
    first = snapshot()
    ingest(worker, now, first)
    initial = worker.report()["services"][0]
    assert initial["idle_seconds"] == 0 and initial["window_24h"]["active_minutes"] is None
    second = snapshot(BASE + 60)
    second["services"][0]["metrics"].update(requests_total=103, generation_tokens_total=1300, prompt_tokens_total=3100)
    ingest(worker, now, second)
    result = worker.report()
    row = result["services"][0]
    assert row["status"] == "active"
    assert row["window_24h"]["requests"] == 3
    assert row["window_24h"]["gen_tokens"] == 300
    assert row["window_24h"]["active_minutes"] == 1
    assert row["window_24h"]["observed_seconds"] == 60
    assert row["window_24h"]["observed_active_ratio"] == 1
    assert row["window_24h"]["active_ratio"] == pytest.approx(60 / 86400)
    assert row["window_24h"]["coverage_ratio"] == pytest.approx(60 / 86400)
    assert result["gpus"][0]["occupants"] == [
        {"container": "ctr-a", "kind": "llm", "used_gb": 10, "service_id": "instance-a",
         "host": False, "host_uid": None, "host_user": None},
        {"container": "ctr-b", "kind": "other", "used_gb": 20, "service_id": None,
         "host": False, "host_uid": None, "host_user": None}]
    assert "argv_redacted" not in row and "model_path" not in row
    assert events[-1][1]["detail"] == {"service_id": "instance-a", "from": "idle", "to": "active"}
    worker.ingest_once()
    worker.store.close()
    worker.store = FleetStore(worker.config.fleet_db_path)
    worker.ingest_once()
    assert worker.report()["services"][0]["window_24h"]["requests"] == 3
    ingest(worker, now, first)  # An older, still-fresh export cannot rewind state.
    assert worker.store.metadata()["generated_at"] == BASE + 60
    third = snapshot(BASE + 120)
    third["services"][0]["metrics"].update(requests_total=105, generation_tokens_total=1500, prompt_tokens_total=3150)
    ingest(worker, now, third)
    assert worker.report()["services"][0]["window_24h"]["requests"] == 5
    assert len(worker.history("instance-a", 24)["samples"]) == 3
    assert len(worker.history("instance-a", 168)["samples"]) == 169


def test_failures_preserve_counter_baseline_and_unknown_coverage(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    failed = snapshot(BASE + 60)
    failed["services"][0]["scrape"] = {"ok": False, "error": "timeout"}
    ingest(worker, now, failed)
    row = worker.report()["services"][0]
    assert row["status"] == "unknown" and row["idle_seconds"] is None
    success = snapshot(BASE + 120)
    success["services"][0]["metrics"].update(requests_total=107, generation_tokens_total=1200)
    ingest(worker, now, success)
    row = worker.report()["services"][0]
    assert row["window_24h"]["requests"] == 7
    assert row["window_24h"]["gen_tokens"] == 200
    assert row["window_24h"]["active_minutes"] is None
    assert row["status"] == "unknown" and row["last_active_at"] is None
    next_data = copy.deepcopy(success)
    next_data["generated_at"] += 60
    ingest(worker, now, next_data)
    assert worker.report()["services"][0]["idle_seconds"] == 0
    assert worker.report()["services"][0]["window_24h"]["observed_seconds"] == 60


@pytest.mark.parametrize("reset_kind", ["negative", "created_epoch", "series_set"])
def test_counter_epochs_reset_without_false_activity(controller, reset_kind):
    worker, now, _ = controller
    first = snapshot()
    first["services"][0]["metrics"]["requests_created"] = BASE - 10
    first["services"][0]["metrics_series_id"] = "series-a"
    ingest(worker, now, first)
    second = copy.deepcopy(first)
    second["generated_at"] += 60
    if reset_kind == "negative":
        second["services"][0]["metrics"].update(requests_total=1, generation_tokens_total=2)
    elif reset_kind == "created_epoch":
        second["services"][0]["metrics"].update(requests_total=500, requests_created=BASE + 30)
    else:
        second["services"][0]["metrics_series_id"] = "series-b"
        second["services"][0]["metrics"].update(requests_total=500, generation_tokens_total=5000)
    ingest(worker, now, second)
    last = worker.history("instance-a", 24)["samples"][-1]
    assert last["counter_reset"] == 1 and last["d_requests"] == 0
    assert last["active"] == 0 and last["observed_seconds"] == 0
    assert worker.report()["services"][0]["last_active_at"] is None


def test_gap_retains_differences_without_minute_attribution(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    after = snapshot(BASE + 1200)
    after["services"][0]["metrics"].update(requests_total=130, generation_tokens_total=2000)
    ingest(worker, now, after)
    point = worker.history("instance-a", 24)["samples"][-1]
    assert point["gap"] == 1 and point["d_requests"] == 30 and point["d_gen_tokens"] == 1000
    assert point["active_minutes"] is None
    result = worker.report()["services"][0]
    assert result["status"] == "unknown" and result["last_active_at"] is None
    assert result["hourly_active_24h"] == [None] * 24


def test_partial_discovery_never_ends_missing_instance(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    ingest(worker, now, snapshot(BASE + 60, [], inventory_complete=False, errors=["process_budget_exhausted"]))
    result = worker.report()["services"][0]
    assert result["status"] == "unknown" and result["idle_seconds"] is None
    assert worker.store.instance("instance-a")["ended_at"] is None
    ingest(worker, now, snapshot(BASE + 120, []))
    assert worker.report()["services"] == []
    assert worker.store.instance("instance-a")["ended_at"] == BASE + 120


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_stale_discovery_does_not_certify_old_gpu_memory(controller, missing):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    initial = worker.report()["services"][0]
    assert initial["gpu_gb"] == 10 and initial["gpu_observation_complete"] is True
    if missing:
        ingest(worker, now, snapshot(BASE + 60, [], inventory_complete=False,
            gpu_inventory_complete=False, gpu_attribution_complete=False, gpus=[]))
    else:
        now[0] += worker.config.fleet_stale_after_seconds + 1
    result = worker.report()
    row = result["services"][0]
    assert row["status"] == "unknown"
    assert row["last_seen"] == BASE
    assert row["gpu_gb"] is None and row["gpu_observation_complete"] is False
    assert result["containers"][0]["gpu_gb"] is None


def test_identity_collision_rolls_back_watermark_and_counts(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    collision = snapshot(BASE + 60)
    collision["services"][0]["pid"] = 9999
    write_snapshot(worker.config, collision)
    now[0] = BASE + 60
    worker.ingest_once()
    assert worker.last_error is not None
    assert worker.store.metadata()["generated_at"] == BASE
    assert worker.store.instance("instance-a")["pid"] == 1234


def test_partial_missing_discovery_breaks_activity_interval(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    ingest(worker, now, snapshot(BASE + 60, [], inventory_complete=False))
    later = snapshot(BASE + 120)
    later["services"][0]["metrics"]["requests_total"] += 4
    ingest(worker, now, later)
    result = worker.report()["services"][0]
    assert result["window_24h"]["requests"] == 4
    assert result["window_24h"]["observed_seconds"] == 0
    assert result["last_active_at"] is None and result["status"] == "unknown"
    assert worker.history("instance-a", 24)["samples"][1]["active"] is None


def test_missing_metrics_and_unsupported_engines_are_unknown(controller):
    worker, now, _ = controller
    missing = service(metrics={})
    unsupported = service("instance-b", "ctr-b", engine="sglang", metrics=None)
    ingest(worker, now, snapshot(services=[missing, unsupported]))
    for result in worker.report()["services"]:
        assert result["status"] == "unknown" and result["last_active_at"] is None
        assert result["idle_seconds"] is None
        assert result["window_24h"]["gen_tokens"] is None


def test_bad_database_is_unavailable_without_overwriting_it(controller):
    worker, _, _ = controller
    path = Path(worker.config.fleet_db_path)
    path.write_bytes(b"unrelated database contents")
    before = path.read_bytes()
    with pytest.raises(FleetError) as error:
        worker.report()
    assert error.value.status == 503 and error.value.error == "fleet_store_unavailable"
    assert path.read_bytes() == before


def test_history_bounds_keep_partial_hours_and_unobserved_buckets(controller):
    worker, now, _ = controller
    data = snapshot(BASE + 30)
    ingest(worker, now, data)
    later = copy.deepcopy(data)
    later["generated_at"] += 60
    later["services"][0]["metrics"]["requests_total"] += 3
    ingest(worker, now, later)
    result = worker.history("instance-a", 168)
    assert result["start_at"] == now[0] - 168 * 3600 and result["end_at"] == now[0]
    assert len(result["samples"]) == 169
    assert result["samples"][0]["partial"] is True and result["samples"][0]["active_minutes"] is None
    assert result["samples"][-1]["active_minutes"] == 1
    assert result["samples"][-1]["observed_seconds"] == 60
    assert result["samples"][-1]["requests"] == 3
    assert all(row["ts"] == row["hour_ts"] for row in result["samples"])


def test_ollama_expiry_proxy_never_reports_tokens(controller):
    worker, now, _ = controller
    model = service(engine="ollama", metrics=None, ollama={"models": [{"name": "example", "expires_at": BASE + 300}]})
    ingest(worker, now, snapshot(services=[model]))
    second = copy.deepcopy(model)
    second["ollama"]["models"][0]["expires_at"] += 60
    ingest(worker, now, snapshot(BASE + 60, [second]))
    result = worker.report()["services"][0]
    assert result["status"] == "active" and result["window_24h"]["active_minutes"] == 1
    for key in ("requests", "gen_tokens", "prompt_tokens"):
        assert result["window_24h"][key] is None


def test_stale_and_read_error_override_current_state(controller):
    worker, now, events = controller
    ingest(worker, now, snapshot())
    now[0] += worker.config.fleet_stale_after_seconds + 1
    result = worker.report()
    assert result["stale"] is True and result["services"][0]["status"] == "unknown"
    worker.ingest_once()
    assert events[-1][1]["detail"]["to"] == "unknown"
    Path(worker.config.fleet_snapshot_path).unlink()
    now[0] = BASE + 1
    worker.ingest_once()
    assert worker.report()["stale"] is True
    assert "fleet_snapshot_unavailable" in worker.report()["errors"]


def test_retention_keeps_hourly_and_does_not_double_count(tmp_path):
    settings = config(tmp_path, fleet_raw_retention_days=1, fleet_hourly_retention_days=180)
    store = FleetStore(settings.fleet_db_path)
    try:
        for offset in (0, 60, 120):
            data = snapshot(BASE + offset)
            data["services"][0]["metrics"]["requests_total"] = 100 + offset // 60
            store.ingest(data, settings, data["generated_at"])
            store.retention_tick(settings, data["generated_at"])
        counts = store.window(BASE - 30, BASE + 121)["instance-a"]
        assert counts["requests"] == 2 and counts["observed_seconds"] == 120
        future = snapshot(BASE + 3 * 86400)
        future["services"][0]["metrics"]["requests_total"] = 110
        store.ingest(future, settings, future["generated_at"])
        store.retention_tick(settings, future["generated_at"])
        assert len(store.raw("instance-a", BASE - 1, BASE + 3 * 86400)) == 1
        counts = store.window(BASE - 3600, BASE + 3 * 86400 + 1)["instance-a"]
        assert counts["requests"] == 10 and counts["observed_seconds"] == 120
        very_late = snapshot(BASE + 181 * 86400)
        store.ingest(very_late, settings, very_late["generated_at"])
        store.retention_tick(settings, very_late["generated_at"])
        assert store.hourly(BASE - 3600, BASE + 3600) == []
    finally:
        store.close()


@pytest.mark.parametrize("export", ["stale", "missing", "invalid", "duplicate"])
def test_retention_prunes_during_export_outage_without_rewriting_observations(tmp_path, export):
    settings = config(tmp_path, fleet_raw_retention_days=1, fleet_hourly_retention_days=2,
                      fleet_stale_after_seconds=4 * 86400 if export == "duplicate" else 180)
    now = [BASE]
    worker = FleetController(settings, clock=lambda: now[0], emit=lambda *args, **kwargs: None)
    try:
        write_mapping(settings)
        ingest(worker, now, snapshot())
        latest = snapshot(BASE + 60)
        latest["services"][0]["metrics"]["requests_total"] += 3
        ingest(worker, now, latest)
        receipt = worker.write_claim("claim", {"service_id": "instance-a", "until": BASE + 7 * 86400,
                                               "reason": "evaluation"}, source_ip="127.0.0.1")
        metadata = worker.store.metadata()
        instances = worker.store.instances(live=False)
        claim = worker.store.claim(receipt["claim"]["id"])
        if export == "missing":
            Path(settings.fleet_snapshot_path).unlink()
        elif export == "invalid":
            write_snapshot(settings, {"schema_version": 0})
        now[0] = BASE + 3 * 86400 + 123
        worker.ingest_once()
        for table in ("fleet_samples", "fleet_gpu_samples", "fleet_hourly"):
            assert worker.store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        retained = worker.store.metadata()
        assert retained["generated_at"] == metadata["generated_at"]
        assert retained["snapshot"] == metadata["snapshot"]
        assert retained["last_retention"] == now[0]
        assert retained["raw_cutoff"] == BASE + 2 * 86400
        assert worker.store.instances(live=False) == instances
        assert worker.store.claim(claim["id"]) == claim
        assert worker.last_error == ("fleet_snapshot_unavailable" if export in {"missing", "invalid"} else None)
        assert worker.report()["services"][0]["status"] == ("claimed" if export == "duplicate" else "unknown")
    finally:
        worker.close()


def test_retention_hourly_cadence_survives_restart_and_clips_whole_hours(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    cutoff = BASE - worker.config.fleet_raw_retention_days * 86400
    hourly_cutoff = BASE - worker.config.fleet_hourly_retention_days * 86400
    with worker.store._db as db:
        for ts in (cutoff + 3599, cutoff + 3600):
            db.execute("INSERT INTO fleet_gpu_samples VALUES(?,?,?,?,?,?)", (ts, 2, 1, 0, 1, 0))
        for hour in (hourly_cutoff, hourly_cutoff + 3600):
            db.execute("INSERT INTO fleet_hourly VALUES(?,?,?,?,?,?,?,?,?)", ("instance-a", hour, 1, 1, 1, 1, 1, 1, 60))
    worker.store.close()
    worker.store = FleetStore(worker.config.fleet_db_path)
    now[0] = BASE + 3599
    worker.ingest_once()
    assert worker.store.metadata()["last_retention"] == BASE
    assert worker.store._writable is False
    assert worker.store._db.execute("SELECT COUNT(*) FROM fleet_gpu_samples WHERE ts<?", (BASE,)).fetchone()[0] == 2
    now[0] = BASE + 3600 + 123
    worker.ingest_once()
    assert worker.store.metadata()["last_retention"] == now[0]
    assert worker.store.metadata()["raw_cutoff"] == cutoff + 3600
    assert [row[0] for row in worker.store._db.execute("SELECT ts FROM fleet_gpu_samples WHERE ts<?", (BASE,))] == [cutoff + 3600]
    assert [row["hour_ts"] for row in worker.store.hourly(hourly_cutoff, hourly_cutoff + 7200)] == [hourly_cutoff + 3600]
    statements = []
    worker.store._db.set_trace_callback(statements.append)
    now[0] += 3599
    worker.ingest_once()
    assert worker.store.metadata()["last_retention"] == now[0] - 3599
    assert not any(sql.startswith(("DELETE", "BEGIN IMMEDIATE")) for sql in statements)


@pytest.mark.parametrize("export", ["stale", "missing", "invalid", "future"])
def test_retention_without_first_ingest_creates_no_database_or_directory(tmp_path, export):
    settings = config(tmp_path)
    settings = replace(settings, fleet_db_path=str(tmp_path / "uncreated" / "fleet.sqlite"))
    worker = FleetController(settings, clock=lambda: BASE, emit=lambda *args, **kwargs: None)
    try:
        if export == "stale":
            write_snapshot(settings, snapshot(BASE - 86400))
        elif export == "invalid":
            write_snapshot(settings, {"schema_version": 0})
        elif export == "future":
            write_snapshot(settings, snapshot(BASE + 86400))
        for _ in range(2):
            worker.ingest_once()
            assert worker.store.retention_tick(settings, BASE + 86400) is False
        assert worker.store._db is None
        assert worker.report()["stale"] is True
        assert not Path(settings.fleet_db_path).parent.exists()
    finally:
        worker.close()


@pytest.mark.parametrize("export", ["fresh", "missing"])
def test_retention_failure_rolls_back_cleanup_and_preserves_ingest_commit(tmp_path, export):
    settings = config(tmp_path, fleet_raw_retention_days=1, fleet_hourly_retention_days=2)
    now = [BASE]
    worker = FleetController(settings, clock=lambda: now[0], emit=lambda *args, **kwargs: None)
    try:
        ingest(worker, now, snapshot())
        with worker.store._db as db:
            db.execute("""CREATE TRIGGER reject_gpu_retention BEFORE DELETE ON fleet_gpu_samples
                          BEGIN SELECT RAISE(ABORT, 'retention_failed'); END""")
        now[0] = BASE + 3 * 86400
        if export == "fresh":
            latest = snapshot(now[0])
            latest["services"][0]["metrics"]["requests_total"] += 5
            write_snapshot(settings, latest)
        else:
            Path(settings.fleet_snapshot_path).unlink()
        worker.ingest_once()
        metadata = worker.store.metadata()
        assert metadata["last_retention"] == BASE
        assert metadata["raw_cutoff"] == BASE - 86400
        assert metadata["generated_at"] == (now[0] if export == "fresh" else BASE)
        for table in ("fleet_samples", "fleet_gpu_samples", "fleet_hourly"):
            assert worker.store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == (2 if export == "fresh" else 1)
        if export == "fresh":
            assert worker.store.raw("instance-a", now[0] - 1, now[0])[0]["d_requests"] == 5
        assert worker.last_error == ("fleet_store_unavailable" if export == "fresh" else "fleet_snapshot_unavailable")
        with worker.store._db as db:
            db.execute("DROP TRIGGER reject_gpu_retention")
        worker.ingest_once()
        assert worker.store.metadata()["last_retention"] == now[0]
        assert worker.store.metadata()["generated_at"] == metadata["generated_at"]
        assert worker.last_error == (None if export == "fresh" else "fleet_snapshot_unavailable")
        assert len(worker.store.raw("instance-a", BASE - 1, now[0])) == (1 if export == "fresh" else 0)
    finally:
        worker.close()


@pytest.mark.parametrize("mode", ["disabled", "sampling_only", "check_config", "once", "dry_run"])
def test_no_worker_modes_do_not_prune_existing_expired_history(tmp_path, mode):
    import yaml
    settings = config(tmp_path, fleet_raw_retention_days=1, fleet_hourly_retention_days=2)
    observed = time.time() - 3 * 86400
    data = snapshot(observed, [service(started_at=observed - 30 * 86400)])
    write_snapshot(settings, data)
    store = FleetStore(settings.fleet_db_path)
    try:
        store.ingest(data, settings, observed)
        store.retention_tick(settings, observed)
        before = store.metadata()
    finally:
        store.close()
    if mode in {"disabled", "sampling_only"}:
        scheduler = Scheduler(replace(settings, fleet_enabled=mode != "disabled"), lambda: StateSnapshot())
        scheduler.start(sampling_only=mode == "sampling_only")
        scheduler.stop()
    else:
        configuration = tmp_path / "scheduler.yaml"
        configuration.write_text(yaml.safe_dump({"listen_host": "127.0.0.1", "listen_port": 8011,
            "fleet_enabled": True, "fleet_snapshot_path": settings.fleet_snapshot_path,
            "fleet_db_path": settings.fleet_db_path, "fleet_raw_retention_days": 1, "fleet_hourly_retention_days": 2}))
        arguments = {"check_config": ["--check-config"], "once": ["--once"], "dry_run": ["--dry-run", "--once"]}[mode]
        result = subprocess.run([sys.executable, "-m", "llmsvc", "--config", str(configuration), *arguments],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    store = FleetStore(settings.fleet_db_path)
    try:
        assert store.metadata() == before
        for table in ("fleet_samples", "fleet_gpu_samples", "fleet_hourly"):
            assert store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        store.close()


def test_actual_interval_split_across_hour_and_capped(controller):
    worker, now, _ = controller
    start = int(BASE // 3600) * 3600 + 3590
    data = snapshot(start)
    ingest(worker, now, data)
    data = copy.deepcopy(data)
    data["generated_at"] += 60
    data["services"][0]["metrics"]["requests_total"] += 1
    ingest(worker, now, data)
    points = worker.store.hourly(start - 3600, start + 3600)
    assert [point["observed_seconds"] for point in points] == [10, 50]
    assert sum(point["active_minutes"] for point in points) == 1
    before = worker.store.window(start, start + 61)["instance-a"]
    assert before["observed_seconds"] == 60 and before["requests"] == 1
    data["generated_at"] += 240
    ingest(worker, now, data)
    last = worker.history("instance-a", 24)["samples"][-1]
    assert last["observed_seconds"] == 120


@pytest.mark.parametrize("interval", [180, 240, 300])
def test_long_observation_intervals_clip_hour_boundaries_exactly(controller, interval):
    worker, now, _ = controller
    start = BASE + 3500
    first = snapshot(start, sample_interval_seconds=interval)
    ingest(worker, now, first)
    second = copy.deepcopy(first)
    second["generated_at"] += interval
    second["services"][0]["metrics"].update(requests_total=101, generation_tokens_total=1100)
    ingest(worker, now, second)
    before = worker.store.window(start, BASE + 3600)["instance-a"]
    assert before["observed_seconds"] == 100
    assert before["active_minutes"] == pytest.approx(100 / 60)
    assert before["requests"] == 0
    after = worker.store.window(BASE + 3600, second["generated_at"] + 0.000001)["instance-a"]
    assert after["observed_seconds"] == interval - 100
    assert after["active_minutes"] == pytest.approx((interval - 100) / 60)
    assert after["requests"] == 1
    combined = worker.store.window(start, BASE + 7200)["instance-a"]
    assert combined["observed_seconds"] == interval
    assert combined["active_minutes"] == pytest.approx(interval / 60)
    assert combined["requests"] == 1
    hourly = worker.store.hourly(BASE, BASE + 7200, instance_id="instance-a")
    assert [row["observed_seconds"] for row in hourly] == [100, interval - 100]
    now[0] = start + 168 * 3600
    history = worker.history("instance-a", 168)
    assert history["samples"][0]["partial"] is True
    assert history["samples"][0]["observed_seconds"] == 100
    assert history["samples"][0]["active_minutes"] == pytest.approx(100 / 60)
    assert history["samples"][1]["observed_seconds"] == interval - 100
    assert sum(row["requests"] or 0 for row in history["samples"]) == 1


def test_hourly_history_boundary_counter_is_not_counted_twice(controller):
    worker, now, _ = controller
    start = BASE + 3500
    first = snapshot(start)
    ingest(worker, now, first)
    second = copy.deepcopy(first)
    second["generated_at"] = BASE + 3600
    second["services"][0]["metrics"]["requests_total"] += 1
    ingest(worker, now, second)
    now[0] = start + 168 * 3600
    history = worker.history("instance-a", 168)
    assert history["samples"][0]["observed_seconds"] == 100
    assert history["samples"][0]["requests"] == 0
    assert history["samples"][1]["requests"] == 1
    assert sum(row["requests"] or 0 for row in history["samples"]) == 1


def test_claims_socket_identity_dry_run_revoke_restart(controller, monkeypatch):
    worker, now, _ = controller
    ingest(worker, now, snapshot(services=[service(), service("instance-b", "ctr-b")]))
    body = {"service_id": "instance-a", "until": BASE + 3600, "reason": "evaluation", "by": "ctr-b", "container": "ctr-b"}
    before = worker.store.metadata()
    def no_id():
        raise AssertionError("dry-run allocated an ID")
    with monkeypatch.context() as patch:
        patch.setattr("llmsvc.fleet.controller.uuid.uuid4", no_id)
        receipt = worker.write_claim("claim", body, source_ip="127.0.0.1", dry_run=True)
    assert receipt["dry_run"] is True and "id" not in receipt["claim"]
    assert worker.store.claims(BASE) == {} and worker.store.metadata() == before
    receipt = worker.write_claim("claim", body, source_ip="127.0.0.1")
    assert receipt["claim"]["container"] == "ctr-a"
    assert receipt["claim"]["service_id"] == receipt["claim"]["instance_id"] == body["service_id"]
    claim_id = receipt["claim"]["id"]
    assert worker.report()["services"][0]["status"] == "claimed"
    worker.store.close()
    worker.store = FleetStore(worker.config.fleet_db_path)
    assert worker.report()["services"][0]["claim"]["id"] == claim_id
    with pytest.raises(FleetError, match="forbidden_container"):
        worker.write_claim("unclaim", {"id": claim_id}, source_ip="192.0.2.2")
    receipt = worker.write_claim("unclaim", {"id": claim_id}, source_ip="127.0.0.1", dry_run=True)
    assert receipt["claim"]["revoked_at"] == BASE
    assert worker.store.claim(claim_id)["revoked_at"] is None
    worker.write_claim("unclaim", {"id": claim_id}, source_ip="127.0.0.1")
    assert worker.report()["services"][0]["claim"] is None


@pytest.mark.parametrize("mapping", [None, {"generated_at": BASE - 181, "containers": {"127.0.0.1": "ctr-a"}},
    {"generated_at": BASE + 1, "containers": {"127.0.0.1": "ctr-a"}},
    {"generated_at": BASE, "containers": {"192.0.2.3": "ctr-a"}},
    {"generated_at": BASE, "containers": {"127.0.0.1": "ctr-a", "::ffff:127.0.0.1": "ctr-b"}}])
def test_missing_stale_ambiguous_map_fails_closed(controller, mapping):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    path = Path(worker.config.collectors["ip_containers_path"])
    if mapping is None:
        path.unlink()
    else:
        path.write_text(json.dumps(mapping))
    with pytest.raises(FleetError) as error:
        worker.write_claim("claim", {"service_id": "instance-a", "until": BASE + 10, "reason": "work"}, source_ip="127.0.0.1")
    assert error.value.status == 403
    with pytest.raises(FleetError):
        worker.report(source_ip="127.0.0.1", mine=True)
    assert worker.report(source_ip="127.0.0.1")["services"][0]["mine"] is False


def test_existing_ip_export_producer_matches_claim_identity_reader(controller, tmp_path):
    worker, now, _ = controller
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    fake_lxc = command_dir / "lxc"
    rows = [{"name": "ctr-a", "state": {"network": {"eth0": {"addresses": [
        {"family": "inet", "scope": "global", "address": "192.0.2.2"}]}}}}]
    fake_lxc.write_text("#!" + sys.executable + "\n# Generated-By: Codex / gpt-6.1-sol\nimport json\nprint(json.dumps(" + repr(rows) + "))\n")
    fake_lxc.chmod(0o755)
    script = Path(__file__).parents[1] / "deploy/host/llmsvc-export-ip-containers.sh"
    result = subprocess.run(["bash", str(script), str(tmp_path)], capture_output=True, text=True,
        env={**os.environ, "PATH": str(command_dir) + os.pathsep + os.environ["PATH"]}, timeout=5)
    assert result.returncode == 0, result.stderr
    now[0] = time.time()
    payload = json.loads(Path(worker.config.collectors["ip_containers_path"]).read_text())
    assert type(payload["generated_at"]) is int
    assert worker.owner_for_ip("192.0.2.2") == "ctr-a"


@pytest.mark.parametrize("change", [{"until": BASE}, {"until": BASE + 7 * 86400 + 1}, {"until": float("nan")},
    {"reason": ""}, {"reason": " "}, {"reason": "x" * 201}, {"service_id": []}, {"reason": "line\nbreak"}])
def test_claim_bounds_and_validation(controller, change):
    worker, now, _ = controller
    ingest(worker, now, snapshot())
    body = {"service_id": "instance-a", "until": BASE + 3600, "reason": "evaluation", **change}
    with pytest.raises(FleetError) as error:
        worker.write_claim("claim", body, source_ip="127.0.0.1")
    assert error.value.status == 400
    assert worker.store.claims(BASE) == {}


def test_first_dry_run_and_reads_do_not_create_database(controller):
    worker, _, _ = controller
    assert worker.report()["services"] == []
    assert not Path(worker.config.fleet_db_path).exists()
    with pytest.raises(FleetError):
        worker.write_claim("claim", {"service_id": "missing", "until": BASE + 1, "reason": "work"}, source_ip="127.0.0.1", dry_run=True)
    assert not Path(worker.config.fleet_db_path).exists()


def request(address, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def http_service(tmp_path):
    settings = config(tmp_path)
    scheduler = Scheduler(settings, lambda: StateSnapshot(), clock=lambda: BASE)
    write_mapping(settings)
    write_snapshot(settings, snapshot(services=[service(), service("instance-b", "ctr-b")]))
    scheduler.fleet.ingest_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield scheduler, server.server_address
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_http_shape_read_only_independent_claims_and_socket_peer(http_service):
    scheduler, address = http_service
    assert scheduler.config.read_only is True
    status, result = request(address, "GET", "/v1/fleet")
    assert status == 200 and result["schema_version"] == 1
    assert result["services"][0]["api_address"] == "http://127.0.0.1:8010"
    assert result["services"][0]["api_access"] == "local_only"
    assert result["services"][0]["idle_time_sensitive"] is True
    assert result["services"][0]["mine"] is True and result["services"][1]["mine"] is False
    assert request(address, "GET", "/v1/fleet?mine=1")[1]["containers"][0]["services"] == 1
    assert request(address, "GET", "/v1/fleet/history?service=instance-a&hours=168")[1]["resolution"] == "hourly"
    assert request(address, "GET", "/v1/fleet/history?service=instance-a&hours=48")[0] == 400
    assert request(address, "GET", "/v1/fleet?mine=1&mine=1")[0] == 400
    body = {"service_id": "instance-b", "until": BASE + 300, "reason": "work", "container": "ctr-b"}
    status, result = request(address, "POST", "/v1/fleet/claims", body, {"X-Forwarded-For": "192.0.2.2", "X-Real-IP": "192.0.2.2"})
    assert status == 403 and result["error"] == "forbidden_container"
    body["service_id"] = "instance-a"
    status, result = request(address, "POST", "/v1/fleet/claims?dry_run=1", body)
    assert status == 200 and "id" not in result["claim"]
    status, result = request(address, "POST", "/v1/fleet/claims", body)
    assert status == 200 and result["claim"]["container"] == "ctr-a"
    claim_id = result["claim"]["id"]
    status, result = request(address, "DELETE", "/v1/fleet/claims/" + claim_id + "?dry_run=1")
    assert status == 200 and result["dry_run"] is True and result["claim"]["id"] == claim_id
    assert request(address, "DELETE", "/v1/fleet/claims/" + claim_id)[0] == 200
    assert request(address, "POST", "/v1/free", {})[0] == 405
    scheduler.fleet.config = replace(scheduler.fleet.config, fleet_claims_enabled=False)
    assert request(address, "POST", "/v1/fleet/claims?dry_run=1", body)[0] == 405
    assert request(address, "GET", "/v1/fleet")[0] == 200


def test_http_sse_publishes_real_fleet_status_event(http_service):
    scheduler, address = http_service
    body = {"service_id": "instance-a", "until": BASE + 300, "reason": "evaluation"}
    assert request(address, "POST", "/v1/fleet/claims", body)[0] == 200
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request("GET", "/v1/events?since=0")
        response = connection.getresponse()
        assert response.status == 200
        lines = []
        while len(lines) < 6:
            line = response.fp.readline().decode("utf-8")
            lines.append(line)
            if line.startswith("data:"):
                data = json.loads(line[5:])
                assert data["kind"] == "fleet_status_changed"
                assert data["detail"] == {"service_id": "instance-a", "from": "idle", "to": "claimed"}
                break
        assert "event: fleet_status_changed\n" in lines
    finally:
        connection.close()
        scheduler.events_closed.set()
        with scheduler.changed:
            scheduler.changed.notify_all()


def test_claim_expiry_emits_status_change_without_duplicate_ingestion(controller):
    worker, now, events = controller
    ingest(worker, now, snapshot())
    receipt = worker.write_claim("claim", {"service_id": "instance-a", "until": BASE + 30, "reason": "evaluation"}, source_ip="127.0.0.1")
    now[0] = BASE + 60
    worker.ingest_once()
    assert worker.report()["services"][0]["claim"] is None
    assert events[-1][1]["detail"] == {"service_id": "instance-a", "from": "claimed", "to": "idle"}
    assert worker.store.claim(receipt["claim"]["id"])["revoked_at"] is None
    assert len(worker.history("instance-a", 24)["samples"]) == 1


@pytest.mark.parametrize("argument", ["--check-config", "--once"])
def test_entrypoint_validation_modes_have_no_fleet_writer(tmp_path, argument):
    import yaml
    settings = config(tmp_path)
    configuration = tmp_path / "scheduler.yaml"
    configuration.write_text(yaml.safe_dump({"listen_host": "127.0.0.1", "listen_port": 8011,
        "fleet_enabled": True, "fleet_snapshot_path": settings.fleet_snapshot_path, "fleet_db_path": settings.fleet_db_path}))
    write_snapshot(settings, snapshot())
    result = subprocess.run([sys.executable, "-m", "llmsvc", "--config", str(configuration), argument], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not Path(settings.fleet_db_path).exists()


def test_disabled_and_sampling_only_have_no_workers_or_database(tmp_path):
    settings = config(tmp_path)
    disabled = Scheduler(replace(settings, fleet_enabled=False), lambda: StateSnapshot())
    assert disabled.fleet is None
    with pytest.raises(FleetError, match="fleet_disabled"):
        disabled.fleet_report(source_ip="127.0.0.1")
    disabled.start()
    disabled.stop()
    assert not Path(settings.fleet_db_path).exists()
    enabled = Scheduler(settings, lambda: StateSnapshot())
    enabled.start(sampling_only=True)
    assert enabled.fleet.thread is None
    enabled.stop()
    assert not Path(settings.fleet_db_path).exists()


def test_worker_lifecycle_single_start_and_stop(tmp_path):
    settings = config(tmp_path, fleet_ingest_interval_seconds=0.01)
    write_snapshot(settings, snapshot())
    scheduler = Scheduler(settings, lambda: StateSnapshot(), clock=lambda: BASE)
    scheduler.start()
    deadline = time.monotonic() + 3
    while not Path(settings.fleet_db_path).exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        with pytest.raises(RuntimeError):
            scheduler.start()
        with pytest.raises(RuntimeError):
            scheduler.fleet.start()
        assert scheduler.fleet.thread.is_alive()
    finally:
        scheduler.stop()
    assert not scheduler.fleet.thread.is_alive()
    with sqlite3.connect(settings.fleet_db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM fleet_samples").fetchone()[0] == 1


def test_fleet_close_error_still_closes_collector_and_event_stream(tmp_path, monkeypatch):
    class Collector:
        closed = False
        def __call__(self):
            return StateSnapshot()
        def close(self):
            self.closed = True
    collector = Collector()
    scheduler = Scheduler(config(tmp_path), collector)
    original_close = scheduler.fleet.close
    def fail_close():
        original_close()
        raise RuntimeError("fleet_close_failed")
    monkeypatch.setattr(scheduler.fleet, "close", fail_close)
    with pytest.raises(RuntimeError, match="fleet_close_failed"):
        scheduler.stop()
    assert collector.closed is True and scheduler.events_closed.is_set()


@pytest.mark.parametrize("change", [{"schema_version": 2}, {"generated_at": float("nan")}, {"inventory_complete": "yes"}, {"sample_interval_seconds": 0}])
def test_snapshot_validation_rejects_malformed_input(change):
    with pytest.raises(ValueError):
        validate_snapshot(snapshot(**change))


def test_bounded_export_reader_rejects_size_symlink_fifo_and_duplicate_keys(tmp_path):
    path = tmp_path / "export.json"
    path.write_text('{"a":1,"a":2}')
    path.chmod(0o644)
    with pytest.raises(ValueError, match="duplicate_export_key"):
        read_json(path)
    path.write_text("x" * 101)
    with pytest.raises(ValueError, match="export_too_large"):
        read_json(path, 100)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_json(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="unsafe_export_file"):
        read_json(fifo)
    path.write_text("{}")
    path.chmod(0o666)
    with pytest.raises(ValueError, match="unsafe_export_file"):
        read_json(path)


@pytest.mark.parametrize("options", [{"fleet_enabled": 1}, {"fleet_db_path": "relative"}, {"fleet_ingest_interval_seconds": 0},
    {"fleet_stale_after_seconds": float("inf")}, {"fleet_claim_max_days": 8}, {"fleet_raw_retention_days": 0},
    {"fleet_raw_retention_days": 14, "fleet_hourly_retention_days": 7}, {"fleet_claims_enabled": "yes"}])
def test_fleet_config_validation(options):
    with pytest.raises(ValueError):
        SchedulerConfig("127.0.0.1", 8011, **options)


def test_actual_host_producer_snapshot_preserves_unknown_ownership(controller):
    worker, now, _ = controller
    data = json.loads((Path(__file__).parent / "fixtures/fleet_host_snapshot.json").read_text())
    ingest(worker, now, data)
    result = worker.report(source_ip="127.0.0.1")
    assert len(result["services"]) == 3
    host = next(row for row in result["services"] if row["host"])
    assert host["container"] is None and host["mine"] is False and host["gpu_gb"] is None
    ollama = next(row for row in result["services"] if row["engine"] == "ollama")
    assert ollama["model"] is None and ollama["window_24h"]["gen_tokens"] is None
    assert result["containers"][0]["container"] is None
    assert any(row["container"] is None for gpu in result["gpus"] for row in gpu["occupants"])
    assert result["gpu_attribution_complete"] is False
    write_mapping(worker.config, now=now[0])
    with pytest.raises(FleetError) as error:
        worker.write_claim("claim", {"service_id": host["id"], "until": now[0] + 300, "reason": "work"}, source_ip="127.0.0.1")
    assert error.value.status == 403


def test_gpu5_host_job_and_unresolved_process_keep_distinct_ownership(controller):
    worker, now, _ = controller
    processes = [
        {"container": None, "host": True, "host_uid": 61001, "host_user": "sample-host-a",
         "pid": 51001, "gpu": 5, "used_mib": 30 * 1024, "comm": "internal-process", "argv": "private-argument"},
        {"container": None, "host": None, "host_uid": None, "host_user": None,
         "pid": 51002, "gpu": 5, "used_mib": 10 * 1024, "comm": "unknown"},
    ]
    data = snapshot(services=[], other_gpu_processes=processes, gpu_attribution_complete=False,
        gpus=[{"index": 5, "uuid": "GPU-synthetic-five", "total_mib": 140 * 1024,
               "used_mib": 40 * 1024, "util_percent": 75}])
    ingest(worker, now, data)
    result = worker.report()
    card, = result["gpus"]
    assert card["index"] == 5 and card["used_gb"] == 40 and card["total_gb"] == 140
    assert card["occupants"] == [
        {"container": None, "kind": "other", "used_gb": 30, "service_id": None,
         "host": True, "host_uid": 61001, "host_user": "sample-host-a"},
        {"container": None, "kind": "other", "used_gb": 10, "service_id": None,
         "host": None, "host_uid": None, "host_user": None},
    ]
    assert result["services"] == [] and result["gpu_attribution_complete"] is False
    assert worker.store.metadata()["snapshot"]["other_gpu_processes"] == processes
    assert worker.store._db.execute("SELECT used_mib,other_mib FROM fleet_gpu_samples").fetchone()[:] == (40 * 1024, None)
    assert "internal-process" not in json.dumps(result) and "private-argument" not in json.dumps(result)


def test_host_llm_users_group_by_uid_and_survive_historical_detail(controller):
    worker, now, _ = controller
    rows = [service("host-a", None, host=True, pid=51001, host_uid=61001, host_user="sample-host-a"),
            service("host-a-extra", None, host=True, pid=51002, host_uid=61001, host_user="sample-host-a"),
            service("host-b", None, host=True, pid=51003, host_uid=61002, host_user=None),
            service("legacy-host", None, host=True, pid=51004),
            service("legacy-host-extra", None, host=True, pid=51005)]
    data = snapshot(services=rows, other_gpu_processes=[])
    data["gpus"][0]["used_mib"] = 50 * 1024
    ingest(worker, now, data)
    result = worker.report(source_ip="127.0.0.1")
    assert all(row["mine"] is False for row in result["services"])
    assert [(row["host_uid"], row["host_user"], row["services"], row["gpu_gb"])
            for row in result["containers"]] == [(None, None, 2, 20), (61001, "sample-host-a", 2, 20), (61002, None, 1, 10)]
    assert all(row["host"] is True and row["container"] is None for row in result["containers"])
    services = {row["id"]: row for row in result["services"]}
    occupants = {row["service_id"]: row for row in result["gpus"][0]["occupants"]}
    for source in rows:
        for projected in (services[source["id"]], occupants[source["id"]]):
            assert projected["host"] is True
            assert projected["host_uid"] == source.get("host_uid")
            assert projected["host_user"] == source.get("host_user")
    worker.store.close()
    worker.store = FleetStore(worker.config.fleet_db_path)
    ingest(worker, now, snapshot(BASE + 60, services=[], other_gpu_processes=[]))
    for hours in (24, 168):
        detail = worker.history("host-a", hours)["service"]
        assert detail["ended_at"] == BASE + 60
        assert (detail["host"], detail["host_uid"], detail["host_user"]) == (True, 61001, "sample-host-a")
    assert worker.report()["services"] == []


def test_legacy_schema1_ownership_remains_accepted(controller):
    worker, now, _ = controller
    data = snapshot(services=[service("legacy-host", None, host=True)],
        other_gpu_processes=[{"container": None, "pid": 51002, "gpu": 2, "used_mib": 1024, "comm": "python"},
                             {"container": "ctr-b", "pid": 51003, "gpu": 2, "used_mib": 1024, "comm": "python"}])
    original = copy.deepcopy(data)
    assert validate_snapshot(data) == original
    ingest(worker, now, data)
    result = worker.report()
    legacy = result["services"][0]
    assert legacy["host"] is True and legacy["host_uid"] is None and legacy["host_user"] is None
    unresolved, mapped = [row for row in result["gpus"][0]["occupants"] if row["kind"] == "other"]
    assert unresolved["host"] is None and unresolved["host_uid"] is None and unresolved["host_user"] is None
    assert mapped["host"] is False and mapped["host_uid"] is None and mapped["host_user"] is None


def test_container_summary_accepts_legacy_abbreviated_services():
    result = container_summary([
        {"container": "ctr-a", "gpu_gb": 2, "status": "idle"},
        {"container": None, "gpu_gb": None, "status": "unknown"},
        {"container": "ctr-a", "gpu_gb": 3, "status": "over_limit"},
    ])
    assert result == [
        {"container": None, "host": True, "host_uid": None, "host_user": None,
         "services": 1, "gpu_gb": None, "over_limit": 0},
        {"container": "ctr-a", "host": False, "host_uid": None, "host_user": None,
         "services": 2, "gpu_gb": 5, "over_limit": 1},
    ]


@pytest.mark.parametrize("target", ["service", "other"])
@pytest.mark.parametrize("changes", [
    {"host": "true"}, {"host": 1}, {"host_uid": True}, {"host_uid": -1},
    {"host_uid": 2 ** 32}, {"host_uid": 1.0}, {"host_uid": "61001"},
    {"host_user": True}, {"host_user": ""}, {"host_user": "x" * 129},
    {"host_user": "sample\nhost"}, {"host_user": "sample\x1b[31m"}, {"host_user": "sample\x7f"},
    {"host_user": "sample\x85"}, {"host_user": "sample\u202ehost"}, {"host_user": "sample\udc00"},
    {"host_uid": None}, {"host": False}, {"container": "ctr-a"},
    {"host": False, "container": "ctr-a", "host_user": None},
    {"host": True, "container": "ctr-a", "host_uid": None, "host_user": None},
])
def test_snapshot_rejects_invalid_host_identity(target, changes):
    owner = {"container": None, "host": True, "host_uid": 61001, "host_user": "sample-host-a"}
    owner.update(changes)
    if target == "service":
        data = snapshot(services=[service(**owner)])
    else:
        data = snapshot(services=[], other_gpu_processes=[{"pid": 51001, "gpu": 5, "used_mib": 1024, **owner}])
    with pytest.raises(ValueError, match="invalid_fleet_"):
        validate_snapshot(data)


@pytest.mark.parametrize("owner", [
    {"host": True, "container": None, "host_uid": None, "host_user": None},
    {"host": False, "container": None, "host_uid": None, "host_user": None},
    {"host": None, "container": "ctr-a", "host_uid": None, "host_user": None},
    {"host": None, "container": None, "host_uid": 61001, "host_user": None},
])
def test_snapshot_rejects_contradictory_other_owner(owner):
    with pytest.raises(ValueError, match="invalid_fleet_host_owner"):
        validate_snapshot(snapshot(other_gpu_processes=[{"pid": 51001, "gpu": 5, "used_mib": 1024, **owner}]))


@pytest.mark.parametrize("target", ["service", "other"])
@pytest.mark.parametrize("uid,user", [(0, None), (2 ** 32 - 1, "x" * 128)])
def test_snapshot_accepts_bounded_host_uid_and_user(target, uid, user):
    owner = {"container": None, "host": True, "host_uid": uid, "host_user": user}
    data = snapshot(services=[service(**owner)]) if target == "service" else snapshot(
        services=[], other_gpu_processes=[{"pid": 51001, "gpu": 5, "used_mib": 1024, **owner}])
    assert validate_snapshot(data) is data


@pytest.mark.parametrize("host,container", [(False, "ctr-a"), (None, None)])
def test_snapshot_accepts_resolved_container_and_unresolved_other_owners(host, container):
    data = snapshot(other_gpu_processes=[{"pid": 51001, "gpu": 5, "used_mib": 1024,
        "host": host, "container": container, "host_uid": None, "host_user": None}])
    assert validate_snapshot(data) is data


def test_invalid_host_owner_export_keeps_committed_observations(controller):
    worker, now, _ = controller
    ingest(worker, now, snapshot(services=[service("host-a", None, host=True,
        host_uid=61001, host_user="sample-host-a")]))
    before = worker.store.metadata()
    instances = worker.store.instances()
    now[0] += 60
    write_snapshot(worker.config, snapshot(now[0], services=[service("host-a", None, host=True,
        host_uid=True, host_user="sample-host-a")]))
    worker.ingest_once()
    assert worker.last_error == "fleet_snapshot_unavailable"
    assert worker.store.metadata() == before and worker.store.instances() == instances
    result = worker.report()
    assert result["stale"] is True and result["services"][0]["status"] == "unknown"


def test_fleet_get_21k_raw_rows_is_bounded_under_100ms(http_service):
    scheduler, address = http_service
    worker = scheduler.fleet
    db = worker.store._db
    points = [("instance-a", BASE - 60 * index, 0, 0, 0, 1, 10, 20, 0, 1, 1, 60, 60, 60, 0, 0) for index in range(1, 20999)]
    with db:
        db.executemany("INSERT INTO fleet_samples VALUES(" + ",".join("?" for _ in points[0]) + ")", points)
        for index in range(0, 350):
            db.execute("INSERT OR IGNORE INTO fleet_hourly VALUES(?,?,?,?,?,?,?,?,?)", ("instance-a", BASE - (index + 1) * 3600, 60, 60, 600, 1200, 0, 60, 3600))
    measured = []
    assert db.execute("SELECT COUNT(*) FROM fleet_samples").fetchone()[0] == 21000
    for _ in range(3):
        started = time.perf_counter()
        status, result = request(address, "GET", "/v1/fleet")
        measured.append((time.perf_counter() - started) * 1000)
        assert status == 200 and len(result["services"]) == 2
        assert result["services"][0]["window_24h"]["requests"] is not None
    assert max(measured) < 100, measured
    print("fleet_21k_http_ms=" + json.dumps(measured))
