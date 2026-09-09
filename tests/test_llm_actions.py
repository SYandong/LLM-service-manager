# Generated-By: Codex / gpt-6-astra
"""Client/core loopback protocol tests with disposable SQLite and simulated effects."""

import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest

from llmsvc.actions import ManagedModelTransport, ModelActionController
from llmsvc.state import Activity, GPUState, MemoryState, ModelState
from test_llm_pin import command, pin_api, pin_service
from test_model_actions_http import system  # Real downstream loopback HTTP fixture; no live service.


@pytest.fixture
def action_service(pin_service):
    service = pin_service
    scheduler = service.scheduler
    scheduler.config = replace(scheduler.config, model_actions_enabled=True, free_timeout_seconds=2,
                               wake_timeout_seconds=2, action_observe_seconds=1, action_poll_seconds=0.005)
    state = {"model": ModelState("model", state="awake", unit="vllm-0.service", unit_active=True,
                                health_ok=True, is_sleeping=False, swap_state="ready", gpu=0,
                                resident_gb=80, weights_gb=40, budget_gb=80, cold_start_seconds=120),
             "free": 100, "available": 500, "calls": [], "collects": 0}
    def collect():
        state["collects"] += 1
        return replace(scheduler._snapshot, sampled_at=time.time(), models=(state["model"],),
                       errors=(), gpus=(GPUState(0, total_gb=200, free_gb=state["free"], external_gb=0),),
                       memory=MemoryState(state["available"], 40 if state["model"].state == "sleeping" else 0),
                       activity=(Activity(state["model"].name, time.time()-1000, 0, 0, 0),))
    def simulated_http(method, path, *, deadline):
        state["calls"].append((method, path))
        if method == "POST":
            assert path == "/api/models/unload/" + quote(state["model"].name, safe="")
            state["model"] = replace(state["model"], state="sleeping", is_sleeping=True, resident_gb=2)
            state["free"] += 12  # Deliberately different from the policy estimate of 78.
        else:
            assert unquote(path[len('/upstream/'):-1]) == state["model"].name
            state["model"] = replace(state["model"], state="awake", is_sleeping=False,
                                     unit_active=True, health_ok=True, swap_state="ready", resident_gb=80)
        return 200
    def simulated_stop(argv, **kwargs):
        state["calls"].append(("stop", argv[-1]))
        state["model"] = replace(state["model"], state="stopped", unit_active=False, resident_gb=0)
        state["available"] += 25
        return SimpleNamespace(returncode=0)
    transport = ManagedModelTransport(swap_url="http://unused.invalid", systemctl="fixture-only",
        models={name: {"unit": "vllm-%s.service" % index} for index, name in enumerate(service.names)},
        run=simulated_stop)
    transport.http_request = simulated_http
    scheduler.collect = collect
    scheduler.model_actions = ModelActionController(scheduler, transport)
    scheduler.sample_once()
    service.effects = state
    return service


def run(api, service, *words):
    args = command(api, *words)
    result = api["execute_command"](args, api["SchedulerClient"](service.url, timeout=0.01))
    return args, result, api["format_result"](args, result)


@pytest.mark.parametrize("words", [
    ["free", "--need", "-1G"], ["free", "--need", "nan"], ["free", "--need", "inf"],
    ["free", "--gpu", "-1"], ["free", "--gpu", "1.5"], ["free", "--wait", "0"],
    ["wake"], ["wake", ""], ["wake", "model\nheader"], ["wake", "model", "--wait", "nan"],
    ["reserve"], ["add", "x"], ["rm"],
])
def test_invalid_arguments_reject_before_request(pin_api, words):
    with pytest.raises(SystemExit):
        command(pin_api, *words)


