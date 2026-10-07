#!/usr/bin/env python3
# Generated-By: Codex / gpt-6.1-sol
"""Install or roll back the dedicated host observer, preserving all observations."""

import argparse
import fcntl
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import syslog
import tempfile

SOURCE = Path(__file__).resolve().parent
ROOT_UID = 0
SERVICE = "llmsvc-fleet-observer.service"
ARCHIVE = "llmsvc-fleet-observer-archive.service"
TIMER = "llmsvc-fleet-observer-archive.timer"
UNITS = (SERVICE, ARCHIVE, TIMER)
PATH_KEYS = {"source_dir", "python", "unit_dir", "config_path", "archive_config_path",
             "state_dir", "archive_dir", "receipt_dir"}
PROPERTIES = ("Id", "LoadState", "ActiveState", "UnitFileState", "FragmentPath", "DropInPaths",
              "InvocationID")
EFFECTS = {
    "install_reload": ["daemon-reload"],
    "enable_observer": ["enable", "--now", SERVICE],
    "enable_timer": ["enable", "--now", TIMER],
    "disable_timer": ["disable", "--now", TIMER],
    "stop_archive": ["stop", ARCHIVE],
    "disable_observer": ["disable", "--now", SERVICE],
    "rollback_reload": ["daemon-reload"],
}


def fail(code):
    raise ValueError(code)


def absolute(value):
    if (not isinstance(value, str) or len(value) > 512 or value == "/"
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts):
        fail("invalid_path")
    return str(Path(value))


def target(root, logical):
    path = root / absolute(logical).lstrip("/")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            fail("symlink_path")
    return path


