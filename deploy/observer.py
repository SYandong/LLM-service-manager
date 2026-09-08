#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Stage or operate the owned read-only observer units."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import yaml


SCHEMA_VERSION = 1
JOURNAL = False


class ObserverError(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(event, **detail):
    record = json.dumps({"event": event, **detail}, sort_keys=True)
    print(record, flush=True)
    if JOURNAL:
        import syslog
        syslog.openlog("llmsvc-observer")
        syslog.syslog(syslog.LOG_INFO, record)


def inside(root, value):
    logical = Path(value)
    if not logical.is_absolute() or ".." in logical.parts or logical == Path("/"):
        raise ObserverError("observer paths must be absolute non-root paths")
    target = root / logical.relative_to("/")
    for item in [target, *target.parents]:
        if item.is_symlink():
            raise ObserverError("symlink observer path: " + str(item))
        if item == root:
            break
    return target


def require_name(value, key):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", value):
        raise ObserverError("invalid " + key)
    return value


def load_json(path, label):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ObserverError("cannot read " + label + ": " + str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ObserverError("invalid " + label + " JSON: " + str(exc)) from exc
    if not isinstance(data, dict):
        raise ObserverError(label + " must be a JSON object")
    return data


def load_settings(path):
    settings = load_json(path, "settings")
    for key in ("prefix", "unit_path", "config_path", "state_dir", "python", "user"):
        if not isinstance(settings.get(key), str) or not settings[key]:
            raise ObserverError("missing setting: " + key)
    for key in ("prefix", "unit_path", "config_path", "state_dir", "python"):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", settings[key]):
            raise ObserverError("unsupported characters in " + key)
    if Path(settings["unit_path"]).name != "llmsvc-scheduler.service":
        raise ObserverError("unit_path must name llmsvc-scheduler.service")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", settings["user"]):
        raise ObserverError("invalid service user")
    return settings


def load_proposal(path):
    proposal = load_json(path, "proposal")
    if proposal.get("read_only") is not True:
        raise ObserverError("proposal read_only must be true")
    if proposal.get("required_exec_flag") != "--dry-run":
        raise ObserverError("proposal must require --dry-run")
    service = require_name(proposal.get("service"), "observer service")
    capture = proposal.get("capture_schedule_proposal")
    if not isinstance(capture, dict):
        raise ObserverError("missing capture schedule proposal")
    capture_service = require_name(capture.get("service"), "capture service")
    capture_timer = require_name(capture.get("timer"), "capture timer")
    if service == capture_service or service == capture_timer or capture_service == capture_timer:
        raise ObserverError("observer unit names must be distinct")
    if not capture_service.endswith(".service") or not capture_timer.endswith(".timer") or not service.endswith(".service"):
        raise ObserverError("observer unit names must be service/timer units")
    for key in ("capture_config_path", "private_observation_directory", "source_checkout_path"):
        if not isinstance(proposal.get(key), str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", proposal[key]):
            raise ObserverError("missing or invalid " + key)
    if not capture_service.startswith("llmsvc-observation-") or not capture_timer.startswith("llmsvc-observation-"):
        raise ObserverError("capture units must use the llmsvc-observation namespace")
    if capture.get("umask", "0077") != "0077":
        raise ObserverError("capture umask must be 0077")
    seconds = capture.get("on_unit_active_seconds")
    runtime = capture.get("runtime_max_seconds")
    if type(seconds) is not int or seconds <= 0:
        raise ObserverError("capture on_unit_active_seconds must be positive")
    if type(runtime) is not int or runtime <= 0:
        raise ObserverError("capture runtime_max_seconds must be positive")
    return proposal


def load_scheduler_config(path):
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ObserverError("cannot read scheduler config: " + str(exc)) from exc
    if not isinstance(config, dict):
        raise ObserverError("scheduler config must be a mapping")
    if config.get("read_only") is not True:
        raise ObserverError("scheduler config read_only must be true")
    from llmsvc.config import SchedulerConfig
    SchedulerConfig(**config)
    return config


def scheduler_unit_text(settings):
    return """# Generated-By: Codex / gpt-6-astra
[Unit]
Description=LLM scheduler read-only observation
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={state_dir}
ExecStart={prefix}/venv/bin/python -m llmsvc --config {config_path} --dry-run
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=llmsvc-scheduler
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
""".format(**settings)


def capture_unit_text(settings, proposal):
    capture = proposal["capture_schedule_proposal"]
    return """# Generated-By: Codex / gpt-6-astra
[Unit]
Description=LLM scheduler observation capture
After={service}
Wants={service}

[Service]
Type=oneshot
User={user}
WorkingDirectory={state_dir}
ExecStart={python} {source_checkout_path}/deploy/capture.py --config {capture_config_path} --output-dir {private_observation_directory}
TimeoutStartSec={runtime_max_seconds}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=llmsvc-observation-capture
UMask={umask}
NoNewPrivileges=true
""".format(user=settings["user"], python=settings["python"], state_dir=settings["state_dir"],
           service=proposal["service"], source_checkout_path=proposal["source_checkout_path"],
           capture_config_path=proposal["capture_config_path"],
           private_observation_directory=proposal["private_observation_directory"],
           runtime_max_seconds=capture["runtime_max_seconds"], umask=capture.get("umask", "0077"))


def capture_timer_text(proposal):
    capture = proposal["capture_schedule_proposal"]
    return """# Generated-By: Codex / gpt-6-astra
[Unit]
Description=LLM scheduler observation capture timer

[Timer]
OnActiveSec=1s
OnUnitActiveSec={on_unit_active_seconds}
AccuracySec=1s
Unit={service}
Persistent=false

[Install]
WantedBy=timers.target
""".format(on_unit_active_seconds=capture["on_unit_active_seconds"], service=capture["service"])


def write_new(path, content, mode, directories, files):
    ensure_dir(path.parent, directories)
    with path.open("x", encoding="utf-8") as handle:
        try:
            handle.write(content)
            path.chmod(mode)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    files.append(path)


def ensure_dir(path, directories):
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directories.append(str(directory))


def observer_paths(root, settings, proposal):
    return {
        "manifest": inside(root, settings["prefix"] + "/observer-manifest.json"),
        "scheduler_unit": inside(root, settings["unit_path"]),
        "scheduler_config": inside(root, settings["config_path"]),
        "capture_unit": inside(root, str(Path(settings["unit_path"]).parent) + "/" + proposal["capture_schedule_proposal"]["service"]),
        "capture_timer": inside(root, str(Path(settings["unit_path"]).parent) + "/" + proposal["capture_schedule_proposal"]["timer"]),
        "capture_config": inside(root, proposal["capture_config_path"]),
        "observation_dir": inside(root, proposal["private_observation_directory"]),
    }


def build_rendered(settings, proposal, scheduler_config_text):
    return {
        "scheduler_unit": scheduler_unit_text(settings),
        "scheduler_config": scheduler_config_text,
        "capture_unit": capture_unit_text(settings, proposal),
        "capture_timer": capture_timer_text(proposal),
        "capture_config": json.dumps(proposal["capture_config"], indent=2, sort_keys=True) + "\n",
    }


def validate_inputs(args):
    settings = load_settings(args.settings)
    proposal = load_proposal(args.proposal)
    config = load_scheduler_config(args.config)
    config_text = args.config.read_text(encoding="utf-8")
    if proposal["service"] != Path(settings["unit_path"]).name:
        raise ObserverError("proposal service must match settings unit_path")
    if not isinstance(proposal.get("capture_config"), dict):
        raise ObserverError("proposal requires capture_config")
    from urllib.parse import urlsplit
    capture_sources = proposal["capture_config"].get("sources", [])
    if not isinstance(capture_sources, list) or len(capture_sources) != 1 or not isinstance(capture_sources[0], dict) or capture_sources[0].get("type") != "http_json" or capture_sources[0].get("method", "GET") != "GET":
        raise ObserverError("capture must only GET this scheduler state")
    url = urlsplit(capture_sources[0].get("url", ""))
    if (url.scheme != "http" or url.hostname != config["listen_host"] or url.port != config["listen_port"]
            or url.path != "/v1/state" or url.query or url.fragment or url.username or url.password):
        raise ObserverError("capture URL must match the configured scheduler state endpoint")
    return settings, proposal, build_rendered(settings, proposal, config_text)


def stage(args, settings, proposal, rendered, root):
    paths = observer_paths(root, settings, proposal)
    file_paths = [p for key, p in paths.items() if key != "observation_dir"]
    if len(set(paths.values())) != len(paths) or any(a in b.parents or b in a.parents for i,a in enumerate(file_paths) for b in file_paths[i+1:]):
        raise ObserverError("observer file paths overlap")
    if paths["observation_dir"].exists():
        raise ObserverError("observation directory exists; refuse foreign state")
    if paths["manifest"].exists():
        raise ObserverError("observer manifest already exists")
    for key in ("scheduler_unit", "scheduler_config", "capture_unit", "capture_timer", "capture_config"):
        if paths[key].exists():
            raise ObserverError("destination exists; refuse to adopt: " + str(paths[key]))
    plan = {
        "root": str(root),
        "service": proposal["service"],
        "capture_service": proposal["capture_schedule_proposal"]["service"],
        "capture_timer": proposal["capture_schedule_proposal"]["timer"],
        "read_only": True,
    }
    emit("observer_stage_plan", dry_run=args.dry_run, **plan)
    if args.dry_run:
        return
    if root == Path("/"):
        for unit in (proposal["service"], proposal["capture_schedule_proposal"]["service"], proposal["capture_schedule_proposal"]["timer"]):
            values = unit_properties(unit)
            if values.get("LoadState") != "not-found":
                raise ObserverError("refuse preexisting live unit: " + unit)
    token = uuid.uuid4().hex
    rendered = dict(rendered)
    for key in ("scheduler_unit", "capture_unit", "capture_timer"):
        rendered[key] = rendered[key].replace("Description=", "Description=[llmsvc-owner:" + token + "] ", 1)
    created_dirs = []
    created_files = []
    try:
        ensure_dir(paths["observation_dir"], created_dirs)
        for key, mode in (
                ("scheduler_unit", 0o644),
                ("scheduler_config", 0o600),
                ("capture_unit", 0o644),
                ("capture_timer", 0o644),
                ("capture_config", 0o600)):
            write_new(paths[key], rendered[key], mode, created_dirs, created_files)
        files = []
        for key in ("scheduler_unit", "scheduler_config", "capture_unit", "capture_timer", "capture_config"):
            files.append({"role": key, "path": logical_path(paths[key], root), "sha256": digest(paths[key])})
        ensure_dir(paths["manifest"].parent, created_dirs)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "ownership_token": token,
            "settings": settings,
            "proposal": proposal,
            "root": str(root),
            "files": files,
            "directories": created_dirs,
            "units": {
                "scheduler_service": proposal["service"],
                "capture_service": proposal["capture_schedule_proposal"]["service"],
                "capture_timer": proposal["capture_schedule_proposal"]["timer"],
            },
            "started": False,
        }
        write_new(paths["manifest"], json.dumps(manifest, indent=2, sort_keys=True) + "\n", 0o600, created_dirs, created_files)
        emit("observer_stage_complete", **plan)
    except BaseException:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for value in reversed(created_dirs):
            try:
                Path(value).rmdir()
            except OSError:
                pass
        raise


def logical_path(path, root):
    if root == Path("/"):
        return str(path)
    return "/" + str(path.relative_to(root))


def load_manifest(root, settings, proposal, allow_missing_files=False):
    paths = observer_paths(root, settings, proposal)
    if not paths["manifest"].is_file():
        raise ObserverError("missing observer manifest")
    manifest = load_json(paths["manifest"], "observer manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("settings") != settings:
        raise ObserverError("observer manifest/settings mismatch")
    if manifest.get("proposal") != proposal or manifest.get("root") != str(root):
        raise ObserverError("observer manifest/proposal/root mismatch")
    expected_units = {
        "scheduler_service": proposal["service"],
        "capture_service": proposal["capture_schedule_proposal"]["service"],
        "capture_timer": proposal["capture_schedule_proposal"]["timer"],
    }
    if manifest.get("units") != expected_units:
        raise ObserverError("observer unit scope mismatch")
    expected_files = {
        "scheduler_unit": settings["unit_path"],
        "scheduler_config": settings["config_path"],
        "capture_unit": str(Path(settings["unit_path"]).parent) + "/" + expected_units["capture_service"],
        "capture_timer": str(Path(settings["unit_path"]).parent) + "/" + expected_units["capture_timer"],
        "capture_config": proposal["capture_config_path"],
    }
    token = manifest.get("ownership_token", "")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ObserverError("invalid ownership token")
    entries = manifest.get("files", [])
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ObserverError("invalid manifest files")
    records = {item.get("role"): item for item in entries}
    if len(records) != len(entries):
        raise ObserverError("duplicate manifest file roles")
    allowed_dirs = set()
    for target in paths.values():
        allowed_dirs.update(str(parent) for parent in target.parents if parent != root and root in parent.parents)
    allowed_dirs.add(str(paths["observation_dir"]))
    directories = manifest.get("directories", [])
    if not isinstance(directories, list) or any(value not in allowed_dirs for value in directories):
        raise ObserverError("manifest directory scope mismatch")
    for value in directories:
        inside(root, logical_path(Path(value), root))
    if set(records) != set(expected_files):
        raise ObserverError("observer file scope mismatch")
    for role, logical in expected_files.items():
        if records[role].get("path") != logical:
            raise ObserverError("observer file path mismatch")
        path = inside(root, logical)
        if allow_missing_files and not path.exists():
            continue
        if not path.is_file() or path.is_symlink() or digest(path) != records[role].get("sha256"):
            raise ObserverError("observer file changed or missing: " + logical)
    return manifest


def systemctl(argv):
    return subprocess.run(argv, check=True, timeout=30)


def unit_properties(unit):
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=FragmentPath,LoadState,Description,DropInPaths"],
        capture_output=True, text=True, check=True, timeout=10)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def assert_live_unit_ownership(unit, expected_path, token):
    values = unit_properties(unit)
    if values.get("LoadState") == "not-found":
        return False
    if (values.get("LoadState") != "loaded" or values.get("FragmentPath") != expected_path
            or "[llmsvc-owner:" + token + "]" not in values.get("Description", "")
            or values.get("DropInPaths") != ""):
        raise ObserverError("refuse foreign or altered live unit: " + unit)
    return True


def assert_runtime_ownership(manifest, proposal):
    expected = {item["role"]: item["path"] for item in manifest["files"]}
    units = ((proposal["service"], expected["scheduler_unit"]),
             (proposal["capture_schedule_proposal"]["service"], expected["capture_unit"]),
             (proposal["capture_schedule_proposal"]["timer"], expected["capture_timer"]))
    return {unit for unit,path in units if assert_live_unit_ownership(unit, path, manifest["ownership_token"])}


def start(args, settings, proposal, root):
    if root != Path("/"):
        raise ObserverError("start is live-only; use stage for rehearsal roots")
    manifest = load_manifest(root, settings, proposal)
    emit("observer_start_plan", dry_run=args.dry_run, units=manifest["units"], read_only=True)
    if args.dry_run:
        return
    assert_runtime_ownership(manifest, proposal)
    systemctl(["systemctl", "daemon-reload"])
    live = assert_runtime_ownership(manifest, proposal)
    if set(manifest["units"].values()) != live:
        raise ObserverError("rendered observer units not loaded after daemon-reload")
    try:
        systemctl(["systemctl", "start", proposal["service"]])
        systemctl(["systemctl", "start", proposal["capture_schedule_proposal"]["timer"]])
    except subprocess.SubprocessError:
        for unit in (proposal["capture_schedule_proposal"]["timer"], proposal["capture_schedule_proposal"]["service"], proposal["service"]):
            if unit in assert_runtime_ownership(manifest, proposal):
                systemctl(["systemctl", "stop", unit])
        raise
    emit("observer_start_complete", units=manifest["units"], read_only=True)


def stop(args, settings, proposal, root):
    manifest = load_manifest(root, settings, proposal)
    emit("observer_stop_plan", dry_run=args.dry_run, units=manifest["units"])
    if args.dry_run:
        return
    if root != Path("/"):
        return
    live = assert_runtime_ownership(manifest, proposal)
    for unit in (proposal["capture_schedule_proposal"]["timer"], proposal["capture_schedule_proposal"]["service"], proposal["service"]):
        if unit in live:
            systemctl(["systemctl", "stop", unit])
    emit("observer_stop_complete", units=manifest["units"])


def remove(args, settings, proposal, root):
    manifest = load_manifest(root, settings, proposal, allow_missing_files=True)
    paths = observer_paths(root, settings, proposal)
    observation_dir = paths["observation_dir"]
    if observation_dir.exists() and any(observation_dir.iterdir()):
        raise ObserverError("observation directory is nonempty; archive evidence before remove")
    emit("observer_remove_plan", dry_run=args.dry_run, units=manifest["units"])
    if args.dry_run:
        return
    if root == Path("/"):
        live = assert_runtime_ownership(manifest, proposal)
        for unit in (proposal["capture_schedule_proposal"]["timer"], proposal["capture_schedule_proposal"]["service"], proposal["service"]):
            if unit in live:
                systemctl(["systemctl", "stop", unit])
    for item in manifest["files"]:
        inside(root, item["path"]).unlink(missing_ok=True)
    paths["manifest"].unlink(missing_ok=True)
    for value in reversed(manifest.get("directories", [])):
        try:
            Path(value).rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if root == Path("/"):
        systemctl(["systemctl", "daemon-reload"])
    emit("observer_remove_complete", units=manifest["units"])


def main(argv=None):
    global JOURNAL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "start", "stop", "remove"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.root.is_dir():
            raise ObserverError("create the staging root first")
        if not args.root.is_absolute() or args.root.is_symlink() or args.root.resolve() != args.root:
            raise ObserverError("root must be an absolute canonical non-symlink path")
        JOURNAL = args.root == Path("/") and not args.dry_run
        settings, proposal, rendered = validate_inputs(args)
        if args.action == "stage":
            stage(args, settings, proposal, rendered, args.root)
        elif args.action == "start":
            start(args, settings, proposal, args.root)
        elif args.action == "stop":
            stop(args, settings, proposal, args.root)
        else:
            remove(args, settings, proposal, args.root)
        return 0
    except (ObserverError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        emit("observer_error", error=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