@pytest.mark.parametrize("words,wait,path,body", [
    (["free"], 150, "/v1/free", {"ram": False}),
    (["free", "--gpu", "0", "--need", "80G", "--ram", "--dry-run"], 150,
     "/v1/free?dry_run=1", {"ram": True, "gpu": 0, "need_gb": 80.0}),
    (["wake", "org/a b?#%/模型"], 930, "/v1/wake/org%2Fa%20b%3F%23%25%2F%E6%A8%A1%E5%9E%8B", None),
    (["wake", "--wait", "1200", "--", "-model"], 1200, "/v1/wake/-model", None),
])
def test_wire_payload_and_response_wait_ignore_short_read_timeout(pin_api, words, wait, path, body):
    seen = []
    def opener(request, timeout):
        seen.append((request.full_url, request.data, timeout, request.method))
        raise TimeoutError("fixture wait")
    client = pin_api["SchedulerClient"]("http://fixture.invalid", timeout=0.01, opener=opener)
    with pytest.raises(pin_api["ClientError"]) as exc:
        pin_api["execute_command"](command(pin_api, *words), client)
    assert len(seen) == 1  # No retry even when server acceptance is unknown.
    url, data, timeout, method = seen[0]
    assert (url, timeout, method) == ("http://fixture.invalid" + path, wait, "POST")
    assert (json.loads(data) if data else None) == body
    if "--dry-run" not in words:
        assert "outcome unknown" in str(exc.value) and "no automatic retry" in str(exc.value)
    assert client.timeout == 0.01


def test_real_core_free_measurement_and_wake_readiness(pin_api, system):
    scheduler, address, _, state = system
    state["model"] = replace(state["model"], state="awake", unit_active=True, health_ok=True,
                             is_sleeping=False, swap_state="ready", gpu=0, resident_gb=80)
    service = SimpleNamespace(url="http://%s:%s" % address)
    args, result, text = run(pin_api, service, "free", "--need", "10G", "--gpu", "0")
    assert result["status"] == "complete" and result["freed_gb"] == 12
    assert "12.0 GiB" in text and "Measurement complete: yes" in text and "Slept: model" in text
    assert pin_api["result_exit_code"](args, result) == 0
    args, result, text = run(pin_api, service, "wake", "model")
    assert result["ready"] is True and "status: ready" in text and "elapsed:" in text
    assert pin_api["result_exit_code"](args, result) == 0
    assert state["http_calls"] == [("POST", "/api/models/unload/model"), ("GET", "/upstream/model/")]


@pytest.mark.parametrize("name", ["org/a b?#%/模型", "literal%2Fmodel", "-leading-dash"])
def test_url_encoded_wake_uses_real_core_path(pin_api, action_service, name):
    index = action_service.names.index(name)
    action_service.effects["model"] = replace(action_service.effects["model"], name=name,
        unit="vllm-%s.service" % index, state="sleeping", is_sleeping=True, resident_gb=2)
    _, result, _ = run(pin_api, action_service, "wake", "--", name)
    assert result["model"] == name and result["ready"] is True
    assert action_service.requests[-1] == ("POST", "/v1/wake/" + quote(name, safe=""))


@pytest.mark.parametrize("words", [("free", "--need", "10G"), ("free", "--ram"), ("wake", "model")])
def test_dry_run_zero_effects_collections_and_sqlite_writes(pin_api, action_service, monkeypatch, words):
    service = action_service
    service.scheduler.config = replace(service.scheduler.config, read_only=True)
    before = service.database.read_bytes(), service.scheduler.snapshot(), service.scheduler.events_since(0), service.effects["collects"]
    def forbidden(*args, **kwargs):
        pytest.fail("dry run attempted an effect")
    monkeypatch.setattr(service.scheduler.model_actions.transport, "http_request", forbidden)
    monkeypatch.setattr(service.scheduler.model_actions.transport, "stop_unit", forbidden)
    args, result, text = run(pin_api, service, *words, "--dry-run")
    assert "Dry run" in text
    assert (service.database.read_bytes(), service.scheduler.snapshot(), service.scheduler.events_since(0), service.effects["collects"]) == before
    assert not service.effects["calls"]
    if args.command == "free":
        assert "policy estimate, not measured" in text


@pytest.mark.parametrize("enabled,readonly,error", [(True, True, "read_only"), (False, False, "operation_not_enabled")])
@pytest.mark.parametrize("words", [("free",), ("wake", "model")])
def test_default_gates_preserved(pin_api, action_service, enabled, readonly, error, words):
    service = action_service
    service.scheduler.config = replace(service.scheduler.config, model_actions_enabled=enabled, read_only=readonly)
    with pytest.raises(pin_api["ClientError"]) as exc:
        run(pin_api, service, *words)
    assert exc.value.status == 405 and error in str(exc.value)
    assert not service.effects["calls"]


