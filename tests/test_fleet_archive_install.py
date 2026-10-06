# Generated-By: Codex / gpt-6.1-sol
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "deploy/fleet-archive"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("fleet_archive_admin_test", SOURCE / "fleet-archive-admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    script = tmp_path / "archive.py"
    script.write_text('# Generated-By: Codex / gpt-6.1-sol\n"""Synthetic standalone archive."""\n')
    monkeypatch.setattr(module, "SCRIPT", script)
    return module


class Systemd:
    """No live systemctl; all unit definitions are temporary fixtures."""
    def __init__(self, admin, root):
        self.admin, self.root, self.calls = admin, root, []
        self.fail = self.after = None
        self.units = {unit: {"exists": False, "enabled": False, "active": False} for unit in (admin.TIMER, admin.SERVICE)}

    def run(self, argv, **kwargs):
        assert argv[0] == "/usr/bin/systemctl" and kwargs["shell"] is False and kwargs["timeout"] == 10
        args = argv[1:]
        self.calls.append(args)
        if args == self.fail: return subprocess.CompletedProcess(argv, 1, b"", b"private-exception")
        raw = ""
        if args[0] == "show":
            unit = args[1]; state = self.units[unit]
            props = {"Id": unit, "LoadState": "loaded" if state["exists"] else "not-found",
                     "ActiveState": "active" if state["active"] else "inactive",
                     "UnitFileState": "enabled" if state["enabled"] else "disabled" if state["exists"] else "",
                     "FragmentPath": "/etc/systemd/system/" + unit if state["exists"] else "", "DropInPaths": ""}
            raw = "\n".join(key + "=" + value for key, value in props.items()) + "\n"
        elif args[0] == "daemon-reload":
            for unit, state in self.units.items():
                state["exists"] = (self.root / "etc/systemd/system" / unit).exists()
                if not state["exists"]: state.update(enabled=False, active=False)
        elif args[0] == "enable":
            assert args == ["enable", "--now", self.admin.TIMER]
            self.units[self.admin.TIMER].update(enabled=True, active=True)
        elif args[0] == "disable":
            assert args == ["disable", "--now", self.admin.TIMER]
            self.units[self.admin.TIMER].update(enabled=False, active=False)
        elif args[0] == "stop":
            assert args == ["stop", self.admin.SERVICE]
            self.units[self.admin.SERVICE]["active"] = False
        else: pytest.fail("unexpected systemctl action")
        if self.after: self.after(args)
        return subprocess.CompletedProcess(argv, 0, raw.encode(), b"")


def live_fixture(admin, tmp_path, monkeypatch):
    root = tmp_path / "container"
    original = admin.target
    monkeypatch.setattr(admin, "target", lambda ignored, logical: original(root, logical))
    systemd = Systemd(admin, root)
    monkeypatch.setattr(admin.subprocess, "run", systemd.run)
    return root, systemd


def test_dry_run_install_and_rollback_create_no_artifacts(admin, tmp_path, monkeypatch, capsys):
    root = tmp_path / "absent"
    monkeypatch.setattr(admin, "control", lambda *args: pytest.fail("staging called systemctl"))
    monkeypatch.setattr(admin.syslog, "syslog", lambda *args: pytest.fail("dry run logged to journal"))
    before = set(tmp_path.rglob("*"))
    assert admin.main(["install", "--root", str(root), "--dry-run"]) == 0
    assert admin.main(["rollback", "--root", str(root), "--dry-run"]) == 0
    assert set(tmp_path.rglob("*")) == before
    assert all(json.loads(line)["dry_run"] for line in capsys.readouterr().out.splitlines())


def test_isolated_install_provisions_private_output_and_preserves_log(admin, tmp_path, monkeypatch):
    root = tmp_path / "stage"
    monkeypatch.setattr(admin, "control", lambda *args: pytest.fail("staging called systemctl"))
    result = admin.administer("install", root)
    assert result["activate_timer"] is False
    directory = root / "var/lib/llmsvc/fleet-sessions"
    assert directory.stat().st_mode & 0o777 == 0o700
    log = directory / "synthetic-session.json.gz"
    log.write_bytes(b"keep gzip data"); log.chmod(0o600)
    receipt = root / admin.RECEIPT.lstrip("/")
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_bytes())["timer_before"] == {"enabled": False, "active": False}
    for logical, mode in admin.FILES.items(): assert (root / logical.lstrip("/")).stat().st_mode & 0o777 == mode
    admin.administer("rollback", root)
    assert log.read_bytes() == b"keep gzip data" and log.stat().st_mode & 0o777 == 0o600
    assert not receipt.exists() and all(not (root / name.lstrip("/")).exists() for name in admin.FILES)


