# Generated-By: Codex / gpt-6.1-sol
"""Standalone fleet client behavior through bounded loopback HTTP fixtures."""

import copy
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

import pytest


CLI = Path(__file__).resolve().parents[1] / "cli" / "llm"


@pytest.fixture
def fleet_api():
    return runpy.run_path(str(CLI))


@pytest.fixture
def fleet():
    def service(id_, model, owner, status, memory, idle, tokens):
        return {"id": id_, "model": model, "container": owner, "engine": "vllm", "mine": owner == "ctr-a",
                "api_address": "http://192.0.2.10:8000" if id_ == "id-a" else "http://127.0.0.1:8000" if id_ == "id-b" else None,
                "api_access": "shared" if id_ == "id-a" else "local_only" if id_ == "id-b" else "unknown",
                "idle_time_sensitive": id_ != "id-a",
                "gpus": [1], "gpu_gb": memory, "status": status, "idle_seconds": idle,
                "uptime_seconds": 864000, "window_24h": {"active_minutes": 200, "active_ratio": 0.14,
                "gen_tokens": tokens, "prompt_tokens": None, "requests": 1500, "coverage_ratio": 0.5},
                "hourly_active_24h": [None, 0, 12, 60] * 6, "claim": None}

    return {"schema_version": 1, "generated_at": 1800000000, "snapshot_age_seconds": 42,
            "stale": False, "config": {"idle_limit_hours": 6}, "errors": [],
            "containers": [{"container": "ctr-a", "services": 2, "gpu_gb": 100, "over_limit": 1}],
            "gpus": [{"index": 0, "used_gb": 50, "total_gb": 144, "util_percent": 30,
                      "occupants": [{"container": "ctr-c", "kind": "other", "used_gb": 50, "service_id": None}]},
                     {"index": 1, "used_gb": 120, "total_gb": 144, "util_percent": 10, "occupants": []}],
            "services": [service("id-a", "large-model", "ctr-a", "idle", 80, 300, 210000),
                         service("id-b", "small-model", "ctr-a", "over_limit", 20, 68400, 0),
                         service("id-c", "unknown-model", "ctr-b", "unknown", None, None, None)],
            "future_field": {"preserve": None}}


@pytest.fixture
def write_response():
    return {}


