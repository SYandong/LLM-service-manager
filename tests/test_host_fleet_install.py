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


class SystemdModel:
    """Temporary fixture definitions and explicit systemctl show replies."""

    def __init__(self, admin, root):
        self.admin = admin
        self.root = root
        self.calls = []
        self.units = {}
        self.fail = None
        self.apply_failed_action = False
        self.refresh()

    def refresh(self):
        for unit in (self.admin.TIMER, self.admin.SERVICE):
            present = (self.root / "etc/systemd/system" / unit).exists()
            previous = self.units.get(unit, {})
            self.units[unit] = {"present": present, "enabled": previous.get("enabled", False) if present else False,
                                "active": previous.get("active", False) if present else False}

    def properties(self, unit):
        state = self.units[unit]
        props = {"Id": unit, "LoadState": "loaded" if state["present"] else "not-found",
                 "ActiveState": "active" if state["active"] else "inactive",
                 "UnitFileState": ("enabled" if state["enabled"] else "disabled") if state["present"] else "",
                 "FragmentPath": "/etc/systemd/system/" + unit if state["present"] else "",
                 "DropInPaths": "", "InvocationID": "a" * 32 if state["active"] else "", "Job": ""}
        if unit == self.admin.SERVICE:
            props.update(MainPID="42" if state["active"] else "0", ControlPID="0",
                         ControlGroup="/system.slice/" + unit if state["active"] else "")
        return props

    def run(self, command, **kwargs):
        args = command[1:]
        self.calls.append(args)
        fail = args == self.fail
        if fail and not self.apply_failed_action:
            return subprocess.CompletedProcess(command, 1, b"", b"")
        if args[0] == "show":
            props = self.properties(args[1])
            keys = next(arg for arg in args if arg.startswith("--property=")).split("=", 1)[1].split(",")
            raw = "\n".join(key + "=" + props[key] for key in reversed(keys)) + "\n"
        else:
            raw = ""
            if args[0] == "daemon-reload":
                self.refresh()
            elif args[0] == "enable":
                self.units[args[-1]]["enabled"] = True
                if "--now" in args:
                    self.units[args[-1]]["active"] = True
            elif args[0] == "disable":
                self.units[args[-1]]["enabled"] = False
                if "--now" in args:
                    self.units[args[-1]]["active"] = False
            elif args[0] == "stop":
                self.units[args[-1]]["active"] = False
            elif args[0] == "start":
                self.units[args[-1]]["active"] = True
            else:
                pytest.fail("unexpected fixture systemctl action")
        return subprocess.CompletedProcess(command, 1 if fail else 0, raw.encode(), b"")


def fake_live_paths(admin, root, monkeypatch):
    target = admin.target
    monkeypatch.setattr(admin, "target", lambda ignored, absolute: target(root, absolute))


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
    service = root / "etc/systemd/system" / admin.SERVICE
    service.parent.mkdir(parents=True)
    service.write_bytes((SOURCE / admin.SERVICE).read_bytes())
    manager = SystemdModel(admin, root)
    manager.units[admin.SERVICE]["active"] = True
    fake_live_paths(admin, root, monkeypatch)
    monkeypatch.setattr(admin.subprocess, "run", manager.run)
    admin.administer("install", Path("/"), SOURCE)
    assert ["enable", "--now", admin.TIMER] in manager.calls
    assert ["daemon-reload"] in manager.calls
    admin.administer("uninstall", Path("/"), SOURCE)
    assert ["disable", "--now", admin.TIMER] in manager.calls
    assert ["stop", admin.SERVICE] in manager.calls


def test_wrappers_dry_run_are_executable_and_do_not_touch_root(tmp_path):
    root = tmp_path / "absent"
    result = subprocess.run(["bash", str(SOURCE / "install-fleet-scan.sh"), "--root", str(root), "--dry-run"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["activate_timer"] is False
    assert not root.exists()
    for name in ("install-fleet-scan.sh", "uninstall-fleet-scan.sh"):
        assert "set -euo pipefail" in (SOURCE / name).read_text()


def test_partial_restore_resumes_registered_before_after_bytes(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    previous = root / "etc/llmsvc/fleet-scan.json"
    previous.parent.mkdir(parents=True)
    old_data = json.dumps({"sample_interval_seconds": 75}).encode()
    previous.write_bytes(old_data)
    previous.chmod(0o600)
    old_service = root / "etc/systemd/system/llmsvc-fleet-scan.service"
    old_service.parent.mkdir(parents=True)
    old_service.write_bytes(b"previous service bytes\n")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"sample_interval_seconds": 90}))
    admin.administer("install", root, SOURCE, candidate)
    write = admin.write_atomic
    failed = [False]

    def fail_once(path, data, mode):
        if path == old_service and not failed[0]:
            failed[0] = True
            raise OSError("injected partial restoration")
        write(path, data, mode)

    monkeypatch.setattr(admin, "write_atomic", fail_once)
    with pytest.raises(OSError):
        admin.administer("rollback", root, SOURCE)
    assert previous.read_bytes() == old_data
    assert (root / admin.RECEIPT.lstrip("/")).exists()
    assert admin.administer("rollback", root, SOURCE)["action"] == "rollback"
    assert previous.read_bytes() == old_data
    assert previous.stat().st_mode & 0o777 == 0o600
    assert old_service.read_bytes() == b"previous service bytes\n"


