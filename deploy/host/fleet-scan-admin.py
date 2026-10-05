#!/usr/bin/env python3
# Generated-By: Codex / gpt-6.1-sol
"""Install, roll back or uninstall only the host fleet scanner's files/timer."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


FILES = {
    "/usr/local/lib/llmsvc/llmsvc-fleet-scan.py": ("llmsvc-fleet-scan.py", 0o755),
    "/etc/llmsvc/fleet-scan.json": ("fleet-scan.example.json", 0o644),
    "/etc/systemd/system/llmsvc-fleet-scan.service": ("llmsvc-fleet-scan.service", 0o644),
    "/etc/systemd/system/llmsvc-fleet-scan.timer": ("llmsvc-fleet-scan.timer", 0o644),
}
TIMER = "llmsvc-fleet-scan.timer"
SERVICE = "llmsvc-fleet-scan.service"
RECEIPT = "/var/lib/llmsvc-fleet-install/receipt.json"


def target(root, absolute):
    if not absolute.startswith("/") or ".." in Path(absolute).parts:
        raise ValueError("invalid_install_path")
    path = root / absolute.lstrip("/")
    # Never follow an existing symlink in an install path.
    for parent in [path, *path.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError("symlink_install_path")
    return path


def write_atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fleet-install-")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def systemctl(args, check=True):
    result = subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10, shell=False, check=False)
    if check and result.returncode:
        raise ValueError("systemctl_failed")
    return result.returncode == 0


def scanner_module(source):
    spec = importlib.util.spec_from_file_location("fleet_install_scanner", source / "llmsvc-fleet-scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # compile/exec avoids writing __pycache__ even when called with --dry-run
    # by an interpreter that was not launched with -B.
    exec(compile((source / "llmsvc-fleet-scan.py").read_bytes(), str(source / "llmsvc-fleet-scan.py"), "exec"), module.__dict__)
    return module


def prepare(root, source, config_path):
    config_file = target(root, "/etc/llmsvc/fleet-scan.json")
    config = scanner_module(source).load_config(config_path or (config_file if config_file.exists() else source / "fleet-scan.example.json"))
    payloads = {}
    for absolute, (name, mode) in FILES.items():
        target(root, absolute)
        data = (source / name).read_bytes()
        if name.endswith(".json"):
            # Preserve the existing operator's valid configuration by default.
            if config_path:
                data = Path(config_path).read_bytes()
            elif config_file.exists():
                data = config_file.read_bytes()
        elif name.endswith(".timer"):
            interval = config["sample_interval_seconds"]
            data = data.replace(b"OnUnitActiveSec=60s", f"OnUnitActiveSec={interval:g}s".encode())
        elif name.endswith(".service"):
            # The interpreter path is configuration-owned, not a shell fragment.
            python = config["python_path"]
            if any(char.isspace() or char in '%"\\' for char in python):
                raise ValueError("invalid_service_python_path")
            data = data.replace(b"ExecStart=/usr/bin/python3", ("ExecStart=" + python).encode())
        payloads[absolute] = (data, mode)
    if root != Path("/"):
        # Isolated installation is staged, never activated and never scans /proc.
        # Runtime paths remain host paths so the staged tree is relocatable.
        target(root, config["output_path"])
    return payloads, config


def save_receipt(path, receipt):
    write_atomic(path, json.dumps(receipt, sort_keys=True).encode(), 0o600)


def restore(root, receipt, live):
    if live:
        disabled = systemctl(["disable", "--now", TIMER], check=False)
        stopped = systemctl(["stop", SERVICE], check=False)
        # A first/partially installed unit may be absent. An active unit after
        # a failed stop remains a rollback failure with the receipt retained.
        if not disabled and systemctl(["is-active", "--quiet", TIMER], check=False) or not stopped and systemctl(["is-active", "--quiet", SERVICE], check=False):
            raise ValueError("scanner_stop_failed")
    for absolute, previous in receipt["previous"].items():
        path = target(root, absolute)
        if previous is None:
            if path.exists():
                path.unlink()
        else:
            write_atomic(path, base64.b64decode(previous["data"], validate=True), previous["mode"])
    if live:
        systemctl(["daemon-reload"])
        if receipt["timer_enabled"]:
            systemctl(["enable", TIMER])
        if receipt["timer_active"]:
            systemctl(["start", TIMER])


def administer(action, root, source, config_path=None, dry_run=False):
    if not root.is_absolute() or root != root.resolve() or root.is_symlink():
        raise ValueError("invalid_install_root")
    live = root == Path("/")
    receipt_path = target(root, RECEIPT)
    receipt = None
    if receipt_path.exists():
        if receipt_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("invalid_install_receipt")
        receipt = json.loads(receipt_path.read_bytes())
        if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or set(receipt.get("previous", {})) != set(FILES) or set(receipt.get("installed", {})) != set(FILES):
            raise ValueError("invalid_install_receipt")
        for absolute, previous in receipt["previous"].items():
            if previous is not None and (not isinstance(previous, dict) or set(previous) != {"data", "mode"} or not isinstance(previous["data"], str) or len(previous["data"]) > 2 * 1024 * 1024 or not isinstance(previous["mode"], int) or not 0 <= previous["mode"] <= 0o7777):
                raise ValueError("invalid_install_receipt")
            digest = receipt["installed"][absolute]
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("invalid_install_receipt")
        if not isinstance(receipt.get("timer_enabled"), bool) or not isinstance(receipt.get("timer_active"), bool):
            raise ValueError("invalid_install_receipt")
    if action == "install":
        payloads, config = prepare(root, source, config_path)
        if receipt is not None:
            raise ValueError("already_installed_use_rollback_first")
        output_directory = target(root, config["output_path"]).parent
        if dry_run:
            return {"event": "fleet_install_dry_run", "action": action, "files": list(FILES), "activate_timer": live}
        previous = {}
        backup_bytes = 0
        for absolute in FILES:
            path = target(root, absolute)
            if path.exists():
                if not path.is_file():
                    raise ValueError("invalid_previous_file")
                backup_bytes += path.stat().st_size
                if backup_bytes > 1024 * 1024:
                    raise ValueError("previous_files_too_large")
            previous[absolute] = {"data": base64.b64encode(path.read_bytes()).decode(), "mode": stat.S_IMODE(path.stat().st_mode)} if path.exists() else None
        receipt = {"schema_version": 1, "previous": previous,
                   "installed": {absolute: hashlib.sha256(data).hexdigest() for absolute, (data, _) in payloads.items()},
                   "timer_enabled": systemctl(["is-enabled", "--quiet", TIMER], check=False) if live else False,
                   "timer_active": systemctl(["is-active", "--quiet", TIMER], check=False) if live else False}
        # Persist recovery bytes before changing any installed file or timer.
        save_receipt(receipt_path, receipt)
        try:
            if live and receipt["timer_active"]:
                systemctl(["stop", TIMER])
            if live:
                stopped = systemctl(["stop", SERVICE], check=False)
                if not stopped and systemctl(["is-active", "--quiet", SERVICE], check=False):
                    raise ValueError("scanner_stop_failed")
            for absolute, (data, mode) in payloads.items():
                write_atomic(target(root, absolute), data, mode)
            output_directory.mkdir(parents=True, exist_ok=True)
            if live:
                systemctl(["daemon-reload"])
                systemctl(["enable", "--now", TIMER])
        except (OSError, ValueError, subprocess.SubprocessError):
            restore(root, receipt, live)
            receipt_path.unlink()
            raise
        return {"event": "fleet_install_complete", "action": action, "activate_timer": live}
    if receipt is None:
        raise ValueError("installation_receipt_missing")
    # Refuse to overwrite changes made after installation, including config edits.
    # The operator can save those files before using this bounded rollback path.
    for absolute, digest in receipt["installed"].items():
        path = target(root, absolute)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("installed_file_changed")
    if dry_run:
        return {"event": "fleet_install_dry_run", "action": action, "files": list(FILES), "activate_timer": live}
    restore(root, receipt, live)
    receipt_path.unlink()
    return {"event": "fleet_install_complete", "action": action, "activate_timer": live}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall", "rollback"])
    parser.add_argument("--root", type=Path, default=Path("/"), help="stage under an isolated absolute root; no systemctl calls")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--config", type=Path, help="validated JSON configuration to install")
    parser.add_argument("--dry-run", action="store_true", help="read and report only; create/write nothing")
    args = parser.parse_args(argv)
    try:
        result = administer(args.action, args.root, args.source, args.config, args.dry_run)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"event": "fleet_install_failed", "action": args.action, "error": "install_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