@pytest.fixture
def fleet_service(fleet, write_response):
    requests, claims = [], {}

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, data):
            body = None
            if self.command in ("POST", "DELETE"):
                status = write_response.get("status", status)
                if "rewrite" in write_response:
                    data = write_response["rewrite"](copy.deepcopy(data))
                body = write_response.get("raw")
            if body is None:
                body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append(("GET", self.path, None))
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/fleet":
                data = copy.deepcopy(fleet)
                if parse_qs(parsed.query).get("mine") == ["1"]:
                    data["services"] = [item for item in data["services"] if item["mine"]]
                self.send(200, data)
            elif parsed.path == "/v1/fleet/history":
                query = parse_qs(parsed.query)
                hours = int(query["hours"][0])
                sample = ({"hour_ts": 1800000000, "active_minutes": None, "requests": 10,
                           "gen_tokens": None, "prompt_tokens": 1500} if hours == 168 else
                          {"ts": 1800000000, "active": None, "running": None, "waiting": None,
                           "d_requests": 10, "d_gen_tokens": None, "d_prompt_tokens": 1500})
                self.send(200, {"schema_version": 1, "service_id": query["service"][0], "hours": hours,
                               "resolution": "hourly" if hours == 168 else "minute", "samples": [sample],
                               "service": {"model": "test-model", "engine": "vllm", "container": "ctr-a",
                                           "argv_redacted": "vllm serve test-model --api-key [REDACTED]"}})
            elif parsed.path == "/v1/state":
                self.send(200, {"models": [], "reserves": [], "unknown": None})
            else:
                self.send(404, {"error": "not_found"})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, payload))
            if payload["service_id"] == "id-c":
                self.send(403, {"error": "forbidden_container"})
                return
            claim = dict(payload, instance_id=payload["service_id"], container="ctr-a", created_by_container="ctr-a")
            dry_run = parse_qs(urlsplit(self.path).query).get("dry_run") == ["1"]
            if not dry_run:
                claim["id"] = "claim/a %"
                claims[claim["id"]] = claim
            self.send(200, {"ok": True, "dry_run": dry_run, "claim": claim})

        def do_DELETE(self):
            requests.append(("DELETE", self.path, None))
            parsed = urlsplit(self.path)
            id_ = unquote(parsed.path.rsplit("/", 1)[-1])
            dry_run = parse_qs(parsed.query).get("dry_run") == ["1"]
            if id_ not in claims:
                self.send(404, {"error": "claim_not_found"})
                return
            claim = dict(claims[id_], revoked_at=None if dry_run else 1800000001)
            if not dry_run:
                del claims[id_]
            self.send(200, {"ok": True, "dry_run": dry_run, "claim": claim})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s" % httpd.server_port, requests, claims
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("command", [[], ["status"], ["status", "--plain"], ["fleet"], ["top"]])
def test_copied_stdlib_cli_defaults_to_fleet_without_tty(tmp_path, fleet_service, command):
    address, requests, _ = fleet_service
    script = tmp_path / "llm"
    shutil.copyfile(CLI, script)
    result = subprocess.run([sys.executable, "-I", "-S", str(script), "--url", address,
                             "--config", str(tmp_path / "missing"), *command], cwd=tmp_path,
                            env=dict(os.environ, COLUMNS="80"), capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "GPU inference services" in result.stdout and "small-model" in result.stdout
    assert "Running · inactive" in result.stdout and "·▁▂█" in result.stdout
    assert "API: http://192.0.2.10:8000 · Shared" in result.stdout and "API: Local only" in result.stdout
    assert "OVER LIMIT" not in result.stdout
    assert "\x1b" not in result.stdout
    assert requests == [("GET", "/v1/fleet", None)]
    assert ("pip install" in result.stdout) == (command in ([], ["top"]))


@pytest.mark.parametrize("command", [["status", "--json"], ["fleet", "--json"]])
def test_json_response_is_preserved(fleet_api, fleet_service, fleet, command):
    address, requests, _ = fleet_service
    args = fleet_api["build_parser"]().parse_args(command)
    result = fleet_api["execute_command"](args, fleet_api["SchedulerClient"](address))
    assert json.loads(fleet_api["format_result"](args, result)) == fleet
    assert requests == [("GET", "/v1/fleet", None)]


@pytest.mark.parametrize("by", ["person", "gpu"])
def test_default_text_anonymizes_ids_claims_errors_and_history(fleet_api, fleet, by):
    item = fleet["services"][0]
    item.update(id="private-owner:42:55", container="private-owner",
                model="/srv/private-owner/gemma-4-31b-it-qat-w4a16-ct")
    item["claim"] = {"id": "private-owner:claim", "until": 1900086400,
                     "reason": "private-owner is running this service"}
    fleet["errors"] = ["private-owner: GPU reading unavailable"]
    fleet["services"] = [item]
    fleet["containers"] = []
    fleet["gpus"][0]["occupants"][0]["container"] = "private-owner"
    before = copy.deepcopy(fleet)
    label = fleet_api["fleet_owner"](item)[1]
    args = fleet_api["build_parser"]().parse_args(["fleet", "--by", by])
    text = fleet_api["format_result"](args, fleet, width=200)
    assert label in text and label + ":42:55" in text
    assert "private-owner" not in text and "/srv/" not in text
    assert "gemma-4-31b-it-qat-w4a16-ct" in text
    args.show_names = True
    revealed = fleet_api["format_result"](args, fleet, width=200)
    assert "private-owner" in revealed and "/srv/" not in revealed
    args.json = True
    assert json.loads(fleet_api["format_result"](args, fleet)) == before
    history = {"service_id": item["id"], "hours": 168, "resolution": "hourly",
               "samples": [], "service": dict(item, argv_redacted="vllm serve %s --owner private-owner" % item["model"])}
    text = fleet_api["format_fleet_history"](history, width=200)
    assert label in text and "private-owner" not in text and "/srv/" not in text
    assert "private-owner" in fleet_api["format_fleet_history"](history, show_names=True)
    assert fleet == before


@pytest.mark.parametrize("name", ["User", "Work", "google"])
def test_owner_names_do_not_rewrite_generated_labels_or_public_models(fleet_api, fleet, name):
    item = fleet["services"][0]
    item.update(container=name, model=name + "/gemma", id=name + ":42:55")
    fleet["services"] = [item]
    fleet["gpus"][0]["occupants"][0]["container"] = name
    label = fleet_api["fleet_owner"](item)[1]
    text = fleet_api["format_fleet"](fleet, width=200, by="gpu")
    assert label + " (Work)" in text
    assert label + ":42:55" in text
    assert name + "/gemma" in text
    assert label + " " + label not in text


@pytest.mark.parametrize("name, model", [("gemma-owner", "gemma"),
                                        ("User", "/srv/models/User"),
                                        ("Work", "/srv/models/Work")])
def test_model_owner_collisions_keep_visible_models_and_anonymous_service_ids(fleet_api, fleet, name, model):
    item = fleet["services"][0]
    item.update(container=name, model=model, id=name)
    fleet["services"] = [item]
    text = fleet_api["format_fleet"](fleet, width=200)
    assert "id " + fleet_api["fleet_owner"](item)[1] in text
    assert fleet_api["fleet_model_label"](model) + "  vllm" in text


@pytest.mark.parametrize("code", ["fleet_disabled", "fleet_claims_disabled", "fleet_store_unavailable",
                                  "unmapped_container", "invalid_fleet_history_query", "service_not_found",
                                  "forbidden_container", "service_observation_unknown", "claim_not_found"])
def test_anonymous_cli_preserves_safe_structured_http_reasons(fleet_api, monkeypatch, capsys, code):
    class Client:
        def request(self, *args):
            raise fleet_api["ClientError"]("HTTP 503: %s private-owner" % code, status=503,
                                           payload={"error": code, "container": "private-owner"})

    globals_ = fleet_api["main"].__globals__
    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "SchedulerClient", lambda **kwargs: Client())
    assert fleet_api["main"](["fleet"]) == 1
    error = capsys.readouterr().err
    assert code in error and "private-owner" not in error
    assert fleet_api["main"](["fleet", "--show-names"]) == 1
    assert "private-owner" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [["--show-names", "fleet"], ["fleet", "--show-names"],
                                       ["top", "--show-names"], ["history", "raw-id", "--show-names"]])
