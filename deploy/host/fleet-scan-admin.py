#!/usr/bin/env python3
# Generated-By: Codex / gpt-6.1-sol
"""Install, roll back or uninstall only the host fleet scanner's files/timer."""

import argparse
import base64
import fcntl
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
UNIT_KEYS = ("Id", "LoadState", "ActiveState", "UnitFileState", "FragmentPath", "DropInPaths", "InvocationID", "Job")
EFFECTS = {
    "install_stop_timer": ["stop", TIMER],
    "install_stop_service": ["stop", SERVICE],
    "install_reload": ["daemon-reload"],
    "install_enable_timer": ["enable", "--now", TIMER],
    "restore_stop_timer": ["disable", "--now", TIMER],
    "restore_stop_service": ["stop", SERVICE],
    "restore_reload": ["daemon-reload"],
    "restore_enable_timer": ["enable", TIMER],
    "restore_start_timer": ["start", TIMER],
}
FILE_STATES = {"pending", "install_submitted", "installed", "restore_submitted", "restored"}


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
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_durable(path):
    path.unlink()
    sync_directory(path.parent)


def systemctl(args):
    result = subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=10, shell=False, check=False,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    # A nonzero exit is an error, never evidence of an inactive/absent unit.
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > 8192:
        raise ValueError("systemctl_result_unknown")
    try:
        return result.stdout.decode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("systemctl_result_unknown") from None


def unit_state(unit):
    keys = UNIT_KEYS + (("MainPID", "ControlPID", "ControlGroup") if unit == SERVICE else ())
    raw = systemctl(["show", unit, "--no-pager", "--all", "--property=" + ",".join(keys)])
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values or key not in keys:
            raise ValueError("systemctl_properties_unknown")
        values[key] = value
    if set(values) != set(keys) or values["Id"] != unit or values["Job"] not in {"", "0"}:
        raise ValueError("systemctl_properties_unknown")
    if values["LoadState"] == "not-found":
        if values["ActiveState"] != "inactive" or values["FragmentPath"] or values["DropInPaths"] or values["UnitFileState"] not in {"", "not-found"}:
            raise ValueError("systemctl_properties_unknown")
    elif values["LoadState"] == "loaded":
        if values["FragmentPath"] != "/etc/systemd/system/" + unit or values["DropInPaths"]:
            raise ValueError("systemctl_unit_identity_changed")
        if values["ActiveState"] not in {"active", "inactive", "failed"} or values["UnitFileState"] not in {"enabled", "disabled", "static", "indirect"}:
            raise ValueError("systemctl_properties_unknown")
    else:
        raise ValueError("systemctl_properties_unknown")
    invocation = values["InvocationID"]
    if invocation and (len(invocation) != 32 or any(char not in "0123456789abcdef" for char in invocation)):
        raise ValueError("systemctl_properties_unknown")
    if values["ActiveState"] == "active" and (not invocation or int(invocation, 16) == 0):
        raise ValueError("systemctl_properties_unknown")
    if unit == SERVICE:
        if any(not values[key].isascii() or not values[key].isdecimal() or int(values[key]) > 2 ** 31 - 1 for key in ("MainPID", "ControlPID")):
            raise ValueError("systemctl_properties_unknown")
        if values["ActiveState"] in {"inactive", "failed"} and (values["MainPID"] != "0" or values["ControlPID"] != "0" or values["ControlGroup"]):
            raise ValueError("systemctl_service_presence_unknown")
    return values


def assert_no_pending_effect(receipt):
    if any(effect["state"] == "submitted" for effect in receipt["effects"].values()):
        raise ValueError("systemctl_action_outcome_unknown")


def effect(receipt_path, receipt, name, binding=None):
    mark = receipt["effects"].get(name)
    if mark:
        if mark["state"] != "acknowledged":
            raise ValueError("systemctl_action_outcome_unknown")
        return
    receipt["effects"][name] = {"state": "submitted", "binding": binding}
    save_receipt(receipt_path, receipt)
    systemctl(EFFECTS[name])
    receipt["effects"][name]["state"] = "acknowledged"
    save_receipt(receipt_path, receipt)


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