def test_real_mode_enables_timer_once_and_rollback_stops_only_owned_units(admin, tmp_path, monkeypatch):
    root, systemd = live_fixture(admin, tmp_path, monkeypatch)
    result = admin.administer("install")
    assert result["activate_timer"] is True
    mutations = lambda: [args for args in systemd.calls if args[0] != "show"]
    assert mutations() == [["daemon-reload"], ["enable", "--now", admin.TIMER]]
    before = list(mutations())
    admin.administer("install")
    assert mutations() == before
    archive = root / "var/lib/llmsvc/fleet-sessions/synthetic.json.gz"
    archive.write_bytes(b"keep"); archive.chmod(0o600)
    systemd.units[admin.SERVICE]["active"] = True
    admin.administer("rollback")
    assert mutations()[2:] == [["disable", "--now", admin.TIMER], ["stop", admin.SERVICE], ["daemon-reload"]]
    assert archive.read_bytes() == b"keep"


def test_config_renders_only_logical_paths_and_readonly_source(admin, tmp_path, monkeypatch):
    root = tmp_path / "staged"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"database": "/opt/fleet-data/source.sqlite", "directory": "/var/lib/local-archive", "hourly_retention_days": 180}))
    admin.administer("install", root, config)
    service = (root / "etc/systemd/system" / admin.SERVICE).read_text()
    assert "ReadOnlyPaths=/opt/fleet-data" in service and "ReadWritePaths=/var/lib/local-archive" in service
    assert "ProtectSystem=strict" in service and "ProtectHome=true" in service and "PrivateTmp=true" in service
    assert "python3 -I -B /usr/local/libexec/llmsvc-fleet-archive.py --config /etc/llmsvc/fleet-archive.json" in service
    assert str(root) not in service and str(root) not in (root / "etc/llmsvc/fleet-archive.json").read_text()
    assert "OnUnitActiveSec=60s" in (root / "etc/systemd/system" / admin.TIMER).read_text()


@pytest.mark.parametrize("logical", ["/usr/local/libexec/llmsvc-fleet-archive.py", "/etc/llmsvc/fleet-archive.json", "/etc/systemd/system/llmsvc-fleet-archive.timer"])
def test_first_install_rejects_foreign_files_without_overwrite(admin, tmp_path, logical):
    root = tmp_path / "stage"
    foreign = root / logical.lstrip("/")
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign")
    with pytest.raises(ValueError, match="foreign_file"): admin.administer("install", root)
    assert foreign.read_bytes() == b"foreign" and not (root / admin.RECEIPT.lstrip("/")).exists()


def test_first_install_rejects_existing_unit_even_without_files(admin, tmp_path, monkeypatch):
    root, systemd = live_fixture(admin, tmp_path, monkeypatch)
    systemd.units[admin.TIMER]["exists"] = True
    with pytest.raises(ValueError, match="foreign_unit"): admin.administer("install")
    assert all(args[0] == "show" for args in systemd.calls) and not root.exists()


@pytest.mark.parametrize("change", ["bytes", "mode"])
def test_changed_owned_file_blocks_rollback_and_preserves_logs(admin, tmp_path, change):
    root = tmp_path / "stage"
    admin.administer("install", root)
    owned = root / "etc/llmsvc/fleet-archive.json"
    if change == "bytes": owned.write_bytes(b"foreign edit")
    else: owned.chmod(0o644)
    before = owned.read_bytes()
    with pytest.raises(ValueError, match="owned_file_changed"): admin.administer("rollback", root)
    assert owned.read_bytes() == before and (root / admin.RECEIPT.lstrip("/")).exists()