def test_show_names_is_available_without_authentication(fleet_api, arguments):
    assert fleet_api["build_parser"]().parse_args(arguments).show_names is True


def test_mine_is_resolved_by_server(fleet_api, fleet_service):
    address, requests, _ = fleet_service
    args = fleet_api["build_parser"]().parse_args(["fleet", "--mine"])
    result = fleet_api["execute_command"](args, fleet_api["SchedulerClient"](address))
    assert {item["container"] for item in result["services"]} == {"ctr-a"}
    assert requests == [("GET", "/v1/fleet?mine=1", None)]


@pytest.mark.parametrize("width", [1, 20, 40, 80, 100, 160])
@pytest.mark.parametrize("by", ["person", "gpu"])
def test_narrow_display_uses_real_character_widths(fleet_api, fleet, width, by):
    fleet["services"][0]["model"] = "模型e\u0301\x1b[2J\r\n" * 20
    text = fleet_api["format_fleet"](fleet, width=width, by=by)
    assert all(fleet_api["cell_width"](line) <= width for line in text.splitlines())
    assert "\x1b" not in text and "\r" not in text


def test_unknown_coverage_stale_and_other_occupants_are_visible(fleet_api, fleet):
    text = fleet_api["format_fleet"](fleet, width=80, by="gpu")
    owner = fleet_api["fleet_owner"]({"container": "ctr-c"})[1]
    for expected in (owner + " (Work) 50G", "output —", "idle —", "observed coverage 50%", "·▁▂█"):
        assert expected in text
    fleet["stale"] = True
    stale = fleet_api["format_fleet"](fleet, width=80)
    assert "WARNING stale" in stale and "OVER LIMIT" not in stale
    assert "idle 19h" not in stale


@pytest.mark.parametrize("by", ["person", "gpu"])
def test_model_rows_show_api_access_and_running_inactive_label(fleet_api, fleet, by):
    fleet["services"][0].update(api_address="http://192.0.2.10:8000", api_access="shared", idle_time_sensitive=False)
    fleet["services"][1].update(api_address="http://127.0.0.1:8000", api_access="local_only", idle_time_sensitive=True)
    fleet["services"][2].update(api_address="http://[2001:db8::10]:8000", api_access="direct", idle_time_sensitive=True)
    text = fleet_api["format_fleet"](fleet, width=160, by=by)
    assert "http://192.0.2.10:8000 · Shared" in text
    assert "Local only" in text and "http://127.0.0.1:8000" not in text
    assert "http://[2001:db8::10]:8000" in text
    assert "Running · inactive" in text and "OVER LIMIT" not in text and "over limit" not in text
    assert fleet["services"][1]["status"] == "over_limit"
    fleet["stale"] = True
    stale = fleet_api["format_fleet"](fleet, width=160, by=by)
    assert "http://" not in stale and "Shared" not in stale and "Local only" not in stale
    assert "Unknown" in stale


