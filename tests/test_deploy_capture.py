# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
import sys
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "capture.py"


def load_capture():
    loader = SourceFileLoader("deploy_capture", str(SCRIPT))
    spec = importlib.util.spec_from_loader("deploy_capture", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_config(tmp_path, sources):
    path = tmp_path / "capture.json"
    path.write_text(json.dumps({"timeout_seconds": 1, "max_output_bytes": 64, "sources": sources}), encoding="utf-8")
    return path


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size):
        return self.payload[:size]


def read_snapshot(output_dir):
    paths = sorted(output_dir.glob("snapshot-*.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def test_capture_success_writes_private_snapshot_and_manifest(tmp_path, monkeypatch, capsys):
    capture = load_capture()
    config = write_config(tmp_path, [
        {"name": "cmd", "type": "command", "argv": [sys.executable, "-c", "print('0, GPU')"]},
        {"name": "state", "type": "http_json", "url": "http://127.0.0.1:8011/v1/state"},
    ])
    output_dir = tmp_path / "out"

    monkeypatch.setattr(capture.urllib.request, "urlopen", lambda request, timeout: FakeResponse(b'{"read_only": true}'))

    assert capture.main(["--config", str(config), "--output-dir", str(output_dir)]) == 0

    reported = json.loads(capsys.readouterr().out)
    snapshot_path, snapshot = read_snapshot(output_dir)
    manifest_path = Path(reported["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert snapshot["read_only"] is True
    assert snapshot["dry_run"] is False
    assert [source["status"] for source in snapshot["sources"]] == ["ok", "ok"]
    assert snapshot["sources"][1]["json"] == {"read_only": True}
    assert manifest["snapshot"] == snapshot_path.name
    assert manifest["sha256"] == reported["snapshot_sha256"]
    assert oct(snapshot_path.stat().st_mode & 0o777) == "0o600"
    assert oct(manifest_path.stat().st_mode & 0o777) == "0o600"


def test_command_failure_and_http_unavailable_are_per_source_results(tmp_path, monkeypatch):
    capture = load_capture()
    config = write_config(tmp_path, [
        {"name": "cmd", "type": "command", "argv": [sys.executable, "-c", "import sys; print('raw token=abc'); sys.exit(7)"]},
        {"name": "state", "type": "http_json", "url": "http://user:secret@127.0.0.1:8011/v1/state"},
    ])

    def fail_urlopen(request, timeout):
        raise urllib.error.URLError("connection refused token=abc")

    monkeypatch.setattr(capture.urllib.request, "urlopen", fail_urlopen)

    assert capture.main(["--config", str(config), "--output-dir", str(tmp_path / "out")]) == 0

    snapshot = read_snapshot(tmp_path / "out")[1]
    assert [source["status"] for source in snapshot["sources"]] == ["unavailable", "unavailable"]
    assert snapshot["sources"][0]["returncode"] == 7
    assert "raw token=abc" in snapshot["sources"][0]["stdout"]
    assert "<redacted>" in snapshot["sources"][1]["detail"]


def test_real_command_flood_is_retained_to_configured_limit(tmp_path):
    capture = load_capture()
    config = write_config(tmp_path, [{
        "name": "flood",
        "type": "command",
        "argv": [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('o' * 200000); sys.stderr.write('e' * 200000)",
        ],
        "max_output_bytes": 32,
    }])

    assert capture.main(["--config", str(config), "--output-dir", str(tmp_path / "out")]) == 0

    snapshot = read_snapshot(tmp_path / "out")[1]
    source = snapshot["sources"][0]
    assert source["status"] == "ok"
    assert len(source["stdout"]) == 32
    assert len(source["stderr"]) == 32
    assert source["truncated"] == {"stdout": True, "stderr": True}
    assert source["total_bytes"]["stdout"] == 200000
    assert source["total_bytes"]["stderr"] == 200000


def test_real_command_timeout_is_explicit_and_bounded_with_inherited_pipe(tmp_path):
    capture = load_capture()
    config = write_config(tmp_path, [{
        "name": "slow",
        "type": "command",
        "argv": [
            sys.executable,
            "-c",
            "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=sys.stdout, stderr=sys.stderr); time.sleep(30)",
        ],
        "timeout_seconds": 0.1,
    }])
    started = capture.time_monotonic()
    assert capture.main(["--config", str(config), "--output-dir", str(tmp_path / "out")]) == 0
    elapsed = capture.time_monotonic() - started

    snapshot = read_snapshot(tmp_path / "out")[1]
    assert snapshot["sources"][0]["status"] == "timeout"
    assert snapshot["sources"][0]["error"] == "timeout"
    assert elapsed < 2.5


def test_dry_run_validates_but_runs_no_io_or_file_mutation(tmp_path, monkeypatch, capsys):
    capture = load_capture()
    config = write_config(tmp_path, [
        {"name": "cmd", "type": "command", "argv": ["nvidia-smi"]},
        {"name": "state", "type": "http_json", "url": "http://127.0.0.1:8011/v1/state"},
    ])
    calls = []

    monkeypatch.setattr(capture.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(capture.urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args))

    assert capture.main(["--dry-run", "--config", str(config), "--output-dir", str(tmp_path / "out")]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert calls == []
    assert plan["snapshot"]["dry_run"] is True
    assert plan["snapshot"]["sources"] == []
    assert not (tmp_path / "out").exists()


def test_invalid_url_and_mutation_method_are_rejected(tmp_path, capsys):
    capture = load_capture()
    bad_url = write_config(tmp_path, [{"name": "bad", "type": "http_json", "url": "file:///etc/passwd"}])
    assert capture.main(["--config", str(bad_url), "--output-dir", str(tmp_path / "out")]) == 2
    assert "http(s) URL" in capsys.readouterr().err

    bad_method = write_config(tmp_path, [{"name": "bad", "type": "http_json", "method": "POST", "url": "http://127.0.0.1"}])
    assert capture.main(["--config", str(bad_method), "--output-dir", str(tmp_path / "out")]) == 2
    assert "only support GET" in capsys.readouterr().err


def test_atomic_write_leaves_no_final_snapshot_on_replace_failure(tmp_path, monkeypatch):
    capture = load_capture()
    config = write_config(tmp_path, [{"name": "cmd", "type": "command", "argv": [sys.executable, "-c", "print('ok')"]}])
    output_dir = tmp_path / "out"

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(capture.os, "replace", fail_replace)

    assert capture.main(["--config", str(config), "--output-dir", str(output_dir)]) == 1
    assert not list(output_dir.glob("snapshot-*.json"))
    assert not list(output_dir.glob("*.tmp"))


def test_invalid_timeout_max_output_and_duplicate_names_rejected_before_io(tmp_path, monkeypatch):
    capture = load_capture()
    calls = []
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))

    bad_timeout = tmp_path / "bad-timeout.json"
    bad_timeout.write_text(json.dumps({"timeout_seconds": 0, "sources": [{"name": "cmd", "type": "command", "argv": ["x"]}]}), encoding="utf-8")
    assert capture.main(["--config", str(bad_timeout), "--output-dir", str(tmp_path / "out")]) == 2

    bad_limit = tmp_path / "bad-limit.json"
    bad_limit.write_text(json.dumps({"max_output_bytes": 0, "sources": [{"name": "cmd", "type": "command", "argv": ["x"]}]}), encoding="utf-8")
    assert capture.main(["--config", str(bad_limit), "--output-dir", str(tmp_path / "out")]) == 2

    duplicate = write_config(tmp_path, [
        {"name": "same", "type": "command", "argv": ["x"]},
        {"name": "same", "type": "command", "argv": ["y"]},
    ])
    assert capture.main(["--config", str(duplicate), "--output-dir", str(tmp_path / "out")]) == 2
    assert calls == []