def load_json(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        fail("invalid_json_file")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail("duplicate_json_key")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        fail("invalid_json_file")
    return value


def settings_from(path):
    settings = load_json(Path(path))
    if set(settings) - PATH_KEYS - {"user", "uid", "_comments", "_generated_by"}:
        fail("unknown_setting")
    if not PATH_KEYS | {"user", "uid"} <= settings.keys():
        fail("missing_setting")
    settings = {key: settings[key] for key in PATH_KEYS | {"user", "uid"}}
    for key in PATH_KEYS:
        settings[key] = absolute(settings[key])
    if (not isinstance(settings["user"], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", settings["user"])
            or type(settings["uid"]) is not int or not 0 < settings["uid"] < 2 ** 32 - 1):
        fail("invalid_account")
    return settings


def account(root, settings):
    if root == Path("/"):
        entry = pwd.getpwnam(settings["user"])
        uid, gid, shell = entry.pw_uid, entry.pw_gid, entry.pw_shell
    else:
        passwd = target(root, "/etc/passwd")
        if not passwd.is_file() or passwd.stat().st_size > 65536:
            fail("account_unknown")
        matches = [line.split(":") for line in passwd.read_text().splitlines()
                   if line.split(":", 1)[0] == settings["user"]]
        if len(matches) != 1 or len(matches[0]) != 7:
            fail("account_unknown")
        uid, gid, shell = int(matches[0][2]), int(matches[0][3]), matches[0][6]
    if (uid != settings["uid"] or uid <= 0 or gid <= 0
            or shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"}):
        fail("account_not_unprivileged")
    return uid, gid


def fingerprint(path, limit=16 * 1024 * 1024):
    if not path.exists():
        return None
    info = path.stat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        fail("unsafe_file")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid}


def source_binding(root, settings):
    owner = ROOT_UID if root == Path("/") else os.geteuid()
    uid, gid = account(root, settings) if root == Path("/") else (os.geteuid(), os.getegid())

    def readable(info, directory):
        shift = 6 if info.st_uid == uid else 3 if info.st_gid == gid else 0
        if not (info.st_mode >> shift) & (1 if directory else 4):
            fail("source_not_readable")

    source = target(root, settings["source_dir"])
    package = source / "llmsvc"
    for required in (package / "__init__.py", package / "fleet/observer.py", package / "fleet/archive.py"):
        if not required.is_file():
            fail("source_incomplete")
    for parent in (source, *source.parents):
        if parent == root.parent:
            break
        info = parent.stat()
        if not parent.is_dir() or info.st_uid != owner or info.st_mode & 0o022:
            fail("source_not_root_owned")
        readable(info, True)
    binding = {}
    size = 0
    for path in chain((source, package), package.rglob("*")):
        if len(binding) >= 500 or path.is_symlink() or "__pycache__" in path.parts:
            fail("source_unsafe")
        info = path.stat()
        if info.st_uid != owner or info.st_mode & 0o022:
            fail("source_not_root_owned")
        readable(info, path.is_dir())
        if path.is_dir():
            item = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        else:
            item = fingerprint(path, 1024 * 1024)
            size += info.st_size
        binding[str(path.relative_to(source))] = item
        if size > 10 * 1024 * 1024:
            fail("source_size")
    python = Path(settings["python"]).resolve(strict=True)
    info = python.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not os.access(python, os.X_OK):
        fail("python_not_root_owned")
    binding["python"] = dict(path=str(python), **fingerprint(python))
    return binding


def validate_layout(settings, config):
    state, archive = Path(settings["state_dir"]), Path(settings["archive_dir"])
    database = Path(absolute(config.get("fleet_db_path")))
    exports = [Path(absolute(config.get(key))) for key in ("fleet_snapshot_path", "ip_containers_path")]
    if database == state or not database.is_relative_to(state):
        fail("database_outside_state")
    protected = [Path(settings[key]) for key in PATH_KEYS - {"state_dir", "archive_dir"}]
    for directory in (state, archive):
        for other in [archive if directory == state else state, *protected, *(path.parent for path in exports)]:
            if directory.is_relative_to(other) or other.is_relative_to(directory):
                fail("path_overlap")
    files = [Path(settings[key]) for key in ("config_path", "archive_config_path")]
    if (any(left.is_relative_to(right) or right.is_relative_to(left)
            for left, right in [(files[0], files[1])]) or any(path.is_relative_to(Path(settings[key]))
                                 for path in files for key in ("source_dir", "receipt_dir", "unit_dir"))):
        fail("path_overlap")
    directories = [Path(settings[key]) for key in ("source_dir", "unit_dir", "receipt_dir")]
    if any(left.is_relative_to(right) or right.is_relative_to(left)
           for index, left in enumerate(directories) for right in directories[index + 1:]):
        fail("path_overlap")
    for value in settings.values():
        if isinstance(value, str) and value.startswith(("/home/", "/root/", "/run/user/")):
            fail("path_hidden_by_sandbox")
    return database, exports


def prepare(root, settings, config_path, gid, source):
    config = load_json(Path(config_path))
    database, exports = validate_layout(settings, config)
    for logical in (database, *exports):
        target(root, str(logical))
    result = subprocess.run([source["python"]["path"], "-E", "-s", "-B", "-m", "llmsvc.fleet.observer",
                             "--config", str(Path(config_path).absolute()), "--check-config"],
                            cwd=target(root, settings["source_dir"]), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, shell=False,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 65536:
        fail("observer_config_invalid")
    values = dict(settings, gid=gid, python=source["python"]["path"],
                  export_dirs=" ".join(sorted({str(path.parent) for path in exports})))
    archive = {"database": config["fleet_db_path"], "directory": settings["archive_dir"],
               "hourly_retention_days": config.get("fleet_hourly_retention_days", 180)}
    payloads = {settings["config_path"]: json.dumps(config, sort_keys=True).encode() + b"\n",
                settings["archive_config_path"]: json.dumps(archive, sort_keys=True).encode() + b"\n"}
    for unit in UNITS:
        payloads[str(Path(settings["unit_dir"]) / unit)] = (SOURCE / unit).read_text().format(**values).encode()
    return config, payloads


def control(args):
    result = subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10, shell=False, check=False,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if result.returncode != 0 or len(result.stdout) > 8192:
        fail("systemctl_unknown")
    return result.stdout.decode("utf-8", "strict")


def unit_state(unit, settings):
    keys = PROPERTIES + (() if unit == TIMER else ("MainPID", "ControlPID", "ControlGroup"))
    values = {}
    for line in control(["show", unit, "--all", "--no-pager", "--property=" + ",".join(keys)]).splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in keys or key in values:
            fail("unit_unknown")
        values[key] = value
    if set(values) != set(keys) or values["Id"] != unit or values["DropInPaths"]:
        fail("unit_identity_unknown")
    if values["LoadState"] == "not-found":
        if (values["ActiveState"] != "inactive" or values["FragmentPath"]
                or values["UnitFileState"] not in {"", "not-found"}):
            fail("unit_unknown")
    elif (values["LoadState"] != "loaded" or values["FragmentPath"] != str(Path(settings["unit_dir"]) / unit)
          or values["ActiveState"] not in {"active", "activating", "inactive", "failed"}
          or values["UnitFileState"] not in {"enabled", "disabled", "static"}):
        fail("unit_identity_unknown")
    invocation = values["InvocationID"]
    if (invocation and re.fullmatch(r"[a-f0-9]{32}", invocation) is None
            or values["ActiveState"] in {"active", "activating"} and (not invocation or int(invocation, 16) == 0)):
        fail("unit_process_unknown")
    if unit != TIMER:
        if not all(values[key].isascii() and values[key].isdecimal() for key in ("MainPID", "ControlPID")):
            fail("unit_process_unknown")
        if (values["ActiveState"] in {"inactive", "failed"}
                and (values["MainPID"] != "0" or values["ControlPID"] != "0" or values["ControlGroup"])):
            fail("unit_process_unknown")
    return values


def active(state):
    return state["ActiveState"] in {"active", "activating"}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(path, data, mode, uid, gid):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fleet-observer-install-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_receipt(path, receipt, owner):
    write(path, json.dumps(receipt, sort_keys=True).encode() + b"\n", 0o600, owner, os.getegid())


def check_files(root, receipt, allow_missing=False):
    for logical, expected in receipt["files"].items():
        current = fingerprint(target(root, logical))
        if current != expected and not (current is None and allow_missing):
            fail("owned_file_changed")


def private_directory(path, uid, gid, create=False):
    if not path.exists():
        if not create:
            return
        path.mkdir(parents=True, mode=0o700)
        os.chown(path, uid, gid)
        sync_directory(path.parent)
    info = path.stat()
    if not path.is_dir() or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != uid or info.st_gid != gid:
        fail("private_directory_unknown")


def effect(root, path, receipt, owner, name):
    if root != Path("/") or name in receipt["effects"]:
        return
    check_files(root, receipt, receipt["phase"] == "rolling_back")
    if source_binding(root, receipt["settings"]) != receipt["source"]:
        fail("source_changed")
    binding = {unit: unit_state(unit, receipt["settings"]) for unit in UNITS}
    args = EFFECTS[name]
    if args[0] in {"disable", "stop"} and binding[args[-1]]["LoadState"] == "not-found":
        return
    receipt["effects"][name] = {"state": "submitted", "units": binding}
    save_receipt(path, receipt, owner)
    control(args)
    receipt["effects"][name]["state"] = "done"
    save_receipt(path, receipt, owner)


def administer(action, settings_path, config_path=None, root=Path("/"), dry_run=False):
    root = Path(absolute(str(root))) if Path(root) != Path("/") else Path("/")
    settings = settings_from(settings_path)
    uid, gid = account(root, settings)
    owner = ROOT_UID if root == Path("/") else os.geteuid()
    data_uid, data_gid = (uid, gid) if root == Path("/") else (os.geteuid(), os.getegid())
    path = target(root, str(Path(settings["receipt_dir"]) / "receipt.json"))
    for logical in settings.values():
        if isinstance(logical, str) and logical.startswith("/") and logical != settings["python"]:
            target(root, logical)
    source = source_binding(root, settings)
    if path.exists() and (path.stat().st_uid != owner or stat.S_IMODE(path.stat().st_mode) != 0o600):
        fail("receipt_unknown")
    receipt = load_json(path) if path.exists() else None
    config, payloads = prepare(root, settings, config_path, gid, source) if action == "install" else (None, None)
    modes = {logical: 0o640 if logical in {settings["config_path"], settings["archive_config_path"]} else 0o644
             for logical in payloads or ()}
    wanted = {logical: {"sha256": hashlib.sha256(data).hexdigest(), "mode": modes[logical], "uid": owner,
                        "gid": gid if root == Path("/") and modes[logical] == 0o640 else os.getegid()}
              for logical, data in (payloads or {}).items()}
    if receipt:
        expected_paths = {settings["config_path"], settings["archive_config_path"],
                          *(str(Path(settings["unit_dir"]) / unit) for unit in UNITS)}
        if (receipt.get("schema_version") != 1 or receipt.get("settings") != settings
                or receipt.get("phase") not in {"prepared", "installed", "rolling_back", "rolled_back"}
                or set(receipt.get("effects", {})) - EFFECTS.keys()
                or set(receipt.get("files", {})) != expected_paths):
            fail("receipt_unknown")
        if any(mark.get("state") != "done" for mark in receipt["effects"].values()):
            fail("action_outcome_unknown")
        if receipt.get("source") != source:
            fail("source_changed")
        check_files(root, receipt, receipt["phase"] != "installed")
        config = receipt["config"] if action == "rollback" else config
        if action == "install" and (wanted != receipt["files"] or receipt["phase"] == "rolling_back"):
            fail("installed_artifact_changed")
    elif action == "rollback":
        return {"event": "fleet_observer_deploy", "action": action, "dry_run": dry_run, "installed": False}
    else:
        if any(target(root, logical).exists() for logical in payloads):
            fail("foreign_file")
    validate_layout(settings, config)
    states = {unit: unit_state(unit, settings) for unit in UNITS} if root == Path("/") else {}
    if not receipt and any(state["LoadState"] != "not-found" for state in states.values()):
        fail("foreign_unit")
    for key in ("state_dir", "archive_dir"):
        private_directory(target(root, settings[key]), data_uid, data_gid)
    private_directory(path.parent, owner, os.getegid())
    result = {"event": "fleet_observer_deploy", "action": action, "dry_run": dry_run,
              "activate": root == Path("/") and action == "install", "preserve_data": True}
    if dry_run:
        return result
    private_directory(path.parent, owner, os.getegid(), create=True)
    lock = os.open(path.with_name("install.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600:
            fail("install_lock_unknown")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (load_json(path) if path.exists() else None) != receipt:
            fail("receipt_changed")
        if receipt:
            check_files(root, receipt, receipt["phase"] != "installed")
        elif any(target(root, logical).exists() for logical in payloads):
            fail("foreign_file")
        if action == "install":
            if receipt is None or receipt["phase"] == "rolled_back":
                if receipt:
                    history = path.parent / (hashlib.sha256(path.read_bytes()).hexdigest() + ".json")
                    if not history.exists():
                        write(history, path.read_bytes(), 0o600, owner, os.getegid())
                receipt = {"schema_version": 1, "phase": "prepared", "settings": settings, "config": config,
                           "source": source, "files": wanted, "effects": {}}
                save_receipt(path, receipt, owner)
            for key in ("state_dir", "archive_dir"):
                private_directory(target(root, settings[key]), data_uid, data_gid, create=True)
            for logical, data in payloads.items():
                destination = target(root, logical)
                if not destination.exists():
                    expected = wanted[logical]
                    write(destination, data, expected["mode"], expected["uid"], expected["gid"])
            check_files(root, receipt)
            for name in ("install_reload", "enable_observer", "enable_timer"):
                effect(root, path, receipt, owner, name)
            if root == Path("/"):
                observer, timer = unit_state(SERVICE, settings), unit_state(TIMER, settings)
                if any(not active(state) or state["UnitFileState"] != "enabled" for state in (observer, timer)):
                    fail("activation_unknown")
            receipt["phase"] = "installed"
        elif receipt["phase"] != "rolled_back":
            receipt["phase"] = "rolling_back"
            save_receipt(path, receipt, owner)
            for name in ("disable_timer", "stop_archive", "disable_observer"):
                effect(root, path, receipt, owner, name)
            if root == Path("/"):
                states = [unit_state(unit, settings) for unit in UNITS]
                if any(active(state) or state["UnitFileState"] == "enabled" for state in states):
                    fail("owned_unit_still_active")
            check_files(root, receipt, allow_missing=True)
            for logical in receipt["files"]:
                destination = target(root, logical)
                if destination.exists():
                    destination.unlink()
                    sync_directory(destination.parent)
            # Reload after deleting fragments; only this final reload permits missing files.
            if root == Path("/") and "rollback_reload" not in receipt["effects"]:
                receipt["effects"]["rollback_reload"] = {"state": "submitted"}
                save_receipt(path, receipt, owner)
                control(EFFECTS["rollback_reload"])
                receipt["effects"]["rollback_reload"]["state"] = "done"
            receipt["phase"] = "rolled_back"
        save_receipt(path, receipt, owner)
        return result
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "rollback"))
    parser.add_argument("--settings", required=True)
    parser.add_argument("--config")
    parser.add_argument("--root", default="/")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "install" and not args.config:
            fail("config_required")
        if args.root == "/" and not args.dry_run and os.geteuid() != 0:
            fail("root_required")
        result = administer(args.action, args.settings, args.config, Path(args.root), args.dry_run)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]+", str(error)) else "deploy_failed"
        result = {"event": "fleet_observer_deploy", "action": args.action, "ok": False, "error": code}
    print(json.dumps(result, sort_keys=True))
    if args.root == "/" and not args.dry_run:
        syslog.openlog("llmsvc-fleet-observer-deploy")
        syslog.syslog(syslog.LOG_INFO, json.dumps(result, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