def test_real_blocked_outcome_http200_exits_nonzero(pin_api, action_service, capsys):
    action_service.effects["model"] = replace(action_service.effects["model"], state="sleeping", is_sleeping=True)
    action_service.effects["free"] = 0
    action_service.effects["model"] = replace(action_service.effects["model"], resident_gb=2)
    code = pin_api["main"](["--url", action_service.url, "wake", "model", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "blocked" and result["error"] == "insufficient_gpu_memory"
    assert not action_service.effects["calls"]


def free_outcome(status="partial", freed=None):
    return {"status": status, "freed_gb": freed, "measured_at": 123.0 if freed is not None else None,
            "measurement_complete": False, "measurement": "net_gpu_free_gib", "slept": ["a"],
            "stopped": ["b"], "skipped": [{"model": "c", "reason": "in_flight", "by": ["ctr-a"], "in_flight": 2}],
            "error": "measurement_unavailable", "error_model": "b"}


@pytest.mark.parametrize("status", ["blocked", "partial", "failed", "timeout", "no_progress"])
@pytest.mark.parametrize("freed", [None, 12.5])
def test_partial_unknown_and_all_blocker_details_retained(pin_api, status, freed):
    args = command(pin_api, "free")
    result = pin_api["validate_action_result"](args, free_outcome(status, freed))
    text = pin_api["format_result"](args, result)
    for value in [status, "Slept: a", "Stopped: b", "in_flight", "ctr-a", "measurement_unavailable", "Error model: b", "not a final/current total"]:
        assert value in text
    assert ("change: unknown" if freed is None else "12.5 GiB") in text
    assert pin_api["result_exit_code"](args, result) == 1
    args.json = True
    assert json.loads(pin_api["format_result"](args, result)) == result


@pytest.mark.parametrize("patch", [{"status": "success"}, {"freed_gb": True}, {"freed_gb": float('nan')},
    {"measurement_complete": "yes"}, {"measurement_complete": True}, {"measurement": "estimate"},
    {"skipped": [{}]}, {"slept": "a"}])
def test_invalid_result_never_claims_success(pin_api, patch):
    result = free_outcome()
    result.update(patch)
    with pytest.raises(pin_api["ClientError"], match="outcome may be unknown"):
        pin_api["validate_action_result"](command(pin_api, "free"), result)


def test_partial_wake_ready_is_not_clean_success(pin_api):
    args = command(pin_api, "wake", "model")
    result = {"model": "model", "status": "partial", "ready": True, "elapsed_seconds": 1.25,
              "cold_start": True, "error": "upstream_error"}
    pin_api["validate_action_result"](args, result)
    text = pin_api["format_result"](args, result)
    assert "Ready: yes" in text and "cold start: yes" in text and "upstream_error" in text
    assert pin_api["result_exit_code"](args, result) == 1


def test_copied_cli_actions_have_no_repository_imports(action_service, tmp_path):
    script = tmp_path / "llm"
    shutil.copyfile(__file__.rsplit('/tests/', 1)[0] + '/cli/llm', script)
    for words in [["free", "--need", "10G", "--json"], ["wake", "model", "--json"]]:
        completed = subprocess.run([sys.executable, "-I", "-S", str(script), "--url", action_service.url, *words],
                                   cwd=tmp_path, capture_output=True, text=True, timeout=5)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        assert json.loads(completed.stdout)["status"] in ("complete", "ready")


@pytest.mark.parametrize("status", ["blocked", "partial", "failed", "timeout", "no_progress"])
def test_http200_failed_free_json_preserves_body_and_exit(pin_api, action_service, monkeypatch, capsys, status):
    result = free_outcome(status)
    monkeypatch.setattr(action_service.scheduler.model_actions, "free", lambda *a, **kw: result)
    code = pin_api["main"](["--url", action_service.url, "free", "--json"])
    assert code == 1
    assert json.loads(capsys.readouterr().out) == result
    assert action_service.requests[-1] == ("POST", "/v1/free")
    assert not action_service.effects["calls"]


def test_zero_target_is_not_a_model_action(pin_api, action_service):
    args, result, text = run(pin_api, action_service, "free", "--need", "0")
    assert result["status"] == "complete" and result["freed_gb"] == 0
    assert pin_api["result_exit_code"](args, result) == 0
    assert not action_service.effects["calls"]
