# Generated-By: Claude Code / claude-fable-5-1
"""Discovery over the real core HTTP surface and the copied standalone CLI."""

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
                                 "util": .4, "weights_gb": 12.5, "status": "importable", "reason": None}
    assert rows["saved"]["status"] == "imported" and rows["saved"]["reason"] is None
    assert rows["broken"]["status"] == "invalid" and "is_default" in rows["broken"]["reason"]
    assert_readonly(described, before)


def test_import_preview_uses_the_descriptor_and_stays_a_preview(described):
    before = described.files(), described.scheduler.events_since(0)
    status, result = request(described.address, "POST", "/v1/models?dry_run=1", {"import": "candidate"})
    assert status == 200, result
    assert result["would"] == [{"kind": "add_model", "model": "candidate", "base": "base"}]
    assert result["dry_run"] is True and result["config_committed"] is False and "id" not in result
    assert result["plan"]["model"]["name"] == "candidate" and result["plan"]["config_written"] is False
    assert any(item["reason"] == "inflight_stream_unknown" for item in result["blocked_by"])
    assert_readonly(described, before)


@pytest.mark.parametrize("body,message", [
    ({"import": "missing"}, "no discovered model named missing"),
    ({"import": "saved"}, "already configured"),
    ({"import": "broken"}, "is_default"),
    ({"import": ""}, "import requires the discovered model name"),
    ({"import": 7}, "import requires the discovered model name"),
    ({"import": "candidate", "util": .9}, "add requires name, path and base"),
])
def test_import_rejections_explain_themselves(described, body, message):
    before = described.files(), described.scheduler.events_since(0)
    status, result = request(described.address, "POST", "/v1/models?dry_run=1", body)
    assert status == 400 and result["error"] == "registry_invalid_request"
    assert message in result["message"]
    assert_readonly(described, before)


def test_import_is_unavailable_without_configured_discovery(described):
    described.registry.discover = None
    status, result = request(described.address, "POST", "/v1/models?dry_run=1", {"import": "candidate"})
    assert status == 400 and "discovery is not configured" in result["message"]
    status, listed = request(described.address, "GET", "/v1/models")
    assert status == 200 and listed["discovered"] == []


def test_copied_cli_lists_candidates_and_previews_imports(api, described, tmp_path):
    script = tmp_path / "llm"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cli" / "llm", script)
    before = described.files(), described.scheduler.events_since(0)
    listing = run_cli(script, described, "models", cwd=tmp_path)
    assert listing.returncode == 0, listing.stderr
    assert "candidate [importable]" in listing.stdout and "saved [imported]" in listing.stdout
    assert "llm import NAME" in listing.stdout

    preview = run_cli(script, described, "import", "candidate", "--dry-run", "--json", cwd=tmp_path)
    assert preview.returncode == 1, preview.stderr  # Blocked previews keep the nonzero convention.
    assert json.loads(preview.stdout)["would"] == [{"kind": "add_model", "model": "candidate", "base": "base"}]

    missing = run_cli(script, described, "import", "absent", "--dry-run", cwd=tmp_path)
    assert missing.returncode == 1 and "no discovered model named absent" in missing.stderr

    everything = run_cli(script, described, "import", "--all", "--dry-run", "--json", cwd=tmp_path)
    assert everything.returncode == 1, everything.stderr
    imports = json.loads(everything.stdout)["imports"]
    assert [item["model"] for item in imports] == ["candidate"]
    assert imports[0]["result"]["dry_run"] is True

    conflict = run_cli(script, described, "import", "candidate", "--all", cwd=tmp_path)
    assert conflict.returncode == 1 and "one discovered model name, or --all" in conflict.stderr
    assert_readonly(described, before)


def test_cli_refuses_an_unshaped_discovery_list(api):
    parsed = api["build_parser"]().parse_args(["models", "--json"])
    good = {"records": {}, "writes_enabled": False, "blocked_by": [],
            "discovered": [{"name": "a", "path": "/srv/models/a", "base": "b", "util": None,
                            "weights_gb": None, "status": "importable", "reason": None}]}
    assert api["validate_registry_result"](parsed, good) == good
    for row in ({"name": "a"}, {**good["discovered"][0], "status": "maybe"},
                {**good["discovered"][0], "util": "0.4"}, {**good["discovered"][0], "extra": 1}):
        with pytest.raises(api["ClientError"], match="Invalid discovered model list"):
            api["validate_registry_result"](parsed, {**good, "discovered": [row]})
    with pytest.raises(api["ClientError"], match="does not report discovered models"):
        api["importable_names"]({"records": {}, "writes_enabled": False, "blocked_by": []})
