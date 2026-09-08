# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


SPEC = importlib.util.spec_from_file_location("deploy_observer", Path(__file__).parents[1] / "deploy/observer.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def write_inputs(tmp_path, read_only=True):
    root = tmp_path / "stage"
    root.mkdir(parents=True)
    settings = json.loads((Path(__file__).parents[1] / "deploy/deployment.example.json").read_text())
    settings["python"] = sys.executable
    proposal = json.loads((Path(__file__).parents[1] / "deploy/observer-activation-proposal.json").read_text())
    proposal["source_checkout_path"] = "/opt/llmsvc-source"
    settings_path = tmp_path / "settings.json"
    proposal_path = tmp_path / "proposal.json"
    config_path = tmp_path / "scheduler.yaml"
    config_path.write_text("read_only: " + ("true" if read_only else "false") + "\nlisten_host: 127.0.0.1\nlisten_port: 8011\n", encoding="utf-8")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    args = [
        "--settings", str(settings_path),
        "--proposal", str(proposal_path),
        "--config", str(config_path),
        "--root", str(root),
    ]
    return root, settings, proposal, args


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_stage_renders_read_only_scheduler_capture_units_and_manifest(tmp_path):
    root, settings, proposal, args = write_inputs(tmp_path)

    assert observer.main(["stage", *args]) == 0

    scheduler_unit = root / settings["unit_path"].lstrip("/")
    capture_unit = root / "etc/systemd/system/llmsvc-observation-capture.service"
    capture_timer = root / "etc/systemd/system/llmsvc-observation-capture.timer"
    capture_config = root / proposal["capture_config_path"].lstrip("/")
    manifest = json.loads((root / settings["prefix"].lstrip("/") / "observer-manifest.json").read_text())

    assert "--dry-run" in scheduler_unit.read_text(encoding="utf-8")
    assert "StandardOutput=journal" in scheduler_unit.read_text(encoding="utf-8")
    assert "deploy/capture.py" in capture_unit.read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15" in capture_timer.read_text(encoding="utf-8")
    assert "OnActiveSec=1s" in capture_timer.read_text(encoding="utf-8")
    assert json.loads(capture_config.read_text(encoding="utf-8")) == proposal["capture_config"]
    assert manifest["units"] == {
        "scheduler_service": "llmsvc-scheduler.service",
        "capture_service": "llmsvc-observation-capture.service",
        "capture_timer": "llmsvc-observation-capture.timer",
    }
    assert {item["role"] for item in manifest["files"]} == {
        "scheduler_unit", "scheduler_config", "capture_unit", "capture_timer", "capture_config",
    }


def test_read_only_false_refuses_before_mutation(tmp_path):
    root, settings, proposal, args = write_inputs(tmp_path, read_only=False)
    before = files(root)

    assert observer.main(["stage", *args]) == 1

    assert files(root) == before


def test_dry_run_has_zero_filesystem_mutation(tmp_path):
    root, settings, proposal, args = write_inputs(tmp_path)
    before = files(root)

    assert observer.main(["stage", *args, "--dry-run"]) == 0

    assert files(root) == before


def test_foreign_destination_and_symlink_are_refused(tmp_path):
    root, settings, proposal, args = write_inputs(tmp_path)
    unit = root / settings["unit_path"].lstrip("/")
    unit.parent.mkdir(parents=True)
    unit.write_text("foreign\n", encoding="utf-8")
    before = files(root)

    assert observer.main(["stage", *args]) == 1
    assert files(root) == before

    root, settings, proposal, args = write_inputs(tmp_path / "symlink")
    (root / "etc").symlink_to(tmp_path, target_is_directory=True)
    assert observer.main(["stage", *args]) == 1


def test_changed_owned_file_refuses_stop_or_remove(tmp_path):
    root, settings, proposal, args = write_inputs(tmp_path)
    assert observer.main(["stage", *args]) == 0
    capture_config = root / proposal["capture_config_path"].lstrip("/")
    capture_config.write_text("{}\n", encoding="utf-8")
    before = files(root)

    assert observer.main(["stop", *args, "--dry-run"]) == 1
    assert observer.main(["remove", *args]) == 1

    assert files(root) == before


def test_remove_can_rerun_after_partial_cleanup_failure(tmp_path, monkeypatch):
    root, settings, proposal, args = write_inputs(tmp_path)
    assert observer.main(["stage", *args]) == 0
    original_unlink = observer.Path.unlink
    calls = {"count": 0}

    def fail_once(path, *call_args, **kwargs):
        if path.name == "llmsvc-observation-capture.timer" and calls["count"] == 0:
            calls["count"] += 1
            raise OSError("first cleanup failed")
        return original_unlink(path, *call_args, **kwargs)

    monkeypatch.setattr(observer.Path, "unlink", fail_once)
    assert observer.main(["remove", *args]) == 1
    monkeypatch.setattr(observer.Path, "unlink", original_unlink)

    assert observer.main(["remove", *args]) == 0
    assert not (root / settings["prefix"].lstrip("/")).exists()


def test_live_start_stop_remove_use_only_exact_owned_units(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "emit", lambda *args, **kwargs: None)
    root, settings, proposal, args = write_inputs(tmp_path)
    assert observer.main(["stage", *args]) == 0
    manifest_path = root / settings["prefix"].lstrip("/") / "observer-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root"] = "/"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    live_args = []
    for index, item in enumerate(args):
        live_args.extend(["/"] if item == str(root) and args[index - 1] == "--root" else [item])

    commands = []

    def fake_inside(root_arg, value):
        return root / value.lstrip("/")

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["systemctl", "show"]:
            unit = command[2]
            description = "[llmsvc-owner:" + manifest["ownership_token"] + "] test"
            return subprocess.CompletedProcess(command, 0, stdout="LoadState=loaded\nDescription="+description+"\nDropInPaths=\nFragmentPath=/etc/systemd/system/"+unit+"\n")
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(observer, "inside", fake_inside)
    monkeypatch.setattr(observer.subprocess, "run", fake_run)

    assert observer.main(["start", *live_args]) == 0
    assert ["systemctl", "start", "llmsvc-scheduler.service"] in commands
    assert ["systemctl", "start", "llmsvc-observation-capture.timer"] in commands
    assert not any("vllm-" in part for command in commands for part in command)

    commands.clear()
    assert observer.main(["stop", *live_args]) == 0
    stop_commands = [command for command in commands if command[:2] == ["systemctl", "stop"]]
    assert stop_commands == [
        ["systemctl", "stop", "llmsvc-observation-capture.timer"],
        ["systemctl", "stop", "llmsvc-observation-capture.service"],
        ["systemctl", "stop", "llmsvc-scheduler.service"],
    ]

    commands.clear()
    assert observer.main(["remove", *live_args]) == 0
    assert ["systemctl", "daemon-reload"] in commands
    assert not any("vllm-" in part for command in commands for part in command)


