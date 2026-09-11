#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Read-only preflight for a future explicit writable scheduler replacement."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import manage
from deploy.upgrade import Upgrade, bundle, sha

Error = manage.DeploymentError


class MaintenanceUpgrade(Upgrade):
    """Inspect an already staged candidate; never perform a cutover."""

    def __init__(self, settings, root, *, command=None):
        super().__init__(settings, root, command=command)
        path = settings.get("maintenance_config_path")
        if not isinstance(path, str) or not path:
            raise Error("maintenance_config_path is required")
        self.maintenance_config_path = manage.inside(root, path)
        config_path = settings.get("config_path")
        if not isinstance(config_path, str) or not config_path:
            raise Error("config_path is required")
        self.config = manage.inside(root, config_path)
        if self.maintenance_config_path != self.config:
            raise Error("maintenance config must be the configured scheduler config")
        self.candidate_root = manage.inside(root, settings.get("candidate_root", "/"))
        if self.candidate_root == root:
            raise Error("candidate_root must be a staged generation")

    def _staged(self, payload):
        if self.candidate_root.is_symlink() or not self.candidate_root.is_dir():
            raise Error("staged candidate is unavailable")
        release = self.candidate_root / "release.json"
        if release.is_symlink() or not release.is_file():
            raise Error("staged candidate release record is unavailable")
        try:
            record = self.verify_generation(self.candidate_root)
        except (Error, OSError, TypeError, ValueError, KeyError) as exc:
            raise Error("staged candidate runtime manifest is invalid") from exc
        expected = {
            "version": payload["version"],
            "commit": payload["commit"],
            "tag": payload["tag"],
            "bundle_sha256": payload["bundle_sha256"],
            "generation": payload["generation"],
        }
        if any(record.get(key) != value for key, value in expected.items()):
            raise Error("staged candidate does not match verified bundle")
        expected = self.cfg.get("candidate_commit")
        if expected is not None and record["commit"] != expected:
            raise Error("staged candidate commit differs")
        python = self.candidate_root / "venv/bin/python"
        if not python.is_file() or not python.resolve().is_file() or not os.access(python.resolve(), os.X_OK):
            raise Error("staged candidate interpreter is unavailable")
        return record, python

    def ledger_snapshot(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise Error("same ledger is unavailable")
        sidecars = tuple(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm"))
        if any(sidecar.exists() for sidecar in sidecars):
            raise Error("same ledger WAL journal state is unsupported for no-write preflight")
        try:
            header = path.open("rb").read(20)
        except OSError as exc:
            raise Error("same ledger compatibility read failed") from exc
        if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
            raise Error("same ledger header is unsupported")
        # SQLite records rollback (1) or WAL (2) in the file header.  Reject
        # WAL before opening so a read-only connection cannot create -shm.
        if header[18] != 1 or header[19] != 1:
            raise Error("same ledger journal mode is unsupported for no-write preflight")
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if journal_mode == "wal" or any(sidecar.exists() for sidecar in sidecars):
                    raise Error("same ledger WAL journal state is unsupported for no-write preflight")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = [row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                rows = {}; counts = {}
                for name in tables:
                    quoted = '"' + name.replace('"', '""') + '"'
                    values = db.execute("SELECT * FROM " + quoted + " ORDER BY rowid").fetchall()
                    counts[name] = len(values)
                    rows[name] = sha(json.dumps(values, sort_keys=True, default=str).encode())
                return {"path": str(path), "user_version": version, "tables": tables,
                        "counts": counts, "rows_sha256": rows}
            finally:
                db.close()
        except (OSError, sqlite3.Error) as exc:
            raise Error("same ledger compatibility read failed") from exc

    def _candidate_read(self, python, config, ledger, expected_version):
        code = """import json,sys,threading
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from dataclasses import replace
from llmsvc.config import load_config
from llmsvc.store import IntentStore
c=load_config(sys.argv[1])
if c.state_db_path != sys.argv[2]: raise ValueError('ledger path differs')
if c.read_only is not False: raise ValueError('intended config is not writable')
import llmsvc
origin=Path(llmsvc.__file__).resolve()
candidate=Path(sys.argv[4]).resolve()
if candidate not in origin.parents: raise ValueError('candidate import escaped staged runtime')
if llmsvc.__version__ != sys.argv[3]: raise ValueError('candidate package version differs')
ro=replace(c, read_only=True)
s=IntentStore(ro.state_db_path, action_lock=threading.RLock(), read_only=True)
try:
 print(json.dumps({'version':llmsvc.__version__,'origin':str(origin),'read_only':ro.read_only,'ledger':{'user_version':s._db.execute('PRAGMA user_version').fetchone()[0]}}))
finally: s.close()
"""
        result = subprocess.run([str(python), "-I", "-B", "-c", code, str(config), ledger["path"],
                                 expected_version, str(self.candidate_root)],
                                capture_output=True, text=True, timeout=min(30, self.command_timeout),
                                cwd=str(self.candidate_root), env={"PATH": os.environ.get("PATH", "")})
        if result.returncode:
            raise Error("candidate read-only ledger preflight failed")
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise Error("candidate preflight output is invalid") from exc
        if (value.get("read_only") is not True
                or value.get("version") != expected_version
                or not str(value.get("origin", "")).startswith(str(self.candidate_root) + "/")
                or value.get("ledger", {}).get("user_version") != ledger["user_version"]):
            raise Error("candidate ledger compatibility is unsupported")
        return value

    def preflight(self, directory):
        payload = bundle(directory)
        record, python = self._staged(payload)
        config_bytes = self.maintenance_config_path.read_bytes()
        config_sha = sha(config_bytes)
        source_root = Path(__file__).resolve().parents[1]
        current = json.loads(self.run([self.cfg["python"], "-I", "-B", "-c", CONFIG,
                                       str(self.config), str(source_root)]).stdout)
        state_db_path = current.get("state_db_path")
        if not isinstance(state_db_path, str) or not state_db_path.startswith("/"):
            raise Error("configured scheduler ledger path is unavailable")
        ledger = self.ledger_snapshot(state_db_path)
        candidate = self._candidate_read(python, self.maintenance_config_path, ledger, record["version"])
        if sha(self.maintenance_config_path.read_bytes()) != config_sha:
            raise Error("maintenance config changed during preflight")
        return {"status": "preflight", "read_only": True, "apply_supported": False,
                "candidate": {"version": record["version"], "commit": record["commit"], "python": str(python)},
                "bundle": {"version": payload["version"], "commit": payload["commit"]},
                "config_sha256": config_sha, "ledger": ledger, "candidate_reader": candidate,
                "external_effect_settlement": "UNKNOWN", "writes": False}

    def apply(self, directory, *, confirm=False, dry_run=False):
        if not dry_run:
            raise Error("UNSUPPORTED: writable maintenance cutover is not implemented")
        return self.preflight(directory)

    def rollback(self, token, *, dry_run=False):
        if not dry_run:
            raise Error("UNSUPPORTED: maintenance rollback is not implemented")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise Error("invalid transaction id")
        return {"status": "preflight_only", "dry_run": True, "apply_supported": False,
                "transaction": token, "writes": False}


CONFIG = '''import json,sys
sys.path.insert(0, sys.argv[2])
from llmsvc.config import load_config
c=load_config(sys.argv[1])
print(json.dumps({'read_only':c.read_only,'state_db_path':c.state_db_path}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "apply", "rollback"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--transaction")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        upgrade = MaintenanceUpgrade(json.loads(args.settings.read_text()), args.root)
        if args.action in ("preflight", "apply"):
            if args.bundle is None:
                raise Error("preflight/apply requires --bundle")
            result = upgrade.preflight(args.bundle) if args.action == "preflight" else upgrade.apply(args.bundle, dry_run=args.dry_run)
        else:
            result = upgrade.rollback(args.transaction or "", dry_run=args.dry_run)
        print(json.dumps(result, allow_nan=False)); return 0
    except (Error, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
