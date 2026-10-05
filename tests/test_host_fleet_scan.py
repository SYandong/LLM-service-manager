# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/host/llmsvc-fleet-scan.py"
EXAMPLE = SCRIPT.with_name("fleet-scan.example.json")


def load_scanner():
    spec = importlib.util.spec_from_file_location("host_fleet_scanner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scanner():
    return load_scanner()


def proc_stat(pid, ppid, start, comm="python"):
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 12
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


def tcp_row(port, inode, bind="0.0.0.0"):
    address = socket.inet_aton(bind)[::-1].hex().upper()
    return f"0: {address}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 {inode} 1\n"


def tcp6_row(port, inode, bind="::"):
    raw = socket.inet_pton(socket.AF_INET6, bind)
    address = b"".join(raw[index:index + 4][::-1] for index in range(0, 16, 4)).hex().upper()
    return f"0: {address}:{port:04X} {'0' * 32}:0000 0A 00000000:00000000 00:00000000 00000000 0 0 {inode} 1\n"


def make_process(root, pid, argv, *, ppid=1, start=100, container="team-a", port=None, comm="python", cgroup=None, bind="0.0.0.0"):
    path = root / str(pid)
    path.mkdir(parents=True)
    (path / "stat").write_text(proc_stat(pid, ppid, start, comm))
    (path / "status").write_text(f"Name:\t{comm}\nPPid:\t{ppid}\n")
    (path / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (path / "cgroup").write_text(cgroup or (f"0::/lxc.payload.{container}/workload\n" if container else "0::/user.slice/workload\n"))
    (path / "comm").write_text(comm + "\n")
    (path / "environ").write_bytes(b"UNRELATED_SECRET=never-export-this\0")
    (path / "fd").mkdir()
    (path / "net").mkdir()
    (path / "ns").mkdir()
    (path / "ns/net").write_bytes(b"fake-network-namespace")
    (path / "net/tcp").write_text("header\n" + (tcp_row(port, pid + 5000, bind) if port else ""))
    (path / "net/tcp6").write_text("header\n")
    if port:
        (path / "fd/5").symlink_to(f"socket:[{pid + 5000}]")
    return path


def fake_root(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text("cpu 1 2 3\nbtime 1700000000\n")
    return root


METRICS_022 = b'''# HELP vllm:generation_tokens_total Number of tokens
vllm:generation_tokens_total{model_name="demo-model",engine="0"} 100
vllm:prompt_tokens_total{model_name="demo-model",engine="0"} 200
vllm:prompt_tokens_cached_total{model_name="demo-model",engine="0"} 20
vllm:e2e_request_latency_seconds_count{model_name="demo-model",engine="0"} 10
vllm:num_requests_running{model_name="demo-model",engine="0"} 0
vllm:num_requests_waiting{model_name="demo-model",engine="0"} 1
vllm:kv_cache_usage_perc{model_name="demo-model",engine="0"} 0.5
vllm:generation_tokens_created{model_name="demo-model",engine="0"} 1700000001
'''
METRICS_030 = b'''# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="1",model_name="demo-model"} 50
vllm:generation_tokens_total{engine="0",model_name="demo-model"} 100
vllm:kv_cache_usage_perc{engine="1",model_name="demo-model"} 0.8
vllm:kv_cache_usage_perc{engine="0",model_name="demo-model"} 0.6
vllm:generation_tokens_created{engine="1",model_name="demo-model"} 1700000009
vllm:generation_tokens_created{engine="0",model_name="demo-model"} 1700000001
vllm:engine_sleep_state{engine="1",model_name="demo-model"} 1
vllm:engine_sleep_state{engine="0",model_name="demo-model"} 0
attacker_metric{private_key="do-not-export"} 999
'''


class FakeRunner:
    def __init__(self, apps=b"", metrics=METRICS_022):
        self.apps = apps
        self.metrics = metrics
        self.calls = []
        self.after_get = None

    def __call__(self, argv, timeout, max_output, pass_fds=()):
        self.calls.append((argv, timeout, max_output, pass_fds))
        assert 0 < timeout <= 2
        assert max_output <= 4 * 1024 * 1024
        if argv[1].startswith("--query-gpu="):
            return b"0, GPU-one, 140000, 100000, 70\n1, GPU-two, 140000, 1000, 0\n"
        if argv[1].startswith("--query-compute-apps="):
            return self.apps
        assert argv[1].startswith("--net=/proc/self/fd/")
        assert argv[2:6] == ["--", "/usr/bin/python3", "-I", "-S"]
        assert len(pass_fds) == 1 and str(pass_fds[0]) == argv[1].rsplit("/", 1)[1]
        assert not any(arg in {"--mount", "--pid", "--user", "--target", "-m", "-p", "-U"} for arg in argv)
        if self.after_get:
            self.after_get(argv)
        if "/api/ps" in argv[8]:
            return b'{"models":[{"name":"demo-ollama","size_vram":2048,"expires_at":"2026-10-05T12:00:00Z","ignored_secret":"never-export"}]}'
        return self.metrics


def fixture_config(scanner, root, tmp_path, **overrides):
    config = scanner.load_config(EXAMPLE)
    config.update(proc_root=str(root), output_path=str(tmp_path / "fleet.json"))
    config.update(overrides)
    return config


def test_discovers_engines_aggregates_enginecore_and_redacts(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 100, ["/usr/bin/vllm", "serve", "/models/demo", "--served-model-name", "demo-model", "--port", "8000", "--api-key", "private-api-value", "--hf-token=private-hf-value", "--password", "password-value", "hf_abcdef", "sk-abcdef"], port=8000)
    make_process(root, 101, ["VLLM::EngineCore"], ppid=100, start=120, comm="VLLM::EngineCore")
    make_process(root, 200, ["ollama", "serve"], container="team-b", port=11434, bind="127.0.0.1")
    make_process(root, 300, ["python", "train.py", "--password", "training-secret"], container="team-c", comm="python\x1b[31m")
    make_process(root, 400, ["python", "-m", "sglang.launch_server"], container=None)
    make_process(root, 500, ["llama-server", "--model", "/models/local"], container="llmsvc")
    runner = FakeRunner(b"101, GPU-one, 95000\n300, GPU-two, 900\n")
    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner)
    assert result["schema_version"] == 1
    assert result["sample_interval_seconds"] == 60
    assert result["inventory_complete"] and result["gpu_inventory_complete"] and result["gpu_attribution_complete"]
    assert result["host"] == {"gpu_count": 2}
    by_pid = {row["pid"]: row for row in result["services"]}
    assert set(by_pid) == {100, 200, 400, 500}
    assert by_pid[100]["gpus"] == [{"index": 0, "used_mib": 95000}]
    assert by_pid[100]["model"] == "demo-model"
    assert by_pid[100]["model_path"] == "/models/demo"
    assert by_pid[100]["started_at"] == 1700000000 + 100 / os.sysconf("SC_CLK_TCK")
    assert by_pid[100]["metrics"]["requests_total"] == 10
    assert by_pid[100]["scrape"]["ok"]
    assert by_pid[200]["ollama"]["models"][0]["size_vram"] == 2048
    assert by_pid[400]["host"] and by_pid[400]["container"] is None
    assert by_pid[400]["scrape"]["error"] == "unsupported_engine"
    assert result["other_gpu_processes"] == [{"container": "team-c", "pid": 300, "gpu": 1, "used_mib": 900, "comm": "python"}]
    exported = json.dumps(result)
    for secret in ("private-api-value", "private-hf-value", "password-value", "hf_abcdef", "sk-abcdef", "training-secret", "UNRELATED_SECRET", "ignored_secret", "never-export", "\u001b"):
        assert secret not in exported
    assert len(by_pid[100]["argv_redacted"]) <= 1024


def test_instance_id_changes_on_restart_and_detects_managed_cgroup(scanner, tmp_path):
    root = fake_root(tmp_path)
    process_path = make_process(root, 100, ["python", "-m", "vllm.entrypoints.openai.api_server", "--model", "demo"], cgroup="0::/lxc.payload.llmsvc/system.slice/vllm-demo.service\n", container="llmsvc", port=8000)
    config = fixture_config(scanner, root, tmp_path)
    before = scanner.scan(config, FakeRunner())["services"][0]
    (process_path / "stat").write_text(proc_stat(100, 1, 200))
    after = scanner.scan(config, FakeRunner())["services"][0]
    assert before["managed_by"] == "llmsvc"
    assert before["id"] != after["id"]
    assert before["engine_version"] is None


def test_nested_service_candidates_are_collapsed_independent_of_proc_order(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 101, ["vllm", "serve", "demo"], ppid=100, start=110)
    make_process(root, 100, ["vllm", "serve", "demo"], start=100, port=8000)
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner(b"101, GPU-one, 500\n"))
    assert [row["pid"] for row in result["services"]] == [100]
    assert result["services"][0]["gpus"] == [{"index": 0, "used_mib": 500}]


def test_parent_pid_reuse_and_cross_container_do_not_merge_gpu_process(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 100, ["vllm", "serve", "demo"], start=200, port=8000)
    make_process(root, 101, ["VLLM::EngineCore"], ppid=100, start=100)
    make_process(root, 102, ["VLLM::EngineCore"], ppid=100, start=300, container="team-b")
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner(b"101, GPU-one, 500\n102, GPU-one, 600\n"))
    assert result["services"][0]["gpus"] == []
    assert {row["pid"] for row in result["other_gpu_processes"]} == {101, 102}