@pytest.mark.parametrize("prompt,generated,total", [(1234, 5678, "6,912 tokens"), (None, 5678, "— tokens"), (1234, None, "— tokens")])
def test_fleet_token_display_has_units_and_preserves_unknown_totals(fleet_api, fleet, prompt, generated, total):
    fleet["services"] = fleet["services"][:1]
    fleet["services"][0]["window_24h"].update(prompt_tokens=prompt, gen_tokens=generated)
    text = fleet_api["format_fleet"](fleet, width=200)
    assert "input %s" % ("— tokens" if prompt is None else "1,234 tokens") in text
    assert "output %s" % ("— tokens" if generated is None else "5,678 tokens") in text
    assert "total " + total in text


def test_fleet_model_and_api_wrap_without_truncating_names(fleet_api, fleet):
    fleet["services"] = fleet["services"][:1]
    model = "organization/complete-long-model-name-for-review"
    address = "http://[2001:db8:1234:5678::10]:11434"
    fleet["services"][0].update(model=model, api_address=address, api_access="shared", idle_time_sensitive=False)
    text = fleet_api["format_fleet"](fleet, width=40)
    unwrapped = text.replace("\n", "")
    assert model in unwrapped and address in unwrapped and "API:" in text
    assert all(fleet_api["cell_width"](line) <= 40 for line in text.splitlines())


def test_gpu_view_uses_per_card_observations_for_multi_gpu_service(fleet_api, fleet):
    item = fleet["services"][0]
    item["gpus"] = [0, 1]
    for gpu, amount in zip(fleet["gpus"], [20, 60]):
        gpu["occupants"].append({"container": item["container"], "kind": "llm",
                                 "service_id": item["id"], "used_gb": amount})
    text = fleet_api["format_fleet"](fleet, width=100, by="gpu")
    assert "vllm  GPU0  20G  idle" in text
    assert "vllm  GPU1  60G  idle" in text
    assert "vllm  GPU0  80G" not in text and "vllm  GPU1  80G" not in text


def test_host_and_unknown_model_keep_known_observations(fleet_api, fleet):
    fleet["services"][0].update({"host": True, "container": None, "model": None})
    fleet["services"][0]["window_24h"]["observed_active_ratio"] = 0.28
    assert fleet_api["validate_fleet"](fleet) is fleet
    text = fleet_api["format_fleet"](fleet, width=80)
    assert "Unknown · 1 services · 80G" in text
    assert "unknown  vllm" in text
    assert "observed coverage 50% (24h) · observed active 28%" in text
    assert "id id-a" in text


@pytest.mark.parametrize("command", ["status", "fleet"])
@pytest.mark.parametrize("by", ["person", "gpu"])
def test_host_uid_labels_match_status_fleet_and_other_occupants(fleet_api, fleet, command, by):
    for index, item in enumerate(fleet["services"]):
        item.update(container=None, host=True, host_uid=1000 if index < 2 else 1005,
                    host_user="host-a" if index < 2 else "host-b", mine=False)
    fleet["gpus"][0].update(used_gb=5, occupants=[
        {"container": None, "host": True, "host_uid": 1000, "host_user": "host-a",
         "kind": "other", "used_gb": 5, "service_id": None}])
    args = fleet_api["build_parser"]().parse_args([command, "--by", by, "--show-names"])
    fleet_api["validate_fleet"](fleet)
    text = fleet_api["format_result"](args, fleet, width=80)
    assert "host-a" in text and "host-b" in text and "Host " not in text
    if by == "person":
        assert "host-a · 2 services · 100G" in text
        assert "host-b · 1 services" in text
    else:
        assert "host-a (Work) 5G" in text
        assert "owner host-b" in text
    assert all(fleet_api["cell_width"](line) <= 80 for line in text.splitlines())


def test_cli_groups_host_uid_instead_of_visible_username(fleet_api, fleet):
    for index, item in enumerate(fleet["services"]):
        item.update(container=None, host=True, host_uid=1000 if index < 2 else 1005,
                    host_user="operator", mine=False)
    text = fleet_api["format_fleet"](fleet, width=80, show_names=True)
    assert "operator · 2 services" in text and "operator · 1 services" in text
    assert "operator · 3 services" not in text


@pytest.mark.parametrize("user, expected", [(None, "UID 0"), ("\x00\r\n", "UID 0"),
                                            ("host\x1b[31m\r\n\u202e", "host [31m")])
