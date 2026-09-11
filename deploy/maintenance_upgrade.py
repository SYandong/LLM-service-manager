#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Explicit, operator-invoked writable scheduler replacement.

The unattended pull/upgrade path remains read-only.  This entrypoint reuses its
staging, transaction, pointer and byte guards, and adds the old-unit absence
boundary required before a writable candidate is started.
"""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import manage
from deploy.upgrade import Upgrade, atomic, bundle, json_write, launcher, point, sha

Error = manage.DeploymentError


class MaintenanceUpgrade(Upgrade):
    def __init__(self, settings, root, *, command=None):
        super().__init__(settings, root, command=command)
        path = settings.get("maintenance_config_path")
        if not isinstance(path, str) or not path:
            raise Error("maintenance_config_path is required")
        self.maintenance_config_path = manage.inside(root, path)
        self.proc_root = Path(settings.get("proc_root", "/proc"))
        if self.root == Path("/") and self.proc_root != Path("/proc"):
            raise Error("live maintenance must use /proc identity source")
        self.expected_read_only = False
        self.old_identity = None

    def _properties(self, unit):
        raw = self.run(["systemctl", "show", unit,
                        "-p", "ActiveState,MainPID,InvocationID,ControlGroup,FragmentPath,DropInPaths"], timeout=5).stdout
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def _proc_identity(self, pid):
        root = self.proc_root / str(pid)
        try:
            stat = (root / "stat").read_text()
            fields = stat.rsplit(") ", 1)[1].split()
            if len(fields) <= 19 or fields[0] in ("Z", "X", "x"):
                raise Error("scheduler process identity is unavailable")
            start_ticks = fields[19]
            if not re.fullmatch(r"[0-9]+", start_ticks):
                raise Error("scheduler process start identity is invalid")
            exe = os.readlink(root / "exe")
            cmdline = (root / "cmdline").read_bytes()
            cgroup = (root / "cgroup").read_text()
            if not exe or not cmdline or not cgroup:
                raise Error("scheduler process identity is incomplete")
            return {"pid": pid, "start_ticks": start_ticks, "exe": exe,
                    "cmdline_sha256": sha(cmdline), "cgroup": cgroup}
        except (OSError, UnicodeError, IndexError) as exc:
            raise Error("scheduler process identity is unavailable") from exc

    def capture_old_identity(self):
        unit = Path(self.site["unit_path"]).name
        props = self._properties(unit)
        if (props.get("ActiveState") != "active" or props.get("FragmentPath") != self.site["unit_path"]
                or props.get("DropInPaths") not in (None, "") or props.get("MainPID", "0") == "0"
                or not re.fullmatch(r"[0-9a-f-]{8,}", props.get("InvocationID", ""))
                or not props.get("ControlGroup")):
            raise Error("old scheduler identity is unknown or not active")
        first = {"unit": unit, "pid": int(props["MainPID"]),
                 "invocation_id": props["InvocationID"], "control_group": props["ControlGroup"],
                 "fragment_path": props["FragmentPath"]}
        process = self._proc_identity(first["pid"])
        if (first["control_group"] not in process["cgroup"]
                and process["cgroup"].strip() not in ("", "0::" + first["control_group"])):
            raise Error("scheduler process cgroup does not match unit")
        second = self._properties(unit)
        if (second.get("ActiveState") != "active" or second.get("MainPID") != str(first["pid"])
                or second.get("InvocationID") != first["invocation_id"]
                or second.get("ControlGroup") != first["control_group"]
                or second.get("FragmentPath") != first["fragment_path"]):
            raise Error("scheduler identity changed during capture")
        return {**first, "start_ticks": process["start_ticks"], "exe": process["exe"],
                "cmdline_sha256": process["cmdline_sha256"], "proc_cgroup": process["cgroup"]}

    def prove_old_absent(self, identity):
        props = self._properties(identity["unit"])
        if props.get("ActiveState") not in ("inactive", "failed") or props.get("MainPID") != "0":
            raise Error("old scheduler unit is still active")
        if props.get("ControlGroup", ""):
            raise Error("old scheduler control group is still present")
        if (self.proc_root / str(identity["pid"])).exists():
            raise Error("old scheduler process is still present")
        return {"unit_absent": True, "old_pid": identity["pid"],
                "old_invocation_id": identity["invocation_id"]}

    def stop_old(self, identity):
        current = self.capture_old_identity()
        if current != identity:
            raise Error("scheduler identity changed before stop")
        self.run(["systemctl", "stop", identity["unit"]], timeout=45)
        return self.prove_old_absent(identity)

    def ledger_snapshot(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise Error("same ledger is unavailable")
        uri = path.resolve().as_uri() + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = [row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                rows = {}
                counts = {}
                pending = []
                terminal = {"complete", "committed", "rolled_back", "aborted", "default_confirmed"}
                for name in tables:
                    quoted = "\"" + name.replace('"', '""') + "\""
                    values = db.execute("SELECT * FROM " + quoted + " ORDER BY rowid").fetchall()
                    counts[name] = len(values)
                    rows[name] = sha(json.dumps(values, sort_keys=True, default=str).encode())
                    columns = [row[1] for row in db.execute("PRAGMA table_info(" + quoted + ")")]
                    if "record" in columns:
                        record_index = columns.index("record")
                        for value in values:
                            try:
                                stage = json.loads(value[record_index]).get("stage")
                            except (TypeError, ValueError, IndexError):
                                stage = None
                            if stage not in terminal:
                                pending.append(name)
                return {"path": str(path), "user_version": version, "tables": tables,
                        "counts": counts, "rows_sha256": rows, "pending_tables": sorted(set(pending))}
            finally:
                db.close()
        except (OSError, sqlite3.Error) as exc:
            raise Error("same ledger compatibility read failed") from exc

    def maintenance_unit_candidate(self):
        old = self.unit.read_text()
        lines = old.splitlines()
        indexes = [i for i, line in enumerate(lines) if line.startswith("ExecStart=")]
        if len(indexes) != 1:
            raise Error("maintenance requires one scheduler ExecStart")
        lines[indexes[0]] = "ExecStart=" + self.cfg["prefix"] + "/bin/scheduler-run"
        return ("\n".join(lines) + "\n").encode()

    def preflight_candidate(self, generation, ledger):
        python = str(generation / "venv/bin/python")
        config = json.loads(self.run([python, "-c", CONFIG, str(self.maintenance_config_path)]).stdout)
        if config.get("read_only") is not False:
            raise Error("maintenance candidate config must be explicitly writable")
        if config.get("state_db_path") != str(ledger["path"]):
            raise Error("maintenance candidate ledger path differs")
        # --dry-run forces read-only in __main__; this is the compatibility read,
        # not the writable startup. It must not migrate or create the ledger.
        once = json.loads(self.run([python, "-m", "llmsvc", "--config",
                                    str(self.maintenance_config_path), "--dry-run", "--once"],
                                   timeout=min(30, self.command_timeout)).stdout)
        if once.get("read_only") is not True or once.get("errors"):
            raise Error("candidate compatibility read was not read-only")
        current = self.ledger_snapshot(ledger["path"])
        unsupported = current.get("pending_tables", [])
        if unsupported:
            raise Error("UNSUPPORTED: pending external-effect fences in " + ",".join(unsupported))
        return {"config": config, "once": once, "ledger": current}

    def health(self, version=None, expected_identity=None):
        if self.root != Path("/"):
            return version or self.run([self.cfg["python"], str(self.cli), "--version"], timeout=5).stdout.strip()
        end = time.monotonic() + self.health_timeout
        while time.monotonic() < end:
            try:
                code = """import json,sys,urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k): return None
