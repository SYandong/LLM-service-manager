# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location("deploy_manage", Path(__file__).parents[1] / "deploy/manage.py")
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / "stage"
    root.mkdir()
    source = tmp_path / "src"
    (source / "llmsvc").mkdir(parents=True)
    (source / "cli").mkdir()
    (source / "llmsvc/__init__.py").write_text("")
    (source / "cli/llm").write_text("#!/usr/bin/env python3\nprint('status')\n")
    (source / "pyproject.toml").write_text("[project]\nname='llmsvc'\nversion='0.1.0'\n")
    config = tmp_path / "scheduler.yaml"
    config.write_text("read_only: true\nbind_port: 8011\n")
    settings = json.loads((Path(__file__).parents[1] / "deploy/deployment.example.json").read_text())
    settings["python"] = sys.executable
    for name in settings["backup_files"]:
        path = root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("globalTTL: 600\nmodels:\n  example: {ttl: 123}\n")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(settings))
    calls = []
    monkeypatch.setattr(manage.subprocess, "run", lambda argv, **kw: calls.append(argv))
    args = ["--settings", str(settings_path), "--root", str(root), "--config", str(config), "--source", str(source)]
    return root, settings, args, calls


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_install_rollback_uninstall_restores_original_tree(deployment):
    root, settings, args, calls = deployment
    before = files(root)
    assert manage.main(["install", *args]) == 0
    unit = root / settings["unit_path"].lstrip("/")
    assert "--dry-run" in unit.read_text()
    assert "StandardOutput=journal" in unit.read_text()
    assert len(calls) == 3  # venv, pip, config validation; no systemctl or data-plane call.
    assert str(root) in calls[1][-1]
    original = root / settings["backup_files"][0].lstrip("/")
    original.write_text("globalTTL: 0\nmodels: {}\n")
    assert manage.main(["rollback", *args]) == 0
    assert original.read_bytes() == before[settings["backup_files"][0].lstrip("/")]
    assert manage.main(["uninstall", *args]) == 0
    assert files(root) == before
    assert not (root / "opt").exists()
    assert not (root / "srv").exists()
    assert not (root / "var").exists()


def test_dry_run_has_zero_filesystem_and_subprocess_changes(deployment):
    root, settings, args, calls = deployment
    before = files(root)
    assert manage.main(["install", *args, "--dry-run"]) == 0
    assert files(root) == before and not calls
    assert manage.main(["install", *args]) == 0
    before = files(root)
    calls.clear()
    for action in ("uninstall", "rollback"):
        assert manage.main([action, *args, "--dry-run"]) == 0
        assert files(root) == before and not calls


def test_failed_install_unwinds_only_created_files(deployment, monkeypatch):
    root, settings, args, calls = deployment
    before = files(root)
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(manage.subprocess, "run", fail)
    assert manage.main(["install", *args]) == 1
    assert files(root) == before
    assert not (root / "opt").exists()
    assert not (root / "var").exists()


def test_uninstall_preserves_modified_files_and_state(deployment):
    root, settings, args, calls = deployment
    assert manage.main(["install", *args]) == 0
    config = root / settings["config_path"].lstrip("/")
    original = config.read_bytes()
    config.write_text("operator changed\n")
    before = files(root)
    assert manage.main(["uninstall", *args]) == 1
    assert files(root) == before
    config.write_bytes(original)
    state = root / settings["state_dir"].lstrip("/") / "intents.sqlite"
    state.write_text("persistent pin data")
    before = files(root)
    assert manage.main(["uninstall", *args]) == 1
    assert files(root) == before


def test_refuse_symlink_and_path_escape(deployment, tmp_path):
    root, settings, args, calls = deployment
    (root / "opt").symlink_to(tmp_path, target_is_directory=True)
    assert manage.main(["install", *args]) == 1
    assert not calls
    with pytest.raises(manage.DeploymentError):
        manage.inside(root, "/../outside")


def test_missing_backup_prevents_install_before_any_mutation(deployment):
    root, settings, args, calls = deployment
    (root / settings["backup_files"][0].lstrip("/")).unlink()
    before = files(root)
    assert manage.main(["install", *args]) == 1
    assert files(root) == before and not calls


def test_tampered_backup_and_manifest_refuse_mutation(deployment):
    root, settings, args, calls = deployment
    assert manage.main(["install", *args]) == 0
    prefix = root / settings["prefix"].lstrip("/")
    (prefix / "backup/0").write_text("corrupt")
    before = files(root)
    assert manage.main(["rollback", *args]) == 1
    assert files(root) == before
    path = prefix / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = "/usr/local/sbin/vllm-launch"
    path.write_text(json.dumps(manifest))
    before = files(root)
    assert manage.main(["uninstall", *args]) == 1
    assert files(root) == before


def test_existing_destination_is_never_adopted(deployment):
    root, settings, args, calls = deployment
    prefix = root / settings["prefix"].lstrip("/")
    prefix.mkdir(parents=True)
    marker = prefix / "foreign"
    marker.write_text("keep")
    before = files(root)
    assert manage.main(["install", *args]) == 1
    assert files(root) == before and not calls
