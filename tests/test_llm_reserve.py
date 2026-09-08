# Generated-By: Codex / gpt-6-astra
"""Current core reserve preview/405; proposed #107 live replies are explicit fixtures."""

import copy
import io
import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from llmsvc.state import Pin
from test_llm_actions import action_service, pin_api, pin_service
from test_llm_pin import command


ARGS = ["reserve", "--gpu", "0", "--size", "80G", "--for", "1h"]


def reserve_args(api, *extra):
    return command(api, *ARGS, *extra)


@pytest.fixture
def reserve_service(action_service):
    action_service.effects["model"] = replace(action_service.effects["model"],
        state="sleeping", is_sleeping=True, resident_gb=2)
    action_service.scheduler.sample_once()
    return action_service


@pytest.mark.parametrize("read_only", [True, False])
def test_actual_current_preview_has_zero_database_transport_or_id_mutation(pin_api, reserve_service, monkeypatch, read_only):
    service = reserve_service
    service.scheduler.config = replace(service.scheduler.config, read_only=read_only)
    before = service.database.read_bytes(), service.scheduler.snapshot(), service.scheduler.events_since(0), service.effects["collects"]
    def forbidden(*args, **kwargs):
        pytest.fail("preview attempted a mutation")
    for target, name in [(service.store, "_write"), (service.scheduler.model_actions.transport, "http_request"),
                         (service.scheduler.model_actions.transport, "stop_unit")]:
        monkeypatch.setattr(target, name, forbidden)
    args = reserve_args(pin_api, "--dry-run")
    now = time.time()
    result = pin_api["execute_command"](args, pin_api["SchedulerClient"](service.url), now=now)
    record = result["would"][0]
    assert record == {"kind": "reserve", "gpu": 0, "size_gb": 80, "until": now+3600,
                      "by": pin_api["PIN_COMPATIBILITY_LABEL"]}
    assert "id" not in record
    assert any(item["kind"] == "stop" and item["model"] == "model" for item in result["would"])
    text = pin_api["format_result"](args, result)
    assert "no reservation ID allocated" in text and "hypothetical" in text
    assert pin_api["result_exit_code"](args, result) == 0
    assert (service.database.read_bytes(), service.scheduler.snapshot(), service.scheduler.events_since(0), service.effects["collects"]) == before
    assert service.effects["calls"] == []
    assert service.requests == [("POST", "/v1/reserve?dry_run=1")]