def test_partial_file_install_can_be_rolled_back_without_archive_source(admin, tmp_path, monkeypatch):
    root = tmp_path / "stage"
    original = admin.write
    def write(path, data, mode):
        if path.name == admin.TIMER: raise OSError("injected")
        original(path, data, mode)
    monkeypatch.setattr(admin, "write", write)
    with pytest.raises(OSError): admin.administer("install", root)
    monkeypatch.setattr(admin, "SCRIPT", tmp_path / "missing.py")
    admin.administer("rollback", root)
    assert all(not (root / name.lstrip("/")).exists() for name in admin.FILES)


def test_unknown_systemctl_result_does_not_replay_or_remove_files(admin, tmp_path, monkeypatch):
    root, systemd = live_fixture(admin, tmp_path, monkeypatch)
    systemd.fail = ["enable", "--now", admin.TIMER]
    with pytest.raises(ValueError, match="systemctl_unknown"): admin.administer("install")
    before = list(systemd.calls)
    with pytest.raises(ValueError, match="action_outcome_unknown"): admin.administer("install")
    with pytest.raises(ValueError, match="action_outcome_unknown"): admin.administer("rollback")
    assert systemd.calls == before and all((root / name.lstrip("/")).exists() for name in admin.FILES)


def test_nonzero_state_query_is_unknown_and_cannot_start_install(admin, tmp_path, monkeypatch):
    root, systemd = live_fixture(admin, tmp_path, monkeypatch)
    monkeypatch.setattr(admin.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", b""))
    with pytest.raises(ValueError, match="systemctl_unknown"): admin.administer("install")
    assert not root.exists()


def test_rollback_rechecks_files_after_stopping_owned_service(admin, tmp_path, monkeypatch):
    root, systemd = live_fixture(admin, tmp_path, monkeypatch)
    admin.administer("install")
    owned = root / "etc/llmsvc/fleet-archive.json"
    def after(args):
        if args == ["stop", admin.SERVICE]: owned.write_bytes(b"foreign edit during stop")
    systemd.after = after
    with pytest.raises(ValueError, match="owned_file_changed"): admin.administer("rollback")
    assert owned.read_bytes() == b"foreign edit during stop" and (root / admin.RECEIPT.lstrip("/")).exists()


def test_partial_rollback_resumes_known_missing_files_and_preserves_log(admin, tmp_path, monkeypatch):
    root = tmp_path / "stage"
    admin.administer("install", root)
    log = root / "var/lib/llmsvc/fleet-sessions/synthetic.json.gz"
    log.write_bytes(b"keep"); log.chmod(0o600)
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == root / "etc/llmsvc/fleet-archive.json": raise OSError("injected deletion failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError): admin.administer("rollback", root)
    assert not (root / "usr/local/libexec/llmsvc-fleet-archive.py").exists()
    assert json.loads((root / admin.RECEIPT.lstrip("/")).read_bytes())["phase"] == "rolling_back"
    monkeypatch.setattr(Path, "unlink", original)
    admin.administer("rollback", root)
    assert log.read_bytes() == b"keep" and not (root / admin.RECEIPT.lstrip("/")).exists()


def test_unknown_rollback_action_is_not_repeated_and_files_remain(admin, tmp_path, monkeypatch):
    root, systemd = live_fixture(admin, tmp_path, monkeypatch)
    admin.administer("install")
    systemd.fail = ["disable", "--now", admin.TIMER]
    with pytest.raises(ValueError, match="systemctl_unknown"): admin.administer("rollback")
    before = list(systemd.calls)
    with pytest.raises(ValueError, match="action_outcome_unknown"): admin.administer("rollback")
    assert systemd.calls == before and all((root / name.lstrip("/")).exists() for name in admin.FILES)


def test_symlink_in_install_path_is_rejected_without_writing_target(admin, tmp_path):
    root, outside = tmp_path / "stage", tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    (root / "etc").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink_path"): admin.administer("install", root)
    assert list(outside.iterdir()) == [] and not (root / admin.RECEIPT.lstrip("/")).exists()


@pytest.mark.parametrize("overrides", [{"directory": "/var/lib/llmsvc"}, {"directory": "/unsafe%path"}, {"directory": "/unsafe'path"}, {"hourly_retention_days": True}, {"hourly_retention_days": 0}, {"unknown": 1}])
def test_invalid_or_database_writable_config_has_no_artifacts(admin, tmp_path, overrides):
    cfg = json.loads((SOURCE / "fleet-archive.example.json").read_bytes())
    cfg.update(overrides)
    path = tmp_path / "config.json"; path.write_text(json.dumps(cfg))
    root = tmp_path / "stage"
    with pytest.raises(ValueError): admin.administer("install", root, path)
    assert not root.exists()


@pytest.mark.parametrize("logical", [
    "/usr/local/libexec/llmsvc-fleet-archive.py", "/etc/llmsvc/fleet-archive.json",
    "/etc/systemd/system/llmsvc-fleet-archive.service", "/etc/systemd/system/llmsvc-fleet-archive.timer",
    "/var/lib/llmsvc-fleet-archive-install/receipt.json", "/var/lib/llmsvc-fleet-archive-install/install.lock",
], ids=["script", "config", "service", "timer", "receipt", "lock"])
@pytest.mark.parametrize("relation", ["equal", "descendant", "ancestor"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "isolated-install"])
def test_archive_output_overlap_rejected_before_any_artifacts(admin, tmp_path, monkeypatch, logical, relation, dry_run):
    directory = Path(logical)
    if relation == "descendant": directory /= "session-data"
    if relation == "ancestor": directory = directory.parent
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"database": "/var/lib/llmsvc/fleet.sqlite", "directory": str(directory), "hourly_retention_days": 180}))
    root = tmp_path / "absent"
    monkeypatch.setattr(admin, "control", lambda *args: pytest.fail("overlap reached systemctl"))
    before = set(tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="^archive_install_path_overlap$"):
        admin.administer("install", root, config, dry_run)
    assert not root.exists() and set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("directory", ["//usr/local/libexec/llmsvc-fleet-archive.py", "//var/lib/llmsvc-fleet-archive-install"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "isolated-install"])