def test_cli_host_labels_and_history_sanitize_names(fleet_api, fleet, user, expected):
    item = fleet["services"][0]
    item.update(container=None, host=True, host_uid=0, host_user=user, mine=False)
    if user is None:
        item["model"] = None
    fleet["services"] = [item]
    text = fleet_api["format_fleet"](fleet, width=80, show_names=True)
    assert expected in text
    args = fleet_api["build_parser"]().parse_args(["history", item["id"], "--hours", "168"])
    history = {"schema_version": 1, "service_id": item["id"], "hours": 168,
               "resolution": "hourly", "samples": [], "service": copy.deepcopy(item)}
    fleet_api["validate_fleet_history"](args, history)
    rendered = fleet_api["format_fleet_history"](history, width=80, show_names=True)
    assert expected in rendered
    assert all(character not in text + rendered for character in ("\x1b", "\r", "\u202e"))
    assert all(fleet_api["cell_width"](line) <= 80 for line in rendered.splitlines())


@pytest.mark.parametrize("proof", [None, False])
def test_cli_unresolved_other_keeps_unknown_without_host_proof(fleet_api, fleet, proof):
    fleet["gpus"][0]["occupants"] = [
        {"container": None, "host": proof, "host_uid": 1000, "host_user": "unverified",
         "kind": "other", "used_gb": 5, "service_id": None}]
    text = fleet_api["format_fleet"](fleet, width=80, by="gpu")
    assert "Unknown (Work) 5G" in text and "unverified" not in text


@pytest.mark.parametrize("fields", [{"host": "true"}, {"host_uid": True},
                                   {"host_uid": -1}, {"host_user": 123}])
@pytest.mark.parametrize("where", ["service", "occupant"])
def test_cli_rejects_malformed_additive_host_fields(fleet_api, fleet, fields, where):
    item = fleet["services"][0] if where == "service" else fleet["gpus"][0]["occupants"][0]
    item.update(fields)
    with pytest.raises(fleet_api["ClientError"], match="Invalid fleet response"):
        fleet_api["validate_fleet"](fleet)


def test_missing_gpu_probe_does_not_hide_discovered_services(fleet_api, fleet):
    fleet["gpus"] = []
    fleet["errors"] = ["GPU probe unavailable"]
    text = fleet_api["format_fleet"](fleet, width=80, by="gpu")
    assert "WARNING GPU probe unavailable" in text
    assert "GPU1 · ?/?G · utilization ?%" in text
    assert "large-model" in text and "id id-a" in text


@pytest.mark.parametrize("sort,first", [(None, "small-model"), ("mem", "large-model"),
                                       ("tokens", "large-model"), ("idle", "small-model")])
def test_sorting_keeps_unknown_last(fleet_api, fleet, sort, first):
    fleet["services"][2]["container"] = "ctr-a"
    text = fleet_api["format_fleet"](fleet, width=100, sort=sort)
    other = "large-model" if first == "small-model" else "small-model"
    assert text.index(first) < text.index(other) < text.index("unknown-model")


@pytest.mark.parametrize("tty,no_color,plain,colored", [(True, False, False, True), (True, True, False, False),
                                                        (True, False, True, False), (False, False, False, False)])
def test_color_requires_tty_and_respects_no_color(fleet_api, fleet, monkeypatch, capsys, tty, no_color, plain, colored):
    globals_ = fleet_api["main"].__globals__

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, *args):
            return fleet

    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "SchedulerClient", Client)
    monkeypatch.setattr(globals_["sys"].stdout, "isatty", lambda: tty)
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    assert fleet_api["main"](["fleet", *(["--plain"] if plain else [])]) == 0
    output = capsys.readouterr().out
    assert ("\x1b[" in output) == colored


def test_disabled_fleet_never_falls_back_to_shared(fleet_api, monkeypatch, capsys):
    globals_ = fleet_api["main"].__globals__
    paths = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, path):
            paths.append(path)
            raise fleet_api["ClientError"]("HTTP 503: fleet_disabled", status=503, payload={"error": "fleet_disabled"})

    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "SchedulerClient", Client)
    assert fleet_api["main"](["status"]) == 1
    output = capsys.readouterr()
    assert "fleet_disabled" in output.err and output.out == ""
    assert paths == ["/v1/fleet"]


@pytest.mark.parametrize("value", ["+0d", "+1h", "forever", "2020-01-01", "2030-02-30", "2030-01-01T25:00"])
def test_invalid_deadlines_fail_before_request(fleet_api, value):
    with pytest.raises(SystemExit) as exc:
        fleet_api["build_parser"]().parse_args(["claim", "id-a", "--until", value, "--reason", "test"])
    assert exc.value.code == 2