def test_current_blocked_preview_keeps_blockers_and_nonzero_exit(pin_api, reserve_service, capsys):
    reserve_service.store.put_pin(Pin("model", time.time()+3600, "protected-owner"))
    before = reserve_service.database.read_bytes()
    code = pin_api["main"](["--url", reserve_service.url, *ARGS, "--dry-run", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["blocked_by"]
    assert any(item["model"] == "model" for item in result["blocked_by"])
    assert reserve_service.database.read_bytes() == before
    assert not reserve_service.effects["calls"]


def test_readonly_405_is_not_a_preview_or_retry(pin_api, reserve_service, capsys):
    reserve_service.scheduler.config = replace(reserve_service.scheduler.config, read_only=True)
    before = reserve_service.database.read_bytes()
    code = pin_api["main"](["--url", reserve_service.url, *ARGS])
    captured = capsys.readouterr()
    assert code == 1 and "405" in captured.err and "read_only" in captured.err
    assert not captured.out
    assert reserve_service.requests == [("POST", "/v1/reserve")]
    assert reserve_service.database.read_bytes() == before and not reserve_service.effects["calls"]


@pytest.mark.parametrize("words", [
    ["reserve"], ["reserve", "--gpu", "0", "--size", "80G"],
    ["reserve", "--gpu", "0", "--for", "1h"], ["reserve", "--size", "80G", "--for", "1h"],
    ARGS+["--gpu", "-1"], ARGS+["--gpu", "1.5"], ARGS+["--size", "0G"], ARGS+["--size", "-1G"],
    ARGS+["--size", "nan"], ARGS+["--size", "inf"], ARGS+["--for", "0h"],
    ARGS+["--for", "-1h"], ARGS+["--for", "nan"], ARGS+["--wait", "0"],
])
def test_invalid_arguments_before_http(pin_api, words):
    with pytest.raises(SystemExit) as exc:
        command(pin_api, *words)
    assert exc.value.code == 2


def test_unrepresentable_expiry_rejected_before_http(pin_api, reserve_service):
    with pytest.raises(pin_api["ClientError"], match="UTC range"):
        pin_api["execute_command"](reserve_args(pin_api, "--for", "999999999d"),
                                    pin_api["SchedulerClient"](reserve_service.url))
    assert not reserve_service.requests


def test_unknown_gpu_is_rejected_by_real_core_before_mutation(pin_api, reserve_service):
    before = reserve_service.database.read_bytes()
    with pytest.raises(pin_api["ClientError"]) as exc:
        pin_api["execute_command"](reserve_args(pin_api, "--gpu", "9", "--dry-run"),
                                    pin_api["SchedulerClient"](reserve_service.url))
    assert exc.value.status == 400
    assert reserve_service.database.read_bytes() == before and not reserve_service.effects["calls"]


def live_receipt(status="blocked"):
    # #107 proposed envelope fixture, NOT a mounted current-core live response.
    return {"id": "fixture-reservation", "gpu": 0, "size_gb": 80, "until": time.time()+3600,
            "by": "server-owner", "evacuation": {"status": status,
            "stopped": ["already-confirmed"] if status in ("partial", "complete") else [],
            "skipped": [] if status == "complete" else [{"model": "protected", "reason": "pinned", "user": "pin-owner"}],
            **({} if status == "complete" else {"error": "evacuation_incomplete"})}}


@pytest.mark.parametrize("status", ["complete", "blocked", "partial"])
def test_proposed_live_envelope_retains_receipt_owner_and_outcome(pin_api, monkeypatch, capsys, status):
    result = live_receipt(status)
    calls = []
    def opener(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(json.dumps(result).encode())
    original_client = pin_api["SchedulerClient"]
    monkeypatch.setitem(pin_api["main"].__globals__, "SchedulerClient", lambda **config: original_client(**config, opener=opener))
    code = pin_api["main"](["--url", "http://fixture.invalid", "--timeout", "0.01", *ARGS, "--json"])
    assert code == (0 if status == "complete" else 1)
    assert json.loads(capsys.readouterr().out) == result
    assert len(calls) == 1 and calls[0][1] == 150
    payload = json.loads(calls[0][0].data)
    assert set(payload) == {"gpu", "size_gb", "until", "by"}
    assert payload["by"] == pin_api["PIN_COMPATIBILITY_LABEL"] != result["by"]
    text = pin_api["format_result"](reserve_args(pin_api), result)
    for expected in ["Reservation saved", result["id"], "owner server-owner", "Evacuation status: "+status,
                     "does not roll back", "whole GPU", "not measured", "does not imply zero"]:
        assert expected in text
    if status != "complete":
        assert "pin-owner" in text and "evacuation_incomplete" in text


def test_lost_reply_never_retries_and_warns_intent_may_persist(pin_api):
    calls = []
    def opener(request, timeout):
        calls.append(request)
        raise TimeoutError("reply lost")
    with pytest.raises(pin_api["ClientError"], match="reservation may have persisted"):
        pin_api["execute_command"](reserve_args(pin_api), pin_api["SchedulerClient"]("http://fixture.invalid", opener=opener))
    assert len(calls) == 1


@pytest.mark.parametrize("change", [{"id": ""}, {"by": ""}, {"gpu": True}, {"gpu": 1},
    {"size_gb": float('nan')}, {"until": float('inf')}, {"until": 0}, {"until": -1}, {"evacuation": {}},
    {"evacuation": {"status": "failed", "stopped": [], "skipped": []}}])
def test_invalid_receipt_cannot_be_reported_as_success(pin_api, change):
    result = live_receipt()
    result.update(copy.deepcopy(change))
    with pytest.raises(pin_api["ClientError"], match="intent may have persisted"):
        pin_api["validate_reserve_result"](reserve_args(pin_api), result)


def test_expired_receipt_is_not_misrepresented_as_active(pin_api):
    result = live_receipt("partial")
    result["until"] = time.time()-60
    pin_api["validate_reserve_result"](reserve_args(pin_api), result)
    assert "check status for current expiry/deletion" in pin_api["format_result"](reserve_args(pin_api), result)


def test_copied_stdlib_cli_preview(reserve_service, tmp_path):
    script = tmp_path/'llm'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'cli'/'llm', script)
    completed = subprocess.run([sys.executable, '-I', '-S', str(script), '--url', reserve_service.url,
                                *ARGS, '--dry-run', '--json'], cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout)["would"][0]["kind"] == "reserve"
    assert reserve_service.scheduler.snapshot().reserves == ()