def test_double_root_cannot_bypass_archive_output_overlap(admin, tmp_path, directory, dry_run):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"database": "/var/lib/llmsvc/fleet.sqlite", "directory": directory, "hourly_retention_days": 180}))
    root = tmp_path / "absent"
    with pytest.raises(ValueError, match="^archive_install_path_overlap$"):
        admin.administer("install", root, config, dry_run)
    assert not root.exists()


def test_cli_reports_safe_archive_overlap_error(admin, tmp_path, capsys):
    config = tmp_path / "private-config.json"
    config.write_text(json.dumps({"database": "/var/lib/llmsvc/fleet.sqlite", "directory": "/var/lib/llmsvc-fleet-archive-install", "hourly_retention_days": 180}))
    root = tmp_path / "absent"
    assert admin.main(["install", "--root", str(root), "--config", str(config), "--dry-run"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "archive_install_path_overlap" and result["ok"] is False
    assert str(config) not in json.dumps(result) and not root.exists()


def test_cli_reports_safe_expected_error_without_private_paths(admin, tmp_path, capsys):
    config = tmp_path / "private-config.json"
    config.write_text(json.dumps({"database": "/synthetic-private/database.sqlite", "directory": "/synthetic-private", "hourly_retention_days": 180}))
    root = tmp_path / "absent"
    assert admin.main(["install", "--root", str(root), "--config", str(config), "--dry-run"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "database_in_writable_archive" and result["ok"] is False
    assert "synthetic-private" not in json.dumps(result) and not root.exists()


@pytest.mark.parametrize("failure", [ValueError("synthetic_private_token"), OSError("/synthetic-private/path")])
def test_cli_keeps_unexpected_errors_generic(admin, tmp_path, monkeypatch, capsys, failure):
    monkeypatch.setattr(admin, "administer", lambda *args: (_ for _ in ()).throw(failure))
    assert admin.main(["install", "--root", str(tmp_path / "stage"), "--dry-run"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "install_failed" and "synthetic" not in json.dumps(result)
