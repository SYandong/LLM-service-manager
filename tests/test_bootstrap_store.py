# Generated-By: Codex / gpt-6-astra
"""Schema7 claim/real lease insertion atomicity; no bootstrap launch claim here."""
import hashlib
import threading
from dataclasses import replace

import pytest

from llmsvc import bootstrap_state as state
from llmsvc.state import Lease, Pin
from llmsvc.store import IntentStore


def record():
    return {'id':'1'*32,'profile_sha256':'2'*64,'model':'default','unit':'vllm-default.service','util':.4,
            'actor':{'pid':123,'start_ticks':'456'},'token_sha256':'3'*64,'stage':'claimed','lease_id':None,
            'migration':{},'effects':{},'error':None,'catalog_present':False}


def staging(store):
    value=state.claim(store,record())
    for phase in ('stage_submitted','staged','placing'):
        value=state.save(store,value,{**value,'stage':phase})
    return value


def test_lazy_schema_preserves_intents_and_fences_restart(tmp_path):
    path=tmp_path/'state.sqlite';lock=threading.RLock()
    store=IntentStore(path,action_lock=lock)
    store.put_pin(Pin('default',10000,'owner'))
    assert store._db.execute('PRAGMA user_version').fetchone()[0]==2
    state.claim(store,record())
    assert store._db.execute('PRAGMA user_version').fetchone()[0]==7
    assert store.active(1)[0]==(Pin('default',10000,'owner'),)
    store.close();store=IntentStore(path,action_lock=lock)
    assert store.bootstrap_pending() and not store.bootstrap_authorized()
    with pytest.raises(ValueError,match='bootstrap reconciliation'):
        store.remove_pin('default')
    with pytest.raises(ValueError,match='bootstrap reconciliation'):
        store.create_lease(Lease('real','default',0,.4,10000,40),'vllm-default.service')
    store.close()


def test_bootstrap_scope_creates_and_attaches_the_same_lease_atomically(tmp_path):
    store=IntentStore(tmp_path/'state.sqlite',action_lock=threading.RLock())
    staging(store)
    lease=Lease('real','default',0,.4,10000,40)
    with store.bootstrap_scope('1'*32):
        store.create_lease(lease,'vllm-default.service')
    assert store.bootstrap_checkpoint()['lease_id']=='real'
    assert store.bootstrap_checkpoint()['stage']=='placed'
    assert store.lease('real')==(lease,'vllm-default.service')
    with pytest.raises(ValueError):
        store.transition_lease('real','confirmed')
    with store.bootstrap_scope('1'*32):
        store.transition_lease('real','confirmed')
    assert store.lease('real')[0].budget_gb==40
    assert store.bootstrap_pending()
    store.close()


def test_failed_attachment_rolls_back_lease_insert(tmp_path):
    store=IntentStore(tmp_path/'state.sqlite',action_lock=threading.RLock())
    expected=staging(store)
    store._db.execute("CREATE TRIGGER fail_attach BEFORE UPDATE ON llmsvc_bootstrap BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END")
    with store.bootstrap_scope('1'*32),pytest.raises(Exception,match='fixture write failure'):
        store.create_lease(Lease('real','default',0,.4,10000,40),'vllm-default.service')
    assert store.leases()==() and store.bootstrap_checkpoint()==expected
    store.close()


@pytest.mark.parametrize('name,unit,util',[('foreign','vllm-foreign.service',.4),('default','vllm-other.service',.4),('default','vllm-default.service',.5)])
def test_bootstrap_capability_cannot_allocate_another_target(tmp_path,name,unit,util):
    store=IntentStore(tmp_path/'state.sqlite',action_lock=threading.RLock());staging(store)
    with store.bootstrap_scope('1'*32),pytest.raises(ValueError,match='not bound'):
        store.create_lease(Lease('real',name,0,util,10000,40),unit)
    assert not store.leases()
    store.close()


def test_readonly_does_not_create_or_migrate_bootstrap_state(tmp_path):
    path=tmp_path/'state.sqlite'
    IntentStore(path,action_lock=threading.RLock()).close()
    before=path.read_bytes()
    store=IntentStore(path,action_lock=threading.RLock(),read_only=True)
    with pytest.raises(PermissionError):state.claim(store,record())
    assert not store.bootstrap_pending();store.close()
    assert path.read_bytes()==before



def test_recovery_invalidates_prior_thread_capability(tmp_path):
    store=IntentStore(tmp_path/'state.sqlite',action_lock=threading.RLock());value=staging(store)
    with store.bootstrap_scope(value['id']):
        assert store.bootstrap_authorized()
        state.reclaim(store,value,value['actor'],'4'*64)
        assert not store.bootstrap_authorized()
        with pytest.raises(ValueError,match='bootstrap reconciliation'):
            store.create_lease(Lease('old','default',0,.4,10000,40),'vllm-default.service')
    assert not store.leases();store.close()


def test_corrupt_terminal_claim_cannot_silently_clear_fence(tmp_path):
    import json
    path=tmp_path/'state.sqlite';store=IntentStore(path,action_lock=threading.RLock());value=state.claim(store,record())
    store._db.execute('UPDATE llmsvc_bootstrap SET record=?',(json.dumps({**value,'stage':'complete'}),));store._db.commit();store.close()
    with pytest.raises(ValueError,match='terminal state lacks proof'):
        IntentStore(path,action_lock=threading.RLock())
