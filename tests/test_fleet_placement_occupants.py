# Generated-By: Codex / gpt-6.1-sol
"""Synthetic, read-only fleet ownership hints and the actual 503 HTTP path."""

import copy
import http.client
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from llmsvc.config import SchedulerConfig
from llmsvc.fleet.occupants import enrich_unavailable
from llmsvc.leases import LeaseError
from llmsvc.server import SchedulerHTTPServer
from test_placement_unavailable import blocked_system, place

NOW = 1000.0


def service(service_id="inference-a", **updates):
    row = {"id": service_id, "engine": "vllm", "container": "team-a", "host": False,
           "managed_by": None, "pid": 4321, "started_at": 970,
           "model": "demo-model", "model_path": "/private/model-path",
           "argv_redacted": "command-line-must-not-appear", "port": 8000,
           "gpus": [{"index": 7, "used_mib": 50 * 1024}], "gpu_observation_complete": True,
           "metrics": {}, "scrape": {"ok": True, "error": None}}
    row.update(updates)
    return row


def snapshot(**updates):
    row = {"schema_version": 1, "generated_at": NOW, "sample_interval_seconds": 60,
           "inventory_complete": True, "gpu_inventory_complete": True, "gpu_attribution_complete": True,
           "host": {"gpu_count": 2},
           "gpus": [{"index": index, "uuid": uuid, "total_mib": 200 * 1024,
                     "used_mib": 80 * 1024, "util_percent": 50}
                    for index, uuid in ((7, "GPU-target"), (8, "GPU-second"))],
           "services": [service(started_at=updates.get("generated_at", NOW) - 30)],
           "other_gpu_processes": [{"container": "team-b", "pid": 9876, "gpu": 7,
                                    "used_mib": 30 * 1024, "comm": "private-comm",
                                    "argv": "private-other-command"}], "errors": []}
    row.update(updates)
    return row


def unavailable():
    return {"error": "no_feasible_gpu", "retryable": True, "retry_after_seconds": 60,
            "message": "All placement GPUs are occupied by workloads outside llmsvc",
            "gpus": [{"index": 0, "free_gb": 20, "external_gb": 80}],
            "blockers": [{"gpu": 0, "reason": "external_pressure", "external_gb": 80}]}


@pytest.fixture
def context(tmp_path):
    config = SchedulerConfig("127.0.0.1", 8011, fleet_enabled=True,
        fleet_snapshot_path=str(tmp_path / "fleet.json"), fleet_db_path=str(tmp_path / "fleet.sqlite"))

    def write(payload):
        path = Path(config.fleet_snapshot_path)
        path.write_text(json.dumps(payload))
        path.chmod(0o644)

    write(snapshot())
    return config, write


def enrich(config, error=None, gpu_uuids=None, now=NOW):
    return enrich_unavailable(error if error is not None else unavailable(), config,
        gpu_uuids={0: "GPU-target"} if gpu_uuids is None else gpu_uuids, now=now)


def test_fresh_occupants_match_uuid_across_different_gpu_indices(context):
    config, _ = context
    original = unavailable()
    before = copy.deepcopy(original)
    result = enrich(config, original, now=NOW + 90)
    assert result["blockers"][0]["occupants"] == [
        {"container": "team-a", "kind": "llm", "used_gb": 50, "service_id": "inference-a", "model": "demo-model"},
        {"container": "team-b", "kind": "other", "used_gb": 30, "service_id": None}]
    assert result["blockers"][0]["occupants_remaining"] == 0
    assert "GPU0: team-a 50.0G (LLM demo-model), team-b 30.0G (other workload)" in result["message"]
    assert result["gpus"] == original["gpus"] and original == before
    assert not Path(config.fleet_db_path).exists()


@pytest.mark.parametrize("age", [180.001, -0.001])
def test_stale_and_future_snapshots_omit_attribution(context, age):
    config, write = context
    write(snapshot(generated_at=NOW - age))
    original = unavailable()
    assert enrich(config, original) is original


@pytest.mark.parametrize("threshold,expected", [(60, False), (180, True), (360, True)])
def test_configured_freshness_and_inclusive_boundary(context, threshold, expected):
    config, write = context
    write(snapshot(generated_at=NOW - 180))
    result = enrich(replace(config, fleet_stale_after_seconds=threshold))
    assert ("occupants" in result["blockers"][0]) is expected


@pytest.mark.parametrize("flag", ["inventory_complete", "gpu_inventory_complete", "gpu_attribution_complete"])
@pytest.mark.parametrize("value", [False, None])
def test_partial_and_old_snapshots_without_completeness_omit_attribution(context, flag, value):
    config, write = context
    payload = snapshot()
    if value is None:
        del payload[flag]
    else:
        payload[flag] = value
    write(payload)
    original = unavailable()
    assert enrich(config, original) is original