@pytest.mark.parametrize("which", ["stat", "namespace", "listener"])
def test_response_discarded_on_identity_races(scanner, tmp_path, which):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["vllm", "serve", "demo"], port=8000)
    runner = FakeRunner(b"100, GPU-one, 500\n")

    def race(argv):
        if which == "stat":
            (path / "stat").write_text(proc_stat(100, 1, 999))
        elif which == "namespace":
            replacement = path / "ns/new-net"
            replacement.write_bytes(b"new-namespace")
            os.replace(replacement, path / "ns/net")
        else:
            (path / "fd/5").unlink()
            (path / "fd/5").symlink_to("socket:[999999]")

    runner.after_get = race
    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner)
    row = result["services"][0]
    assert not row["scrape"]["ok"]
    assert row["scrape"]["error"] == "process_identity_changed"
    assert row["metrics"] == {} and row["gpus"] == []
    assert not result["gpu_attribution_complete"]


def test_refuses_spoofed_listener_and_argv_endpoint(scanner, tmp_path):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["vllm", "serve", "demo", "--port", "9000"], port=8000)
    runner = FakeRunner()
    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner)
    assert result["services"][0]["scrape"]["error"] == "listener_identity_unavailable"
    assert len(runner.calls) == 2
    (path / "cmdline").write_bytes(b"vllm\0serve\0demo\0")
    (path / "fd/5").unlink()
    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner)
    assert not result["services"][0]["scrape"]["ok"]
    assert len(runner.calls) == 4


