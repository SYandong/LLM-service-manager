#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Stage the read-only scheduler without changing the running data plane."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


JOURNAL = False


class DeploymentError(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(event, **detail):
    # systemd captures stdout as structured journal messages when run as a unit.
    record = json.dumps({"event": event, **detail}, sort_keys=True)
    print(record, flush=True)
    if JOURNAL:
        import syslog
        syslog.openlog("llmsvc-deploy")
        syslog.syslog(syslog.LOG_INFO, record)


def inside(root, value):
    """Reject path traversal and symlinks, including dangling symlinks."""
    logical = Path(value)
    if not logical.is_absolute() or ".." in logical.parts or logical == Path("/"):
        raise DeploymentError("deployment paths must be absolute non-root paths")
    target = root / logical.relative_to("/")
    for part in [target, *target.parents]:
        if part.is_symlink():
            raise DeploymentError("symlink deployment path: " + str(part))
        if part == root:
            break
    return target


def load_settings(path):
    settings = json.loads(path.read_text())
    for key in ("prefix", "unit_path", "config_path", "cli_path", "state_dir", "python", "user"):
        if not isinstance(settings.get(key), str) or not settings[key]:
            raise DeploymentError("missing setting: " + key)
    # Paths appear in systemd directives, so refuse control/quoting specifiers.
    for key in ("prefix", "unit_path", "config_path", "cli_path", "state_dir", "python"):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", settings[key]):
            raise DeploymentError("unsupported characters in " + key)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", settings["user"]):
        raise DeploymentError("invalid service user")
    if Path(settings["unit_path"]).name != "llmsvc-scheduler.service":
        raise DeploymentError("unit_path must name llmsvc-scheduler.service")
    paths = [Path(settings[k]) for k in ("prefix", "unit_path", "config_path", "cli_path", "state_dir")]
    if len(set(paths)) != len(paths) or any(a in b.parents or b in a.parents for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise DeploymentError("managed paths must not overlap")
    return settings


def unit_text(settings):
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


def write_new(path, content, mode, directories):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        directories.append(str(directory))
    with path.open("x") as handle:
        try:
            handle.write(content)
            path.chmod(mode)
        except BaseException:
            path.unlink()
            raise


def install(args, settings, root):
    prefix = inside(root, settings["prefix"])
    paths = {key: inside(root, settings[key]) for key in ("unit_path", "config_path", "cli_path", "state_dir")}
    if prefix.exists() or any(paths[key].exists() for key in ("unit_path", "config_path", "cli_path")):
        raise DeploymentError("destination exists; preserve it and use a fresh staging root")
    if paths["state_dir"].exists():
        raise DeploymentError("state directory exists; refuse to adopt unowned state")
    source = args.source.resolve()
    config = args.config.resolve()
    if root == source or source in root.parents:
        raise DeploymentError("staging root must be outside the source tree")
    for required in (source / "pyproject.toml", source / "llmsvc/__init__.py", source / "cli/llm", config):
        if not required.is_file():
            raise DeploymentError("missing install prerequisite: " + str(required))
    backups = []
    for logical in settings.get("backup_files", []):
        existing = inside(root, logical)
        if not existing.is_file():
            raise DeploymentError("missing pre-scheduler backup source: " + logical)
        backups.append((logical, existing))
    plan = {"root": str(root), "prefix": str(prefix), "config": str(config),
            "read_only": True, "activate": False, "backups": [x[0] for x in backups]}
    emit("install_plan", dry_run=args.dry_run, **plan)
    if args.dry_run:
        return
    created_dirs = []
    created_files = []
    try:
        # All directories are owned in the manifest, so failed staging can unwind.
        for directory in (prefix, paths["state_dir"]):
            missing = []
            parent = directory
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for item in reversed(missing):
                item.mkdir(mode=0o700)
                created_dirs.append(str(item))
        (prefix / "backup").mkdir()
        backup_records = []
        for index, (logical, existing) in enumerate(backups):
            target = prefix / "backup" / str(index)
            shutil.copy2(existing, target)
            backup_records.append({"path": logical, "file": str(index), "sha256": digest(target),
                                   "mode": existing.stat().st_mode & 0o777})
        subprocess.run([settings["python"], "-m", "venv", str(prefix / "venv")], check=True)
        pip = [str(prefix / "venv/bin/python"), "-m", "pip", "install"]
        if args.wheelhouse:
            pip += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
        # Build from an owned copy: pip/setuptools may create build metadata.
        build_source = prefix / "source"
        shutil.copytree(source, build_source, ignore=shutil.ignore_patterns(
            ".git", ".omx", ".codex", ".agents", "var", "__pycache__",
            ".pytest_cache", "*.egg-info", "build", "dist", ".venv"))
        subprocess.run(pip + [str(build_source)], check=True)
        shutil.rmtree(build_source)
        subprocess.run([str(prefix / "venv/bin/python"), "-m", "llmsvc",
                        "--config", str(config), "--dry-run", "--check-config"], check=True)
        for key, content, mode in (
                ("unit_path", unit_text(settings), 0o644),
                ("config_path", config.read_text(), 0o600),
                ("cli_path", (source / "cli/llm").read_text(), 0o755)):
            write_new(paths[key], content, mode, created_dirs)
            created_files.append(paths[key])
        manifest = {"schema_version": 1, "settings": settings, "root": str(root),
                    "files": [{"path": settings[key], "sha256": digest(paths[key])}
                              for key in ("unit_path", "config_path", "cli_path")],
                    "directories": created_dirs, "backups": backup_records,
                    "activated": False}
        (prefix / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        emit("install_complete", **plan)
    except BaseException:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        if str(prefix) in created_dirs and prefix.exists():
            shutil.rmtree(prefix)
        for value in reversed(created_dirs):
            try:
                Path(value).rmdir()
            except OSError:
                pass
        raise


def manifest_for(settings, root):
    prefix = inside(root, settings["prefix"])
    path = inside(root, settings["prefix"] + "/manifest.json")
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") == 2:
        raise DeploymentError("versioned installation: use upgrade.py rollback; archive generations before uninstall")
    if manifest.get("schema_version") != 1 or manifest.get("settings") != settings or manifest.get("root") != str(root):
        raise DeploymentError("manifest/settings/root mismatch")
    # Manifest must never authorize deletion outside this installation.
    expected = {settings[key] for key in ("unit_path", "config_path", "cli_path")}
    if {item["path"] for item in manifest["files"]} != expected:
        raise DeploymentError("manifest file list mismatch")
    allowed_dirs = set()
    for value in [prefix, *[inside(root, settings[k]) for k in ("unit_path", "config_path", "cli_path", "state_dir")]]:
        allowed_dirs.update(str(p) for p in [value, *value.parents] if p != root and root in p.parents)
    if not set(manifest["directories"]) <= allowed_dirs:
        raise DeploymentError("manifest directory list mismatch")
    return prefix, manifest


def uninstall(args, settings, root):
    prefix, manifest = manifest_for(settings, root)
    for item in manifest["files"]:
        path = inside(root, item["path"])
        if path.exists() and (not path.is_file() or digest(path) != item["sha256"]):
            raise DeploymentError("managed file changed; preserve and reconcile before uninstall: " + str(path))
    state = inside(root, settings["state_dir"])
    if state.exists() and any(state.iterdir()):
        raise DeploymentError("state directory is nonempty; archive observations/intents before uninstall")
    if root == Path("/"):
        result = subprocess.run(["systemctl", "show", "llmsvc-scheduler.service", "--property=ActiveState", "--value"], capture_output=True, text=True, check=True)
        if result.stdout.strip() not in ("inactive", "failed"):
            raise DeploymentError("disable/stop the scheduler before uninstall; use rollback runbook")
        # Do not delete backups from a deployment with active production policy.
        raise DeploymentError("live uninstall requires reviewed rollback/archive handoff; use a staging root for rehearsal")
    emit("uninstall_plan", dry_run=args.dry_run, root=str(root), files=manifest["files"])
    if args.dry_run:
        return
    for item in manifest["files"]:
        inside(root, item["path"]).unlink(missing_ok=True)
    shutil.rmtree(prefix)
    for value in reversed(manifest["directories"]):
        try:
            Path(value).rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # A new, unrelated file appeared in a parent directory; preserve it.
            pass
    emit("uninstall_complete", root=str(root))


def rollback(args, settings, root):
    prefix, manifest = manifest_for(settings, root)
    for item in manifest["backups"]:
        original = inside(root, item["path"])
        if item["path"] not in settings.get("backup_files", []) or not re.fullmatch(r"[0-9]+", item["file"]):
            raise DeploymentError("invalid backup record")
        backup = inside(root, settings["prefix"] + "/backup/" + item["file"])
        if backup.is_symlink() or digest(backup) != item["sha256"]:
            raise DeploymentError("backup hash mismatch")
        if original.exists() and not original.is_file():
            raise DeploymentError("backup target is not a file")
    emit("rollback_plan", dry_run=args.dry_run, steps=[
        "Stop/disable scheduler service and its timers",
        "Restore pre-scheduler launch/reaper scripts and re-enable reaper timer",
        "Validate complete saved llama-swap config; wait for protected-model/RAM/quiet gate then atomically restore/reload",
        "Reconcile units with /running; observe original idle TTL"], root=str(root))
    if args.dry_run:
        return
    if root == Path("/"):
        raise DeploymentError("production rollback needs current quiet-period/protection evidence; follow OPERATIONS.md")
    # Staging rehearsal restores bytes only; never invokes host systemctl/reload.
    for item in manifest["backups"]:
        target = inside(root, item["path"])
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write((prefix / "backup" / item["file"]).read_bytes())
        temporary.chmod(item["mode"])
        os.replace(temporary, target)
    emit("rollback_staging_complete", root=str(root), production_actions=False)


def main(argv=None):
    global JOURNAL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall", "rollback"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True, help="disposable staging root, or / for explicit installation only")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, help="reviewed scheduler YAML with alternate bind port")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print plan without creating files or running installers")
    args = parser.parse_args(argv)
    try:
        if not args.root.is_dir():
            raise DeploymentError("create an empty staging root first")
        if not args.root.is_absolute() or args.root.is_symlink() or args.root.resolve() != args.root:
            raise DeploymentError("root must be an absolute canonical non-symlink path")
        if args.action == "install" and args.config is None:
            raise DeploymentError("install requires --config")
        JOURNAL = args.root == Path("/") and not args.dry_run
        settings = load_settings(args.settings)
        globals()[args.action](args, settings, args.root)
        return 0
    except (DeploymentError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        emit("deployment_error", error=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