def test_deadline_relative_and_local_calendar(fleet_api):
    from datetime import datetime
    assert fleet_api["claim_until"]("+3d", now=1800000000) == 1800259200
    assert fleet_api["claim_until"]("2030-01-02T12:34", now=1800000000) == datetime(2030, 1, 2, 12, 34).timestamp()
    assert fleet_api["claim_until"]("2030-01-02", now=1800000000) == datetime(2030, 1, 2).timestamp()


@pytest.mark.parametrize("reason", ["", " " * 4, "x" * 201, "reason\x1b[2J"])
def test_invalid_reason_is_rejected(fleet_api, reason):
    with pytest.raises(SystemExit):
        fleet_api["build_parser"]().parse_args(["claim", "id-a", "--until", "+3d", "--reason", reason])


def test_claim_and_unclaim_preview_and_actual_http(fleet_api, fleet_service):
    address, requests, claims = fleet_service
    client = fleet_api["SchedulerClient"](address)
    parser = fleet_api["build_parser"]()
    preview = parser.parse_args(["claim", "large-model", "--until", "+3d", "--reason", "experiment", "--dry-run"])
    result = fleet_api["execute_command"](preview, client, now=1800000000)
    assert "id" not in result["claim"] and claims == {}
    assert "Dry run: would claim" in fleet_api["format_result"](preview, result)
    payload = {"service_id": "id-a", "until": 1800259200, "reason": "experiment"}
    assert requests == [("GET", "/v1/fleet", None), ("POST", "/v1/fleet/claims?dry_run=1", payload)]
    preview.dry_run = False
    receipt = fleet_api["execute_command"](preview, client, now=1800000000)
    id_ = receipt["claim"]["id"]
    assert list(claims) == [id_]
    remove = parser.parse_args(["unclaim", id_, "--dry-run"])
    result = fleet_api["execute_command"](remove, client)
    assert result["claim"]["id"] == id_ and list(claims) == [id_]
    assert requests[-1] == ("DELETE", "/v1/fleet/claims/claim%2Fa%20%25?dry_run=1", None)
    remove.dry_run = False
    fleet_api["execute_command"](remove, client)
    assert claims == {}


def test_ambiguous_model_requires_service_id_without_write(fleet_api, fleet):
    fleet["services"][1]["model"] = "large-model"
    calls = []

    class Client:
        def request(self, *args):
            calls.append(args)
            return fleet

    args = fleet_api["build_parser"]().parse_args(["claim", "large-model", "--until", "+1d", "--reason", "test"])
    with pytest.raises(fleet_api["ClientError"], match="Ambiguous model.*id-a, id-b"):
        fleet_api["execute_command"](args, Client())
    assert calls == [("GET", "/v1/fleet")]
    assert fleet_api["resolve_fleet_service"]("id-a", fleet) == "id-a"


def test_foreign_service_403_remains_authoritative(fleet_api, fleet_service):
    address, requests, _ = fleet_service
    args = fleet_api["build_parser"]().parse_args(["claim", "id-c", "--until", "+1d", "--reason", "test"])
    with pytest.raises(fleet_api["ClientError"], match="forbidden_container") as exc:
        fleet_api["execute_command"](args, fleet_api["SchedulerClient"](address))
    assert exc.value.status == 403
    assert "outcome unknown" not in str(exc.value)
    assert [item[0] for item in requests] == ["GET", "POST"]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("field,value", [("until", "different"), ("reason", "different"), ("until", 1e300)])
