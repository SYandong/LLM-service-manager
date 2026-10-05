# Generated-By: Codex / gpt-6.1-sol
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "deploy/host"


def load_admin():
    spec = importlib.util.spec_from_file_location("fleet_scan_admin_test", SOURCE / "fleet-scan-admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_isolated_install_uninstall_restores_previous_files_and_uses_period(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    old = root / "etc/llmsvc/fleet-scan.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"sample_interval_seconds": 75}))
    old.chmod(0o600)
    monkeypatch.setattr(admin, "systemctl", lambda *args, **kwargs: pytest.fail("isolated install called systemctl"))
    result = admin.administer("install", root, SOURCE)
    assert result["activate_timer"] is False
    timer = root / "etc/systemd/system/llmsvc-fleet-scan.timer"
    assert "OnUnitActiveSec=75s" in timer.read_text()
    assert "AccuracySec=5s" in timer.read_text()
    script = root / "usr/local/lib/llmsvc/llmsvc-fleet-scan.py"
    assert script.stat().st_mode & 0o777 == 0o755
    assert (root / "var/lib/llmsvc-host-export").is_dir()
    export = root / "var/lib/llmsvc-host-export/fleet.json"
    export.write_text("keep-export")
    ip_map = export.with_name("ip-containers.json")
    ip_map.write_text("keep-ip-map")
    admin.administer("uninstall", root, SOURCE)
    assert not script.exists() and not timer.exists()
    assert old.read_text() == json.dumps({"sample_interval_seconds": 75})
    assert old.stat().st_mode & 0o777 == 0o600
    assert export.read_text() == "keep-export" and ip_map.read_text() == "keep-ip-map"


def test_dry_run_writes_nothing_and_rejects_symlink_escape(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "absent"
    monkeypatch.setattr(admin, "systemctl", lambda *args, **kwargs: pytest.fail("dry run called systemctl"))
    result = admin.administer("install", root, SOURCE, dry_run=True)
    assert result["event"] == "fleet_install_dry_run"
    assert not root.exists()
    root.mkdir()
    (root / "etc").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink_install_path"):
        admin.administer("install", root, SOURCE)


def test_rollback_and_changed_file_guard(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    monkeypatch.setattr(admin, "systemctl", lambda *args, **kwargs: pytest.fail("isolated install called systemctl"))
    admin.administer("install", root, SOURCE)
    config = root / "etc/llmsvc/fleet-scan.json"
    original = config.read_bytes()
    config.write_text("operator changed config")
    with pytest.raises(ValueError, match="installed_file_changed"):
        admin.administer("rollback", root, SOURCE)
    config.write_bytes(original)
    result = admin.administer("rollback", root, SOURCE)
    assert result["action"] == "rollback"
    assert not config.exists()
    assert not (root / admin.RECEIPT.lstrip("/")).exists()


def test_partial_install_failure_rolls_back_bytes(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    old = root / "usr/local/lib/llmsvc/llmsvc-fleet-scan.py"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"previous-scanner")
    original = admin.write_atomic
    failed = [False]

    def write(path, data, mode):
        if path.name.endswith(".timer") and not failed[0]:
            failed[0] = True
            raise OSError("injected-install-failure")
        original(path, data, mode)

    monkeypatch.setattr(admin, "write_atomic", write)
    with pytest.raises(OSError):
        admin.administer("install", root, SOURCE)
    assert old.read_bytes() == b"previous-scanner"
    assert not (root / "etc/llmsvc/fleet-scan.json").exists()
    assert not (root / admin.RECEIPT.lstrip("/")).exists()


def test_live_install_requests_enable_now_and_failure_restores_timer(tmp_path, monkeypatch):
    # Redirect every path to a fixture root; only inspect simulated systemctl.
    admin = load_admin()
    root = tmp_path / "virtual-host"
    calls = []
    original_target = admin.target
    monkeypatch.setattr(admin, "target", lambda ignored, absolute: original_target(root, absolute))
    monkeypatch.setattr(admin, "systemctl", lambda args, check=True: calls.append(args) or False if not check else calls.append(args) or True)
    admin.administer("install", Path("/"), SOURCE)
    assert ["enable", "--now", admin.TIMER] in calls
    assert ["daemon-reload"] in calls
    admin.administer("uninstall", Path("/"), SOURCE)
    assert ["disable", "--now", admin.TIMER] in calls
    assert ["stop", admin.SERVICE] in calls


def test_wrappers_dry_run_are_executable_and_do_not_touch_root(tmp_path):
    root = tmp_path / "absent"
    result = subprocess.run(["bash", str(SOURCE / "install-fleet-scan.sh"), "--root", str(root), "--dry-run"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["activate_timer"] is False
    assert not root.exists()
    for name in ("install-fleet-scan.sh", "uninstall-fleet-scan.sh"):
        assert "set -euo pipefail" in (SOURCE / name).read_text()
