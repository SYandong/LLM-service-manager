# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
"""Read-only preflight contract for the future explicit maintenance path."""
import json
import hashlib
import os
import shutil
import sqlite3
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from deploy.maintenance_upgrade import Error, MaintenanceUpgrade
from deploy.upgrade import Upgrade
from llmsvc import __version__ as PACKAGE_VERSION
from llmsvc.state import Pin
from llmsvc.store import IntentStore

ROOT=Path(__file__).parents[1]


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path), mode)
        elif path.is_file():
            result[relative] = ("file", path.read_bytes(), mode)
        elif path.is_dir():
            result[relative] = ("dir", mode)
    return result


def make_bundle(path, version=PACKAGE_VERSION):
    (path/"wheelhouse").mkdir(parents=True)
    cli=b"#!/usr/bin/env python3\nprint("+repr(version).encode()+b")\n"
    (path/"llm").write_bytes(cli)
    wheel=path/("wheelhouse/llmsvc-"+version+"-py3-none-any.whl")
    with zipfile.ZipFile(wheel,"w") as z:
        z.writestr("llmsvc-"+version+".dist-info/METADATA","Metadata-Version: 2.1\nName: llmsvc\nVersion: "+version+"\n")
        z.writestr("llmsvc-"+version+".data/scripts/llm",b"#!python\n"+cli.split(b"\n",1)[1])
    pip=path/"wheelhouse/pip-26.2.1-py3-none-any.whl"; pip.write_bytes(b"pip")
    info={"schema_version":1,"tag":"v"+version.replace("a", "-alpha."),"version":version,"commit":"a"*40,
          "scope":"read_only","app_wheel":"wheelhouse/"+wheel.name,"cli":"llm",
          "bootstrap_pip":"wheelhouse/"+pip.name,"install_wheels":["wheelhouse/"+wheel.name],
          "files":{n:sha(path/n) for n in ("llm","wheelhouse/"+wheel.name,"wheelhouse/"+pip.name)}}
    (path/"deployment.json").write_text(json.dumps(info)); return path


def runtime_fingerprint(path):
    result={}
    for item in sorted(path.rglob("*")):
        if "__pycache__" in item.parts or item.suffix==".pyc" or item==path/"release.json":
            continue
        name=str(item.relative_to(path))
        if item.is_symlink(): result[name]={"link":os.readlink(item)}
        elif item.is_file(): result[name]={"sha256":sha(item)}
    return result


def make_candidate(root, db, bundle_path):
    info=json.loads((bundle_path/"deployment.json").read_text())
    bundle_digest=sha(bundle_path/"deployment.json")
    generation=info["version"]+"-"+bundle_digest[:12]
    staged=root/"shared/releases"/generation; (staged/"venv/bin").mkdir(parents=True)
    (staged/"venv/bin/python").symlink_to(sys.executable)
    shutil.copytree(ROOT/"llmsvc", staged/"llmsvc")
    record={"tag":info["tag"],"version":info["version"],"commit":info["commit"],
            "bundle_sha256":bundle_digest,"generation":generation}
    (staged/"release.json").write_text(json.dumps({**record,"runtime_files":runtime_fingerprint(staged)}))
    config=root/"scheduler.yaml"; config.write_text("listen_host: 127.0.0.1\nlisten_port: 8011\nread_only: false\nstate_db_path: %s\n" % db)
    (root/"prefix").mkdir(); (root/"shared").mkdir(exist_ok=True)
    settings={"prefix":"/prefix","shared_dir":"/shared","python":sys.executable,
              "scheduler_url":"http://127.0.0.1:8011","swap_url":"http://127.0.0.1:8000",
              "trampoline_path":"/trampoline","config_path":"/scheduler.yaml",
              "maintenance_config_path":"/scheduler.yaml","candidate_root":"/shared/releases/"+generation,
              "candidate_commit":"a"*40}
    return staged,config,settings


def test_preflight_uses_actual_candidate_reader_and_no_writes(tmp_path):
    db=tmp_path/"ledger.sqlite"; store=IntentStore(db,action_lock=threading.RLock()); store.put_pin(Pin("kept",4102444800,"owner")); store.close()
    bundle=make_bundle(tmp_path/"bundle"); staged,config,settings=make_candidate(tmp_path,db,bundle)
    before_db=db.read_bytes(); before_config=config.read_bytes()
    before_tree=tree_snapshot(tmp_path)
    result=MaintenanceUpgrade(settings,tmp_path).preflight(bundle)
    assert result["status"]=="preflight" and result["apply_supported"] is False and result["writes"] is False
    assert result["candidate_reader"]["read_only"] is True
    assert result["candidate_reader"]["version"]==PACKAGE_VERSION
    assert str(staged/"llmsvc") in result["candidate_reader"]["origin"]
    assert db.read_bytes()==before_db and config.read_bytes()==before_config
    assert tree_snapshot(tmp_path)==before_tree
    reopened=IntentStore(db,action_lock=threading.RLock(),read_only=True)
    assert [pin.model for pin in reopened.active(4102444700)[0]]==["kept"]; reopened.close()