o=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
r=o.open(sys.argv[1],timeout=2)
raw=r.read(4194305)
assert len(raw)<=4194304
print(raw.decode())
"""
                state = json.loads(self.run([self.cfg["python"], "-c", code,
                                             self.cfg["scheduler_url"].rstrip("/") + "/v1/state"], timeout=3).stdout)
                if state.get("read_only") is not False or state.get("sampled_at") is None or state.get("errors"):
                    raise Error("writable candidate is not ready")
                if expected_identity is not None and self.capture_old_identity() != expected_identity:
                    raise Error("candidate identity changed during health")
                break
            except (OSError, ValueError, Error, subprocess.SubprocessError):
                time.sleep(.2)
        else:
            raise Error("maintenance candidate health deadline")
        result = self.run([self.cfg["python"], str(self.cli), "--version"], timeout=5).stdout.strip()
        if version and result != version:
            raise Error("shared CLI health version mismatch")
        return result

    def old_reader_compatible(self, record, transaction):
        previous = record.get("previous_pointer")
        if not previous:
            raise Error("unsupported rollback: no previous generation")
        generation = self.shared / previous
        old = self.verify_generation(generation)
        python = str(generation / "venv/bin/python")
        before = record["before"].get(str(self.config))
        if not before or not before.get("exists"):
            raise Error("unsupported rollback: previous config bytes unavailable")
        old_config = transaction / "rollback-old-config"
        atomic(old_config, base64.b64decode(before["data"]), before.get("mode", 0o600))
        self.run([python, "-m", "llmsvc", "--config", str(old_config), "--dry-run", "--check-config"], timeout=min(30, self.command_timeout))
        once = json.loads(self.run([python, "-m", "llmsvc", "--config", str(old_config), "--dry-run", "--once"],
                                   timeout=min(30, self.command_timeout)).stdout)
        if once.get("read_only") is not True:
            raise Error("unsupported rollback: old reader compatibility is not read-only")
        return {"generation": previous, "version": old.get("version"), "once": once}

    def stop_candidate_before_rollback(self, record):
        identity = record.get("candidate_identity")
        if not isinstance(identity, dict):
            raise Error("unsupported rollback: candidate identity is unavailable")
        return self.stop_old(identity)

    def rollback_after_failure(self, record, transaction):
        """Require candidate absence and old-reader compatibility before restore."""
        try:
            record["candidate_absent"] = self.stop_candidate_before_rollback(record)
            record["old_reader"] = self.old_reader_compatible(record, transaction)
        except Error as rollback_error:
            record["status"] = "unsupported_rollback"
            record["rollback_error"] = str(rollback_error)
            json_write(transaction / "transaction.json", record)
            json_write(self.state_path, {"transaction": record["transaction"], "status": "unsupported_rollback"})
            raise Error("unsupported rollback: " + str(rollback_error)) from rollback_error
        return self.restore(record, transaction)

    def apply(self, directory, *, confirm=False, dry_run=False):
        descriptor = os.open(self.prefix / "maintenance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Error("maintenance operation already in progress") from exc
            return self._apply_locked(directory, confirm=confirm, dry_run=dry_run)

    def _apply_locked(self, directory, *, confirm=False, dry_run=False):
        if not confirm and not dry_run:
            raise Error("explicit maintenance confirmation required")
        payload = bundle(directory)
        self.load()
        current_config = json.loads(self.run([self.cfg["python"], "-c", CONFIG,
                                              str(self.config)]).stdout)
        ledger = self.ledger_snapshot(current_config["state_db_path"])
        if dry_run:
            self.guard()
            self.maintenance_unit_candidate()
            return {"dry_run": True, "ledger": ledger, "unit": str(self.unit),
                    "read_only_updater_unchanged": True}
        maintenance_bytes = self.maintenance_config_path.read_bytes()
        maintenance_hash = sha(maintenance_bytes)
        generation = self.prepare(directory, payload)
        candidate = self.preflight_candidate(generation, ledger)
        if sha(self.maintenance_config_path.read_bytes()) != maintenance_hash:
            raise Error("maintenance config changed during candidate preflight")
        self.guard()
        old_identity = self.capture_old_identity()
        token = __import__("uuid").uuid4().hex
        transaction = manage.inside(self.root, self.cfg["prefix"] + "/transactions/" + token)
        transaction.mkdir(mode=0o700, parents=True)
        planned = {str(self.unit): self.maintenance_unit_candidate(),
                   str(self.config): maintenance_bytes,
                   str(self.cli): launcher(self.cfg["python"], self.cfg["scheduler_url"]),
                   str(self.cli_run): launcher(self.cfg["python"], self.cfg["scheduler_url"]),
                   str(self.scheduler_run): launcher(self.cfg["python"], self.cfg["scheduler_url"],
                                                    scheduler=True, shared=self.cfg["shared_dir"],
                                                    config=self.site["config_path"], read_only=False)}
        record = {"schema_version": 1, "transaction": token, "status": "preparing", "root": str(self.root),
                  "upgrade_settings": self.cfg, "before": self.before, "previous_pointer": self.previous_pointer,
                  "new_pointer": "releases/" + payload["generation"], "protected": self.protected,
                  "planned_sha256": {path: sha(data) for path, data in planned.items()},
                  "version": payload["version"], "tag": payload["tag"], "bundle_sha256": payload["bundle_sha256"],
                  "old_identity": old_identity, "ledger_before": ledger, "candidate": candidate}
        json_write(transaction / "transaction.json", record); json_write(self.state_path, {"transaction": token, "status": "preparing"})
        try:
            record["status"] = "stopping_old"; json_write(transaction / "transaction.json", record); json_write(self.state_path, {"transaction": token, "status": "stopping_old"})
            self.stop_old(old_identity); record["status"] = "old_absent"; self.guard(); record["ledger_after_old_absent"] = self.ledger_snapshot(ledger["path"])
            if sha(self.maintenance_config_path.read_bytes()) != maintenance_hash:
                raise Error("maintenance config changed after old process exit")
            self.preflight_candidate(generation, ledger)
            record["status"] = "switching"; json_write(transaction / "transaction.json", record); json_write(self.state_path, {"transaction": token, "status": "switching"})
            point(self.pointer, record["new_pointer"])
            for path, data in planned.items():
                mode = 0o644 if Path(path) in (self.unit, self.config) else 0o755
                if Path(path) == self.config:
                    mode = 0o600
                atomic(Path(path), data, mode)
            self.restart(); self.protection_guard()
            record["candidate_identity"] = self.capture_old_identity()
            json_write(transaction / "transaction.json", record)
            self.health(payload["version"], expected_identity=record["candidate_identity"])
            new = json.loads(json.dumps(self.manifest)); new["schema_version"] = 2
            new["files"] = [{"path": self.site[key], "sha256": sha(manage.inside(self.root, self.site[key]).read_bytes())} for key in ("unit_path", "config_path", "cli_path")]
            new["managed_extra_files"] = {str(path): sha(path.read_bytes()) for path in (self.cli_run, self.scheduler_run)}
            new["upgrade"] = {"transaction": token, "generation": payload["generation"], "version": payload["version"], "tag": payload["tag"], "bundle_sha256": payload["bundle_sha256"]}
            encoded = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode(); record["planned_sha256"][str(self.manifest_path)] = sha(encoded)
            json_write(transaction / "transaction.json", record); atomic(self.manifest_path, encoded)
            record["status"] = "committed"; json_write(transaction / "transaction.json", record); json_write(self.state_path, {"transaction": token, "status": "committed"})
            return {"status": "committed", "transaction": token, "generation": payload["generation"]}
        except BaseException:
            if record["status"] in ("preparing", "stopping_old", "old_absent"):
                record["status"] = "unsupported_maintenance_phase"; json_write(transaction / "transaction.json", record); json_write(self.state_path, {"transaction": token, "status": "unsupported_maintenance_phase"})
            else:
                self.rollback_after_failure(record, transaction)
            raise

    def rollback(self, token, *, dry_run=False):
        if dry_run:
            return super().rollback(token, dry_run=True)
        original_restore = self.restore
        def guarded_restore(record, transaction):
            try:
                candidate_absent = self.stop_candidate_before_rollback(record)
                old_reader = self.old_reader_compatible(record, transaction)
            except Error as exc:
                record["status"] = "unsupported_rollback"
                record["rollback_error"] = str(exc)
                json_write(transaction / "transaction.json", record)
                json_write(self.state_path, {"transaction": record["transaction"], "status": "unsupported_rollback"})
                raise Error("unsupported rollback: " + str(exc)) from exc
            record["candidate_absent"] = candidate_absent
            record["old_reader"] = old_reader
            return original_restore(record, transaction)
        self.restore = guarded_restore
        return super().rollback(token, dry_run=False)


CONFIG = '''import json,sys
from llmsvc.config import load_config
c=load_config(sys.argv[1])
print(json.dumps({'read_only':c.read_only,'state_db_path':c.state_db_path}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "rollback"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--transaction")
    parser.add_argument("--confirm-maintenance", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = json.loads(args.settings.read_text())
        upgrade = MaintenanceUpgrade(settings, args.root)
        if args.action == "apply":
            if args.bundle is None:
                raise Error("apply requires --bundle")
            result = upgrade.apply(args.bundle, confirm=args.confirm_maintenance, dry_run=args.dry_run)
        else:
            result = upgrade.rollback(args.transaction or "", dry_run=args.dry_run)
        print(json.dumps(result, allow_nan=False))
        return 0
    except (Error, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
