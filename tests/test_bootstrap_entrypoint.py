# Generated-By: Codex / gpt-6-astra
"""No side effects from preview or an occupied bootstrap control endpoint."""
import socket
import threading
from types import SimpleNamespace

import pytest

import llmsvc.__main__ as entry
from llmsvc.config import SchedulerConfig
from llmsvc.state import StateSnapshot
from llmsvc.store import IntentStore


def settings():
    return {'model':'default','util':.4,'command':['/unused/vllm','--port','9001','--gpu-memory-utilization','.4'],
            'launcher_path':'/unused/launcher','launcher_sha256':'0'*64,'launcher_config_path':'/unused/launcher.json',
            'launcher_config_sha256':'0'*64,'migration_command':['/unused/migrate'],'manifest_sha256':'0'*64,
            'base_config_sha256':'0'*64,'target_config_sha256':'0'*64,'timeout_seconds':15}


@pytest.mark.parametrize('flags',[['--bootstrap-default'],['--bootstrap-recover','observe'],['--bootstrap-recover','rollback']])
def test_bootstrap_dryrun_does_not_construct_sources_or_database(tmp_path,monkeypatch,capsys,flags):
    database=tmp_path/'missing.sqlite'
    config=SchedulerConfig('127.0.0.1',8011,state_db_path=str(database),bootstrap=settings())
    monkeypatch.setattr(entry,'load_config',lambda path:config)
    monkeypatch.setattr(entry,'build_collector',lambda cfg:pytest.fail('preview constructed collector'))
    monkeypatch.setattr(entry,'IntentStore',lambda *args,**kwargs:pytest.fail('preview opened DB'))
    monkeypatch.setattr('sys.argv',['llmsvc','--config','unused','--dry-run',*flags])
    assert entry.main()==0 and '"dry_run": true' in capsys.readouterr().out
    assert not database.exists()


def test_occupied_authority_refuses_bootstrap_before_actor_construction(tmp_path,monkeypatch):
    import llmsvc.bootstrap as module
    database=tmp_path/'state.sqlite';IntentStore(database,action_lock=threading.RLock()).close()
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        config=SchedulerConfig('127.0.0.1',occupied.getsockname()[1],read_only=False,state_db_path=str(database),bootstrap=settings())
        monkeypatch.setattr(entry,'load_config',lambda path:config)
        monkeypatch.setattr(entry,'build_collector',lambda cfg:lambda:StateSnapshot())
        monkeypatch.setattr(entry,'build_event_relay',lambda cfg:None)
        monkeypatch.setattr(entry,'build_registry',lambda cfg,s:None)
        monkeypatch.setattr(module,'BootstrapController',lambda *args:pytest.fail('occupied port allowed bootstrap'))
        monkeypatch.setattr('sys.argv',['llmsvc','--config','unused','--bootstrap-default'])
        with pytest.raises(SystemExit):entry.main()
    store=IntentStore(database,action_lock=threading.RLock(),read_only=True)
    assert store.bootstrap_checkpoint() is None and not store.leases();store.close()
