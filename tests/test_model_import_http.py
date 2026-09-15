# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Shared-root discovery over the core HTTP surface and the copied CLI.

The old ``llm import``/``llm add``/``llm rm`` surface is gone; what remains is a
read-only discovery listing and the removed-write 405 contract.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_llm_events import api
from test_registry_http_preview import assert_readonly, mounted, registry_fixture, request


def describe(directory, document):
    (Path(directory) / "llmsvc.json").write_text(json.dumps(document))


def address(service):
    return "http://%s:%s" % service.address


def run_cli(script, service, *words, cwd):
    return subprocess.run([sys.executable, "-I", "-S", str(script), "--url", address(service), *words],
                          cwd=cwd, capture_output=True, text=True, timeout=10)


@pytest.fixture
def described(mounted):
    describe(mounted.weights, {"base": "base", "util": .4, "max_model_len": 4096,
                               "aliases": ["candidate-alias"], "weights_gb": 12.5})
    taken = mounted.weights.parent / "saved"
    shutil.copytree(mounted.weights, taken)
    describe(taken, {"base": "base"})
    plain = mounted.weights.parent / "plain"
    shutil.copytree(mounted.weights, plain)
    (plain / "llmsvc.json").unlink()
    broken = mounted.weights.parent / "broken"
    broken.mkdir()
    (broken / "llmsvc.json").write_text('{"base": "base", "is_default": true}')
    return mounted


def test_list_reports_discovery_without_touching_anything(described):
    before = described.files(), described.scheduler.events_since(0)
    status, listed = request(described.address, "GET", "/v1/models")
    assert status == 200
    rows = {row["name"]: row for row in listed["discovered"]}
    assert set(rows) == {"broken", "candidate", "saved"}
    assert rows["candidate"] == {"name": "candidate", "path": str(described.weights), "base": "base",
                                 "util": .4, "weights_gb": 12.5, "status": "pending", "reason": None}
    assert rows["saved"]["status"] == "configured" and rows["saved"]["reason"] is None
    assert rows["broken"]["status"] == "invalid" and "is_default" in rows["broken"]["reason"]
    assert listed["reconcile"] == {"enabled": False, "last": None}
    assert_readonly(described, before)


def test_list_uses_reconciler_annotate_when_mounted(described):
    class Reconciler:
        last = {"action": "add", "model": "candidate", "reason": None}

        def annotate(self, rows):
            return [dict(row, reason="annotated") for row in rows]

    described.scheduler.reconciler = Reconciler()
    status, listed = request(described.address, "GET", "/v1/models")
    assert status == 200
    assert listed["reconcile"] == {"enabled": True, "last": Reconciler.last}
    assert listed["discovered"] and all(row["reason"] == "annotated" for row in listed["discovered"])


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/models?dry_run=1"), ("POST", "/v1/models"),
    ("DELETE", "/v1/models/saved?dry_run=1"), ("DELETE", "/v1/models/saved"),
])
def test_registry_write_endpoints_are_removed(described, method, path):
    before = described.files(), described.scheduler.events_since(0)
    status, result = request(described.address, method, path, {"import": "candidate"} if method == "POST" else None)
    assert status == 405 and result == {"error": "registry_writes_removed"}
    assert_readonly(described, before)


def test_copied_cli_lists_shared_roots(api, described, tmp_path):
    script = tmp_path / "llm"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cli" / "llm", script)
    before = described.files(), described.scheduler.events_since(0)
    listing = run_cli(script, described, "models", cwd=tmp_path)
    assert listing.returncode == 0, listing.stderr
    assert "Shared roots (models are registered automatically when the service is idle)" in listing.stdout
    assert "pending  candidate" in listing.stdout and "configured  saved" in listing.stdout
    assert "invalid  broken" in listing.stdout
    assert "llm import" not in listing.stdout and "llm add" not in listing.stdout
    assert_readonly(described, before)


def test_cli_refuses_an_unshaped_discovery_list(api):
    parsed = api["build_parser"]().parse_args(["models", "--json"])
    good = {"records": {}, "writes_enabled": False, "blocked_by": [],
            "discovered": [{"name": "a", "path": "/srv/models/a", "base": "b", "util": None,
                            "weights_gb": None, "status": "pending", "reason": None}]}
    assert api["validate_registry_result"](parsed, good) == good
    for row in ({"name": "a"}, {**good["discovered"][0], "status": "maybe"},
                {**good["discovered"][0], "util": "0.4"}, {**good["discovered"][0], "extra": 1}):
        with pytest.raises(api["ClientError"], match="Invalid discovered model list"):
            api["validate_registry_result"](parsed, {**good, "discovered": [row]})