def file_record(path):
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("installed_file_changed")
    return {"data": base64.b64encode(path.read_bytes()).decode(), "mode": stat.S_IMODE(path.stat().st_mode)}


def check_files(root, receipt):
    for absolute, state in receipt["files"].items():
        current = file_record(target(root, absolute))
        allowed = []
        if state in {"pending", "install_submitted", "restore_submitted", "restored"}:
            allowed.append(receipt["previous"][absolute])
        if state in {"install_submitted", "installed", "restore_submitted"}:
            allowed.append(receipt["installed"][absolute])
        if current not in allowed:
            raise ValueError("installed_file_changed")


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 2 or receipt.get("phase") not in {"installing", "installed", "restoring", "restored"}:
        raise ValueError("invalid_install_receipt")
    for group in ("previous", "installed", "files"):
        if not isinstance(receipt.get(group), dict) or set(receipt[group]) != set(FILES):
            raise ValueError("invalid_install_receipt")
    for absolute in FILES:
        if receipt["files"][absolute] not in FILE_STATES:
            raise ValueError("invalid_install_receipt")
        for group in ("previous", "installed"):
            record = receipt[group][absolute]
            if record is None and group == "previous":
                continue
            if not isinstance(record, dict) or set(record) != {"data", "mode"} or not isinstance(record["data"], str) or len(record["data"]) > 2 * 1024 * 1024 or isinstance(record["mode"], bool) or not isinstance(record["mode"], int) or not 0 <= record["mode"] <= 0o7777:
                raise ValueError("invalid_install_receipt")
            base64.b64decode(record["data"], validate=True)
    if not isinstance(receipt.get("timer_enabled"), bool) or not isinstance(receipt.get("timer_active"), bool) or not isinstance(receipt.get("effects"), dict):
        raise ValueError("invalid_install_receipt")
    for name, mark in receipt["effects"].items():
        if name not in EFFECTS or not isinstance(mark, dict) or set(mark) != {"state", "binding"} or mark["state"] not in {"submitted", "acknowledged"} or mark["binding"] is not None and not isinstance(mark["binding"], dict):
            raise ValueError("invalid_install_receipt")


def read_receipt(path):
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("invalid_install_receipt")
    receipt = json.loads(path.read_bytes())
    validate_receipt(receipt)
    return receipt


def stop_units(receipt_path, receipt, prefix):
    assert_no_pending_effect(receipt)
    # Read both identities before submitting any stop/disable command.
    timer, service = unit_state(TIMER), unit_state(SERVICE)
    if timer["ActiveState"] == "active" or prefix == "restore" and timer["UnitFileState"] == "enabled":
        timer = unit_state(TIMER)
        effect(receipt_path, receipt, prefix + "_stop_timer", timer)
    timer = unit_state(TIMER)
    if timer["ActiveState"] not in {"inactive", "failed"} or prefix == "restore" and timer["UnitFileState"] == "enabled":
        raise ValueError("scanner_stop_not_confirmed")
    if service["ActiveState"] == "active":
        service = unit_state(SERVICE)
        effect(receipt_path, receipt, prefix + "_stop_service", service)
    service = unit_state(SERVICE)
    if service["ActiveState"] not in {"inactive", "failed"}:
        raise ValueError("scanner_stop_not_confirmed")