def test_ollama_host_only_and_ipv6_socket_decode(scanner, tmp_path):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["ollama", "serve"], port=12345, bind="127.0.0.1")
    (path / "environ").write_bytes(b"SECRET=do-not-read-out\0OLLAMA_HOST=http://127.0.0.1:12345\0")
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner())
    assert result["services"][0]["port"] == 12345
    assert result["services"][0]["scrape"]["ok"]
    assert "do-not-read-out" not in json.dumps(result)
    assert scanner.decode_address("00000000000000000000000001000000:1F40", socket.AF_INET6) == ("::1", 8000)


@pytest.mark.parametrize("explicit_port", [False, True])
@pytest.mark.parametrize("configured,observed,endpoint", [
    ("0.0.0.0", "::", "[::1]"),
    ("[::]", "0.0.0.0", "127.0.0.1"),
])
def test_ollama_cross_family_wildcard_uses_actual_owned_listener(
        scanner, tmp_path, explicit_port, configured, observed, endpoint):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["ollama", "serve"], port=11434)
    host = configured + (":11434" if explicit_port else "")
    (path / "environ").write_bytes(f"OLLAMA_HOST={host}\0".encode())
    if observed == "::":
        (path / "net/tcp").write_text("header\n")
        (path / "net/tcp6").write_text("header\n" + tcp6_row(11434, 5100))
    runner = FakeRunner()
    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner)
    row = result["services"][0]
    assert row["bind"] == observed and row["port"] == 11434
    assert row["scrape"]["ok"] and row["scrape"]["error"] is None
    assert runner.calls[-1][0][8] == f"http://{endpoint}:11434/api/ps"
    assert row["ollama"]["models"][0]["name"] == "demo-ollama"
    assert row["metrics"] == {}


@pytest.mark.parametrize("failure", ["different_port", "specific_ipv4", "specific_ipv6", "two_wildcards", "unowned"])
def test_ollama_wildcard_selection_keeps_endpoint_identity_checks(scanner, tmp_path, failure):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["ollama", "serve"], port=11434)
    (path / "environ").write_bytes(b"OLLAMA_HOST=0.0.0.0:11434\0")
    (path / "net/tcp").write_text("header\n")
    observed = "::1" if failure == "specific_ipv6" else "::"
    port = 9000 if failure == "different_port" else 11434
    (path / "net/tcp6").write_text("header\n" + tcp6_row(port, 5100, observed))
    if failure == "specific_ipv4":
        (path / "net/tcp6").write_text("header\n")
        (path / "net/tcp").write_text("header\n" + tcp_row(11434, 5100, "127.0.0.1"))
    elif failure == "two_wildcards":
        (path / "fd/6").symlink_to("socket:[5101]")
        (path / "net/tcp").write_text("header\n" + tcp_row(11434, 5101))
    elif failure == "unowned":
        (path / "fd/5").unlink()
    runner = FakeRunner()
    row = scanner.scan(fixture_config(scanner, root, tmp_path), runner)["services"][0]
    assert not row["scrape"]["ok"]
    assert row["scrape"]["error"] == "listener_identity_unavailable"
    assert len(runner.calls) == 2


