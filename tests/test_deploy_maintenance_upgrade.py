# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Explicit maintenance replacement gates over real temporary SQLite."""

import base64
import fcntl
import json
import os
import subprocess
import sqlite3
from pathlib import Path
import threading
import time

import pytest

from deploy.maintenance_upgrade import Error, MaintenanceUpgrade
from deploy.upgrade import Upgrade
from deploy.upgrade import sha
from llmsvc.state import Pin
from llmsvc.store import IntentStore


def _ledger(path, *models):
    store = IntentStore(path, action_lock=threading.RLock())
    for model in models:
        store.put_pin(Pin(model, 4102444800, "fixture"))
    store.close()


def _maintenance(root, config_path, shared):
    obj = MaintenanceUpgrade.__new__(MaintenanceUpgrade)
    obj.root = root
    obj.config = config_path
    obj.maintenance_config_path = config_path
    obj.shared = shared
    obj.state_path = root / "upgrade-state.json"
    obj.command_timeout = 30
    obj.proc_root = root / "proc"
    obj.verify_generation = lambda path: {"version": "0.1.0a13"}
    return obj


def _record(root, config_path, transaction):
    raw = config_path.read_bytes()
    return {
        "transaction": transaction,
        "status": "switching",
        "previous_pointer": "releases/old",
        "before": {str(config_path): {"exists": True, "data": base64.b64encode(raw).decode(),
                                        "mode": 0o600, "sha256": sha(raw)}},
    }


def test_old_reader_reopens_current_ledger_after_candidate_write(tmp_path):
    db = tmp_path / "ledger.sqlite"
    _ledger(db, "before")
    config = tmp_path / "scheduler.yaml"
    config.write_text("read_only: false\nstate_db_path: %s\n" % db)
    shared = tmp_path / "shared"; (shared / "releases" / "old").mkdir(parents=True)
    transaction = tmp_path / "transaction"; transaction.mkdir()
    obj = _maintenance(tmp_path, config, shared)
    obj.run = lambda argv, **kwargs: __import__("subprocess").CompletedProcess(argv, 0,
        '{"read_only": true}' if "--once" in argv else "")
    record = _record(tmp_path, config, "tx")
    store = IntentStore(db, action_lock=threading.RLock())
    store.put_pin(Pin("candidate", 4102444800, "fixture"))
    store.close()
    result = obj.old_reader_compatible(record, transaction)
    assert result["generation"] == "releases/old"
    reopened = IntentStore(db, action_lock=threading.RLock(), read_only=True)
    assert {pin.model for pin in reopened.active(4102444700)[0]} == {"before", "candidate"}
    reopened.close()


def test_incompatible_old_reader_blocks_rollback_without_discarding_candidate_write(tmp_path):
    db = tmp_path / "ledger.sqlite"
    _ledger(db, "old")
    config = tmp_path / "scheduler.yaml"
    config.write_text("read_only: false\nstate_db_path: %s\n" % db)
    shared = tmp_path / "shared"; (shared / "releases" / "old").mkdir(parents=True)
    transaction = tmp_path / "transaction"; transaction.mkdir()
    obj = _maintenance(tmp_path, config, shared)
    obj.stop_candidate_before_rollback = lambda record: {"unit_absent": True}
    obj.restore = lambda *args: pytest.fail("rollback must stay unsupported")
    def incompatible(argv, **kwargs):
        if "--once" in argv:
            raise Error("schema checkpoint incompatible")
        return __import__("subprocess").CompletedProcess(argv, 0, "")
    obj.run = incompatible
    record = _record(tmp_path, config, "tx")
    store = IntentStore(db, action_lock=threading.RLock())
    store.put_pin(Pin("candidate-write-before-health-failure", 4102444800, "fixture"))
    store.close()
    with pytest.raises(Error, match="unsupported rollback"):
        obj.rollback_after_failure(record, transaction)
    state = json.loads((tmp_path / "upgrade-state.json").read_text())
    assert state["status"] == "unsupported_rollback"
    reopened = IntentStore(db, action_lock=threading.RLock(), read_only=True)
    assert {pin.model for pin in reopened.active(4102444700)[0]} == {"old", "candidate-write-before-health-failure"}
    reopened.close()