def test_claim_receipt_must_match_the_submitted_request_before_success(
        fleet_api, fleet_service, write_response, monkeypatch, capsys, json_output, dry_run, field, value):
    address, requests, claims = fleet_service

    def rewrite(result):
        result["claim"][field] = result["claim"][field] + 3600 if field == "until" and value == "different" else value
        return result

    write_response["rewrite"] = rewrite
    monkeypatch.setitem(fleet_api["main"].__globals__, "load_config", lambda **kwargs: {"url": address})
    command = ["claim", "id-a", "--until", "+1d", "--reason", "experiment"]
    command += ["--json"] if json_output else []
    command += ["--dry-run"] if dry_run else []
    assert fleet_api["main"](command) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "outcome unknown" in output.err and "llm fleet --json" in output.err
    assert "no automatic write retry" in output.err
    assert len([item for item in requests if item[0] == "POST"]) == 1
    assert len(claims) == (0 if dry_run else 1)


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("field,value,dry_run", [
    ("revoked_at", None, False), ("revoked_at", True, False), ("revoked_at", 1e300, False),
    ("until", 1e300, False), ("revoked_at", 1e300, True),
])
def test_unclaim_requires_valid_receipt_and_actual_revocation_before_success(
        fleet_api, fleet_service, write_response, monkeypatch, capsys, json_output, field, value, dry_run):
    address, requests, claims = fleet_service
    claims["claim-a"] = {"id": "claim-a", "service_id": "id-a", "instance_id": "id-a",
                         "until": 1800259200, "reason": "experiment", "revoked_at": None}

    def rewrite(result):
        result["claim"][field] = value
        return result

    write_response["rewrite"] = rewrite
    monkeypatch.setitem(fleet_api["main"].__globals__, "load_config", lambda **kwargs: {"url": address})
    command = ["unclaim", "claim-a"] + (["--json"] if json_output else [])
    command += ["--dry-run"] if dry_run else []
    assert fleet_api["main"](command) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "outcome unknown" in output.err and "llm fleet --json" in output.err
    assert "no automatic write retry" in output.err
    assert len([item for item in requests if item[0] == "DELETE"]) == 1
    assert len(claims) == (1 if dry_run else 0)


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("command", ["claim", "unclaim"])
@pytest.mark.parametrize("failure", ["http_503", "invalid_json"])
def test_failed_write_response_reports_unknown_without_resubmitting(
        fleet_api, fleet_service, write_response, monkeypatch, capsys, json_output, command, failure):
    address, requests, claims = fleet_service
    if command == "unclaim":
        claims["claim-a"] = {"id": "claim-a", "service_id": "id-a", "instance_id": "id-a",
                             "until": 1800259200, "reason": "experiment", "revoked_at": None}
        arguments = ["unclaim", "claim-a"]
    else:
        arguments = ["claim", "id-a", "--until", "+1d", "--reason", "experiment"]
    if failure == "http_503":
        write_response.update(status=503, rewrite=lambda result: {"error": "response_failure"})
    else:
        write_response["raw"] = b"{"
    monkeypatch.setitem(fleet_api["main"].__globals__, "load_config", lambda **kwargs: {"url": address})
    assert fleet_api["main"](arguments + (["--json"] if json_output else [])) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "outcome unknown" in output.err and "llm fleet --json" in output.err
    assert "no automatic write retry" in output.err
    assert len([item for item in requests if item[0] in ("POST", "DELETE")]) == 1


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("command", ["claim", "unclaim"])
def test_failed_http_error_body_preserves_unknown_write_outcome(
        fleet_api, fleet, monkeypatch, capsys, json_output, command):
    requests = []

    class ErrorBody(io.BytesIO):
        def read(self, *args):
            raise TimeoutError("Error response body timed out")

    def open_request(request, **kwargs):
        requests.append(request.method)
        if request.method == "GET":
            return io.BytesIO(json.dumps(fleet).encode())
        raise HTTPError(request.full_url, 503, "Unavailable", {}, ErrorBody())

    client = fleet_api["SchedulerClient"]("http://scheduler:8011", opener=open_request)
    globals_ = fleet_api["main"].__globals__
    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "SchedulerClient", lambda **kwargs: client)
    arguments = (["claim", "id-a", "--until", "+1d", "--reason", "experiment"]
                 if command == "claim" else ["unclaim", "claim-a"])
    assert fleet_api["main"](arguments + (["--json"] if json_output else [])) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "outcome unknown" in output.err and "llm fleet --json" in output.err
    assert "no automatic write retry" in output.err
    assert requests == (["GET", "POST"] if command == "claim" else ["DELETE"])


@pytest.mark.parametrize("command", [["claim", "id-a", "--until", "+1d", "--reason", "test"], ["unclaim", "claim-id"]])
def test_unknown_write_outcome_is_not_retried(fleet_api, fleet, command):
    calls = []

    class Client:
        def request(self, *args):
            calls.append(args)
            if args[0] == "GET":
                return fleet
            raise fleet_api["ClientError"]("Connection ended after request")

    args = fleet_api["build_parser"]().parse_args(command)
    with pytest.raises(fleet_api["ClientError"], match="claim outcome unknown.*no automatic write retry"):
        fleet_api["execute_command"](args, Client())
    assert sum(item[0] != "GET" for item in calls) == 1


@pytest.mark.parametrize("hours", [24, 168])
def test_history_encodes_id_and_preserves_unknown(fleet_api, fleet_service, hours):
    address, requests, _ = fleet_service
    args = fleet_api["build_parser"]().parse_args(["history", "id/a ?%", "--hours", str(hours)])
    result = fleet_api["execute_command"](args, fleet_api["SchedulerClient"](address))
    text = fleet_api["format_result"](args, result, width=80)
    assert "—" in text and "Parameters:" in text and "[REDACTED]" in text
    assert all(fleet_api["cell_width"](line) <= 80 for line in text.splitlines())
    assert parse_qs(urlsplit(requests[0][1]).query) == {"service": ["id/a ?%"], "hours": [str(hours)]}
    args.json = True
    assert json.loads(fleet_api["format_result"](args, result)) == result