def test_ollama_wildcard_listener_identity_change_discards_response(scanner, tmp_path):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["ollama", "serve"], port=11434)
    (path / "environ").write_bytes(b"OLLAMA_HOST=0.0.0.0:11434\0")
    (path / "net/tcp").write_text("header\n")
    (path / "net/tcp6").write_text("header\n" + tcp6_row(11434, 5100))
    runner = FakeRunner()
    runner.after_get = lambda argv: (path / "net/tcp6").write_text("header\n")
    row = scanner.scan(fixture_config(scanner, root, tmp_path), runner)["services"][0]
    assert row["scrape"]["error"] == "process_identity_changed"
    assert not row["scrape"]["ok"] and row["ollama"] is None


def test_ollama_cross_family_wildcard_empty_model_list_is_success(scanner, tmp_path):
    root = fake_root(tmp_path)
    path = make_process(root, 100, ["ollama", "serve"], port=11434)
    (path / "environ").write_bytes(b"OLLAMA_HOST=0.0.0.0\0")
    (path / "net/tcp").write_text("header\n")
    (path / "net/tcp6").write_text("header\n" + tcp6_row(11434, 5100))
    runner = FakeRunner()

    def empty_models(argv, timeout, max_output, pass_fds=()):
        response = runner(argv, timeout, max_output, pass_fds)
        return b'{"models":[]}' if any(arg.endswith("/api/ps") for arg in argv) else response

    row = scanner.scan(fixture_config(scanner, root, tmp_path), empty_models)["services"][0]
    assert row["scrape"]["ok"] and row["ollama"] == {"models": []}
    assert row["model"] is None and row["metrics"] == {}


def test_whitelist_series_identity_created_epochs_and_worker_gauges(scanner):
    metrics, identity = scanner.parse_metrics(METRICS_030, scanner.Budget(20), with_identity=True)
    assert metrics["generation_tokens_total"] == 150
    assert metrics["generation_tokens_created"] == 1700000009
    assert metrics["kv_cache_usage_perc"] == pytest.approx(0.7)
    assert "engine_sleep_state" not in metrics
    assert "attacker_metric" not in metrics
    changed = METRICS_030.replace(b'engine="1"', b'engine="2"')
    assert scanner.parse_metrics(changed, scanner.Budget(20), with_identity=True)[1] != identity
    reordered = b"\n".join(reversed(METRICS_030.splitlines()))
    assert scanner.parse_metrics(reordered, scanner.Budget(20), with_identity=True)[1] == identity


@pytest.mark.parametrize("data", [
    b'vllm:generation_tokens_total{model_name="x"} NaN\n',
    b'vllm:generation_tokens_total{model_name="x"} Inf\n',
    b'vllm:generation_tokens_total{model_name="x"} -1\n',
    b'vllm:generation_tokens_total{model_name="x"} 1e999\n',
    b'vllm:generation_tokens_total{bad="unterminated} 1\n',
    b'vllm:generation_tokens_total{model_name="' + b"a" * 9000 + b'"} 1\n',
    b'vllm:generation_tokens_total{model_name="x"} 1\nvllm:generation_tokens_total{model_name="x"} 2\n',
    b'vllm:kv_cache_usage_perc{model_name="x"} 9\n',
])
def test_malicious_metrics_fail_bounded(scanner, data):
    with pytest.raises((scanner.ScanError, ValueError)):
        scanner.parse_metrics(data, scanner.Budget(20))


def test_sanitizes_terminal_labels_models_and_argv(scanner):
    raw = scanner.redact_argv(["vllm", "serve", "demo\x1b[31m\n", "--my-secret", "value", "--private-key=other", "https://user:pass@example.test/model?token=x", "sk-secret", "hf_abcdef"])
    assert "value" not in raw and "other" not in raw and "user:pass" not in raw
    assert "token=x" not in raw and "sk-secret" not in raw and "hf_abcdef" not in raw
    assert "\x1b" not in raw and "\n" not in raw
    assert scanner.safe_text("python\x1b]52;c;secret\x07", 64).find("\x1b") == -1
    assert "\u202e" not in scanner.safe_text("demo\u202e-model")
    raw = scanner.redact_argv(["vllm", "serve", "demo", "--api-key", "secret-one", "secret-two", "--port", "8000"])
    assert "secret-one" not in raw and "secret-two" not in raw and "8000" in raw


