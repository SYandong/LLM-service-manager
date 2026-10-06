#!/usr/bin/env python3
# Generated-By: Codex / gpt-6.1-sol
"""Install or roll back only the independent local fleet archive timer."""
import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import syslog
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
SCRIPT = SOURCE.parents[1] / "llmsvc/fleet/archive.py"
TIMER, SERVICE = "llmsvc-fleet-archive.timer", "llmsvc-fleet-archive.service"
RECEIPT = "/var/lib/llmsvc-fleet-archive-install/receipt.json"
FILES = {"/usr/local/libexec/llmsvc-fleet-archive.py": 0o755, "/etc/llmsvc/fleet-archive.json": 0o600,
         "/etc/systemd/system/" + SERVICE: 0o644, "/etc/systemd/system/" + TIMER: 0o644}
PROPERTIES = ("Id", "LoadState", "ActiveState", "UnitFileState", "FragmentPath", "DropInPaths")
ERROR_CODES = {"invalid_path", "symlink_path", "invalid_json_file", "systemctl_unknown", "unit_unknown", "unit_identity_unknown",
               "config_size", "config_field", "config_path", "unit_path", "config_retention", "database_in_writable_archive",
               "owned_file_changed", "receipt_unknown", "action_outcome_unknown", "installed_artifact_changed", "foreign_file", "foreign_unit",
               "timer_changed", "service_changed", "archive_directory_unknown", "receipt_directory_unknown", "install_lock_unknown",
               "receipt_changed", "timer_activation_unknown", "archive_still_active", "invalid_root", "root_required"}


def target(root, logical):
    path = Path(logical)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError("invalid_path")
    result = root / str(path).lstrip("/")
    for parent in (result, *result.parents):
        if parent == root.parent: break
        if parent.is_symlink(): raise ValueError("symlink_path")
    return result


def write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fleet-archive-install-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def read_json(path):
    if not path.is_file() or path.stat().st_size > 65536: raise ValueError("invalid_json_file")
    return json.loads(path.read_bytes())


def control(args):
    result = subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10, shell=False, check=False,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if result.returncode != 0 or len(result.stdout) > 8192: raise ValueError("systemctl_unknown")
    return result.stdout.decode("utf-8", "strict")


def unit_state(unit):
    values = {}
    for line in control(["show", unit, "--all", "--no-pager", "--property=" + ",".join(PROPERTIES)]).splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in PROPERTIES or key in values: raise ValueError("unit_unknown")
        values[key] = value
    if set(values) != set(PROPERTIES) or values["Id"] != unit or values["DropInPaths"]: raise ValueError("unit_unknown")
    if values["LoadState"] == "not-found":
        if values["ActiveState"] != "inactive" or values["FragmentPath"] or values["UnitFileState"] not in {"", "not-found"}:
            raise ValueError("unit_unknown")
        return {"exists": False, "enabled": False, "active": False}
    if (values["LoadState"] != "loaded" or values["FragmentPath"] != "/etc/systemd/system/" + unit
            or values["UnitFileState"] not in {"enabled", "disabled", "static"}
            or values["ActiveState"] not in ({"active", "inactive", "failed", "activating"} if unit == SERVICE else {"active", "inactive", "failed"})):
        raise ValueError("unit_identity_unknown")
    return {"exists": True, "enabled": values["UnitFileState"] == "enabled", "active": values["ActiveState"] in {"active", "activating"}}


def prepare(config_path=None):
    raw = Path(config_path or SOURCE / "fleet-archive.example.json").read_bytes()
    if len(raw) > 65536: raise ValueError("config_size")
    cfg = json.loads(raw)
    if set(cfg) - {"database", "directory", "hourly_retention_days", "_comments", "_generated_by"}: raise ValueError("config_field")
    for key in ("database", "directory"):
        if not isinstance(cfg.get(key), str) or not cfg[key].startswith("/") or ".." in Path(cfg[key]).parts or cfg[key] == "/":
            raise ValueError("config_path")
        if any(char.isspace() or char in '\\%"\'' or ord(char) < 32 for char in cfg[key]): raise ValueError("unit_path")
    if type(cfg.get("hourly_retention_days")) is not int or not 1 <= cfg["hourly_retention_days"] <= 3650: raise ValueError("config_retention")
    if Path(cfg["database"]).is_relative_to(Path(cfg["directory"])): raise ValueError("database_in_writable_archive")
    script = SCRIPT.read_bytes()
    compile(script, str(SCRIPT), "exec")
    service = (SOURCE / SERVICE).read_bytes().replace(b"ReadOnlyPaths=/var/lib/llmsvc", ("ReadOnlyPaths=" + str(Path(cfg["database"]).parent)).encode())
    service = service.replace(b"ReadWritePaths=/var/lib/llmsvc/fleet-sessions", ("ReadWritePaths=" + cfg["directory"]).encode())
    return cfg, dict(zip(FILES, (script, raw, service, (SOURCE / TIMER).read_bytes())))


def fingerprint(path):
    if not path.exists(): return None
    if not path.is_file() or path.stat().st_size > 1024 * 1024: raise ValueError("owned_file_changed")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": stat.S_IMODE(path.stat().st_mode)}


def save_receipt(path, receipt):
    write(path, json.dumps(receipt, sort_keys=True).encode() + b"\n", 0o600)


def check_files(root, files, allow_missing=False):
    for logical, expected in files.items():
        current = fingerprint(target(root, logical))
        if current != expected and not (current is None and allow_missing): raise ValueError("owned_file_changed")