def test_shared_json_keeps_old_structure(fleet_api, fleet_service):
    address, requests, _ = fleet_service
    args = fleet_api["build_parser"]().parse_args(["status", "--shared", "--json"])
    result = fleet_api["execute_command"](args, fleet_api["SchedulerClient"](address))
    assert json.loads(fleet_api["format_result"](args, result)) == {"models": [], "reserves": [], "unknown": None}
    assert requests == [("GET", "/v1/state", None)]


def test_invalid_history_sample_cannot_be_hidden_by_later_valid_row(fleet_api):
    args = fleet_api["build_parser"]().parse_args(["history", "id-a"])
    result = {"schema_version": 1, "service_id": "id-a", "hours": 24, "resolution": "minute",
              "samples": [{"ts": 1800000000, "d_gen_tokens": -1}, {"ts": 1800000060, "d_gen_tokens": 5}]}
    with pytest.raises(fleet_api["ClientError"], match="Invalid fleet history response"):
        fleet_api["validate_fleet_history"](args, result)


def test_claim_receipt_must_bind_both_service_identifiers(fleet_api):
    args = fleet_api["build_parser"]().parse_args(["claim", "id-a", "--until", "+1d", "--reason", "test"])
    result = {"ok": True, "claim": {"id": "claim-a", "service_id": "id-a", "instance_id": "id-b",
                                   "until": 1800003600, "reason": "test"}}
    result["dry_run"] = False
    with pytest.raises(fleet_api["ClientError"], match="Invalid fleet claim response"):
        fleet_api["validate_fleet_claim"](args, result, "id-a",
                                         payload={"until": 1800003600, "reason": "test"})


@pytest.mark.parametrize("patch", [{"schema_version": 2}, {"services": [{}]}, {"gpus": [None]}, {"stale": "false"}])
def test_invalid_fleet_is_reported(fleet_api, fleet, patch):
    fleet.update(patch)
    with pytest.raises(fleet_api["ClientError"], match="Invalid fleet response"):
        fleet_api["validate_fleet"](fleet)


@pytest.mark.parametrize("address,access,sensitive", [
    ("http://192.0.2.10:8000", "shared", False),
    ("http://127.0.0.1:8000", "local_only", True),
    ("http://[2001:db8::10]:8000", "direct", True),
    ("http://[::1]:8000/", "local_only", True),
    (None, "unknown", True),
    (None, "shared", False),
])
def test_optional_service_api_metadata_is_preserved(fleet_api, fleet, address, access, sensitive):
    fleet["services"][0].update(api_address=address, api_access=access, idle_time_sensitive=sensitive)
    assert fleet_api["validate_fleet"](fleet) is fleet


def test_old_service_payload_without_api_metadata_remains_compatible(fleet_api, fleet):
    for row in fleet["services"]:
        for key in ("api_address", "api_access", "idle_time_sensitive"):
            del row[key]
    assert fleet_api["validate_fleet"](fleet) is fleet
    assert "API: Unknown" in fleet_api["format_fleet"](fleet)


@pytest.mark.parametrize("patch", [
    {"api_address": True}, {"api_address": "https://192.0.2.10:8000"},
    {"api_address": "http://192.0.2.10"}, {"api_address": "http://192.0.2.10:0"},
    {"api_address": "http://example.test:8000"}, {"api_address": "http://2001:db8::10:8000"},
    {"api_address": "http://[fe80::1%eth0]:8000"}, {"api_address": "http://user:secret@192.0.2.10:8000"},
    {"api_address": "http://192.0.2.10:8000/v1"}, {"api_address": "http://192.0.2.10:8000?token=secret"},
    {"api_address": "http://192.0.2.10:8000#fragment"}, {"api_address": " http://192.0.2.10:8000"},
    {"api_address": "http://192.0.2.10:8000\n"}, {"api_access": "public"}, {"api_access": None},
    {"idle_time_sensitive": 0}, {"idle_time_sensitive": "false"},
])
def test_invalid_optional_service_api_metadata_is_rejected(fleet_api, fleet, patch):
    fleet["services"][0].update(patch)
    with pytest.raises(fleet_api["ClientError"], match="Invalid fleet response"):
        fleet_api["validate_fleet"](fleet)