@pytest.mark.parametrize("url", [
    "HTTP://synthetic-user:synthetic-pass@example.invalid/model?credential=synthetic-query",
    "hTtPs://synthetic-user:synthetic-pass@example.invalid/model#synthetic-fragment",
    "HTTP://synthetic%2Duser:synthetic%2Dpass@example.invalid/model?credential=synthetic%2Dquery",
    "HTTP://synthetic-user%3Asynthetic-pass%40example.invalid/model?credential=synthetic-query",
])
def test_url_redaction_is_scheme_aware_and_handles_encoded_userinfo(scanner, url):
    redacted = scanner.safe_text(url, 512)
    argv = scanner.redact_argv(["vllm", "serve", url])
    for output in (redacted, argv):
        for private in ("synthetic-user", "synthetic-pass", "synthetic-query", "synthetic-fragment", "synthetic%2Duser", "synthetic%2Dpass", "synthetic%2Dquery"):
            assert private not in output
        assert "?" not in output and "#" not in output


def test_response_limit_and_untrusted_container_names_become_unknown(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 100, ["vllm", "serve", "demo"], port=8000)
    runner = FakeRunner(metrics=b"x" * 1025)
    result = scanner.scan(fixture_config(scanner, root, tmp_path, max_response_bytes=1024), runner)
    assert result["services"][0]["scrape"]["error"] == "response_limit"
    assert result["services"][0]["metrics"] == {}
    (root / "100/cgroup").write_text("0::/lxc.payload.team-a\u202e/workload\n")
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner(b"100, GPU-one, 500\n"))
    assert not result["inventory_complete"] and result["services"] == []
    assert result["other_gpu_processes"][0]["container"] is None
    assert result["other_gpu_processes"][0]["comm"] == "unknown"


@pytest.mark.parametrize("stat_state", ["missing", "unreadable", "nonnumeric", "negative", "zero", "future"])
def test_boot_time_requires_canonical_stat_and_retains_previous(scanner, tmp_path, monkeypatch, capsys, stat_state):
    root = fake_root(tmp_path)
    make_process(root, 100, ["vllm", "serve", "demo"], port=8000)
    config = fixture_config(scanner, root, tmp_path)
    previous = scanner.scan(config, FakeRunner(), wall_clock=lambda: 1700000500)
    scanner.atomic_write(config["output_path"], previous)
    path = Path(config["output_path"])
    previous_bytes = path.read_bytes()
    stat = root / "stat"
    if stat_state in {"missing", "unreadable"}:
        stat.unlink()
        if stat_state == "unreadable":
            stat.mkdir()
    else:
        invalid_values = {"nonnumeric": "unknown", "negative": "-1", "zero": "0", "future": "1700000600"}
        stat.write_text(f"cpu 1 2 3\nbtime {invalid_values[stat_state]}\n")
    (root / "uptime").write_text("500.01 0\n")
    runner = FakeRunner()
    original_scan = scanner.scan
    monkeypatch.setattr(scanner, "scan", lambda config: original_scan(config, runner, wall_clock=lambda: 1700000500))
    with pytest.raises(scanner.ScanError, match="^boot_time_unavailable$"):
        scanner.scan(config)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    assert scanner.main(["--config", str(config_path)]) == 1
    assert path.read_bytes() == previous_bytes
    assert path.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.glob(".fleet-*.tmp")) == []
    assert runner.calls == []
    assert json.loads(capsys.readouterr().err)["snapshot_retained"] is True


def test_service_start_identity_is_stable_across_scans(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 100, ["vllm", "serve", "demo"], port=8000)
    config = fixture_config(scanner, root, tmp_path)
    (root / "uptime").write_text("500.01 0\n")
    first = scanner.scan(config, FakeRunner(), wall_clock=lambda: 1700000500.005)
    (root / "uptime").write_text("560.02 0\n")
    second = scanner.scan(config, FakeRunner(), wall_clock=lambda: 1700000560.019)
    assert first["generated_at"] != second["generated_at"]
    assert first["services"][0]["started_at"] == 1700000000 + 100 / os.sysconf("SC_CLK_TCK")
    for key in ("id", "pid", "started_at"):
        assert first["services"][0][key] == second["services"][0][key]


@pytest.mark.parametrize("failure", ["gpus", "apps", "unmapped"])
def test_failed_gpu_scan_has_explicit_completeness(scanner, tmp_path, failure):
    root = fake_root(tmp_path)
    make_process(root, 100, ["vllm", "serve", "demo"], port=8000)
    fake = FakeRunner(b"99999, GPU-one, 700\n" if failure == "unmapped" else b"")

    def runner(argv, timeout, max_output, pass_fds=()):
        if failure == "gpus" and argv[1].startswith("--query-gpu=") or failure == "apps" and argv[1].startswith("--query-compute-apps="):
            raise scanner.ScanError("command_failed")
        return fake(argv, timeout, max_output, pass_fds)

    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner)
    assert result["services"][0]["scrape"]["ok"]
    assert not result["gpu_attribution_complete"]
    assert not result["services"][0]["gpu_observation_complete"]
    if failure == "gpus":
        assert result["gpus"] == []
        assert result["host"]["gpu_count"] is None
        assert not result["gpu_inventory_complete"]
    elif failure == "unmapped":
        assert result["other_gpu_processes"] == [{"container": None, "pid": 99999, "gpu": 0, "used_mib": 700, "comm": "unknown"}]


