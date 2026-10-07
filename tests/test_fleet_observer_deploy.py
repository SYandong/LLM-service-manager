# Generated-By: Codex / gpt-6.1-sol
"""Filesystem and fake-systemd qualification; no live service operations."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "deploy/fleet-observer"


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("fleet_observer_admin_test", SOURCE / "fleet-observer-admin.py")
    admin = importlib.util.module_from_spec(spec)
    exec(compile((SOURCE / "fleet-observer-admin.py").read_bytes(), str(SOURCE / "fleet-observer-admin.py"), "exec"), admin.__dict__)
    root = tmp_path / "host"
    settings = json.loads((SOURCE / "fleet-observer-install.example.json").read_text())
    settings["source_dir"] = "/opt/fixture-observer/source"
    code = root / settings["source_dir"].lstrip("/")
    code.mkdir(parents=True)
    shutil.copytree(REPO / "llmsvc", code / "llmsvc", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    passwd = root / "etc/passwd"
    passwd.parent.mkdir(parents=True)
    passwd.write_text("fleet-observer:x:990:990:Fixture observer:/nonexistent:/usr/sbin/nologin\n")
    settings_path = tmp_path / "install.json"
    settings_path.write_text(json.dumps(settings))
    settings_path.chmod(0o600)
    config = json.loads((SOURCE / "fleet-observer.example.json").read_text())
    config_path = tmp_path / "observer.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    monkeypatch.setattr(admin.syslog, "syslog", lambda *args: pytest.fail("fixture touched journal"))
    return admin, root, settings, settings_path, config, config_path


def receipt_path(root, settings):
    return root / settings["receipt_dir"].lstrip("/") / "receipt.json"


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in root.rglob("*") if path.is_file()}


class Systemd:
    def __init__(self, admin, root, settings):
        self.admin, self.root, self.settings = admin, root, settings
        self.calls = []
        self.fail = self.after = None
        self.units = {unit: {"exists": False, "enabled": False, "active": False, "dropins": "", "fragment": None}
                      for unit in admin.UNITS}

    def show(self, unit):
        state = self.units[unit]
        values = {"Id": unit, "LoadState": "loaded" if state["exists"] else "not-found",
                  "ActiveState": "active" if state["active"] else "inactive",
                  "UnitFileState": "enabled" if state["enabled"] else "disabled" if state["exists"] else "",
                  "FragmentPath": (state["fragment"] or str(Path(self.settings["unit_dir"]) / unit)) if state["exists"] else "",
                  "DropInPaths": state["dropins"], "InvocationID": "1" * 32 if state["active"] else ""}
        if unit != self.admin.TIMER:
            values.update(MainPID="2345" if state["active"] else "0", ControlPID="0",
                          ControlGroup="/system.slice/" + unit if state["active"] else "")
        return "\n".join(key + "=" + value for key, value in values.items())

    def control(self, args):
        self.calls.append(args)
        if args == self.fail:
            raise ValueError("systemctl_unknown")
        if args[0] == "show":
            result = self.show(args[1])
        elif args[0] == "daemon-reload":
            for unit, state in self.units.items():
                state["exists"] = (self.root / self.settings["unit_dir"].lstrip("/") / unit).exists()
            result = ""
        elif args[0] == "enable":
            assert args in (["enable", "--now", self.admin.SERVICE], ["enable", "--now", self.admin.TIMER])
            self.units[args[-1]].update(active=True, enabled=True)
            result = ""
        elif args[0] == "disable":
            assert args in (["disable", "--now", self.admin.TIMER], ["disable", "--now", self.admin.SERVICE])
            self.units[args[-1]].update(active=False, enabled=False)
            result = ""
        elif args[0] == "stop":
            assert args == ["stop", self.admin.ARCHIVE]
            self.units[self.admin.ARCHIVE]["active"] = False
            result = ""
        else:
            pytest.fail("unexpected systemd effect")
        if self.after:
            self.after(args)
        return result

    def effects(self):
        return [args for args in self.calls if args[0] != "show"]


def live_fixture(deployment, monkeypatch):
    admin, root, settings, settings_path, config, config_path = deployment
    settings["uid"] = os.geteuid()
    settings_path.write_text(json.dumps(settings))
    monkeypatch.setattr(admin, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(admin.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid(), pw_shell="/usr/sbin/nologin"))
    original_target, original_source = admin.target, admin.source_binding
    monkeypatch.setattr(admin, "target", lambda ignored, logical: original_target(root, logical))
    monkeypatch.setattr(admin, "source_binding", lambda ignored, values: original_source(root, values))
    systemd = Systemd(admin, root, settings)
    monkeypatch.setattr(admin, "control", systemd.control)
    return (*deployment, systemd)


def test_dry_run_creates_nothing_and_check_config_never_starts_observer(deployment, monkeypatch, capsys):
    admin, root, _, settings_path, _, config_path = deployment
    before = files(root)
    monkeypatch.setattr(admin, "control", lambda args: pytest.fail("filesystem preview touched systemd"))
    assert admin.main(["install", "--settings", str(settings_path), "--config", str(config_path), "--root", str(root), "--dry-run"]) == 0
    assert admin.main(["rollback", "--settings", str(settings_path), "--root", str(root), "--dry-run"]) == 0
    assert files(root) == before and not list(root.rglob("__pycache__"))
    assert all(json.loads(line)["dry_run"] for line in capsys.readouterr().out.splitlines())


def test_install_and_rollback_preserve_database_archives_and_receipts(deployment):
    admin, root, settings, settings_path, config, config_path = deployment
    state = root / settings["state_dir"].lstrip("/")
    archive = root / settings["archive_dir"].lstrip("/")
    state.mkdir(parents=True, mode=0o700)
    archive.mkdir(parents=True, mode=0o700)
    database = root / config["fleet_db_path"].lstrip("/")
    database.write_bytes(b"fixture database including new observations")
    database.chmod(0o600)
    log = archive / "session.json.gz"
    log.write_bytes(b"private gzip observations")
    log.chmod(0o600)
    result = admin.administer("install", settings_path, config_path, root)
    assert result["activate"] is False
    receipt = receipt_path(root, settings)
    installed = json.loads(receipt.read_bytes())
    assert installed["phase"] == "installed" and receipt.stat().st_mode & 0o777 == 0o600
    for logical, expected in installed["files"].items():
        assert admin.fingerprint(root / logical.lstrip("/")) == expected
    assert installed["source"]["llmsvc/fleet/archive.py"]["sha256"] == hashlib.sha256((REPO / "llmsvc/fleet/archive.py").read_bytes()).hexdigest()
    admin.administer("rollback", settings_path, root=root)
    assert database.read_bytes() == b"fixture database including new observations"
    assert log.read_bytes() == b"private gzip observations" and log.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == archive.stat().st_mode & 0o777 == 0o700
    assert json.loads(receipt.read_bytes())["phase"] == "rolled_back"
    assert all(not (root / name.lstrip("/")).exists() for name in installed["files"])
    before = files(root)
    admin.administer("rollback", settings_path, root=root)
    assert files(root) == before
    admin.administer("install", settings_path, config_path, root)
    histories = list(receipt.parent.glob("[0-9a-f]" * 64 + ".json"))
    assert len(histories) == 1 and json.loads(histories[0].read_bytes())["phase"] == "rolled_back"
    assert database.read_bytes() == b"fixture database including new observations" and log.exists()


def test_rendered_units_use_nonlogin_account_and_disjoint_write_permissions(deployment):
    admin, root, settings, settings_path, config, config_path = deployment
    admin.administer("install", settings_path, config_path, root)
    units = root / settings["unit_dir"].lstrip("/")
    observer = (units / admin.SERVICE).read_text()
    archive = (units / admin.ARCHIVE).read_text()
    timer = (units / admin.TIMER).read_text()
    assert "User=fleet-observer\nGroup=990" in observer and "User=fleet-observer\nGroup=990" in archive
    assert "-E -s -B -m llmsvc.fleet.observer --config " + settings["config_path"] in observer
    assert "-I -B " + settings["source_dir"] + "/llmsvc/fleet/archive.py" in archive
    assert "ReadWritePaths=" + settings["state_dir"] in observer
    assert "ReadWritePaths=" + settings["archive_dir"] in archive
    assert "ReadOnlyPaths=" + settings["source_dir"] + " " + settings["state_dir"] in archive
    assert "ReadOnlyPaths=" + settings["source_dir"] + " /var/lib/llmsvc-host/export" in observer
    assert all("ProtectSystem=strict" in unit and "UMask=0077" in unit for unit in (observer, archive))
    assert "OnUnitActiveSec=60s" in timer and "WantedBy=timers.target" in timer
    assert str(root) not in observer + archive + timer
    archive_config = json.loads((root / settings["archive_config_path"].lstrip("/")).read_bytes())
    assert archive_config == {"database": config["fleet_db_path"], "directory": settings["archive_dir"], "hourly_retention_days": 180}


def test_real_mode_enables_both_units_and_stops_only_owned_observation_units(deployment, monkeypatch):
    admin, root, settings, settings_path, _, config_path, systemd = live_fixture(deployment, monkeypatch)
    admin.administer("install", settings_path, config_path)
    assert systemd.effects() == [["daemon-reload"], ["enable", "--now", admin.SERVICE], ["enable", "--now", admin.TIMER]]
    before = list(systemd.effects())
    admin.administer("install", settings_path, config_path)
    assert systemd.effects() == before
    systemd.units[admin.ARCHIVE]["active"] = True
    admin.administer("rollback", settings_path)
    assert systemd.effects()[3:] == [["disable", "--now", admin.TIMER], ["stop", admin.ARCHIVE],
                                   ["disable", "--now", admin.SERVICE], ["daemon-reload"]]
    receipt = json.loads(receipt_path(root, settings).read_bytes())
    assert receipt["effects"]["stop_archive"]["units"][admin.ARCHIVE]["MainPID"] == "2345"
    assert receipt["phase"] == "rolled_back"


@pytest.mark.parametrize("shell,uid", [("/bin/bash", 990), ("/usr/sbin/nologin", 0), ("/usr/sbin/nologin", 991)])
def test_wrong_account_blocks_before_writes(deployment, shell, uid):
    admin, root, _, settings_path, _, config_path = deployment
    (root / "etc/passwd").write_text(f"fleet-observer:x:{uid}:990:Fixture:/nonexistent:{shell}\n")
    before = files(root)
    with pytest.raises(ValueError, match="account_not_unprivileged"):
        admin.administer("install", settings_path, config_path, root)
    assert files(root) == before


@pytest.mark.parametrize("setting", ["state_dir", "archive_dir", "receipt_dir", "unit_dir"])
def test_overlap_blocks_without_artifacts(deployment, setting):
    admin, root, settings, settings_path, _, config_path = deployment
    settings[setting] = settings["source_dir"]
    settings_path.write_text(json.dumps(settings))
    before = files(root)
    with pytest.raises(ValueError, match="database_outside_state|path_overlap"):
        admin.administer("install", settings_path, config_path, root)
    assert files(root) == before


@pytest.mark.parametrize("change", ["writable", "symlink", "cache"])
def test_untrusted_source_blocks_before_writes(deployment, change):
    admin, root, settings, settings_path, _, config_path = deployment
    code = root / settings["source_dir"].lstrip("/") / "llmsvc/fleet"
    if change == "writable":
        (code / "observer.py").chmod(0o666)
    elif change == "symlink":
        (code / "observer.py").unlink()
        (code / "observer.py").symlink_to(REPO / "llmsvc/fleet/observer.py")
    else:
        (code / "__pycache__").mkdir()
    before = files(root)
    with pytest.raises(ValueError, match="source_not_root_owned|source_unsafe"):
        admin.administer("install", settings_path, config_path, root)
    assert files(root) == before


@pytest.mark.parametrize("extra", [{"read_only": True}, {"fleet_claims_enabled": False}, {"listen_host": "0.0.0.0"}])
def test_config_cannot_add_controls_or_wildcard_bind(deployment, extra):
    admin, root, _, settings_path, config, config_path = deployment
    config.update(extra)
    config_path.write_text(json.dumps(config))
    before = files(root)
    with pytest.raises(ValueError, match="observer_config_invalid"):
        admin.administer("install", settings_path, config_path, root)
    assert files(root) == before


def test_foreign_file_or_unit_is_not_adopted(deployment, monkeypatch):
    admin, root, settings, settings_path, _, config_path, systemd = live_fixture(deployment, monkeypatch)
    systemd.units[admin.SERVICE]["exists"] = True
    with pytest.raises(ValueError, match="foreign_unit"):
        admin.administer("install", settings_path, config_path)
    assert systemd.effects() == [] and not receipt_path(root, settings).exists()
    systemd.units[admin.SERVICE]["exists"] = False
    path = root / settings["config_path"].lstrip("/")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"foreign operator config")
    with pytest.raises(ValueError, match="foreign_file"):
        admin.administer("install", settings_path, config_path)
    assert path.read_bytes() == b"foreign operator config" and systemd.effects() == []


@pytest.mark.parametrize("drift", ["bytes", "mode", "dropin", "fragment", "source"])
def test_changed_identity_blocks_rollback_without_service_effects(deployment, monkeypatch, drift):
    admin, root, settings, settings_path, _, config_path, systemd = live_fixture(deployment, monkeypatch)
    admin.administer("install", settings_path, config_path)
    before = list(systemd.effects())
    config = root / settings["config_path"].lstrip("/")
    if drift == "bytes":
        config.write_bytes(b"operator edit")
    elif drift == "mode":
        config.chmod(0o666)
    elif drift == "source":
        (root / settings["source_dir"].lstrip("/") / "llmsvc/fleet/observer.py").write_text("# Changed source\n")
    else:
        systemd.units[admin.SERVICE]["dropins" if drift == "dropin" else "fragment"] = "/foreign/service.conf"
    with pytest.raises(ValueError, match="owned_file_changed|unit_identity_unknown|source_changed"):
        admin.administer("rollback", settings_path)
    assert systemd.effects() == before and config.exists()


def test_unknown_activation_result_is_retained_and_never_replayed(deployment, monkeypatch):
    admin, root, settings, settings_path, _, config_path, systemd = live_fixture(deployment, monkeypatch)
    systemd.fail = ["enable", "--now", admin.TIMER]
    with pytest.raises(ValueError, match="systemctl_unknown"):
        admin.administer("install", settings_path, config_path)
    receipt = receipt_path(root, settings)
    assert json.loads(receipt.read_bytes())["effects"]["enable_timer"]["state"] == "submitted"
    before = list(systemd.effects())
    for action in ("install", "rollback"):
        with pytest.raises(ValueError, match="action_outcome_unknown"):
            admin.administer(action, settings_path, config_path)
    assert systemd.effects() == before and receipt.exists()


def test_partial_install_rollback_retains_data_and_receipt(deployment, monkeypatch):
    admin, root, settings, settings_path, _, config_path, systemd = live_fixture(deployment, monkeypatch)
    original = admin.write

    def interrupted(path, *args):
        if path.name == admin.TIMER:
            raise OSError("fixture interruption")
        original(path, *args)

    monkeypatch.setattr(admin, "write", interrupted)
    with pytest.raises(OSError):
        admin.administer("install", settings_path, config_path)
    monkeypatch.setattr(admin, "write", original)
    admin.administer("rollback", settings_path)
    assert json.loads(receipt_path(root, settings).read_bytes())["phase"] == "rolled_back"
    assert (root / settings["state_dir"].lstrip("/")).exists()
    assert systemd.effects() == [["daemon-reload"]]


def test_installed_units_pass_local_systemd_verification(deployment):
    executable = shutil.which("systemd-analyze")
    if executable is None:
        pytest.skip("systemd-analyze unavailable")
    admin, root, settings, settings_path, _, config_path = deployment
    admin.administer("install", settings_path, config_path, root)
    units = root / settings["unit_dir"].lstrip("/")
    result = subprocess.run([executable, "verify", *(str(units / unit) for unit in admin.UNITS)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=10, check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    assert result.returncode == 0, result.stderr.decode()


def test_systemctl_commands_are_bounded_and_shell_free(deployment, monkeypatch):
    admin, *_ = deployment

    def run(argv, **kwargs):
        assert argv == ["/usr/bin/systemctl", "show", admin.TIMER]
        assert kwargs["timeout"] == 10 and kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return subprocess.CompletedProcess(argv, 0, b"fixture", b"")

    monkeypatch.setattr(admin.subprocess, "run", run)
    assert admin.control(["show", admin.TIMER]) == "fixture"
