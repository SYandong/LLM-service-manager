# Generated-By: Codex / gpt-6-astra
"""Durable first-default bootstrap fence; never a synthetic model allocation."""
import json
import re
from contextlib import contextmanager

PHASES = ('claimed','stage_submitted','staged','placing','placed','start_submitted',
          'start_acknowledged','health_observed','confirm_submitted','default_confirmed',
          'activate_submitted','complete','aborted')


def encoded(record):
    expected = {'id','profile_sha256','model','unit','util','actor','token_sha256','stage',
                'lease_id','migration','effects','error','catalog_present'}
    if not isinstance(record,dict) or set(record)!=expected or record['stage'] not in PHASES or type(record['catalog_present']) is not bool:
        raise ValueError('invalid bootstrap claim')
    for key,size in (('id',32),('profile_sha256',64),('token_sha256',64)):
        if not isinstance(record[key],str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}',record[key]):
            raise ValueError('invalid bootstrap identity')
    if (not isinstance(record['model'],str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*',record['model'])
            or record['unit']!='vllm-'+record['model']+'.service'
            or type(record['util']) not in (int,float) or not 0<record['util']<=1):
        raise ValueError('invalid bootstrap target')
    actor=record['actor']
    if (not isinstance(actor,dict) or set(actor)!={'pid','start_ticks'} or type(actor['pid']) is not int or actor['pid']<=0
            or not isinstance(actor['start_ticks'],str) or not re.fullmatch('[0-9]{1,32}',actor['start_ticks'])
            or str(int(actor['start_ticks']))!=actor['start_ticks']):
        raise ValueError('invalid bootstrap actor')
    if record['lease_id'] is not None and (not isinstance(record['lease_id'],str) or not 1<=len(record['lease_id'])<=128):
        raise ValueError('invalid bootstrap lease')
    if not isinstance(record['effects'],dict) or not isinstance(record['migration'],dict):
        raise ValueError('invalid bootstrap progress')
    for key,value in record['effects'].items():
        if key not in ('stage','launch','activate','rollback') or value not in ('submitted','acknowledged'):
            raise ValueError('invalid bootstrap effect')
    if record['error'] is not None and (not isinstance(record['error'],str) or len(record['error'])>256):
        raise ValueError('invalid bootstrap error')
    if record['stage']=='complete':
        proof=record['migration'].get('activated')
        if (record['lease_id'] is None or record['effects'].get('activate') not in ('submitted','acknowledged')
                or 'launch' not in record['effects'] or not isinstance(proof,dict) or proof.get('active_ready') is not True):
            raise ValueError('bootstrap terminal state lacks proof')
    if record['stage']=='aborted' and record['effects']:
        proof=record['migration'].get('rolled_back')
        if record['effects'].get('rollback')!='acknowledged' or not isinstance(proof,dict) or proof.get('rolled_back') is not True:
            raise ValueError('bootstrap abort lacks rollback proof')
    raw=json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(raw.encode())>262144:raise ValueError('bootstrap claim exceeds limit')
    return raw


def read(store):
    if not store._has_bootstrap:return None
    rows=store._db.execute('SELECT record FROM llmsvc_bootstrap WHERE singleton=1').fetchall()
    if len(rows)>1:raise ValueError('duplicate bootstrap claim')
    if not rows:return None
    record=json.loads(rows[0][0]);encoded(record)
    return record


def pending(store):
    record=read(store)
    return record is not None and record['stage'] not in ('complete','aborted')


def authorized(store):
    record=read(store)
    return record is not None and getattr(store._bootstrap_local,'owner',None)==(record['id'],record['token_sha256'])


@contextmanager
def scope(store,owner):
    record=read(store)
    if record is None or record['id']!=owner:raise ValueError('bootstrap owner changed')
    previous=getattr(store._bootstrap_local,'owner',None)
    store._bootstrap_local.owner=(owner,record['token_sha256'])
    try:yield
    finally:store._bootstrap_local.owner=previous


def claim(store,record):
    raw=encoded(record)
    with store.action_lock:
        if store.read_only:raise PermissionError('intent store is read-only')
        if read(store) is not None or store.catalog_checkpoint() is not None or store.leases() or store.faults() or store.recoveries():
            raise ValueError('bootstrap needs a new unallocated topology')
        if record['stage']!='claimed' or record['lease_id'] is not None or record['effects'] or record['catalog_present']:
            raise ValueError('bootstrap must begin before effects')
        with store._db:
            store._db.execute('BEGIN IMMEDIATE')
            if store.leases():raise ValueError('bootstrap allocation changed')
            store._db.execute("CREATE TABLE IF NOT EXISTS llmsvc_faults (lease_id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL CHECK(stage IN ('claimed','released','complete')), record TEXT NOT NULL)")
            store._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_fault ON llmsvc_faults(model) WHERE stage != 'complete'")
            store._db.execute('CREATE TABLE IF NOT EXISTS llmsvc_recoveries (id TEXT PRIMARY KEY, model TEXT NOT NULL, stage TEXT NOT NULL, record TEXT NOT NULL)')
            store._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS llmsvc_one_recovery ON llmsvc_recoveries(model) WHERE stage != 'complete'")
            store._db.execute('CREATE TABLE IF NOT EXISTS llmsvc_catalog (singleton INTEGER PRIMARY KEY CHECK(singleton=1), record TEXT NOT NULL)')
            store._db.execute('CREATE TABLE IF NOT EXISTS llmsvc_maintenance (transaction_id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            store._db.execute('CREATE TABLE IF NOT EXISTS llmsvc_bootstrap (singleton INTEGER PRIMARY KEY CHECK(singleton=1), record TEXT NOT NULL)')
            store._db.execute('INSERT INTO llmsvc_bootstrap VALUES (1,?)',(raw,))
            store._db.execute('PRAGMA user_version=7')
        store._has_bootstrap=store._has_maintenance=store._has_catalog=store._has_recoveries=store._has_faults=True
    return record


def save(store,expected,record):
    raw=encoded(record)
    for key in set(record)-{'stage','lease_id','migration','effects','error','catalog_present'}:
        if expected[key]!=record[key]:raise ValueError('bootstrap identity changed')
    if expected['lease_id']!=record['lease_id']:
        raise ValueError('bootstrap lease changed')
    before,after=expected['stage'],record['stage']
    recovered_health = (before=='start_submitted' and after=='health_observed'
                        and isinstance(record['migration'].get('default_observed'),dict))
    if before!=after and not recovered_health and (before in ('complete','aborted') or after!='aborted' and PHASES.index(after)!=PHASES.index(before)+1):
        raise ValueError('bootstrap stage transition rejected')
    for key,value in expected['effects'].items():
        if key not in record['effects'] or value=='acknowledged' and record['effects'][key]!=value:
            raise ValueError('bootstrap effect receipt regressed')
    for key,value in expected['migration'].items():
        if record['migration'].get(key)!=value:raise ValueError('bootstrap migration receipt changed')
    with store.action_lock:
        if store.read_only:raise PermissionError('intent store is read-only')
        if read(store)!=expected:raise ValueError('bootstrap checkpoint changed')
        if after=='aborted':
            if store.leases():raise ValueError('bootstrap accounts require observed release')
            if record['effects'] and record['effects'].get('rollback')!='acknowledged':
                raise ValueError('bootstrap effects require verified migration rollback')
        if after=='complete':
            row=store.lease(record['lease_id'])
            if row is None or row[0].status!='confirmed' or row[0].model!=record['model'] or row[1]!=record['unit']:
                raise ValueError('bootstrap completion lacks confirmed account')
        with store._db:
            store._db.execute('BEGIN IMMEDIATE')
            if read(store)!=expected:raise ValueError('bootstrap changed before commit')
            store._db.execute('UPDATE llmsvc_bootstrap SET record=? WHERE singleton=1',(raw,))
    return record


def insert_lease(store,lease,unit):
    from dataclasses import asdict
    record=read(store)
    if (not authorized(store) or record['stage']!='placing' or record['lease_id'] is not None
            or (lease.model,unit,lease.util)!=(record['model'],record['unit'],record['util'])):
        raise ValueError('bootstrap placement is not bound to its target')
    if store.read_only:raise PermissionError('intent store is read-only')
    attached={**record,'stage':'placed','lease_id':lease.lease_id}
    with store._db:
        store._db.execute('BEGIN IMMEDIATE')
        if read(store)!=record:raise ValueError('bootstrap changed before lease')
        store._db.execute('INSERT INTO llmsvc_leases VALUES (?,?,?,?,?,?,?,?)',(*asdict(lease).values(),unit))
        store._db.execute('UPDATE llmsvc_bootstrap SET record=? WHERE singleton=1',(encoded(attached),))


def reclaim(store,expected,actor,token_sha256):
    record={**expected,'actor':actor,'token_sha256':token_sha256}
    raw=encoded(record)
    with store.action_lock,store._db:
        if store.read_only:raise PermissionError('intent store is read-only')
        store._db.execute('BEGIN IMMEDIATE')
        if read(store)!=expected:raise ValueError('bootstrap changed during owner recovery')
        store._db.execute('UPDATE llmsvc_bootstrap SET record=? WHERE singleton=1',(raw,))
    return record