def test_limits_and_budget_cover_discovery_and_metrics_parsing(scanner, tmp_path):
    root = fake_root(tmp_path)
    for pid in (100, 101):
        make_process(root, pid, ["vllm", "serve", "demo"], port=8000)
    result = scanner.scan(fixture_config(scanner, root, tmp_path, max_processes=1), FakeRunner())
    assert not result["inventory_complete"]
    assert "proc_process_limit" in result["errors"]
    ticks = [0.0]

    def clock():
        ticks[0] += 0.01
        return ticks[0]

    result = scanner.scan(fixture_config(scanner, root, tmp_path, scan_budget_seconds=0.2), FakeRunner(), clock=clock)
    assert not result["inventory_complete"] and not result["gpu_inventory_complete"]
    assert "scan_budget_exceeded" in result["errors"]
    with pytest.raises(scanner.BudgetExceeded):
        scanner.parse_metrics(METRICS_022 * 200, scanner.Budget(0.1, clock))


@pytest.mark.parametrize("cmdline_bytes", [64 * 1024 + 1, 128 * 1024, 256 * 1024])
def test_long_ordinary_cmdline_preserves_complete_discovery(scanner, tmp_path, cmdline_bytes):
    root = fake_root(tmp_path)
    prefix = ["python", "testdummy"]
    filler = "x" * (cmdline_bytes - sum(len(arg) + 1 for arg in prefix) - 1)
    ordinary = make_process(root, 100, prefix + [filler], container="ctr-a", comm="python")
    make_process(root, 200, ["vllm", "serve", "demo"], container="ctr-a", port=8000)
    assert (ordinary / "cmdline").stat().st_size == cmdline_bytes
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner(b"100, GPU-one, 1000\n"))
    assert result["inventory_complete"] is True
    assert result["gpu_attribution_complete"] is True
    assert [row["pid"] for row in result["services"]] == [200]
    assert result["other_gpu_processes"] == [{"container": "ctr-a", "pid": 100, "gpu": 0, "used_mib": 1000, "comm": "python"}]
    assert result["errors"] == []
    assert "testdummy" not in json.dumps(result) and filler not in json.dumps(result)


@pytest.mark.parametrize("argc,cmdline_bytes", [(2048, 32 * 1024), (4096, 128 * 1024)])
def test_many_ordinary_arguments_preserve_complete_discovery(scanner, tmp_path, argc, cmdline_bytes):
    root = fake_root(tmp_path)
    argv = ["python", "testdummy"] + ["item"] * (argc - 3)
    argv.append("x" * (cmdline_bytes - sum(len(arg) + 1 for arg in argv) - 1))
    ordinary = make_process(root, 100, argv, container="ctr-a")
    make_process(root, 200, ["vllm", "serve", "demo"], container="ctr-a", port=8000)
    assert len(argv) == argc and (ordinary / "cmdline").stat().st_size == cmdline_bytes
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner())
    assert result["inventory_complete"] is True
    assert result["gpu_attribution_complete"] is True
    assert [row["pid"] for row in result["services"]] == [200]
    assert result["errors"] == []


def test_oversized_argument_count_keeps_discovery_incomplete(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 100, ["python", "testdummy"] + ["item"] * 4095, container="ctr-a")
    make_process(root, 200, ["vllm", "serve", "demo"], container="ctr-a", port=8000)
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner())
    assert result["inventory_complete"] is False
    assert result["gpu_attribution_complete"] is False
    assert "proc_argv_limit" in result["errors"]
    assert [row["pid"] for row in result["services"]] == [200]


def test_many_service_arguments_keep_redacted_export_bounded(scanner, tmp_path):
    root = fake_root(tmp_path)
    argv = ["vllm", "serve", "demo", "--api-key", "synthetic-secret-value"]
    argv += [f"synthetic-item-{index}" for index in range(4096 - len(argv))]
    make_process(root, 100, argv, container="ctr-a", port=8000)
    config = fixture_config(scanner, root, tmp_path)
    result = scanner.scan(config, FakeRunner())
    assert result["inventory_complete"] is True
    assert [row["pid"] for row in result["services"]] == [100]
    assert len(result["services"][0]["argv_redacted"].encode()) <= 1024
    assert "[REDACTED]" in result["services"][0]["argv_redacted"]
    scanner.atomic_write(config["output_path"], result, config["max_snapshot_bytes"])
    exported = Path(config["output_path"]).read_bytes()
    assert config["max_snapshot_bytes"] == 2 * 1024 * 1024
    assert len(exported) <= config["max_snapshot_bytes"]
    assert b"synthetic-secret-value" not in exported
    assert b"synthetic-item-4090" not in exported


