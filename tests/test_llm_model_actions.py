# Generated-By: Claude Code / claude-fable-5-1
"""CLI parsing, routing, display and exit codes for sleep/stop/preload."""

from dataclasses import replace

import pytest

from test_llm_pin import command, pin_api
from test_model_action_endpoints_http import service  # Real loopback core HTTP fixture.


class Recorder:
    """Records the request instead of reaching a scheduler."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def request(self, method, path, payload=None, *, timeout=None):
        self.calls.append((method, path, payload, timeout))
        return self.result


@pytest.mark.parametrize("words", [
    ["sleep"], ["stop"], ["preload"], ["sleep", ""], ["stop", "model\nheader"],
    ["preload", "model", "--wait", "0"], ["sleep", "model", "--wait", "nan"],
    ["stop", "model", "--need", "1G"], ["preload", "model", "--ram"],
])
def test_invalid_arguments_reject_before_any_request(pin_api, words):
    with pytest.raises(SystemExit):
        command(pin_api, *words)


@pytest.mark.parametrize("name,wait", [("sleep", 150), ("stop", 150), ("preload", 960)])
def test_each_subcommand_keeps_the_wake_style_options(pin_api, name, wait):
    args = command(pin_api, name, "org/model name")
    assert args.command == name and args.model == "org/model name"
    assert args.wait == wait and args.dry_run is False and args.json is False
    assert command(pin_api, name, "m", "--wait", "12", "--dry-run", "--json").wait == 12


@pytest.mark.parametrize("name", ["sleep", "stop", "preload"])
def test_the_model_is_sent_as_an_encoded_path_with_an_empty_body(pin_api, name):
    args = command(pin_api, name, "org/a b?#%")
    client = Recorder({"model": "org/a b?#%", "status": "ready", "error": None,
                       "elapsed_seconds": 1.5, "state": "sleeping", "already_resident": False})
    result = pin_api["execute_command"](args, client)
    assert client.calls == [("POST", "/v1/%s/org%%2Fa%%20b%%3F%%23%%25" % name, None, args.wait)]
    assert result["status"] == "ready"
    assert pin_api["result_exit_code"](args, result) == 0


@pytest.mark.parametrize("name", ["sleep", "stop", "preload"])
def test_dry_run_adds_the_preview_query_and_reports_blocked_previews(pin_api, name):
    args = command(pin_api, name, "model", "--dry-run")
    client = Recorder({"would": [], "blocked_by": [{"model": "model", "reason": "in_flight"}]})
    result = pin_api["execute_command"](args, client)
    assert client.calls[0][1] == "/v1/%s/model?dry_run=1" % name
    assert pin_api["result_exit_code"](args, result) == 1
    assert "Blocked: " in pin_api["format_result"](args, result)


def test_a_blocked_outcome_is_displayed_and_exits_nonzero(pin_api):
    args = command(pin_api, "stop", "model")
    result = {"model": "model", "status": "blocked", "error": "default_model",
              "elapsed_seconds": 0.25, "state": "awake"}
    text = pin_api["format_result"](args, pin_api["validate_action_result"](args, result))
    assert text.splitlines() == ["Stop model status: blocked",
                                "Observed state: awake; elapsed: 0.25 seconds",
                                "Error: default_model"]
    assert pin_api["result_exit_code"](args, result) == 1


def test_preload_reports_whether_the_weights_were_already_resident(pin_api):
    args = command(pin_api, "preload", "model")
    result = {"model": "model", "status": "ready", "error": None, "elapsed_seconds": 0.0,
              "state": "awake", "already_resident": True}
    text = pin_api["format_result"](args, pin_api["validate_action_result"](args, result))
    assert text.splitlines() == ["Preload model status: ready",
                                 "Observed state: awake; elapsed: 0.0 seconds",
                                 "Weights already resident: yes"]
    assert pin_api["result_exit_code"](args, result) == 0


def test_an_unknown_final_state_is_shown_as_unknown_rather_than_assumed(pin_api):
    args = command(pin_api, "sleep", "model")
    result = {"model": "model", "status": "partial", "error": "effect_not_confirmed",
              "elapsed_seconds": 2.0, "state": None}
    text = pin_api["format_result"](args, pin_api["validate_action_result"](args, result))
    assert "Observed state: unknown; elapsed: 2.0 seconds" in text
    assert pin_api["result_exit_code"](args, result) == 1


@pytest.mark.parametrize("name,result", [
    ("sleep", {"model": "other", "status": "ready", "error": None, "elapsed_seconds": 0.0, "state": "sleeping"}),
    ("stop", {"model": "model", "status": "done", "error": None, "elapsed_seconds": 0.0, "state": "stopped"}),
    ("sleep", {"model": "model", "status": "ready", "error": 7, "elapsed_seconds": 0.0, "state": "sleeping"}),
    ("sleep", {"model": "model", "status": "ready", "error": None, "elapsed_seconds": -1, "state": "sleeping"}),
    ("stop", {"model": "model", "status": "ready", "error": None, "elapsed_seconds": 0.0, "state": 3}),
    ("preload", {"model": "model", "status": "ready", "error": None, "elapsed_seconds": 0.0, "state": "awake"}),
])
def test_invalid_responses_are_rejected_instead_of_displayed(pin_api, name, result):
    args = command(pin_api, name, "model")
    with pytest.raises(pin_api["ClientError"]):
        pin_api["validate_action_result"](args, result)


def client_for(pin_api, address):
    return pin_api["SchedulerClient"]("http://127.0.0.1:%s" % address[1], timeout=5)


@pytest.mark.parametrize("name,call,state", [
    ("sleep", ("sleep", "a"), "sleeping"),
    ("stop", ("stop", "a"), "stopped"),
])
def test_the_commands_drive_the_real_scheduler_endpoints(pin_api, service, name, call, state):
    scheduler, address, backend = service
    args = command(pin_api, name, "a")
    result = pin_api["execute_command"](args, client_for(pin_api, address))
    assert result["status"] == "ready" and result["state"] == state
    assert backend.calls == [call]
    assert pin_api["result_exit_code"](args, result) == 0
    assert pin_api["format_result"](args, result).startswith("%s a status: ready" % name.capitalize())


def test_a_read_only_scheduler_is_reported_as_a_client_failure(pin_api, service):
    scheduler, address, backend = service
    scheduler.config = replace(scheduler.config, read_only=True)
    args = command(pin_api, "preload", "a")
    with pytest.raises(pin_api["ClientError"]) as error:
        pin_api["execute_command"](args, client_for(pin_api, address))
    assert "read_only" in str(error.value) and backend.calls == []