@pytest.mark.parametrize("field", ["gpu_observation_complete", "used_mib"])
def test_partial_service_observation_cannot_attribute_a_gpu(context, field):
    config, write = context
    payload = snapshot()
    if field == "used_mib":
        payload["services"][0]["gpus"][0][field] = None
    else:
        payload["services"][0][field] = False
    write(payload)
    assert "occupants" not in enrich(config)["blockers"][0]


@pytest.mark.parametrize("case", ["missing", "foreign", "no_uuid", "duplicate_host", "duplicate_managed", "same_index_other_card"])
def test_uuid_binding_has_no_index_or_pid_fallback(context, case):
    config, write = context
    payload = snapshot()
    mapping = {0: "GPU-target"}
    if case == "missing":
        mapping = {}
    elif case == "foreign":
        mapping[0] = "GPU-foreign"
    elif case == "no_uuid":
        del payload["gpus"][0]["uuid"]
    elif case == "duplicate_host":
        payload["gpus"][1]["uuid"] = "GPU-target"
    elif case == "duplicate_managed":
        mapping[1] = "GPU-target"
    else:
        payload["gpus"][0]["index"] = 0
        payload["gpus"][0]["uuid"] = "GPU-foreign"
        payload["services"][0]["gpus"][0]["index"] = 0
        payload["other_gpu_processes"][0]["gpu"] = 0
    write(payload)
    assert "occupants" not in enrich(config, gpu_uuids=mapping)["blockers"][0]


@pytest.mark.parametrize("field", ["gpu", "used_mib"])
def test_unknown_other_gpu_measurement_omits_attribution(context, field):
    config, write = context
    payload = snapshot()
    payload["other_gpu_processes"][0][field] = 999 if field == "gpu" else None
    write(payload)
    assert "occupants" not in enrich(config)["blockers"][0]


def test_multiple_occupants_have_top_three_remaining_count_and_exclude_managed(context):
    config, write = context
    payload = snapshot()
    payload["services"] += [service("inference-b", container="team-c", gpus=[{"index": 7, "used_mib": 20 * 1024}]),
                            service("managed", container="llmsvc", managed_by="llmsvc",
                                    gpus=[{"index": 7, "used_mib": 100 * 1024}])]
    payload["other_gpu_processes"] += [
        {"container": "team-d", "gpu": 7, "used_mib": 70 * 1024},
        {"container": "team-e", "gpu": 7, "used_mib": 10 * 1024}]
    write(payload)
    blocker = enrich(config)["blockers"][0]
    assert [row["container"] for row in blocker["occupants"]] == ["team-d", "team-a", "team-b"]
    assert blocker["occupants_remaining"] == 2
    assert "+2 more" in enrich(config)["message"]


def test_other_processes_are_grouped_by_container_without_pid_matching(context):
    config, write = context
    payload = snapshot()
    # Deliberately equal to the inference service PID: namespace PID equality
    # cannot make this independently attributed row a managed inference owner.
    payload["other_gpu_processes"] += [{"container": "team-b", "pid": 4321, "gpu": 7, "used_mib": 30 * 1024}]
    write(payload)
    rows = enrich(config)["blockers"][0]["occupants"]
    assert [(row["kind"], row["used_gb"]) for row in rows] == [("other", 60), ("llm", 50)]


def test_container_name_does_not_exclude_unmanaged_workload(context):
    config, write = context
    write(snapshot(services=[service(container="llmsvc")]))
    assert enrich(config)["blockers"][0]["occupants"][0]["container"] == "llmsvc"


def test_missing_unreadable_or_invalid_export_keeps_original_error(context):
    config, _ = context
    path = Path(config.fleet_snapshot_path)
    original = unavailable()
    path.unlink()
    assert enrich(config, original) is original
    path.write_text("malformed JSON")
    assert enrich(config, original) is original
    path.write_text(json.dumps(snapshot()))
    path.chmod(0o666)
    assert enrich(config, original) is original
    assert not Path(config.fleet_db_path).exists()


@pytest.mark.parametrize("case", ["disabled", "timeout", "oversize"])
def test_disabled_and_unrelated_errors_do_not_read_snapshot(context, monkeypatch, case):
    config, _ = context
    original = unavailable()
    if case == "disabled":
        config = replace(config, fleet_enabled=False)
    elif case == "timeout":
        original["error"] = "placement_timeout"
    else:
        original["blockers"][0]["reason"] = "request_exceeds_gpu_capacity"
    monkeypatch.setattr("llmsvc.fleet.occupants.read_json", lambda *args: pytest.fail("unexpected snapshot read"))
    assert enrich(config, original) is original


def test_unknown_owner_model_and_privacy_whitelist(context):
    config, write = context
    payload = snapshot(services=[service(container=None, host=True, model=None)])
    payload["other_gpu_processes"][0]["container"] = None
    write(payload)
    result = enrich(config)
    rows = result["blockers"][0]["occupants"]
    assert rows[0]["container"] is None and rows[0]["model"] is None
    assert rows[1]["container"] is None
    assert set(rows[1]) == {"container", "used_gb", "kind", "service_id"}
    text = json.dumps(result)
    for private in ("4321", "9876", "private-comm", "private-other-command", "command-line-must-not-appear", "/private/model-path", "training"):
        assert private not in text
    assert "unknown container" in result["message"]