def test_generic_systemctl_failure_does_not_authorize_file_restore(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    admin.administer("install", root, SOURCE)
    installed = {absolute: (root / absolute.lstrip("/")).read_bytes() for absolute in admin.FILES}
    target = admin.target
    monkeypatch.setattr(admin, "target", lambda ignored, absolute: target(root, absolute))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b"", b"")

    monkeypatch.setattr(admin.subprocess, "run", run)
    with pytest.raises(ValueError):
        admin.administer("rollback", Path("/"), SOURCE)
    assert all((root / absolute.lstrip("/")).read_bytes() == data for absolute, data in installed.items())
    assert all(command[1] == "show" for command in calls)


def test_generic_systemctl_failure_blocks_install_before_receipt(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    target = admin.target
    monkeypatch.setattr(admin, "target", lambda ignored, absolute: target(root, absolute))
    monkeypatch.setattr(admin.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b"", b""))
    with pytest.raises(ValueError):
        admin.administer("install", Path("/"), SOURCE)
    assert not (root / admin.RECEIPT.lstrip("/")).exists()
    assert not (root / "usr/local/lib/llmsvc/llmsvc-fleet-scan.py").exists()


def test_partial_restore_recognizes_replaced_file_without_repeating_write(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    config = root / "etc/llmsvc/fleet-scan.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"sample_interval_seconds": 75}))
    original_bytes = config.read_bytes()
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"sample_interval_seconds": 90}))
    admin.administer("install", root, SOURCE, candidate)
    original = admin.write_atomic
    writes = []

    def committed_then_failed(path, data, mode):
        original(path, data, mode)
        if path == config:
            writes.append(path)
            if len(writes) == 1:
                raise OSError("injected after rename")

    monkeypatch.setattr(admin, "write_atomic", committed_then_failed)
    with pytest.raises(OSError):
        admin.administer("rollback", root, SOURCE)
    receipt = json.loads((root / admin.RECEIPT.lstrip("/")).read_bytes())
    assert receipt["schema_version"] == 2 and receipt["phase"] == "restoring"
    assert receipt["files"]["/etc/llmsvc/fleet-scan.json"] == "restore_submitted"
    assert config.read_bytes() == original_bytes
    admin.administer("rollback", root, SOURCE)
    assert writes == [config]


def test_partial_restore_unknown_file_edit_is_rejected_before_other_writes(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    admin.administer("install", root, SOURCE)
    original = admin.remove_durable
    config = root / "etc/llmsvc/fleet-scan.json"
    service = root / "etc/systemd/system" / admin.SERVICE

    def fail_service(path):
        if path == service:
            raise OSError("injected before service removal")
        original(path)

    monkeypatch.setattr(admin, "remove_durable", fail_service)
    with pytest.raises(OSError):
        admin.administer("rollback", root, SOURCE)
    config.write_bytes(b"unknown operator modification")
    service_bytes = service.read_bytes()
    with pytest.raises(ValueError, match="installed_file_changed"):
        admin.administer("rollback", root, SOURCE)
    assert config.read_bytes() == b"unknown operator modification"
    assert service.read_bytes() == service_bytes


@pytest.mark.parametrize("returncode,change", [
    (1, {}),
    (0, {"ActiveState": "unrecognized"}),
    (0, {"LoadState": "error"}),
    (0, {"Id": "other.service"}),
    (0, {"FragmentPath": "/etc/systemd/system/other.service"}),
    (0, {"DropInPaths": "/etc/systemd/system/override.conf"}),
    (0, {"Job": "123"}),
])
def test_systemctl_status_requires_known_result_and_bound_unit(tmp_path, monkeypatch, returncode, change):
    admin = load_admin()
    root = tmp_path / "stage"
    service = root / "etc/systemd/system" / admin.SERVICE
    service.parent.mkdir(parents=True)
    service.write_bytes((SOURCE / admin.SERVICE).read_bytes())
    manager = SystemdModel(admin, root)
    props = manager.properties(admin.SERVICE)
    props.update(change)
    raw = "\n".join(key + "=" + value for key, value in props.items()).encode()
    monkeypatch.setattr(admin.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, raw, b""))
    with pytest.raises(ValueError):
        admin.unit_state(admin.SERVICE)