def test_preflight_rejects_actual_candidate_version_mismatch(tmp_path):
    db=tmp_path/"ledger.sqlite"; store=IntentStore(db,action_lock=threading.RLock()); store.close()
    base, alpha = PACKAGE_VERSION.rsplit("a", 1)
    bundle=make_bundle(tmp_path/"bundle", version=base+"a"+str(int(alpha)+1))
    staged,config,settings=make_candidate(tmp_path,db,bundle)
    before_tree=tree_snapshot(tmp_path)
    with pytest.raises(Error,match="candidate read-only ledger preflight failed"):
        MaintenanceUpgrade(settings,tmp_path).preflight(bundle)
    assert tree_snapshot(tmp_path)==before_tree


def test_preflight_rejects_staged_runtime_drift_before_reader(tmp_path):
    db=tmp_path/"ledger.sqlite"; store=IntentStore(db,action_lock=threading.RLock()); store.close()
    bundle=make_bundle(tmp_path/"bundle"); staged,config,settings=make_candidate(tmp_path,db,bundle)
    before_db=db.read_bytes(); (staged/"llmsvc/__init__.py").write_text("__version__ = "+repr(PACKAGE_VERSION)+"\n")
    with pytest.raises(Error,match="staged candidate runtime manifest is invalid"):
        MaintenanceUpgrade(settings,tmp_path).preflight(bundle)
    assert db.read_bytes()==before_db


def test_preflight_rejects_verified_generation_in_wrong_sibling(tmp_path):
    db=tmp_path/"ledger.sqlite"; store=IntentStore(db,action_lock=threading.RLock()); store.close()
    bundle=make_bundle(tmp_path/"bundle"); staged,config,settings=make_candidate(tmp_path,db,bundle)
    wrong=staged.parent/"other-generation"; shutil.move(str(staged),wrong)
    settings["candidate_root"]="/shared/releases/other-generation"
    before_db=db.read_bytes(); before_tree=tree_snapshot(tmp_path)
    with pytest.raises(Error,match="candidate path does not match verified generation"):
        MaintenanceUpgrade(settings,tmp_path).preflight(bundle)
    assert db.read_bytes()==before_db and tree_snapshot(tmp_path)==before_tree


def test_preflight_rejects_wal_state_without_touching_sidecars(tmp_path):
    db=tmp_path/"ledger.sqlite"; connection=sqlite3.connect(db); connection.execute("PRAGMA journal_mode=WAL"); connection.execute("CREATE TABLE retained (value TEXT)"); connection.commit()
    bundle=make_bundle(tmp_path/"bundle"); staged,config,settings=make_candidate(tmp_path,db,bundle)
    # The helper-created candidate never opens the ledger; the sidecar is an explicit unsupported state.
    before_tree=tree_snapshot(tmp_path)
    try:
        with pytest.raises(Error,match="WAL journal state is unsupported"):
            MaintenanceUpgrade(settings,tmp_path).preflight(bundle)
        assert tree_snapshot(tmp_path)==before_tree
    finally:
        connection.close()
def test_preflight_incompatible_actual_reader_rejects_without_mutation(tmp_path):
    db=tmp_path/"ledger.sqlite"; connection=sqlite3.connect(db); connection.execute("PRAGMA user_version=99"); connection.commit(); connection.close()
    bundle=make_bundle(tmp_path/"bundle"); staged,config,settings=make_candidate(tmp_path,db,bundle)
    before_db=db.read_bytes(); before_config=config.read_bytes(); before_tree=tree_snapshot(tmp_path)
    with pytest.raises(Error,match="candidate read-only ledger preflight failed"):
        MaintenanceUpgrade(settings,tmp_path).preflight(bundle)
    assert db.read_bytes()==before_db and config.read_bytes()==before_config
    assert tree_snapshot(tmp_path)==before_tree


def test_apply_and_rollback_refuse_before_lock_or_staging(tmp_path):
    db=tmp_path/"ledger.sqlite"; store=IntentStore(db,action_lock=threading.RLock()); store.close()
    bundle=make_bundle(tmp_path/"bundle"); staged,config,settings=make_candidate(tmp_path,db,bundle)
    obj=MaintenanceUpgrade(settings,tmp_path)
    before_tree=tree_snapshot(tmp_path)
    result=obj.apply(bundle,dry_run=True)
    assert result["apply_supported"] is False and result["writes"] is False
    assert tree_snapshot(tmp_path)==before_tree
    with pytest.raises(Error,match="UNSUPPORTED"):
        obj.apply(bundle,confirm=True)
    with pytest.raises(Error,match="UNSUPPORTED"):
        obj.rollback("a"*32)
    assert not (tmp_path/"prefix/upgrade.lock").exists()
    assert not list((tmp_path/"prefix").glob("transactions/*"))


def test_unattended_readonly_unit_gate_still_rejects_writable_execstart(tmp_path):
    obj=Upgrade.__new__(Upgrade); obj.unit=tmp_path/"unit"; obj.unit.write_text("ExecStart=/legacy/writable\n"); obj.cfg={"prefix":"/prefix"}
    with pytest.raises(Error,match="expected one explicitly read-only scheduler ExecStart"):
        obj.unit_candidate()
