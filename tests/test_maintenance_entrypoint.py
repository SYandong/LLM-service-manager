# Generated-By: Codex / gpt-6-astra
"""Operator recovery cannot bypass an already-bound control endpoint."""

import socket
import threading
from types import SimpleNamespace

import pytest

import llmsvc.__main__ as entry
from llmsvc.config import SchedulerConfig
from llmsvc.state import StateSnapshot
from llmsvc.store import IntentStore


@pytest.mark.parametrize('dry_run',[False,True])
def test_operator_recovery_respects_endpoint_exclusion_and_dryrun(monkeypatch,capsys,tmp_path,dry_run):
    calls=[]
    database=tmp_path/"state.sqlite"
    IntentStore(database,action_lock=threading.RLock()).close()
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        config=SchedulerConfig('127.0.0.1',occupied.getsockname()[1],read_only=False,catalog_enabled=True,state_db_path=str(database))
        monkeypatch.setattr(entry,'load_config',lambda path:config)
        monkeypatch.setattr(entry,'build_collector',lambda cfg:lambda:StateSnapshot())
        monkeypatch.setattr(entry,'build_event_relay',lambda cfg:None)
        monkeypatch.setattr(entry,'build_registry',lambda cfg,scheduler:None)
        def build(cfg,scheduler):
            controller=SimpleNamespace(reconcile=lambda:calls.append('observe'))
            scheduler.catalog=SimpleNamespace(transition=controller,retire=lambda:None)
            return scheduler.catalog
        monkeypatch.setattr(entry,'build_catalog',build)
        argv=['llmsvc','--config','unused','--maintenance-recover','observe']
        if dry_run: argv.append('--dry-run')
        monkeypatch.setattr('sys.argv',argv)
        if dry_run:
            assert entry.main()==0
            assert 'reconcile_maintenance' in capsys.readouterr().out
        else:
            with pytest.raises(SystemExit): entry.main()
        assert calls==[]