def test_model_labels_are_bounded_and_url_credentials_remain_redacted(context):
    config, write = context
    model = "HTTP://synthetic-user:synthetic-pass@example.invalid/model?credential=synthetic-query"
    write(snapshot(services=[service(model=model)]))
    result = enrich(config)
    text = json.dumps(result)
    assert "synthetic-user" not in text and "synthetic-pass" not in text and "synthetic-query" not in text
    write(snapshot(services=[service(model="m" * 512)]))
    assert len(enrich(config)["blockers"][0]["occupants"][0]["model"]) == 128
    write(snapshot(services=[service(model="token=synthetic-assignment hf_synthetic sk-synthetic")]))
    assert "synthetic" not in json.dumps(enrich(config))


def test_long_owner_labels_do_not_merge_different_containers_or_exceed_message_limit(context):
    config, write = context
    payload = snapshot(services=[])
    payload["other_gpu_processes"] = [
        {"container": "c" * 128 + suffix, "gpu": 7, "used_mib": 1024}
        for suffix in ("a", "b")]
    write(payload)
    assert len(enrich(config)["blockers"][0]["occupants"]) == 2
    payload["gpus"] = []
    payload["services"] = []
    payload["other_gpu_processes"] = []
    original = unavailable()
    original["blockers"] = []
    mapping = {}
    for index in range(8):
        payload["gpus"].append({"index": index + 10, "uuid": f"GPU-{index}",
                                "total_mib": 100 * 1024, "used_mib": 30 * 1024, "util_percent": 20})
        payload["services"].append(service(f"instance-{index}", container="c" * 128, model="m" * 128,
                                           gpus=[{"index": index + 10, "used_mib": 30 * 1024}]))
        original["blockers"].append({"gpu": index, "reason": "external_pressure"})
        mapping[index] = f"GPU-{index}"
    write(payload)
    result = enrich(config, original, gpu_uuids=mapping)
    assert len(result["message"]) <= 2048
    assert all(len(blocker["occupants"]) == 1 for blocker in result["blockers"])


def test_only_external_pressure_blockers_receive_fleet_hints(context):
    config, _ = context
    original = unavailable()
    original["blockers"].append({"gpu": 0, "reason": "request_exceeds_gpu_capacity"})
    result = enrich(config, original)
    assert "occupants" in result["blockers"][0] and "occupants" not in result["blockers"][1]


def test_core_failure_carries_private_uuids_from_its_exact_sample(blocked_system):
    scheduler, _, _ = blocked_system
    original = scheduler.collect

    def collect():
        result = original()
        return replace(result, gpus=tuple(replace(gpu, uuid=f"GPU-{gpu.index}") for gpu in result.gpus))

    scheduler.collect = collect
    scheduler.sample_once()
    with pytest.raises(LeaseError) as caught:
        place(scheduler)
    assert caught.value.gpu_uuids == {0: "GPU-0", 1: "GPU-1"}
    assert all("uuid" not in gpu for gpu in caught.value.gpus)


@pytest.mark.parametrize("fresh", [True, False])
def test_actual_http_503_context_keeps_retry_header_and_no_actions(blocked_system, tmp_path, fresh):
    scheduler, state, clock = blocked_system
    scheduler.config = replace(scheduler.config, fleet_enabled=True,
        fleet_snapshot_path=str(tmp_path / "fleet.json"), fleet_db_path=str(tmp_path / "fleet.sqlite"))
    payload = snapshot(generated_at=NOW if fresh else NOW - 181)
    payload["other_gpu_processes"][0]["used_mib"] = 10 * 1024
    path = Path(scheduler.config.fleet_snapshot_path)
    path.write_text(json.dumps(payload))
    path.chmod(0o644)
    original = scheduler.collect

    def collect():
        result = original()
        return replace(result, gpus=tuple(replace(gpu, uuid="GPU-target" if gpu.index == 0 else "GPU-second")
                                          for gpu in result.gpus))

    scheduler.collect = collect
    scheduler.sample_once()
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("POST", "/v1/place", json.dumps({"model": "incoming", "util": 0.6}))
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 503 and response.getheader("Retry-After") == "60"
        assert result["error"] == "no_feasible_gpu" and result["retryable"] is True
        blockers = [row for row in result["blockers"] if row["reason"] == "external_pressure"]
        assert ("occupants" in blockers[0]) is fresh
        assert "occupants" not in blockers[1]
        assert ("GPU0: team-a" in result["message"]) is fresh
        assert all("uuid" not in gpu for gpu in result["gpus"])
        assert 10 <= clock.now < 20 and not scheduler.store.leases() and not state["probes"]
        assert not Path(scheduler.config.fleet_db_path).exists()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