def test_oversized_cmdline_keeps_discovery_incomplete(scanner, tmp_path):
    root = fake_root(tmp_path)
    make_process(root, 100, ["python", "testdummy", "x" * (256 * 1024)], container="ctr-a")
    make_process(root, 200, ["vllm", "serve", "demo"], container="ctr-a", port=8000)
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner())
    assert result["inventory_complete"] is False
    assert result["gpu_attribution_complete"] is False
    assert "proc_file_limit" in result["errors"]


def test_long_cmdline_still_obeys_global_proc_byte_budget(scanner, tmp_path):
    assert scanner.load_config()["max_proc_bytes"] == 64 * 1024 * 1024
    root = fake_root(tmp_path)
    make_process(root, 100, ["python", "testdummy", "x" * (128 * 1024)], container="ctr-a")
    make_process(root, 200, ["vllm", "serve", "demo"], container="ctr-a", port=8000)
    result = scanner.scan(fixture_config(scanner, root, tmp_path, max_proc_bytes=64 * 1024), FakeRunner())
    assert result["inventory_complete"] is False
    assert result["gpu_attribution_complete"] is False
    assert "proc_file_limit" in result["errors"]


def test_long_cmdline_still_obeys_global_time_budget(scanner, tmp_path, monkeypatch):
    assert scanner.load_config()["scan_budget_seconds"] == 20
    root = fake_root(tmp_path)
    make_process(root, 100, ["python", "testdummy", "x" * (128 * 1024)], container="ctr-a")
    make_process(root, 200, ["vllm", "serve", "demo"], container="ctr-a", port=8000)
    ticks = [0.0]
    original_read = scanner.ProcReader.read

    def read(reader, relative, limit):
        result = original_read(reader, relative, limit)
        if relative == "100/cmdline":
            ticks[0] = 20
        return result

    monkeypatch.setattr(scanner.ProcReader, "read", read)
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner(), clock=lambda: ticks[0])
    assert result["inventory_complete"] is False
    assert result["gpu_inventory_complete"] is False
    assert result["gpu_attribution_complete"] is False
    assert "scan_budget_exceeded" in result["errors"]


def test_remaining_targets_skipped_after_global_budget(scanner, tmp_path):
    root = fake_root(tmp_path)
    for pid in (100, 200):
        make_process(root, pid, ["vllm", "serve", "demo"], port=8000)
    ticks = [0.0]
    fake = FakeRunner()

    def runner(argv, timeout, max_output, pass_fds=()):
        if pass_fds:
            ticks[0] = 20
        return fake(argv, timeout, max_output, pass_fds)

    result = scanner.scan(fixture_config(scanner, root, tmp_path), runner, clock=lambda: ticks[0])
    assert all(row["scrape"]["error"] == "scrape_skipped" for row in result["services"])
    assert "scan_budget_exceeded" in result["errors"]


def test_atomic_0644_size_limit_and_failure_retains_previous(scanner, tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"
    path.write_bytes(b"previous\n")
    with pytest.raises(scanner.ScanError):
        scanner.atomic_write(path, {"payload": "x" * 500}, max_bytes=20)
    assert path.read_bytes() == b"previous\n"
    monkeypatch.setattr(scanner.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("private-path-secret")))
    with pytest.raises(OSError):
        scanner.atomic_write(path, {"ok": True})
    assert path.read_bytes() == b"previous\n"
    assert list(tmp_path.glob(".fleet-*.tmp")) == []
    monkeypatch.undo()
    scanner.atomic_write(path, {"schema_version": 1})
    assert json.loads(path.read_bytes()) == {"schema_version": 1}
    assert path.stat().st_mode & 0o777 == 0o644


def test_overall_failure_and_dry_run_write_nothing(scanner, tmp_path, monkeypatch, capsys):
    path = tmp_path / "fleet.json"
    path.write_bytes(b"previous\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"output_path": str(path)}))
    monkeypatch.setattr(scanner, "scan", lambda *args: (_ for _ in ()).throw(RuntimeError("private-secret")))
    assert scanner.main(["--config", str(config)]) == 1
    assert path.read_bytes() == b"previous\n"
    assert "private-secret" not in capsys.readouterr().err
    monkeypatch.setattr(scanner, "scan", lambda *args: {"schema_version": 1})
    assert scanner.main(["--config", str(config), "--dry-run"]) == 0
    assert path.read_bytes() == b"previous\n"


def test_config_is_valid_json_bounded_and_rules_configurable(scanner, tmp_path):
    assert scanner.load_config(EXAMPLE)["sample_interval_seconds"] == 60
    assert scanner.load_config(EXAMPLE)["max_services"] == 256
    assert scanner.load_config()["max_services"] == 256
    for overrides in ({"scan_budget_seconds": 21}, {"target_timeout_seconds": 3}, {"max_response_bytes": 5 * 1024 * 1024}, {"output_path": "/tmp/../escape"}, {"match_rules": [{"engine": "vllm", "all": "bad"}]}, {"other": 1}):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(overrides))
        with pytest.raises(scanner.ScanError):
            scanner.load_config(path)
    process = scanner.Process(1, 0, 1, ["custom-server"], "", None, "server")
    assert scanner.engine_for(process, [{"engine": "vllm", "all": ["custom-server"]}]) == "vllm"