def test_live_start_dry_run_uses_no_systemctl(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "emit", lambda *args, **kwargs: None)
    root, settings, proposal, args = write_inputs(tmp_path)
    assert observer.main(["stage", *args]) == 0
    manifest_path = root / settings["prefix"].lstrip("/") / "observer-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root"] = "/"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    live_args = []
    for index, item in enumerate(args):
        live_args.extend(["/"] if item == str(root) and args[index - 1] == "--root" else [item])

    monkeypatch.setattr(observer, "inside", lambda root_arg, value: root / value.lstrip("/"))
    monkeypatch.setattr(observer.subprocess, "run", lambda command, **kwargs: (_ for _ in ()).throw(AssertionError(command)))

    assert observer.main(["start", *live_args, "--dry-run"]) == 0


def test_live_foreign_unit_refuses_start(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "emit", lambda *args, **kwargs: None)
    root, settings, proposal, args = write_inputs(tmp_path)
    assert observer.main(["stage", *args]) == 0
    manifest_path = root / settings["prefix"].lstrip("/") / "observer-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root"] = "/"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    live_args = []
    for index, item in enumerate(args):
        live_args.extend(["/"] if item == str(root) and args[index - 1] == "--root" else [item])

    monkeypatch.setattr(observer, "inside", lambda root_arg, value: root / value.lstrip("/"))

    def fake_run(command, **kwargs):
        if command[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(command, 0, stdout="LoadState=loaded\nFragmentPath=/etc/systemd/system/foreign.service\nDescription=foreign\nDropInPaths=\n")
        raise AssertionError("unexpected mutation command")

    monkeypatch.setattr(observer.subprocess, "run", fake_run)

    assert observer.main(["start", *live_args]) == 1


def test_manifest_cannot_authorize_outside_directories_or_duplicate_paths(tmp_path):
    root, settings, proposal, args = write_inputs(tmp_path)
    assert observer.main(["stage", *args]) == 0
    path=root/settings["prefix"].lstrip("/")/"observer-manifest.json"
    original=json.loads(path.read_text())
    foreign=tmp_path/"foreign";foreign.mkdir()
    manifest=dict(original);manifest["directories"]=original["directories"]+[str(foreign)]
    path.write_text(json.dumps(manifest))
    assert observer.main(["remove", *args]) == 1
    assert foreign.exists()
    manifest=dict(original);manifest["files"]=[{**original["files"][0],"path":"/foreign"}]+original["files"]
    path.write_text(json.dumps(manifest))
    assert observer.main(["remove", *args]) == 1


def test_runtime_ownership_requires_token_path_and_no_dropins(monkeypatch):
    import pytest
    token="a"*32
    values={"LoadState":"loaded","FragmentPath":"/etc/systemd/system/llmsvc-scheduler.service","Description":"[llmsvc-owner:"+token+"] observer","DropInPaths":""}
    monkeypatch.setattr(observer,"unit_properties",lambda unit:values)
    assert observer.assert_live_unit_ownership("llmsvc-scheduler.service",values["FragmentPath"],token)
    values["Description"]="foreign"
    with pytest.raises(observer.ObserverError):observer.assert_live_unit_ownership("llmsvc-scheduler.service",values["FragmentPath"],token)
    values["Description"]="[llmsvc-owner:"+token+"] observer";values["DropInPaths"]="/etc/foreign.conf"
    with pytest.raises(observer.ObserverError):observer.assert_live_unit_ownership("llmsvc-scheduler.service",values["FragmentPath"],token)