def test_positive_inactive_and_absent_queries_are_distinct_from_errors(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    manager = SystemdModel(admin, root)
    monkeypatch.setattr(admin.subprocess, "run", manager.run)
    assert admin.unit_state(admin.SERVICE)["LoadState"] == "not-found"
    service = root / "etc/systemd/system" / admin.SERVICE
    service.parent.mkdir(parents=True)
    service.write_bytes((SOURCE / admin.SERVICE).read_bytes())
    manager.refresh()
    state = admin.unit_state(admin.SERVICE)
    assert state["LoadState"] == "loaded" and state["ActiveState"] == "inactive"
    assert state["MainPID"] == "0" and state["ControlGroup"] == ""


@pytest.mark.parametrize("apply_action", [False, True])
def test_unknown_stop_outcome_retains_receipt_and_is_not_resubmitted(tmp_path, monkeypatch, apply_action):
    admin = load_admin()
    root = tmp_path / "stage"
    admin.administer("install", root, SOURCE)
    installed = {absolute: (root / absolute.lstrip("/")).read_bytes() for absolute in admin.FILES}
    manager = SystemdModel(admin, root)
    manager.units[admin.TIMER].update(active=True, enabled=True)
    manager.fail = ["disable", "--now", admin.TIMER]
    manager.apply_failed_action = apply_action
    fake_live_paths(admin, root, monkeypatch)
    monkeypatch.setattr(admin.subprocess, "run", manager.run)
    with pytest.raises(ValueError, match="systemctl_result_unknown"):
        admin.administer("rollback", Path("/"), SOURCE)
    receipt_path = root / admin.RECEIPT.lstrip("/")
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["effects"]["restore_stop_timer"]["state"] == "submitted"
    assert receipt["effects"]["restore_stop_timer"]["binding"]["Id"] == admin.TIMER
    assert all((root / absolute.lstrip("/")).read_bytes() == data for absolute, data in installed.items())
    calls = list(manager.calls)
    with pytest.raises(ValueError, match="systemctl_action_outcome_unknown"):
        admin.administer("rollback", Path("/"), SOURCE)
    assert manager.calls == calls
    assert manager.calls.count(["disable", "--now", admin.TIMER]) == 1


def test_unknown_install_enable_outcome_preserves_registered_files(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    manager = SystemdModel(admin, root)
    manager.fail = ["enable", "--now", admin.TIMER]
    fake_live_paths(admin, root, monkeypatch)
    monkeypatch.setattr(admin.subprocess, "run", manager.run)
    with pytest.raises(ValueError, match="systemctl_action_outcome_unknown"):
        admin.administer("install", Path("/"), SOURCE)
    receipt = json.loads((root / admin.RECEIPT.lstrip("/")).read_bytes())
    assert receipt["effects"]["install_enable_timer"]["state"] == "submitted"
    assert all(state == "installed" for state in receipt["files"].values())
    installed = {absolute: (root / absolute.lstrip("/")).read_bytes() for absolute in admin.FILES}
    calls = list(manager.calls)
    with pytest.raises(ValueError, match="systemctl_action_outcome_unknown"):
        admin.administer("rollback", Path("/"), SOURCE)
    assert manager.calls == calls
    assert all((root / absolute.lstrip("/")).read_bytes() == data for absolute, data in installed.items())


def test_failed_action_ack_checkpoint_uses_durable_receipt_for_recovery(tmp_path, monkeypatch):
    admin = load_admin()
    root = tmp_path / "stage"
    service = root / "etc/systemd/system" / admin.SERVICE
    service.parent.mkdir(parents=True)
    service.write_bytes((SOURCE / admin.SERVICE).read_bytes())
    manager = SystemdModel(admin, root)
    manager.units[admin.SERVICE]["active"] = True
    fake_live_paths(admin, root, monkeypatch)
    monkeypatch.setattr(admin.subprocess, "run", manager.run)
    save = admin.save_receipt

    def fail_ack(path, receipt):
        mark = receipt["effects"].get("install_stop_service", {})
        if mark.get("state") == "acknowledged":
            raise OSError("injected lost ACK checkpoint")
        save(path, receipt)

    monkeypatch.setattr(admin, "save_receipt", fail_ack)
    with pytest.raises(ValueError, match="systemctl_action_outcome_unknown"):
        admin.administer("install", Path("/"), SOURCE)
    receipt = json.loads((root / admin.RECEIPT.lstrip("/")).read_bytes())
    assert receipt["effects"]["install_stop_service"]["state"] == "submitted"
    assert [args for args in manager.calls if args[0] != "show"] == [["stop", admin.SERVICE]]
    calls = list(manager.calls)
    with pytest.raises(ValueError, match="systemctl_action_outcome_unknown"):
        admin.administer("rollback", Path("/"), SOURCE)
    assert manager.calls == calls