def restore(root, receipt_path, receipt, live):
    assert_no_pending_effect(receipt)
    check_files(root, receipt)
    if receipt["phase"] not in {"restoring", "restored"}:
        receipt["phase"] = "restoring"
        save_receipt(receipt_path, receipt)
    if any(state != "restored" for state in receipt["files"].values()):
        if live:
            stop_units(receipt_path, receipt, "restore")
        for absolute in sorted(FILES):
            check_files(root, receipt)
            if receipt["files"][absolute] == "restored":
                continue
            path = target(root, absolute)
            previous = receipt["previous"][absolute]
            receipt["files"][absolute] = "restore_submitted"
            save_receipt(receipt_path, receipt)
            # A previous attempt may already have applied the exact registered
            # bytes/mode or removal. Do not repeat a known completed file effect.
            if file_record(path) != previous:
                if previous is None:
                    remove_durable(path)
                else:
                    write_atomic(path, base64.b64decode(previous["data"], validate=True), previous["mode"])
            receipt["files"][absolute] = "restored"
            save_receipt(receipt_path, receipt)
    if live:
        effect(receipt_path, receipt, "restore_reload")
        timer = unit_state(TIMER)
        unit_state(SERVICE)
        if receipt["timer_enabled"]:
            effect(receipt_path, receipt, "restore_enable_timer", timer)
        if receipt["timer_active"]:
            effect(receipt_path, receipt, "restore_start_timer", unit_state(TIMER))
        timer = unit_state(TIMER)
        if (timer["UnitFileState"] == "enabled") != receipt["timer_enabled"] or (timer["ActiveState"] == "active") != receipt["timer_active"]:
            raise ValueError("timer_restore_not_confirmed")
    receipt["phase"] = "restored"
    save_receipt(receipt_path, receipt)


def administer_locked(action, root, source, config_path, dry_run):
    live = root == Path("/")
    receipt_path = target(root, RECEIPT)
    receipt = None
    if receipt_path.exists():
        receipt = read_receipt(receipt_path)
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
            previous[absolute] = file_record(path)
        timer = unit_state(TIMER) if live else None
        if live:
            unit_state(SERVICE)
        receipt = {"schema_version": 2, "phase": "installing", "previous": previous,
                   "installed": {absolute: {"data": base64.b64encode(data).decode(), "mode": mode} for absolute, (data, mode) in payloads.items()},
                   "files": {absolute: "pending" for absolute in FILES}, "effects": {},
                   "timer_enabled": timer["UnitFileState"] == "enabled" if live else False,
                   "timer_active": timer["ActiveState"] == "active" if live else False}
        # Persist recovery bytes before changing any installed file or timer.
        save_receipt(receipt_path, receipt)
        try:
            if live:
                stop_units(receipt_path, receipt, "install")
            for absolute, (data, mode) in payloads.items():
                check_files(root, receipt)
                receipt["files"][absolute] = "install_submitted"
                save_receipt(receipt_path, receipt)
                write_atomic(target(root, absolute), data, mode)
                receipt["files"][absolute] = "installed"
                save_receipt(receipt_path, receipt)
            output_directory.mkdir(parents=True, exist_ok=True)
            if live:
                effect(receipt_path, receipt, "install_reload")
                unit_state(SERVICE)
                effect(receipt_path, receipt, "install_enable_timer", unit_state(TIMER))
                timer = unit_state(TIMER)
                if timer["UnitFileState"] != "enabled" or timer["ActiveState"] != "active":
                    raise ValueError("timer_install_not_confirmed")
            receipt["phase"] = "installed"
            save_receipt(receipt_path, receipt)
        except (OSError, ValueError, subprocess.SubprocessError):
            # In-memory progress may be ahead of a failed checkpoint. Recover
            # only from the on-disk receipt; a lost action ACK stays submitted.
            receipt = read_receipt(receipt_path)
            restore(root, receipt_path, receipt, live)
            remove_durable(receipt_path)
            raise
        return {"event": "fleet_install_complete", "action": action, "activate_timer": live}
    if receipt is None:
        raise ValueError("installation_receipt_missing")
    check_files(root, receipt)
    if dry_run:
        return {"event": "fleet_install_dry_run", "action": action, "files": list(FILES), "activate_timer": live}
    restore(root, receipt_path, receipt, live)
    remove_durable(receipt_path)
    return {"event": "fleet_install_complete", "action": action, "activate_timer": live}


def administer(action, root, source, config_path=None, dry_run=False):
    if not root.is_absolute() or root != root.resolve() or root.is_symlink():
        raise ValueError("invalid_install_root")
    if dry_run:
        return administer_locked(action, root, source, config_path, True)
    lock = target(root, RECEIPT + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid_install_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("installation_busy") from None
        return administer_locked(action, root, source, config_path, False)
    finally:
        os.close(fd)


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