@pytest.mark.parametrize("overrides", [
    {"sample_interval_seconds": 1},
    {"sample_interval_seconds": 29},
    {"sample_interval_seconds": 301},
    {"sample_interval_seconds": 86400},
    {"max_services": 257},
    {"max_services": 512},
])
def test_config_rejects_values_outside_consumer_bounds(scanner, tmp_path, overrides):
    path = tmp_path / "out-of-contract.json"
    path.write_text(json.dumps(overrides))
    with pytest.raises(scanner.ScanError, match="invalid_config_bound"):
        scanner.load_config(path)


@pytest.mark.parametrize("overrides", [
    {"sample_interval_seconds": 30},
    {"sample_interval_seconds": 300},
    {"max_services": 256},
])
def test_config_accepts_inclusive_consumer_bounds(scanner, tmp_path, overrides):
    path = tmp_path / "boundary.json"
    path.write_text(json.dumps(overrides))
    config = scanner.load_config(path)
    assert all(config[key] == value for key, value in overrides.items())


def test_service_inventory_cap_marks_truncated_export_incomplete(scanner, tmp_path):
    root = fake_root(tmp_path)
    for pid in range(100, 357):
        make_process(root, pid, ["vllm", "serve", "demo"], port=8000)
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner())
    assert len(result["services"]) == 256
    assert result["inventory_complete"] is False
    assert result["gpu_attribution_complete"] is False
    assert "service_limit" in result["errors"]


def test_command_runner_is_no_shell_bounds_output_and_timeout(scanner, monkeypatch):
    original = scanner.subprocess.Popen
    calls = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(scanner.subprocess, "Popen", spy)
    assert scanner.run_bounded([sys.executable, "-c", "print('ok')"], 1, 100) == b"ok\n"
    assert calls[0]["shell"] is False
    assert calls[0]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    with pytest.raises(scanner.ScanError, match="command_output_limit"):
        scanner.run_bounded([sys.executable, "-c", "print('x'*200000)"], 1, 1000)
    started = time.monotonic()
    with pytest.raises(scanner.ScanError, match="command_timeout"):
        scanner.run_bounded([sys.executable, "-c", "import time; time.sleep(5)"], 0.05, 1000)
    assert time.monotonic() - started < 0.5


def test_http_helper_get_only_no_redirect_proxies_and_body_limit(scanner, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            if self.server.redirect:
                self.send_response(302)
                self.send_header("Location", "/private-endpoint")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 200)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.redirect = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    command = [sys.executable, str(SCRIPT), "--http-helper", f"http://127.0.0.1:{server.server_port}/metrics", "--helper-limit", "100", "--helper-timeout", "1"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=2)
        assert result.returncode == 20 and result.stdout == b""
        assert requests == [("GET", "/metrics")]
        server.redirect = False
        result = subprocess.run(command, capture_output=True, timeout=2)
        assert result.returncode == 22 and result.stdout == b""
        command[command.index("100")] = "300"
        result = subprocess.run(command, capture_output=True, timeout=2)
        assert result.returncode == 0 and result.stdout == b"x" * 200
        assert all(method == "GET" and path == "/metrics" for method, path in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert scanner.http_helper("http://example.test/metrics", 100, 1) == 25
    assert scanner.http_helper("http://127.0.0.1:1/private", 100, 1) == 25


def test_twenty_synthetic_targets_complete_under_three_seconds(scanner, tmp_path):
    root = fake_root(tmp_path)
    apps = []
    for index in range(20):
        pid = 100 + index * 2
        make_process(root, pid, ["vllm", "serve", f"demo-{index}"], port=8000)
        make_process(root, pid + 1, ["VLLM::EngineCore"], ppid=pid, start=120)
        apps.append(f"{pid + 1}, GPU-one, 1000\n")
    started = time.monotonic()
    result = scanner.scan(fixture_config(scanner, root, tmp_path), FakeRunner("".join(apps).encode()))
    elapsed = time.monotonic() - started
    assert len(result["services"]) == 20
    assert all(row["scrape"]["ok"] for row in result["services"])
    assert result["inventory_complete"] and result["gpu_attribution_complete"]
    assert elapsed < 3