def effect(root, path, receipt, name, args):
    if root != Path("/") or receipt["effects"].get(name) == "done": return
    receipt["effects"][name] = "submitted"
    save_receipt(path, receipt)
    control(args)
    receipt["effects"][name] = "done"
    save_receipt(path, receipt)


def administer(action, root=Path("/"), config_path=None, dry_run=False):
    root = Path(root)
    path = target(root, RECEIPT)
    cfg, payloads = prepare(config_path) if action == "install" else (None, None)
    receipt = read_json(path) if path.exists() else None
    wanted = None if payloads is None else {name: {"sha256": hashlib.sha256(data).hexdigest(), "mode": FILES[name]} for name, data in payloads.items()}
    if receipt:
        if (receipt.get("schema_version") != 1 or receipt.get("phase") not in {"prepared", "installed", "rolling_back"}
                or set(receipt.get("files", {})) != set(FILES) or not isinstance(receipt.get("effects"), dict)
                or receipt.get("timer_before") != {"enabled": False, "active": False}):
            raise ValueError("receipt_unknown")
        if any(value not in {"submitted", "done"} for value in receipt["effects"].values()): raise ValueError("receipt_unknown")
        if "submitted" in receipt["effects"].values(): raise ValueError("action_outcome_unknown")
        if action == "install" and (wanted != receipt["files"] or receipt["phase"] == "rolling_back"): raise ValueError("installed_artifact_changed")
        check_files(root, receipt["files"], receipt["phase"] != "installed")
    else:
        if action == "rollback": return {"event": "fleet_archive_install", "action": action, "installed": False, "dry_run": dry_run}
        if any(target(root, logical).exists() for logical in FILES): raise ValueError("foreign_file")
    timer, service = (unit_state(TIMER), unit_state(SERVICE)) if root == Path("/") else (dict(exists=False, enabled=False, active=False),) * 2
    if not receipt and (timer["exists"] or service["exists"]): raise ValueError("foreign_unit")
    if receipt and receipt["phase"] == "rolling_back":
        if receipt["effects"].get("disable_timer") == "done" and (timer["active"] or timer["enabled"]): raise ValueError("timer_changed")
        if receipt["effects"].get("stop_service") == "done" and service["active"]: raise ValueError("service_changed")
    directory = target(root, cfg["directory"] if cfg else receipt["directory"])
    if directory.exists() and (not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700 or directory.stat().st_uid != os.geteuid()):
        raise ValueError("archive_directory_unknown")
    result = {"event": "fleet_archive_install", "action": action, "dry_run": dry_run, "activate_timer": root == Path("/") and action == "install"}
    if dry_run: return result
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700 or path.parent.stat().st_uid != os.geteuid(): raise ValueError("receipt_directory_unknown")
    lock = os.open(path.with_name("install.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600: raise ValueError("install_lock_unknown")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A competing install that finished after preflight must be re-read first.
        if (read_json(path) if path.exists() else None) != receipt: raise ValueError("receipt_changed")
        if receipt:
            check_files(root, receipt["files"], receipt["phase"] != "installed")
        elif any(target(root, logical).exists() for logical in FILES): raise ValueError("foreign_file")
        if action == "install":
            if receipt is None:
                receipt = {"schema_version": 1, "phase": "prepared", "files": wanted, "directory": cfg["directory"],
                           "timer_before": {key: timer[key] for key in ("enabled", "active")}, "effects": {}}
                save_receipt(path, receipt)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for logical, data in payloads.items():
                destination = target(root, logical)
                if not destination.exists(): write(destination, data, FILES[logical])
            check_files(root, receipt["files"])
            effect(root, path, receipt, "install_reload", ["daemon-reload"])
            effect(root, path, receipt, "enable_timer", ["enable", "--now", TIMER])
            if root == Path("/"):
                state = unit_state(TIMER)
                if not state["enabled"] or not state["active"]: raise ValueError("timer_activation_unknown")
            receipt["phase"] = "installed"
            save_receipt(path, receipt)
        else:
            receipt["phase"] = "rolling_back"
            save_receipt(path, receipt)
            if timer["exists"]: effect(root, path, receipt, "disable_timer", ["disable", "--now", TIMER])
            if service["exists"]: effect(root, path, receipt, "stop_service", ["stop", SERVICE])
            if root == Path("/") and (unit_state(TIMER)["active"] or unit_state(TIMER)["enabled"] or unit_state(SERVICE)["active"]):
                raise ValueError("archive_still_active")
            check_files(root, receipt["files"], allow_missing=True)
            for logical in FILES:
                destination = target(root, logical)
                if destination.exists():
                    destination.unlink()
                    sync_directory(destination.parent)
            effect(root, path, receipt, "rollback_reload", ["daemon-reload"])
            path.unlink()
            sync_directory(path.parent)
        return result
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "rollback"))
    parser.add_argument("--config")
    parser.add_argument("--root", default="/")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not Path(args.root).is_absolute() or ".." in Path(args.root).parts: raise ValueError("invalid_root")
        if args.root == "/" and not args.dry_run and os.geteuid() != 0: raise ValueError("root_required")
        result = administer(args.action, Path(args.root), args.config, args.dry_run)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in ERROR_CODES else "install_failed"
        result = {"event": "fleet_archive_install", "action": args.action, "ok": False, "error": code}
    print(json.dumps(result, sort_keys=True))
    if args.root == "/" and not args.dry_run:
        syslog.openlog("llmsvc-fleet-archive-install"); syslog.syslog(syslog.LOG_INFO, json.dumps(result, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