def test_unattended_readonly_unit_gate_still_rejects_writable_execstart(tmp_path):
    obj = Upgrade.__new__(Upgrade)
    obj.unit = tmp_path / "llmsvc-scheduler.service"
    obj.unit.write_text("[Service]\nExecStart=/opt/legacy/scheduler --config /etc/llmsvc/scheduler.yaml\n")
    obj.cfg = {"prefix": "/opt/llmsvc-scheduler"}
    with pytest.raises(Error, match="expected one explicitly read-only scheduler ExecStart"):
        obj.unit_candidate()


def test_maintenance_apply_uses_nonblocking_shared_upgrade_lock(tmp_path):
    obj = MaintenanceUpgrade.__new__(MaintenanceUpgrade)
    obj.prefix = tmp_path / "prefix"; obj.prefix.mkdir()
    obj._apply_locked = lambda *args, **kwargs: pytest.fail("lock conflict must stop before apply")
    lock_path = obj.prefix / "upgrade.lock"
    with lock_path.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(Error, match="already in progress"):
            obj.apply(tmp_path, confirm=True)


def test_maintenance_dry_run_and_missing_confirmation_do_not_create_lock(tmp_path):
    obj = MaintenanceUpgrade.__new__(MaintenanceUpgrade)
    obj.prefix = tmp_path / "prefix"; obj.prefix.mkdir()
    obj._apply_locked = lambda *args, **kwargs: {"dry_run": True}
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    with pytest.raises(Error, match="explicit maintenance confirmation"):
        obj.apply(tmp_path, confirm=False)
    assert not (obj.prefix / "upgrade.lock").exists()
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


def test_process_identity_capture_uses_proc_start_and_stability_checks(tmp_path):
    obj = MaintenanceUpgrade.__new__(MaintenanceUpgrade)
    obj.root = tmp_path; obj.proc_root = Path("/proc")
    obj.site = {"unit_path": "/etc/systemd/system/llmsvc-scheduler.service"}
    pid = os.getpid()
    cgroup = (Path("/proc") / str(pid) / "cgroup").read_text()
    props = {"ActiveState": "active", "MainPID": str(pid), "InvocationID": "a" * 32,
             "ControlGroup": cgroup.split("::", 1)[-1].strip(),
             "FragmentPath": obj.site["unit_path"], "DropInPaths": ""}
    obj._properties = lambda unit: dict(props)
    identity = obj.capture_old_identity()
    assert identity["pid"] == pid and identity["start_ticks"].isdigit()
    changed = dict(props); changed["MainPID"] = str(pid + 1)
    sequence = iter((props, changed))
    obj._properties = lambda unit: next(sequence)
    with pytest.raises(Error, match="identity changed"):
        obj.capture_old_identity()


def test_pending_ledger_fence_is_unsupported_before_candidate_admission(tmp_path):
    db = tmp_path / "ledger.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE llmsvc_maintenance (transaction_id TEXT, record TEXT)")
    connection.execute("INSERT INTO llmsvc_maintenance VALUES (?, ?)", ("tx", json.dumps({"stage": "submitted"})))
    connection.execute("PRAGMA user_version=6"); connection.commit(); connection.close()
    config = tmp_path / "scheduler.yaml"; config.write_text("read_only: false\nstate_db_path: %s\n" % db)
    obj = _maintenance(tmp_path, config, tmp_path / "shared")
    obj.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0,
        '{"read_only": true}' if "--once" in argv else '{"read_only": false,"state_db_path":"%s"}' % db)
    with pytest.raises(Error, match="UNSUPPORTED: pending external-effect fences"):
        obj.preflight_candidate(tmp_path / "generation", {"path": str(db)})
